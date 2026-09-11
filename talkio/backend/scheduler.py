"""Agent scheduler for Sportstech — runs an agent against a fixed prompt on a daily
timetable and posts the result into a chat.

Wiring (main.py):
    import scheduler
    app.include_router(scheduler.router)          # near the other routes

    @app.on_event("startup")
    async def _start_scheduler():
        scheduler.init_scheduler_tables()
        scheduler.set_deliver(deliver_scheduled)
        scheduler.start()

Design notes
------------
* One asyncio task ticks every TICK_SECONDS and claims due rows, rather than one
  task per schedule. Edits and deletes need no task juggling, and a run that
  overruns its own slot can't be started twice (the `running` claim flag).
* Delivery is injected via set_deliver() so this module never imports main.py —
  main imports scheduler, and a circular import would break both.
* All timezone/DST maths lives in schedule_time.py and is unit-tested there.
* Every run is recorded in agent_schedule_runs, including failures. A schedule
  that has been quietly erroring for a week should be visible, not silent.
"""
from __future__ import annotations

import asyncio
import traceback

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import agents as agx
import auth
import database as dbx
import schedule_time as st

TICK_SECONDS = 30          # how often we look for due schedules
RUN_TIMEOUT = 300          # kill a single agent run after 5 minutes
MAX_PROMPT = 4000
PREVIEW_CHARS = 300
KEEP_RUNS = 50             # per-schedule run history retained

TARGETS = ("conversation", "owner_dm")


# ============================== storage ==============================

SCHEDULER_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_schedules (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    prompt TEXT NOT NULL,
    kind TEXT DEFAULT 'daily',           -- daily | weekdays | weekly | hourly | interval
    hour INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    tz TEXT DEFAULT 'UTC',               -- IANA name, e.g. Asia/Kolkata
    target TEXT DEFAULT 'conversation',  -- conversation | owner_dm
    conversation_id TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    next_run_at REAL DEFAULT 0,          -- UTC epoch; 0 = not yet armed
    last_run_at REAL DEFAULT 0,
    last_status TEXT DEFAULT '',         -- ok | error
    last_error TEXT DEFAULT '',
    running INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_schedule_runs (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL DEFAULT 0,
    status TEXT DEFAULT 'running',       -- running | ok | error
    missed INTEGER DEFAULT 0,            -- fired late because the server was down
    message_id TEXT DEFAULT '',
    preview TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON agent_schedules(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_sched_runs ON agent_schedule_runs(schedule_id, started_at);
"""

SCHEDULER_MIGRATIONS: list[str] = [
    # Weekly needs the chosen days; interval needs its length. Additive, so an
    # existing schedules table keeps working and every row stays 'daily'.
    "ALTER TABLE agent_schedules ADD COLUMN days TEXT DEFAULT ''",
    "ALTER TABLE agent_schedules ADD COLUMN every_minutes INTEGER DEFAULT 60",
]


def init_scheduler_tables():
    with dbx.db() as conn:
        conn.executescript(SCHEDULER_SCHEMA)
        for stmt in SCHEDULER_MIGRATIONS:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        # A crash mid-run leaves running=1 forever, which would silently disable
        # the schedule. Clear stale claims on every boot.
        conn.execute("UPDATE agent_schedules SET running = 0 WHERE running = 1")


def create_schedule(agent_id: str, owner_id: str, title: str, prompt: str,
                    hour: int, minute: int, tz: str, target: str,
                    conversation_id: str = "", kind: str = "daily",
                    days: str = "", every_minutes: int = 60) -> dict:
    kind = (kind or "daily").lower()
    if kind not in st.KINDS:
        kind = "daily"
    days = ",".join(str(d) for d in st.parse_days(days))
    every_minutes = max(1, min(int(every_minutes or 60), 10080))
    sid = dbx.new_id()
    nxt = st.next_run_for(kind, hour, minute, tz, days, every_minutes)
    with dbx.db() as conn:
        conn.execute(
            """INSERT INTO agent_schedules
               (id, agent_id, owner_id, title, prompt, kind, hour, minute, tz, target,
                conversation_id, enabled, next_run_at, created_at, days, every_minutes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
            (sid, agent_id, owner_id, title, prompt, kind, hour, minute, tz, target,
             conversation_id, nxt, dbx.now(), days, every_minutes))
    return get_schedule(sid)


def get_schedule(sid: str) -> dict | None:
    with dbx.db() as conn:
        row = conn.execute("SELECT * FROM agent_schedules WHERE id = ?", (sid,)).fetchone()
        return dict(row) if row else None


def schedules_for_agent(agent_id: str) -> list[dict]:
    with dbx.db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_schedules WHERE agent_id = ? ORDER BY hour, minute",
            (agent_id,)).fetchall()
        return [dict(r) for r in rows]


def schedules_for_owner(owner_id: str) -> list[dict]:
    with dbx.db() as conn:
        rows = conn.execute(
            """SELECT s.*, a.name AS agent_name, a.slug AS agent_slug,
                      a.user_id AS agent_user_id
               FROM agent_schedules s JOIN agents a ON a.id = s.agent_id
               WHERE s.owner_id = ? ORDER BY s.hour, s.minute""",
            (owner_id,)).fetchall()
        return [dict(r) for r in rows]


def update_schedule(sid: str, **fields) -> dict | None:
    allowed = {"title", "prompt", "hour", "minute", "tz", "target",
               "conversation_id", "enabled", "next_run_at"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return get_schedule(sid)
    clause = ", ".join(f"{k} = ?" for k in sets)
    with dbx.db() as conn:
        conn.execute(f"UPDATE agent_schedules SET {clause} WHERE id = ?",
                     (*sets.values(), sid))
    return get_schedule(sid)


def delete_schedule(sid: str, owner_id: str) -> bool:
    with dbx.db() as conn:
        cur = conn.execute("DELETE FROM agent_schedules WHERE id = ? AND owner_id = ?",
                           (sid, owner_id))
        if cur.rowcount:
            conn.execute("DELETE FROM agent_schedule_runs WHERE schedule_id = ?", (sid,))
        return cur.rowcount > 0


def delete_schedules_for_agent(agent_id: str):
    """Call when an agent is deleted so its schedules don't linger as orphans."""
    with dbx.db() as conn:
        rows = conn.execute("SELECT id FROM agent_schedules WHERE agent_id = ?",
                            (agent_id,)).fetchall()
        for r in rows:
            conn.execute("DELETE FROM agent_schedule_runs WHERE schedule_id = ?", (r["id"],))
        conn.execute("DELETE FROM agent_schedules WHERE agent_id = ?", (agent_id,))


def candidate_schedules(now_ts: float) -> list[dict]:
    """Enabled, not already running, and either unarmed or due."""
    with dbx.db() as conn:
        rows = conn.execute(
            """SELECT * FROM agent_schedules
               WHERE enabled = 1 AND running = 0
                 AND (next_run_at = 0 OR next_run_at <= ?)
               ORDER BY next_run_at""",
            (now_ts,)).fetchall()
        return [dict(r) for r in rows]


def claim(sid: str) -> bool:
    """Atomically take ownership of a run. Returns False if another tick got there
    first — the WHERE running = 0 makes this a compare-and-set."""
    with dbx.db() as conn:
        cur = conn.execute(
            "UPDATE agent_schedules SET running = 1 WHERE id = ? AND running = 0", (sid,))
        return cur.rowcount > 0


def release(sid: str, next_run_at: float, status: str = "", error: str = ""):
    with dbx.db() as conn:
        conn.execute(
            """UPDATE agent_schedules
               SET running = 0, next_run_at = ?, last_run_at = ?,
                   last_status = ?, last_error = ?
               WHERE id = ?""",
            (next_run_at, dbx.now() if status else 0, status, error[:500], sid))


def arm_only(sid: str, next_run_at: float):
    """Set the next slot without recording a run (used for freshly created rows)."""
    with dbx.db() as conn:
        conn.execute("UPDATE agent_schedules SET running = 0, next_run_at = ? WHERE id = ?",
                     (next_run_at, sid))


def start_run(sid: str, missed: bool) -> str:
    rid = dbx.new_id()
    with dbx.db() as conn:
        conn.execute(
            """INSERT INTO agent_schedule_runs (id, schedule_id, started_at, status, missed)
               VALUES (?,?,?,'running',?)""",
            (rid, sid, dbx.now(), int(missed)))
    return rid


def finish_run(rid: str, status: str, message_id: str = "", preview: str = "", error: str = ""):
    with dbx.db() as conn:
        conn.execute(
            """UPDATE agent_schedule_runs
               SET finished_at = ?, status = ?, message_id = ?, preview = ?, error = ?
               WHERE id = ?""",
            (dbx.now(), status, message_id, preview[:PREVIEW_CHARS], error[:500], rid))


def runs_for_schedule(sid: str, limit: int = 20) -> list[dict]:
    with dbx.db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_schedule_runs WHERE schedule_id = ? ORDER BY started_at DESC LIMIT ?",
            (sid, limit)).fetchall()
        return [dict(r) for r in rows]


def trim_runs(sid: str, keep: int = KEEP_RUNS):
    with dbx.db() as conn:
        conn.execute(
            """DELETE FROM agent_schedule_runs WHERE schedule_id = ? AND id NOT IN
               (SELECT id FROM agent_schedule_runs WHERE schedule_id = ?
                ORDER BY started_at DESC LIMIT ?)""",
            (sid, sid, keep))


# ============================== delivery hook ==============================

# main.py injects this: async def deliver(conversation_id, agent, text) -> msg dict.
_deliver = None


def set_deliver(fn):
    global _deliver
    _deliver = fn


# ============================== running a schedule ==============================

SCHEDULED_SUFFIX = (
    "\n\nThis is an automated scheduled run — nobody is waiting to answer follow-up "
    "questions, so produce a complete, self-contained result. Do not ask clarifying "
    "questions. If information is missing, state the assumption you made and continue."
)


async def generate(agent: dict, prompt: str, cid: str = "") -> str:
    """Run the agent against a fixed prompt (no chat history)."""
    owner = dbx.get_user(agent["owner_id"])
    system = agx.agent_system_prompt(
        agent, owner["name"] if owner else "a user",
        has_web=bool(agx.agent_caps(agent).get("web_scraping")),
        has_media=bool(cid and agx.attachment_index(cid)))
    system += SCHEDULED_SUFFIX
    text = await agx.call_model(agent, system, [{"role": "user", "content": prompt}],
                                max_tokens=2000, cid=cid)
    return (text or "").strip()


def resolve_target(sched: dict, agent: dict) -> str:
    """Which conversation this run posts into. Creates the owner DM on demand."""
    if sched["target"] == "owner_dm":
        existing = dbx.find_dm(sched["owner_id"], agent["user_id"])
        if existing:
            return existing["id"]
        conv = dbx.create_conversation("dm", "", sched["owner_id"], [agent["user_id"]])
        return conv["id"]

    cid = sched["conversation_id"]
    if not cid or not dbx.get_conversation(cid):
        raise RuntimeError("target conversation no longer exists")
    # The owner must still be in the chat — otherwise a schedule could keep posting
    # into a group they were removed from.
    if not dbx.is_member(cid, sched["owner_id"]):
        raise RuntimeError("you are no longer a member of the target conversation")
    return cid


async def run_schedule(sched: dict, missed: bool = False) -> dict:
    """Execute one schedule end to end. Never raises — records the failure instead."""
    sid = sched["id"]
    rid = start_run(sid, missed)
    agent = dbx.get_agent(sched["agent_id"])
    if not agent:
        finish_run(rid, "error", error="agent was deleted")
        return {"status": "error", "error": "agent was deleted"}

    try:
        cid = resolve_target(sched, agent)
        text = await asyncio.wait_for(generate(agent, sched["prompt"], cid), timeout=RUN_TIMEOUT)
        if not text:
            raise RuntimeError("the agent returned an empty reply")
        if missed:
            when = st.format_local(sched["next_run_at"], sched["tz"])
            text = f"🕗 *(delayed scheduled run — was due {when})*\n\n{text}"
        if _deliver is None:
            raise RuntimeError("scheduler delivery not configured (set_deliver was never called)")
        msg = await _deliver(cid, agent, text)
        finish_run(rid, "ok", message_id=msg.get("id", ""), preview=text)
        trim_runs(sid)
        return {"status": "ok", "conversation_id": cid, "message_id": msg.get("id", "")}
    except asyncio.TimeoutError:
        finish_run(rid, "error", error=f"run exceeded {RUN_TIMEOUT}s and was cancelled")
        return {"status": "error", "error": "timed out"}
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        finish_run(rid, "error", error=detail)
        print(f"[scheduler] run failed for {sid}: {detail}", flush=True)
        return {"status": "error", "error": detail}


async def _execute(sched: dict, plan: dict):
    result = await run_schedule(sched, missed=plan["missed"])
    release(sched["id"], plan["next_run_at"],
            status=result["status"], error=result.get("error", ""))


async def tick(now_ts: float | None = None) -> int:
    """One pass over due schedules. Returns how many runs were launched."""
    now_ts = dbx.now() if now_ts is None else now_ts
    launched = 0
    for sched in candidate_schedules(now_ts):
        try:
            plan = st.plan_run(sched["hour"], sched["minute"], sched["tz"],
                               sched["next_run_at"] or None, now_ts,
                               kind=sched["kind"] or "daily",
                               days=sched["days"] if "days" in sched.keys() else "",
                               every_minutes=(sched["every_minutes"]
                                              if "every_minutes" in sched.keys() else 60) or 60)
        except Exception as e:
            print(f"[scheduler] bad schedule {sched['id']}: {e}", flush=True)
            continue
        if not plan["run"]:
            arm_only(sched["id"], plan["next_run_at"])   # e.g. freshly created row
            continue
        if not claim(sched["id"]):
            continue                                     # another tick beat us to it
        asyncio.create_task(_execute(sched, plan))
        launched += 1
    return launched


_task: asyncio.Task | None = None


async def _loop():
    print(f"[scheduler] started (tick every {TICK_SECONDS}s)", flush=True)
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A bad row must never kill the loop — every schedule would stop silently.
            traceback.print_exc()
        await asyncio.sleep(TICK_SECONDS)


def start():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
    return _task


async def stop():
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


# ============================== REST API ==============================

router = APIRouter()


def guard(fn):
    """An unhandled exception escapes past CORSMiddleware, so the browser sees a
    response with no Access-Control-Allow-Origin and reports the useless
    'Failed to fetch'. Converting it to an HTTPException keeps the response on the
    normal path, where CORS headers are added and the UI can show the real reason."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=500,
                                detail=f"Scheduler error: {type(e).__name__}: {e}") from e
    return wrapper


class ScheduleBody(BaseModel):
    title: str = ""
    prompt: str
    time: str = "09:00"                  # HH:MM in the schedule's own timezone
    tz: str = "UTC"                      # IANA name from the browser
    target: str = "conversation"         # conversation | owner_dm
    conversation_id: str = ""
    enabled: bool = True
    kind: str = "daily"                  # daily | weekdays | weekly | hourly | interval
    days: str = ""                       # weekly only: "0,4" or "Monday,Friday"
    every_minutes: int = 60              # interval only


def _owned_agent(aid: str, user_id: str) -> dict:
    agent = dbx.get_agent(aid)
    if not agent or agent["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def _owned_schedule(sid: str, user_id: str) -> dict:
    sched = get_schedule(sid)
    if not sched or sched["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return sched


def _validate(body: ScheduleBody, user_id: str) -> tuple[int, int, str]:
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="A prompt is required")
    if len(prompt) > MAX_PROMPT:
        raise HTTPException(status_code=400, detail=f"Prompt is too long (max {MAX_PROMPT} characters)")
    try:
        hour, minute = st.parse_hhmm(body.time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    if body.target not in TARGETS:
        raise HTTPException(status_code=400, detail="target must be 'conversation' or 'owner_dm'")
    if body.target == "conversation":
        if not body.conversation_id:
            raise HTTPException(status_code=400, detail="Pick a conversation to post into")
        if not dbx.is_member(body.conversation_id, user_id):
            raise HTTPException(status_code=403, detail="You're not a member of that conversation")
    return hour, minute, prompt


def _decorate(sched: dict) -> dict:
    s = dict(sched)
    s["human"] = st.describe(s["hour"], s["minute"], s["tz"],
                                s["kind"] or "daily",
                                s["days"] if "days" in s.keys() else "",
                                (s["every_minutes"] if "every_minutes" in s.keys() else 60) or 60)
    s["next_run_local"] = st.format_local(s["next_run_at"], s["tz"])
    s["last_run_local"] = st.format_local(s["last_run_at"], s["tz"])
    s["time"] = f"{s['hour']:02d}:{s['minute']:02d}"
    return s


@router.get("/api/vision/check")
@guard
async def vision_check(user_id: str = Depends(auth.current_user_id)):
    """Diagnostics: which of my agents can actually see images, and if not, why.
    Hit this in the browser when analyze_image misbehaves."""
    import agents as _agx
    out = []
    for agent in dbx.my_agents(user_id):
        base = (agent.get("ollama_url") or "").rstrip("/")
        if base and not base.startswith("http"):
            base = "http://" + base
        entry = {"agent": agent["name"], "model": agent.get("model"),
                 "server": base, "vision_models": [], "ok": False, "note": ""}
        if agent.get("provider") != "ollama" or not base:
            entry["note"] = "Not an Ollama agent — vision detection does not apply."
        else:
            try:
                models = await _agx.vision_models(base, refresh=True)
                entry["vision_models"] = models
                entry["ok"] = bool(models)
                entry["note"] = ("Ready — analyze_image will use " + models[0]) if models else (
                    "No image-capable model installed. Run: ollama pull llama3.2-vision")
            except Exception as e:
                entry["note"] = f"Couldn't reach the model server: {type(e).__name__}: {e}"
        out.append(entry)
    return {"configured_vision_model": _agx.VISION_MODEL or "(auto-detect)", "agents": out}


@router.get("/api/schedules")
@guard
async def my_schedules(user_id: str = Depends(auth.current_user_id)):
    return [_decorate(s) for s in schedules_for_owner(user_id)]


@router.get("/api/agents/{aid}/schedules")
@guard
async def agent_schedules(aid: str, user_id: str = Depends(auth.current_user_id)):
    _owned_agent(aid, user_id)
    return [_decorate(s) for s in schedules_for_agent(aid)]


@router.post("/api/agents/{aid}/schedules")
@guard
async def add_schedule(aid: str, body: ScheduleBody, user_id: str = Depends(auth.current_user_id)):
    _owned_agent(aid, user_id)
    hour, minute, prompt = _validate(body, user_id)
    sched = create_schedule(aid, user_id, (body.title or "").strip()[:120], prompt,
                            hour, minute, body.tz, body.target, body.conversation_id,
                            kind=body.kind, days=body.days,
                            every_minutes=body.every_minutes)
    if not body.enabled:
        sched = update_schedule(sched["id"], enabled=0)
    return _decorate(sched)


@router.put("/api/schedules/{sid}")
@guard
async def edit_schedule(sid: str, body: ScheduleBody, user_id: str = Depends(auth.current_user_id)):
    sched = _owned_schedule(sid, user_id)
    hour, minute, prompt = _validate(body, user_id)
    # Re-arm whenever the time or timezone moves, so an edit takes effect today
    # rather than after the old slot fires.
    kind = (body.kind or "daily").lower()
    if kind not in st.KINDS:
        kind = "daily"
    days = ",".join(str(d) for d in st.parse_days(body.days))
    every = max(1, min(int(body.every_minutes or 60), 10080))
    old_kind = sched["kind"] or "daily"
    old_days = sched["days"] if "days" in sched.keys() else ""
    old_every = (sched["every_minutes"] if "every_minutes" in sched.keys() else 60) or 60
    # Re-arm whenever anything about the timetable moves — not just the clock
    # time — or an edit from daily to hourly would wait for tomorrow's slot.
    changed = ((hour, minute, body.tz, kind, days, every)
               != (sched["hour"], sched["minute"], sched["tz"], old_kind, old_days, old_every))
    nxt = (st.next_run_for(kind, hour, minute, body.tz, days, every)
           if changed else sched["next_run_at"])
    updated = update_schedule(sid, title=(body.title or "").strip()[:120], prompt=prompt,
                              hour=hour, minute=minute, tz=body.tz, target=body.target,
                              conversation_id=body.conversation_id,
                              enabled=int(body.enabled), next_run_at=nxt,
                              kind=kind, days=days, every_minutes=every)
    return _decorate(updated)


class ToggleBody(BaseModel):
    enabled: bool


@router.post("/api/schedules/{sid}/toggle")
@guard
async def toggle_schedule(sid: str, body: ToggleBody, user_id: str = Depends(auth.current_user_id)):
    sched = _owned_schedule(sid, user_id)
    # Re-enabling arms from now, so a schedule paused for a month doesn't
    # immediately fire a stale catch-up run the moment it's switched back on.
    nxt = st.next_run_for(sched["kind"] or "daily", sched["hour"], sched["minute"],
                            sched["tz"], sched["days"] if "days" in sched.keys() else "",
                            (sched["every_minutes"] if "every_minutes" in sched.keys() else 60) or 60) if body.enabled \
        else sched["next_run_at"]
    return _decorate(update_schedule(sid, enabled=int(body.enabled), next_run_at=nxt))


@router.delete("/api/schedules/{sid}")
@guard
async def remove_schedule(sid: str, user_id: str = Depends(auth.current_user_id)):
    if not delete_schedule(sid, user_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"ok": True}


@router.post("/api/schedules/{sid}/run")
@guard
async def run_now(sid: str, user_id: str = Depends(auth.current_user_id)):
    """Fire a schedule immediately without touching its timetable — the 'test it'
    button. Doesn't shift next_run_at."""
    sched = _owned_schedule(sid, user_id)
    if not claim(sid):
        raise HTTPException(status_code=409, detail="This schedule is already running")
    result = {"status": "error", "error": "run did not complete"}
    try:
        result = await run_schedule(sched, missed=False)
    finally:
        # next_run_at is deliberately preserved — a manual test must not shift
        # the timetable.
        release(sid, sched["next_run_at"], status=result["status"], error=result.get("error", ""))
    if result["status"] != "ok":
        raise HTTPException(status_code=502, detail=result.get("error", "Run failed"))
    return result


@router.get("/api/schedules/{sid}/runs")
@guard
async def schedule_runs(sid: str, user_id: str = Depends(auth.current_user_id)):
    _owned_schedule(sid, user_id)
    return runs_for_schedule(sid)