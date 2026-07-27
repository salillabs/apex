import logging
import os
from slack_sdk import WebClient
from models.domain import ApprovalRequest, BugReport

log = logging.getLogger(__name__)
_client: WebClient | None = None


def _slack_enabled() -> bool:
    if os.environ.get("SLACK_ENABLED", "").lower() == "false":
        return False
    return bool(os.environ.get("SLACK_BOT_TOKEN"))


def _get() -> WebClient:
    global _client
    if _client is None:
        _client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    return _client


def default_channel() -> str:
    return os.environ.get("SLACK_CHANNEL_ID", "no-channel")


def send_message(channel: str, text: str, blocks: list[dict] | None = None) -> str:
    """Post a message. Returns the Slack message timestamp (used as ID)."""
    if not _slack_enabled():
        log.info("[SLACK] %s: %s", channel, text)
        return "0"
    kwargs: dict = {"channel": channel, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    response = _get().chat_postMessage(**kwargs)
    return response["ts"]


def send_text(text: str, channel: str | None = None) -> str:
    return send_message(channel or default_channel(), text)


def send_approval_request(request: ApprovalRequest, channel: str | None = None) -> str:
    """Post an approval card with Approve/Reject buttons. Returns the message ts."""
    if not _slack_enabled():
        log.info("[SLACK APPROVAL] id=%s reason=%s", request.id, request.reason)
        return "0"
    ch = channel or default_channel()
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Approval Required*\n{request.summary}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Reason:* {request.reason}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "apex_approve",
                    "value": request.id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "apex_reject",
                    "value": request.id,
                },
            ],
        },
    ]
    return send_message(ch, f"Approval Required: {request.reason}", blocks)


def send_thread_reply(channel: str, thread_ts: str, text: str) -> str:
    """Reply inside an existing Slack message thread."""
    if not _slack_enabled():
        log.info("[SLACK THREAD] %s/%s: %s", channel, thread_ts, text)
        return "0"
    response = _get().chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
    return response["ts"]


def send_bug_report(report: BugReport, channel: str | None = None) -> str:
    if not _slack_enabled():
        log.info("[SLACK BUG REPORT] %s", report.summary)
        return "0"
    ch = channel or default_channel()
    failures_text = "\n".join(
        f"• *{f.get('test_name', 'Unknown')}*: {f.get('plain_english', f.get('error', ''))}"
        for f in (report.test_result.failures or [])
    )
    causes_text = "\n".join(f"• {c}" for c in report.likely_causes)
    text = (
        f"*Test Failure*\n{report.summary}\n\n"
        f"*Failures:*\n{failures_text or 'See details'}\n\n"
        f"*Likely causes:*\n{causes_text}"
    )
    return send_message(ch, text)
