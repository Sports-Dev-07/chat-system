"""Reproduces the reported bug: POST /schedules on a box with no tz database.
Before the fix this raised ZoneInfoNotFoundError -> 500 -> browser 'Failed to fetch'.
"""
import sys, tempfile, zoneinfo
from pathlib import Path

# Simulate Windows without the `tzdata` package: EVERY lookup fails, including UTC.
_real = zoneinfo.ZoneInfo
class Boom(_real):
    def __new__(cls, key): raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")
zoneinfo.ZoneInfo = Boom

import database as dbx
dbx.DB_PATH = Path(tempfile.mkdtemp()) / "notz.db"
for f in (dbx.init_db, dbx.init_agent_tables, dbx.init_group_tables, dbx.init_v5): f()

import agents as agx
async def fake(agent, system, messages, max_tokens=1500): return "output"
agx.call_model = fake

import auth, scheduler as sch
from fastapi import FastAPI
from fastapi.testclient import TestClient

sch.init_scheduler_tables()
async def deliver(cid, agent, text): return dbx.add_message(cid, agent["user_id"], text)
sch.set_deliver(deliver)
app = FastAPI(); app.include_router(sch.router)
c = TestClient(app, raise_server_exceptions=False)

owner = dbx.upsert_user("o@x.de", "Owner")
bu = dbx.upsert_user("b@agents.talkio.local", "Teddy", is_bot=True)
ag = dbx.create_agent(owner["id"], bu["id"], "Teddy", "teddy", "", "", "m")
grp = dbx.create_conversation("group", "teddy", owner["id"], [])
auth.version_checker = lambda uid, tv: True
H = {"Authorization": "Bearer " + auth.make_token(owner["id"], 0)}

P = F = 0
def check(n, cond, x=""):
    global P, F
    if cond: P += 1; print("PASS ", n)
    else: F += 1; print("FAIL ", n, " ->", x)

print("=== the exact reported request ===")
body = {"prompt": "provide current ai news", "time": "17:48", "tz": "Asia/Calcutta",
        "target": "conversation", "conversation_id": grp["id"], "title": ""}
r = c.post(f"/api/agents/{ag['id']}/schedules", json=body, headers=H)
check("POST no longer 500s without tzdata", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
s = r.json()
check("schedule created", "id" in s)
check("falls back to UTC in the label", "UTC" in s["human"], s["human"])
check("still armed with a real timestamp", s["next_run_at"] > 0)
check("next run renders", s["next_run_local"] != "—", s["next_run_local"])

print("\n=== the rest of the flow still works ===")
sid = s["id"]
check("list", c.get(f"/api/agents/{ag['id']}/schedules", headers=H).status_code == 200)
check("toggle", c.post(f"/api/schedules/{sid}/toggle", json={"enabled": False}, headers=H).status_code == 200)
check("resume", c.post(f"/api/schedules/{sid}/toggle", json={"enabled": True}, headers=H).status_code == 200)
r = c.put(f"/api/schedules/{sid}", json={**body, "time": "06:00"}, headers=H)
check("edit", r.status_code == 200, r.text[:120])
r = c.post(f"/api/schedules/{sid}/run", headers=H)
check("run now", r.status_code == 200, r.text[:120])
check("message posted", dbx.get_messages(grp["id"])[-1]["content"] == "output")
check("runs list", c.get(f"/api/schedules/{sid}/runs", headers=H).status_code == 200)
check("tick works", isinstance(__import__("asyncio").get_event_loop().run_until_complete(sch.tick()), int))
check("delete", c.delete(f"/api/schedules/{sid}", headers=H).status_code == 200)

print("\n=== unexpected errors now arrive as JSON, not a CORS black hole ===")
_orig = sch.create_schedule
def explode(*a, **k): raise RuntimeError("simulated inner failure")
sch.create_schedule = explode
r = c.post(f"/api/agents/{ag['id']}/schedules", json=body, headers=H)
check("returns a proper 500 response", r.status_code == 500, r.status_code)
check("with a readable reason the UI can print",
      "simulated inner failure" in r.text, r.text[:160])
sch.create_schedule = _orig

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)