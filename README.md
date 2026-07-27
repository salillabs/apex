# APEX

Autonomous engineering organization. APEX manages your GitHub projects via AI agents — handling the full software delivery loop from issue intake to merged PR, with human approval at every key decision point.

You interact via the **Web UI** or **Slack** to review plans, approve architecture decisions, review PRs, and handle test failures. APEX handles everything in between.

---

## How It Works

```
GitHub Issue
    ↓
Engineering Manager — plans, decides route
    ↓ (complex issues)
Architect — generates full technical spec
    → if risky: Web UI / Slack → you approve or reject
    ↓
Developer — triggers Claude Code in your project directory
    ↓
QA — runs tests
    → if failures: Web UI / Slack → you approve retry or abandon
    ↓
Reviewer — reads PR diff, approves or requests changes
    ↓
Web UI / Slack → you approve merge or reject with feedback
    ↓
APEX merges → Reporter posts completion summary
```

**You are always in the loop for:** architecture decisions · test failures · PRs ready to merge · anything risky.
**APEX decides autonomously:** routine implementation · test reruns · minor fixes · task sequencing.

---

## Prerequisites

- Python 3.12+
- Claude Code CLI (`claude --print` must work — requires a Claude Pro or Max subscription)
- PostgreSQL database — local, [Neon](https://neon.tech), Supabase, Railway, or any other host
- GitHub personal access token with `repo` scope
- Slack app with Bot Token and Signing Secret _(required for Slack integration; optional if using the Web UI only)_

---

## Setup

```bash
# macOS / Linux / Git Bash
bash setup.sh
```

```bat
# Windows
setup.bat
```

This creates the virtual environment, installs dependencies, and copies `.env.example` → `.env` and `projects.yaml.example` → `projects.yaml` if they don't exist yet.

Edit both files with your credentials and repo list before starting.

Database tables are created automatically on first startup. No migration command needed.

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | Personal access token with `repo` scope |
| `GITHUB_WEBHOOK_SECRET` | Random string — set the same value in your GitHub repo Webhook settings |
| `DATABASE_URL` | PostgreSQL connection string (`postgresql://user:pass@host/db`) |
| `UI_SECRET` | Password to access the Web UI — anyone without it gets a login page |
| `SLACK_ENABLED` | Set to `true` to enable Slack notifications and approval requests |
| `SLACK_BOT_TOKEN` | _(Required when `SLACK_ENABLED=true`)_ Bot User OAuth Token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | _(Required when `SLACK_ENABLED=true`)_ Slack app → Basic Information → Signing Secret |
| `SLACK_CHANNEL_ID` | _(Required when `SLACK_ENABLED=true`)_ Default channel ID for notifications (`C...`) |

### Registering Projects

Edit `projects.yaml` (gitignored — never committed) to tell APEX which repos to manage:

```yaml
- repo: your-org/your-project
  name: Your Project
  priority: 5
  local_path: /path/to/local/clone
  slack_channel: CXXXXXXXXXX   # omit if not using Slack
```

Projects in `projects.yaml` are loaded automatically on startup and appear in the Web UI sidebar. If you use Slack, you can also link a channel to a repo by sending `@apex --register your-org/your-project` from that channel — this lets Slack commands from that channel target the right repo without specifying it each time.

---

## Running

```bash
# macOS / Linux / Git Bash
bash start.sh
```

```bat
# Windows
start.bat
```

APEX listens on `http://localhost:8000`.

---

## Web UI

APEX ships a built-in dashboard at `http://localhost:8000`.

### Features

**Sidebar** — lists every project from `projects.yaml`; click one to switch context.

**Dashboard tab** (per project):
- **Commands panel** — quick-action buttons (`Next Issues`, `Status`, `Shipped`, `Test Report`, `Review Code`, `Run Tests`) and a free-text input that accepts any `--build #42` command or a natural-language question about the codebase. Responses are rendered inline with Markdown.
- **Work on GitHub Issue** — enter an issue number and click Start to trigger the full engineering loop (Engineering Manager → Architect → Developer → QA → Reviewer → Reporter). Approvals surface in the Pending Approvals panel below.
- **Pending Approvals** — all decisions waiting for a human (architecture reviews, test-failure retries, PR merges) appear here with Approve / Reject buttons.

**Tasks tab** (per project):
- Paginated history of every task APEX has run, with status badges (`new`, `in_progress`, `review`, `merged`, `done`, `failed`) and expandable response text.

> The UI has no authentication — it is designed for local and self-hosted use. Do not expose it to the public internet without adding your own auth layer.

---

## Slack Commands

```
@apex --next                List open issues ready to build
@apex --build #42           Build issue #42 (or --build #42 #43 for multiple)
@apex --shipped             Show recently merged PRs
@apex --status              Show active build queue
@apex --review              Review recent code changes
@apex --test                Run the test suite and report results
@apex --report              Show last test report from history
@apex --register org/repo   Link this Slack channel to a repo
@apex --help                Show this message
@apex <question>            Ask anything about the codebase
```

---

## Exposing APEX to the Internet

GitHub and Slack need a public URL to reach your local APEX server. Use one of the two options below.

---

### Option A — Cloudflare Tunnel _(recommended for persistent setups)_

Gives APEX a permanent public URL tied to your own domain. No ports to open, no static IP needed.

**1. Install cloudflared**
```bash
# macOS
brew install cloudflare/cloudflare/cloudflared

# Windows (winget)
winget install Cloudflare.cloudflared
```

**2. Authenticate**
```bash
cloudflared tunnel login
```
This opens your browser — select the domain you want to use (e.g. `example.in`).

**3. Create the tunnel**
```bash
cloudflared tunnel create apex
```
Note the tunnel ID printed — you'll need it in the next step.

**4. Create the config file**

Create `~/.cloudflared/config.yml`:
```yaml
tunnel: <your-tunnel-id>
credentials-file: /Users/<you>/.cloudflared/<your-tunnel-id>.json

ingress:
  - hostname: apex.example.in
    service: http://localhost:8000
  - service: http_status:404
```

**5. Route your domain to the tunnel**
```bash
cloudflared tunnel route dns apex apex.example.in
```
This creates a DNS record in Cloudflare automatically — no manual dashboard work needed.

**6. Run the tunnel**
```bash
# macOS / Linux
cloudflared tunnel --config ~/.cloudflared/config.yml run apex

# Windows
cloudflared tunnel --config "C:\Users\<you>\.cloudflared\config.yml" run apex
```

APEX is now reachable at `https://apex.example.in`.

---

### Option B — ngrok _(quick local testing)_

No domain or account needed. Gives a temporary URL that changes every time you restart.

```bash
ngrok http 8000
```

ngrok prints a `https://*.ngrok.io` URL — use that as your public URL below. Update GitHub and Slack webhook URLs each time you restart ngrok.

---

### Webhook Configuration

Once APEX is publicly reachable (via either option above), register the URL in GitHub and Slack.

#### GitHub — repo Settings → Webhooks

- URL: `https://your-public-url/webhook/github`
- Content type: `application/json`
- Secret: value from `GITHUB_WEBHOOK_SECRET`
- Events: Issues

Repeat for every repo you want APEX to manage.

#### Slack — Interactivity & Shortcuts

- Request URL: `https://your-public-url/webhook/slack`

#### Slack — Event Subscriptions

- Request URL: `https://your-public-url/webhook/slack`
- Subscribe to bot event: `app_mention`

#### Slack — OAuth Scopes

| Scope | Purpose |
|-------|---------|
| `chat:write` | Send messages |
| `chat:write.public` | Post to channels without joining |
| `app_mentions:read` | Receive `@apex` mentions |

---

## Project Structure

```
apex/
├── agents/              ← Agent logic (all intelligence lives here)
│   ├── engineering_manager.py
│   ├── architect.py
│   ├── developer.py
│   ├── qa.py
│   ├── reviewer.py
│   ├── reporter.py
│   └── knowledge_curator.py
├── workflows/
│   └── engineering_loop.py   ← LangGraph graph (thin wiring only)
├── services/
│   ├── github.py             ← PyGitHub wrapper
│   ├── slack.py              ← Slack SDK wrapper
│   ├── db.py                 ← SQLAlchemy + PostgreSQL
│   └── claude_code.py        ← Claude Code CLI subprocess
├── prompts/             ← System prompts per agent (fully customizable)
├── static/
│   └── index.html            ← Web UI (served at /ops)
├── models/domain.py     ← Pydantic models
├── database/            ← SQLAlchemy table definitions
├── docs/
│   └── architecture/    ← System design docs
├── projects.yaml        ← Your managed repos (gitignored — never committed)
├── projects.yaml.example← Template for projects.yaml
├── main.py              ← FastAPI entry point
└── tests/
```

---

## Customizing Agent Behavior

Every agent is driven by a prompt file in `prompts/`. Edit them to change how agents think, what they escalate, and how they communicate. The prompts are the core product — they determine decision quality.

```
prompts/
├── engineering_manager.md   ← Prioritization, planning, escalation rules
├── architect.md             ← How to analyze, design, flag risks
├── developer.md             ← How to interpret specs, trigger Claude Code
├── qa.md                    ← How to evaluate tests, write bug reports
├── reviewer.md              ← Code review standards
├── reporter.md              ← Completion summary format
└── knowledge_curator.md     ← How to store and retrieve project knowledge
```

---

## Implementation Status

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Research docs (`docs/research/`) | ✅ Complete |
| 1 | Agent contracts + prompts | ✅ Complete |
| 2 | Core loop: Issue → implement → test → PR → merge | ✅ Complete |
| 3 | Architect, full QA cycle, Reviewer, Reporter | ✅ Complete |
| 4 | Multi-project portfolio management | ✅ Complete |
| 5 | Production hardening | Planned |

---

## License

GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).

© 2026 salillabs — https://github.com/salillabs/apex

**Personal and self-hosted use is free and encouraged.**
Commercial use — including running APEX as a service or embedding it in a commercial product — requires written approval. Reach out at salil@salillabs.in to discuss.
