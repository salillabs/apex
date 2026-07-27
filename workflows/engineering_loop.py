"""
Engineering Loop — LangGraph StateGraph wiring the APEX agent pipeline.
Thin wiring only: no business logic here. All intelligence lives in agent functions.
"""
import operator
import uuid
from typing import Literal
from typing_extensions import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import agents.architect as architect
import agents.developer as dev
import agents.engineering_manager as em
import agents.qa as qa_agent
import agents.reporter as reporter
import agents.reviewer as reviewer_agent
import services.github as github_service
import services.db as db_service
import services.slack as slack_service
from models.domain import ImplementationReport, Issue, Plan, ReviewResult, SimpleTask, TaskStatus, TechnicalSpec


# ── State ─────────────────────────────────────────────────────────────────────

class LoopState(TypedDict):
    run_id: str
    issue: dict           # Issue serialized via .model_dump(mode="json")
    plan: dict            # Plan context dict from engineering_manager
    spec: dict            # TechnicalSpec from architect (empty dict until architect runs)
    review: dict          # ReviewResult from reviewer (empty dict until reviewer runs)
    branch: str
    task_id: str
    repo: str
    status: str
    dev_failure_count: int
    result: dict          # latest agent result payload
    errors: Annotated[list, operator.add]
    messages: Annotated[list, operator.add]


# ── Nodes ─────────────────────────────────────────────────────────────────────

def plan_node(state: LoopState) -> dict:
    """Engineering Manager: read issue, decide route, create branch."""
    issue = Issue.model_validate(state["issue"])
    plan = em.plan_issue(issue)
    assignment = em.assign(plan)

    task_id = f"task-{issue.number}-{issue.repo.replace('/', '-')}"
    db_service.save_task(
        task_id=task_id,
        issue_number=issue.number,
        repo=issue.repo,
        title=issue.title,
        status=TaskStatus.PLANNING,
        branch=assignment.branch,
    )
    return {
        "plan": plan.context,
        "branch": assignment.branch,
        "task_id": task_id,
        "repo": issue.repo,
        "status": TaskStatus.PLANNING,
        "messages": [f"Planned issue #{issue.number}: {plan.context.get('action', 'unknown')}"],
    }


def architect_node(state: LoopState) -> dict:
    """Architect: generate a complete technical spec for the Developer."""
    issue = Issue.model_validate(state["issue"])
    plan_data = state["plan"]

    plan = Plan(
        issue=issue,
        summary=plan_data.get("instruction") or plan_data.get("context") or "",
        needs_architect=True,
        priority="normal",
        urgency=plan_data.get("urgency", "normal"),
        context=plan_data,
    )

    risk = architect.analyze_impact(plan)
    spec = architect.generate_spec(plan)

    needs_approval = architect.requires_approval(spec, risk)

    return {
        "spec": spec.model_dump(mode="json"),
        "result": {
            "spec": spec.model_dump(mode="json"),
            "requires_approval": needs_approval,
            "approval_reason": spec.summary if needs_approval else None,
        },
        "status": TaskStatus.PLANNING,
        "messages": [f"Architect spec ready. Requires approval: {needs_approval}"],
    }


def architect_approval_node(state: LoopState) -> dict:
    """Interrupt: ask user to approve the architect's spec before implementation."""
    spec_data = state["result"].get("spec", {})
    approval_reason = state["result"].get("approval_reason", "Spec requires your approval")

    decision = interrupt({
        "type": "architect_approval",
        "task_id": state["task_id"],
        "approval_reason": approval_reason,
        "spec_summary": spec_data.get("summary", ""),
        "options": ["approved", "rejected"],
    })

    return {
        "result": {**state["result"], "human_decision": decision},
        "status": TaskStatus.IN_PROGRESS if decision == "approved" else TaskStatus.ABANDONED,
        "messages": [f"User decision on architect spec: {decision}"],
    }


def developer_node(state: LoopState) -> dict:
    """Developer: implement the spec or trivial task via Claude Code."""
    issue = Issue.model_validate(state["issue"])
    plan = state["plan"]
    action = plan.get("action", "")

    if action == "route_to_developer" and plan.get("task_type") == "trivial":
        task = SimpleTask(issue=issue, description=plan.get("instruction", ""), branch=state["branch"])
        report = dev.implement_simple(task)
    elif state.get("spec"):
        spec = TechnicalSpec.model_validate(state["spec"])
        report = dev.implement(spec)
    else:
        spec_data = state["result"].get("spec") or {
            "issue": state["issue"],
            "summary": plan.get("context", plan.get("instruction", "")),
            "files_to_create": [], "files_to_modify": [], "api_changes": [],
            "database_changes": [], "env_vars": [], "dependencies": [],
            "test_requirements": [], "risks": [], "requires_approval": False,
        }
        spec = TechnicalSpec.model_validate(spec_data)
        report = dev.implement(spec)

    failure_count = state.get("dev_failure_count", 0) + (0 if report.success else 1)
    return {
        "result": report.model_dump(mode="json"),
        "dev_failure_count": failure_count,
        "status": TaskStatus.TESTING if report.success else TaskStatus.FAILED,
        "messages": [f"Developer {'succeeded' if report.success else 'failed'}: {report.summary[:100]}"],
    }


def qa_run_node(state: LoopState) -> dict:
    """QA: run the full test suite and record results."""
    report = ImplementationReport.model_validate(state["result"])
    test_result = qa_agent.run_tests(report)

    return {
        "result": test_result.model_dump(mode="json"),
        "status": TaskStatus.REVIEWING if test_result.success else TaskStatus.FAILED,
        "messages": [f"QA: {test_result.tests_passed}/{test_result.tests_run} tests passing"],
    }


def qa_gate_node(state: LoopState) -> dict:
    """Interrupt for human decision when tests fail."""
    from models.domain import TestResult
    test_result = TestResult.model_validate(state["result"])
    bug_report = qa_agent.write_bug_report(test_result)

    slack_service.send_bug_report(bug_report)

    decision = interrupt({
        "type": "qa_failure",
        "task_id": state["task_id"],
        "summary": bug_report.summary,
        "failures": [f.get("plain_english", "") for f in test_result.failures],
        "options": bug_report.options,
    })

    return {
        "result": {**state["result"], "human_decision": decision},
        "status": TaskStatus.IN_PROGRESS if decision == "retry_fix" else TaskStatus.ABANDONED,
        "messages": [f"User decided on QA failure: {decision}"],
    }


def dev_failure_gate_node(state: LoopState) -> dict:
    """Interrupt for human when developer has failed twice."""
    error_summary = state["result"].get("error", "No details available")
    decision = interrupt({
        "type": "dev_failure",
        "task_id": state["task_id"],
        "attempt": state.get("dev_failure_count", 2),
        "error": error_summary,
        "options": ["retry", "abandon"],
    })
    return {
        "result": {**state["result"], "human_decision": decision},
        "status": TaskStatus.IN_PROGRESS if decision == "retry" else TaskStatus.ABANDONED,
        "messages": [f"User decided on dev failure: {decision}"],
    }


def pr_create_node(state: LoopState) -> dict:
    """Create the GitHub PR after tests pass (idempotent — reuses existing PR for branch)."""
    existing_pr = github_service.get_open_pr_for_branch(state["repo"], state["branch"])
    if existing_pr:
        return {
            "result": {**state["result"], "pr": existing_pr},
            "status": TaskStatus.REVIEWING,
            "messages": [f"Reusing existing PR #{existing_pr['number']}"],
        }

    branch_status = github_service.get_branch_status(state["repo"], state["branch"])
    if not branch_status.get("exists"):
        return {
            "result": {**state["result"], "error": f"Branch {state['branch']} not found"},
            "status": TaskStatus.FAILED,
            "errors": [f"Branch {state['branch']} missing — cannot create PR"],
        }

    issue = Issue.model_validate(state["issue"])
    pr = github_service.create_pr(
        repo=state["repo"],
        title=f"[APEX] {issue.title}",
        body=(
            f"Resolves #{issue.number}\n\n"
            f"Implemented autonomously by APEX.\n\n"
            f"**Files changed:** {', '.join(state['result'].get('files_changed', []))}"
        ),
        branch=state["branch"],
    )
    return {
        "result": {**state["result"], "pr": pr},
        "status": TaskStatus.REVIEWING,
        "messages": [f"PR #{pr['number']} created: {pr['url']}"],
    }


def reviewer_node(state: LoopState) -> dict:
    """Reviewer: read PR diff against spec, approve or request changes."""
    from models.domain import TestResult
    pr = state["result"].get("pr", {})
    pr_number = pr.get("number")

    if not pr_number:
        return {
            "review": {"approved": True, "summary": "No PR to review — proceeding.", "issues_found": [], "pr_number": 0, "pr_url": ""},
            "status": TaskStatus.REVIEWING,
            "messages": ["Reviewer skipped — no PR number available"],
        }

    result_data = state["result"]
    test_result = TestResult.model_validate({
        "branch": state["branch"],
        "tests_run": result_data.get("tests_run", 0),
        "tests_passed": result_data.get("tests_passed", 0),
        "tests_failed": result_data.get("tests_failed", 0),
        "failures": result_data.get("failures", []),
        "success": result_data.get("success", True),
    })

    review = reviewer_agent.review_pr(
        test_result=test_result,
        repo=state["repo"],
        pr_number=pr_number,
        task_id=state["task_id"],
    )

    return {
        "review": review.model_dump(mode="json"),
        "result": {**state["result"], "review": review.model_dump(mode="json")},
        "status": TaskStatus.REVIEWING if review.approved else TaskStatus.IN_PROGRESS,
        "messages": [f"Reviewer: {'approved' if review.approved else 'changes requested'} — {review.summary[:80]}"],
    }


def merge_approval_node(state: LoopState) -> dict:
    """Interrupt: ask user to approve or reject the merge."""
    pr = state["result"].get("pr", {})
    review_data = state.get("review", {})

    reporter.report_pr_ready(
        ReviewResult.model_validate({
            "pr_number": pr.get("number", 0),
            "pr_url": pr.get("url", ""),
            "approved": True,
            "issues_found": review_data.get("issues_found", []),
            "summary": review_data.get("summary", f"PR #{pr.get('number')} ready to merge"),
        })
    )

    decision = interrupt({
        "type": "merge_approval",
        "task_id": state["task_id"],
        "pr_number": pr.get("number"),
        "pr_url": pr.get("url"),
        "pr_title": pr.get("title"),
    })
    return {
        "result": {**state["result"], "merge_decision": decision},
        "status": TaskStatus.MERGED if decision == "approved" else TaskStatus.ABANDONED,
        "messages": [f"User merge decision: {decision}"],
    }


def merge_node(state: LoopState) -> dict:
    """Merge the PR."""
    pr = state["result"].get("pr", {})
    github_service.merge_pr(state["repo"], pr["number"])
    return {
        "status": TaskStatus.MERGED,
        "messages": [f"Merged PR #{pr['number']}"],
    }


def reporter_node(state: LoopState) -> dict:
    """Reporter: post completion summary to Slack after merge."""
    pr = state["result"].get("pr", {})
    issue = Issue.model_validate(state["issue"])
    summary = f"PR #{pr.get('number')} merged — {issue.title}"
    reporter.report_completion(task_id=state["task_id"], summary=summary)
    return {
        "messages": [f"Reported completion for {state['task_id']}"],
    }


# ── Routers ───────────────────────────────────────────────────────────────────

def route_after_plan(state: LoopState) -> Literal["architect_node", "developer_node", "__end__"]:
    action = state["plan"].get("action", "")
    if action == "route_to_architect":
        return "architect_node"
    if action == "route_to_developer":
        return "developer_node"
    return END


def route_after_architect(
    state: LoopState,
) -> Literal["developer_node", "architect_approval_node"]:
    if state["result"].get("requires_approval"):
        return "architect_approval_node"
    return "developer_node"


def route_after_architect_approval(
    state: LoopState,
) -> Literal["developer_node", "__end__"]:
    decision = state["result"].get("human_decision", "")
    return "developer_node" if decision == "approved" else END


def route_after_developer(
    state: LoopState,
) -> Literal["qa_run_node", "developer_node", "dev_failure_gate_node"]:
    if state["result"].get("success"):
        return "qa_run_node"
    if state.get("dev_failure_count", 0) < 2:
        return "developer_node"
    return "dev_failure_gate_node"


def route_after_qa_run(state: LoopState) -> Literal["pr_create_node", "qa_gate_node"]:
    return "pr_create_node" if state["status"] == TaskStatus.REVIEWING else "qa_gate_node"


def route_after_qa_gate(state: LoopState) -> Literal["developer_node", "__end__"]:
    decision = state["result"].get("human_decision", "")
    return "developer_node" if decision == "retry_fix" else END


def route_after_dev_failure_gate(state: LoopState) -> Literal["developer_node", "__end__"]:
    decision = state["result"].get("human_decision", "")
    return "developer_node" if decision == "retry" else END


def route_after_reviewer(
    state: LoopState,
) -> Literal["merge_approval_node", "developer_node"]:
    review = state.get("review", {})
    return "merge_approval_node" if review.get("approved", True) else "developer_node"


def route_after_merge_approval(state: LoopState) -> Literal["merge_node", "__end__"]:
    return "merge_node" if state["result"].get("merge_decision") == "approved" else END


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_graph(checkpointer) -> object:
    """Compile and return the engineering loop graph."""
    builder = StateGraph(LoopState)

    builder.add_node("plan_node", plan_node)
    builder.add_node("architect_node", architect_node)
    builder.add_node("architect_approval_node", architect_approval_node)
    builder.add_node("developer_node", developer_node)
    builder.add_node("dev_failure_gate_node", dev_failure_gate_node)
    builder.add_node("qa_run_node", qa_run_node)
    builder.add_node("qa_gate_node", qa_gate_node)
    builder.add_node("pr_create_node", pr_create_node)
    builder.add_node("reviewer_node", reviewer_node)
    builder.add_node("merge_approval_node", merge_approval_node)
    builder.add_node("merge_node", merge_node)
    builder.add_node("reporter_node", reporter_node)

    builder.add_edge(START, "plan_node")
    builder.add_conditional_edges("plan_node", route_after_plan)
    builder.add_conditional_edges("architect_node", route_after_architect)
    builder.add_conditional_edges("architect_approval_node", route_after_architect_approval)
    builder.add_conditional_edges("developer_node", route_after_developer)
    builder.add_conditional_edges("dev_failure_gate_node", route_after_dev_failure_gate)
    builder.add_conditional_edges("qa_run_node", route_after_qa_run)
    builder.add_conditional_edges("qa_gate_node", route_after_qa_gate)
    builder.add_edge("pr_create_node", "reviewer_node")
    builder.add_conditional_edges("reviewer_node", route_after_reviewer)
    builder.add_conditional_edges("merge_approval_node", route_after_merge_approval)
    builder.add_edge("merge_node", "reporter_node")
    builder.add_edge("reporter_node", END)

    return builder.compile(checkpointer=checkpointer)


# ── Entry points ──────────────────────────────────────────────────────────────

async def start_issue(graph, issue: Issue) -> str:
    """Kick off a new engineering loop for a GitHub issue. Returns the run_id."""
    run_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": run_id}}
    initial: LoopState = {
        "run_id": run_id,
        "issue": issue.model_dump(mode="json"),
        "plan": {},
        "spec": {},
        "review": {},
        "branch": "",
        "task_id": "",
        "repo": issue.repo,
        "status": TaskStatus.NEW,
        "dev_failure_count": 0,
        "result": {},
        "errors": [],
        "messages": [],
    }
    async for chunk in graph.astream(initial, config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            break
    return run_id


async def resume_with_decision(graph, run_id: str, decision: str) -> None:
    """Resume a paused graph with a human decision."""
    config = {"configurable": {"thread_id": run_id}}
    async for _ in graph.astream(Command(resume=decision), config, stream_mode="updates"):
        pass
