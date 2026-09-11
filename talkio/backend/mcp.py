"""MCP connector routing.

The MCP client itself lives in the DESKTOP APP (frontend/mcp-client.js), not here.
This module only decides which connectors an agent has and asks the right person's
machine to talk to them.

Why the client is not on the server
-----------------------------------
An MCP connector is a personal account — someone's Notion, their Shopify store.
Holding those tokens server-side would put every user's credentials in one place,
reachable from a public URL. Instead:

  * the OAuth browser flow runs on the user's own PC, in their own browser
  * the access token is written to that machine's config and never transmitted
  * the HTTPS calls to the connector come from that machine

The backend sends only "list your tools" or "call this tool with these arguments"
over the existing WebSocket, and receives the result. Same bridge the local file
tools use, so multi-user routing and device selection are already solved.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

TOOL_CACHE_TTL = 300           # seconds; connectors rarely change their tool list
MAX_TOOLS_PER_SERVER = 40

# (user_id, url) -> {"at": epoch, "tools": [...]}
_cache: dict[tuple, dict] = {}

# main.py injects this: fn(user_id, op, args) -> dict, over the user's WebSocket.
_bridge = None


def set_bridge(fn):
    global _bridge
    _bridge = fn


async def _ask(user_id: str, op: str, args: dict) -> dict:
    if not user_id:
        return {"error": "no user to ask"}
    if _bridge is None:
        return {"error": "connector bridge not configured"}
    try:
        res = await _bridge(user_id, op, args)
    except TimeoutError:
        return {"error": "their computer didn't answer"}
    except Exception as e:
        return {"error": f"{type(e).__name__}"}
    return res if isinstance(res, dict) else {"error": "bad reply"}


def server_label(url: str) -> str:
    """A short, stable name for a connector, used in tool names and messages."""
    host = (urlparse(url).hostname or url).lower()
    host = re.sub(r"^(www|mcp|api|setup)\.", "", host)
    return re.sub(r"[^a-z0-9]+", "_", host.split(".")[0])[:24] or "mcp"


def tool_name(url: str, tool: str) -> str:
    return f"mcp_{server_label(url)}_{re.sub(r'[^a-zA-Z0-9_]', '_', tool)}"[:64]


def agent_connectors(agent: dict) -> list[str]:
    try:
        urls = json.loads(agent.get("mcp_connectors") or "[]")
    except json.JSONDecodeError:
        return []
    return [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]


async def list_tools(user_id: str, url: str, refresh: bool = False) -> dict:
    """{'tools': [...]} or {'needsAuth': True} or {'error': ...}."""
    key = (user_id, url)
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit["at"] < TOOL_CACHE_TTL:
        return {"tools": hit["tools"]}
    res = await _ask(user_id, "mcp_list", {"url": url})
    if res.get("tools") is not None:
        tools = res["tools"][:MAX_TOOLS_PER_SERVER]
        _cache[key] = {"at": time.time(), "tools": tools}
        return {"tools": tools}
    return res


async def tools_for_agent(agent: dict, requester_id: str) -> tuple[list[dict], dict, list]:
    """Ollama-shaped schemas for this agent's connectors, a routing map of
    {namespaced_name: (url, real_tool_name)}, and a list of (name, reason) for
    connectors that are configured but currently unusable.

    The third item matters: without it an agent whose connector failed says "I have
    no integrations at all", which is indistinguishable from having none configured
    and sends the person hunting for a problem in the wrong place.

    Connectors are asked one at a time rather than in parallel: they all share one
    WebSocket to one machine, and a burst of concurrent requests just queues there.
    A connector that fails is skipped so it can't cost the agent its other tools.
    """
    urls = agent_connectors(agent)
    if not urls or not requester_id:
        return [], {}, []

    schemas, routes, unavailable = [], {}, []
    for url in urls:
        res = await list_tools(requester_id, url)
        if res.get("needsAuth"):
            _needs_auth[(requester_id, url)] = res.get("wwwAuthenticate", "")
            unavailable.append((server_label(url), "not signed in yet"))
            continue
        if res.get("error"):
            reason = str(res["error"])[:160]
            if res.get("notMcp"):
                reason = "the saved URL is not an MCP endpoint — " + reason
            unavailable.append((server_label(url), reason))
            continue
        for t in res.get("tools") or []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            full = tool_name(url, t["name"])
            routes[full] = (url, t["name"])
            schemas.append({"type": "function", "function": {
                "name": full,
                "description": (f"[{server_label(url)}] "
                                + (t.get("description") or t["name"]))[:900],
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            }})
    return schemas, routes, unavailable


# (user_id, url) -> www-authenticate value, so connect_tool knows where to sign in
_needs_auth: dict[tuple, str] = {}


def pending_auth(user_id: str) -> list[str]:
    return [url for (uid, url) in _needs_auth if uid == user_id]


async def run_agent_tool(routes: dict, name: str, args: dict, requester_id: str) -> str:
    route = routes.get(name)
    if not route:
        return f"(unknown connector tool: {name})"
    url, real = route
    res = await _ask(requester_id, "mcp_call", {"url": url, "name": real, "args": args or {}})
    if res.get("needsAuth"):
        _needs_auth[(requester_id, url)] = res.get("wwwAuthenticate", "")
        return (f"TELL THE USER, addressing them as 'you': the {server_label(url)} connector "
                "needs you to sign in first. Call connect_connector to start it — their "
                "browser will open so they can sign in on their own computer. Do NOT retry "
                "this tool until that is done.")
    if res.get("notMcp"):
        return (f"TELL THE USER, addressing them as 'you': the URL saved for "
                f"{server_label(url)} is not an MCP endpoint — {res.get('error')} "
                "Ask them to remove or correct that connector. Do NOT retry, and do NOT "
                "offer to sign in.")
    if res.get("forbidden"):
        return (f"TELL THE USER, addressing them as 'you': the {server_label(url)} server "
                "refuses connections from this app — it only allows certain approved MCP "
                "clients, and signing in will not change that. Do NOT retry, and do NOT "
                "suggest they sign in again.")
    if res.get("error"):
        return f"The {server_label(url)} connector failed: {res['error']}. Do NOT retry."
    return str(res.get("text") or "(the connector returned nothing)")


def unavailable_note(unavailable: list) -> str:
    """A sentence for the system prompt about connectors that exist but aren't
    working, so the agent can name them instead of denying they exist."""
    if not unavailable:
        return ""
    parts = "; ".join(f"{name} ({reason})" for name, reason in unavailable)
    return ("\n\nCONNECTORS CONFIGURED BUT NOT USABLE RIGHT NOW: " + parts + ". "
            "If you are asked about one of these, say it is set up but currently "
            "unavailable and give that reason — do NOT say you have no integrations "
            "or that the service doesn't exist. If the reason is that it isn't signed "
            "in, offer to run connect_connector.")

CONNECT_TOOL = {
    "type": "function",
    "function": {
        "name": "connect_connector",
        "description": ("Start sign-in for a connector that needs authorization. The "
                        "person's own browser opens on their computer so they sign in to "
                        "their own account; the token is stored on their machine, never on "
                        "the server. Use it when a connector tool says it needs sign-in, or "
                        "when they ask to connect a service."),
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string",
                                       "description": "Connector name or URL, e.g. notion."}},
            "required": ["service"],
        },
    },
}


async def connect(agent: dict, requester_id: str, service: str) -> str:
    urls = agent_connectors(agent)
    want = (service or "").strip().lower()
    target = next((u for u in urls if want and (want in u.lower()
                                                or want == server_label(u))), None)
    if not target and len(urls) == 1:
        target = urls[0]
    if not target:
        names = ", ".join(server_label(u) for u in urls) or "none"
        return (f"There is no connector called '{service}' on this agent. "
                f"Connectors set up here: {names}.")

    res = await _ask(requester_id, "mcp_auth",
                     {"url": target, "wwwAuthenticate": _needs_auth.get((requester_id, target), "")})
    if res.get("error") == "denied":
        return "They cancelled the sign-in. Accept that and do NOT ask again."
    if res.get("error"):
        return f"Sign-in for {server_label(target)} failed: {res['error']}"
    _needs_auth.pop((requester_id, target), None)
    _cache.pop((requester_id, target), None)     # re-discover with the new token
    return str(res.get("text") or f"Connected to {server_label(target)}.")


async def status(user_id: str) -> dict:
    return await _ask(user_id, "mcp_status", {})


async def logout(user_id: str, url: str) -> dict:
    _cache.pop((user_id, url), None)
    return await _ask(user_id, "mcp_logout", {"url": url})


async def probe(user_id: str, url: str) -> dict:
    """Used by the UI 'Test' button."""
    res = await list_tools(user_id, url, refresh=True)
    if res.get("needsAuth"):
        return {"ok": False, "server": server_label(url), "needs_auth": True,
                "error": "needs sign-in — connect it from the desktop app"}
    if res.get("error"):
        return {"ok": False, "server": server_label(url), "needs_auth": False,
                "forbidden": bool(res.get("forbidden")),
                "not_mcp": bool(res.get("notMcp")), "error": res["error"]}
    tools = res.get("tools") or []
    return {"ok": True, "server": server_label(url), "tool_count": len(tools),
            "tools": [t.get("name") for t in tools][:40]}