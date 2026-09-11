"""Sportstech v2 — chat app backend (FastAPI + WebSockets).

Run locally / on your WiFi (LAN):
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
The startup banner prints the LAN URL other devices on the same WiFi can open.
"""
import asyncio
import json
import re
import mimetypes
import os
import time
import socket
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agents as agx
import auth
import bot
import database as dbx
import mcp
import passwords as pw
import scheduler

app = FastAPI(title="Sportstech")

# Allow the frontend to be hosted on a different PC/port than this backend.
# The app authenticates with a Bearer token (not cookies), so allow_credentials
# is left off and allow_origins="*" carries no session-hijack risk.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND = Path(__file__).parent.parent / "frontend"
UPLOADS = Path(__file__).parent / "uploads"
UPLOADS.mkdir(exist_ok=True)
MAX_UPLOAD = 25 * 1024 * 1024  # 25 MB


async def read_capped(file, limit: int, what: str = "File") -> bytes:
    """Read an upload in chunks, stopping the moment it exceeds the limit.

    `await file.read()` buffers the WHOLE body before the size check, so a 2 GB
    POST was held in memory before being rejected — a one-line denial of service.
    """
    chunks, total = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413,
                                detail=f"{what} too large (max {limit // (1024 * 1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------- rate limiting
# Deliberately in-process: one uvicorn worker, and a dependency-free limiter that
# works beats a better one that never gets installed.
_hits: dict[tuple, list] = {}


def rate_limit(bucket: str, key: str, limit: int, per_seconds: int):
    """Allow `limit` actions per `per_seconds`, or raise 429."""
    now = time.time()
    k = (bucket, key)
    recent = [t for t in _hits.get(k, []) if now - t < per_seconds]
    if len(recent) >= limit:
        wait = int(per_seconds - (now - recent[0])) + 1
        _hits[k] = recent
        raise HTTPException(status_code=429,
                            detail=f"Too many attempts. Try again in {wait}s.")
    recent.append(now)
    _hits[k] = recent
    # Keep the dict from growing without bound on a long-running server.
    if len(_hits) > 5000:
        for old in [kk for kk, v in _hits.items() if not v or now - v[-1] > 3600]:
            _hits.pop(old, None)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

dbx.init_db()
dbx.init_agent_tables()
dbx.init_group_tables()
dbx.init_v5()
scheduler.init_scheduler_tables()
agx.init_agent_notes()
# After every table exists, or the agent indexes are silently skipped.
pw.init_password_tables()
dbx.ensure_indexes()
dbx.init_search()
dbx.init_audit()
dbx.init_runs()
_recovered = dbx.recover_interrupted_runs()
if _recovered:
    print(f"[runs] {_recovered} run(s) were interrupted by a restart and marked failed",
          flush=True)

_admin = pw.ensure_builtin_admin()
if _admin.get("generated"):
    print("\n" + "=" * 56)
    print("  ADMINISTRATOR ACCOUNT CREATED")
    print(f"  Email    : {_admin['email']}")
    print(f"  Password : {_admin['generated']}")
    print("  Shown once. Set ADMIN_PASSWORD in .env to choose your own.")
    print("=" * 56 + "\n")
auth.version_checker = lambda uid, tv: (dbx.get_user(uid) or {}).get("token_version", 0) == tv
OLLAMA_PORT = int(os.environ.get("OLLAMA_DETECT_PORT", "11434"))
# Workspace-local model server (the invisible default). The URL stays server-side
# and is NEVER returned to the browser. Configure both in .env.
LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:11434")
LOCAL_LLM_DEFAULT_MODEL = os.environ.get("LOCAL_LLM_DEFAULT_MODEL", "llama3.2")
BOT = bot.ensure_bot_user()

# Scheduler REST routes (/api/schedules, /api/agents/{aid}/schedules).
app.include_router(scheduler.router)


@app.on_event("startup")
async def startup_banner():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        lan_ip = "unknown"
    print("\n" + "=" * 56)
    print("  Sportstech is running")
    print(f"  This computer : http://localhost:8000")
    print(f"  Same WiFi/LAN : http://{lan_ip}:8000")
    # The admin page is not linked from the chat app by design, so print it here
    # or nobody will know it exists.
    admin_ready = (FRONTEND / "admin.html").is_file()
    print(f"  Administration: http://localhost:8000/admin"
          + ("" if admin_ready else "   <- admin.html NOT FOUND in "
             + str(FRONTEND)))
    print("  (Windows: allow Python through the firewall when asked)")
    print("=" * 56 + "\n")

    # Say plainly when the server is in an unsafe state, rather than leaving it
    # to whoever remembers to check the config.
    if auth.DEV_MODE:
        print("  !! DEV_MODE is ON — anyone can sign in as anyone with no password.")
    if not os.environ.get("JWT_SECRET"):
        print("  !! JWT_SECRET is not set — everyone is signed out on each restart.")
    if auth.DEV_MODE or not os.environ.get("JWT_SECRET"):
        print("     Set these in .env before anyone else can reach this server.\n")

    # Agent scheduler: inject the delivery hook, then start the tick loop.
    # scheduler.py deliberately never imports main.py (that would be circular),
    # so main hands it the one function it needs.
    asyncio.create_task(nightly_backup())
    scheduler.set_deliver(deliver_scheduled)
    scheduler.start()
    # Lets an agent's message_user tool actually deliver a chat message.
    agx.set_dm_sender(send_agent_dm)


@app.on_event("shutdown")
async def stop_scheduler():
    await scheduler.stop()


# ---------------- local-file bridge to a user's desktop app ----------------
# The server has no access to anyone's disk, so a tool call is forwarded to that
# user's connected client, which reads the file and answers. Correlated by id,
# with a timeout so an agent can't hang on a closed laptop.
_local_fs_waiters: dict[str, asyncio.Future] = {}
LOCAL_FS_TIMEOUT = 30


# Reads are noise; these change something or leave the app.
AUDITED_LOCAL_OPS = {"create", "delete", "move", "mkdir", "open", "openapp", "openurl",
                     "mcp_auth", "mcp_token", "mcp_client"}


async def request_local_fs(user_id: str, op: str, args: dict) -> dict:
    if op in AUDITED_LOCAL_OPS:
        detail = args.get("path") or args.get("name") or args.get("url") or ""
        dbx.audit(user_id, f"local.{op}", "", str(detail)[:200])
    # Addressed to ONE device, never broadcast. A user with a browser tab open as
    # well would otherwise have the tab answer "no_desktop" and win the race.
    target = hub.local_tool_socket(user_id)
    if target is None:
        return {"error": "no_desktop"}
    rid = dbx.new_id()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _local_fs_waiters[rid] = fut
    try:
        sent = await hub.send_to(target, {
            "type": "local_fs_request", "request_id": rid, "op": op, "args": args,
            "device_id": hub.meta.get(target, {}).get("device_id", "")})
        if not sent:
            return {"error": "no_desktop"}
        return await asyncio.wait_for(fut, timeout=LOCAL_FS_TIMEOUT)
    except asyncio.TimeoutError:
        raise TimeoutError("client did not answer")
    finally:
        _local_fs_waiters.pop(rid, None)


agx.set_local_fs(request_local_fs)
agx.set_local_client_check(lambda uid: hub.local_tool_socket(uid) is not None)
# Connector calls travel the same bridge as the local file tools, so multi-user
# routing and device selection are already handled.
mcp.set_bridge(request_local_fs)
# Claude/Gemini calls run on the asker's machine, with their own key.
agx.set_client_llm(request_local_fs)


async def share_workspace_file(cid: str, agent: dict, path) -> dict:
    """Copy a workspace file into uploads and post it as an attachment, so the
    people in the chat can download it."""
    import shutil
    UPLOADS.mkdir(parents=True, exist_ok=True)
    safe = f"{dbx.new_id()[:8]}-{path.name}"
    dest = UPLOADS / safe
    shutil.copyfile(path, dest)
    size = dest.stat().st_size
    msg = dbx.add_message(cid, agent["user_id"], f"/uploads/{safe}",
                          kind="file", file_name=path.name, file_size=size)
    sender = dbx.get_user(agent["user_id"])
    await hub.broadcast_conversation(cid, {
        "type": "message", **msg,
        "sender_name": sender["name"], "sender_avatar": sender.get("avatar", ""),
        "sender_is_bot": 1,
    })
    return msg


def make_agent_schedule(agent: dict, cid: str, title: str, prompt: str,
                        hour: int, minute: int) -> dict:
    """Let an agent schedule its own daily task, posting into this conversation."""
    return scheduler.create_schedule(
        agent["id"], agent["owner_id"], title, prompt, hour, minute,
        os.environ.get("TZ") or "UTC", "conversation", cid)


async def post_generated_image(cid: str, agent: dict, b64: str, mime: str,
                               caption: str) -> dict:
    """Write a generated image into uploads and post it as a message."""
    import base64
    UPLOADS.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
    raw = base64.b64decode(b64)
    if len(raw) > MAX_UPLOAD:
        raise ValueError("generated image too large")
    fid = dbx.new_id()
    (UPLOADS / (fid + ext)).write_bytes(raw)
    url = f"/files/{fid}{ext}"
    msg = dbx.add_message(cid, agent["user_id"], url, kind="image",
                          file_name=(caption or "image")[:80] + ext, file_size=len(raw))
    sender = dbx.get_user(agent["user_id"])
    await hub.broadcast_conversation(cid, {
        "type": "message", **msg,
        "sender_name": sender["name"], "sender_avatar": sender.get("avatar", ""),
        "sender_is_bot": 1,
    })
    return msg


async def read_conversation_artifact(cid: str, message_id: str) -> dict:
    """Read an image posted in this conversation, as base64, for editing.

    Scoped to the conversation deliberately: an agent naming an id from somewhere
    else must not be able to read it. Ids are database ids, which stay valid when
    new messages arrive — unlike "the second image".
    """
    import base64
    msg = dbx.get_message(message_id)
    if not msg:
        return {"error": "no message with that id"}
    if msg.get("conversation_id") != cid:
        return {"error": "that image is not in this conversation"}
    if msg.get("kind") != "image":
        return {"error": f"that message is a {msg.get('kind')}, not an image"}
    name = Path(str(msg.get("content") or "")).name
    path = (UPLOADS / name).resolve()
    if not path.is_file() or UPLOADS.resolve() not in path.parents:
        return {"error": "the file is missing from the server"}
    if path.stat().st_size > 8 * 1024 * 1024:
        return {"error": "that image is too large to edit (max 8 MB)"}
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return {"data": base64.b64encode(path.read_bytes()).decode(), "mimeType": mime}


agx.set_read_artifact(read_conversation_artifact)
agx.set_post_image(post_generated_image)
agx.set_share_file(share_workspace_file)
agx.set_scheduler(make_agent_schedule)


def resolve_local_fs(rid: str, payload: dict):
    fut = _local_fs_waiters.get(rid)
    if fut and not fut.done():
        fut.set_result(payload or {})


async def send_agent_dm(agent_user_id: str, owner_id: str, to_user_id: str, text: str) -> dict:
    """Post a message from an agent into its OWNER's chat with that person.

    Deliberately posts into owner<->person rather than agent<->person: the owner
    asked for it, so it has to land somewhere the owner can actually see. The
    sender is still the agent's user, so the bubble is attributed to the agent and
    carries the AGENT badge — nobody is misled into thinking the owner typed it.

    The agent is NOT added as a member. A DM is looked up by "both are members",
    so a third member would make find_dm(owner, agent) return this conversation
    and corrupt the owner's private chat with their own agent. Posting without
    membership keeps the DM a clean two-person row.
    """
    conv = dbx.find_dm(owner_id, to_user_id)
    if not conv:
        conv = dbx.create_conversation("dm", "", owner_id, [to_user_id])
        await hub.notify_users([owner_id, to_user_id],
                               {"type": "conversation_new", "conversation_id": conv["id"]})
    sender = dbx.get_user(agent_user_id)
    msg = dbx.add_message(conv["id"], agent_user_id, text)
    await hub.broadcast_conversation(conv["id"], {
        "type": "message", **msg,
        "sender_name": sender["name"], "sender_avatar": sender.get("avatar", ""),
        "sender_is_bot": 1,
    })
    return msg


BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", str(Path(__file__).parent / "backups")))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))


async def nightly_backup():
    """One snapshot a day, kept for two weeks. Cheap insurance against the single
    file that holds every conversation, agent and schedule."""
    while True:
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            info = dbx.backup_to(BACKUP_DIR / f"sportstech-{stamp}.db")
            gone = dbx.prune_backups(BACKUP_DIR, BACKUP_KEEP)
            print(f"[backup] {info['path']} ({info['bytes'] // 1024} KB)"
                  + (f", pruned {gone}" if gone else ""), flush=True)
        except Exception as e:
            print(f"[backup] failed: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(24 * 3600)


async def deliver_scheduled(cid: str, agent: dict, text: str) -> dict:
    """Post a scheduled agent run into a conversation and push it to live clients.
    Mirrors reply_as() minus the typing indicator (nobody is waiting on a cron run)."""
    if not dbx.is_member(cid, agent["user_id"]):
        dbx.add_member(cid, agent["user_id"])
    msg = dbx.add_message(cid, agent["user_id"], text)
    await hub.broadcast_conversation(cid, {
        "type": "message", **msg,
        "sender_name": agent["name"], "sender_avatar": "", "sender_is_bot": 1,
    })
    return msg


# ============================== REST: auth ==============================

class GoogleLogin(BaseModel):
    id_token: str


class DevLogin(BaseModel):
    email: str
    name: str


@app.post("/api/auth/google")
async def login_google(body: GoogleLogin, request: Request):
    rate_limit("login", request.client.host if request.client else "?", 8, 60)
    info = await auth.verify_google_id_token(body.id_token)
    user = dbx.upsert_user(info["email"], info["name"], avatar=info["picture"])
    dbx.audit(user["id"], "auth.login", "google", info.get("email", ""))
    return {"token": auth.make_token(user["id"], user.get("token_version", 0)),
            "user": _public_user(user)}


@app.post("/api/auth/dev")
async def login_dev(body: DevLogin, request: Request):
    rate_limit("login", request.client.host if request.client else "?", 8, 60)
    if not auth.DEV_MODE:
        raise HTTPException(status_code=403, detail="Dev login disabled")
    if "@" not in body.email:
        raise HTTPException(status_code=400, detail="Invalid email")
    user = dbx.upsert_user(body.email.lower().strip(), body.name.strip() or "User")
    dbx.audit(user["id"], "auth.login", "dev", body.email.lower().strip())
    return {"token": auth.make_token(user["id"], user.get("token_version", 0)),
            "user": _public_user(user)}


@app.get("/api/config")
async def config():
    return {"google_client_id": auth.GOOGLE_CLIENT_ID, "dev_mode": auth.DEV_MODE,
            "bot_name": bot.BOT_NAME, "bot_mention": bot.MENTIONS[0],
            # No admin yet means nobody can sign in and nobody can be invited, so
            # the app offers to create the first account instead of a dead login.
            "needs_setup": pw.admin_count() == 0,
            # So the UI can list the rules instead of hardcoding a copy that
            # drifts out of step with the server.
            "password_rules": pw.password_rules()}


@app.get("/api/me")
async def me(user_id: str = Depends(auth.current_user_id)):
    user = dbx.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    out = _public_user(user)
    # Useful to the client and safe to expose.
    out["must_change_password"] = bool(user.get("must_change_password"))
    out["is_admin"] = bool(user.get("is_admin"))
    return out


# ============================== REST: users / conversations ==============================

@app.get("/api/users")
async def users(user_id: str = Depends(auth.current_user_id)):
    # list_users() is SELECT * — sanitise before it reaches a browser.
    return [_public_user(u) for u in dbx.list_users()]


class NewConversation(BaseModel):
    type: str            # dm | group | channel
    name: str = ""
    member_ids: list[str] = []


def _enrich_members(members: list[dict]) -> list[dict]:
    """Attach @mention handles + agent owner names to member lists.

    Also strips the sensitive columns: these rows come from SELECT u.*, so once
    users gained a password_hash every member list in every conversation would
    have carried everyone's hash to the browser.
    """
    members = [_public_user(m) for m in members]
    bots = [m["id"] for m in members if m["is_bot"]]
    agent_map = dbx.agents_by_user_ids(bots)
    for m in members:
        if m["id"] in agent_map:
            ag = agent_map[m["id"]]
            m["slug"] = ag["slug"]
            m["agent_id"] = ag["id"]
            m["agent_owner_id"] = ag["owner_id"]
            owner = dbx.get_user(ag["owner_id"])
            m["agent_owner_name"] = owner["name"] if owner else ""
            m["agent_description"] = ag.get("instruction") or ag.get("description") or ""
            m["agent_model"] = ag.get("model", "")
        elif m["is_bot"]:
            m["slug"] = "nova"
    return members


@app.get("/api/conversations")
async def conversations(user_id: str = Depends(auth.current_user_id)):
    convs = dbx.user_conversations(user_id)
    for c in convs:
        members = _enrich_members(dbx.conversation_members_with_roles(c["id"]))
        c["members"] = members
        me_row = next((m for m in members if m["id"] == user_id), None)
        c["my_role"] = me_row["role"] if me_row else "member"
        if c["type"] == "dm":
            other = next((m for m in members if m["id"] != user_id), None)
            if other:
                c["name"] = other["name"]
                c["peer"] = other
    return convs


@app.post("/api/conversations")
async def create_conversation(body: NewConversation, user_id: str = Depends(auth.current_user_id)):
    if body.type not in ("dm", "group", "channel"):
        raise HTTPException(status_code=400, detail="Invalid type")
    if body.type == "dm":
        if len(body.member_ids) != 1:
            raise HTTPException(status_code=400, detail="DM needs exactly one other member")
        existing = dbx.find_dm(user_id, body.member_ids[0])
        if existing:
            return existing
    if body.type in ("group", "channel") and not body.name.strip():
        raise HTTPException(status_code=400, detail="Name required")
    conv = dbx.create_conversation(body.type, body.name.strip(), user_id, body.member_ids)
    dbx.set_admin_role(conv["id"], user_id, "admin")
    await hub.notify_users(
        [m["id"] for m in dbx.conversation_members(conv["id"])],
        {"type": "conversation_new", "conversation_id": conv["id"]},
    )
    return conv


@app.get("/api/channels")
async def channels(user_id: str = Depends(auth.current_user_id)):
    return dbx.list_channels()


@app.post("/api/channels/{cid}/join")
async def join_channel(cid: str, user_id: str = Depends(auth.current_user_id)):
    conv = dbx.get_conversation(cid)
    if not conv or conv["type"] != "channel":
        raise HTTPException(status_code=404, detail="Channel not found")
    dbx.add_member(cid, user_id)
    return {"ok": True}


@app.get("/api/conversations/{cid}/messages")
async def messages(cid: str, before: float = 0, limit: int = 100,
                   user_id: str = Depends(auth.current_user_id)):
    """`before` pages backwards through history: pass the created_at of the oldest
    message you already have. Without it the older half of a long conversation was
    simply unreachable."""
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")
    limit = max(1, min(int(limit or 100), 200))
    # Anything from before this member deleted the chat stays hidden from them,
    # while remaining intact for everyone else.
    return dbx.reply_previews(dbx.get_messages(
        cid, limit=limit, since=dbx.hidden_at(cid, user_id), before=before))


@app.post("/api/conversations/{cid}/read")
async def mark_read(cid: str, user_id: str = Depends(auth.current_user_id)):
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")
    dbx.mark_read(cid, user_id)
    await hub.broadcast_conversation(cid, {"type": "read_update", "conversation_id": cid,
                                           "user_id": user_id, "read_at": dbx.now()})
    return {"ok": True}


@app.get("/api/conversations/{cid}/shared")
async def shared(cid: str, user_id: str = Depends(auth.current_user_id)):
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")
    return dbx.shared_media(cid, since=dbx.hidden_at(cid, user_id))



# ============================== profile ==============================

class ProfileUpdate(BaseModel):
    name: str
    title: str = ""


@app.put("/api/me")
async def update_me(body: ProfileUpdate, user_id: str = Depends(auth.current_user_id)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    return dbx.update_profile(user_id, name, body.title.strip())


@app.post("/api/me/avatar")
async def upload_avatar(file: UploadFile, user_id: str = Depends(auth.current_user_id)):
    if file.content_type not in IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Please upload a PNG, JPG, GIF, or WebP image")
    data = await read_capped(file, 5 * 1024 * 1024, "Image")
    fid = dbx.new_id()
    ext = Path(file.filename or "a.png").suffix[:10] or ".png"
    (UPLOADS / (fid + ext)).write_bytes(data)
    url = f"/files/{fid}{ext}"
    dbx.update_avatar(user_id, url)
    return {"avatar": url}


@app.post("/api/auth/logout_all")
async def logout_all(user_id: str = Depends(auth.current_user_id)):
    """Invalidate every existing session token for this account and issue a fresh one."""
    tv = dbx.bump_token_version(user_id)
    return {"token": auth.make_token(user_id, tv)}


@app.get("/api/search")
async def search(q: str, user_id: str = Depends(auth.current_user_id)):
    q = q.strip()
    if len(q) < 2:
        return {"messages": [], "users": [], "agents": []}
    agents_hits = [a for a in dbx.my_agents(user_id)
                   if q.lower() in a["name"].lower() or q.lower() in (a["description"] or "").lower()]
    return {
        "messages": dbx.search_messages(user_id, q),
        "users": dbx.search_users(q),
        "agents": [{"id": a["id"], "name": a["name"], "slug": a["slug"],
                    "user_id": a["user_id"], "description": a["description"]} for a in agents_hits],
    }


# ============================== agents ==============================

def _mask(agent: dict, viewer_id: str | None = None) -> dict:
    a = dict(agent)
    a.pop("api_key", "")
    a["api_key_set"] = False
    a["echo_mode"] = agx.agent_is_echo(agent)
    # The workspace-local model server URL is never exposed to any browser.
    a.pop("ollama_url", None)
    # Decode JSON columns so the Edit form can repopulate.
    try:
        a["capabilities"] = json.loads(a.get("capabilities") or "{}")
    except Exception:
        a["capabilities"] = {}
    try:
        a["mcp_connectors"] = json.loads(a.get("mcp_connectors") or "[]")
    except Exception:
        a["mcp_connectors"] = []
    a.setdefault("type", "product")
    a.setdefault("instruction", a.get("description", ""))
    return a


def _clean_product_url(value: str) -> str:
    """Accept 'sportstech.de/f37' as well as a full URL, but never let a non-web
    scheme through. Blindly prefixing 'https://' is not enough: it would turn
    'javascript:alert(1)' into 'https://javascript:alert(1)', which parses fine
    and would later be handed to the agent's fetch_url tool.
    """
    v = (value or "").strip()
    if not v:
        return ""
    low = v.lower()
    if not low.startswith(("http://", "https://")):
        scheme = re.match(r"^([a-z][a-z0-9+.\-]*):(.*)$", low, re.I)
        # A colon can also be a port ('sportstech.de:8080/f37'), so only treat it
        # as a scheme when what follows isn't a port number.
        if scheme and not scheme.group(2).split("/")[0].isdigit():
            raise HTTPException(status_code=400,
                                detail="Product site link must start with http:// or https://")
        v = "https://" + v
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(status_code=400, detail="Product site link must be a valid http(s) URL")
    return v[:500]


class AgentBody(BaseModel):
    type: str = "product"                 # product | create | personal
    name: str
    provider: str = "ollama"              # ollama | claude | gemini
    description: str = ""
    instruction: str = ""
    # Product agents only: which product this agent covers, and its page. Kept
    # separate from `name` so the agent can be called anything while still being
    # pinned to a specific product the tools can look up.
    product_name: str = ""
    product_url: str = ""
    model: str = ""                       # a real model name from /api/models (or "default")
    capabilities: dict = {}
    mcp_connectors: list = []


@app.get("/api/agents")
async def list_agents(user_id: str = Depends(auth.current_user_id)):
    return [_mask(a, user_id) for a in dbx.my_agents(user_id)]


@app.get("/api/models")
async def workspace_models(user_id: str = Depends(auth.current_user_id)):
    """Models available on the workspace-local server. The URL is never exposed."""
    url = LOCAL_LLM_URL.strip().rstrip("/")
    if agx.is_echo_key(url):                       # LOCAL_LLM_URL="test" -> offline/echo mode
        return {"models": ["echo-model"], "note": "echo mode"}
    if not url.startswith("http"):
        url = "http://" + url
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        models = [m["name"] for m in data.get("models", [])]
        return {"models": models,
                "note": "" if models else "No models installed — run: ollama pull llama3.2"}
    except Exception as e:
        return {"models": [], "note": f"Local model server unreachable ({type(e).__name__})"}


@app.post("/api/agents")
async def create_agent(body: AgentBody, user_id: str = Depends(auth.current_user_id)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Agent name required")
    model = (body.model or "").strip() or LOCAL_LLM_DEFAULT_MODEL
    instruction = (body.instruction or body.description or "").strip()
    caps = json.dumps(body.capabilities or {})
    mcp = json.dumps(body.mcp_connectors or [])
    product_name = (body.product_name or "").strip()[:200]
    product_url = _clean_product_url(body.product_url)
    base = dbx.slugify(name)
    slug, n = base, 2
    while dbx.slug_taken(slug):
        slug, n = f"{base}{n}", n + 1
    owner = dbx.get_user(user_id)
    bot_user = dbx.upsert_user(f"{slug}@agents.sportstech.local", name,
                               title=f"AI Agent · by {owner['name']}", is_bot=True)
    # Local server is the default: provider + URL are set here, server-side, and
    # the client never sees them. api_key stays empty (the local server needs none).
    agent = dbx.create_agent(
        user_id, bot_user["id"], name, slug,
        instruction, "", model,
        # A claude/gemini agent runs on the user's own machine with their own key;
        # ollama_url is irrelevant there but harmless to keep.
        provider=(body.provider if body.provider in ("ollama", "claude", "gemini") else "ollama"),
        ollama_url=LOCAL_LLM_URL,
        type=body.type, instruction=instruction, capabilities=caps, mcp_connectors=mcp,
        product_name=product_name, product_url=product_url)
    dbx.audit(user_id, "agent.create", agent["id"], f"{body.type} / {body.provider}")
    return _mask(agent)


@app.put("/api/agents/{aid}")
async def edit_agent(aid: str, body: AgentBody, user_id: str = Depends(auth.current_user_id)):
    agent = dbx.get_agent(aid)
    if not agent or agent["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    name = (body.name or "").strip() or agent["name"]
    model = (body.model or "").strip() or agent.get("model") or LOCAL_LLM_DEFAULT_MODEL
    instruction = (body.instruction or body.description or "").strip()
    caps = json.dumps(body.capabilities or {})
    mcp = json.dumps(body.mcp_connectors or [])
    product_name = (body.product_name or "").strip()[:200]
    product_url = _clean_product_url(body.product_url)
    ok = dbx.update_agent(
        aid, user_id, name, instruction, None, model,
        # A claude/gemini agent runs on the user's own machine with their own key;
        # ollama_url is irrelevant there but harmless to keep.
        provider=(body.provider if body.provider in ("ollama", "claude", "gemini") else "ollama"),
        ollama_url=LOCAL_LLM_URL,
        type=body.type, instruction=instruction, capabilities=caps, mcp_connectors=mcp,
        product_name=product_name, product_url=product_url)
    if not ok:
        raise HTTPException(status_code=400, detail="Update failed")
    return _mask(dbx.get_agent(aid))


class McpUrlBody(BaseModel):
    url: str


@app.get("/api/mcp/status")
async def mcp_status(user_id: str = Depends(auth.current_user_id)):
    """Which connectors this user's own machine has signed in to. Tokens are stored
    on that machine and never reach the server, so this only reports state."""
    return await mcp.status(user_id)


@app.post("/api/mcp/test")
async def mcp_test(body: McpUrlBody, user_id: str = Depends(auth.current_user_id)):
    """Try a connector from the user's own machine and report what happened."""
    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Connector URL must be http(s)")
    return await mcp.probe(user_id, url)


@app.post("/api/mcp/connect")
async def mcp_connect(body: McpUrlBody, user_id: str = Depends(auth.current_user_id)):
    """Start OAuth on the user's own machine — their browser opens there."""
    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Connector URL must be http(s)")
    res = await mcp._ask(user_id, "mcp_auth", {"url": url})
    # needsClientId is a setup step the UI can act on, not a failure.
    if res.get("needsClientId"):
        return res
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class McpClientBody(BaseModel):
    url: str
    client_id: str
    client_secret: str = ""


@app.post("/api/mcp/client")
async def mcp_set_client(body: McpClientBody, user_id: str = Depends(auth.current_user_id)):
    """Store a client ID the user registered themselves, for a connector whose
    provider has no dynamic registration. Stored on their machine, like the token."""
    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Connector URL must be http(s)")
    if not (body.client_id or "").strip():
        raise HTTPException(status_code=400, detail="A client ID is required")
    res = await mcp._ask(user_id, "mcp_client", {
        "url": url, "clientId": body.client_id, "clientSecret": body.client_secret})
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


class McpTokenBody(BaseModel):
    url: str
    token: str = ""


@app.post("/api/mcp/token")
async def mcp_set_token(body: McpTokenBody, user_id: str = Depends(auth.current_user_id)):
    """Store an access token the user pasted, instead of running OAuth. Kept on
    their machine like every other credential; an empty token clears it."""
    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Connector URL must be http(s)")
    res = await mcp._ask(user_id, "mcp_token", {"url": url, "token": body.token})
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    mcp._cache.pop((user_id, url), None)     # re-discover with the new token
    return res


@app.get("/api/mcp/redirect-uri")
async def mcp_redirect_uri(user_id: str = Depends(auth.current_user_id)):
    """The redirect URI to register with a provider, read from the user's own app."""
    return await mcp._ask(user_id, "mcp_redirect", {})


@app.post("/api/mcp/disconnect")
async def mcp_disconnect(body: McpUrlBody, user_id: str = Depends(auth.current_user_id)):
    return await mcp.logout(user_id, (body.url or "").strip())


@app.post("/api/messages/{mid}/rerun")
async def rerun_message(mid: str, user_id: str = Depends(auth.current_user_id)):
    """Ask the agent to answer again.

    On an AGENT message: that reply is deleted and a fresh one generated, so the
    chat isn't left with two competing answers to the same question.
    On a HUMAN message: the agent simply answers it again; nothing is deleted,
    because deleting somebody's own words is not what "re-run" should mean.
    """
    msg = dbx.get_message(mid)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    cid = msg["conversation_id"]
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")

    conv_agents = dbx.conversation_agents(cid)
    if not conv_agents:
        raise HTTPException(status_code=400, detail="No agent in this conversation")

    sender = dbx.get_user(msg["sender_id"])
    from_agent = bool(sender and sender.get("is_bot"))

    # Pick the agent that wrote it; otherwise the one in the room.
    agent = next((a for a in conv_agents if a["user_id"] == msg["sender_id"]), None) \
        or conv_agents[0]
    if not dbx.has_agent_access(agent["id"], user_id):
        raise HTTPException(status_code=403, detail="You don't have access to that agent")

    if from_agent:
        # Remove the old answer first so the agent doesn't read its own previous
        # reply as context and simply paraphrase it.
        dbx.delete_message(mid, msg["sender_id"])
        await hub.broadcast_conversation(cid, {
            "type": "message_delete", "conversation_id": cid, "message_id": mid})

    if (_agent_runs.get(cid) or {}).get(agent["user_id"]):
        raise HTTPException(status_code=409, detail="That agent is already working")

    asyncio.create_task(reply_as(
        agent["user_id"], agent["name"], cid,
        lambda a=agent: agx.agent_reply(
            a, cid, tool_reporter(cid, a["user_id"], a["name"]), requester_id=user_id)))
    return {"ok": True, "agent": agent["name"], "replaced": from_agent}


@app.post("/api/messages/{mid}/rerun")
async def rerun_message(mid: str, user_id: str = Depends(auth.current_user_id)):
    """Ask the agent to answer again.

    Target an AGENT message and that reply is removed first, so you get a
    replacement rather than a second opinion stacked underneath. Target your own
    message and the agent simply answers it again, leaving the original in place.
    """
    msg = dbx.get_message(mid)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    cid = msg["conversation_id"]
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")

    conv_agents = dbx.conversation_agents(cid)
    if not conv_agents:
        raise HTTPException(status_code=400, detail="No agent in this conversation")

    sender = dbx.get_user(msg["sender_id"])
    is_agent_msg = bool(sender and sender["is_bot"])

    # Prefer the agent that wrote the reply being re-run; otherwise the first one here.
    agent = next((a for a in conv_agents if a["user_id"] == msg["sender_id"]), None) \
        or conv_agents[0]
    if not dbx.has_agent_access(agent["id"], user_id):
        raise HTTPException(status_code=403, detail="You don't have access to that agent")

    if is_agent_msg:
        # Remove the old answer so the new one replaces it. delete_message is scoped
        # to the sender, and the sender here is the agent, not the person asking.
        with dbx.db() as conn:
            conn.execute("UPDATE messages SET deleted = 1, content = '' WHERE id = ?", (mid,))
        await hub.broadcast_conversation(cid, {
            "type": "message_delete", "conversation_id": cid, "message_id": mid})

    asyncio.create_task(reply_as(
        agent["user_id"], agent["name"], cid,
        lambda a=agent: agx.agent_reply(a, cid, tool_reporter(cid, a["user_id"], a["name"]),
                                        requester_id=user_id)))
    return {"ok": True, "agent": agent["name"], "replaced": is_agent_msg}


@app.post("/api/conversations/{cid}/stop")
async def stop_agents(cid: str, user_id: str = Depends(auth.current_user_id)):
    """Stop whatever agents are generating in this conversation.

    Same effect as the websocket "agent_stop" message. Exists so the stop path can
    be tested independently of the socket — if this works and the button doesn't,
    the problem is the frontend, not the cancellation.
    """
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")
    running = list((_agent_runs.get(cid) or {}).keys())
    stopped = await cancel_agent_runs(cid)
    return {"stopped": stopped, "agents": running}


@app.get("/api/conversations/{cid}/running")
async def running_agents(cid: str, user_id: str = Depends(auth.current_user_id)):
    """Which agents are mid-generation here right now."""
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")
    names = []
    for bot_id in (_agent_runs.get(cid) or {}):
        u = dbx.get_user(bot_id)
        names.append(u["name"] if u else bot_id)
    return {"running": names, "count": len(names)}


class LlmProbe(BaseModel):
    provider: str


@app.post("/api/llm/models")
async def llm_models(body: LlmProbe, user_id: str = Depends(auth.current_user_id)):
    """Models this user's own API key can reach. The key stays on their machine;
    only the resulting model list comes back."""
    p = (body.provider or "").lower()
    if p not in ("claude", "gemini"):
        raise HTTPException(status_code=400, detail="provider must be claude or gemini")
    res = await mcp._ask(user_id, "llm_models", {"provider": p})
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@app.get("/api/llm/keys")
async def llm_keys(user_id: str = Depends(auth.current_user_id)):
    """Which providers this user has configured. Never returns a key."""
    return await mcp._ask(user_id, "llm_keys", {})


# ============================ username + password ============================
# There is deliberately NO signup endpoint. An account exists only because an
# admin created it, which is what makes "only people on the list can log in" true
# by construction rather than by a check someone might forget.

SENSITIVE_USER_FIELDS = ("password_hash", "failed_attempts", "locked_until",
                         "password_set_at", "token_version")


def _public_user(user: dict) -> dict:
    """Never hand a user row straight to a client. Adding password_hash to the
    users table meant every existing login response would have shipped the hash to
    the browser — an offline cracking target for anyone who signs in."""
    return {k: v for k, v in dict(user or {}).items() if k not in SENSITIVE_USER_FIELDS}


class LoginBody(BaseModel):
    email: str
    password: str


class ChangePwBody(BaseModel):
    current_password: str
    new_password: str


class NewUserBody(BaseModel):
    email: str
    name: str = ""
    password: str = ""          # blank = generate one and return it once
    is_admin: bool = False


class SetupBody(BaseModel):
    email: str
    name: str = ""
    password: str


@app.post("/api/auth/setup")
async def first_run_setup(body: SetupBody, request: Request):
    """Create the very first admin, from the login screen, once.

    Open on purpose — but only while there are NO admins. The moment one exists
    this returns 403 forever, so it cannot be used to add a second back door.
    """
    if pw.admin_count() > 0:
        raise HTTPException(status_code=403,
                            detail="Setup is already done. Ask an admin to add you.")
    rate_limit("setup", request.client.host if request.client else "?", 5, 60)

    email = pw.normalise_email(body.email)
    if "@" not in email or len(email) < 5:
        raise HTTPException(status_code=400, detail="That doesn't look like an email address")
    problem = pw.password_problem(body.password, email)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    existing = pw.find_by_email(email)
    user = existing or dbx.upsert_user(email, body.name.strip() or email.split("@")[0])
    if body.name.strip() and user.get("name") != body.name.strip():
        with dbx.db() as conn:
            conn.execute("UPDATE users SET name = ? WHERE id = ?",
                         (body.name.strip(), user["id"]))
            user = dict(conn.execute("SELECT * FROM users WHERE id = ?",
                                     (user["id"],)).fetchone())
    # No must_change: they just chose it themselves.
    pw.set_password(user["id"], body.password, must_change=False)
    pw.set_admin(user["id"], True)
    dbx.audit(user["id"], "auth.first_run_setup", email)

    ver = dbx.get_user(user["id"]).get("token_version", 0)
    return {"token": auth.make_token(user["id"], ver), "user": _public_user(user)}


@app.post("/api/auth/login")
async def password_login(body: LoginBody):
    try:
        user = pw.attempt_login(body.email, body.password)
    except pw.LoginError as e:
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        raise HTTPException(status_code=401, detail=str(e), headers=headers)
    token = auth.make_token(user["id"], user.get("token_version", 0))
    dbx.set_last_seen(user["id"])
    return {
        "token": token,
        "user": _public_user(user),
        # The client should send the person straight to a change form; the password
        # they were given is meant to be used once.
        "must_change_password": bool(user.get("must_change_password")),
    }


@app.post("/api/auth/change-password")
async def do_change_password(body: ChangePwBody,
                             user_id: str = Depends(auth.current_user_id)):
    problem = pw.change_password(user_id, body.current_password, body.new_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    # Every other session is invalidated: if the change was prompted by a suspected
    # leak, leaving old tokens working would defeat the point.
    ver = dbx.bump_token_version(user_id)
    return {"ok": True, "token": auth.make_token(user_id, ver)}


def _require_admin_user(user_id: str):
    """The first account is admin by default, otherwise nobody could grant it."""
    if pw.admin_count() == 0:
        return
    if not pw.is_admin(user_id):
        raise HTTPException(status_code=403, detail="Admins only")


@app.get("/api/admin/users")
async def admin_list_users(user_id: str = Depends(auth.current_user_id)):
    _require_admin_user(user_id)
    with dbx.db() as conn:
        rows = conn.execute(
            """SELECT id, email, name, is_admin, must_change_password, locked_until,
                      failed_attempts, password_hash != '' AS can_log_in,
                      COALESCE(last_seen_at, 0) AS last_seen_at
               FROM users WHERE is_bot = 0 ORDER BY LOWER(name)""").fetchall()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/admin/users")
async def admin_create_user(body: NewUserBody, user_id: str = Depends(auth.current_user_id)):
    """Create an account, or set a password on one that already exists (people who
    signed in with Google before, for instance)."""
    _require_admin_user(user_id)
    email = pw.normalise_email(body.email)
    if "@" not in email or len(email) < 5:
        raise HTTPException(status_code=400, detail="That doesn't look like an email address")

    password = (body.password or "").strip() or pw.generate_password()
    if body.password:
        problem = pw.password_problem(password, email)
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    existing = pw.find_by_email(email)
    user = existing or dbx.upsert_user(email, body.name.strip() or email.split("@")[0])
    pw.set_password(user["id"], password, must_change=True)
    if body.is_admin:
        pw.set_admin(user["id"], True)

    # Returned ONCE. It is not stored anywhere in readable form, so this response
    # is the only chance to pass it on.
    return {"ok": True, "created": not existing, "user": _public_user(user),
            "password": password,
            "note": "Give this to them now — it can't be shown again, and they must "
                    "change it at first login."}


@app.post("/api/admin/users/{target_id}/reset-password")
async def admin_reset_password(target_id: str, user_id: str = Depends(auth.current_user_id)):
    _require_admin_user(user_id)
    target = dbx.get_user(target_id)
    if not target or target.get("is_bot"):
        raise HTTPException(status_code=404, detail="No such person")
    password = pw.generate_password()
    pw.set_password(target_id, password, must_change=True)
    dbx.bump_token_version(target_id)     # sign them out everywhere
    return {"ok": True, "password": password,
            "note": "Shown once. They are signed out on all devices."}


@app.post("/api/admin/users/{target_id}/revoke")
async def admin_revoke(target_id: str, user_id: str = Depends(auth.current_user_id)):
    """Remove access without deleting the person or their messages."""
    _require_admin_user(user_id)
    if target_id == user_id:
        raise HTTPException(status_code=400, detail="You can't revoke your own access")
    target = dbx.get_user(target_id)
    if not target or target.get("is_bot"):
        raise HTTPException(status_code=404, detail="No such person")
    pw.clear_password(target_id)
    dbx.bump_token_version(target_id)
    return {"ok": True, "name": target["name"]}


@app.delete("/api/admin/users/{target_id}")
async def admin_delete_user(target_id: str, user_id: str = Depends(auth.current_user_id)):
    """Permanently remove a person. Not reversible."""
    _require_admin_user(user_id)
    if target_id == user_id:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    target = dbx.get_user(target_id)
    if not target or target.get("is_bot"):
        raise HTTPException(status_code=404, detail="No such person")
    if pw.is_admin(target_id) and pw.admin_count() <= 1:
        raise HTTPException(status_code=400,
                            detail="That's the only administrator left")
    # Log BEFORE deleting: afterwards there is no record of who they were.
    dbx.audit(user_id, "admin.delete_user", target_id,
              f"{target['email']} ({target['name']})")
    result = dbx.delete_user_completely(target_id)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    await hub.disconnect_user(target_id)
    return result


@app.get("/api/admin/users/{target_id}/activity")
async def admin_user_activity(target_id: str, limit: int = 100,
                              user_id: str = Depends(auth.current_user_id)):
    """What this person has been doing: counts plus their audit trail."""
    _require_admin_user(user_id)
    data = dbx.user_activity(target_id, max(1, min(int(limit or 100), 500)))
    if data.get("error"):
        raise HTTPException(status_code=404, detail=data["error"])
    return data


@app.get("/api/admin/overview")
async def admin_overview(user_id: str = Depends(auth.current_user_id)):
    """Headline numbers for the admin page."""
    _require_admin_user(user_id)
    day = dbx.now() - 86400
    week = dbx.now() - 7 * 86400
    with dbx.db() as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()["c"]
        return {
            "people": q("SELECT COUNT(*) c FROM users WHERE is_bot = 0"),
            "can_sign_in": q("SELECT COUNT(*) c FROM users WHERE is_bot = 0 "
                             "AND password_hash != ''"),
            "signed_in_ever": q("SELECT COUNT(*) c FROM users WHERE is_bot = 0 "
                                "AND COALESCE(last_seen_at, 0) > 0"),
            "active_24h": q("SELECT COUNT(*) c FROM users WHERE is_bot = 0 "
                            "AND COALESCE(last_seen_at, 0) > ?", day),
            "active_7d": q("SELECT COUNT(*) c FROM users WHERE is_bot = 0 "
                           "AND COALESCE(last_seen_at, 0) > ?", week),
            "locked": q("SELECT COUNT(*) c FROM users WHERE is_bot = 0 "
                        "AND COALESCE(locked_until, 0) > ?", dbx.now()),
            "admins": pw.admin_count(),
            "agents": q("SELECT COUNT(*) c FROM agents"),
            "conversations": q("SELECT COUNT(*) c FROM conversations"),
            "messages": q("SELECT COUNT(*) c FROM messages WHERE deleted = 0"),
            "online_now": len(hub.online_user_ids()) if hasattr(hub, "online_user_ids") else 0,
        }


@app.post("/api/admin/users/{target_id}/unlock")
async def admin_unlock(target_id: str, user_id: str = Depends(auth.current_user_id)):
    _require_admin_user(user_id)
    with dbx.db() as conn:
        conn.execute("UPDATE users SET failed_attempts = 0, locked_until = 0 WHERE id = ?",
                     (target_id,))
    return {"ok": True}


@app.get("/api/audit")
async def audit_log(limit: int = 200, mine: bool = True,
                    user_id: str = Depends(auth.current_user_id)):
    """What has been done, newest first. Defaults to the caller's own actions —
    a shared history would be a privacy problem in itself."""
    limit = max(1, min(int(limit or 200), 500))
    return {"entries": dbx.audit_recent(limit, actor_id="" if not mine else user_id)}


class ApprovalDecision(BaseModel):
    approve: bool


@app.get("/api/conversations/{cid}/runs")
async def list_runs(cid: str, user_id: str = Depends(auth.current_user_id)):
    """Task activity for this conversation, for the progress view."""
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")
    return {"runs": dbx.runs_for_conversation(cid)}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, user_id: str = Depends(auth.current_user_id)):
    run = dbx.get_run(run_id)
    if not run or not dbx.is_member(run["conversation_id"], user_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user_id: str = Depends(auth.current_user_id)):
    run = dbx.get_run(run_id)
    if not run or not dbx.is_member(run["conversation_id"], user_id):
        raise HTTPException(status_code=404, detail="Run not found")
    live = agx.LIVE_RUNS.get(run_id)
    if live is not None:
        live.cancel()          # reaches every agent in the run, at any depth
    dbx.set_run_status(run_id, "cancelled", "Cancelled by " + (
        (dbx.get_user(user_id) or {}).get("name", "a user")))
    dbx.add_run_event(run_id, "cancelled", f"by {user_id}")
    return dbx.get_run(run_id)


@app.post("/api/approvals/{approval_id}")
async def decide_approval(approval_id: str, body: ApprovalDecision,
                          user_id: str = Depends(auth.current_user_id)):
    """Approve or reject. Bound to one artifact version and one approver, and
    safe to click twice."""
    res = dbx.decide_approval(approval_id, user_id, body.approve)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    if res.get("already"):
        # Not an error: the person clicked twice, or two tabs were open.
        return {"ok": True, "already_decided": True, "approval": res}

    dbx.add_run_event(res["run_id"], "approval",
                      f"{'approved' if body.approve else 'rejected'} {res['action']}")
    if not body.approve:
        dbx.set_run_status(res["run_id"], "waiting_for_input",
                           "Rejected — waiting for a revision")
        return {"ok": True, "approval": res}

    # Approved. Claim it before doing anything, so a retry or a second tab
    # cannot publish the same thing twice.
    if not dbx.claim_approval_execution(approval_id):
        return {"ok": True, "already_executing": True, "approval": res}

    outcome = ("No publishing connector is configured for this action, so nothing "
               "was sent anywhere. The approved file is in the conversation and can "
               "be downloaded and uploaded by hand.")
    dbx.record_approval_result(approval_id, outcome)
    dbx.set_run_status(res["run_id"], "completed")
    dbx.add_run_event(res["run_id"], "note", outcome)
    return {"ok": True, "approval": dbx.get_run(res["run_id"])["approvals"][0],
            "result": outcome}


@app.get("/api/devices")
async def my_devices(user_id: str = Depends(auth.current_user_id)):
    """Which of this user's clients are connected, and which can run local tools."""
    devices = hub.devices(user_id)
    return {
        "devices": devices,
        "local_tools_available": hub.local_tool_socket(user_id) is not None,
        "status": hub.local_tool_status(user_id),
        "registered_clients": len([d for d in devices if d.get("device_id")]),
        "connected_clients": len(devices),
    }


@app.get("/api/agents/{aid}/diagnose")
async def diagnose_agent(aid: str, user_id: str = Depends(auth.current_user_id)):
    """Exactly what this agent can do, straight from the runtime.

    `tools` is produced by the same tools_for() the model is given, so if a tool
    isn't in this list the model never saw it. That distinguishes "the switch
    didn't save" from "the server is running old code" from "the model ignored
    the tool" — which is otherwise guesswork.
    """
    agent = dbx.get_agent(aid)
    if not agent or not dbx.has_agent_access(aid, user_id):
        raise HTTPException(status_code=404, detail="Agent not found")

    caps = agx.agent_caps(agent)
    conv = dbx.find_dm(user_id, agent["user_id"])
    cid = conv["id"] if conv else ""
    tools = [t["function"]["name"] for t in agx.tools_for(agent, cid, user_id)]

    expected = {
        "files": ["list_files", "read_file", "write_file"],
        "web_scraping": ["web_search", "fetch_url"],
    }
    problems = []
    for cap, names in expected.items():
        if caps.get(cap) and not set(names) <= set(tools):
            problems.append(f"'{cap}' is ON but {names} are missing — the server is probably "
                            f"running an older agents.py. Restart it.")
        if not caps.get(cap) and set(names) & set(tools):
            problems.append(f"'{cap}' is OFF but its tools are present.")
    # Connectors are the most common "why can't my agent do X" — report what is
    # stored on the agent and whether each one actually answers.
    connectors = []
    for url in mcp.agent_connectors(agent):
        probe = await mcp.probe(user_id, url)
        connectors.append({"url": url, "name": mcp.server_label(url), **probe})
    if agent.get("provider") in ("claude", "gemini"):
        owner = dbx.get_user(agent["owner_id"]) or {}
        if not hub.local_tool_socket(agent["owner_id"]):
            problems.append(
                f"This agent uses {agent['provider']} on its owner's key, and "
                f"{owner.get('name') or 'the owner'}'s computer isn't connected. "
                "It will fall back to the asker's own key if they have one, "
                "otherwise it can't run. Switch to a workspace model for it to run "
                "on the server instead.")

    if not connectors:
        problems.append("This agent has NO connectors saved, which is why it says it has no "
                        "integrations. Edit the agent, add one with the Add button under MCP "
                        "connectors, and save. Connectors are per agent, not global.")
    for c in connectors:
        if c.get("needs_auth"):
            problems.append(f"'{c['name']}' is saved but not signed in yet — ask the agent to "
                            "connect it, or use POST /api/mcp/connect.")
        elif not c.get("ok"):
            problems.append(f"'{c['name']}' is saved but failed: {c.get('error')}")

    if "list_my_files" not in tools:
        problems.append("Local machine tools are not offered because " + hub.local_tool_status(user_id)
                        + ". Open the Sportstech desktop app; no agent setting is involved.")
    if agent.get("provider") != "ollama":
        problems.append(f"provider is '{agent.get('provider')}', not ollama — tools_for() "
                        "is only used on the Ollama path.")

    return {
        "agent": agent["name"],
        "type": agent.get("type"),
        "provider": agent.get("provider"),
        "model": agent.get("model"),
        "capabilities": caps,
        "capabilities_raw": agent.get("capabilities"),
        "local_client_status": hub.local_tool_status(user_id),
        # For a shared agent on a personal key, what matters is whether the
        # OWNER's machine is reachable — not the asker's.
        "runs_on": ("owner" if agent.get("provider") in ("claude", "gemini")
                    else "server"),
        "owner_client_status": (hub.local_tool_status(agent["owner_id"])
                                if agent.get("provider") in ("claude", "gemini")
                                else "not needed"),
        "connectors_saved_on_agent": mcp.agent_connectors(agent),
        "connectors": connectors,
        "tools_the_model_actually_gets": tools,
        "problems": problems or ["Nothing obviously wrong — the tools are present."],
    }


@app.delete("/api/agents/{aid}")
async def remove_agent(aid: str, user_id: str = Depends(auth.current_user_id)):
    if not dbx.delete_agent(aid, user_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    scheduler.delete_schedules_for_agent(aid)   # don't leave orphaned schedules behind
    return {"ok": True}


class ShareBody(BaseModel):
    email: str


@app.post("/api/agents/{aid}/share")
async def share_agent(aid: str, body: ShareBody, user_id: str = Depends(auth.current_user_id)):
    agent = dbx.get_agent(aid)
    if not agent or agent["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    target = dbx.get_user_by_email(body.email.lower().strip())
    if not target:
        raise HTTPException(status_code=404, detail="No user with that email has signed in yet")
    if target["id"] == user_id:
        raise HTTPException(status_code=400, detail="You already own this agent")
    dbx.share_agent(aid, target["id"])
    return {"ok": True, "shared_with": dbx.agent_shares(aid)}


@app.get("/api/agents/requests")
async def list_requests(user_id: str = Depends(auth.current_user_id)):
    return dbx.pending_requests_for_owner(user_id)


@app.post("/api/agents/requests/{rid}/{action}")
async def resolve_request(rid: str, action: str, user_id: str = Depends(auth.current_user_id)):
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Action must be approve or reject")
    req = dbx.get_request(rid)
    if not req or req["status"] != "pending":
        raise HTTPException(status_code=404, detail="Request not found")
    agent = dbx.get_agent(req["agent_id"])
    if not agent or agent["owner_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your agent")
    if action == "approve":
        dbx.share_agent(agent["id"], req["user_id"])
        dbx.resolve_request(rid, "approved")
    else:
        dbx.resolve_request(rid, "rejected")
    await hub.notify_users([req["user_id"]], {
        "type": "agent_request_resolved", "approved": action == "approve",
        "agent_name": agent["name"], "agent_user_id": agent["user_id"]})
    return {"ok": True}


@app.delete("/api/agents/{aid}/share/{target_id}")
async def unshare_agent(aid: str, target_id: str, user_id: str = Depends(auth.current_user_id)):
    agent = dbx.get_agent(aid)
    if not agent or agent["owner_id"] != user_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    dbx.unshare_agent(aid, target_id)
    return {"ok": True}




@app.post("/api/ollama/detect")
async def ollama_detect(request: Request, user_id: str = Depends(auth.current_user_id)):
    """Auto-detect Ollama on the *requesting device*: probe the caller's own IP only.
    This guarantees users connect to their own machine, never the host's."""
    client_ip = (request.client.host if request.client else "") or ""
    if client_ip in ("127.0.0.1", "::1", "localhost"):
        candidates = [f"http://localhost:{OLLAMA_PORT}"]
    else:
        candidates = [f"http://{client_ip}:{OLLAMA_PORT}"]
    import httpx as _httpx
    for url in candidates:
        try:
            async with _httpx.AsyncClient(timeout=4) as client:
                resp = await client.get(f"{url}/api/tags")
                resp.raise_for_status()
                models = [m["name"] for m in resp.json().get("models", [])]
            return {"ok": True, "url": url, "models": models,
                    "note": "" if models else "Connected, but no models installed — run: ollama pull llama3.2"}
        except Exception:
            continue
    raise HTTPException(status_code=502, detail=(
        f"No Ollama server found on your device ({client_ip or 'unknown'}:{OLLAMA_PORT}). "
        "Start Ollama with OLLAMA_HOST=0.0.0.0 so it's reachable over the network, "
        "allow port 11434 through your firewall, then try again."))


class OllamaProbe(BaseModel):
    url: str


@app.post("/api/ollama/models")
async def ollama_models(body: OllamaProbe, user_id: str = Depends(auth.current_user_id)):
    """List models on the user's Ollama server so they can pick one when creating an agent."""
    url = body.url.strip().rstrip("/")
    if not url:
        raise HTTPException(status_code=400, detail="URL required")
    if agx.is_echo_key(url):
        return {"ok": True, "models": ["echo-model"], "note": "echo mode"}
    if not url.startswith("http"):
        url = "http://" + url
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        return {"ok": True, "models": [m["name"] for m in data.get("models", [])]}
    except Exception as e:
        raise HTTPException(status_code=502,
            detail=f"Could not reach Ollama at {url} ({type(e).__name__}). "
                   "Make sure Ollama is running with OLLAMA_HOST=0.0.0.0 and the URL is reachable from this server.")


# ============================== group settings & permissions ==============================

def _require_member(cid: str, user_id: str) -> dict:
    conv = dbx.get_conversation(cid)
    if not conv or not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member of this conversation")
    return conv


def _require_admin(cid: str, user_id: str) -> dict:
    conv = _require_member(cid, user_id)
    if not dbx.is_admin(cid, user_id):
        raise HTTPException(status_code=403, detail="Only a group admin can do that")
    return conv


async def _notify_update(cid: str):
    await hub.broadcast_conversation(cid, {"type": "conversation_update", "conversation_id": cid})


class ConvMeta(BaseModel):
    name: str
    description: str = ""


@app.put("/api/conversations/{cid}")
async def rename_conversation(cid: str, body: ConvMeta, user_id: str = Depends(auth.current_user_id)):
    conv = _require_admin(cid, user_id)
    if conv["type"] == "dm":
        raise HTTPException(status_code=400, detail="DMs cannot be renamed")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    dbx.update_conversation_meta(cid, name, body.description.strip())
    await _notify_update(cid)
    return dbx.get_conversation(cid)


@app.post("/api/conversations/{cid}/image")
async def conversation_image(cid: str, file: UploadFile, user_id: str = Depends(auth.current_user_id)):
    _require_admin(cid, user_id)
    if file.content_type not in IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Please upload a PNG, JPG, GIF, or WebP image")
    data = await read_capped(file, 5 * 1024 * 1024, "Image")
    fid = dbx.new_id()
    ext = Path(file.filename or "g.png").suffix[:10] or ".png"
    (UPLOADS / (fid + ext)).write_bytes(data)
    url = f"/files/{fid}{ext}"
    dbx.set_conversation_image(cid, url)
    await _notify_update(cid)
    return {"image": url}


@app.delete("/api/conversations/{cid}")
async def delete_conversation(cid: str, scope: str = "me",
                              user_id: str = Depends(auth.current_user_id)):
    """Remove a conversation.

    scope="me" (default) hides it for the caller ONLY. Nothing is deleted: the
    other participants keep their copy and their full history. If someone posts
    again the chat reappears for the caller, showing only messages from that
    point on.

    scope="everyone" is the destructive one — it wipes the conversation, its
    messages and its attachments for all members. Restricted to admins of a
    group or channel, which is what the group settings "Delete group" button
    uses. A DM has no admin, so it can never be deleted for both sides.
    """
    conv = _require_member(cid, user_id)

    if scope == "me":
        dbx.hide_conversation(cid, user_id)
        dbx.audit(user_id, "conversation.hide", cid)
        # Only this user's client needs to know; nobody else's view changed.
        await hub.notify_users([user_id], {"type": "conversation_removed",
                                           "conversation_id": cid})
        return {"ok": True, "scope": "me"}

    if scope != "everyone":
        raise HTTPException(status_code=400, detail="scope must be 'me' or 'everyone'")
    if conv["type"] == "dm":
        raise HTTPException(status_code=400,
                            detail="A DM can only be removed from your own account")
    _require_admin(cid, user_id)

    member_ids = [m["id"] for m in dbx.conversation_members(cid)]
    # Collect attachments before the rows go, otherwise the files are orphaned
    # on disk with nothing left pointing at them.
    orphans = [m.get("content", "") for m in dbx.get_messages(cid, limit=100000)
               if m.get("kind", "text") in ("image", "file")]
    dbx.delete_conversation(cid)
    _remove_upload_files(orphans)
    dbx.audit(user_id, "conversation.delete_for_everyone", cid,
              f"{len(member_ids)} members, {len(orphans)} files")

    await hub.notify_users(member_ids, {"type": "conversation_removed", "conversation_id": cid})
    return {"ok": True, "scope": "everyone"}


def _remove_upload_files(urls: list[str]):
    """Delete uploaded files that belonged to a removed conversation. Best effort:
    a missing or locked file must never fail the delete the user asked for."""
    for url in urls:
        name = Path(str(url or "").split("?")[0]).name
        if not name:
            continue
        target = (UPLOADS / name).resolve()
        try:
            # Never follow a crafted path outside the uploads folder.
            if UPLOADS.resolve() in target.parents and target.is_file():
                target.unlink()
        except OSError as e:
            print(f"[cleanup] couldn't remove {name}: {e}", flush=True)


@app.post("/api/conversations/{cid}/leave")
async def leave_conversation(cid: str, user_id: str = Depends(auth.current_user_id)):
    conv = _require_member(cid, user_id)
    if conv["type"] == "dm":
        raise HTTPException(status_code=400, detail="You can't leave a DM")
    was_admin = dbx.is_admin(cid, user_id)
    dbx.remove_member(cid, user_id)
    if dbx.human_member_count(cid) == 0:
        member_ids = [m["id"] for m in dbx.conversation_members(cid)]
        dbx.delete_conversation(cid)
        await hub.notify_users(member_ids + [user_id], {"type": "conversation_removed", "conversation_id": cid})
        return {"ok": True, "deleted": True}
    if was_admin and dbx.admin_count(cid) == 0:
        promote = dbx.oldest_human_member(cid)
        if promote:
            dbx.set_admin_role(cid, promote, "admin")
    await hub.notify_users([user_id], {"type": "conversation_removed", "conversation_id": cid})
    await _notify_update(cid)
    return {"ok": True}


class AddMembers(BaseModel):
    user_ids: list[str]


@app.post("/api/conversations/{cid}/members")
async def add_members(cid: str, body: AddMembers, user_id: str = Depends(auth.current_user_id)):
    """Admins add humans. Any member can add an agent they own or have shared access to (or Nova)."""
    conv = _require_member(cid, user_id)
    if conv["type"] == "dm":
        raise HTTPException(status_code=400, detail="DMs are fixed at two members")
    caller_is_admin = dbx.is_admin(cid, user_id)
    for uid in body.user_ids:
        target = dbx.get_user(uid)
        if not target:
            continue
        if target["is_bot"]:
            agent = dbx.get_agent_by_user(uid)
            if agent and not dbx.has_agent_access(agent["id"], user_id):
                raise HTTPException(status_code=403,
                    detail=f"You don't have access to the agent '{target['name']}'. Ask its owner to share it.")
        elif not caller_is_admin:
            raise HTTPException(status_code=403, detail="Only a group admin can add people")
        dbx.add_member(cid, uid)
    await _notify_update(cid)
    return {"ok": True}


@app.delete("/api/conversations/{cid}/members/{target_id}")
async def remove_member_ep(cid: str, target_id: str, user_id: str = Depends(auth.current_user_id)):
    """Admins remove anyone. An agent's owner can always remove their own agent."""
    conv = _require_member(cid, user_id)
    if conv["type"] == "dm":
        raise HTTPException(status_code=400, detail="DMs are fixed at two members")
    target = dbx.get_user(target_id)
    if not target or not dbx.is_member(cid, target_id):
        raise HTTPException(status_code=404, detail="Not a member")
    allowed = dbx.is_admin(cid, user_id)
    if not allowed and target["is_bot"]:
        agent = dbx.get_agent_by_user(target_id)
        allowed = bool(agent and agent["owner_id"] == user_id)
    if not allowed:
        raise HTTPException(status_code=403, detail="Only a group admin can remove members")
    if not target["is_bot"] and dbx.is_admin(cid, target_id) and dbx.admin_count(cid) <= 1:
        raise HTTPException(status_code=400, detail="Promote another admin before removing the last one")
    dbx.remove_member(cid, target_id)
    await hub.notify_users([target_id], {"type": "conversation_removed", "conversation_id": cid})
    await _notify_update(cid)
    return {"ok": True}


class RoleBody(BaseModel):
    role: str  # admin | member


@app.put("/api/conversations/{cid}/members/{target_id}/role")
async def set_role(cid: str, target_id: str, body: RoleBody, user_id: str = Depends(auth.current_user_id)):
    _require_admin(cid, user_id)
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="Role must be admin or member")
    target = dbx.get_user(target_id)
    if not target or not dbx.is_member(cid, target_id):
        raise HTTPException(status_code=404, detail="Not a member")
    if target["is_bot"]:
        raise HTTPException(status_code=400, detail="Agents cannot be admins")
    if body.role == "member" and dbx.is_admin(cid, target_id) and dbx.admin_count(cid) <= 1:
        raise HTTPException(status_code=400, detail="A group needs at least one admin")
    dbx.set_admin_role(cid, target_id, body.role)
    await _notify_update(cid)
    return {"ok": True}


@app.get("/api/conversations/{cid}/addable")
async def addable_members(cid: str, user_id: str = Depends(auth.current_user_id)):
    """Who the caller can add: all humans (if admin) + agents they have access to + Nova."""
    conv = _require_member(cid, user_id)
    current = {m["id"] for m in dbx.conversation_members(cid)}
    caller_is_admin = dbx.is_admin(cid, user_id)
    my_agent_users = {a["user_id"] for a in dbx.my_agents(user_id)}
    out = []
    for u in dbx.list_users():
        if u["id"] in current:
            continue
        if u["is_bot"]:
            if u["id"] == BOT["id"] or u["id"] in my_agent_users:
                out.append(u)
        elif caller_is_admin:
            out.append(u)
    return out


# ============================== uploads (images & files) ==============================

@app.post("/api/conversations/{cid}/upload")
async def upload(cid: str, file: UploadFile, user_id: str = Depends(auth.current_user_id)):
    if not dbx.is_member(cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member")
    rate_limit("upload", user_id, 30, 60)
    data = await read_capped(file, MAX_UPLOAD, "File")
    fid = dbx.new_id()
    safe_name = Path(file.filename or "file").name
    ext = Path(safe_name).suffix[:10]
    (UPLOADS / (fid + ext)).write_bytes(data)
    kind = "image" if (file.content_type in IMAGE_TYPES) else "file"
    url = f"/files/{fid}{ext}"
    msg = dbx.add_message(cid, user_id, url, kind=kind, file_name=safe_name, file_size=len(data))
    sender = dbx.get_user(user_id)
    await hub.broadcast_conversation(cid, {
        "type": "message", **msg,
        "sender_name": sender["name"], "sender_avatar": sender["avatar"],
        "sender_is_bot": sender["is_bot"],
    })
    # Sharing a picture or file with an agent should make it look, without needing
    # a follow-up "what is this?" message.
    conv = dbx.get_conversation(cid)
    if conv and conv["type"] == "dm" and not sender["is_bot"]:
        conv_agents = dbx.conversation_agents(cid)
        if conv_agents and dbx.has_agent_access(conv_agents[0]["id"], user_id):
            agent = conv_agents[0]
            asyncio.create_task(reply_as(
                agent["user_id"], agent["name"], cid,
                lambda a=agent: agx.agent_reply(
                    a, cid, tool_reporter(cid, a["user_id"], a["name"]))))
    return msg


@app.get("/files/{fname}")
async def get_file(fname: str, request: Request, token: str = ""):
    """Serve an upload, but only to someone entitled to see it.

    This was previously open: any URL, guessed or shared, returned the file. That
    made every attachment in the system readable by anyone who could reach the
    server, including from conversations they were not in.

    The token may come from the Authorization header or a `token` query parameter,
    because <img> and <video> tags cannot send headers.
    """
    path = (UPLOADS / Path(fname).name).resolve()
    if not path.is_file() or UPLOADS.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="Not found")

    raw = token or ""
    if not raw:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            raw = header[7:]
    try:
        user_id = auth.decode_token(raw)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Sign in to view this file")

    allowed = dbx.file_viewers(f"/files/{path.name}")
    if user_id not in allowed:
        # 404 rather than 403: a 403 confirms the file exists, which is itself a
        # small leak when the name is being guessed.
        raise HTTPException(status_code=404, detail="Not found")

    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


# ============================== WebSocket hub ==============================

class Hub:
    """user_id -> set of live sockets. Chat, presence, reactions, and WebRTC signaling."""

    def online_user_ids(self) -> list[str]:
        return [uid for uid, socks in self.sockets.items() if socks]

    async def disconnect_user(self, user_id: str):
        """Close every socket for one person — used after deleting the account,
        so an open tab doesn't keep receiving messages."""
        for ws in list(self.sockets.get(user_id, set())):
            try:
                await ws.close(code=4001)
            except Exception:
                pass
        self.sockets.pop(user_id, None)

    def __init__(self):
        self.sockets: dict[str, set[WebSocket]] = {}
        # Per-socket device info from client_register: {device_id, platform, app,
        # local_tools}. Needed because a user is often connected twice (web tab +
        # desktop app) and only the desktop one can run local tools.
        self.meta: dict[WebSocket, dict] = {}
        self.lock = asyncio.Lock()

    async def connect(self, user_id: str, ws: WebSocket):
        async with self.lock:
            self.sockets.setdefault(user_id, set()).add(ws)

    async def disconnect(self, user_id: str, ws: WebSocket):
        async with self.lock:
            if user_id in self.sockets:
                self.sockets[user_id].discard(ws)
                if not self.sockets[user_id]:
                    del self.sockets[user_id]
            self.meta.pop(ws, None)

    def online_ids(self) -> list[str]:
        return list(self.sockets.keys())

    def is_online(self, user_id: str) -> bool:
        return bool(self.sockets.get(user_id))

    def register_device(self, ws: WebSocket, info: dict):
        """Called when a client identifies itself after connecting."""
        self.meta[ws] = {
            "device_id": str(info.get("device_id") or "")[:64],
            "platform": str(info.get("platform") or "")[:32],
            "app": str(info.get("app") or "web")[:32],
            "local_tools": bool(info.get("local_tools")),
            "since": dbx.now(),
        }

    def devices(self, user_id: str) -> list[dict]:
        return [dict(self.meta.get(ws, {"app": "unknown"}))
                for ws in self.sockets.get(user_id, set())]

    def local_tool_socket(self, user_id: str):
        """The socket that should run local tools for this user.

        Preference order:
          1. a client that registered local_tools=true (the desktop app), newest first
          2. failing that, a client that never registered at all

        Case 2 matters: registration is sent by the frontend, and an older or
        unpatched client never sends it. Treating "didn't register" as "can't do it"
        would silently disable local tools for every desktop user until they update.
        An unregistered client that genuinely cannot run tools simply answers
        no_desktop, which is exactly what happened before registration existed.

        A client that registered local_tools=false (a browser tab) is never chosen.
        """
        sockets = self.sockets.get(user_id, set())
        registered = [(self.meta[ws].get("since", 0), ws) for ws in sockets
                      if ws in self.meta and self.meta[ws].get("local_tools")]
        if registered:
            registered.sort(key=lambda pair: pair[0], reverse=True)
            return registered[0][1]
        unknown = [ws for ws in sockets if ws not in self.meta]
        return unknown[0] if unknown else None

    def local_tool_status(self, user_id: str) -> str:
        """Why local tools are or aren't available — used for diagnostics."""
        sockets = self.sockets.get(user_id, set())
        if not sockets:
            return "not connected"
        if any(ws in self.meta and self.meta[ws].get("local_tools") for ws in sockets):
            return "desktop app registered"
        if any(ws not in self.meta for ws in sockets):
            return ("a client is connected but never sent client_register — probably an "
                    "un-updated frontend. Trying it anyway.")
        return "only browser clients are connected"

    async def send_to(self, ws: WebSocket, payload: dict) -> bool:
        try:
            await ws.send_text(json.dumps(payload))
            return True
        except Exception:
            return False

    async def notify_users(self, user_ids: list[str], payload: dict):
        data = json.dumps(payload)
        for uid in set(user_ids):
            for ws in list(self.sockets.get(uid, ())):
                try:
                    await ws.send_text(data)
                except Exception:
                    pass

    async def broadcast_conversation(self, cid: str, payload: dict):
        await self.notify_users([m["id"] for m in dbx.conversation_members(cid)], payload)


hub = Hub()


async def handle_chat_message(user_id: str, data: dict):
    cid = data.get("conversation_id", "")
    content = (data.get("content") or "").strip()
    if not content or not dbx.is_member(cid, user_id):
        return
    msg = dbx.add_message_v5(cid, user_id, content, reply_to=(data.get("reply_to") or "")[:64])
    dbx.set_last_seen(user_id)
    msg = dbx.reply_previews([msg])[0]
    sender = dbx.get_user(user_id)
    await hub.broadcast_conversation(cid, {
        "type": "message", **msg,
        "sender_name": sender["name"], "sender_avatar": sender["avatar"],
        "sender_is_bot": sender["is_bot"],
    })
    conv = dbx.get_conversation(cid)
    if sender["is_bot"]:
        return  # bots never trigger other bots

    # ---- built-in Nova assistant ----
    if bot.should_reply(conv, content, BOT["id"]):
        if not dbx.is_member(cid, BOT["id"]):
            dbx.add_member(cid, BOT["id"])
        asyncio.create_task(reply_as(BOT["id"], bot.BOT_NAME, cid,
                                     lambda: bot.generate_reply(cid, BOT["id"])))
        return

    conv_agents = dbx.conversation_agents(cid)

    # ---- DM with a personal agent ----
    if conv["type"] == "dm" and conv_agents:
        agent = conv_agents[0]
        if dbx.has_agent_access(agent["id"], user_id):
            asyncio.create_task(reply_as(agent["user_id"], agent["name"], cid,
                                         lambda a=agent: agx.agent_reply(
                                             a, cid, tool_reporter(cid, a["user_id"], a["name"]),
                                             requester_id=user_id)))
        else:
            req = dbx.create_agent_request(agent["id"], user_id)
            requester = dbx.get_user(user_id)
            if req:
                await hub.notify_users([agent["owner_id"]], {
                    "type": "agent_request", "agent_id": agent["id"], "agent_name": agent["name"],
                    "requester_name": requester["name"], "request_id": req["id"]})
            m = dbx.add_message(cid, agent["user_id"],
                                f"🔒 {agent['name']} is private. I've sent an access request to its owner — "
                                "you'll be notified when they approve it.")
            await hub.broadcast_conversation(cid, {"type": "message", **m,
                "sender_name": agent["name"], "sender_avatar": "", "sender_is_bot": 1})
        return

    if not conv_agents:
        return

    # ---- @team orchestration in groups/channels ----
    if agx.is_team_task(content) and len(conv_agents) >= 2:
        task = agx.strip_team_mention(content) or content
        asyncio.create_task(run_team(conv_agents, task, cid, user_id))
        return

    # ---- @mention specific agents (any group member can ask any agent in the group) ----
    for agent in agx.mentioned_agents(content, conv_agents):
        asyncio.create_task(reply_as(agent["user_id"], agent["name"], cid,
                                     lambda a=agent: agx.agent_reply(
                                         a, cid, tool_reporter(cid, a["user_id"], a["name"]),
                                         requester_id=user_id)))


# conversation_id -> {bot_user_id: asyncio.Task}. A generation can run for a minute
# with tools, so whoever started it needs a way to call it off.
_agent_runs: dict[str, dict[str, asyncio.Task]] = {}


async def cancel_agent_runs(cid: str) -> int:
    """Stop every agent generating in this conversation.

    Cancelling unwinds the await inside the tool loop, which also aborts the
    in-flight HTTP request to Ollama — so this stops the work, not just the UI.
    """
    runs = dict(_agent_runs.get(cid) or {})
    for task in runs.values():
        task.cancel()
    return len(runs)


async def reply_as(bot_user_id: str, name: str, cid: str, gen):
    """Pulse a typing indicator while generating, then store and broadcast the reply."""
    # Registering the running task here rather than at each call site means every
    # path that starts an agent — DM, @mention, upload, scheduler — is stoppable
    # without changing any of them.
    _agent_runs.setdefault(cid, {})[bot_user_id] = asyncio.current_task()
    stop = asyncio.Event()
    typing_frame = {"type": "typing", "conversation_id": cid, "user_id": bot_user_id, "name": name}
    await hub.broadcast_conversation(cid, typing_frame)  # guaranteed first frame

    async def pulse():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except asyncio.TimeoutError:
                await hub.broadcast_conversation(cid, typing_frame)

    pulse_task = asyncio.create_task(pulse())
    cancelled = False
    try:
        reply = await gen()
    except asyncio.CancelledError:
        # Stopped deliberately. Post nothing — the person who pressed stop doesn't
        # need a message about it — but do clear the indicators.
        cancelled = True
    finally:
        stop.set()
        await pulse_task
        runs = _agent_runs.get(cid) or {}
        if runs.get(bot_user_id) is asyncio.current_task():
            runs.pop(bot_user_id, None)
        if not runs:
            _agent_runs.pop(cid, None)
        # Clear any lingering "using a tool" strip in the UI.
        await hub.broadcast_conversation(cid, {
            "type": "agent_tool", "conversation_id": cid, "user_id": bot_user_id,
            "name": name, "state": "end"})
        if cancelled:
            await hub.broadcast_conversation(cid, {
                "type": "agent_stopped", "conversation_id": cid,
                "user_id": bot_user_id, "name": name})
    if cancelled:
        return
    msg = dbx.add_message(cid, bot_user_id, reply)
    await hub.broadcast_conversation(cid, {
        "type": "message", **msg,
        "sender_name": name, "sender_avatar": "", "sender_is_bot": 1,
    })


def tool_reporter(cid: str, bot_user_id: str, name: str):
    """Callback handed to the agent runtime so the chat can show, live, which tool
    the agent is running right now."""
    async def on_tool(event: dict):
        await hub.broadcast_conversation(cid, {
            "type": "agent_tool", "conversation_id": cid, "user_id": bot_user_id,
            "name": name, **event})
    return on_tool


async def run_team(conv_agents: list[dict], task: str, cid: str,
                   requester_id: str = ""):
    lead = conv_agents[0]
    # Register like a single-agent run, otherwise a @team task is unstoppable —
    # it has its own pulse loop and never went through reply_as().
    _agent_runs.setdefault(cid, {})[lead["user_id"]] = asyncio.current_task()
    stop = asyncio.Event()
    typing_frame = {"type": "typing", "conversation_id": cid, "user_id": lead["user_id"],
                    "name": f"Agent team ({len(conv_agents)})"}
    await hub.broadcast_conversation(cid, typing_frame)

    async def pulse():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
            except asyncio.TimeoutError:
                await hub.broadcast_conversation(cid, typing_frame)

    pulse_task = asyncio.create_task(pulse())
    cancelled = False
    try:
        summary, zip_info, outcome = await agx.run_team_task(
            conv_agents, task, cid=cid, requester_id=requester_id)
    except asyncio.CancelledError:
        cancelled = True
        summary, zip_info, outcome = "", None, {"status": "cancelled"}
    except Exception as e:
        # Include the message. "failed: TypeError" is not a diagnosis, and it
        # cost a debugging cycle to find out which TypeError.
        summary = f"⚠️ Team task failed: {type(e).__name__}: {e}"
        zip_info = None
        outcome = {"status": "failed", "problems": [f"{type(e).__name__}: {e}"]}
    finally:
        stop.set()
        await pulse_task
        runs = _agent_runs.get(cid) or {}
        if runs.get(lead["user_id"]) is asyncio.current_task():
            runs.pop(lead["user_id"], None)
        if not runs:
            _agent_runs.pop(cid, None)
        if cancelled:
            await hub.broadcast_conversation(cid, {
                "type": "agent_stopped", "conversation_id": cid,
                "user_id": lead["user_id"], "name": lead["name"]})
    if cancelled:
        return
    msg = dbx.add_message(cid, lead["user_id"], summary)
    await hub.broadcast_conversation(cid, {"type": "message", **msg,
        "sender_name": lead["name"], "sender_avatar": "", "sender_is_bot": 1})
    if zip_info:
        url, fname, size = zip_info
        fmsg = dbx.add_message(cid, lead["user_id"], url, kind="file", file_name=fname, file_size=size)
        await hub.broadcast_conversation(cid, {"type": "message", **fmsg,
            "sender_name": lead["name"], "sender_avatar": "", "sender_is_bot": 1})


async def handle_reaction(user_id: str, data: dict):
    mid = data.get("message_id", "")
    emoji = (data.get("emoji") or "")[:8]
    msg = dbx.get_message(mid)
    if not msg or not emoji or not dbx.is_member(msg["conversation_id"], user_id):
        return
    dbx.toggle_reaction(mid, user_id, emoji)
    await hub.broadcast_conversation(msg["conversation_id"], {
        "type": "reaction", "conversation_id": msg["conversation_id"],
        "message_id": mid, "reactions": dbx.message_reactions(mid),
    })


async def handle_edit(user_id: str, data: dict):
    mid = data.get("message_id", "")
    content = (data.get("content") or "").strip()
    msg = dbx.get_message(mid)
    if not msg or not content:
        return
    if dbx.edit_message(mid, user_id, content):
        await hub.broadcast_conversation(msg["conversation_id"], {
            "type": "message_edit", "conversation_id": msg["conversation_id"],
            "message_id": mid, "content": content,
        })


async def handle_delete(user_id: str, data: dict):
    mid = data.get("message_id", "")
    msg = dbx.get_message(mid)
    if not msg:
        return
    if dbx.delete_message(mid, user_id):
        await hub.broadcast_conversation(msg["conversation_id"], {
            "type": "message_delete", "conversation_id": msg["conversation_id"], "message_id": mid,
        })


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    token = ws.query_params.get("token", "")
    try:
        user_id = auth.decode_token(token)
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    await hub.connect(user_id, ws)
    await hub.notify_users(hub.online_ids(), {"type": "presence", "online": hub.online_ids()})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = data.get("type")
            if mtype == "message":
                await handle_chat_message(user_id, data)
            elif mtype == "react":
                await handle_reaction(user_id, data)
            elif mtype == "edit":
                await handle_edit(user_id, data)
            elif mtype == "delete":
                await handle_delete(user_id, data)
            elif mtype == "agent_stop":
                stop_cid = data.get("conversation_id") or ""
                if not stop_cid:
                    print("[stop] request had no conversation_id", flush=True)
                elif not dbx.is_member(stop_cid, user_id):
                    # A silent rejection here was impossible to diagnose from the UI.
                    print(f"[stop] REFUSED: {user_id} is not a member of {stop_cid}", flush=True)
                else:
                    n = await cancel_agent_runs(stop_cid)
                    print(f"[stop] {user_id} cancelled {n} run(s) in {stop_cid}"
                          + ("" if n else "  <-- nothing was running"), flush=True)
            elif mtype == "client_register":
                hub.register_device(ws, data.get("info") or {})
            elif mtype == "local_fs_response":
                # Reply from this user's desktop app to a file request we sent it.
                resolve_local_fs(data.get("request_id", ""), data.get("result") or {})
            elif mtype == "typing":
                cid = data.get("conversation_id", "")
                if dbx.is_member(cid, user_id):
                    u = dbx.get_user(user_id)
                    await hub.broadcast_conversation(cid, {"type": "typing", "conversation_id": cid,
                                                           "user_id": user_id, "name": u["name"]})
            # -------- WebRTC signaling: relay to target user --------
            elif mtype in ("call-offer", "call-answer", "call-ice", "call-end",
                           "call-decline", "call-screen"):
                target = data.get("to")
                if target:
                    data["from"] = user_id
                    sender = dbx.get_user(user_id)
                    data["from_name"] = sender["name"]
                    data["from_avatar"] = sender["avatar"]
                    await hub.notify_users([target], data)
    except WebSocketDisconnect:
        pass
    finally:
        dbx.set_last_seen(user_id)
        await hub.disconnect(user_id, ws)
        await hub.notify_users(hub.online_ids(), {"type": "presence", "online": hub.online_ids()})


# ============================== Static frontend ==============================

# Mounted last so it only catches paths no API/WS route above already matched.
# html=True serves frontend/index.html automatically at "/".
@app.get("/admin")
async def admin_page():
    """The administration page, served on its own URL.

    Deliberately NOT reachable from the chat app: no link, no button, and it is
    not part of the single-page bundle. Knowing the URL is not access — every
    action behind it still requires an admin token, checked server-side.
    """
    page = FRONTEND / "admin.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="Admin page not installed")
    return FileResponse(page, media_type="text/html")


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="static")