"""
Core Pydantic domain models shared across all agents.
"""
from datetime import datetime
from enum import Enum
from pydantic import BaseModel


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class TaskStatus(str, Enum):
    NEW = "new"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    IN_PROGRESS = "in_progress"
    TESTING = "testing"
    REVIEWING = "reviewing"
    AWAITING_MERGE = "awaiting_merge"
    MERGED = "merged"
    FAILED = "failed"
    ABANDONED = "abandoned"


class Issue(BaseModel):
    id: int
    number: int
    title: str
    body: str
    repo: str
    labels: list[str]
    created_at: datetime


class Plan(BaseModel):
    issue: Issue
    summary: str
    needs_architect: bool
    priority: Priority
    urgency: str
    context: dict


class TechnicalSpec(BaseModel):
    issue: Issue
    summary: str
    files_to_create: list[dict]
    files_to_modify: list[dict]
    api_changes: list[dict]
    database_changes: list[dict]
    env_vars: list[dict]
    dependencies: list[dict]
    test_requirements: list[str]
    risks: list[str]
    requires_approval: bool


class SimpleTask(BaseModel):
    issue: Issue
    description: str
    branch: str


class ImplementationReport(BaseModel):
    issue: Issue
    branch: str
    files_changed: list[str]
    summary: str
    success: bool
    error: str | None = None


class TestResult(BaseModel):
    branch: str
    tests_run: int
    tests_passed: int
    tests_failed: int
    failures: list[dict]
    success: bool


class BugReport(BaseModel):
    test_result: TestResult
    summary: str
    likely_causes: list[str]
    options: list[str]


class ReviewResult(BaseModel):
    pr_number: int
    pr_url: str
    approved: bool
    issues_found: list[str]
    summary: str


class ApprovalRequest(BaseModel):
    id: str
    issue: Issue | None = None
    reason: str
    summary: str
    options: list[str]


class SlackMessage(BaseModel):
    channel: str
    text: str
    blocks: list[dict] | None = None


class AgentResult(BaseModel):
    success: bool
    next_agent: str | None
    payload: dict
    error: str | None = None


class KnowledgeQuery(BaseModel):
    question: str
    project_id: str | None = None
    context: dict | None = None


class KnowledgeAnswer(BaseModel):
    question: str
    answer: str
    sources: list[str]
    has_gaps: bool


class RiskAssessment(BaseModel):
    issue: Issue
    breaking_changes: list[str]
    security_concerns: list[str]
    cost_implications: list[str]
    requires_user_approval: bool
    approval_reason: str | None = None


class Assignment(BaseModel):
    issue: Issue
    agent: str  # "architect" | "developer"
    branch: str
    instructions: str
    context: dict
