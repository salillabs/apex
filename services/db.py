import json
import logging
import os
import time
import uuid
from datetime import datetime
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from database.tables import (
    ApprovalRow,
    Base,
    ChannelProjectRow,
    DecisionRow,
    ImplementationRow,
    KnowledgeRow,
    ProjectRow,
    TaskRow,
    TestResultRow,
)
from models.domain import TaskStatus

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    return _engine


def init_db(retries: int = 3, delay: float = 2.0) -> None:
    """Create all tables and apply any missing column migrations."""
    log = logging.getLogger(__name__)
    for attempt in range(1, retries + 1):
        try:
            engine = _get_engine()
            Base.metadata.create_all(engine)
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS local_path VARCHAR"))
                conn.execute(text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS slack_channel VARCHAR"))
                conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS response TEXT"))
                conn.commit()
            return
        except Exception as exc:
            if attempt == retries:
                raise
            log.warning("DB init attempt %d/%d failed: %s — retrying in %.0fs", attempt, retries, exc, delay)
            time.sleep(delay)


def _session() -> Session:
    return Session(_get_engine())


def _expunge(session: Session, row: object) -> object:
    session.expunge(row)
    return row


# ── Tasks ─────────────────────────────────────────────────────────────────────

def save_task(
    task_id: str,
    issue_number: int,
    repo: str,
    title: str,
    status: str = TaskStatus.NEW,
    priority: str = "normal",
    branch: str | None = None,
) -> None:
    with _session() as session:
        session.merge(TaskRow(
            id=task_id,
            issue_number=issue_number,
            repo=repo,
            title=title,
            status=status,
            priority=priority,
            branch=branch,
        ))
        session.commit()


def update_task(task_id: str, **fields) -> None:
    with _session() as session:
        session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id)
            .values(updated_at=datetime.utcnow(), **fields)
        )
        session.commit()


def get_task(task_id: str) -> TaskRow | None:
    with _session() as session:
        row = session.get(TaskRow, task_id)
        return _expunge(session, row) if row else None


def get_portfolio() -> list[TaskRow]:
    terminal = {TaskStatus.MERGED, TaskStatus.ABANDONED, TaskStatus.FAILED, "done"}
    with _session() as session:
        rows = session.execute(
            select(TaskRow).where(TaskRow.status.notin_(terminal))
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def get_completed_tasks(repo: str, limit: int = 20) -> list[TaskRow]:
    """Return recently completed tasks for a repo (all terminal states including failed)."""
    terminal = [TaskStatus.MERGED, TaskStatus.ABANDONED, TaskStatus.FAILED, "done"]
    with _session() as session:
        rows = session.execute(
            select(TaskRow)
            .where(TaskRow.repo == repo)
            .where(TaskRow.status.in_(terminal))
            .order_by(TaskRow.updated_at.desc())
            .limit(limit)
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def get_recent_tasks(repo: str, limit: int = 10) -> list[TaskRow]:
    """Return most recent tasks for a repo regardless of status."""
    with _session() as session:
        rows = session.execute(
            select(TaskRow)
            .where(TaskRow.repo == repo)
            .order_by(TaskRow.updated_at.desc())
            .limit(limit)
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def get_task_history(task_id: str) -> dict:
    """Return all recorded activity for a task: decisions, implementations, test results."""
    with _session() as session:
        decisions = session.execute(
            select(DecisionRow).where(DecisionRow.task_id == task_id).order_by(DecisionRow.created_at)
        ).scalars().all()
        implementations = session.execute(
            select(ImplementationRow).where(ImplementationRow.task_id == task_id).order_by(ImplementationRow.created_at)
        ).scalars().all()
        test_results = session.execute(
            select(TestResultRow).where(TestResultRow.task_id == task_id).order_by(TestResultRow.created_at)
        ).scalars().all()
        return {
            "decisions": [
                {"type": d.decision_type, "decision": d.decision, "rationale": d.rationale, "at": str(d.created_at)}
                for d in decisions
            ],
            "implementations": [
                {"branch": i.branch, "success": i.success, "summary": i.summary, "error": i.error, "attempt": i.attempt_number, "at": str(i.created_at)}
                for i in implementations
            ],
            "test_results": [
                {"branch": t.branch, "success": t.success, "run": t.tests_run, "passed": t.tests_passed, "failed": t.tests_failed, "at": str(t.created_at)}
                for t in test_results
            ],
        }


def increment_failure_count(task_id: str) -> int:
    with _session() as session:
        row = session.get(TaskRow, task_id)
        if row is None:
            return 0
        row.failure_count = (row.failure_count or 0) + 1
        row.updated_at = datetime.utcnow()
        session.commit()
        return row.failure_count


# ── Projects ──────────────────────────────────────────────────────────────────

def get_projects() -> list[ProjectRow]:
    with _session() as session:
        rows = session.execute(
            select(ProjectRow).where(ProjectRow.active.is_(True))
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def is_managed_repo(repo: str) -> bool:
    with _session() as session:
        row = session.get(ProjectRow, repo)
        return row is not None and bool(row.active)


def sync_projects_from_config(projects: list[dict]) -> None:
    with _session() as session:
        for p in projects:
            session.merge(ProjectRow(
                repo=p["repo"],
                name=p["name"],
                priority=p.get("priority", 5),
                active=p.get("active", True),
                local_path=p.get("local_path"),
                slack_channel=p.get("slack_channel"),
            ))
            if p.get("slack_channel"):
                session.merge(ChannelProjectRow(
                    channel_id=p["slack_channel"],
                    repo=p["repo"],
                ))
        session.commit()


def get_project(repo: str) -> ProjectRow | None:
    with _session() as session:
        row = session.get(ProjectRow, repo)
        return _expunge(session, row) if row else None


# ── Decisions ─────────────────────────────────────────────────────────────────

def save_decision(task_id: str, decision_type: str, decision: str, rationale: str) -> None:
    with _session() as session:
        session.add(DecisionRow(
            id=str(uuid.uuid4()),
            task_id=task_id,
            decision_type=decision_type,
            decision=decision,
            rationale=rationale,
        ))
        session.commit()


# ── Approvals ─────────────────────────────────────────────────────────────────

def save_approval(
    approval_id: str,
    task_id: str,
    reason: str,
    options: list[str],
    slack_channel: str | None = None,
    slack_ts: str | None = None,
) -> None:
    with _session() as session:
        session.add(ApprovalRow(
            id=approval_id,
            task_id=task_id,
            reason=reason,
            options=options,
            slack_channel=slack_channel,
            slack_ts=slack_ts,
        ))
        session.commit()


def update_approval(approval_id: str, decision: str, user_feedback: str | None = None) -> None:
    with _session() as session:
        session.execute(
            update(ApprovalRow)
            .where(ApprovalRow.id == approval_id)
            .values(decision=decision, user_feedback=user_feedback, responded_at=datetime.utcnow())
        )
        session.commit()


def get_pending_approvals() -> list[ApprovalRow]:
    with _session() as session:
        rows = session.execute(
            select(ApprovalRow)
            .where(ApprovalRow.decision.is_(None))
            .order_by(ApprovalRow.requested_at)
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def get_approval(approval_id: str) -> ApprovalRow | None:
    with _session() as session:
        row = session.get(ApprovalRow, approval_id)
        return _expunge(session, row) if row else None


# ── Implementations ───────────────────────────────────────────────────────────

def save_implementation(
    task_id: str,
    branch: str,
    files_changed: list[str],
    summary: str,
    success: bool,
    error: str | None = None,
    attempt_number: int = 1,
) -> None:
    with _session() as session:
        session.add(ImplementationRow(
            id=str(uuid.uuid4()),
            task_id=task_id,
            branch=branch,
            files_changed=files_changed,
            summary=summary,
            success=success,
            error=error,
            attempt_number=attempt_number,
        ))
        session.commit()


# ── Test Results ──────────────────────────────────────────────────────────────

def save_test_result(
    task_id: str,
    branch: str,
    tests_run: int,
    tests_passed: int,
    tests_failed: int,
    failures: list[dict],
    success: bool,
    raw_output: str | None = None,
) -> None:
    with _session() as session:
        session.add(TestResultRow(
            id=str(uuid.uuid4()),
            task_id=task_id,
            branch=branch,
            tests_run=tests_run,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            failures=failures,
            success=success,
            raw_output=raw_output,
        ))
        session.commit()


# ── Last test result for a repo ───────────────────────────────────────────────

def get_last_test_result(repo: str) -> TestResultRow | None:
    """Return the most recent test result across all tasks for a repo."""
    with _session() as session:
        row = session.execute(
            select(TestResultRow)
            .join(TaskRow, TaskRow.id == TestResultRow.task_id)
            .where(TaskRow.repo == repo)
            .order_by(TestResultRow.created_at.desc())
        ).scalars().first()
        if row:
            session.expunge(row)
        return row


# ── Knowledge ─────────────────────────────────────────────────────────────────

def save_knowledge(
    source_type: str,
    content: str,
    source_ref: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
) -> None:
    with _session() as session:
        session.add(KnowledgeRow(
            id=str(uuid.uuid4()),
            project=project,
            source_type=source_type,
            source_ref=source_ref,
            content=content,
            tags=tags or [],
        ))
        session.commit()


def search_knowledge(query_tags: list[str], project: str | None = None) -> list[KnowledgeRow]:
    with _session() as session:
        stmt = select(KnowledgeRow)
        if project:
            stmt = stmt.where(KnowledgeRow.project == project)
        rows = session.execute(stmt).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


# ── Channel → Project mapping ─────────────────────────────────────────────────

def register_channel(channel_id: str, repo: str) -> None:
    with _session() as session:
        session.merge(ChannelProjectRow(channel_id=channel_id, repo=repo))
        session.commit()


def get_repo_for_channel(channel_id: str) -> str | None:
    with _session() as session:
        row = session.get(ChannelProjectRow, channel_id)
        return row.repo if row else None


# ── Specs ─────────────────────────────────────────────────────────────────────

def save_spec(task_id: str, spec_json: dict) -> None:
    """Persist a TechnicalSpec (as JSON) linked to a task."""
    save_knowledge(
        source_type="spec",
        content=json.dumps(spec_json),
        source_ref=task_id,
        project=spec_json.get("issue", {}).get("repo"),
        tags=["spec", task_id],
    )


def get_spec(task_id: str) -> dict | None:
    """Retrieve the most recent spec for a task. Returns None if not found."""
    with _session() as session:
        rows = session.execute(
            select(KnowledgeRow)
            .where(KnowledgeRow.source_type == "spec")
            .where(KnowledgeRow.source_ref == task_id)
            .order_by(KnowledgeRow.created_at.desc())
        ).scalars().all()
        if not rows:
            return None
        row = rows[0]
        session.expunge(row)
        return json.loads(row.content)
