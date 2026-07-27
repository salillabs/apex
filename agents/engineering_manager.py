"""
Engineering Manager — reads GitHub Issues, routes work, tracks progress, talks to the user.
See docs/contracts/engineering_manager.md for the full contract.
"""
import json
import re
import uuid
from pathlib import Path

from models.domain import (
    AgentResult,
    ApprovalRequest,
    Assignment,
    Issue,
    Plan,
    Priority,
    TaskStatus,
)
import services.claude_code as claude_code
import services.github as github_service
import services.db as db_service
import services.slack as slack_service

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "engineering_manager.md").read_text(encoding="utf-8")


def _task_id(issue: Issue) -> str:
    slug = issue.repo.replace("/", "-").replace("_", "-")
    return f"task-{issue.number}-{slug}"


def _branch_name(issue: Issue) -> str:
    words = [w for w in re.sub(r"[^a-z0-9 ]", "", issue.title.lower()).split()][:3]
    return f"feature/issue-{issue.number}-{'-'.join(words)}"


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of Claude's response."""
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No valid JSON in Claude response: {text[:200]}")


def plan_issue(issue: Issue) -> Plan:
    """Read a GitHub Issue and produce a work plan with routing decision."""
    portfolio = db_service.get_portfolio()
    portfolio_summary = [
        {"task_id": t.id, "repo": t.repo, "status": t.status, "title": t.title}
        for t in portfolio
    ]

    user_prompt = json.dumps({
        "type": "new_issue",
        "issue": {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "labels": issue.labels,
            "repo": issue.repo,
        },
        "portfolio": portfolio_summary,
    })

    raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
    decision = _extract_json(raw)
    action = decision.get("action", "route_to_architect")

    priority_map = {"high": Priority.HIGH, "low": Priority.LOW}
    priority = priority_map.get(decision.get("urgency", ""), Priority.NORMAL)

    task_id = _task_id(issue)
    db_service.save_task(
        task_id=task_id,
        issue_number=issue.number,
        repo=issue.repo,
        title=issue.title,
        status=TaskStatus.PLANNING,
        priority=priority.value,
        branch=_branch_name(issue),
    )
    db_service.save_decision(
        task_id=task_id,
        decision_type="route",
        decision=action,
        rationale=json.dumps(decision),
    )

    return Plan(
        issue=issue,
        summary=decision.get("instruction") or decision.get("context") or decision.get("reason") or "",
        needs_architect=(action == "route_to_architect"),
        priority=priority,
        urgency=decision.get("urgency", "normal"),
        context=decision,
    )


def assign(plan: Plan) -> Assignment:
    """Route the plan to Architect or Developer and create the feature branch."""
    branch = _branch_name(plan.issue)

    try:
        github_service.create_branch(plan.issue.repo, branch)
    except Exception:
        pass  # Branch may already exist

    agent = "architect" if plan.needs_architect else "developer"
    db_service.update_task(_task_id(plan.issue), status=TaskStatus.IN_PROGRESS, branch=branch)

    return Assignment(
        issue=plan.issue,
        agent=agent,
        branch=branch,
        instructions=plan.summary,
        context=plan.context,
    )


def prioritize(issues: list[Issue]) -> list[Issue]:
    """Order multiple in-flight issues by urgency and project priority."""
    if len(issues) <= 1:
        return issues

    user_prompt = json.dumps({
        "type": "prioritize",
        "issues": [
            {"number": i.number, "title": i.title, "labels": i.labels, "repo": i.repo}
            for i in issues
        ],
    })

    try:
        raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
        order = _extract_json(raw)
        ordered_numbers = order.get("order", [i.number for i in issues])
        issue_map = {i.number: i for i in issues}
        return [issue_map[n] for n in ordered_numbers if n in issue_map]
    except Exception:
        return issues


def escalate_to_human(reason: str, context: dict) -> ApprovalRequest:
    """Post an approval request to Slack and persist it to Neon."""
    approval_id = str(uuid.uuid4())
    task_id = context.get("task_id", "unknown")
    options = context.get("options", ["approve", "reject"])
    summary = context.get("summary", reason)

    raw_issue = context.get("issue")
    issue: Issue | None = None
    if isinstance(raw_issue, Issue):
        issue = raw_issue
    elif isinstance(raw_issue, dict) and raw_issue:
        issue = Issue.model_validate(raw_issue)

    request = ApprovalRequest(id=approval_id, issue=issue, reason=reason, summary=summary, options=options)

    channel = slack_service.default_channel()
    ts = slack_service.send_approval_request(request, channel)

    db_service.save_approval(
        approval_id=approval_id,
        task_id=task_id,
        reason=reason,
        options=options,
        slack_channel=channel,
        slack_ts=ts,
    )
    db_service.update_task(task_id, status=TaskStatus.AWAITING_APPROVAL)
    return request


def handle_approval(approval_request_id: str, user_response: str) -> AgentResult:
    """Process the user's approve/reject decision and return next routing."""
    approval = db_service.get_approval(approval_request_id)
    if approval is None:
        return AgentResult(
            success=False, next_agent=None, payload={},
            error=f"Approval {approval_request_id} not found",
        )

    db_service.update_approval(approval_request_id, decision=user_response)

    if user_response == "approved":
        db_service.update_task(approval.task_id, status=TaskStatus.IN_PROGRESS)
        return AgentResult(success=True, next_agent="developer", payload={"task_id": approval.task_id})

    db_service.update_task(approval.task_id, status=TaskStatus.ABANDONED)
    return AgentResult(success=True, next_agent=None, payload={"task_id": approval.task_id, "outcome": "abandoned"})


def handle_agent_result(result: AgentResult) -> AgentResult:
    """Receive any agent result and decide the next workflow step."""
    payload = result.payload
    task_id = payload.get("task_id", "")
    from_agent = payload.get("from_agent", "")

    user_prompt = json.dumps({
        "type": "agent_result",
        "from_agent": from_agent,
        "task_id": task_id,
        "status": "success" if result.success else "failure",
        "payload": payload,
    })

    raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
    decision = _extract_json(raw)
    action = decision.get("action", "")

    db_service.save_decision(task_id=task_id, decision_type="routing", decision=action, rationale=json.dumps(decision))

    routing: dict[str, tuple[str | None, str]] = {
        "route_to_qa":       ("qa",         TaskStatus.TESTING),
        "route_to_reviewer": ("reviewer",   TaskStatus.REVIEWING),
        "route_to_developer":("developer",  TaskStatus.IN_PROGRESS),
        "route_to_architect":("architect",  TaskStatus.PLANNING),
    }

    if action in routing:
        next_agent, status = routing[action]
        db_service.update_task(task_id, status=status)
        return AgentResult(success=True, next_agent=next_agent, payload={"task_id": task_id, **payload, "decision": decision})

    if action == "escalate_to_user":
        req = escalate_to_human(
            reason=decision.get("reason", "Decision required"),
            context={"task_id": task_id, "summary": decision.get("reason", ""), "options": ["approve", "reject"], **payload},
        )
        return AgentResult(success=True, next_agent=None, payload={"approval_id": req.id, "task_id": task_id})

    if action == "merge_pr":
        pr_number = decision.get("pr_number") or payload.get("pr_number")
        repo = payload.get("repo", "")
        if pr_number and repo:
            github_service.merge_pr(repo, int(pr_number))
        db_service.update_task(task_id, status=TaskStatus.MERGED)
        slack_service.send_text(f"✅ PR #{pr_number} merged for task `{task_id}`")
        return AgentResult(success=True, next_agent="reporter", payload={"task_id": task_id, **payload})

    if action == "abandon_task":
        db_service.update_task(task_id, status=TaskStatus.ABANDONED)
        return AgentResult(success=True, next_agent=None, payload={"task_id": task_id, "outcome": "abandoned"})

    # defer / close_duplicate / request_clarification — no further routing
    return AgentResult(success=True, next_agent=None, payload={"task_id": task_id, "decision": decision})
