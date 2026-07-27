# APEX Agent Roles — Who Does What and Why

## The Simple Version

Think of APEX like a small software company inside your phone.

| Role | Human Equivalent | One-Line Job |
|------|-----------------|--------------|
| Engineering Manager | Tech Lead / PM | Reads the issue, decides the plan, tracks everything, talks to you |
| Architect | Senior Engineer | Designs HOW to build it — which files, which patterns, what to change |
| Developer | Mid-level Engineer | Executes the plan — tells Claude Code exactly what to write |
| QA | QA Engineer | Runs tests, reads results, writes bug reports for failures |
| Reviewer | Code Reviewer | Reads the final code, checks quality before it goes to you |
| Reporter | Chief of Staff | Writes your Slack summaries — short, clear, decision-ready |
| Knowledge Curator | Librarian | Remembers past decisions and patterns so agents don't repeat mistakes |

---

## The Clearest Way to Understand the Split

### Engineering Manager vs Architect vs Developer

Use this example: **"Add Stripe payments to the your-org/billing-service"**

**Engineering Manager receives the GitHub Issue.**

It asks:
- Is this big or small?
- Does it need a design, or can someone just implement it?
- Does the user need to approve anything before we start?

Decision: *"This is complex — touches money, external API, new dependencies. Needs Architect."*

Actions:
- Creates a plan
- Pings you on Slack: *"Starting payments feature. Routing to Architect for design first."*
- Assigns it to Architect

---

**Architect receives the plan.**

It reads:
- The GitHub Issue
- The existing codebase (via Claude Code reading managed-workspace)
- Relevant knowledge from memory (past decisions about this service)

It produces a **technical spec**:
- Which files to create and which to modify
- What the API endpoints look like
- What the database schema change is
- What environment variables are needed
- What external dependencies to add
- What tests are expected

Decision: *"This adds a new payment table — schema change needs your approval."*

Slack to you: *"Payments spec ready. Adding `payments` table with these columns. Approve?"*

You say yes → Architect hands spec to Developer.

---

**Developer receives the spec.**

It does NOT design anything. It executes.

It:
- Takes the Architect's spec
- Tells Claude Code exactly what to build (using the spec as instructions)
- Monitors Claude Code as it writes files in `D:\managed-workspace`
- Reports back: done or failed

Developer never makes design decisions. If something in the spec is unclear or impossible, it escalates back to Architect — it does not improvise.

---

### Small Issue Example: "Fix typo in dashboard title"

**Engineering Manager** receives it.

Decision: *"Trivial. No design needed. Goes straight to Developer."*

**Architect is not involved.**

**Developer** triggers Claude Code → fixes typo → done.

**QA** runs tests → all pass → done.

**Reporter** sends you: *"Fixed: typo in dashboard title. PR #47 merged."*

---

## Decision Tree: Which Agent Handles What?

```
New GitHub Issue arrives
        ↓
Engineering Manager reads it
        ↓
Is it trivial (typo, copy change, config tweak)?
    YES → Developer directly → QA → Reviewer → Merge
    NO  ↓
Does it need design (new feature, new API, schema change, new dependency)?
    YES → Architect → spec → Developer → QA → Reviewer → Merge
    NO  ↓
Is it a bug fix in existing code?
    → Developer (with context from Knowledge Curator) → QA → Reviewer → Merge
```

---

## What Each Agent CAN and CANNOT Do

### Engineering Manager
**CAN:**
- Read and interpret GitHub Issues
- Create a work plan
- Decide whether Architect is needed
- Decide what needs your approval
- Assign work to other agents
- Ping you on Slack
- Track progress across multiple projects

**CANNOT:**
- Write code or design specs
- Decide technical implementation details
- Approve its own escalations (that's you)

---

### Architect
**CAN:**
- Read the existing codebase
- Produce a full technical spec
- Decide file structure, API design, data models
- Flag decisions that need your approval (breaking changes, security, cost)
- Query Knowledge Curator for past decisions

**CANNOT:**
- Write production code
- Trigger Claude Code
- Make final approval calls (that's you)
- Make decisions that contradict an existing ADR without flagging it

---

### Developer
**CAN:**
- Receive a spec from Architect (or a simple task from Engineering Manager)
- Trigger Claude Code with precise instructions
- Monitor Claude Code execution
- Report success or failure
- Escalate back to Architect if the spec has a gap

**CANNOT:**
- Make design decisions
- Change the spec
- Decide whether tests pass or fail (that's QA)
- Merge code (that's Engineering Manager after your approval)

---

### QA Agent
**CAN:**
- Run the test suite in managed-workspace
- Read test output and interpret pass/fail
- Write a structured bug report for failures
- Send bug report to you via Slack if failures are significant
- Approve retry (minor failures) or escalate (major failures)

**CANNOT:**
- Fix bugs (that goes back to Developer)
- Decide to skip tests
- Merge code

---

### Reviewer
**CAN:**
- Read the final PR diff
- Check against coding standards
- Flag security concerns, missing tests, poor naming
- Post a review summary to Slack for your awareness
- Approve or request changes

**CANNOT:**
- Make architecture changes
- Fix code directly
- Merge code

---

### Reporter
**CAN:**
- Write Slack summaries at each decision point
- Format information clearly for mobile reading
- Include exactly what you need to approve/reject
- Post completion notifications

**CANNOT:**
- Make any decisions
- Take any actions
- Modify other agents' outputs

---

### Knowledge Curator
**CAN:**
- Store decisions, patterns, and lessons in Neon
- Answer queries from other agents ("how did we handle auth last time?")
- Index new PRs and ADRs as they are merged
- Maintain the docs/research/ library

**CANNOT:**
- Make implementation decisions
- Override ADRs
- Take direct action on issues or PRs

---

## What You See on Slack

APEX contacts you in exactly four situations:

| When | What you receive | What you do |
|------|-----------------|-------------|
| Strategic decision needed | Short summary + approve/reject buttons | Approve or reject |
| Test failures | Bug report + options (retry / abandon / override) | Choose an option |
| PR ready to merge | PR summary + link | Approve merge or reject with feedback |
| Task completed | Completion summary | Read and move on |

Everything else happens without interrupting you.

---

## Summary in One Sentence Each

- **Engineering Manager:** Reads the issue, makes the plan, decides who does what, talks to you.
- **Architect:** Designs the solution in detail so the Developer has zero ambiguity.
- **Developer:** Executes the spec via Claude Code — no design, pure execution.
- **QA:** Runs tests, interprets results, surfaces failures to you.
- **Reviewer:** Checks final code quality before it reaches you.
- **Reporter:** Writes your Slack messages — short, clear, actionable.
- **Knowledge Curator:** Remembers everything so APEX gets smarter over time.
