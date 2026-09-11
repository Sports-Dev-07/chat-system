"""SQLite database layer for Sportstech v2. Postgres-ready: swap connection + placeholders."""
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

def _db_path() -> Path:
    """sportstech.db going forward, but an existing talkio.db is still used.
    Renaming the file outright would silently start a brand-new empty database
    and every conversation, agent and schedule would appear to have vanished."""
    here = Path(__file__).parent
    new, old = here / "sportstech.db", here / "talkio.db"
    if old.exists() and not new.exists():
        return old
    return new


DB_PATH = _db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    avatar TEXT DEFAULT '',
    title TEXT DEFAULT '',
    is_bot INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('dm','group','channel')),
    name TEXT DEFAULT '',
    created_by TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    joined_at REAL NOT NULL,
    last_read_at REAL DEFAULT 0,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT DEFAULT 'text',          -- text | image | file
    file_name TEXT DEFAULT '',
    file_size INTEGER DEFAULT 0,
    edited INTEGER DEFAULT 0,
    deleted INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji)
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
"""

# columns added in v2 — migrate older DBs in place
MIGRATIONS = [
    "ALTER TABLE members ADD COLUMN last_read_at REAL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT 'text'",
    "ALTER TABLE messages ADD COLUMN file_name TEXT DEFAULT ''",
    "ALTER TABLE messages ADD COLUMN file_size INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN edited INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN deleted INTEGER DEFAULT 0",
    # Per-member "I deleted this chat" watermark. Nothing is removed for anyone
    # else: the row stays, and this member simply stops seeing the conversation
    # and any message older than this timestamp.
    "ALTER TABLE members ADD COLUMN hidden_at REAL DEFAULT 0",
]

# Indexes for lookups that were doing a full table scan. Both run on nearly every
# request: the conversation list on each incoming message, and the agent lookup on
# every turn an agent takes.
# ---------------------------------------------------------------- audit log
AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    at REAL NOT NULL,
    actor_id TEXT,
    actor_name TEXT,
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, at DESC);
"""

AUDIT_KEEP_DAYS = 90


# ---------------------------------------------------------------- runs & approvals
RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    requester_id TEXT NOT NULL,
    coordinator_id TEXT DEFAULT '',
    objective TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    blocker TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_conv ON agent_runs(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON agent_runs(status);

CREATE TABLE IF NOT EXISTS run_tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    parent_id TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    agent_name TEXT DEFAULT '',
    objective TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    summary TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON run_tasks(run_id, created_at);

-- Versioned, because an approval must bind to the exact thing approved.
CREATE TABLE IF NOT EXISTS run_artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT DEFAULT '',
    message_id TEXT DEFAULT '',
    kind TEXT DEFAULT 'file',
    label TEXT DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    supersedes TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON run_artifacts(run_id, created_at);

CREATE TABLE IF NOT EXISTS run_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    agent TEXT DEFAULT '',
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, at);

CREATE TABLE IF NOT EXISTS run_approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_version INTEGER NOT NULL,
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    approver_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_at REAL DEFAULT 0,
    executed_at REAL DEFAULT 0,
    execution_result TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appr_run ON run_approvals(run_id, created_at DESC);
"""

RUN_STATUSES = ("queued", "running", "waiting_for_owner", "waiting_for_input",
                "awaiting_approval", "completed", "partially_completed",
                "failed", "cancelled")


def init_runs():
    with db() as conn:
        conn.executescript(RUNS_SCHEMA)


def create_run(conversation_id: str, requester_id: str, objective: str,
               coordinator_id: str = "") -> dict:
    rid = new_id()
    with db() as conn:
        conn.execute(
            """INSERT INTO agent_runs (id, conversation_id, requester_id, coordinator_id,
                                       objective, status, created_at, updated_at)
               VALUES (?,?,?,?,?,'queued',?,?)""",
            (rid, conversation_id, requester_id, coordinator_id,
             objective[:2000], now(), now()))
    return get_run(rid)


def get_run(run_id: str) -> dict | None:
    with db() as conn:
        r = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            return None
        run = dict(r)
        run["tasks"] = [dict(t) for t in conn.execute(
            "SELECT * FROM run_tasks WHERE run_id = ? ORDER BY created_at", (run_id,))]
        run["artifacts"] = [dict(a) for a in conn.execute(
            "SELECT * FROM run_artifacts WHERE run_id = ? ORDER BY created_at", (run_id,))]
        run["events"] = [dict(e) for e in conn.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY at DESC LIMIT 100",
            (run_id,))]
        run["approvals"] = [dict(a) for a in conn.execute(
            "SELECT * FROM run_approvals WHERE run_id = ? ORDER BY created_at DESC",
            (run_id,))]
    return run


def set_run_status(run_id: str, status: str, blocker: str = "") -> bool:
    if status not in RUN_STATUSES:
        raise ValueError(f"unknown run status {status!r}")
    with db() as conn:
        n = conn.execute(
            "UPDATE agent_runs SET status = ?, blocker = ?, updated_at = ? WHERE id = ?",
            (status, blocker[:500], now(), run_id)).rowcount
    return n > 0


def add_run_task(run_id: str, agent_id: str, agent_name: str, objective: str,
                 parent_id: str = "") -> dict:
    tid = new_id()
    with db() as conn:
        conn.execute(
            """INSERT INTO run_tasks (id, run_id, parent_id, agent_id, agent_name,
                                      objective, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,'queued',?,?)""",
            (tid, run_id, parent_id, agent_id, agent_name, objective[:2000],
             now(), now()))
        return dict(conn.execute("SELECT * FROM run_tasks WHERE id = ?", (tid,)).fetchone())


def set_task_status(task_id: str, status: str, summary: str = "", error: str = ""):
    with db() as conn:
        conn.execute(
            """UPDATE run_tasks SET status = ?, summary = ?, error = ?, updated_at = ?
               WHERE id = ?""",
            (status, summary[:4000], error[:1000], now(), task_id))


def add_run_artifact(run_id: str, message_id: str, label: str, kind: str = "file",
                     task_id: str = "", supersedes: str = "") -> dict:
    """Register a produced file. A revision supersedes its predecessor and gets
    the next version number, so an approval can name an exact version."""
    aid = new_id()
    version = 1
    with db() as conn:
        if supersedes:
            prev = conn.execute("SELECT version FROM run_artifacts WHERE id = ?",
                                (supersedes,)).fetchone()
            if prev:
                version = int(prev["version"]) + 1
        conn.execute(
            """INSERT INTO run_artifacts (id, run_id, task_id, message_id, kind, label,
                                          version, supersedes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (aid, run_id, task_id, message_id, kind, label[:200], version,
             supersedes, now()))
        # A new version invalidates any approval of the old one: approving v1 is
        # not approving v2, and silently carrying it over is how the wrong file
        # gets published.
        if supersedes:
            conn.execute(
                """UPDATE run_approvals SET status = 'superseded'
                   WHERE artifact_id = ? AND status IN ('pending','approved')
                     AND executed_at = 0""", (supersedes,))
        return dict(conn.execute("SELECT * FROM run_artifacts WHERE id = ?",
                                 (aid,)).fetchone())


def add_run_event(run_id: str, kind: str, detail: str = "", agent: str = ""):
    with db() as conn:
        conn.execute(
            "INSERT INTO run_events (id, run_id, at, kind, agent, detail) VALUES (?,?,?,?,?,?)",
            (new_id(), run_id, now(), kind, agent, str(detail)[:500]))


def request_approval(run_id: str, artifact_id: str, action: str, target: str,
                     approver_id: str) -> dict:
    """Bind an approval to one artifact VERSION, one action and one approver."""
    with db() as conn:
        art = conn.execute("SELECT * FROM run_artifacts WHERE id = ?",
                           (artifact_id,)).fetchone()
        if not art:
            return {"error": "No such artifact"}
        pid = new_id()
        conn.execute(
            """INSERT INTO run_approvals (id, run_id, artifact_id, artifact_version,
                                          action, target, approver_id, status, created_at)
               VALUES (?,?,?,?,?,?,?,'pending',?)""",
            (pid, run_id, artifact_id, art["version"], action[:200], target[:300],
             approver_id, now()))
        return dict(conn.execute("SELECT * FROM run_approvals WHERE id = ?",
                                 (pid,)).fetchone())


def decide_approval(approval_id: str, approver_id: str, approve: bool) -> dict:
    """Record a decision. Refuses if the artifact changed since it was requested,
    if someone else is trying to decide, or if it was already decided."""
    with db() as conn:
        a = conn.execute("SELECT * FROM run_approvals WHERE id = ?",
                         (approval_id,)).fetchone()
        if not a:
            return {"error": "No such approval"}
        if a["approver_id"] != approver_id:
            return {"error": "Only the person this was sent to can decide it"}
        if a["status"] == "superseded":
            # Distinct from "already decided": nobody decided anything, the file
            # moved on underneath it. Saying "already decided" here would be a lie.
            return {"error": "That file has changed since it was sent for approval. "
                             "Review the new version instead."}
        if a["status"] != "pending":
            # A second click is not a second action.
            return {"already": True, **dict(a)}
        art = conn.execute("SELECT version FROM run_artifacts WHERE id = ?",
                           (a["artifact_id"],)).fetchone()
        if not art or int(art["version"]) != int(a["artifact_version"]):
            conn.execute("UPDATE run_approvals SET status = 'superseded' WHERE id = ?",
                         (approval_id,))
            return {"error": "That file has changed since it was sent for approval. "
                             "Review the new version instead."}
        conn.execute(
            "UPDATE run_approvals SET status = ?, decided_at = ? WHERE id = ?",
            ("approved" if approve else "rejected", now(), approval_id))
        return dict(conn.execute("SELECT * FROM run_approvals WHERE id = ?",
                                 (approval_id,)).fetchone())


def claim_approval_execution(approval_id: str) -> bool:
    """Atomically mark an approved action as being carried out.

    Returns False if it was already claimed. This is what stops a double click,
    a retry, or two browser tabs from publishing twice.
    """
    with db() as conn:
        n = conn.execute(
            """UPDATE run_approvals SET executed_at = ?
               WHERE id = ? AND status = 'approved' AND executed_at = 0""",
            (now(), approval_id)).rowcount
    return n > 0


def record_approval_result(approval_id: str, result: str):
    with db() as conn:
        conn.execute("UPDATE run_approvals SET execution_result = ? WHERE id = ?",
                     (str(result)[:1000], approval_id))


def runs_for_conversation(conversation_id: str, limit: int = 20) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM agent_runs WHERE conversation_id = ?
               ORDER BY created_at DESC LIMIT ?""", (conversation_id, limit))]


def recover_interrupted_runs() -> int:
    """Anything left mid-flight by a restart is not silently 'running' forever."""
    with db() as conn:
        n = conn.execute(
            """UPDATE agent_runs SET status = 'failed',
                   blocker = 'The server restarted while this was running.',
                   updated_at = ?
               WHERE status IN ('running','queued')""", (now(),)).rowcount
        conn.execute(
            """UPDATE run_tasks SET status = 'failed',
                   error = 'The server restarted while this was running.', updated_at = ?
               WHERE status IN ('running','queued')""", (now(),))
    return n


def init_audit():
    with db() as conn:
        conn.executescript(AUDIT_SCHEMA)


def audit(actor_id: str, action: str, target: str = "", detail: str = ""):
    """Record something that would be hard to reconstruct afterwards.

    Deliberately best effort: an audit write must never be the reason a delete or
    a login fails. Prune old rows so this can't grow without bound.
    """
    try:
        with db() as conn:
            name = ""
            if actor_id:
                row = conn.execute("SELECT name FROM users WHERE id = ?", (actor_id,)).fetchone()
                name = row["name"] if row else ""
            conn.execute(
                "INSERT INTO audit_log (id, at, actor_id, actor_name, action, target, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (new_id(), now(), actor_id or "", name, action,
                 str(target)[:200], str(detail)[:500]))
            if int(now()) % 50 == 0:          # occasionally, not on every write
                conn.execute("DELETE FROM audit_log WHERE at < ?",
                             (now() - AUDIT_KEEP_DAYS * 86400,))
    except Exception as e:
        print(f"[audit] could not record {action}: {type(e).__name__}", flush=True)


def delete_user_completely(user_id: str) -> dict:
    """Remove a person and everything that belongs only to them.

    Deliberately explicit about what goes, because this is not reversible:
      - their agents, and the bot accounts those agents speak through
      - their one-to-one conversations (which exist only for the two of them)
      - their membership of groups and channels; those survive
      - their messages in surviving groups are KEPT but detached, so other
        people's conversations don't develop holes where replies lost their
        question
      - their notes, schedules and access grants
    """
    counts = {"agents": 0, "dms": 0, "messages_detached": 0, "memberships": 0}
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"error": "No such person"}
        name = row["name"]

        agents = conn.execute("SELECT id, user_id FROM agents WHERE owner_id = ?",
                              (user_id,)).fetchall()
        for a in agents:
            conn.execute("DELETE FROM agent_access WHERE agent_id = ?", (a["id"],))
            conn.execute("DELETE FROM agent_notes WHERE agent_id = ?", (a["id"],))
            try:
                conn.execute("DELETE FROM agent_schedule_runs WHERE schedule_id IN "
                             "(SELECT id FROM agent_schedules WHERE agent_id = ?)", (a["id"],))
                conn.execute("DELETE FROM agent_schedules WHERE agent_id = ?", (a["id"],))
            except sqlite3.OperationalError:
                pass          # scheduler tables not created in this process
            conn.execute("DELETE FROM agents WHERE id = ?", (a["id"],))
            conn.execute("DELETE FROM users WHERE id = ? AND is_bot = 1", (a["user_id"],))
            counts["agents"] += 1

        dms = conn.execute(
            """SELECT c.id FROM conversations c
               JOIN members m ON m.conversation_id = c.id AND m.user_id = ?
               WHERE c.type = 'dm'""", (user_id,)).fetchall()
        for d in dms:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (d["id"],))
            conn.execute("DELETE FROM members WHERE conversation_id = ?", (d["id"],))
            conn.execute("DELETE FROM conversations WHERE id = ?", (d["id"],))
            counts["dms"] += 1

        counts["memberships"] = conn.execute(
            "DELETE FROM members WHERE user_id = ?", (user_id,)).rowcount
        left = conn.execute("SELECT COUNT(*) c FROM messages WHERE sender_id = ?",
                            (user_id,)).fetchone()["c"]
        counts["messages_detached"] = left

        conn.execute("DELETE FROM agent_access WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM agent_requests WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM reactions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"ok": True, "name": name, **counts}


def _count_or_zero(conn, sql: str, *args) -> int:
    """A count from a table another module owns, which may not exist yet."""
    try:
        return conn.execute(sql, args).fetchone()["c"]
    except sqlite3.OperationalError:
        return 0


def user_activity(user_id: str, limit: int = 100) -> dict:
    """A summary of what one person has been doing, for the admin page."""
    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not u:
            return {"error": "No such person"}
        stats = {
            "messages": conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE sender_id = ? AND deleted = 0",
                (user_id,)).fetchone()["c"],
            "conversations": conn.execute(
                "SELECT COUNT(*) c FROM members WHERE user_id = ?", (user_id,)).fetchone()["c"],
            "agents_owned": conn.execute(
                "SELECT COUNT(*) c FROM agents WHERE owner_id = ?", (user_id,)).fetchone()["c"],
            "agents_shared_with_them": conn.execute(
                "SELECT COUNT(*) c FROM agent_access WHERE user_id = ?",
                (user_id,)).fetchone()["c"],
            # scheduler.py owns this table and may not have run yet.
            "schedules": _count_or_zero(
                conn, "SELECT COUNT(*) c FROM agent_schedules WHERE owner_id = ?", user_id),
            "last_message_at": conn.execute(
                "SELECT MAX(created_at) t FROM messages WHERE sender_id = ?",
                (user_id,)).fetchone()["t"] or 0,
        }
        events = [dict(r) for r in conn.execute(
            "SELECT at, action, target, detail FROM audit_log WHERE actor_id = ? "
            "ORDER BY at DESC LIMIT ?", (user_id, limit)).fetchall()]
    return {"user": {"id": u["id"], "name": u["name"], "email": u["email"],
                     "last_seen_at": u["last_seen_at"] or 0},
            "stats": stats, "events": events}


def audit_recent(limit: int = 200, actor_id: str = "") -> list[dict]:
    with db() as conn:
        if actor_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE actor_id = ? ORDER BY at DESC LIMIT ?",
                (actor_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY at DESC LIMIT ?",
                                (limit,)).fetchall()
        return [dict(r) for r in rows]


def backup_to(dest) -> dict:
    """A consistent snapshot, safe to take while the server is running.

    VACUUM INTO is the right tool: it reads through the same connection, so it
    can't catch a half-written transaction, and the result is a compacted copy
    rather than a raw file that may be missing its WAL.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with db() as conn:
        conn.execute("VACUUM INTO ?", (str(dest),))
    return {"path": str(dest), "bytes": dest.stat().st_size}


def prune_backups(folder, keep: int = 14):
    """Keep the newest `keep` snapshots; a backup folder that grows forever
    eventually fills the disk and takes the server with it."""
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    files = sorted(folder.glob("sportstech-*.db"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink(); removed += 1
        except OSError:
            pass
    return removed


def ensure_indexes():
    """Run AFTER every init_* function. Called from init_db it silently skipped the
    agent indexes, because those tables didn't exist yet — so the two scans this
    was meant to fix were still scans."""
    made, skipped = 0, []
    with db() as conn:
        for stmt in INDEXES:
            try:
                conn.execute(stmt)
                made += 1
            except sqlite3.OperationalError as e:
                skipped.append(str(e))
    if skipped:
        print(f"[db] {made} indexes ensured, {len(skipped)} skipped: {skipped}", flush=True)
    return made


INDEXES = [
    # members is keyed (conversation_id, user_id), so "which conversations is this
    # person in" had to scan the whole table.
    "CREATE INDEX IF NOT EXISTS idx_members_user ON members(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_agents_owner ON agents(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_access_user ON agent_access(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id)",
    # the details pane filters by kind within a conversation
    "CREATE INDEX IF NOT EXISTS idx_messages_kind ON messages(conversation_id, kind, created_at)",
]


@contextmanager
def db():
    # timeout: wait for a writer instead of raising "database is locked" the moment
    # two requests overlap, which is routine with a websocket server.
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers carry on while a write is in progress. Without it every
    # SELECT blocks behind every INSERT, which is what makes a busy chat feel slow.
    conn.execute("PRAGMA journal_mode = WAL")
    # With WAL, NORMAL is durable across an application crash and only risks the
    # last transaction on a power cut — the right trade for chat messages.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists



def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


# ---------- users ----------

def upsert_user(email: str, name: str, avatar: str = "", title: str = "", is_bot: bool = False) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET name = ?, avatar = CASE WHEN ? != '' THEN ? ELSE avatar END WHERE email = ?",
                (name, avatar, avatar, email),
            )
            return dict(conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone())
        uid = new_id()
        conn.execute(
            "INSERT INTO users (id, email, name, avatar, title, is_bot, created_at) VALUES (?,?,?,?,?,?,?)",
            (uid, email, name, avatar, title, int(is_bot), now()),
        )
        return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def get_user(user_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY name").fetchall()]


# ---------- conversations ----------

def create_conversation(ctype: str, name: str, created_by: str, member_ids: list[str]) -> dict:
    with db() as conn:
        cid = new_id()
        conn.execute(
            "INSERT INTO conversations (id, type, name, created_by, created_at) VALUES (?,?,?,?,?)",
            (cid, ctype, name, created_by, now()),
        )
        for uid in set(member_ids) | {created_by}:
            conn.execute(
                "INSERT OR IGNORE INTO members (conversation_id, user_id, joined_at) VALUES (?,?,?)",
                (cid, uid, now()),
            )
        return dict(conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone())


def find_dm(user_a: str, user_b: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            """SELECT c.* FROM conversations c
               WHERE c.type = 'dm'
                 AND EXISTS (SELECT 1 FROM members WHERE conversation_id = c.id AND user_id = ?)
                 AND EXISTS (SELECT 1 FROM members WHERE conversation_id = c.id AND user_id = ?)""",
            (user_a, user_b),
        ).fetchone()
        return dict(row) if row else None


def get_conversation(cid: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
        return dict(row) if row else None


def user_conversations(user_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT c.*,
                      (SELECT CASE WHEN m.deleted = 1 THEN '(deleted)'
                                   WHEN m.kind = 'image' THEN '📷 Photo'
                                   WHEN m.kind = 'file' THEN '📎 ' || m.file_name
                                   ELSE m.content END
                       FROM messages m WHERE m.conversation_id = c.id
                         AND m.created_at > COALESCE(mb.hidden_at, 0)
                       ORDER BY m.created_at DESC LIMIT 1) AS last_message,
                      (SELECT created_at FROM messages m WHERE m.conversation_id = c.id
                         AND m.created_at > COALESCE(mb.hidden_at, 0)
                       ORDER BY m.created_at DESC LIMIT 1) AS last_at,
                      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id
                         AND m.created_at > mb.last_read_at
                         AND m.created_at > COALESCE(mb.hidden_at, 0)
                         AND m.sender_id != ? AND m.deleted = 0) AS unread
               FROM conversations c
               JOIN members mb ON mb.conversation_id = c.id AND mb.user_id = ?
               WHERE COALESCE(mb.hidden_at, 0) = 0
                  OR EXISTS (SELECT 1 FROM messages m2
                             WHERE m2.conversation_id = c.id
                               AND m2.created_at > mb.hidden_at)
               ORDER BY COALESCE(last_at, c.created_at) DESC""",
            (user_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]


def conversation_members(cid: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT u.* FROM users u JOIN members m ON m.user_id = u.id
               WHERE m.conversation_id = ? ORDER BY u.name""",
            (cid,),
        ).fetchall()
        return [dict(r) for r in rows]


def is_member(cid: str, user_id: str) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM members WHERE conversation_id = ? AND user_id = ?", (cid, user_id)
        ).fetchone() is not None


def add_member(cid: str, user_id: str):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO members (conversation_id, user_id, joined_at) VALUES (?,?,?)",
            (cid, user_id, now()),
        )


def mark_read(cid: str, user_id: str):
    with db() as conn:
        conn.execute(
            "UPDATE members SET last_read_at = ? WHERE conversation_id = ? AND user_id = ?",
            (now(), cid, user_id),
        )


def list_channels() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM conversations WHERE type = 'channel' ORDER BY name").fetchall()
        return [dict(r) for r in rows]


# ---------- messages ----------

def add_message(cid: str, sender_id: str, content: str, kind: str = "text",
                file_name: str = "", file_size: int = 0) -> dict:
    with db() as conn:
        mid = new_id()
        ts = now()
        conn.execute(
            """INSERT INTO messages (id, conversation_id, sender_id, content, kind, file_name, file_size, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (mid, cid, sender_id, content, kind, file_name, file_size, ts),
        )
        return {"id": mid, "conversation_id": cid, "sender_id": sender_id, "content": content,
                "kind": kind, "file_name": file_name, "file_size": file_size,
                "edited": 0, "deleted": 0, "created_at": ts, "reactions": []}


def file_viewers(url_fragment: str) -> list[str]:
    """Who is allowed to see this uploaded file.

    A file is visible to the members of every conversation it was posted in, plus
    whoever it belongs to as an avatar or group image. Anything not referenced
    anywhere has no viewers, so a stale or guessed URL returns nothing.
    """
    like = f"%{url_fragment}"
    with db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT mb.user_id FROM messages m
               JOIN members mb ON mb.conversation_id = m.conversation_id
               WHERE m.content LIKE ? AND m.kind IN ('image','file')""",
            (like,)).fetchall()
        who = [r["user_id"] for r in rows]

        # An avatar is visible to anyone who shares a conversation with its owner;
        # simplest correct rule is "the owner plus their contacts".
        owners = conn.execute("SELECT id FROM users WHERE avatar LIKE ?", (like,)).fetchall()
        for o in owners:
            who.append(o["id"])
            shared = conn.execute(
                """SELECT DISTINCT mb2.user_id FROM members mb1
                   JOIN members mb2 ON mb2.conversation_id = mb1.conversation_id
                   WHERE mb1.user_id = ?""", (o["id"],)).fetchall()
            who.extend(r["user_id"] for r in shared)

        groups = conn.execute("SELECT id FROM conversations WHERE image LIKE ?",
                              (like,)).fetchall()
        for g in groups:
            members = conn.execute("SELECT user_id FROM members WHERE conversation_id = ?",
                                   (g["id"],)).fetchall()
            who.extend(r["user_id"] for r in members)
    return list(set(who))


def get_message(mid: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        return dict(row) if row else None


def edit_message(mid: str, sender_id: str, content: str) -> bool:
    with db() as conn:
        cur = conn.execute(
            "UPDATE messages SET content = ?, edited = 1 WHERE id = ? AND sender_id = ? AND deleted = 0 AND kind = 'text'",
            (content, mid, sender_id),
        )
        return cur.rowcount > 0


def delete_message(mid: str, sender_id: str) -> bool:
    with db() as conn:
        cur = conn.execute(
            "UPDATE messages SET deleted = 1, content = '' WHERE id = ? AND sender_id = ?",
            (mid, sender_id),
        )
        return cur.rowcount > 0


def toggle_reaction(mid: str, user_id: str, emoji: str):
    with db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
            (mid, user_id, emoji),
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
                         (mid, user_id, emoji))
        else:
            conn.execute("INSERT INTO reactions (message_id, user_id, emoji) VALUES (?,?,?)",
                         (mid, user_id, emoji))


def message_reactions(mid: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT emoji, GROUP_CONCAT(user_id) AS uids, COUNT(*) AS count FROM reactions "
            "WHERE message_id = ? GROUP BY emoji",
            (mid,),
        ).fetchall()
        return [{"emoji": r["emoji"], "count": r["count"],
                 "user_ids": (r["uids"] or "").split(",")} for r in rows]


def get_messages(cid: str, limit: int = 100, since: float = 0,
                 before: float = 0) -> list[dict]:
    """`since` is the caller's hidden_at watermark: messages older than the point
    at which they deleted the chat stay invisible to them, while remaining intact
    for everyone else."""
    with db() as conn:
        rows = conn.execute(
            """SELECT m.*, u.name AS sender_name, u.avatar AS sender_avatar, u.is_bot AS sender_is_bot
               FROM messages m JOIN users u ON u.id = m.sender_id
               WHERE m.conversation_id = ? AND m.created_at > ?
                 AND (? = 0 OR m.created_at < ?)
               ORDER BY m.created_at DESC LIMIT ?""",
            (cid, since or 0, before or 0, before or 0, limit),
        ).fetchall()
        msgs = [dict(r) for r in reversed(rows)]
        ids = [m["id"] for m in msgs]
        by_msg: dict[str, list] = {}
        if ids:
            ph = ",".join("?" * len(ids))
            rrows = conn.execute(
                f"SELECT message_id, emoji, GROUP_CONCAT(user_id) AS uids, COUNT(*) AS count "
                f"FROM reactions WHERE message_id IN ({ph}) GROUP BY message_id, emoji", ids
            ).fetchall()
            for r in rrows:
                by_msg.setdefault(r["message_id"], []).append(
                    {"emoji": r["emoji"], "count": r["count"], "user_ids": (r["uids"] or "").split(",")})
        for m in msgs:
            m["reactions"] = by_msg.get(m["id"], [])
        return msgs


def shared_media(cid: str, since: float = 0) -> dict:
    """Images and files shared in a conversation, for the details panel.
    `since` is the caller's hidden_at watermark — see get_messages()."""
    with db() as conn:
        imgs = conn.execute(
            "SELECT id, content, created_at FROM messages WHERE conversation_id = ? AND kind = 'image' "
            "AND deleted = 0 AND created_at > ? ORDER BY created_at DESC LIMIT 12",
            (cid, since or 0),
        ).fetchall()
        files = conn.execute(
            "SELECT id, content, file_name, file_size, created_at FROM messages WHERE conversation_id = ? "
            "AND kind = 'file' AND deleted = 0 AND created_at > ? ORDER BY created_at DESC LIMIT 12",
            (cid, since or 0),
        ).fetchall()
        return {"media": [dict(r) for r in imgs], "files": [dict(r) for r in files]}


# ============================== agents (v3) ==============================

AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    user_id TEXT NOT NULL,             -- the bot user this agent chats as
    name TEXT NOT NULL,
    slug TEXT NOT NULL,                -- @mention handle
    description TEXT DEFAULT '',
    api_key TEXT DEFAULT '',
    provider TEXT DEFAULT 'claude',    -- claude | ollama (local workspace server)
    ollama_url TEXT DEFAULT '',
    model TEXT DEFAULT 'claude-sonnet-4-6',
    type TEXT DEFAULT 'product',       -- product | create | pet
    instruction TEXT DEFAULT '',
    product_name TEXT DEFAULT '',      -- product this agent covers (product type only)
    product_url TEXT DEFAULT '',       -- its page, so the agent can look up live facts
    capabilities TEXT DEFAULT '{}',    -- JSON: {tool_calling, web_scraping, multi_agent, autonomous_reasoning}
    mcp_connectors TEXT DEFAULT '[]',  -- JSON array of connector names/urls
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_access (
    agent_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (agent_id, user_id)
);
"""


AGENT_MIGRATIONS = [
    "ALTER TABLE agents ADD COLUMN provider TEXT DEFAULT 'claude'",
    "ALTER TABLE agents ADD COLUMN ollama_url TEXT DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN type TEXT DEFAULT 'product'",
    "ALTER TABLE agents ADD COLUMN instruction TEXT DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN capabilities TEXT DEFAULT '{}'",
    "ALTER TABLE agents ADD COLUMN mcp_connectors TEXT DEFAULT '[]'",
    "ALTER TABLE agents ADD COLUMN product_name TEXT DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN product_url TEXT DEFAULT ''",
]


def init_agent_tables():
    with db() as conn:
        conn.executescript(AGENT_SCHEMA)
        for stmt in AGENT_MIGRATIONS:
            try:
                conn.execute(stmt)
            except Exception:
                pass


def slugify(name: str) -> str:
    return "".join(ch for ch in name.lower().replace(" ", "-") if ch.isalnum() or ch == "-").strip("-") or "agent"


def create_agent(owner_id: str, user_id: str, name: str, slug: str, description: str,
                 api_key: str, model: str, provider: str = "claude", ollama_url: str = "",
                 type: str = "product", instruction: str = "",
                 capabilities: str = "{}", mcp_connectors: str = "[]",
                 product_name: str = "", product_url: str = "") -> dict:
    with db() as conn:
        aid = new_id()
        conn.execute(
            """INSERT INTO agents (id, owner_id, user_id, name, slug, description, api_key,
                                   provider, ollama_url, model, type, instruction,
                                   product_name, product_url,
                                   capabilities, mcp_connectors, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, owner_id, user_id, name, slug, description, api_key, provider, ollama_url,
             model, type, instruction, product_name, product_url,
             capabilities, mcp_connectors, now()),
        )
        return dict(conn.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone())


def get_agent(aid: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (aid,)).fetchone()
        return dict(row) if row else None


def get_agent_by_user(user_id: str) -> dict | None:
    """Find the agent record for a bot user id."""
    with db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def slug_taken(slug: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM agents WHERE slug = ?", (slug,)).fetchone() is not None


def update_agent(aid: str, owner_id: str, name: str, description: str, api_key: str | None,
                 model: str, provider: str, ollama_url: str,
                 type: str = "product", instruction: str = "",
                 capabilities: str = "{}", mcp_connectors: str = "[]",
                 product_name: str = "", product_url: str = "") -> bool:
    with db() as conn:
        extra = ("type = ?, instruction = ?, capabilities = ?, mcp_connectors = ?, "
                 "product_name = ?, product_url = ?")
        extra_vals = (type, instruction, capabilities, mcp_connectors,
                      product_name, product_url)
        if api_key is not None:
            cur = conn.execute(
                "UPDATE agents SET name = ?, description = ?, api_key = ?, provider = ?, ollama_url = ?, model = ?, "
                + extra + " WHERE id = ? AND owner_id = ?",
                (name, description, api_key, provider, ollama_url, model, *extra_vals, aid, owner_id))
        else:
            cur = conn.execute(
                "UPDATE agents SET name = ?, description = ?, provider = ?, ollama_url = ?, model = ?, "
                + extra + " WHERE id = ? AND owner_id = ?",
                (name, description, provider, ollama_url, model, *extra_vals, aid, owner_id))
        if cur.rowcount:
            agent = conn.execute("SELECT user_id FROM agents WHERE id = ?", (aid,)).fetchone()
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, agent["user_id"]))
        return cur.rowcount > 0


def delete_agent(aid: str, owner_id: str) -> bool:
    with db() as conn:
        agent = conn.execute("SELECT user_id FROM agents WHERE id = ? AND owner_id = ?", (aid, owner_id)).fetchone()
        if not agent:
            return False
        conn.execute("DELETE FROM agents WHERE id = ?", (aid,))
        conn.execute("DELETE FROM agent_access WHERE agent_id = ?", (aid,))
        return True


def my_agents(user_id: str) -> list[dict]:
    """Agents I own + agents shared with me."""
    with db() as conn:
        rows = conn.execute(
            """SELECT a.*, u.name AS owner_name,
                      CASE WHEN a.owner_id = ? THEN 1 ELSE 0 END AS is_owner
               FROM agents a JOIN users u ON u.id = a.owner_id
               WHERE a.owner_id = ? OR EXISTS
                     (SELECT 1 FROM agent_access x WHERE x.agent_id = a.id AND x.user_id = ?)
               ORDER BY a.created_at""",
            (user_id, user_id, user_id),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["shared_with"] = agent_shares(d["id"]) if d["is_owner"] else []
            out.append(d)
        return out


def agent_shares(aid: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT u.id, u.name, u.email FROM agent_access x
               JOIN users u ON u.id = x.user_id WHERE x.agent_id = ?""",
            (aid,),
        ).fetchall()
        return [dict(r) for r in rows]


def share_agent(aid: str, user_id: str):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO agent_access (agent_id, user_id) VALUES (?,?)", (aid, user_id))


def unshare_agent(aid: str, user_id: str):
    with db() as conn:
        conn.execute("DELETE FROM agent_access WHERE agent_id = ? AND user_id = ?", (aid, user_id))


def has_agent_access(aid: str, user_id: str) -> bool:
    with db() as conn:
        row = conn.execute(
            """SELECT 1 FROM agents a WHERE a.id = ? AND (a.owner_id = ? OR EXISTS
               (SELECT 1 FROM agent_access x WHERE x.agent_id = a.id AND x.user_id = ?))""",
            (aid, user_id, user_id),
        ).fetchone()
        return row is not None


def get_user_by_email(email: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def update_profile(user_id: str, name: str, title: str) -> dict | None:
    with db() as conn:
        conn.execute("UPDATE users SET name = ?, title = ? WHERE id = ?", (name, title, user_id))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def update_avatar(user_id: str, url: str):
    with db() as conn:
        conn.execute("UPDATE users SET avatar = ? WHERE id = ?", (url, user_id))


def conversation_agents(cid: str) -> list[dict]:
    """Agents whose bot users are members of the conversation."""
    with db() as conn:
        rows = conn.execute(
            """SELECT a.* FROM agents a
               JOIN members m ON m.user_id = a.user_id
               WHERE m.conversation_id = ?""",
            (cid,),
        ).fetchall()
        return [dict(r) for r in rows]


# ============================== groups v4: settings, roles, admin ==============================

GROUP_MIGRATIONS = [
    "ALTER TABLE conversations ADD COLUMN description TEXT DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN image TEXT DEFAULT ''",
    "ALTER TABLE members ADD COLUMN role TEXT DEFAULT 'member'",
]


def init_group_tables():
    with db() as conn:
        for stmt in GROUP_MIGRATIONS:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        # backfill: creators of existing groups/channels become admins
        conn.execute(
            """UPDATE members SET role = 'admin'
               WHERE role = 'member' AND EXISTS (
                 SELECT 1 FROM conversations c
                 WHERE c.id = members.conversation_id AND c.created_by = members.user_id)"""
        )


def set_admin_role(cid: str, user_id: str, role: str):
    with db() as conn:
        conn.execute("UPDATE members SET role = ? WHERE conversation_id = ? AND user_id = ?",
                     (role, cid, user_id))


def member_role(cid: str, user_id: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT role FROM members WHERE conversation_id = ? AND user_id = ?",
                           (cid, user_id)).fetchone()
        return row["role"] if row else None


def is_admin(cid: str, user_id: str) -> bool:
    return member_role(cid, user_id) == "admin"


def admin_count(cid: str) -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM members WHERE conversation_id = ? AND role = 'admin'",
            (cid,)).fetchone()["n"]


def update_conversation_meta(cid: str, name: str, description: str):
    with db() as conn:
        conn.execute("UPDATE conversations SET name = ?, description = ? WHERE id = ?",
                     (name, description, cid))


def set_conversation_image(cid: str, url: str):
    with db() as conn:
        conn.execute("UPDATE conversations SET image = ? WHERE id = ?", (url, cid))


def remove_member(cid: str, user_id: str):
    with db() as conn:
        conn.execute("DELETE FROM members WHERE conversation_id = ? AND user_id = ?", (cid, user_id))


def oldest_human_member(cid: str) -> str | None:
    """For auto-promoting when the last admin leaves."""
    with db() as conn:
        row = conn.execute(
            """SELECT m.user_id FROM members m JOIN users u ON u.id = m.user_id
               WHERE m.conversation_id = ? AND u.is_bot = 0
               ORDER BY m.joined_at LIMIT 1""",
            (cid,)).fetchone()
        return row["user_id"] if row else None


def human_member_count(cid: str) -> int:
    with db() as conn:
        return conn.execute(
            """SELECT COUNT(*) AS n FROM members m JOIN users u ON u.id = m.user_id
               WHERE m.conversation_id = ? AND u.is_bot = 0""",
            (cid,)).fetchone()["n"]


def hide_conversation(cid: str, user_id: str) -> bool:
    """Remove a conversation from ONE member's view. The conversation, its
    messages and every other member's copy are untouched. If someone posts again
    later the chat reappears for them, showing only the newer messages."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE members SET hidden_at = ? WHERE conversation_id = ? AND user_id = ?",
            (now(), cid, user_id))
        return cur.rowcount > 0


def hidden_at(cid: str, user_id: str) -> float:
    with db() as conn:
        row = conn.execute(
            "SELECT hidden_at FROM members WHERE conversation_id = ? AND user_id = ?",
            (cid, user_id)).fetchone()
        return float(row["hidden_at"] or 0) if row else 0.0


def delete_conversation(cid: str):
    with db() as conn:
        conn.execute("DELETE FROM reactions WHERE message_id IN (SELECT id FROM messages WHERE conversation_id = ?)", (cid,))
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
        conn.execute("DELETE FROM members WHERE conversation_id = ?", (cid,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))


def conversation_members_with_roles(cid: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT u.*, m.role, m.joined_at, m.last_read_at FROM users u JOIN members m ON m.user_id = u.id
               WHERE m.conversation_id = ? ORDER BY (m.role = 'admin') DESC, u.is_bot, u.name""",
            (cid,)).fetchall()
        return [dict(r) for r in rows]


def agents_by_user_ids(user_ids: list[str]) -> dict[str, dict]:
    """Map bot user_id -> agent record, for enriching member lists with slugs/owners."""
    if not user_ids:
        return {}
    with db() as conn:
        ph = ",".join("?" * len(user_ids))
        rows = conn.execute(f"SELECT * FROM agents WHERE user_id IN ({ph})", user_ids).fetchall()
        return {r["user_id"]: dict(r) for r in rows}


# ============================== v5: receipts, replies, requests, search, sessions ==============================

V5_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN last_seen_at REAL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN reply_to TEXT DEFAULT ''",
]

V5_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_requests (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT DEFAULT 'pending',   -- pending | approved | rejected
    created_at REAL NOT NULL
);
"""


def init_v5():
    with db() as conn:
        conn.executescript(V5_SCHEMA)
        for stmt in V5_MIGRATIONS:
            try:
                conn.execute(stmt)
            except Exception:
                pass


def set_last_seen(user_id: str):
    with db() as conn:
        conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now(), user_id))


def bump_token_version(user_id: str) -> int:
    with db() as conn:
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (user_id,))
        return conn.execute("SELECT token_version FROM users WHERE id = ?", (user_id,)).fetchone()[0]


# ---- agent access requests ----

def create_agent_request(agent_id: str, user_id: str) -> dict | None:
    """Create a pending request unless one already exists."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM agent_requests WHERE agent_id = ? AND user_id = ? AND status = 'pending'",
            (agent_id, user_id)).fetchone()
        if row:
            return None
        rid = new_id()
        conn.execute("INSERT INTO agent_requests (id, agent_id, user_id, created_at) VALUES (?,?,?,?)",
                     (rid, agent_id, user_id, now()))
        return {"id": rid, "agent_id": agent_id, "user_id": user_id, "status": "pending"}


def pending_requests_for_owner(owner_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT r.*, a.name AS agent_name, u.name AS requester_name, u.email AS requester_email
               FROM agent_requests r
               JOIN agents a ON a.id = r.agent_id AND a.owner_id = ?
               JOIN users u ON u.id = r.user_id
               WHERE r.status = 'pending' ORDER BY r.created_at""",
            (owner_id,)).fetchall()
        return [dict(r) for r in rows]


def get_request(rid: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM agent_requests WHERE id = ?", (rid,)).fetchone()
        return dict(row) if row else None


def resolve_request(rid: str, status: str):
    with db() as conn:
        conn.execute("UPDATE agent_requests SET status = ? WHERE id = ?", (status, rid))


# ---- replies ----

def add_message_v5(cid: str, sender_id: str, content: str, kind: str = "text",
                   file_name: str = "", file_size: int = 0, reply_to: str = "") -> dict:
    msg = add_message(cid, sender_id, content, kind, file_name, file_size)
    if reply_to:
        with db() as conn:
            ref = conn.execute(
                "SELECT 1 FROM messages WHERE id = ? AND conversation_id = ?", (reply_to, cid)).fetchone()
            if ref:
                conn.execute("UPDATE messages SET reply_to = ? WHERE id = ?", (reply_to, msg["id"]))
                msg["reply_to"] = reply_to
    return msg


def reply_previews(msgs: list[dict]) -> list[dict]:
    """Attach reply_preview/reply_sender for messages that reply to another."""
    ids = [m["reply_to"] for m in msgs if m.get("reply_to")]
    if not ids:
        return msgs
    with db() as conn:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT m.id, m.content, m.kind, m.deleted, m.file_name, u.name AS sender_name
                FROM messages m JOIN users u ON u.id = m.sender_id WHERE m.id IN ({ph})""", ids).fetchall()
        ref = {r["id"]: dict(r) for r in rows}
    for m in msgs:
        r = ref.get(m.get("reply_to") or "")
        if r:
            prev = "(deleted)" if r["deleted"] else ("📷 Photo" if r["kind"] == "image"
                    else "📎 " + r["file_name"] if r["kind"] == "file" else r["content"][:90])
            m["reply_preview"] = prev
            m["reply_sender"] = r["sender_name"]
    return msgs


# ---- search ----

# ---------------------------------------------------------------- full-text search
# LIKE '%term%' cannot use an index, so search scanned every message in the
# database on every keystroke. FTS5 keeps an inverted index kept in step by
# triggers, so search stays fast as history grows — and it ranks by relevance.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ins AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_del AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_upd AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

FTS_READY = False


def init_search():
    """Build the index, and backfill it once for messages that predate it."""
    global FTS_READY
    with db() as conn:
        try:
            conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError as e:
            print(f"[search] FTS5 unavailable, falling back to LIKE: {e}", flush=True)
            FTS_READY = False
            return False
        # COUNT(*) on an external-content FTS table reports the CONTENT table's
        # count, not the index's — so it can never be used to detect an empty
        # index. It always looked populated and the backfill never ran.
        conn.execute("""CREATE TABLE IF NOT EXISTS search_state (
                            k TEXT PRIMARY KEY, v TEXT)""")
        built = conn.execute("SELECT v FROM search_state WHERE k = 'fts_built'").fetchone()
        if not built:
            # 'rebuild' is the documented way to (re)populate an external-content
            # index from its content table. Idempotent, so safe to repeat.
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            conn.execute("INSERT OR REPLACE INTO search_state (k, v) VALUES ('fts_built', ?)",
                         (str(now()),))
            total = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
            print(f"[search] indexed {total} existing messages", flush=True)
    FTS_READY = True
    return True


def _fts_query(q: str) -> str:
    """Turn what someone typed into an FTS5 MATCH expression.

    Every term is quoted, because a bare apostrophe or a stray `*` in ordinary
    text is a syntax error in FTS5 and would make search fail rather than return
    nothing. A trailing prefix match makes it feel like search-as-you-type.
    """
    words = [w for w in re.split(r"[^\w]+", q or "") if w]
    if not words:
        return ""
    quoted = [f'"{w}"' for w in words[:-1]]
    quoted.append(f'"{words[-1]}"*')          # last word matches as a prefix
    return " AND ".join(quoted)


def search_messages(user_id: str, q: str, limit: int = 20) -> list[dict]:
    """Ranked full-text search across conversations this person is in."""
    if FTS_READY:
        match = _fts_query(q)
        if not match:
            return []
        try:
            with db() as conn:
                rows = conn.execute(
                    """SELECT m.id, m.conversation_id, m.content, m.created_at,
                              u.name AS sender_name, c.name AS conv_name,
                              c.type AS conv_type,
                              snippet(messages_fts, 0, '<<', '>>', '…', 12) AS excerpt
                       FROM messages_fts f
                       JOIN messages m ON m.rowid = f.rowid
                       JOIN members mb ON mb.conversation_id = m.conversation_id
                            AND mb.user_id = ?
                       JOIN users u ON u.id = m.sender_id
                       JOIN conversations c ON c.id = m.conversation_id
                       WHERE messages_fts MATCH ?
                         AND m.deleted = 0 AND m.kind = 'text'
                         AND m.created_at > COALESCE(mb.hidden_at, 0)
                       ORDER BY bm25(messages_fts), m.created_at DESC
                       LIMIT ?""",
                    (user_id, match, limit)).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            # A malformed match expression must not break search entirely.
            print(f"[search] fts query failed ({e}), using LIKE", flush=True)
    return _search_messages_like(user_id, q, limit)


def _search_messages_like(user_id: str, q: str, limit: int = 20) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.conversation_id, m.content, m.created_at,
                      u.name AS sender_name, c.name AS conv_name, c.type AS conv_type
               FROM messages m
               JOIN members mb ON mb.conversation_id = m.conversation_id AND mb.user_id = ?
               JOIN users u ON u.id = m.sender_id
               JOIN conversations c ON c.id = m.conversation_id
               WHERE m.deleted = 0 AND m.kind = 'text' AND m.content LIKE ?
               ORDER BY m.created_at DESC LIMIT ?""",
            (user_id, f"%{q}%", limit)).fetchall()
        return [dict(r) for r in rows]


def search_users(q: str, limit: int = 10) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, email, avatar, is_bot FROM users WHERE name LIKE ? OR email LIKE ? LIMIT ?",
            (f"%{q}%", f"%{q}%", limit)).fetchall()
        return [dict(r) for r in rows]