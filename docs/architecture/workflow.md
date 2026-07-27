# Engineering Loop — LangGraph Workflow

The engineering loop is a 12-node LangGraph `StateGraph` in `workflows/engineering_loop.py`. All logic lives in agent functions. The graph is thin wiring only.

---

## State Shape (`LoopState`)

```python
class LoopState(TypedDict):
    run_id: str           # UUID for this issue run (= LangGraph thread_id)
    issue: dict           # Issue serialized via .model_dump(mode="json")
    plan: dict            # Plan.context dict from engineering_manager
    spec: dict            # TechnicalSpec from architect (empty until architect runs)
    review: dict          # ReviewResult from reviewer (empty until reviewer runs)
    branch: str           # Git branch name
    task_id: str          # "task-{issue_number}-{repo-slug}"
    repo: str             # "owner/repo-name"
    status: str           # TaskStatus enum value
    dev_failure_count: int
    result: dict          # Latest agent result payload
    errors: Annotated[list, operator.add]    # Accumulated (reducer: operator.add)
    messages: Annotated[list, operator.add]  # Accumulated log messages (reducer: operator.add)
```

---

## Nodes

| Node | Agent | What it does |
|------|-------|--------------|
| `plan_node` | Engineering Manager | Reads issue, creates plan, assigns route, creates branch |
| `architect_node` | Architect | Analyzes risk, generates TechnicalSpec, saves to Neon, posts GitHub comment |
| `architect_approval_node` | — (interrupt) | Pauses for user approval of spec; resumes with "approved" or "rejected" |
| `developer_node` | Developer | Triggers Claude Code in managed-workspace via `claude_code.execute()` |
| `dev_failure_gate_node` | — (interrupt) | Pauses after 2 dev failures; user chooses retry or abandon |
| `qa_run_node` | QA | Runs test suite, records pass/fail counts and failure details |
| `qa_gate_node` | QA + Reporter (interrupt) | Sends bug report to Slack; pauses for user decision on test failures |
| `pr_create_node` | — | Creates GitHub PR (idempotent — reuses existing open PR for branch) |
| `reviewer_node` | Reviewer | Reads PR diff + spec from Neon, posts GitHub review, returns ReviewResult |
| `merge_approval_node` | Reporter (interrupt) | Sends PR-ready Slack message; pauses for user merge approval |
| `merge_node` | — | Squash-merges the PR via GitHub API |
| `reporter_node` | Reporter | Posts completion summary to Slack |

---

## Graph Flow

```
START
  │
  ▼
plan_node
  │
  ├─ action="route_to_architect" ──► architect_node
  │                                       │
  │                                       ├─ requires_approval=True ──► architect_approval_node
  │                                       │                                     │
  │                                       │                          approved ──► developer_node
  │                                       │                          rejected ──► END
  │                                       │
  │                                       └─ requires_approval=False ──► developer_node
  │
  ├─ action="route_to_developer" ──► developer_node
  │
  └─ action="end" ──► END
                                          │
                          success=True ──► qa_run_node
                                          │
                    success=False (< 2 attempts) ──► developer_node (retry)
                    success=False (≥ 2 attempts) ──► dev_failure_gate_node
                                                           │
                                                    retry ──► developer_node
                                                   abandon ──► END
                  qa_run_node
                       │
             tests pass ──► pr_create_node ──► reviewer_node
             tests fail ──► qa_gate_node          │
                                │           approved ──► merge_approval_node
                      retry_fix ──► developer_node  │
                      abandon ──► END        rejected ──► developer_node
                                     merge_approval_node
                                               │
                                     approved ──► merge_node ──► reporter_node ──► END
                                     rejected ──► END
```

---

## Routing Functions

| Router | Called after | Returns |
|--------|-------------|---------|
| `route_after_plan` | `plan_node` | `architect_node` · `developer_node` · `END` |
| `route_after_architect` | `architect_node` | `architect_approval_node` · `developer_node` |
| `route_after_architect_approval` | `architect_approval_node` | `developer_node` · `END` |
| `route_after_developer` | `developer_node` | `qa_run_node` · `developer_node` · `dev_failure_gate_node` |
| `route_after_dev_failure_gate` | `dev_failure_gate_node` | `developer_node` · `END` |
| `route_after_qa_run` | `qa_run_node` | `pr_create_node` · `qa_gate_node` |
| `route_after_qa_gate` | `qa_gate_node` | `developer_node` · `END` |
| `route_after_reviewer` | `reviewer_node` | `merge_approval_node` · `developer_node` |
| `route_after_merge_approval` | `merge_approval_node` | `merge_node` · `END` |

`pr_create_node → reviewer_node` and `merge_node → reporter_node → END` are unconditional edges.

---

## Interrupt Points (Human-in-the-Loop)

Four nodes pause execution and wait for a Slack button click:

1. **`architect_approval_node`** — User approves or rejects the technical spec before implementation begins.
2. **`qa_gate_node`** — Tests failed; user chooses to retry fix or abandon.
3. **`merge_approval_node`** — PR is ready; user approves merge or rejects with feedback.

Also pauses on repeated dev failure:
4. **`dev_failure_gate_node`** — Developer has failed twice; user chooses retry or abandon.

Each interrupt is resumed via `/webhook/slack` → `resume_with_decision(graph, run_id, decision)`.

---

## Persistence

- Development: `InMemorySaver` (state lost on restart)
- Production target: `AsyncPostgresSaver` backed by Neon

The `run_id` (= LangGraph `thread_id`) is stored in `_run_id_by_task` in `main.py` to map Slack approval callbacks back to the correct graph execution.

---

## Entry Points

```python
# Start a new loop for an issue
run_id = await start_issue(graph, issue)

# Resume after a Slack button click
await resume_with_decision(graph, run_id, "approved")  # or "rejected", "retry_fix", "retry", "abandon"
```
