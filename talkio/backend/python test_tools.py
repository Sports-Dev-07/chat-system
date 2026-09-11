"""Tests for the agent tool layer in agents.py. Run: python test_tools.py"""
import asyncio, base64, json, sys, tempfile
from pathlib import Path

import database as dbx
TMP = Path(tempfile.mkdtemp())
dbx.DB_PATH = TMP / "tools.db"
for f in (dbx.init_db, dbx.init_agent_tables, dbx.init_group_tables, dbx.init_v5): f()

import agents as agx
UP = TMP / "uploads"; UP.mkdir()
agx.UPLOADS = UP                       # keep test files out of the real uploads dir

P = F = 0
def check(n, c, x=""):
    global P, F
    if c: P += 1; print("PASS ", n)
    else: F += 1; print("FAIL ", n, " ->", x)

run = lambda co: asyncio.get_event_loop().run_until_complete(co)

# ---- fixtures ----
owner = dbx.upsert_user("o@x.de", "Praveen")
bu = dbx.upsert_user("a@agents.talkio.local", "Teddy", is_bot=True)
agent = dbx.create_agent(owner["id"], bu["id"], "Teddy", "teddy", "", "", "qwen3",
                         provider="ollama", ollama_url="http://localhost:11434",
                         capabilities=json.dumps({"web_scraping": True}))
plain = dbx.create_agent(owner["id"], bu["id"], "Plain", "plain", "", "", "qwen3",
                         provider="ollama", ollama_url="http://localhost:11434",
                         capabilities="{}")
conv = dbx.create_conversation("dm", "", owner["id"], [bu["id"]])
empty_conv = dbx.create_conversation("dm", "", owner["id"], [])

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
(UP / "shot.png").write_bytes(PNG)
(UP / "notes.txt").write_text("Line one about pricing\nThe sTread Pro costs 899 EUR\nUnrelated line\n" + "filler line of no interest\n" * 800)
(UP / "data.bin").write_bytes(b"\x00\x01\x02")

dbx.add_message(conv["id"], owner["id"], "hello there")
dbx.add_message(conv["id"], owner["id"], "/files/notes.txt", kind="file", file_name="notes.txt", file_size=99)
dbx.add_message(conv["id"], owner["id"], "/files/shot.png", kind="image", file_name="shot.png", file_size=len(PNG))

print("=== attachment index ===")
idx = agx.attachment_index(conv["id"])
check("image indexed as img1", idx.get("img1", {}).get("file_name") == "shot.png", idx.keys())
check("file indexed as doc1", idx.get("doc1", {}).get("file_name") == "notes.txt")
check("text messages not indexed", len(idx) == 2, list(idx))
check("empty conversation has no attachments", agx.attachment_index(empty_conv["id"]) == {})

print("\n=== toolset assembly ===")
names = lambda ts: [t["function"]["name"] for t in ts]
base = names(agx.tools_for(plain, empty_conv["id"]))
check("base tools always present",
      set(base) == {"calculate", "current_time", "search_chat"}, base)
check("no web tools without the capability", "web_search" not in base)
web = names(agx.tools_for(agent, empty_conv["id"]))
check("web tools follow the capability flag", "web_search" in web and "fetch_url" in web)
check("no image tool when the chat has no images", "analyze_image" not in web)
full = names(agx.tools_for(agent, conv["id"]))
check("image tool appears when an image exists", "analyze_image" in full, full)
check("document tool appears when a file exists", "read_document" in full)
check("every tool has a description the model can act on",
      all(t["function"].get("description") for t in agx.tools_for(agent, conv["id"])))

print("\n=== calculate ===")
check("arithmetic", agx._do_calculate("(1299 - 899) / 1299 * 100").startswith("(1299 - 899)"))
check("correct result", "30.79" in agx._do_calculate("round((1299-899)/1299*100, 2)"),
      agx._do_calculate("round((1299-899)/1299*100, 2)"))
check("integer math", agx._do_calculate("2**10") == "2**10 = 1024")
check("divide by zero handled", "zero" in agx._do_calculate("1/0").lower())
check("refuses code injection", "won't evaluate" in agx._do_calculate("__import__('os').system('ls')"))
check("refuses attribute access", "won't evaluate" in agx._do_calculate("().__class__"))
check("refuses names", "won't evaluate" in agx._do_calculate("open('x')"))
check("empty handled", "No expression" in agx._do_calculate(""))

print("\n=== current_time ===")
t = agx._do_current_time("Asia/Kolkata")
check("returns a date", "Current date and time" in t, t)
check("honours the timezone when tzdata exists", "Asia/Kolkata" in t or "UTC" in t, t)
check("bad timezone falls back rather than raising", "UTC" in agx._do_current_time("Not/AZone"))

print("\n=== search_chat ===")
r = agx._do_search_chat(conv["id"], "hello")
check("finds an earlier message", "hello there" in r, r)
check("reports a miss clearly", "Nothing earlier" in agx._do_search_chat(conv["id"], "zzzz"))
check("empty query handled", "No search term" in agx._do_search_chat(conv["id"], ""))

print("\n=== read_document ===")
r = run(agx._do_read_document(conv["id"], "doc1"))
check("reads a text file", "Line one about pricing" in r, r[:80])
check("truncates long files", "truncated" in r)
r = run(agx._do_read_document(conv["id"], "doc1", query="sTread"))
check("query returns only matching parts", "899 EUR" in r and "matching" in r, r[:100])
check("resolves by filename", "pricing" in run(agx._do_read_document(conv["id"], "notes.txt")))
check("resolves a bare number", "pricing" in run(agx._do_read_document(conv["id"], "1")))
check("missing file reported", "No file found" in run(agx._do_read_document(empty_conv["id"], "doc1")))
dbx.add_message(conv["id"], owner["id"], "/files/data.bin", kind="file", file_name="data.bin", file_size=3)
check("binary file refused politely", "can't read as text" in run(agx._do_read_document(conv["id"], "doc1")),
      run(agx._do_read_document(conv["id"], "doc1"))[:90])
dbx.add_message(conv["id"], owner["id"], "/files/gone.txt", kind="file", file_name="gone.txt", file_size=1)
check("missing file on disk reported", "missing from the server" in run(agx._do_read_document(conv["id"], "doc1")))

print("\n=== path safety ===")
check("cannot escape the uploads dir", agx._local_path("/files/../../database.py") is None,
      agx._local_path("/files/../../database.py"))
check("cannot use an absolute path", agx._local_path("/etc/passwd") is None)
check("legit file resolves", agx._local_path("/files/shot.png") is not None)

print("\n=== analyze_image ===")
calls = []
class FakeResp:
    def __init__(self, data, code=200): self._d, self.status_code = data, code
    def json(self): return self._d
    def raise_for_status(self): pass
class FakeClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url):
        return FakeResp({"models": [{"name": "qwen3:8b"}, {"name": "llava:7b"}]})
    async def post(self, url, json=None):
        calls.append(json)
        return FakeResp({"message": {"content": "A black treadmill, brand sTread Pro, model X7."}})
import httpx as _httpx
_realclient = _httpx.AsyncClient
_httpx.AsyncClient = FakeClient

r = run(agx._do_analyze_image(agent, conv["id"], "img1", "what product is this?"))
check("returns the vision model's description", "sTread Pro" in r, r[:90])
check("names which model read it", "llava" in r, r[:90])
check("nudges the model to search next", "web_search" in r)
check("image was sent as base64", bool(calls and calls[-1]["messages"][0].get("images")))
check("auto-picked an installed vision model", calls[-1]["model"].startswith("llava"))
check("the question was passed through", "what product is this?" in calls[-1]["messages"][0]["content"])
check("no image in the chat is reported clearly",
      "No image found" in run(agx._do_analyze_image(agent, empty_conv["id"], "img1", "?")))

class NoVision(FakeClient):
    async def get(self, url): return FakeResp({"models": [{"name": "qwen3:8b"}]})
_httpx.AsyncClient = NoVision
r = run(agx._do_analyze_image(agent, conv["id"], "img1", "?"))
check("missing vision model gives an actionable message",
      "ollama pull" in r and "vision" in r.lower(), r[:110])
_httpx.AsyncClient = FakeClient

print("\n=== dispatch + context isolation ===")
ctx = {"agent": agent, "cid": conv["id"]}
check("dispatch routes calculate", "= 4" in run(agx._run_web_tool("calculate", {"expression": "2+2"}, ctx)))
check("dispatch routes current_time", "Current date" in run(agx._run_web_tool("current_time", {}, ctx)))
check("dispatch routes read_document",
      "pricing" in run(agx._run_web_tool("read_document", {"doc_id": "notes.txt"}, ctx)))
check("dispatch routes analyze_image",
      "sTread" in run(agx._run_web_tool("analyze_image", {"image_id": "img1", "question": "?"}, ctx)))
check("unknown tool handled", "unknown tool" in run(agx._run_web_tool("nope", {}, ctx)))
check("chat tools refuse without a conversation",
      "No conversation context" in run(agx._run_web_tool("analyze_image", {"image_id": "img1"}, {})))

print("\n=== history exposes attachments ===")
h = agx.build_history(conv["id"], bu["id"])
blob = " ".join(m["content"] for m in h)
check("image appears in the transcript", "image attached: shot.png" in blob, blob[:200])
check("with its tool id", "refer to it as img1" in blob)
check("file appears too", "file attached: notes.txt" in blob)
check("plain text still present", "hello there" in blob)

print("\n=== system prompt ===")
p_media = agx.agent_system_prompt(agent, "Praveen", has_web=True, has_media=True)
check("tells the model to use analyze_image", "analyze_image" in p_media)
check("forbids claiming it cannot see", "never claim you cannot see" in p_media.lower())
check("chains image -> web", "then web_search" in p_media)
p_plain = agx.agent_system_prompt(plain, "Praveen", has_web=False, has_media=False)
check("no media instructions when there are none", "analyze_image" not in p_plain)
check("always mentions calculate", "calculate" in p_plain)

print("\n=== tool labels for the UI ===")
check("web_search label", agx.tool_label("web_search") == "Searching the web")
check("analyze_image label", agx.tool_label("analyze_image") == "Looking at the image")
check("unknown tool still gets a label", agx.tool_label("weird_thing") == "Weird thing")
check("detail prefers the query", agx.tool_detail("web_search", {"query": "sTread price"}) == "sTread price")
check("detail falls back across keys", agx.tool_detail("calculate", {"expression": "2+2"}) == "2+2")
check("detail is safe when empty", agx.tool_detail("current_time", {}) == "")
check("detail tolerates non-dict args", agx.tool_detail("web_search", None) == "")

print("\n=== tool loop emits UI events ===")
events = []
async def on_tool(e): events.append(e)

rounds = [
    {"message": {"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "analyze_image",
                                              "arguments": {"image_id": "img1", "question": "what is it"}}}]}},
    {"message": {"role": "assistant", "content": "",
                 "tool_calls": [{"function": {"name": "web_search",
                                              "arguments": {"query": "sTread Pro X7 price"}}}]}},
    {"message": {"role": "assistant", "content": "It's an sTread Pro X7; current price is 899 EUR."}},
]
state = {"i": 0}
class LoopClient(FakeClient):
    async def get(self, url): return FakeResp({"models": [{"name": "qwen3"}, {"name": "llava"}]})
    async def post(self, url, json=None):
        if url.endswith("/api/chat") and json.get("messages", [{}])[0].get("images"):
            return FakeResp({"message": {"content": "An sTread Pro X7 treadmill."}})
        r = rounds[min(state["i"], len(rounds) - 1)]; state["i"] += 1
        return FakeResp(r)
_httpx.AsyncClient = LoopClient
async def fake_search(q, max_results=5): return f"Search results for {q}: 899 EUR"
agx._do_web_search = fake_search

out = run(agx.call_ollama("http://localhost:11434", "qwen3", "sys",
                          [{"role": "user", "content": "what is in the picture and what does it cost"}],
                          tools=agx.tools_for(agent, conv["id"]),
                          on_tool=on_tool, ctx={"agent": agent, "cid": conv["id"]}))
check("final answer returned", "899 EUR" in out, out[:90])
starts = [e for e in events if e["state"] == "start"]
dones = [e for e in events if e["state"] == "done"]
check("emitted a start per tool call", len(starts) == 2, [e["tool"] for e in starts])
check("emitted a done per tool call", len(dones) == 2)
check("first event is the image tool", starts[0]["tool"] == "analyze_image", starts[0])
check("then the web search", starts[1]["tool"] == "web_search")
check("events carry a human label", starts[0]["label"] == "Looking at the image")
check("events carry the arguments", starts[1]["detail"] == "sTread Pro X7 price")
check("start precedes done for each tool",
      events.index(starts[1]) > events.index(dones[0]))

print("\n=== a broken indicator never breaks the reply ===")
async def bad_on_tool(e): raise RuntimeError("UI exploded")
state["i"] = 0; events.clear()
out = run(agx.call_ollama("http://localhost:11434", "qwen3", "sys",
                          [{"role": "user", "content": "again"}],
                          tools=agx.tools_for(agent, conv["id"]),
                          on_tool=bad_on_tool, ctx={"agent": agent, "cid": conv["id"]}))
check("reply still produced when the callback throws", "899 EUR" in out, out[:90])

_httpx.AsyncClient = _realclient
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)