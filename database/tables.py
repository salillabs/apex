from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)  # "task-{issue_number}-{repo_slug}"
    issue_number = Column(Integer, nullable=False)
    repo = Column(String, nullable=False)
    title = Column(String, nullable=False)
    status = Column(String, nullable=False, default="new")
    priority = Column(String, nullable=False, default="normal")
    branch = Column(String)
    pr_number = Column(Integer)
    failure_count = Column(Integer, nullable=False, default=0)
    response = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProjectRow(Base):
    __tablename__ = "projects"

    repo = Column(String, primary_key=True)  # e.g. "your-org/your-project"
    name = Column(String, nullable=False)
    priority = Column(Integer, nullable=False, default=5)  # 1=highest, 10=lowest
    active = Column(Boolean, nullable=False, default=True)
    local_path = Column(String)
    slack_channel = Column(String)  # Slack channel ID for this project's notifications
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class DecisionRow(Base):
    __tablename__ = "decisions"

    id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False)
    decision_type = Column(String, nullable=False)  # "route", "escalate", "retry"
    decision = Column(String, nullable=False)
    rationale = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False)
    reason = Column(Text, nullable=False)
    options = Column(JSON, nullable=False)
    slack_channel = Column(String)
    slack_ts = Column(String)  # Slack message timestamp — used for threading
    decision = Column(String)  # "approved" or "rejected"
    user_feedback = Column(Text)
    requested_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    responded_at = Column(DateTime)


class ImplementationRow(Base):
    __tablename__ = "implementations"

    id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False)
    branch = Column(String, nullable=False)
    files_changed = Column(JSON)
    summary = Column(Text)
    success = Column(Boolean, nullable=False)
    error = Column(Text)
    attempt_number = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TestResultRow(Base):
    __tablename__ = "test_results"

    id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False)
    branch = Column(String, nullable=False)
    tests_run = Column(Integer, nullable=False)
    tests_passed = Column(Integer, nullable=False)
    tests_failed = Column(Integer, nullable=False)
    failures = Column(JSON)
    success = Column(Boolean, nullable=False)
    raw_output = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class KnowledgeRow(Base):
    __tablename__ = "knowledge"

    id = Column(String, primary_key=True)
    project = Column(String)
    source_type = Column(String, nullable=False)  # "pr", "adr", "decision"
    source_ref = Column(String)
    content = Column(Text, nullable=False)
    tags = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ChannelProjectRow(Base):
    __tablename__ = "channel_projects"

    channel_id = Column(String, primary_key=True)  # Slack channel ID e.g. "C08ABCD1234"
    repo = Column(String, nullable=False)           # e.g. "your-org/your-project"
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
