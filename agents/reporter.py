"""
Reporter — writes every Slack message the user receives. Short, clear, actionable.
See docs/contracts/reporter.md for the full contract.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from models.domain import ApprovalRequest, BugReport, ReviewResult, SlackMessage
import services.claude_code as claude_code
import services.db as db_service
import services.slack as slack_service

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "reporter.md").read_text(encoding="utf-8")


def _extract_json(text: str) -> dict:
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No valid JSON in reporter response: {text[:200]}")


def _channel() -> str:
    return slack_service.default_channel()


def _send(msg: SlackMessage) -> SlackMessage:
    slack_service.send_message(msg.channel, msg.text, msg.blocks)
    return msg


def request_approval(request: ApprovalRequest) -> SlackMessage:
    """Format a strategic decision for user approval via Slack."""
    issue = request.issue
    project = issue.repo.split("/")[-1] if issue else "unknown"
    issue_ref = f"#{issue.number} {issue.title}" if issue else "unknown issue"

    user_prompt = json.dumps({
        "message_type": "approval_request",
        "project": project,
        "issue_number": issue.number if issue else None,
        "issue_title": issue.title if issue else "",
        "data": {
            "reason": request.reason,
            "summary": request.summary,
            "options": request.options,
            "approval_id": request.id,
        },
    })

    try:
        raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
        data = _extract_json(raw)
        channel = data.get("channel", _channel())
        text = data.get("text", f"{project}: approval needed for {issue_ref}")
        blocks = data.get("blocks")
    except Exception:
        channel = _channel()
        text = f"*{project}* — Decision needed\n\n*{issue_ref}*\n{request.summary}"
        blocks = None

    msg = SlackMessage(channel=channel, text=text, blocks=blocks)
    return _send(msg)


def report_bug(report: BugReport, repo: str | None = None) -> SlackMessage:
    """Format a test failure report with clear options for the user."""
    result = report.test_result
    project = repo.split("/")[-1] if repo else result.branch.split("/")[-1]

    user_prompt = json.dumps({
        "message_type": "bug_report",
        "project": project,
        "data": {
            "summary": report.summary,
            "failures": result.failures,
            "tests_failed": result.tests_failed,
            "tests_run": result.tests_run,
            "likely_causes": report.likely_causes,
        },
    })

    try:
        raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
        data = _extract_json(raw)
        channel = data.get("channel", _channel())
        text = data.get("text", f"Tests failing: {report.summary}")
        blocks = data.get("blocks")
    except Exception:
        channel = _channel()
        causes = "\n".join(f"• {c}" for c in report.likely_causes[:3])
        text = (
            f"⚠️ *{project}* — Tests failing\n{report.summary}\n"
            f"{result.tests_failed} of {result.tests_run} failed\n\n{causes}"
        )
        blocks = None

    msg = SlackMessage(channel=channel, text=text, blocks=blocks)
    return _send(msg)


def report_pr_ready(review: ReviewResult) -> SlackMessage:
    """Format a PR summary asking user to approve or reject the merge."""
    minor_count = sum(1 for i in review.issues_found if i.startswith("[minor]"))
    review_note = "Clean" if not review.issues_found else f"{minor_count} minor note{'s' if minor_count != 1 else ''}"

    user_prompt = json.dumps({
        "message_type": "pr_ready",
        "data": {
            "pr_number": review.pr_number,
            "pr_url": review.pr_url,
            "summary": review.summary,
            "review_note": review_note,
        },
    })

    try:
        raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
        data = _extract_json(raw)
        channel = data.get("channel", _channel())
        text = data.get("text", f"PR #{review.pr_number} ready to merge")
        blocks = data.get("blocks")
    except Exception:
        channel = _channel()
        text = (
            f"✅ PR ready to merge\n"
            f"PR #{review.pr_number}: {review.summary}\n"
            f"Review: {review_note}\n"
            f"{review.pr_url}"
        )
        blocks = None

    msg = SlackMessage(channel=channel, text=text, blocks=blocks)
    return _send(msg)


def report_completion(task_id: str, summary: str) -> SlackMessage:
    """Send a task completion notification. No action required from user."""
    task = db_service.get_task(task_id)
    project = task.repo.split("/")[-1] if task else "unknown"
    issue_number = task.issue_number if task else 0

    user_prompt = json.dumps({
        "message_type": "task_complete",
        "project": project,
        "issue_number": issue_number,
        "data": {"summary": summary, "task_id": task_id},
    })

    try:
        raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
        data = _extract_json(raw)
        channel = data.get("channel", _channel())
        text = data.get("text", f"{project}: task {task_id} complete")
        blocks = data.get("blocks")
    except Exception:
        channel = _channel()
        text = f"✅ *{project}* — Done\n\n#{issue_number} {summary}\n\nNo action needed."
        blocks = None

    msg = SlackMessage(channel=channel, text=text, blocks=blocks)
    return _send(msg)


def daily_digest(project_ids: list[str]) -> SlackMessage:
    """Produce a daily summary of all in-progress and completed work."""
    portfolio = db_service.get_portfolio()
    today = datetime.now(tz=timezone.utc).strftime("%A, %B %d").replace(" 0", " ")

    waiting = [t for t in portfolio if t.status == "awaiting_approval" or t.status == "awaiting_merge"]
    in_progress = [t for t in portfolio if t.status in ("in_progress", "planning", "testing", "reviewing")]

    user_prompt = json.dumps({
        "message_type": "daily_digest",
        "date": today,
        "data": {
            "waiting_on_user": [
                {"task_id": t.id, "repo": t.repo, "title": t.title, "status": t.status}
                for t in waiting[:5]
            ],
            "in_progress": [
                {"task_id": t.id, "repo": t.repo, "title": t.title, "status": t.status}
                for t in in_progress[:5]
            ],
        },
    })

    try:
        raw = claude_code.think(_SYSTEM_PROMPT, user_prompt)
        data = _extract_json(raw)
        channel = data.get("channel", _channel())
        text = data.get("text", f"APEX Digest — {today}")
        blocks = data.get("blocks")
    except Exception:
        channel = _channel()
        lines = [f"📋 *APEX Digest* — {today}"]
        if waiting:
            lines.append(f"\n*⏳ Waiting on you ({len(waiting)})*")
            for t in waiting[:3]:
                lines.append(f"• [{t.repo.split('/')[-1]}] {t.title}")
        if in_progress:
            lines.append(f"\n*🔄 In progress ({len(in_progress)})*")
            for t in in_progress[:3]:
                lines.append(f"• [{t.repo.split('/')[-1]}] {t.title} — {t.status}")
        if not waiting and not in_progress:
            lines.append("\nNo active tasks. Inbox is clear.")
        text = "\n".join(lines)
        blocks = None

    msg = SlackMessage(channel=channel, text=text, blocks=blocks)
    return _send(msg)
