# APEX — Codex Agent Instructions

## What APEX Is

APEX is a **Digital Chief of Staff and autonomous engineering organization** built in Python.

The user interacts via **Slack and mobile** to review test reports, bug reports, PRs, and approve or reject decisions. APEX manages multiple software projects autonomously.

---

## Two Workspaces — Never Mix Them

```
/path/to/apex/             ← APEX (Python) — THIS repo — the orchestrator
/path/to/managed-projects/ ← Projects APEX manages — DO NOT TOUCH (set via MANAGED_WORKSPACE_ROOT)
```

APEX manages projects **only through the GitHub API**. Claude Code runs inside the managed project directories to write code. APEX never directly edits those files.

---

## End-to-End Flow

```
GitHub Issue
    ↓
Engineering Manager → plans, prioritizes
    → strategic decision? → Slack to user → await approval
    ↓
Architect (complex issues) → generates spec
    → architecture decision? → Slack to user → await approval/rejection
    ↓
Developer Agent → triggers Claude Code in managed project directory
    ↓
QA Agent → collects test results
    → failures? → bug report → Slack to user → approve retry or reject
    ↓
Reviewer Agent → reviews PR → posts summary to Slack
    ↓
User (Slack/mobile) → approve merge or reject with feedback
    ↓
Reporter → posts completion to Slack
```

---

## LangGraph vs Engineering Manager

These work together but are different:

| LangGraph | Engineering Manager |
|-----------|---------------------|
| Tracks WHERE the task is | Decides WHAT to do next |
| Resumes after crash | Decides IF escalation needed |
| Routes to next agent | Decides HOW to prioritize |
| Waits for Slack approval | Writes the Slack message |

**LangGraph = runtime + state + routing.**
**Engineering Manager = decisions + judgment + communication.**

LangGraph graph nodes call agent functions. All decision logic stays inside agent functions, never inside the graph definition itself.

---

## Prompts Are the Core Product

Every agent is a Claude API call with a system prompt. Prompts define behavior, authority, output format, and escalation rules.

```
apex/prompts/
├── engineering_manager.md
├── architect.md
├── developer.md
├── qa.md
├── reviewer.md
├── reporter.md
└── knowledge_curator.md
```

**Do not write or modify prompts.** Prompts are designed by the developer, not generated. Read them before implementing agents — they define expected inputs and outputs.

---

## Repository Structure

```
apex/
├── agents/
│   ├── engineering_manager.py
│   ├── architect.py
│   ├── developer.py
│   ├── qa.py
│   ├── reviewer.py
│   ├── reporter.py
│   └── knowledge_curator.py
│
├── workflows/
│   └── engineering_loop.py    ← LangGraph graph (thin wiring only)
│
├── services/
│   ├── github.py              ← PyGitHub wrapper
│   ├── slack.py               ← Slack SDK wrapper
│   ├── neon.py                ← SQLAlchemy + Neon
│   └── claude_code.py         ← Triggers Claude Code CLI in managed project directories
│
├── prompts/                   ← System prompts per agent (READ, don't modify)
├── models/                    ← Pydantic models
├── database/                  ← SQLAlchemy + Alembic
│
├── docs/
│   ├── research/              ← READ BEFORE IMPLEMENTING
│   ├── adr/                   ← Architecture decisions
│   ├── contracts/             ← Agent specs — READ BEFORE IMPLEMENTING AGENTS
│   └── architecture/
│
└── tests/
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| Orchestration | LangGraph |
| API | FastAPI |
| ORM | SQLAlchemy 2.x + Alembic |
| Validation | Pydantic v2 |
| Database | Neon (Postgres) |
| LLM | Claude Code CLI via subprocess (`claude --print`) — Claude Pro, no API key |
| Slack | Slack SDK |
| GitHub | PyGitHub |
| Testing | pytest |
| Linting | Ruff |
| Types | mypy |

---

## Coding Rules

1. Read `docs/contracts/<agent>.md` before implementing any agent. Stop if missing.
2. Read `prompts/<agent>.md` before implementing any agent. Stop if missing.
3. Read `docs/research/` before implementing any feature.
4. No business logic in LangGraph graphs — graphs only call agent functions and route.
5. All external calls go through `services/`. Never raw API calls inside agents.
6. All config from environment variables. No hardcoded values.
7. Type hints everywhere. mypy must pass.
8. pytest for every public function.
9. Ruff format + check must pass.
10. Never touch `../research/` or the managed project directories (set via `MANAGED_WORKSPACE_ROOT`).

---

## Build Order

1. ✅ **Phase 0** — Research docs in `docs/research/` (complete)
2. ✅ **Phase 1** — Contracts in `docs/contracts/` + prompts in `prompts/` (complete)
3. ✅ **Phase 2** — Core loop: Issue → plan → implement → test → PR → Slack → user approves → merge (complete)
4. ✅ **Phase 3** — Architect, full QA with bug reports, Reviewer, Reporter — 12-node LangGraph graph wired (complete)
5. **Phase 4** — Multi-project portfolio management (planned)
6. **Phase 5** — Production hardening (planned)

See `docs/architecture/workflow.md` for the complete graph reference.
