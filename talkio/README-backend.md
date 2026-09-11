# Sportstech Agent Hub — Backend

FastAPI + SQLite server for a multi-user chat platform with persistent AI agents.
Handles authentication, messaging over WebSockets, agent execution and tool
calling, scheduled runs, and MCP connectors.

The frontend is a separate package. See its README for the browser and desktop
app.

---

## Requirements

- **Python 3.11+**
- **Ollama** running locally, if you want workspace-hosted models
- No database server: SQLite lives in a single file next to the code

---

## Quick start

```bash
python -m venv venv
venv\Scripts\activate            # Windows
source venv/bin/activate         # macOS / Linux

pip install -r requirements.txt
pip install tzdata               # Windows only — see Timezones below

# Minimum viable configuration
echo DEV_MODE=false >> .env
python -c "import secrets;print('JWT_SECRET='+secrets.token_urlsafe(48))" >> .env

uvicorn main:app --host 0.0.0.0 --port 8000
```

Open the app and you'll be shown a **first-time setup** screen. Create the first
account there; it becomes the admin. Everyone else is added from
**Settings → People & access** inside the app.

There is no command-line step for creating users. `create_user.py`,
`import_users.py` and `list_users.py` exist for bulk or recovery work, but the
normal path is the setup screen.

---

## Configuration

All settings come from environment variables, or a `.env` file beside `main.py`.

### Security — set these before anyone else can reach the server

| Variable | Default | Notes |
|---|---|---|
| `JWT_SECRET` | *(random each start)* | Signs session tokens. **Set it.** Without it a new secret is generated on every restart, signing everyone out. |
| `DEV_MODE` | `false` | `true` exposes `/api/auth/dev`, a passwordless login that bypasses every other control. Never enable it on a reachable server. |

Both print a warning at startup when they're in an unsafe state. Read the startup
log.

### Models

| Variable | Default | Notes |
|---|---|---|
| `LOCAL_LLM_URL` | `http://localhost:11434` | Ollama endpoint for workspace models |
| `LOCAL_LLM_DEFAULT_MODEL` | *(first available)* | Fallback when an agent names no model |
| `VISION_MODEL` | — | Separate model for `analyze_image`; without it images can't be read |
| `OLLAMA_DETECT_PORT` | `11434` | Port scanned by the detect endpoint |
| `ANTHROPIC_API_KEY` | — | Optional server-side key. Personal Claude/Gemini keys live on each user's machine instead and never reach the server. |
| `ANTHROPIC_MODEL` | — | Default model for the server-side key |

### Everything else

| Variable | Default | Notes |
|---|---|---|
| `AGENT_WORKSPACE` | `./workspace` | Folder agents may read and write with the workspace-files capability |
| `BACKUP_DIR` | `./backups` | Nightly snapshots |
| `BACKUP_KEEP` | `14` | Snapshots retained before the oldest is pruned |
| `TZ` | `UTC` | Default timezone for schedules created without one |
| `GOOGLE_CLIENT_ID` | — | Optional Google sign-in alongside passwords |

---

## Modules

| File | Responsibility |
|---|---|
| `main.py` | HTTP and WebSocket endpoints, connection hub, agent dispatch |
| `database.py` | Schema, migrations, every SQL statement, backups, full-text search |
| `auth.py` | JWT issue and verify, Google token verification |
| `passwords.py` | Password hashing, the password policy, allowlist, lockout |
| `agents.py` | Agent prompts, the tool loop, built-in tools, provider routing, delegation and the run context |
| `mcp.py` | Routes MCP tool calls to the user's desktop app — **stores no tokens** |
| `scheduler.py` | Schedule storage, the 30-second tick loop, REST API |
| `schedule_time.py` | Pure timezone and DST arithmetic. No I/O, heavily tested. |
| `bot.py` | The default assistant persona |

**`mcp.py` deliberately holds no credentials.** Connector OAuth runs on each
user's own machine and tokens stay there, so a server compromise doesn't expose
anyone's Notion or Figma account.

---

## Data model

Thirteen tables. The ones worth knowing:

- **`users`** — people and agent bot accounts, distinguished by `is_bot`. Carries
  `password_hash`, `must_change_password`, `failed_attempts`, `locked_until` and
  `token_version` (bumping it invalidates every live session).
- **`conversations`** / **`members`** — DMs, groups and channels. `members` holds
  `last_read_at` for unread counts and `hidden_at`, a per-user watermark so one
  person removing a chat doesn't destroy it for everyone else.
- **`messages`** — mirrored into a `messages_fts` FTS5 index by triggers.
- **`agents`** — configuration, capabilities, connector list, provider and model.
- **`agent_access`** — who else may use an agent. This is what makes an agent
  shared rather than personal.
- **`agent_notes`** — what an agent chose to remember, per agent.
- **`agent_schedules`** / **`agent_schedule_runs`** — recurring tasks and history.
- **`audit_log`** — sign-ins, deletions, access grants, and agent actions that
  touch a user's machine.

Migrations are additive `ALTER TABLE` statements in a `try/except` list, so
starting a newer build against an existing database is safe.

### The database file

`sportstech.db` beside the code. If a `talkio.db` from an earlier build exists,
it is used instead — renaming it outright would silently create an empty
database and every conversation would appear to vanish.

To start clean: **stop the server**, delete `*.db`, `*.db-wal`, `*.db-shm`, and
restart. Tables are recreated automatically.

---

## API

64 endpoints. Everything under `/api/` except `/api/config`, `/api/auth/*`
requires `Authorization: Bearer <token>`.

**Auth** — `POST /api/auth/setup` (first run only), `/login`,
`/change-password`, `/logout_all`, `/google`

**Admin** — `GET|POST /api/admin/users`, and per-user `reset-password`,
`revoke`, `unlock`

**Conversations** — CRUD, members, roles, messages, uploads, read markers,
shared media, `runs`

**Runs** — `GET /api/runs/{id}`, `POST /api/runs/{id}/cancel`,
`POST /api/approvals/{id}`

**Agents** — CRUD, `share`, `diagnose`, `requests`, plus `stop` and `rerun` on a
conversation

**Connectors** — `test`, `connect`, `disconnect`, `status`, `token`, `client`

**Other** — `search` (full-text), `audit`, `models`, `devices`, `files/{name}`

Useful when something misbehaves:

- `GET /api/agents/{id}/diagnose` — the tools an agent actually receives, its
  connector states, and a list of likely problems in plain words
- `GET /api/audit` — what has happened, newest first

---

## Runs, delegation and capabilities

### The run context

Every request creates a `RunContext` shared by every agent it reaches, however
deep the delegation goes. It holds the run id, the requester, the authorized
artifact ids, the chain, and **shared budgets**: total tool calls, delegations,
wall-clock time and a cancel flag.

Shared is the important word. Children hold the same state object by reference,
not a copy, so cancelling a run stops agents that were already running, and a
chain of three cannot spend three times a single agent's allowance.

Two identities are deliberately kept apart:

- **requester** — the human who asked. Local-file access is theirs, and never
  becomes the owner's because an agent delegated.
- **owner** — whose API key pays. Follows the agent being run.

### Delegation

`ask_agent` relays a message and gets prose back. `delegate_task` assigns work:
it carries objective, context, constraints, deliverable and completion criteria,
and returns a structured result with status, summary, artifact ids and blockers.

If an agent returns words where a file was asked for, the status is
`returned_text_only` and the coordinator is told it is not done. A written
promise is not a deliverable.

Depth is capped at three agents total (Manager → Editor → Specialist), and an
agent already in the chain is invisible downstream. **Both are enforced in the
executor, not only in tool discovery** — a model that names a tool it wasn't
offered cannot reach its handler.

### Capabilities

Five flags, meaning one thing in discovery and in execution:

| Flag | Grants |
|---|---|
| `tool_calling` | any tool at all; off means conversation only |
| `multi_agent` | `ask_agent` and `delegate_task` |
| `web_scraping` | `web_search`, `fetch_url` |
| `files` | the shared workspace folder |
| `local_files` | the requester's own machine |

**A connected desktop app is not enough for `local_files`.** The capability must
be on as well; before this, plugging in the desktop silently gave every agent
that person's disk.

Legacy agents predate some flags. The migration policy is explicit in
`CAP_DEFAULTS`: absent flags keep their old behaviour, except `local_files`,
which defaults **off** — disk access is not something to grant by omission.

### Owner-only API keys

A shared agent runs on its **owner's** key, for chat and for image generation.
There is no fallback to the asker's key: billing a colleague for an agent they
were merely given access to is not the server's decision, and prompting them for
a key is worse. An offline owner produces `waiting_for_owner`, flagged on the
exception so callers can persist it.

### Durable runs and approvals

Five additive tables: `agent_runs`, `run_tasks`, `run_artifacts`, `run_events`,
`run_approvals`. Statuses are queued, running, waiting_for_owner,
waiting_for_input, awaiting_approval, completed, partially_completed, failed,
cancelled.

Artifacts are **versioned**, and an approval binds to one artifact version, one
action, one target and one approver. Revising an artifact marks any pending
approval `superseded` — approving v1 is not approving v2. Execution is claimed
atomically, so a double click, a retry or a second browser tab cannot publish
twice.

On startup, `recover_interrupted_runs()` marks anything left mid-flight as
failed with an honest reason rather than leaving it "running" forever.

**There is no Amazon publishing.** Nothing inspected in the connectors performs
listing writes, so an approved action reports that plainly and leaves the file
in the conversation to be downloaded. Existing listing images are never touched.

## Agents

Three types — **product**, **create** and **personal** — differing in their
system prompt and defaults. Capabilities are per agent: workspace files, the
user's own machine, web access.

Built-in tools include web search and fetch, document reading, image analysis,
calculation, chat search, agent-to-agent delegation, messaging a colleague,
workspace file read/write, memory (`remember` / `recall` / `forget`),
`schedule_reminder`, `list_people`, `list_my_tools`, and the local-machine
tools.

**Local-machine tools run in the user's desktop app**, not on the server. The
server sends a request over that user's WebSocket and waits for a reply. Sensitive
operations prompt for confirmation on their machine, and blocked paths (`.ssh`,
`.aws`, browser profiles, credential stores) are refused outright.

**Agents using a personal Claude or Gemini key need that person's desktop app
running**, because the key and the API call both live there. Scheduled overnight
runs on those agents will fail if the machine is off. Workspace models have no
such constraint.

---

## Scheduling

Five kinds: `daily`, `weekdays`, `weekly` (chosen days), `hourly`, `interval`.

`schedule_time.py` is pure arithmetic with no I/O, which is why it can be tested
exhaustively. Two behaviours that differ deliberately:

- **Wall-clock kinds** (daily, weekly, weekdays) keep their local time across a
  DST change — 09:00 stays 09:00, so the gap is 604800 ± 3600 seconds.
- **Interval kinds** keep their duration — an hourly job stays exactly 3600
  seconds apart across a clock change, and aligns to the hour rather than
  drifting from the last run.

A missed window while the server was down coalesces into a single catch-up run,
not one per missed slot.

### Timezones on Windows

Windows ships no IANA timezone database. Without `tzdata` every schedule silently
runs in UTC:

```bash
pip install tzdata
```

The startup log warns when it's missing.

---

## Operations

**Backups** — a `VACUUM INTO` snapshot daily into `BACKUP_DIR`, keeping
`BACKUP_KEEP`. It's consistent while the server runs, unlike copying the file.

**Restore** — stop the server, replace `sportstech.db` with a snapshot, start.

**Performance** — WAL mode so readers don't block on writers, `synchronous=NORMAL`,
a 10-second busy timeout, and indexes on the hot paths. The conversation-list
query is ~2.4× faster with them at 120k messages.

**Rate limits** — 8 login attempts per minute per IP, 30 uploads per minute per
user, 5 setup attempts per minute. In-process, so they reset on restart.

---

## Tests

```bash
python test_api.py          # any test file runs standalone
for f in test_*.py; do python "$f"; done
```

Roughly 800 assertions across 30 files. Each is self-contained, uses a temporary
database, and prints `N passed, M failed` with a non-zero exit on failure. No
pytest required.

Worth knowing: `test_schedule_time.py` covers real DST transitions,
`test_security.py` covers upload authorisation and rate limiting, and
`test_passwords.py` covers the password policy including leetspeak variants.

---

## Before deploying anywhere public

1. `DEV_MODE=false` — otherwise anyone can sign in as anyone
2. `JWT_SECRET` set to a fixed random value
3. HTTPS in front of it, with WebSocket upgrade allowed
4. `pip install tzdata` on Windows
5. Check the startup log for warnings before assuming it's fine
