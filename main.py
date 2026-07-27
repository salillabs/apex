"""
APEX — AI Engineering Team
Copyright (C) 2026 salillabs (https://github.com/salillabs/apex)
Licensed under AGPL-3.0

APEX entry point — FastAPI app that receives GitHub webhooks and Slack interactions,
then drives the engineering loop graph.
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import yaml

from fastapi import FastAPI, Header, HTTPException, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import services.claude_code as claude_code_service
import services.github as github_service
import services.db as db_service
import services.slack as slack_service


_PROJECTS_FILE = Path(__file__).parent / "projects.yaml"
_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str, **kwargs) -> str:
    template = (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return template.format(**kwargs) if kwargs else template


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_service.init_db()
    projects = yaml.safe_load(_PROJECTS_FILE.read_text(encoding="utf-8")) or []
    db_service.sync_projects_from_config(projects)
    if not _UI_SECRET:
        logging.getLogger(__name__).warning("UI_SECRET is not set — Web UI is open to anyone with the URL")
    yield


app = FastAPI(title="APEX", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public = path.startswith("/webhook/") or path.startswith("/static/") or path in ("/login", "/favicon.ico")
    if not public and not _is_authenticated(request):
        return RedirectResponse(url="/login")
    return await call_next(request)

_build_semaphore = asyncio.Semaphore(1)  # one build at a time

_UI_SECRET = os.environ.get("UI_SECRET", "")
_SESSION_COOKIE = "apex_session"

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>APEX — Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#161b22;border:1px solid #21262d;border-radius:12px;padding:40px;width:100%;max-width:360px}}
.logo{{font-size:18px;font-weight:700;margin-bottom:8px}}
.sub{{font-size:13px;color:#7a8694;margin-bottom:28px}}
input{{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;font-size:14px;padding:10px 14px;font-family:inherit;outline:none;margin-bottom:12px}}
input:focus{{border-color:#1f6feb;box-shadow:0 0 0 3px #1f6feb18}}
button{{width:100%;background:#1f6feb;color:#fff;border:none;border-radius:6px;font-size:14px;font-weight:500;padding:10px;cursor:pointer;font-family:inherit}}
button:hover{{background:#388bfd}}
.err{{color:#f85149;font-size:13px;margin-bottom:12px}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">APEX</div>
  <div class="sub">Autonomous Engineering</div>
  {error}
  <form method="post" action="/login">
    <input type="password" name="secret" placeholder="Enter access secret" autofocus>
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>"""


def _is_authenticated(request: Request) -> bool:
    if not _UI_SECRET:
        return True
    cookie = request.cookies.get(_SESSION_COOKIE, "")
    return hmac.compare_digest(cookie, _UI_SECRET)


# ── Signature verification ────────────────────────────────────────────────────

def _verify_github_signature(body: bytes, signature: str) -> None:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        return  # skip verification in dev if not configured
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid GitHub signature")


def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> None:
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        return
    base = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


# ── GitHub webhook ────────────────────────────────────────────────────────────

@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(default=""),
):
    body = await request.body()
    _verify_github_signature(body, x_hub_signature_256)

    payload: dict[str, Any] = await request.json()

    if x_github_event == "issues" and payload.get("action") == "opened":
        asyncio.create_task(_handle_new_issue(payload))

    return {"ok": True}


async def _handle_new_issue(payload: dict) -> None:
    gh_issue = payload["issue"]
    repo_full = payload["repository"]["full_name"]

    if not db_service.is_managed_repo(repo_full):
        return

    project = db_service.get_project(repo_full)
    if not project:
        return

    channel = project.slack_channel or slack_service.default_channel()
    slack_service.send_text(
        f"📋 New issue #{gh_issue['number']}: *{gh_issue['title']}*\n"
        f"Say `@apex --build #{gh_issue['number']}` to start working on it.",
        channel=channel,
    )


# ── Slack interactions ────────────────────────────────────────────────────────

@app.post("/webhook/slack")
async def slack_webhook(
    request: Request,
    x_slack_request_timestamp: str = Header(default=""),
    x_slack_signature: str = Header(default=""),
):
    body = await request.body()
    _verify_slack_signature(body, x_slack_request_timestamp, x_slack_signature)

    # Events API sends raw JSON; Block interactions send form-encoded with a "payload" key.
    try:
        payload: dict = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = parse_qs(body.decode())
        payload = json.loads(parsed.get("payload", ["{}"])[0])

    ptype = payload.get("type")

    if ptype == "url_verification":
        return {"challenge": payload["challenge"]}
    if ptype == "event_callback":
        # Slack retries delivery if we don't respond within 3 s — skip retries to avoid duplicate replies.
        if request.headers.get("x-slack-retry-num"):
            return {"ok": True}
        task = asyncio.create_task(_handle_slack_event(payload))
        task.add_done_callback(lambda t: logging.getLogger(__name__).error("Slack event error: %s", t.exception()) if not t.cancelled() and t.exception() else None)
    if ptype == "block_actions":
        asyncio.create_task(_handle_slack_action(payload))

    return {"ok": True}


async def _handle_slack_event(payload: dict) -> None:
    event = payload.get("event", {})
    if event.get("type") == "app_mention":
        await _handle_app_mention(event)


async def _run_build(channel: str, thread_ts: str, description: str, repo: str, local_path: str) -> None:
    import uuid
    task_id = str(uuid.uuid4())[:8]
    title = description[:500]

    db_service.save_task(task_id, 0, repo, title, status="in_progress", priority="task")

    if _build_semaphore.locked():
        slack_service.send_thread_reply(channel, thread_ts, "⏳ Another build is running. This task will start when it finishes.")
    async with _build_semaphore:
        prompt = _load_prompt("build", description=description)
        try:
            summary = await asyncio.to_thread(claude_code_service.execute, prompt, local_path)
            db_service.update_task(task_id, status="merged", response=summary)
            slack_service.send_thread_reply(channel, thread_ts, summary)
        except Exception as exc:
            db_service.update_task(task_id, status="failed", response=str(exc))
            slack_service.send_thread_reply(channel, thread_ts, f"Build failed: {exc}")


def _extract_pr_url(text: str) -> str | None:
    match = re.search(r'https://github\.com/[^/\s]+/[^/\s]+/pull/\d+', text)
    return match.group(0) if match else None


def _extract_pr_number(text: str) -> int | None:
    match = re.search(r'https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)', text)
    return int(match.group(1)) if match else None


async def _run_add_tests(channel: str, thread_ts: str, description: str, repo: str, local_path: str) -> None:
    import uuid
    task_id = str(uuid.uuid4())[:8]
    db_service.save_task(task_id, 0, repo, f"Add tests: {description[:120]}", status="in_progress", priority="task")

    if _build_semaphore.locked():
        slack_service.send_thread_reply(channel, thread_ts, "⏳ Another task is running. This will start when it finishes.")
    async with _build_semaphore:
        slack_service.send_thread_reply(channel, thread_ts, "✍️ Writing tests...")
        try:
            prompt = _load_prompt("add_tests", description=description)
            summary = await asyncio.to_thread(claude_code_service.execute, prompt, local_path)
            db_service.update_task(task_id, status="done", response=summary)
            slack_service.send_thread_reply(channel, thread_ts, summary)
        except Exception as exc:
            db_service.update_task(task_id, status="failed", response=str(exc))
            slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Failed to add tests: {exc}")


async def _run_ship(channel: str, thread_ts: str, issue_number: int, description: str, repo: str, local_path: str) -> None:
    import uuid as _uuid
    task_id = str(_uuid.uuid4())[:8]
    db_service.save_task(task_id, issue_number, repo, description[:120], status="in_progress", priority="task")

    if _build_semaphore.locked():
        slack_service.send_thread_reply(channel, thread_ts, "⏳ Another task is running. This will start when it finishes.")
    async with _build_semaphore:
        slack_service.send_thread_reply(channel, thread_ts, "🚀 Starting full pipeline: implement → tests → PR…")
        try:
            prompt = _load_prompt("ship", description=description)
            summary = await asyncio.to_thread(claude_code_service.execute, prompt, local_path)
        except Exception as exc:
            db_service.update_task(task_id, status="failed", response=str(exc))
            slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Ship failed: {exc}")
            return

        pr_url = _extract_pr_url(summary)
        pr_number = _extract_pr_number(summary)

        db_service.update_task(task_id, status="reviewing", response=summary, pr_number=pr_number)
        slack_service.send_thread_reply(channel, thread_ts, f"✅ Built and tested.\n\n{summary}")

        if pr_number:
            approval_id = str(_uuid.uuid4())[:8]
            from models.domain import ApprovalRequest
            req = ApprovalRequest(
                id=approval_id,
                reason=f"Merge PR #{pr_number} into main?",
                summary=f"*PR:* {pr_url}\n\n{summary[:400]}",
                options=["approved", "rejected"],
            )
            slack_ts = slack_service.send_approval_request(req, channel=channel)
            db_service.save_approval(
                approval_id=approval_id,
                task_id=task_id,
                reason=req.reason,
                options=req.options,
                slack_channel=channel,
                slack_ts=slack_ts,
            )
        else:
            slack_service.send_thread_reply(channel, thread_ts, "⚠️ Could not find a PR URL in the output — check manually.")


def _answer_question(question: str, repo: str) -> str:
    """Run Claude inside the project directory to answer any question. Falls back to think() if no local path."""
    project = db_service.get_project(repo)
    if project and project.local_path:
        return claude_code_service.execute(
            _load_prompt("question", question=question),
            project.local_path,
        )

    return claude_code_service.think(
        "You are an AI engineering assistant. Answer the question directly and concisely.",
        f"Repo: {repo}\n\nQuestion: {question}",
    )


_HELP_TEXT = """*APEX Commands*
• `@apex --next` — list open issues to build
• `@apex --build #42` — implement issue #42 only
• `@apex --add-tests #42` — write tests for a built feature
• `@apex --ship #42` — full pipeline: implement → tests → PR → approval
• `@apex --test` — run the test suite and report results
• `@apex --shipped` — show recently merged PRs
• `@apex --status` — show active build queue
• `@apex --history` — show recent tasks (including failed)
• `@apex --review` — review recent code changes
• `@apex --report` — show last test report from history
• `@apex --register owner/repo` — register this channel to a repo
• `@apex --help` — show this message
• `@apex <question>` — ask anything about the codebase"""


async def _handle_app_mention(event: dict) -> None:
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    raw_text = event.get("text", "")

    text = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()
    text = text.replace("—", "--").replace("–", "--")  # normalize em/en dash Slack auto-formats

    if not text or text == "--help":
        slack_service.send_thread_reply(channel, thread_ts, _HELP_TEXT)
        return

    # ── --register ────────────────────────────────────────────────────────────
    if text.startswith("--register "):
        repo = text[len("--register "):].strip()
        if not repo or "/" not in repo:
            slack_service.send_thread_reply(channel, thread_ts, "Usage: `@apex --register owner/repo-name`")
            return
        db_service.register_channel(channel, repo)
        slack_service.send_thread_reply(channel, thread_ts, f"✅ Channel registered for `{repo}`.")
        return

    # All other commands require a registered repo
    repo = db_service.get_repo_for_channel(channel)
    if not repo:
        slack_service.send_thread_reply(channel, thread_ts, "No repo registered. Use `@apex --register owner/repo-name` first.")
        return

    # ── --next ────────────────────────────────────────────────────────────────
    if text == "--next":
        slack_service.send_thread_reply(channel, thread_ts, "Fetching open issues…")
        try:
            issues = await asyncio.to_thread(github_service.list_open_issues, repo)
            if not issues:
                slack_service.send_thread_reply(channel, thread_ts, "No open issues found.")
                return
            lines = [f"*Open issues for `{repo}`:*"]
            for i in issues[:15]:
                lines.append(f"• *#{i.number}* — {i.title}")
            lines.append("\nUse `@apex --build #42` to start one.")
            slack_service.send_thread_reply(channel, thread_ts, "\n".join(lines))
        except Exception as exc:
            slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Could not fetch issues: {exc}")
        return

    # ── --build ───────────────────────────────────────────────────────────────
    if text.startswith("--build"):
        arg = text[len("--build"):].strip()
        project = db_service.get_project(repo)
        if not project or not project.local_path:
            slack_service.send_thread_reply(channel, thread_ts, f"No local path configured for `{repo}`.")
            return
        issue_numbers = [int(m) for m in re.findall(r"#(\d+)", arg)]
        if not issue_numbers:
            slack_service.send_thread_reply(channel, thread_ts, "Usage: `@apex --build #42` or `@apex --build #42 #43`")
            return
        for num in issue_numbers:
            try:
                issue = await asyncio.to_thread(github_service.get_issue, repo, num)
                description = f"Issue #{issue.number}: {issue.title}\n\n{issue.body}"
                slack_service.send_thread_reply(channel, thread_ts, f"Queued *#{num}*: {issue.title}")
                asyncio.create_task(_run_build(channel, thread_ts, description, repo, project.local_path))
            except Exception as exc:
                slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Could not fetch issue #{num}: {exc}")
        return

    # ── --add-tests ───────────────────────────────────────────────────────────
    if text.startswith("--add-tests"):
        arg = text[len("--add-tests"):].strip()
        issue_numbers = [int(m) for m in re.findall(r"#(\d+)", arg)]
        project = db_service.get_project(repo)
        if not project or not project.local_path:
            slack_service.send_thread_reply(channel, thread_ts, f"No local path configured for `{repo}`.")
            return
        if not issue_numbers:
            slack_service.send_thread_reply(channel, thread_ts, "Usage: `@apex --add-tests #42`")
            return
        for num in issue_numbers:
            try:
                issue = await asyncio.to_thread(github_service.get_issue, repo, num)
                description = f"Issue #{issue.number}: {issue.title}\n\n{issue.body or ''}"
                slack_service.send_thread_reply(channel, thread_ts, f"Queued test writing for *#{num}*: {issue.title}")
                asyncio.create_task(_run_add_tests(channel, thread_ts, description, repo, project.local_path))
            except Exception as exc:
                slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Could not fetch issue #{num}: {exc}")
        return

    # ── --ship ────────────────────────────────────────────────────────────────
    if text.startswith("--ship"):
        arg = text[len("--ship"):].strip()
        issue_numbers = [int(m) for m in re.findall(r"#(\d+)", arg)]
        project = db_service.get_project(repo)
        if not project or not project.local_path:
            slack_service.send_thread_reply(channel, thread_ts, f"No local path configured for `{repo}`.")
            return
        if not issue_numbers:
            slack_service.send_thread_reply(channel, thread_ts, "Usage: `@apex --ship #42`")
            return
        for num in issue_numbers:
            try:
                issue = await asyncio.to_thread(github_service.get_issue, repo, num)
                description = f"Issue #{issue.number}: {issue.title}\n\n{issue.body or ''}"
                slack_service.send_thread_reply(channel, thread_ts, f"Queued full pipeline for *#{num}*: {issue.title}")
                asyncio.create_task(_run_ship(channel, thread_ts, issue.number, description, repo, project.local_path))
            except Exception as exc:
                slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Could not fetch issue #{num}: {exc}")
        return

    # ── --status ──────────────────────────────────────────────────────────────
    if text == "--status":
        tasks = db_service.get_portfolio()
        if not tasks:
            slack_service.send_thread_reply(channel, thread_ts, "No active tasks.")
            return
        lines = [f"*Active tasks for `{repo}`:*"]
        for t in tasks:
            if t.repo == repo:
                lines.append(f"• *{t.id}* — {t.title} (`{t.status}`)")
        slack_service.send_thread_reply(channel, thread_ts, "\n".join(lines) if len(lines) > 1 else "No active tasks for this repo.")
        return

    # ── --shipped ─────────────────────────────────────────────────────────
    if text == "--shipped":
        slack_service.send_thread_reply(channel, thread_ts, "Fetching merged PRs…")
        try:
            prs = await asyncio.to_thread(github_service.list_merged_prs, repo, 10)
            if not prs:
                slack_service.send_thread_reply(channel, thread_ts, "No merged PRs found.")
                return
            lines = [f"*Recently merged in `{repo}`:*"]
            for pr in prs:
                lines.append(f"• *#{pr['number']}* — {pr['title']} (merged {pr['merged_at'][:10]})")
            slack_service.send_thread_reply(channel, thread_ts, "\n".join(lines))
        except Exception as exc:
            slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Could not fetch PRs: {exc}")
        return

    # ── --review ──────────────────────────────────────────────────────────────
    if text == "--review":
        project = db_service.get_project(repo)
        if not project or not project.local_path:
            slack_service.send_thread_reply(channel, thread_ts, f"No local path configured for `{repo}`.")
            return
        slack_service.send_thread_reply(channel, thread_ts, "Reviewing recent changes...")
        async with _build_semaphore:
            try:
                result = await asyncio.to_thread(
                    claude_code_service.execute,
                    _load_prompt("review"),
                    project.local_path,
                )
                slack_service.send_thread_reply(channel, thread_ts, result)
            except Exception as exc:
                slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Review failed: {exc}")
        return

    # ── --test ────────────────────────────────────────────────────────────────
    if text == "--test":
        project = db_service.get_project(repo)
        if not project or not project.local_path:
            slack_service.send_thread_reply(channel, thread_ts, f"No local path configured for `{repo}`.")
            return
        slack_service.send_thread_reply(channel, thread_ts, "🧪 Running tests...")
        async with _build_semaphore:
            try:
                result = await asyncio.to_thread(
                    claude_code_service.execute,
                    _load_prompt("test"),
                    project.local_path,
                )
                slack_service.send_thread_reply(channel, thread_ts, result)
            except Exception as exc:
                slack_service.send_thread_reply(channel, thread_ts, f"⚠️ Test run failed: {exc}")
        return

    # ── --history ─────────────────────────────────────────────────────────────
    if text == "--history":
        tasks = db_service.get_recent_tasks(repo, limit=10)
        if not tasks:
            slack_service.send_thread_reply(channel, thread_ts, "No tasks found for this repo.")
            return
        status_icon = {"merged": "✅", "done": "✅", "failed": "❌", "abandoned": "🚫", "in_progress": "🔄", "reviewing": "👀"}
        lines = [f"*Recent tasks for `{repo}`:*"]
        for t in tasks:
            icon = status_icon.get(t.status, "•")
            lines.append(f"{icon} `{t.id}` — {t.title[:60]} (`{t.status}`)")
        slack_service.send_thread_reply(channel, thread_ts, "\n".join(lines))
        return

    # ── --report ──────────────────────────────────────────────────────────────
    if text == "--report":
        result = db_service.get_last_test_result(repo)
        if result is None:
            slack_service.send_thread_reply(channel, thread_ts, "No test history found. Run `@apex --test` first.")
            return
        status = "✅ Passed" if result.success else "❌ Failed"
        lines = [
            f"*Last test report for `{repo}`:*",
            f"{status} — {result.tests_passed}/{result.tests_run} passed, {result.tests_failed} failed",
            f"Branch: `{result.branch}` | Run at: {str(result.created_at)[:16]}",
        ]
        if result.failures:
            lines.append("\n*Failures:*")
            for f in result.failures[:5]:
                lines.append(f"• {f.get('test_name', '?')}: {f.get('error', '')}")
        slack_service.send_thread_reply(channel, thread_ts, "\n".join(lines))
        return

    # ── natural language question ─────────────────────────────────────────────
    slack_service.send_thread_reply(channel, thread_ts, "Thinking…")
    try:
        answer = await asyncio.to_thread(_answer_question, text, repo)
    except Exception as exc:
        answer = f"⚠️ Something went wrong: {exc}"
    slack_service.send_thread_reply(channel, thread_ts, answer)


async def _handle_slack_action(payload: dict) -> None:
    actions = payload.get("actions", [])
    if not actions:
        return

    action = actions[0]
    action_id = action.get("action_id", "")
    approval_id = action.get("value", "")

    if action_id not in ("apex_approve", "apex_reject"):
        return

    decision = "approved" if action_id == "apex_approve" else "rejected"
    approval = db_service.get_approval(approval_id)
    if approval is None:
        return

    channel = payload.get("channel", {}).get("id", "")
    message_ts = payload.get("message", {}).get("ts", "")

    if decision == "approved":
        task = db_service.get_task(approval.task_id)
        if task and task.pr_number:
            try:
                github_service.merge_pr(task.repo, task.pr_number)
                db_service.update_task(task.id, status="merged")
                db_service.update_approval(approval_id, decision=decision)
                slack_service.send_thread_reply(channel, message_ts, f"✅ Merged PR #{task.pr_number}.")
            except Exception as exc:
                slack_service.send_thread_reply(channel, message_ts, f"⚠️ Merge failed: {exc}")
        else:
            db_service.update_approval(approval_id, decision=decision)
            slack_service.send_thread_reply(channel, message_ts, "✅ Approved — no PR number found to auto-merge. Merge manually.")
    else:
        db_service.update_approval(approval_id, decision=decision)
        slack_service.send_thread_reply(channel, message_ts, "🚫 Rejected.")


# ── Debug endpoints (no Slack / no GitHub webhook required) ───────────────────

@app.post("/debug/mention")
async def debug_mention(request: Request):
    """Simulate an @apex Slack mention without needing Slack.
    Same commands as Slack: 'build: add dark mode', 'register owner/repo', or a question.
    Example: {"channel": "C123", "text": "build: add dark mode", "thread_ts": "0"}
    Channel must be registered first via 'register' or db_service.register_channel().
    """
    body = await request.json()
    try:
        await _handle_app_mention({
            "channel": body["channel"],
            "text": body["text"],
            "ts": body.get("thread_ts", "0"),
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


@app.post("/debug/trigger")
async def debug_trigger(request: Request):
    """Manually start a build (bypasses webhook + allowlist). Requires repo to be in projects.yaml."""
    body = await request.json()
    repo = body["repo"]
    project = db_service.get_project(repo)
    if not project or not project.local_path:
        raise HTTPException(status_code=404, detail=f"No local_path configured for '{repo}' in projects.yaml.")
    description = f"{body['title']}\n\n{body.get('body', '')}"
    channel = project.slack_channel or slack_service.default_channel()
    thread_ts = body.get("thread_ts", "0")
    slack_service.send_text(f"Starting: {body['title']}", channel=channel)
    asyncio.create_task(_run_build(channel, thread_ts, description, repo, project.local_path))
    return {"ok": True, "repo": repo, "title": body["title"]}


@app.get("/debug/tasks")
async def debug_tasks():
    """List active (non-terminal) tasks from the database."""
    tasks = db_service.get_portfolio()
    return [
        {"task_id": t.id, "repo": t.repo, "title": t.title, "status": t.status, "type": t.priority or "task"}
        for t in tasks
    ]


@app.post("/debug/approve/{approval_id}")
async def debug_approve(approval_id: str, decision: str = "approved"):
    """Record a decision for an approval (replaces clicking Approve/Reject in Slack).
    decision: 'approved' | 'rejected'
    """
    approval = db_service.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"No approval '{approval_id}'.")
    db_service.update_approval(approval_id, decision=decision)
    return {"ok": True, "approval_id": approval_id, "decision": decision}


@app.post("/debug/abandon/{task_id}")
async def debug_abandon(task_id: str):
    """Mark a task as abandoned in the database."""
    db_service.update_task(task_id, status="abandoned")
    return {"ok": True, "task_id": task_id, "status": "abandoned"}


@app.get("/debug/task/{task_id}")
async def debug_task_history(task_id: str):
    """Show everything recorded for a task: decisions made, implementations attempted, test results."""
    task = db_service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    history = db_service.get_task_history(task_id)
    return {
        "task_id": task.id,
        "title": task.title,
        "repo": task.repo,
        "status": task.status,
        "branch": task.branch,
        "pr_number": task.pr_number,
        "failure_count": task.failure_count,
        "created_at": str(task.created_at),
        **history,
    }


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).parent / "static" / "favicon.ico")


@app.get("/login", include_in_schema=False)
async def login_page():
    return HTMLResponse(_LOGIN_HTML.format(error=""))


@app.post("/login", include_in_schema=False)
async def login(secret: str = Form(...)):
    if _UI_SECRET and not hmac.compare_digest(secret, _UI_SECRET):
        return HTMLResponse(_LOGIN_HTML.format(error='<div class="err">Incorrect secret.</div>'))
    response = RedirectResponse(url="/ops", status_code=303)
    response.set_cookie(_SESSION_COOKIE, _UI_SECRET, httponly=True, samesite="lax", secure=True)
    return response


@app.get("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie(_SESSION_COOKIE)
    return response


# ── Web UI ───────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/ops")


@app.get("/ops", include_in_schema=False)
async def ops():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/projects")
async def api_projects():
    projects = db_service.get_projects()
    seen = set()
    result = []
    for p in projects:
        if p.repo not in seen:
            seen.add(p.repo)
            result.append({"repo": p.repo, "name": p.name})
    return result


@app.get("/api/approvals/pending")
async def api_pending_approvals():
    approvals = db_service.get_pending_approvals()
    result = []
    for a in approvals:
        task = db_service.get_task(a.task_id)
        result.append({
            "id": a.id,
            "task_id": a.task_id,
            "repo": task.repo if task else None,
            "reason": a.reason,
            "options": a.options,
            "requested_at": str(a.requested_at),
        })
    return result


@app.post("/api/command")
async def api_command(request: Request):
    body = await request.json()
    repo = (body.get("repo") or "").strip()
    text = (body.get("text") or "").strip()
    if not repo or not text:
        raise HTTPException(status_code=400, detail="repo and text required")

    project = db_service.get_project(repo)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{repo}' not found")

    if text == "--next":
        try:
            issues = await asyncio.to_thread(github_service.list_open_issues, repo)
            if not issues:
                return {"response": "No open issues found."}
            lines = [f"**Open issues for `{repo}`:**\n"]
            for i in issues[:15]:
                lines.append(f"- `#{i.number}` — {i.title}")
            lines.append(f"\n_Use_ `--build #42` _to start one._")
            return {"response": "\n".join(lines)}
        except Exception as exc:
            return {"response": f"Could not fetch issues: {exc}"}

    if text == "--status":
        tasks = db_service.get_portfolio()
        repo_tasks = [t for t in tasks if t.repo == repo]
        if not repo_tasks:
            return {"response": "No active tasks."}
        lines = [f"Active tasks for {repo}:"]
        for t in repo_tasks:
            lines.append(f"  {t.id} — {t.title} ({t.status})")
        return {"response": "\n".join(lines)}

    if text == "--history":
        tasks = db_service.get_recent_tasks(repo, limit=10)
        if not tasks:
            return {"response": "No tasks found for this repo."}
        status_icon = {"merged": "✅", "done": "✅", "failed": "❌", "abandoned": "🚫", "in_progress": "🔄", "reviewing": "👀"}
        lines = [f"**Recent tasks for `{repo}`:**\n"]
        for t in tasks:
            icon = status_icon.get(t.status, "•")
            lines.append(f"{icon} `{t.id}` — {t.title[:60]} (`{t.status}`)")
        return {"response": "\n".join(lines)}

    if text == "--shipped":
        try:
            prs = await asyncio.to_thread(github_service.list_merged_prs, repo, 10)
            if not prs:
                return {"response": "No merged PRs found."}
            lines = [f"Recently merged in {repo}:"]
            for pr in prs:
                lines.append(f"  #{pr['number']} — {pr['title']} (merged {pr['merged_at'][:10]})")
            return {"response": "\n".join(lines)}
        except Exception as exc:
            return {"response": f"Could not fetch PRs: {exc}"}

    if text == "--review":
        if not project.local_path:
            return {"response": f"No local path configured for '{repo}'."}
        try:
            result = await asyncio.to_thread(
                claude_code_service.execute, _load_prompt("review"), project.local_path
            )
            return {"response": result}
        except Exception as exc:
            return {"response": f"Review failed: {exc}"}

    if text == "--test":
        if not project.local_path:
            return {"response": f"No local path configured for '{repo}'."}
        try:
            result = await asyncio.to_thread(
                claude_code_service.execute, _load_prompt("test"), project.local_path
            )
            return {"response": result}
        except Exception as exc:
            return {"response": f"Test run failed: {exc}"}

    if text == "--report":
        result = db_service.get_last_test_result(repo)
        if result is None:
            return {"response": "No test history found. Run --test first."}
        status = "Passed" if result.success else "Failed"
        lines = [
            f"Last test report for {repo}:",
            f"  {status} — {result.tests_passed}/{result.tests_run} passed, {result.tests_failed} failed",
            f"  Branch: {result.branch} | Run at: {str(result.created_at)[:16]}",
        ]
        if result.failures:
            lines.append("  Failures:")
            for f in result.failures[:5]:
                lines.append(f"    • {f.get('test_name', '?')}: {f.get('error', '')}")
        return {"response": "\n".join(lines)}

    if text.startswith("--build"):
        if not project.local_path:
            return {"response": f"No local path configured for '{repo}'."}
        issue_numbers = [int(m) for m in re.findall(r"#(\d+)", text)]
        us_ids = re.findall(r"\bUS-(\d+)\b", text, re.IGNORECASE)
        not_found = []
        if us_ids:
            all_issues = await asyncio.to_thread(github_service.list_open_issues, repo)
            for us in us_ids:
                match = next((i for i in all_issues if f"US-{us}" in i.title), None)
                if match:
                    issue_numbers.append(match.number)
                else:
                    not_found.append(f"US-{us}")
        if not_found and not issue_numbers:
            return {"response": f"No open issue found for: {', '.join(not_found)}"}
        if not issue_numbers:
            return {"response": "Usage: --build #42 or --build US-32"}
        responses = []
        for num in issue_numbers:
            try:
                issue = await asyncio.to_thread(github_service.get_issue, repo, num)
                description = f"Issue #{issue.number}: {issue.title}\n\n{issue.body or ''}"
                channel = project.slack_channel or slack_service.default_channel()
                asyncio.create_task(_run_build(channel, "0", description, repo, project.local_path))
                responses.append(f"Queued #{num}: {issue.title}")
            except Exception as exc:
                responses.append(f"Could not fetch issue #{num}: {exc}")
        return {"response": "\n".join(responses)}

    if text.startswith("--add-tests"):
        if not project.local_path:
            return {"response": f"No local path configured for '{repo}'."}
        issue_numbers = [int(m) for m in re.findall(r"#(\d+)", text)]
        if not issue_numbers:
            return {"response": "Usage: --add-tests #42"}
        responses = []
        for num in issue_numbers:
            try:
                issue = await asyncio.to_thread(github_service.get_issue, repo, num)
                description = f"Issue #{issue.number}: {issue.title}\n\n{issue.body or ''}"
                channel = project.slack_channel or slack_service.default_channel()
                asyncio.create_task(_run_add_tests(channel, "0", description, repo, project.local_path))
                responses.append(f"Queued test writing for #{num}: {issue.title}")
            except Exception as exc:
                responses.append(f"Could not fetch issue #{num}: {exc}")
        return {"response": "\n".join(responses)}

    if text.startswith("--ship"):
        if not project.local_path:
            return {"response": f"No local path configured for '{repo}'."}
        issue_numbers = [int(m) for m in re.findall(r"#(\d+)", text)]
        if not issue_numbers:
            return {"response": "Usage: --ship #42"}
        responses = []
        for num in issue_numbers:
            try:
                issue = await asyncio.to_thread(github_service.get_issue, repo, num)
                description = f"Issue #{issue.number}: {issue.title}\n\n{issue.body or ''}"
                channel = project.slack_channel or slack_service.default_channel()
                asyncio.create_task(_run_ship(channel, "0", issue.number, description, repo, project.local_path))
                responses.append(f"Queued full pipeline for #{num}: {issue.title}")
            except Exception as exc:
                responses.append(f"Could not fetch issue #{num}: {exc}")
        return {"response": "\n".join(responses)}

    # Natural language query
    import uuid
    query_id = str(uuid.uuid4())[:8]
    db_service.save_task(query_id, 0, repo, text[:120], status="in_progress", priority="query")
    try:
        answer = await asyncio.to_thread(_answer_question, text, repo)
        db_service.update_task(query_id, status="done", response=answer)
        return {"response": answer}
    except Exception as exc:
        db_service.update_task(query_id, status="failed", response=str(exc))
        return {"response": f"Something went wrong: {exc}"}


@app.post("/api/build")
async def api_build(request: Request):
    body = await request.json()
    repo = body.get("repo")
    issue_number = body.get("issue_number")
    if not repo or not issue_number:
        raise HTTPException(status_code=400, detail="repo and issue_number required")
    project = db_service.get_project(repo)
    if not project or not project.local_path:
        raise HTTPException(status_code=404, detail=f"No local_path configured for '{repo}'")
    try:
        issue = await asyncio.to_thread(github_service.get_issue, repo, issue_number)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not fetch issue #{issue_number}: {exc}")
    description = f"Issue #{issue.number}: {issue.title}\n\n{issue.body or ''}"
    channel = project.slack_channel or slack_service.default_channel()
    asyncio.create_task(_run_build(channel, "0", description, repo, project.local_path))
    return {"ok": True, "title": issue.title, "repo": repo}


@app.get("/api/tasks")
async def api_tasks(repo: str, limit: int = 20, offset: int = 0):
    from database.tables import TaskRow as TR
    from sqlalchemy import select as sa_select
    with db_service._session() as session:
        rows = session.execute(
            sa_select(TR)
            .where(TR.repo == repo)
            .order_by(TR.updated_at.desc())
            .limit(offset + limit)
        ).scalars().all()
        for r in rows:
            session.expunge(r)
    return [
        {
            "task_id": t.id,
            "title": t.title,
            "status": t.status,
            "type": t.priority or "task",
            "updated_at": str(t.updated_at)[:16],
            "response": t.response,
        }
        for t in rows[offset:]
    ]


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}
