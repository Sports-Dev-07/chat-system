"""Personal AI agents runtime for Sportstech.

Each user creates agents with their own Claude API key. Agents chat as bot users.
- api_key starting with "test" -> deterministic echo/simulation mode (no external calls),
  so flows can be tried without a real key.
- Team orchestration: "@team <task>" in a group with 2+ agents splits the task,
  runs agents in parallel, and posts a combined answer + a ZIP of each agent's output.

WEB SCRAPING ON LOCAL (OLLAMA) MODELS:
  Ollama models cannot browse the web on their own, and there is no server-side
  web_search tool for them. To make "web scraping" work locally we give the model
  two function tools (web_search, fetch_url), and this file ACTUALLY EXECUTES those
  calls (via DuckDuckGo + httpx) and feeds the results back so the model can answer
  from real data instead of guessing. Requires a tool-capable model (llama3.1/3.2,
  qwen2.5, mistral-nemo, ...). Optional: `pip install ddgs` for better search.
"""
import asyncio
import json
import os
import re
import time
import zipfile
from pathlib import Path

import database as dbx
import mcp

UPLOADS = Path(__file__).parent / "uploads"
MAX_HISTORY = 20
TEAM_MENTIONS = ("@team", "@agents")


def is_echo_key(key: str) -> bool:
    return key.strip().lower().startswith("test")


# ---------------------------------------------------------------- capabilities
#
# The five flags the agent forms actually send. Named here so tool discovery and
# tool execution agree on their meaning — previously discovery decided, and
# execution trusted whatever arrived.
#
#   tool_calling  - may call ANY tool at all. Off means conversation only.
#   multi_agent   - may delegate to another agent.
#   web_scraping  - may reach the internet (web_search, fetch_url).
#   files         - may read/write the shared workspace folder on the server.
#   local_files   - may reach the requester's own machine.
#
# Legacy agents predate some of these. The migration policy is written down
# rather than implied: absent flags default to the values below, which match how
# those agents behaved before enforcement existed, so nothing silently breaks —
# EXCEPT local_files, which defaults OFF because reaching someone's disk is not
# something to grant by omission.
CAP_DEFAULTS = {
    "tool_calling": True,
    "multi_agent": True,
    "web_scraping": False,
    "files": False,
    "local_files": False,
}

# Which capability each tool needs. Anything absent needs only tool_calling.
TOOL_CAPABILITY = {
    "web_search": "web_scraping", "fetch_url": "web_scraping",
    "list_files": "files", "read_file": "files", "write_file": "files",
    "share_file_in_chat": "files",
    "list_my_files": "local_files", "read_my_file": "local_files",
    "create_my_file": "local_files", "search_my_files": "local_files",
    "open_on_my_computer": "local_files", "make_folder_on_my_computer": "local_files",
    "move_on_my_computer": "local_files", "delete_on_my_computer": "local_files",
    "my_computer_info": "local_files", "open_app_on_my_computer": "local_files",
    "list_my_apps": "local_files", "open_url_on_my_computer": "local_files",
    "ask_agent": "multi_agent", "delegate_task": "multi_agent",
}


def has_cap(agent: dict, name: str) -> bool:
    """One place that decides whether an agent may do something."""
    caps = agent_caps(agent)
    if name in caps:
        return bool(caps[name])
    return CAP_DEFAULTS.get(name, False)


def tool_allowed(agent: dict, tool_name: str) -> tuple[bool, str]:
    """(allowed, reason). Used by discovery AND by the executor, so a model that
    invents a tool name or repeats one from an earlier turn cannot run it."""
    if not has_cap(agent, "tool_calling"):
        return False, "tool calling is switched off for this agent"
    needed = TOOL_CAPABILITY.get(tool_name)
    if needed and not has_cap(agent, needed):
        return False, f"the '{needed.replace('_', ' ')}' capability is switched off"
    return True, ""


def agent_caps(agent: dict) -> dict:
    """capabilities may arrive as a dict (from the API layer / _mask) OR as a raw JSON
    string straight from the DB (the WebSocket path). Normalise to a dict either way."""
    caps = agent.get("capabilities")
    if isinstance(caps, str):
        try:
            caps = json.loads(caps or "{}")
        except Exception:
            caps = {}
    return caps if isinstance(caps, dict) else {}


def agent_mcp(agent: dict) -> list:
    """Same tolerance for mcp_connectors: dict/list from API, JSON string from DB."""
    mcp = agent.get("mcp_connectors")
    if isinstance(mcp, str):
        try:
            mcp = json.loads(mcp or "[]")
        except Exception:
            mcp = []
    return mcp if isinstance(mcp, list) else []


def agent_is_echo(agent: dict) -> bool:
    """Echo/simulation mode: Claude key 'test...' or Ollama URL 'test...'."""
    if agent.get("provider", "claude") == "ollama":
        return is_echo_key(agent.get("ollama_url", ""))
    return is_echo_key(agent.get("api_key", ""))


# Type-specific behaviour, appended to the shared base. Kept short and concrete:
# a long persona dilutes the operational rules that follow it.
TYPE_PROMPTS = {
    "product": (
        "\n\nYOU ARE A PRODUCT AGENT. Everything you say is about your configured "
        "product. Prefer the official product page and approved references over "
        "general web results, and check a current source when the question turns on "
        "a fact that changes — price, stock, specification, availability. Separate "
        "what you VERIFIED from what you are suggesting, and say which is which. If "
        "two sources disagree on a specification, say so and give both rather than "
        "picking one. Delegate specialist work when you are allowed to."),
    "create": (
        "\n\nYOU ARE A COORDINATOR. For anything with several parts: decide the "
        "steps, pick ONLY the agents whose capabilities the work actually needs, and "
        "respect order — do not start something that depends on a result you do not "
        "have yet. When work comes back, check it against what you asked for before "
        "reporting it done. Report real progress: what is finished, what is running, "
        "what is blocked and why. Never report a promise as a deliverable."),
    "pet": (
        "\n\nYOU ARE A PERSONAL ASSISTANT. Be brief. Drafts, summaries, reminders "
        "and preparation are your work. Use what you have remembered about this "
        "person, and keep it to yourself — it is theirs, not something to repeat to "
        "other agents. Delegate only when asked to, or when the task plainly needs "
        "a specialist you have."),
}


def agent_system_prompt(agent: dict, owner_name: str, has_web: bool = False,
                        has_media: bool = False) -> str:
    desc = agent.get("instruction") or agent.get("description") or "a helpful general-purpose assistant"
    prompt = (
        f"You are {agent['name']}, a personal AI agent in the Sportstech chat app, "
        f"created by {owner_name}. Your role: {desc}. "
        "You respond on behalf of your owner when people in the chat ask you things. "
        "Be helpful, concrete, and complete the task you are asked to do. "
        "Keep casual replies short; give full detail when a task requires it."
        "\n\nUse a tool when the answer depends on something you cannot know: "
        "calculate for arithmetic, current_time before reasoning about 'today' or "
        "'latest', web_search for anything that changes, and the relevant tool for "
        "anything in a file or a connected service. Do NOT reach for a tool to "
        "answer a greeting, an opinion, or something you plainly already know — "
        "that wastes the person's time. When you do use one, prefer it to guessing."
        "\n\nNEVER describe your own abilities from memory — call list_my_tools and "
        "answer from what it returns. It shows the exact tools each connector exposes, "
        "which is usually narrower than the service as a whole."
        "\n\nBEFORE YOU DECLINE, CHECK YOUR OTHER TOOLS. A connector often covers only "
        "part of a service: a LinkedIn connector may handle ads but not job search, a "
        "Shopify one may read orders but not analytics. When the obvious tool can't do "
        "what was asked, ask yourself what the person actually wants and whether another "
        "tool gets them there — web_search and fetch_url can answer a great many "
        "questions a connector cannot. Do that FIRST, and say which route you took. "
        "Offering to open a website so they can look it up themselves is a last resort, "
        "not a first answer: it hands the work back to them."
        "\n\nBE HONEST ABOUT WHAT YOU CAN DO. Your ONLY abilities are: replying in "
        "this conversation, and calling the tools listed for this turn. You cannot "
        "moderate or delete messages, cannot filter or block anyone, cannot 'take "
        "over' or control the chat, cannot act while nobody is talking to you, and "
        "cannot message anyone except through a tool. If you are asked to do "
        "something outside that list, say plainly that you cannot instead of "
        "agreeing and then doing nothing — agreeing to a job you cannot perform is "
        "worse than refusing it."
        "\n\nYou run on a server. You cannot see anyone's screen or install anything. "
        "File access, if you have it, is limited: list_files/read_file reach one shared "
        "folder on the server, and list_my_files/read_my_file reach only the single "
        "folder the person you're talking to has explicitly shared from their own "
        "computer through the desktop app. Inside their user folder you CAN list, search "
        "and read files, create text files and folders, move or rename things, delete "
        "things, and open a file or folder on their screen. You can also report their OS, "
        "CPU, memory and free disk space. Nothing outside the shared folder is visible to "
        "you. You CAN start applications they already have installed (open_app_on_my_computer) "
        "and open web pages in their browser, but you cannot run arbitrary commands or "
        "install anything."
        "\n\nDeleting and moving are irreversible and always prompt the person for "
        "confirmation. Never call them unless they asked for that specific thing; do not "
        "tidy up, reorganise or clean anything on your own initiative."
        "\n\nWhen someone asks anything about THEIR files, folders, disk or drive, your "
        "FIRST action is to call list_my_files with the folder they named (downloads, "
        "documents, desktop…). There is NO setup step and nothing for them to share "
        "first — never tell them to share a folder. The tool's answer is authoritative, "
        "and its EXACT COUNT line is the number to quote. Only describe a limitation after "
        "a tool has actually reported one. Requests like 'open my disk', 'show me my "
        "files', 'what's on my computer' all mean: call list_my_files now. If they want "
        "something genuinely impossible, such as a live view of their screen, say so — "
        "but still show them the file list you CAN produce."
        "\n\nTo reach anyone outside this conversation you MUST use a tool: ask_agent "
        "for another agent, message_user for a person. Writing a greeting or a question "
        "into this chat does not send it to them — only the people in this conversation "
        "can see it. You can only message people your owner already talks to, and only "
        "inside this app; you have no email, SMS or WhatsApp access."
    )

    # A product agent is pinned to one product. Naming it (and its page) stops the
    # model answering about a similarly-named product it half-remembers.
    product = (agent.get("product_name") or "").strip()
    product_url = (agent.get("product_url") or "").strip()
    if product or product_url:
        prompt += "\n\nTHIS AGENT COVERS ONE SPECIFIC PRODUCT."
        if product:
            prompt += (f" It is: {product}. When someone says 'the product', 'it', or asks "
                       f"a bare question about specs or price, they mean {product} — never a "
                       "different model in the same range.")
        if product_url:
            prompt += (f" Its official page is {product_url} — treat that page as the "
                       "authoritative source. Call fetch_url on it FIRST for any question "
                       "about specs, dimensions, weight limits, features or price, before "
                       "searching the wider web, and prefer what it says over anything else.")
        prompt += (" If a question is clearly about a different product, say so rather than "
                   "answering as if it were this one.")
    # Type behaviour goes before the shared tail, so the tail's generic advice
    # can't override a type-specific rule (e.g. the product agent's "official
    # page first" was being contradicted by the tail's "search first").
    prompt += TYPE_PROMPTS.get(agent.get("type") or "", "")
    return prompt + _tail_prompt(has_web, has_media)


def _tail_prompt(has_web: bool, has_media: bool) -> str:
    prompt = ""
    if has_media:
        prompt += (
            "\n\nIMAGES AND FILES — attachments in this chat are listed in the transcript "
            "with an id such as img1 or doc1 (img1 is the most recent image). You CANNOT "
            "see them directly. To answer anything about a picture you MUST call "
            "analyze_image with its id; to answer anything about a file you MUST call "
            "read_document. Never describe or summarise an attachment you have not read "
            "with a tool, and never claim you cannot see images — use the tool instead."
            "\n\nA filename is NOT evidence. Upload filenames are random artefacts, so "
            "never infer a product, brand, or model number from one, and never search "
            "the web for a term you pulled out of a filename. If analyze_image fails, "
            "say so honestly and ask the user to read out the label on the product — do "
            "not produce a guess dressed up as an answer."
        )
    if has_web and has_media:
        prompt += (
            "\n\nCombine them: analyze_image first to identify what is in the picture, "
            "then web_search using the specific names, brands or model numbers it reports, "
            "then fetch_url to read the best result. The image tells you WHAT to look up; "
            "the web tells you the current facts about it."
        )
    if has_web:
        prompt += (
            "\n\nIMPORTANT — you have web tools (web_search and fetch_url). "
            "You do NOT already know product specs, prices, or current facts, and your "
            "internal guesses about them are usually wrong. NEVER state a spec, price, "
            "dimension, weight, date, or price from memory. Look it up, and base the "
            "answer on what the tool returned. If you have an authoritative source for "
            "this subject — an official product page, a connected system — go there "
            "FIRST and only search the wider web if it doesn't answer. If nothing "
            "useful comes back, say you couldn't verify it rather than guessing. "
            "This is about facts that can be checked, not about ordinary conversation: "
            "do not search to answer a greeting or to explain something you know."
        )
    return prompt


def build_tools_and_mcp(agent: dict) -> tuple[list[dict], list[dict]]:
    """Turn an agent's capability flags + mcp_connectors chips into real Claude API params."""
    caps = agent_caps(agent)
    tools = []
    if caps.get("web_scraping"):
        tools.append({"type": "web_search_20250305", "name": "web_search"})

    mcp_servers = []
    for i, url in enumerate(agent_mcp(agent)):
        url = (url or "").strip()
        if not url:
            continue
        mcp_servers.append({"type": "url", "url": url, "name": f"connector-{i+1}"})

    return tools, mcp_servers


async def call_claude(api_key: str, model: str, system: str, messages: list[dict],
                      max_tokens: int = 1500, tools: list[dict] | None = None,
                      mcp_servers: list[dict] | None = None) -> str:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=api_key)
    kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
    if tools:
        kwargs["tools"] = tools
    if mcp_servers:
        # MCP connector support is currently a beta header on the Anthropic API.
        kwargs["mcp_servers"] = mcp_servers
        kwargs["extra_headers"] = {"anthropic-beta": "mcp-client-2025-04-04"}
    resp = await client.messages.create(**kwargs)
    # Only "text" blocks are the final answer; web_search / mcp_tool_use / mcp_tool_result
    # blocks are intermediate — Claude already folds their findings into the text reply.
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# ============================== local web tools (for Ollama agents) ==============================
# Ollama only *requests* a tool call; we run the actual web calls here and feed results back.

WEB_TOOLS_OLLAMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": ("Search the public web for current, factual, or up-to-date "
                            "information. Returns result titles, URLs, and snippets. Use this "
                            "whenever you are asked for facts, specs, prices, or anything you "
                            "are not fully certain about — do not guess."),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": ("Fetch the readable text of a specific web page. Use after "
                            "web_search to read a promising result in full."),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Full http(s) URL to read."}},
                "required": ["url"],
            },
        },
    },
]

# ---- vision ----------------------------------------------------------------
# The agent's own chat model is usually text-only, so image understanding runs as a
# TOOL against a dedicated vision model rather than by stuffing pixels into the main
# conversation. That keeps text agents working unchanged and means the vision model
# is swappable via VISION_MODEL in .env.
VISION_MODEL = os.environ.get("VISION_MODEL", "").strip()
# Used only as a fallback when the Ollama build is too old to report per-model
# capabilities. Ordered best-first for reading text and labels in product photos.
# Deliberately excludes families with text-only variants under the same name
# (gemma3:1b is not multimodal, gemma3:4b is) — those are picked up by capability
# detection instead, never by name. Note qwen3-vl is multimodal but plain qwen3
# is NOT, so the "-vl" suffix is load-bearing here.
VISION_CANDIDATES = ["qwen3-vl", "llama3.2-vision", "qwen2.5vl", "qwen2-vl", "minicpm-v",
                     "llava-llama3", "llava-phi3", "llava", "bakllava",
                     "granite3.2-vision", "moondream"]
# Suggested in error messages when nothing vision-capable is installed.
VISION_SUGGESTION = os.environ.get("VISION_SUGGESTION", "qwen3-vl:4b")

VISION_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": ("Look at an image that was shared in this chat and describe what it "
                        "shows. Use it for ANY question about a picture, screenshot, product "
                        "photo, chart, or scanned page. Ask a specific question — e.g. 'what "
                        "brand and model is this?' or 'read all the text'. Pair it with "
                        "web_search when you need current facts about whatever you identify: "
                        "analyze the image first, then search for what you found."),
        "parameters": {
            "type": "object",
            "properties": {
                "image_id": {"type": "string",
                             "description": "The id shown next to the image in the chat, e.g. img1. "
                                            "Use img1 for the most recent image."},
                "question": {"type": "string",
                             "description": "What you want to know about the image."},
            },
            "required": ["image_id", "question"],
        },
    },
}

# When vision is unavailable the model must not fall back on inventing an answer.
# A filename like "61+NAOM-FZL.jpg" is a CDN artefact, not a brand — but a model
# under pressure to be helpful will happily read one into it.
NO_GUESSING = (
    "CRITICAL: you have NO information about this image. Do NOT infer the product, "
    "brand, or model from the file NAME — filenames are random upload artefacts and "
    "guessing from them produces confident false answers. Do NOT search the web for "
    "anything derived from the filename. State only that you could not see the image, "
    "and ask the user to type what is written on the product label."
)

DOC_TOOL = {
    "type": "function",
    "function": {
        "name": "read_document",
        "description": ("Read the text of a file shared in this chat (PDF, txt, md, csv, json, "
                        "log). Use the id shown next to the file, e.g. doc1 for the most recent."),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "File id from the chat, e.g. doc1."},
                "query": {"type": "string",
                          "description": "Optional: only return the parts about this."},
            },
            "required": ["doc_id"],
        },
    },
}

ASK_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_agent",
        "description": ("Send a message to one of your owner's OTHER agents and get its "
                        "reply. Use this whenever you are asked to talk to, greet, ask, or "
                        "delegate something to another agent by name — you cannot message "
                        "them any other way, and simply writing the message in this chat "
                        "does NOT reach them."),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {"type": "string",
                          "description": "The other agent's name or @slug, e.g. teddy."},
                "message": {"type": "string",
                            "description": "What to say or ask them."},
            },
            "required": ["agent", "message"],
        },
    },
}

MESSAGE_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "message_user",
        "description": ("Send a chat message to a PERSON your owner already talks to. Use "
                        "this when asked to message, tell, notify or say something to someone "
                        "by name. It opens a chat between you and them and posts the message "
                        "immediately — writing it in this conversation does NOT reach them."),
        "parameters": {
            "type": "object",
            "properties": {
                "person": {"type": "string",
                           "description": "Their name or email, e.g. bala."},
                "message": {"type": "string", "description": "The message to send them."},
            },
            "required": ["person", "message"],
        },
    },
}

# A single sandboxed folder on the server that agents may read and write. NOT the
# user's own PC — agents run server-side, so "the local disk" is the server's disk.
# Everything is confined to this directory: see _ws_path().
WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE",
                                str(Path(__file__).parent / "workspace"))).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
WS_MAX_READ = 100_000       # bytes returned from one read
WS_MAX_WRITE = 500_000      # bytes an agent may write in one go
WS_MAX_LIST = 200           # entries returned from one listing
WS_TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml",
               ".xml", ".html", ".py", ".js", ".sql", ".ini", ".cfg", ".env.example"}

LIST_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": ("List files in the shared workspace folder on the server. This is a "
                        "sandbox — it is NOT the user's own computer and you cannot reach "
                        "anything outside it. Use it to see what you have to work with."),
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "Optional sub-folder, e.g. reports."}}},
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": ("Read a text file from the workspace folder. Use list_files first to "
                        "see what exists. For files someone attached to the chat, use "
                        "read_document instead."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path within the workspace, e.g. data/sales.csv."},
                "query": {"type": "string", "description": "Optional: return only lines about this."},
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": ("Save a text file into the workspace folder so the user can download "
                        "it later. Overwrites an existing file of the same name."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Filename, e.g. report.md."},
                "content": {"type": "string", "description": "The full file contents."},
            },
            "required": ["path", "content"],
        },
    },
}

# ---- the asker's own computer, via their desktop app --------------------------
# The server cannot touch anyone's disk. These tools send a request down the
# WebSocket to the requesting user's Electron client, which reads the file locally
# and sends the result back. Confined to a folder that user explicitly picked.
LOCAL_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "list_my_files",
        "description": ("List files in the folder the person you are talking to shared from "
                        "THEIR OWN computer. Only works in the desktop app, and only after "
                        "they have picked a folder to share."),
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "Optional sub-folder inside the shared one."}}},
    },
}

LOCAL_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read_my_file",
        "description": ("Read a text file on the person's own computer, e.g. "
                        "downloads/notes.txt. Call list_my_files first if unsure of the name."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to their shared folder."},
                "query": {"type": "string", "description": "Optional: only return lines about this."},
            },
            "required": ["path"],
        },
    },
}

OPEN_LOCAL_TOOL = {
    "type": "function",
    "function": {
        "name": "open_on_my_computer",
        "description": ("Open a folder or file on the person's own computer — it appears in "
                        "their file manager or default app, on their screen. Use it when they "
                        "say 'open my documents folder', 'show me that file', or similar. They "
                        "get a confirmation prompt first, so nothing opens without their click."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Path inside their shared folder. Empty string opens "
                                        "the shared folder itself."},
            },
        },
    },
}

CREATE_LOCAL_TOOL = {
    "type": "function",
    "function": {
        "name": "create_my_file",
        "description": ("Create or overwrite a text file on the person's own computer, inside "
                        "the folder they shared. Use it when they ask you to make, write or "
                        "save a file for them. They get a confirmation prompt first. Text "
                        "formats only: .txt .md .csv .json .log .yaml .xml .html"),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Filename, e.g. notes.txt or ideas/list.md."},
                "content": {"type": "string", "description": "The full contents of the file."},
            },
            "required": ["path", "content"],
        },
    },
}

def _local_tool(name, desc, props, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       **({"required": required} if required else {})}}}


SEARCH_LOCAL_TOOL = _local_tool(
    "search_my_files",
    "Find files by name anywhere inside the folder the person shared from their own "
    "computer. Use it when they don't know where something is.",
    {"term": {"type": "string", "description": "Part of the filename, e.g. invoice."},
     "folder": {"type": "string", "description": "Where to look, e.g. documents. Default: home."}},
    ["term"])

MKDIR_LOCAL_TOOL = _local_tool(
    "make_folder_on_my_computer",
    "Create a new folder inside the shared folder on the person's own computer.",
    {"path": {"type": "string", "description": "Folder path, e.g. reports/2026."}},
    ["path"])

DELETE_LOCAL_TOOL = _local_tool(
    "delete_on_my_computer",
    "Permanently delete a file or folder on the person's own computer. They must "
    "confirm on their screen first. It does NOT go to the Recycle Bin, so never call "
    "this speculatively — only when they clearly asked for that exact thing to be deleted.",
    {"path": {"type": "string", "description": "What to delete, relative to the shared folder."}},
    ["path"])

MOVE_LOCAL_TOOL = _local_tool(
    "move_on_my_computer",
    "Move or rename a file inside the shared folder on the person's own computer. "
    "They confirm on their screen first.",
    {"from": {"type": "string", "description": "Current path."},
     "to": {"type": "string", "description": "New path or filename."}},
    ["from", "to"])

OPEN_APP_TOOL = _local_tool(
    "open_app_on_my_computer",
    "Start an installed application on the person's own computer — Chrome, Word, "
    "Spotify, whatever they have. Use it whenever they say 'open', 'launch' or "
    "'start' an app by name. They confirm on their screen first. If you are not "
    "sure of the exact name, call list_my_apps first.",
    {"name": {"type": "string", "description": "App name, e.g. chrome or excel."}},
    ["name"])

LIST_APPS_TOOL = _local_tool(
    "list_my_apps",
    "List the applications installed on the person's own computer. Use it when they "
    "ask what they have installed, or when open_app_on_my_computer can't find a name.",
    {})

OPEN_URL_TOOL = _local_tool(
    "open_url_on_my_computer",
    "Open a web page in the person's default browser, on their screen. Use it when "
    "they ask you to open a site or a link. They confirm first.",
    {"url": {"type": "string", "description": "Full http(s) URL."}},
    ["url"])

SYSINFO_LOCAL_TOOL = _local_tool(
    "my_computer_info",
    "Facts about the person's own computer: operating system and version, CPU, memory, "
    "free disk space, uptime. Use it when asked about their machine, specs or disk space.",
    {})

# ---- agent memory: notes that survive between conversations -------------------
MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_notes (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_notes ON agent_notes(agent_id);
"""


def init_agent_notes():
    with dbx.db() as conn:
        conn.executescript(MEMORY_SCHEMA)


REMEMBER_TOOL = _local_tool(
    "remember",
    "Save a short note you will still need in a later conversation — a preference, "
    "a decision, an ID, a name. Only for things worth keeping; do not save whole "
    "conversations. Saving the same topic again replaces the old note.",
    {"topic": {"type": "string", "description": "A few words naming what this is about."},
     "note": {"type": "string", "description": "What to remember."}},
    ["topic", "note"])

RECALL_TOOL = _local_tool(
    "recall",
    "Look up what you saved earlier with remember. Call it when someone refers to "
    "something from a past conversation, or before saying you don't know.",
    {"topic": {"type": "string", "description": "Optional: only notes matching this."}})

FORGET_TOOL = _local_tool(
    "forget",
    "Delete a note you saved. Use it when the person says something has changed or "
    "asks you to forget it.",
    {"topic": {"type": "string", "description": "The topic to delete."}},
    ["topic"])

MY_TOOLS_TOOL = _local_tool(
    "list_my_tools",
    "List the tools you actually have this turn, including the exact tools each "
    "connected service exposes. Call it whenever you are about to tell someone what "
    "you can or cannot do — describing your own abilities from memory is guesswork, "
    "and a connector often covers only part of a service.",
    {})

EDIT_IMAGE_TOOL = _local_tool(
    "edit_image",
    "Change an EXISTING image in this conversation and post the result. Use it "
    "to revise something already produced, or to work from a product photo "
    "someone uploaded — that is the only way the real product is preserved, "
    "because generating from a description invents a lookalike instead.",
    {"image_id": {"type": "string",
                  "description": "The id of the image message to work from. Use "
                                 "list_images to find it."},
     "changes": {"type": "string",
                 "description": "What to change, specifically. 'Brighter' is not "
                                "enough; say what, where and how much."},
     "caption": {"type": "string", "description": "A short line to post with it."}},
    ["image_id", "changes"])

LIST_IMAGES_TOOL = _local_tool(
    "list_images",
    "List the images and files in this conversation with their ids, so you can "
    "name one when editing. Ids are stable; positions are not.",
    {})

DELEGATE_TASK_TOOL = _local_tool(
    "delegate_task",
    "Assign a piece of WORK to another agent and get a structured result back. "
    "Use this instead of ask_agent whenever you want something produced rather "
    "than answered — an image, a draft, a lookup. Say exactly what you need and "
    "how you will know it is done; a vague brief comes back vague.",
    {"agent": {"type": "string", "description": "Which agent to assign it to."},
     "objective": {"type": "string",
                   "description": "What they must produce, in one clear sentence."},
     "context": {"type": "string",
                 "description": "Background they need: product, audience, what came before."},
     "constraints": {"type": "string",
                     "description": "Limits: style, length, what to avoid."},
     "deliverable": {"type": "string",
                     "description": "The concrete thing you expect back."},
     "done_when": {"type": "string",
                   "description": "How you will judge it complete."}},
    ["agent", "objective", "deliverable"])

CREATE_IMAGE_TOOL = _local_tool(
    "create_image",
    "Create an image from a description and post it into this conversation. Use "
    "it when someone asks for a picture, a product shot, a gallery image or a "
    "mock-up. Describe the subject, style, lighting and composition — a longer, "
    "specific prompt gives a far better result than a short one.",
    {"prompt": {"type": "string",
                "description": "What the image should show, in detail."},
     "caption": {"type": "string",
                 "description": "A short line to post alongside it."}},
    ["prompt"])

PEOPLE_TOOL = _local_tool(
    "list_people",
    "List the people you can send a message to, so you can name them rather than "
    "guessing. Use it before message_user if you are unsure of a name.",
    {})

SHARE_FILE_TOOL = _local_tool(
    "share_file_in_chat",
    "Post a file from the server workspace into this conversation so everyone can "
    "download it. Use it after write_file when someone asked for a document.",
    {"path": {"type": "string", "description": "Path inside the workspace folder."}},
    ["path"])

REMIND_TOOL = _local_tool(
    "schedule_reminder",
    "Set yourself a daily task at a fixed time — a reminder, a report, a check. It "
    "runs and posts here even if nobody is talking to you. Time is 24-hour local.",
    {"title": {"type": "string", "description": "Short name for the task."},
     "prompt": {"type": "string", "description": "What you should do when it runs."},
     "time": {"type": "string", "description": "24-hour time, e.g. 09:00."}},
    ["title", "prompt", "time"])

CALC_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": ("Evaluate an arithmetic expression exactly. Use this for ANY sum, "
                        "percentage, conversion, or comparison of numbers — do not do mental "
                        "arithmetic. Supports + - * / // % ** ( ) and round/abs/min/max/sum."),
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string",
                                          "description": "e.g. (1299 - 899) / 1299 * 100"}},
            "required": ["expression"],
        },
    },
}

TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "current_time",
        "description": ("Get today's date and the current time. Call this before answering "
                        "anything involving 'today', 'now', 'latest', 'this week', or before "
                        "searching for recent news — you do not otherwise know the date."),
        "parameters": {"type": "object", "properties": {
            "timezone": {"type": "string", "description": "Optional IANA zone, e.g. Asia/Kolkata."}}},
    },
}

CHAT_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_chat",
        "description": ("Search earlier messages in this conversation for something that has "
                        "scrolled out of view. Use when asked what someone said before."),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Words to look for."}},
            "required": ["query"],
        },
    },
}

# Always available — cheap, local, and they fix the things models are worst at
# (arithmetic and not knowing what day it is).
BASE_TOOLS = [CALC_TOOL, TIME_TOOL, CHAT_SEARCH_TOOL]

# Human-readable labels for the "agent is using X" indicator in the chat UI.
TOOL_LABELS = {
    "web_search": "Searching the web",
    "fetch_url": "Reading a web page",
    "analyze_image": "Looking at the image",
    "read_document": "Reading the document",
    "calculate": "Calculating",
    "current_time": "Checking the date",
    "search_chat": "Searching this chat",
    "ask_agent": "Asking another agent",
    "message_user": "Messaging a teammate",
    "list_files": "Listing workspace files",
    "read_file": "Reading a file",
    "write_file": "Saving a file",
    "list_my_files": "Looking at your files",
    "read_my_file": "Reading your file",
    "open_on_my_computer": "Opening it on your PC",
    "create_my_file": "Creating a file on your PC",
    "search_my_files": "Searching your files",
    "make_folder_on_my_computer": "Creating a folder",
    "delete_on_my_computer": "Deleting on your PC",
    "move_on_my_computer": "Moving a file",
    "my_computer_info": "Checking your system",
    "open_app_on_my_computer": "Opening an app on your PC",
    "list_my_apps": "Checking your installed apps",
    "open_url_on_my_computer": "Opening a link on your PC",
    "connect_connector": "Connecting a service",
    "remember": "Making a note",
    "recall": "Checking my notes",
    "forget": "Deleting a note",
    "list_people": "Checking who's here",
    "list_my_tools": "Checking what I can do",
    "create_image": "Creating an image",
    "delegate_task": "Assigning work to another agent",
    "edit_image": "Editing an image",
    "list_images": "Looking at what's here",
    "share_file_in_chat": "Sharing a file here",
    "schedule_reminder": "Setting up a daily task",
}


def tool_label(name: str) -> str:
    if name and name.startswith("mcp_"):
        # mcp_notion_search_pages -> "Using Notion"
        return "Using " + name.split("_")[1].capitalize()
    return TOOL_LABELS.get(name, name.replace("_", " ").capitalize() if name else "Working")


def tool_detail(name: str, args: dict) -> str:
    """Short subtitle for the indicator: what the tool is actually being asked."""
    args = args if isinstance(args, dict) else {}
    for key in ("query", "url", "expression", "question", "agent", "person", "path",
                "term", "from", "folder", "name", "topic", "title", "message",
                "image_id", "doc_id", "timezone"):
        val = str(args.get(key) or "").strip()
        if val:
            return val[:80]
    return ""


async def _do_web_search(query: str, max_results: int = 5) -> str:
    """Real web search. Prefers the `ddgs` package; falls back to raw DuckDuckGo HTML."""
    query = (query or "").strip()
    if not query:
        return "No query provided."
    # Primary: ddgs / duckduckgo_search library (most reliable).
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        out = []
        with DDGS() as ddg:
            for r in ddg.text(query, max_results=max_results):
                out.append(f"- {r.get('title', '')}\n  {r.get('href', '')}\n  {r.get('body', '')}")
        if out:
            return "Search results:\n" + "\n".join(out)
    except Exception:
        pass
    # Fallback: scrape DuckDuckGo's HTML endpoint with httpx (no extra dependency).
    try:
        import html as _html
        import re as _re
        from urllib.parse import parse_qs, unquote, urlparse

        import httpx
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
            resp.raise_for_status()
            body = resp.text
        pairs = _re.findall(r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, _re.S)
        out = []
        for href, title in pairs[:max_results]:
            if href.startswith("//"):
                href = "https:" + href
            uddg = parse_qs(urlparse(href).query).get("uddg")
            real = unquote(uddg[0]) if uddg else href
            title = _html.unescape(_re.sub(r"<[^>]+>", "", title)).strip()
            out.append(f"- {title}\n  {real}")
        return "Search results:\n" + "\n".join(out) if out else "No results found."
    except Exception as e:
        return f"web_search failed: {type(e).__name__}"


async def _do_fetch_url(url: str, max_chars: int = 4000) -> str:
    """Fetch a page and return a rough plain-text extraction."""
    url = (url or "").strip()
    if not url:
        return "No URL provided."
    if not url.startswith("http"):
        url = "https://" + url
    try:
        import html as _html
        import re as _re

        import httpx
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return f"(Skipped: non-text content-type {ctype})"
            body = resp.text
    except Exception as e:
        return f"fetch_url failed: {type(e).__name__}"
    body = _re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", body)
    text = _html.unescape(_re.sub(r"(?s)<[^>]+>", " ", body))
    text = _re.sub(r"\s+", " ", text).strip()
    return text[:max_chars] if text else "(no readable text found)"


# ============================== attachment index ==============================
# Tools refer to chat attachments by a short id (img1, doc1). The runtime builds
# that map per turn, so the model never sees file paths and can't reach outside
# the conversation it is answering in.

def _local_path(content: str) -> Path | None:
    """Map a stored message URL (/files/<id>.<ext>) to a file inside uploads/."""
    name = Path((content or "").split("?")[0]).name
    if not name:
        return None
    p = (UPLOADS / name).resolve()
    try:
        if p.is_file() and UPLOADS.resolve() in p.parents:
            return p
    except OSError:
        pass
    return None


def attachment_index(cid: str, limit: int = MAX_HISTORY) -> dict:
    """{'img1': msg, 'doc1': msg, ...} — newest first, so img1 is the latest image."""
    msgs = [m for m in dbx.get_messages(cid, limit=limit) if not m.get("deleted")]
    imgs, docs, out = 0, 0, {}
    for m in reversed(msgs):                       # newest first
        kind = m.get("kind", "text")
        name = (m.get("file_name") or "").lower()
        is_img = kind == "image" or name.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
        if is_img:
            imgs += 1
            out[f"img{imgs}"] = m
        elif kind == "file":
            docs += 1
            out[f"doc{docs}"] = m
    return out


def _resolve(index: dict, ref: str, prefix: str):
    """Forgiving lookup: 'img1', 'IMG 1', '1', or a filename all work."""
    ref = (ref or "").strip().lower().replace(" ", "").replace("_", "")
    if ref in index:
        return index[ref]
    if ref.isdigit() and f"{prefix}{ref}" in index:
        return index[f"{prefix}{ref}"]
    for key, m in index.items():
        if key.startswith(prefix) and ref and ref in (m.get("file_name") or "").lower():
            return m
    # Bare "the image" / empty -> most recent of that kind.
    return index.get(f"{prefix}1")


# ============================== vision ==============================

async def _ollama_json(client, method: str, url: str, payload=None):
    """Return (status, parsed_or_none, raw_text). Never raises on HTTP status —
    callers need the body to explain what went wrong."""
    resp = await (client.post(url, json=payload) if method == "POST" else client.get(url))
    text = resp.text
    try:
        data = resp.json()
    except Exception:
        data = None
    return resp.status_code, data, text


def _ollama_error(data, text: str) -> str:
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])[:300]
    return (text or "").strip()[:300] or "no error message"


_vision_cache: dict[str, list[str]] = {}   # base_url -> ordered candidate models


async def vision_models(base_url: str, refresh: bool = False) -> list[str]:
    """Installed models that can actually accept images, best first.

    Name matching alone is not enough — gemma3:1b matches 'gemma3' but is text-only,
    and Ollama then rejects the image with a 400. Newer Ollama reports a per-model
    `capabilities` list via /api/show, so ask it. Fall back to a conservative name
    list only when /api/show doesn't provide capabilities.
    """
    if VISION_MODEL:
        return [VISION_MODEL]
    if not refresh and base_url in _vision_cache:
        return _vision_cache[base_url]

    import httpx
    found, guessed = [], []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            status, data, text = await _ollama_json(client, "GET", f"{base_url}/api/tags")
            if status != 200 or not isinstance(data, dict):
                return []
            names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            for name in names:
                st, info, _ = await _ollama_json(client, "POST", f"{base_url}/api/show",
                                                 {"model": name})
                caps = []
                if st == 200 and isinstance(info, dict):
                    caps = [str(c).lower() for c in (info.get("capabilities") or [])]
                if "vision" in caps:
                    found.append(name)
                elif not caps:
                    # Old Ollama build with no capability reporting — fall back to names.
                    base = name.split(":")[0].lower()
                    if any(base.startswith(c.lower()) for c in VISION_CANDIDATES):
                        guessed.append(name)
    except Exception as e:
        print(f"[vision] couldn't list models on {base_url}: {type(e).__name__}: {e}", flush=True)
        return []

    ranked = found + [g for g in guessed if g not in found]
    # Prefer models known to be good at reading text/labels in photos.
    ranked.sort(key=lambda n: next((i for i, c in enumerate(VISION_CANDIDATES)
                                    if n.split(":")[0].lower().startswith(c.lower())), 99))
    _vision_cache[base_url] = ranked
    print(f"[vision] vision-capable models on {base_url}: {ranked or 'none'}", flush=True)
    return ranked


async def _do_analyze_image(agent: dict, cid: str, image_id: str, question: str) -> str:
    import base64

    import httpx
    msg = _resolve(attachment_index(cid), image_id, "img")
    if not msg:
        return ("No image found in this conversation. Ask the person to attach one, "
                "then try again.")
    path = _local_path(msg.get("content", ""))
    if not path:
        return f"The file for {msg.get('file_name') or 'that image'} is missing from the server."

    base = (agent.get("ollama_url") or "").rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base

    candidates = await vision_models(base)
    if not candidates:
        return ("VISION UNAVAILABLE — no image-capable model is installed on the model "
                "server, so nobody can look at this picture. Tell the user plainly that "
                "image analysis is not set up and that the owner needs to run: "
                f"ollama pull {VISION_SUGGESTION}   (or set VISION_MODEL in .env). "
                "Do NOT retry this tool. " + NO_GUESSING)

    try:
        b64 = base64.b64encode(path.read_bytes()).decode()
    except OSError as e:
        return f"Couldn't read the image file ({type(e).__name__}). Do NOT retry."

    prompt = (question or "Describe this image in detail.").strip()
    content = (
        "Describe exactly what you can see. Report only what is actually visible — "
        "brand names, model numbers, labels, readable text, colours, counts, and any "
        "figures. If something is unreadable, say so rather than guessing.\n\n"
        f"Question: {prompt}")

    problems = []
    async with httpx.AsyncClient(timeout=180) as client:
        for model in candidates[:3]:      # try a couple before giving up
            payload = {"model": model, "stream": False,
                       "messages": [{"role": "user", "content": content, "images": [b64]}]}
            try:
                status, data, text = await _ollama_json(client, "POST", f"{base}/api/chat", payload)
            except httpx.TimeoutException:
                problems.append(f"{model}: timed out after 180s")
                continue
            except Exception as e:
                problems.append(f"{model}: {type(e).__name__}")
                continue

            if status == 200 and isinstance(data, dict):
                out = strip_think(((data.get("message") or {}).get("content") or "").strip())
                if out:
                    print(f"[vision] {model} described {path.name} "
                          f"({len(out)} chars)", flush=True)
                    return (f"Image '{msg.get('file_name') or image_id}' (read by {model}):\n{out}\n\n"
                            "Treat this description as your only evidence about the picture. If you "
                            "now need current facts about anything named here, call web_search next.")
                problems.append(f"{model}: returned an empty description")
                continue

            err = _ollama_error(data, text)
            problems.append(f"{model}: HTTP {status} — {err}")
            print(f"[vision] {model} failed: HTTP {status} — {err}", flush=True)
            # This model can't do images after all; drop it from the cache so we
            # don't keep picking it, and try the next candidate.
            if status in (400, 404, 500) and base in _vision_cache:
                _vision_cache[base] = [m for m in _vision_cache[base] if m != model]

    detail = "; ".join(problems) or "unknown error"
    return ("VISION FAILED — the image could not be read. The model server reported: "
            f"{detail}. Do NOT call analyze_image again for this image. Tell the user "
            "exactly what the error was and that a working vision model is needed "
            f"(e.g. `ollama pull {VISION_SUGGESTION}`, then set VISION_MODEL if needed). "
            + NO_GUESSING)


# ============================== documents ==============================

def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader          # older installs
        except ImportError:
            return ("(Can't read PDFs — the server needs `pip install pypdf`.)")
    try:
        reader = PdfReader(str(path))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception as e:
        return f"(Couldn't parse that PDF: {type(e).__name__})"


async def _do_read_document(cid: str, doc_id: str, query: str = "", max_chars: int = 6000) -> str:
    msg = _resolve(attachment_index(cid), doc_id, "doc")
    if not msg:
        return "No file found in this conversation."
    path = _local_path(msg.get("content", ""))
    if not path:
        return f"The file for {msg.get('file_name') or 'that document'} is missing from the server."
    name = msg.get("file_name") or path.name
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        text = _extract_pdf(path)
    elif suffix in (".txt", ".md", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".xml", ".html"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"Couldn't read {name} ({type(e).__name__})."
    else:
        return (f"'{name}' is a {suffix or 'binary'} file, which I can't read as text. "
                "I can read PDF, txt, md, csv, json, log, yaml, xml and html.")

    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not text:
        return f"'{name}' has no extractable text (it may be a scan — try analyze_image instead)."

    # With a query, return only the matching neighbourhoods so long files still fit.
    q = (query or "").strip().lower()
    if q and len(text) > max_chars:
        hits, lines = [], text.splitlines()
        for i, line in enumerate(lines):
            if q in line.lower():
                hits.append("\n".join(lines[max(0, i - 2): i + 3]))
            if sum(len(h) for h in hits) > max_chars:
                break
        if hits:
            return f"From '{name}' (matching '{query}'):\n" + "\n---\n".join(hits)[:max_chars]
    clipped = text[:max_chars]
    more = "" if len(text) <= max_chars else f"\n\n(…truncated, {len(text)} chars total)"
    return f"Contents of '{name}':\n{clipped}{more}"


# ============================== calculator ==============================

_CALC_OK = set("0123456789.+-*/%()<>=!, e")
_CALC_FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "sum": sum,
               "int": int, "float": float, "pow": pow}


def _do_calculate(expression: str) -> str:
    expr = (expression or "").strip()
    if not expr:
        return "No expression given."
    # Whitelist characters and known function names — never eval arbitrary input.
    probe = expr
    for fn in _CALC_FUNCS:
        probe = probe.replace(fn, "")
    if not set(probe.lower()) <= _CALC_OK:
        return ("That expression has characters I won't evaluate. Use numbers and "
                "+ - * / // % ** ( ) round abs min max sum only.")
    if len(expr) > 300 or "**" in expr and len(expr) > 60:
        return "Expression too long to evaluate safely."
    try:
        value = eval(expr, {"__builtins__": {}}, dict(_CALC_FUNCS))   # noqa: S307 - whitelisted
    except ZeroDivisionError:
        return "Division by zero."
    except Exception as e:
        return f"Couldn't evaluate that ({type(e).__name__})."
    if isinstance(value, float):
        value = round(value, 10)
    return f"{expr} = {value}"


# ============================== clock ==============================

def _do_current_time(tz_name: str = "") -> str:
    from datetime import datetime, timezone as _tz
    label, now_dt = "UTC", datetime.now(_tz.utc)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(tz_name)
            now_dt, label = datetime.now(zone), tz_name
        except Exception:
            pass
    return (f"Current date and time: {now_dt.strftime('%A, %d %B %Y, %H:%M')} ({label}). "
            f"Use this when judging whether information is current.")


# ============================== chat search ==============================

def _do_search_chat(cid: str, query: str, limit: int = 8) -> str:
    q = (query or "").strip().lower()
    if not q:
        return "No search term given."
    hits = []
    for m in dbx.get_messages(cid, limit=400):
        if m.get("deleted") or m.get("kind", "text") != "text":
            continue
        if q in (m.get("content") or "").lower():
            hits.append(f'- {m.get("sender_name", "someone")}: {m["content"][:200]}')
        if len(hits) >= limit:
            break
    return ("Earlier messages mentioning '%s':\n%s" % (query, "\n".join(hits))
            if hits else f"Nothing earlier in this chat mentions '{query}'.")


# ============================== workspace files ==============================

def _ws_path(rel: str) -> Path | None:
    """Resolve a path inside WORKSPACE, or None if it escapes. Rejects absolute
    paths, drive letters and any traversal — resolve() collapses '..' before the
    containment check, so symlinks and encoded tricks are covered too."""
    raw = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not raw or ":" in raw.split("/")[0]:
        return None
    target = (WORKSPACE / raw).resolve()
    if target != WORKSPACE and WORKSPACE not in target.parents:
        return None
    return target


def _do_list_files(folder: str = "") -> str:
    base = _ws_path(folder) if folder else WORKSPACE
    if base is None:
        return "That folder is outside the workspace. You can only see files inside it."
    if not base.is_dir():
        return f"There is no folder called '{folder}' in the workspace."
    rows = []
    for p in sorted(base.rglob("*")):
        if p.is_dir() or p.name.startswith("."):
            continue
        try:
            rows.append(f"- {p.relative_to(WORKSPACE).as_posix()}  ({fmt_size(p.stat().st_size)})")
        except OSError:
            continue
        if len(rows) >= WS_MAX_LIST:
            rows.append(f"…(showing the first {WS_MAX_LIST})")
            break
    if not rows:
        return ("The workspace folder is empty. Tell the user they can drop files into it, "
                "or attach a file to the chat and use read_document instead.")
    return "Files in the workspace:\n" + "\n".join(rows)


def fmt_size(n: int) -> str:
    return f"{n/1048576:.1f} MB" if n > 1048576 else f"{max(1, n // 1024)} KB"


def _do_read_file(path: str, query: str = "") -> str:
    target = _ws_path(path)
    if target is None:
        return ("That path is outside the workspace and I will not read it. Only files "
                "inside the workspace folder are available.")
    if not target.is_file():
        return f"There is no file at '{path}'. Call list_files to see what exists."
    if target.suffix.lower() == ".pdf":
        text = _extract_pdf(target)
    elif target.suffix.lower() in WS_TEXT_EXT or target.suffix == "":
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"Couldn't read that file ({type(e).__name__})."
    else:
        return (f"'{path}' is a {target.suffix or 'binary'} file, which I can't read as text.")

    text = (text or "").strip()
    if not text:
        return f"'{path}' is empty."
    q = (query or "").strip().lower()
    if q and len(text) > WS_MAX_READ:
        lines = text.splitlines()
        hits = ["\n".join(lines[max(0, i - 2): i + 3])
                for i, line in enumerate(lines) if q in line.lower()][:40]
        if hits:
            return f"From '{path}' (matching '{query}'):\n" + "\n---\n".join(hits)[:WS_MAX_READ]
    clipped = text[:WS_MAX_READ]
    more = "" if len(text) <= WS_MAX_READ else f"\n\n(…truncated, {len(text)} chars total)"
    return f"Contents of '{path}':\n{clipped}{more}"


def _do_write_file(path: str, content: str) -> str:
    target = _ws_path(path)
    if target is None:
        return "That path is outside the workspace, so I won't write there."
    if target.suffix.lower() not in WS_TEXT_EXT:
        return (f"I can only write text files ({', '.join(sorted(WS_TEXT_EXT))}). "
                f"'{path}' isn't one.")
    body = content or ""
    if len(body.encode("utf-8")) > WS_MAX_WRITE:
        return f"That's too large to write (limit {WS_MAX_WRITE // 1000} KB)."
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    except OSError as e:
        return f"Couldn't save it ({type(e).__name__})."
    return (f"Saved '{path}' ({fmt_size(target.stat().st_size)}) in the workspace folder. "
            "Tell the user where it is.")


# ============================== agent memory ==============================

MAX_NOTES = 200
MAX_NOTE_LEN = 1200


def _do_remember(agent: dict, topic: str, note: str) -> str:
    t = (topic or "").strip()[:120]
    n = (note or "").strip()[:MAX_NOTE_LEN]
    if not t or not n:
        return "Both a topic and a note are needed. Nothing was saved."
    with dbx.db() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM agent_notes WHERE agent_id = ?",
                             (agent["id"],)).fetchone()["c"]
        # Replace rather than pile up duplicates on the same topic.
        conn.execute("DELETE FROM agent_notes WHERE agent_id = ? AND LOWER(topic) = LOWER(?)",
                     (agent["id"], t))
        if count >= MAX_NOTES:
            conn.execute("""DELETE FROM agent_notes WHERE id IN (
                              SELECT id FROM agent_notes WHERE agent_id = ?
                              ORDER BY created_at ASC LIMIT 1)""", (agent["id"],))
        conn.execute("INSERT INTO agent_notes (id, agent_id, topic, note, created_at) "
                     "VALUES (?,?,?,?,?)", (dbx.new_id(), agent["id"], t, n, dbx.now()))
    return f"Noted under '{t}'. Tell the user briefly that you'll remember it."


def _do_recall(agent: dict, topic: str = "") -> str:
    q = (topic or "").strip().lower()
    with dbx.db() as conn:
        rows = conn.execute(
            "SELECT topic, note, created_at FROM agent_notes WHERE agent_id = ? "
            "ORDER BY created_at DESC", (agent["id"],)).fetchall()
    notes = [dict(r) for r in rows]
    if q:
        notes = [n for n in notes
                 if q in n["topic"].lower() or q in n["note"].lower()]
    if not notes:
        return ("Nothing saved" + (f" about '{topic}'." if q else " yet.")
                + " Say you don't have a note on it rather than inventing one.")
    return "Your saved notes:\n" + "\n".join(
        f"- {n['topic']}: {n['note']}" for n in notes[:40])


def _do_forget(agent: dict, topic: str) -> str:
    t = (topic or "").strip()
    if not t:
        return "Which topic? Nothing was deleted."
    with dbx.db() as conn:
        n = conn.execute("DELETE FROM agent_notes WHERE agent_id = ? AND LOWER(topic) = LOWER(?)",
                         (agent["id"], t)).rowcount
    return f"Deleted the note on '{t}'." if n else f"There was no note on '{t}'."


def _do_list_my_tools(ctx: dict) -> str:
    """What this agent can actually do right now, read from the turn's own tool list
    rather than from the model's memory of itself."""
    names = [t["function"]["name"] for t in (ctx.get("tool_schemas") or [])
             if isinstance(t, dict) and t.get("function")]
    if not names:
        return "I couldn't read my own tool list this turn."

    built_in = sorted(n for n in names if not n.startswith("mcp_"))
    by_service: dict[str, list[str]] = {}
    for n in names:
        if not n.startswith("mcp_"):
            continue
        parts = n.split("_", 2)
        service = parts[1] if len(parts) > 2 else "connector"
        by_service.setdefault(service, []).append(parts[2] if len(parts) > 2 else n)

    out = ["Built-in tools: " + ", ".join(built_in)]
    for service, tools in sorted(by_service.items()):
        out.append(f"\n{service} connector — these and nothing else: "
                   + ", ".join(sorted(tools)))
    out.append("\nAnswer using ONLY this list. If something is not here, say plainly "
               "that the connector doesn't offer it, and name what it does offer.")
    return "\n".join(out)


# main.py injects this: it writes the bytes into uploads and posts the message.
_post_image = None


def set_post_image(fn):
    global _post_image
    _post_image = fn


# main.py injects this: message id -> {data(base64), mimeType} for an image the
# caller is entitled to read.
_read_artifact = None


def set_read_artifact(fn):
    global _read_artifact
    _read_artifact = fn


def _do_list_images(cid: str) -> str:
    """Ids of the images and files in this conversation, so an agent can name one."""
    if not cid:
        return "No conversation to look at."
    try:
        rows = dbx.get_messages(cid, limit=40)
    except Exception:
        return "Couldn't read this conversation."
    items = [m for m in rows if m.get("kind") in ("image", "file")]
    if not items:
        return ("There are no images or files in this conversation yet. If you need "
                "one to work from, ask the user to upload it.")
    lines = [f"- {m['id']}  ({m.get('kind')}, {m.get('file_name') or 'unnamed'}, "
             f"from {m.get('sender_name') or 'someone'})" for m in items[:25]]
    return ("Images and files here, newest first. Use the id when editing:\n"
            + "\n".join(lines))


async def _do_edit_image(agent: dict, cid: str, image_id: str, changes: str,
                         caption: str) -> str:
    """Edit an existing image, working from the real bytes.

    Distinct from create_image on purpose. Generating from a description produces
    a plausible lookalike, NOT the product in the reference — claiming otherwise
    is how a wrong image reaches a listing.
    """
    text = (changes or "").strip()
    if len(text) < 8:
        return ("Say what to change, specifically — what, where, and how much. "
                "A vague instruction produces a vague edit.")
    if _read_artifact is None or _post_image is None or _client_llm is None:
        return "Image editing isn't wired up on this server."
    if not cid:
        return "No conversation to work in."

    owner_id = agent.get("owner_id") or ""
    if not owner_id:
        return "This agent has no owner, so there is no key to edit images with."
    if _has_local_client is not None and not _has_local_client(owner_id):
        owner = dbx.get_user(owner_id) or {}
        return (f"waiting_for_owner: editing uses {owner.get('name') or 'the owner'}'s "
                "key and their computer isn't connected. Tell the user that, and do "
                "NOT ask them for a key of their own.")

    # Authorisation: the image must be one in THIS conversation. An id from
    # somewhere else is not readable just because an agent named it.
    ref = await _read_artifact(cid, (image_id or "").strip())
    if not isinstance(ref, dict) or ref.get("error"):
        reason = ref.get("error") if isinstance(ref, dict) else "couldn't read it"
        return (f"Can't use that image: {reason}. Call list_images to see what is "
                "actually here, and use an id from that list.")

    try:
        res = await _client_llm(owner_id, "llm_image",
                                {"prompt": text, "reference": {
                                    "data": ref["data"], "mimeType": ref.get("mimeType")}})
    except Exception as e:
        return f"Couldn't reach the owner's computer to edit the image ({type(e).__name__})."
    if not isinstance(res, dict):
        return (f"The edit didn't happen: the image service sent back something "
                f"unexpected ({type(res).__name__}). Do not claim you edited anything.")
    if res.get("unsupported") or res.get("no_reference_support"):
        # Be explicit rather than quietly generating a lookalike from the text.
        return ("This image provider can't edit an existing image — only generate a "
                "new one from a description. Tell the user that editing is "
                "unavailable, and do NOT pretend a generated image preserves the "
                "product in their photo.")
    if res.get("error"):
        return (f"The edit didn't happen: {res['error']} Tell the user exactly this "
                "— do not claim you edited anything.")
    if not res.get("data"):
        return "No edited image came back. Tell the user, and do not pretend one exists."

    try:
        info = await _post_image(cid, agent, res["data"], res.get("mimeType", "image/png"),
                                 (caption or f"Edited: {text}")[:300])
    except Exception as e:
        return f"The edit was made but couldn't be posted ({type(e).__name__})."
    if not isinstance(info, dict) or not info.get("id"):
        return ("The edit was made but the post didn't confirm. Tell the user it may "
                "not have appeared, and do not claim it is in the chat.")
    return (f"Posted the edited image ({info.get('file_name', 'image')}), based on "
            f"{image_id}. Tell the user it's there and ask whether it needs more work.")


async def _do_create_image(agent: dict, cid: str, prompt: str, caption: str) -> str:
    """Generate an image on the OWNER's machine and post it into the chat.

    The owner's Gemini key does the work, exactly as their chat model does, so a
    colleague using a shared agent needs no key of their own.
    """
    text = (prompt or "").strip()
    if len(text) < 10:
        return ("Describe the image properly first — subject, style, lighting, "
                "composition. A one-word prompt gives a poor result.")
    if _post_image is None or _client_llm is None:
        return "Image creation isn't wired up on this server."

    owner_id = agent.get("owner_id") or ""
    if not owner_id:
        return "This agent has no owner, so there is no key to create images with."
    # Owner only, same as chat: never spend a colleague's key on a shared agent.
    if _has_local_client is not None and not _has_local_client(owner_id):
        owner = dbx.get_user(owner_id) or {}
        return (f"waiting_for_owner: images are made with {owner.get('name') or 'the owner'}'s "
                "key and their computer isn't connected. Tell the user that, and do "
                "NOT ask them for a key of their own.")

    try:
        res = await _client_llm(owner_id, "llm_image", {"prompt": text})
    except Exception as e:
        return f"Couldn't reach the owner's computer to create the image ({type(e).__name__})."
    # The bridge is meant to return a dict, but a malformed reply must not take
    # the whole turn down with an AttributeError. `(res or {}).get(...)` was NOT
    # safe: a non-empty string is truthy, so it reached .get() on a str.
    if not isinstance(res, dict):
        return (f"The image wasn't created: the image service sent back something "
                f"unexpected ({type(res).__name__}). Tell the user exactly this — "
                "do not claim you made an image.")
    if res.get("error"):
        return (f"The image wasn't created: {res['error']} "
                "Tell the user exactly this — do not claim you made an image.")
    if not res.get("data"):
        return "No image came back. Tell the user, and do not pretend one exists."

    try:
        info = await _post_image(cid, agent, res["data"], res.get("mimeType", "image/png"),
                                 (caption or text)[:300])
    except Exception as e:
        return f"The image was created but couldn't be posted ({type(e).__name__})."
    # Confirm something was actually written before saying so. A poster that
    # silently returns nothing would otherwise be reported as a success.
    if not isinstance(info, dict) or not info.get("id"):
        return ("The image was generated but the post didn't confirm. Tell the user "
                "it may not have appeared, and do not claim it is in the chat.")
    note = (res.get("note") or "").strip()
    return (f"Posted the image in this conversation ({info.get('file_name', 'image')}). "
            "Tell the user it's there and ask whether it needs changing."
            + (f" The generator also said: {note}" if note else ""))


async def _do_delegate_task(agent: dict, args: dict, ctx: dict) -> str:
    """Assign work to another agent and return a STRUCTURED result.

    Distinct from ask_agent on purpose. ask_agent relays a message and gets prose
    back; this states an objective, a deliverable and completion criteria, and
    reports status, artifacts and blockers. A written promise is not a result,
    so the brief asks for the thing itself.
    """
    run = ctx.get("run")
    others = [a for a in sibling_agents(agent)
              if a["id"] not in (run.chain if run else ())]
    if not others:
        return ("There is no other agent free to take this on — either you have none, "
                "or the ones you have are already working on this request. Say which "
                "kind of agent the user would need to create, and do the parts you can.")

    want = _norm(args.get("agent", ""))
    target = next((a for a in others if _norm(a["slug"]) == want or _norm(a["name"]) == want), None)
    if not target:
        target = next((a for a in others if want and (want in _norm(a["name"])
                                                      or want in _norm(a["slug"]))), None)
    if not target:
        # Name AND capabilities, so the next choice can be made on what agents can
        # do rather than on what they happen to be called.
        listing = "; ".join(
            f"{a['name']} (can: {', '.join(agent_capability_summary(a)) or 'reply only'})"
            for a in others)
        return (f"There is no agent called '{args.get('agent', '')}'. Available: {listing}. "
                "Pick one by what it can do, or tell the user which agent to create.")

    if run is not None:
        stop = run.spend_delegation()
        if stop:
            return stop
        if run.depth + 1 >= MAX_DELEGATION_DEPTH:
            return (f"You are {run.depth + 1} agents deep, which is the limit. Do this "
                    "yourself or report it as blocked.")
        if target["id"] in run.chain:
            return (f"{target['name']} is already working on this request further up "
                    "the chain, so it can't be asked again. Do it yourself or report "
                    "it as blocked.")

    brief = "\n".join(filter(None, [
        f"OBJECTIVE: {args.get('objective', '').strip()}",
        f"CONTEXT: {args.get('context', '').strip()}" if args.get("context") else "",
        f"CONSTRAINTS: {args.get('constraints', '').strip()}" if args.get("constraints") else "",
        f"DELIVERABLE: {args.get('deliverable', '').strip()}",
        f"DONE WHEN: {args.get('done_when', '').strip()}" if args.get("done_when") else "",
    ]))
    if run:
        run.note("delegate", f"{agent.get('name')} -> {target['name']}", target["name"])

    reply = await _relay_to_agent(target, agent.get("name", "another agent"), brief,
                                  ctx=ctx, is_task=True)
    reply = (reply or "").strip() or "(no reply)"

    # Say plainly what did and didn't arrive, so the coordinator can't report a
    # promise as a delivery.
    made = _artifacts_since(ctx, target)
    status = "completed" if made else "returned_text_only"
    lines = [f"RESULT from {target['name']}",
             f"status: {status}",
             f"summary: {reply[:1200]}"]
    if made:
        lines.append("artifacts: " + ", ".join(made))
    else:
        lines.append("artifacts: none — they returned words, not a file. If you asked "
                     "for something produced, it is NOT done. Say so, or ask again "
                     "being specific about the file you need.")
    return "\n".join(lines)


def agent_capability_summary(agent: dict) -> list[str]:
    """Plain-language list of what an agent may actually do, from its flags."""
    out = []
    if has_cap(agent, "web_scraping"):
        out.append("search the web")
    if has_cap(agent, "files"):
        out.append("workspace files")
    if has_cap(agent, "local_files"):
        out.append("your computer's files")
    if has_cap(agent, "multi_agent"):
        out.append("delegate to others")
    if agent_mcp(agent):
        out.append(f"{len(agent_mcp(agent))} connector(s)")
    if has_cap(agent, "tool_calling"):
        out.append("create images")
    return out


def _artifacts_since(ctx: dict, target: dict) -> list[str]:
    """Message ids this agent posted into the conversation during the delegation.

    Stable database ids, not positional labels like img1 — those shift the moment
    anyone else posts.
    """
    cid = ctx.get("cid", "")
    run = ctx.get("run")
    if not cid or run is None:
        return []
    started = run.events[0]["at"] if run.events else 0
    try:
        rows = dbx.get_messages(cid, limit=20)
    except Exception:
        return []
    return [m["id"] for m in rows
            if m.get("sender_id") == target.get("user_id")
            and m.get("kind") in ("image", "file")
            and m.get("created_at", 0) >= started]


def _do_list_people(agent: dict) -> str:
    people = owner_contacts(agent)
    others = sibling_agents(agent)
    lines = []
    if people:
        lines.append("People you can message: " + ", ".join(p["name"] for p in people))
    if others:
        lines.append("Agents you can ask: " + ", ".join(a["name"] for a in others))
    return "\n".join(lines) or "Nobody to message yet."


# main.py injects these so this module never imports it.
_share_file = None
_make_schedule = None


def set_share_file(fn):
    global _share_file
    _share_file = fn


def set_scheduler(fn):
    global _make_schedule
    _make_schedule = fn


async def _do_share_file(agent: dict, cid: str, path: str) -> str:
    target = _ws_path(path)
    if target is None:
        return "That path is outside the workspace, so it can't be shared."
    if not target.is_file():
        return f"There is no file at '{path}'. Write it with write_file first."
    if _share_file is None:
        return "Sharing files isn't wired up on this server."
    try:
        await _share_file(cid, agent, target)
    except Exception as e:
        return f"Couldn't share it ({type(e).__name__})."
    return (f"Posted {target.name} in this conversation. Tell the user it's there "
            "to download.")


def _do_schedule_reminder(agent: dict, cid: str, title: str, prompt: str, when: str) -> str:
    if _make_schedule is None:
        return "Scheduling isn't available on this server."
    m = re.match(r"^\s*(\d{1,2})\s*[:.]\s*(\d{2})\s*$", str(when or ""))
    if not m:
        return "Give the time as 24-hour HH:MM, e.g. 09:00 or 17:30. Nothing was scheduled."
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return f"'{when}' isn't a real time. Nothing was scheduled."
    if not (title or "").strip() or not (prompt or "").strip():
        return "A title and a prompt are both needed. Nothing was scheduled."
    try:
        _make_schedule(agent, cid, title.strip()[:80], prompt.strip()[:2000], hour, minute)
    except Exception as e:
        return f"Couldn't set that up ({type(e).__name__})."
    return (f"Set up '{title.strip()}' to run daily at {hour:02d}:{minute:02d} and post "
            "here. Tell the user, and mention they can change or pause it in the Agents "
            "panel.")


# ====================== the asker's own machine (desktop) ======================

# main.py injects this: fn(user_id, op, args) -> dict, round-tripping through that
# user's connected desktop client.
_local_fs = None

# main.py injects this too: fn(user_id) -> bool, "does this person have a client
# that can run local tools right now?"
_has_local_client = None


def set_local_client_check(fn):
    global _has_local_client
    _has_local_client = fn


def local_tools_available(requester_id: str) -> bool:
    """Whether to hand an agent the local-machine tools for this turn.

    Deliberately NOT a per-agent setting. The meaningful consent is the folder the
    person picks in the desktop app, plus the confirmation prompt on anything
    destructive — a checkbox on the agent adds a second thing to get wrong without
    adding safety, and it was the single most common cause of "the agent says it
    has no file access".
    """
    if not requester_id or _has_local_client is None:
        return False
    try:
        return bool(_has_local_client(requester_id))
    except Exception:
        return False


def set_local_fs(fn):
    global _local_fs
    _local_fs = fn


async def _do_local_fs(requester_id: str, op: str, args: dict) -> str:
    if not requester_id:
        return "I don't know whose computer to ask. Do not retry."
    if _local_fs is None:
        return "Local file access isn't enabled on this server."
    try:
        res = await _local_fs(requester_id, op, args)
    except TimeoutError:
        return ("Their computer didn't answer. They may have closed the desktop app. "
                "Tell them to open it and try again — do not retry by yourself.")
    except Exception as e:
        return f"Couldn't reach their computer ({type(e).__name__}). Do not retry."

    if not isinstance(res, dict):
        return "Their computer sent back something I couldn't read."
    # These strings get relayed to the user almost verbatim, so they are written in
    # the second person — an earlier version said "they haven't shared…" and the model
    # copied it straight into the chat, addressing the user in the third person.
    if res.get("error") == "no_desktop":
        return ("TELL THE USER, in your own words: reading files from their computer only "
                "works in the Sportstech desktop app, and they are currently on the web version. "
                "Address them as 'you'. Do NOT retry this tool.")

    if res.get("error") == "denied":
        return ("They declined the prompt, so nothing was opened. Accept that and move on — "
                "do NOT ask again or retry.")
    if res.get("error") == "outside":
        return ("TELL THE USER, addressing them as 'you': that location is outside your user "
                "folder, or it is a protected location such as a credentials store, so it was "
                "refused. Do NOT retry the same path.")
    if res.get("error"):
        return f"Their computer reported: {str(res['error'])[:200]}"
    return str(res.get("text") or "(empty)")[:12000]


# ============================== agent-to-agent ==============================

def sibling_agents(agent: dict) -> list[dict]:
    """The other agents this agent's owner can reach. Scoped to the owner so an
    agent can never reach into someone else's workspace."""
    owner_id = agent.get("owner_id")
    if not owner_id:
        return []
    return [a for a in dbx.my_agents(owner_id) if a["id"] != agent.get("id")]


def owner_contacts(agent: dict) -> list[dict]:
    """Humans the agent's owner already shares a conversation with. Scoping to
    existing contacts stops an agent cold-messaging arbitrary users."""
    owner_id = agent.get("owner_id")
    if not owner_id:
        return []
    seen, out = set(), []
    for conv in dbx.user_conversations(owner_id):
        for m in dbx.conversation_members(conv["id"]):
            if m["id"] in seen or m["id"] == owner_id or m.get("is_bot"):
                continue
            seen.add(m["id"])
            out.append(m)
    return out


# main.py injects this — agents.py must never import main (circular).
_send_dm = None


def set_dm_sender(fn):
    """fn(agent_user_id, owner_id, to_user_id, text) -> posts and broadcasts."""
    global _send_dm
    _send_dm = fn


async def _do_message_user(agent: dict, person: str, message: str) -> str:
    people = owner_contacts(agent)
    if not people:
        return ("Your owner has no contacts to message yet. Tell the user there is nobody "
                "to send this to.")

    want = _norm(person or "")
    target = next((p for p in people
                   if _norm(p["name"]) == want or _norm(p.get("email", "")) == want), None)
    if not target:
        target = next((p for p in people if want and want in _norm(p["name"])), None)
    if not target:
        names = ", ".join(p["name"] for p in people)
        return (f"There is no person called '{person}' in your owner's contacts. "
                f"People available: {names}. Say so rather than pretending you sent anything.")

    text = (message or "").strip()
    if not text:
        return "Nothing to send — decide what to say first, then call message_user again."
    if _send_dm is None:
        return "Messaging isn't wired up on this server, so nothing was sent."

    try:
        await _send_dm(agent["user_id"], agent["owner_id"], target["id"], text)
    except Exception as e:
        return f"Couldn't send it ({type(e).__name__}). Tell the user it failed."
    return (f"Sent to {target['name']}: \"{text}\"\n"
            f"It is now in your owner's chat with {target['name']}, shown as a message from "
            f"you ({agent.get('name', 'this agent')}), so both of them can see it. "
            "Confirm to the user that it went out and where it landed.")


# Live runs by id, so a cancel from the API reaches the RunContext the agents
# are actually holding. Cleared when the run finishes.
LIVE_RUNS: dict = {}


class _RunState:
    """The parts of a run that every agent in it must SHARE, by reference.

    Kept separate from RunContext because a child was previously a copy: cancel
    the parent after a child existed and the child never noticed, which is
    exactly when you most want cancellation to work.
    """

    __slots__ = ("run_id", "calls", "delegations", "max_calls", "max_delegations",
                 "deadline", "cancelled", "on_progress", "events")

    def __init__(self, max_calls, max_delegations, time_budget, on_progress):
        self.run_id = dbx.new_id()
        self.calls = 0
        self.delegations = 0
        self.max_calls = max_calls
        self.max_delegations = max_delegations
        self.deadline = time.time() + time_budget
        self.cancelled = False
        self.on_progress = on_progress
        self.events = []


class RunContext:
    """One shared budget and identity set for an entire request, however many
    agents it passes through.

    Limits are shared, not per agent. Before this, each delegated agent got a
    fresh allowance, so a chain of three could spend three times what one could.

    Identities stay separate on purpose:
      requester_id - the human who asked. Local-file access is THEIRS, and never
                     becomes the owner's because an agent delegated.
      owner        - whose API key pays; follows the agent being run, resolved at
                     the point of the call rather than carried here.
    """

    __slots__ = ("_st", "requester_id", "root_cid", "artifacts", "chain", "depth")

    def __init__(self, requester_id: str = "", root_cid: str = "",
                 artifacts: tuple = (), max_calls: int = 40,
                 max_delegations: int = 6, time_budget: float = 300.0,
                 on_progress=None, _state=None):
        self._st = _state or _RunState(max_calls, max_delegations, time_budget,
                                       on_progress)
        self.requester_id = requester_id
        self.root_cid = root_cid
        self.artifacts = tuple(artifacts)     # message ids this run may read
        self.chain: tuple = ()
        self.depth = 0

    # Shared state, read and written through the one object.
    @property
    def run_id(self): return self._st.run_id
    @property
    def calls(self): return self._st.calls
    @property
    def delegations(self): return self._st.delegations
    @property
    def max_calls(self): return self._st.max_calls
    @property
    def max_delegations(self): return self._st.max_delegations
    @property
    def events(self): return self._st.events
    @property
    def cancelled(self): return self._st.cancelled
    @cancelled.setter
    def cancelled(self, v): self._st.cancelled = bool(v)
    @property
    def deadline(self): return self._st.deadline
    @deadline.setter
    def deadline(self, v): self._st.deadline = v
    @property
    def on_progress(self): return self._st.on_progress

    def spend_call(self, tool_name: str) -> str:
        """Charge one tool call to the run. Returns a message for the model when
        the run is out of budget, or "" to proceed."""
        if self._st.cancelled:
            return "This run was cancelled. Stop and report that you were cancelled."
        if time.time() > self._st.deadline:
            return ("This run has taken too long and is out of time. Report what you "
                    "have so far and stop — do not start anything new.")
        self._st.calls += 1
        if self._st.calls > self._st.max_calls:
            return (f"This run has used its {self._st.max_calls} tool calls. Report "
                    "what you have and stop.")
        self.note("tool", tool_name)
        return ""

    def spend_delegation(self) -> str:
        if self._st.cancelled:
            return "This run was cancelled. Stop and report that you were cancelled."
        if self._st.delegations >= self._st.max_delegations:
            return (f"This run has already delegated {self._st.max_delegations} times, "
                    "which is its limit. Do the remaining work yourself or report "
                    "what is blocked.")
        self._st.delegations += 1
        return ""

    def child(self, agent_id: str) -> "RunContext":
        """A context for a delegated agent: SAME state object, deeper chain."""
        kid = RunContext(requester_id=self.requester_id, root_cid=self.root_cid,
                         artifacts=self.artifacts, _state=self._st)
        kid.chain = self.chain + (agent_id,)
        kid.depth = self.depth + 1
        return kid

    def cancel(self):
        """Stops this run and every agent in it, wherever they are."""
        self._st.cancelled = True

    def note(self, kind: str, detail: str, agent: str = ""):
        self._st.events.append({"at": time.time(), "kind": kind,
                                "detail": str(detail)[:200], "agent": agent})
        if self._st.on_progress:
            try:
                self._st.on_progress(kind, detail, agent)
            except Exception:
                pass     # progress reporting must never break the run


# How far a chain of agents may run: A asks B, B asks C, C asks D. Beyond this the
# next agent simply gets no ask_agent tool, so it must answer for itself.
MAX_DELEGATION_DEPTH = 3


async def _relay_to_agent(target: dict, sender_name: str, text: str,
                          chain: tuple = (), depth: int = 0,
                          requester_id: str = "", ctx: dict | None = None,
                          is_task: bool = False) -> str:
    """Deliver a message or a task to another agent and return its reply.

    Two things the previous version got wrong:

      * It framed everything as "an ordinary message, NOT a work assignment",
        which is right for a greeting and wrong for a brief. `is_task` picks
        the wording.
      * It dropped the conversation id, so a delegated agent lost image
        creation, file sharing and attachment reading — exactly the tools you
        delegate work in order to use. The cid is passed on now, but the
        REQUESTER is preserved: local-file access stays with the human who
        asked, and never becomes the owner's by way of a delegation.
    """
    owner = dbx.get_user(target["owner_id"])
    system = agent_system_prompt(target, owner["name"] if owner else "a user",
                                 has_web=_agent_has_web(target))

    run = (ctx or {}).get("run")
    child = run.child(target["id"]) if run is not None else None
    eff_depth = child.depth if child else depth + 1
    eff_chain = child.chain if child else chain + (target["id"],)
    # The originating conversation, so the delegate can post what it produces
    # where the person asking can actually see it.
    cid = (ctx or {}).get("cid", "")

    if eff_depth + 1 >= MAX_DELEGATION_DEPTH:
        system += ("\n\nYou are at the end of a chain of agents passing work along. "
                   "Do this yourself — you cannot pass it on again.")

    if is_task:
        msgs = [{"role": "user", "content":
                 f"{sender_name}, another agent in this workspace, has assigned you "
                 f"this task:\n\n{text}\n\n"
                 "Do the work now using your tools. If the deliverable is a file or "
                 "an image, PRODUCE IT — describing what you would make is not "
                 "completing the task. When you are done, reply with what you "
                 "produced and where it is. If you cannot do it, say exactly what "
                 "blocked you rather than approximating."}]
    else:
        msgs = [{"role": "user", "content":
                 f"{sender_name}, another agent in this workspace, sent you this message:\n\n"
                 f"{text}\n\n"
                 "Reply to it directly and naturally, the way you would to a person. This is an "
                 "ordinary message, NOT a work assignment — do not ask for 'subtask details' or "
                 "mention subtasks. If it is a greeting, greet them back."}]
    try:
        return await call_model(
            target, system, msgs, max_tokens=1200, cid=cid,
            requester_id=requester_id or (run.requester_id if run else ""),
            depth=eff_depth, chain=eff_chain, run=child)
    except Exception as e:
        # Include the message: "couldn't reach them: TypeError" told nobody
        # anything, and hid a real signature mismatch for an entire debug cycle.
        detail = str(e)[:200]
        if "waiting_for_owner" in detail:
            return f"(waiting_for_owner: {target['name']}'s owner is offline)"
        return f"(couldn't reach {target['name']}: {type(e).__name__}: {detail})"


async def _do_ask_agent(agent: dict, target_name: str, message: str,
                        chain: tuple = (), depth: int = 0,
                        requester_id: str = "") -> str:
    others = sibling_agents(agent)
    # An agent already in this chain must not be re-entered, or two agents can
    # bounce a task between them until something times out.
    others = [a for a in others if a["id"] not in chain]
    if not others:
        return ("There is no other agent available to pass this to — either you have "
                "none, or the ones you have are already working on this request. "
                "Tell the user plainly, and if a particular kind of agent would be "
                "needed, say which one they should create.")

    want = _norm(target_name or "")
    target = next((a for a in others
                   if _norm(a["slug"]) == want or _norm(a["name"]) == want), None)
    if not target:
        target = next((a for a in others
                       if want and (want in _norm(a["name"]) or want in _norm(a["slug"]))), None)
    if not target:
        names = ", ".join(a["name"] for a in others)
        return (f"There is no agent called '{target_name}'. Available agents: {names}. "
                "Tell the user which ones exist rather than pretending you sent anything.")

    text = (message or "").strip()
    if not text:
        return "Nothing to send — decide what to say first, then call ask_agent again."

    # Deliberately NOT run_subtask: its prompt frames everything as "your assigned
    # subtask", which made a plain "hi" come back as "I'm missing the details of my
    # subtask". A relayed message is just a message.
    reply = await _relay_to_agent(target, agent.get("name", "another agent"), text,
                                  chain=chain or (agent["id"],), depth=depth,
                                  requester_id=requester_id)
    reply = (reply or "").strip() or "(no reply)"
    return (f"You sent to {target['name']}: \"{text}\"\n"
            f"{target['name']} replied: {reply}\n\n"
            "Report this back to the user, quoting what the other agent actually said.")


# ============================== dispatch ==============================

async def _run_web_tool(name: str, args: dict, ctx: dict | None = None) -> str:
    """Execute one tool call. `ctx` carries the agent and conversation so chat-scoped
    tools (image, document, chat search) can only reach their own conversation.

    Every call is authorised here, not only at discovery. A model can name a tool
    it was never offered — from an earlier turn, from another agent's toolset, or
    invented outright — and before this check those names reached their handlers.
    """
    ctx = ctx or {}
    cid = ctx.get("cid", "")
    agent = ctx.get("agent") or {}
    run = ctx.get("run")

    ok, why = tool_allowed(agent, name)
    if not ok:
        return (f"You don't have the '{name}' tool — {why}. Tell the user that "
                "plainly; do not describe what you would have done with it.")

    # The exact list this turn was given. A name outside it was not offered,
    # whatever the model believes.
    offered = ctx.get("offered_tools")
    if offered is not None and name not in offered:
        return (f"'{name}' is not one of your tools this turn. Call list_my_tools "
                "to see what you actually have, and answer from that.")

    if run is not None:
        stop = run.spend_call(name)
        if stop:
            return stop
    if name == "web_search":
        return await _do_web_search(args.get("query", ""))
    if name == "fetch_url":
        return await _do_fetch_url(args.get("url", ""))
    if name == "analyze_image":
        if not cid:
            return "No conversation context for image analysis."
        return await _do_analyze_image(agent, cid, args.get("image_id", "img1"),
                                       args.get("question", ""))
    if name == "read_document":
        if not cid:
            return "No conversation context for document reading."
        return await _do_read_document(cid, args.get("doc_id", "doc1"), args.get("query", ""))
    if name == "calculate":
        return _do_calculate(args.get("expression", ""))
    if name == "current_time":
        return _do_current_time(args.get("timezone", ""))
    if name == "search_chat":
        if not cid:
            return "No conversation context for chat search."
        return _do_search_chat(cid, args.get("query", ""))
    if name == "ask_agent":
        return await _do_ask_agent(agent, args.get("agent", ""), args.get("message", ""),
                                   chain=ctx.get("chain") or (agent["id"],),
                                   depth=ctx.get("depth", 0),
                                   requester_id=ctx.get("requester", ""))
    if name == "message_user":
        return await _do_message_user(agent, args.get("person", ""), args.get("message", ""))
    if name == "list_files":
        return _do_list_files(args.get("folder", ""))
    if name == "read_file":
        return _do_read_file(args.get("path", ""), args.get("query", ""))
    if name == "write_file":
        return _do_write_file(args.get("path", ""), args.get("content", ""))
    if name == "list_my_files":
        return await _do_local_fs(ctx.get("requester"), "list", {"folder": args.get("folder", "")})
    if name == "read_my_file":
        return await _do_local_fs(ctx.get("requester"), "read",
                                  {"path": args.get("path", ""), "query": args.get("query", "")})
    if name == "open_on_my_computer":
        return await _do_local_fs(ctx.get("requester"), "open", {"path": args.get("path", "")})
    if name == "create_my_file":
        return await _do_local_fs(ctx.get("requester"), "create",
                                  {"path": args.get("path", ""), "content": args.get("content", "")})
    if name == "search_my_files":
        return await _do_local_fs(ctx.get("requester"), "search", {"term": args.get("term", "")})
    if name == "make_folder_on_my_computer":
        return await _do_local_fs(ctx.get("requester"), "mkdir", {"path": args.get("path", "")})
    if name == "delete_on_my_computer":
        return await _do_local_fs(ctx.get("requester"), "delete", {"path": args.get("path", "")})
    if name == "move_on_my_computer":
        return await _do_local_fs(ctx.get("requester"), "move",
                                  {"from": args.get("from", ""), "to": args.get("to", "")})
    if name == "my_computer_info":
        return await _do_local_fs(ctx.get("requester"), "sysinfo", {})
    if name == "open_app_on_my_computer":
        return await _do_local_fs(ctx.get("requester"), "openapp", {"name": args.get("name", "")})
    if name == "list_my_apps":
        return await _do_local_fs(ctx.get("requester"), "apps", {})
    if name == "open_url_on_my_computer":
        return await _do_local_fs(ctx.get("requester"), "openurl", {"url": args.get("url", "")})
    if name == "remember":
        return _do_remember(agent, args.get("topic", ""), args.get("note", ""))
    if name == "recall":
        return _do_recall(agent, args.get("topic", ""))
    if name == "forget":
        return _do_forget(agent, args.get("topic", ""))
    if name == "list_my_tools":
        return _do_list_my_tools(ctx)
    if name == "delegate_task":
        return await _do_delegate_task(agent, args, ctx)
    if name == "list_images":
        return _do_list_images(cid)
    if name == "edit_image":
        if not cid:
            return "No conversation to post the image into."
        return await _do_edit_image(agent, cid, args.get("image_id", ""),
                                    args.get("changes", ""), args.get("caption", ""))
    if name == "create_image":
        if not cid:
            return "No conversation to post the image into."
        return await _do_create_image(agent, cid, args.get("prompt", ""),
                                      args.get("caption", ""))
    if name == "list_people":
        return _do_list_people(agent)
    if name == "share_file_in_chat":
        if not cid:
            return "No conversation to post into."
        return await _do_share_file(agent, cid, args.get("path", ""))
    if name == "schedule_reminder":
        if not cid:
            return "No conversation to post the reminder into."
        return _do_schedule_reminder(agent, cid, args.get("title", ""),
                                     args.get("prompt", ""), args.get("time", ""))
    if name == "connect_connector":
        return await mcp.connect(agent, ctx.get("requester", ""), args.get("service", ""))
    routes = ctx.get("mcp_routes") or {}
    if name in routes:
        return await mcp.run_agent_tool(routes, name, args, ctx.get("requester", ""))
    return f"(unknown tool: {name})"


def tools_for(agent: dict, cid: str = "", requester_id: str = "",
              depth: int = 0, chain: tuple = ()) -> list[dict]:
    if not has_cap(agent, "tool_calling"):
        return []          # conversation only; the executor refuses the rest too
    """Assemble the toolset for one turn. Web tools follow the agent's capability
    flag; image/document tools only appear when the chat actually has attachments,
    which keeps the prompt small and stops the model inventing files."""
    tools = list(BASE_TOOLS)
    if agent_caps(agent).get("web_scraping"):
        if has_cap(agent, "web_scraping"):
            tools = WEB_TOOLS_OLLAMA + tools
    # Delegation is offered when the owner has another agent that is not already
    # in this chain, and the chain has room left. Depth is the guard now, not the
    # absence of a cid — that older rule made chained delegation impossible.
    reachable = [a for a in sibling_agents(agent) if a["id"] not in chain]
    if reachable and depth + 1 < MAX_DELEGATION_DEPTH and has_cap(agent, "multi_agent"):
        tools.append(ASK_AGENT_TOOL)
        tools.append(DELEGATE_TASK_TOOL)
    if cid and owner_contacts(agent):
        tools.append(MESSAGE_USER_TOOL)
    # Memory and scheduling are useful to every agent and need no configuration.
    tools += [REMEMBER_TOOL, RECALL_TOOL, FORGET_TOOL, MY_TOOLS_TOOL]
    # Image creation runs on the owner's Gemini key, so it needs a conversation
    # to post into and an owner whose machine can be reached.
    if cid and agent.get("owner_id"):
        tools.append(CREATE_IMAGE_TOOL)
        tools.append(EDIT_IMAGE_TOOL)
        tools.append(LIST_IMAGES_TOOL)
    if cid:
        tools.append(REMIND_TOOL)
    if cid and (owner_contacts(agent) or sibling_agents(agent)):
        tools.append(PEOPLE_TOOL)
    if agent_caps(agent).get("files"):
        tools += [LIST_FILES_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL]
        if cid:
            tools.append(SHARE_FILE_TOOL)
    # Offered whenever the person asking has the desktop app running. No agent
    # setting to forget, and no tools dangled at someone who can't use them.
    # Desktop connectivity is necessary but NOT sufficient: the agent must also
    # have been granted local_files. Before this, plugging in a desktop app
    # silently gave every agent access to that person's disk.
    if has_cap(agent, "local_files") and local_tools_available(requester_id):
        tools += [LOCAL_LIST_TOOL, LOCAL_READ_TOOL, OPEN_LOCAL_TOOL, CREATE_LOCAL_TOOL,
                  SEARCH_LOCAL_TOOL, MKDIR_LOCAL_TOOL, MOVE_LOCAL_TOOL,
                  DELETE_LOCAL_TOOL, SYSINFO_LOCAL_TOOL,
                  OPEN_APP_TOOL, LIST_APPS_TOOL, OPEN_URL_TOOL]
    if cid:
        index = attachment_index(cid)
        if any(k.startswith("img") for k in index):
            tools.append(VISION_TOOL)
        if any(k.startswith("doc") for k in index):
            tools.append(DOC_TOOL)
    return tools


def strip_think(text: str) -> str:
    """Qwen3 and other 'thinking' models wrap reasoning in <think>...</think>.
    Remove it so users only see the final answer (and empty-after-think isn't shown raw)."""
    if not text:
        return text
    cleaned = re.sub(r"(?is)<think>.*?</think>", "", text)
    # If the model left an unclosed <think> (streamed/cut off), drop everything after it.
    cleaned = re.sub(r"(?is)<think>.*$", "", cleaned)
    return cleaned.strip()


async def _ollama_preflight(url: str, model: str) -> None:
    """Fast check (5s) that the server is up and the model exists — turns a silent
    3-minute hang into an instant, clear error the user actually sees."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
    except httpx.ConnectError:
        raise RuntimeError(f"Ollama server at {url} is offline or unreachable — is it running?")
    except httpx.TimeoutException:
        raise RuntimeError(f"Ollama server at {url} didn't respond within 5s")
    except Exception as e:
        raise RuntimeError(f"Couldn't reach Ollama at {url} ({type(e).__name__})")
    # Ollama tags carry a :tag suffix; match with or without it.
    base = model.split(":")[0]
    if names and not any(n == model or n.split(":")[0] == base for n in names):
        raise RuntimeError(
            f"model '{model}' is not installed. Installed models: {', '.join(names) or '(none)'}. "
            f"Fix the agent's model name (run `ollama list`) or `ollama pull {model}`.")


# main.py injects this: fn(user_id, op, args) -> dict, over the user's WebSocket.
_client_llm = None


def set_client_llm(fn):
    global _client_llm
    _client_llm = fn


async def call_client_provider(agent: dict, system: str, messages: list[dict],
                               tools: list[dict] | None, requester_id: str,
                               max_tokens: int = 2000, on_tool=None,
                               ctx: dict | None = None, max_rounds: int = 6) -> str:
    """Run the tool loop for Claude or Gemini, executed on the OWNER's machine so
    the owner's personal API key is used and never reaches this server.

    The owner pays for the agent they created and shared: a colleague given access
    should be able to use it without holding a key of their own. If the owner's
    machine is unreachable we fall back to the asker's own key, and say clearly
    what happened if neither is available.

    Structurally identical to the Ollama loop: offer tools, execute whatever the
    model asks for, feed results back, and withhold tools on the final round so it
    has to commit to prose.
    """
    provider = agent.get("provider", "claude")
    if _client_llm is None:
        raise RuntimeError("client LLM bridge not configured")

    owner_id = agent.get("owner_id") or ""
    if not owner_id:
        raise RuntimeError(f"{provider} agents need an owner whose key can be used")

    # OWNER ONLY. There is no fallback to the asker's key: billing someone else
    # for an agent they were merely given access to is not ours to decide, and a
    # colleague should never be prompted for a key to use a shared agent.
    if _has_local_client is not None and not _has_local_client(owner_id):
        owner = dbx.get_user(owner_id) or {}
        err = RuntimeError(
            f"waiting_for_owner: this agent runs on {owner.get('name') or 'its owner'}'s "
            f"{provider} key, and their computer isn't connected. It will work again "
            "when they open the desktop app. Switch the agent to a workspace model if "
            "it needs to run unattended.")
        err.waiting_for_owner = True
        err.owner_id = owner_id
        raise err
    runner_id = owner_id


    convo = list(messages)
    use_tools = bool(tools)
    did_any_tool = False
    last = ""

    async def emit(**event):
        if on_tool:
            try:
                await on_tool(event)
            except Exception:
                pass

    for round_no in range(max_rounds):
        final_round = round_no == max_rounds - 1
        send_tools = use_tools and not final_round
        res = await _client_llm(runner_id, "llm_chat", {
            "provider": provider, "model": agent.get("model") or "",
            "system": system, "messages": convo,
            "tools": tools if send_tools else [], "maxTokens": max_tokens,
        })
        if not isinstance(res, dict):
            raise RuntimeError("their computer sent back something unreadable")
        if res.get("error"):
            raise RuntimeError(str(res["error"]))

        last = (res.get("content") or "").strip()
        calls = res.get("tool_calls") or []
        print(f"[{provider}] round {round_no+1}: tools_sent={send_tools} "
              f"calls={[ (c.get('function') or {}).get('name') for c in calls ]} "
              f"content_len={len(last)}", flush=True)

        if send_tools and calls:
            did_any_tool = True
            if last:
                convo.append({"role": "assistant", "content": last})
            for call in calls:
                fn = call.get("function") or {}
                tname = fn.get("name", "")
                targs = fn.get("arguments") or {}
                if isinstance(targs, str):
                    try:
                        targs = json.loads(targs)
                    except Exception:
                        targs = {}
                await emit(state="start", tool=tname, label=tool_label(tname),
                           detail=tool_detail(tname, targs))
                result = await _run_web_tool(tname, targs, ctx)
                await emit(state="done", tool=tname, label=tool_label(tname),
                           detail=tool_detail(tname, targs))
                convo.append({"role": "tool", "name": tname, "content": result[:6000]})
            convo.append({"role": "user", "content":
                          "Use the tool results above to answer my question now. "
                          "Write the final answer unless something essential is missing."})
            continue

        answer = strip_think(last)
        if answer:
            return answer
        if did_any_tool and not final_round:
            convo.append({"role": "user", "content":
                          "Please write the final answer now based on the results above."})
            continue
        return answer or "(empty reply)"

    return strip_think(last) or "I couldn't put together a clear answer."


async def call_ollama(base_url: str, model: str, system: str, messages: list[dict],
                      tools: list[dict] | None = None, max_rounds: int = 6,
                      on_tool=None, ctx: dict | None = None) -> str:
    """Call a user's local Ollama server. If `tools` is given, run a tool-calling loop so
    the model can actually search/read the web (Ollama only *requests* the calls; we run
    them here and feed the results back). On the final round we drop tools so the model is
    forced to produce a text answer from what it gathered instead of looping forever.

    `on_tool(event)` is an optional async callback used to show the user which tool the
    agent is running right now."""
    import httpx
    url = base_url.rstrip("/")
    if not url.startswith("http"):
        url = "http://" + url
    await _ollama_preflight(url, model)   # fail fast & clearly if server/model is wrong
    convo = [{"role": "system", "content": system}] + list(messages)
    use_tools = bool(tools)
    did_any_tool = False
    last_content = ""

    async def emit(**event):
        if on_tool:
            try:
                await on_tool(event)
            except Exception:
                pass          # the indicator must never break the actual reply

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            for round_no in range(max_rounds):
                # Last round (or once we've already gathered tool data): stop offering tools
                # so the model has to commit to a final written answer.
                final_round = round_no == max_rounds - 1
                send_tools = use_tools and not final_round
                payload = {"model": model, "messages": convo, "stream": False}
                if send_tools:
                    payload["tools"] = tools
                resp = await client.post(f"{url}/api/chat", json=payload)

                if resp.status_code == 404:
                    err = (resp.json().get("error") or "")[:120]
                    raise RuntimeError(f"model '{model}' is not installed on the Ollama server ({err})")
                # Older Ollama / non-tool models reject the `tools` field — degrade gracefully.
                if resp.status_code == 400 and send_tools and "tool" in resp.text.lower():
                    print(f"[ollama] model '{model}' rejected tools (400) — "
                          f"it likely doesn't support tool calling. Continuing without tools.",
                          flush=True)
                    use_tools = False
                    continue
                resp.raise_for_status()

                msg = (resp.json().get("message") or {})
                last_content = (msg.get("content") or "").strip()
                tool_calls = msg.get("tool_calls") or []
                print(f"[ollama] round {round_no+1}: tools_sent={send_tools} "
                      f"tool_calls={[ (tc.get('function') or {}).get('name') for tc in tool_calls ]} "
                      f"content_len={len(last_content)}", flush=True)

                if send_tools and tool_calls:
                    did_any_tool = True
                    convo.append(msg)  # the model's tool-call request
                    for tc in tool_calls:
                        fn = tc.get("function") or {}
                        tname = fn.get("name", "")
                        targs = fn.get("arguments") or {}
                        if isinstance(targs, str):
                            try:
                                targs = json.loads(targs)
                            except Exception:
                                targs = {}
                        await emit(state="start", tool=tname, label=tool_label(tname),
                                   detail=tool_detail(tname, targs))
                        result = await _run_web_tool(tname, targs, ctx)
                        await emit(state="done", tool=tname, label=tool_label(tname),
                                   detail=tool_detail(tname, targs))
                        print(f"[ollama]   executed {tname}({targs}) "
                              f"-> {result[:120]!r}", flush=True)
                        convo.append({"role": "tool", "name": tname,
                                      "content": result[:6000]})
                    # Nudge the model to now answer rather than search again.
                    convo.append({"role": "user", "content":
                                  "Use the tool results above to answer my question now. "
                                  "Do not call any more tools unless something essential is "
                                  "still missing — write the final answer."})
                    continue  # loop so the model can answer using the tool results

                answer = strip_think(last_content)
                if answer:
                    return answer
                # Empty this round — if we have tool data, loop once more to force text.
                if did_any_tool and not final_round:
                    convo.append({"role": "user", "content":
                                  "Please write the final answer now based on the results above."})
                    continue
                return answer or "(empty reply)"
    except httpx.ConnectError:
        raise RuntimeError(f"Ollama server at {url} is offline or unreachable")
    except httpx.TimeoutException:
        raise RuntimeError(f"Ollama server at {url} timed out")

    return strip_think(last_content) or (
        "I searched but couldn't pull together a clear answer. "
        "Try rephrasing, or ask about a more specific detail.")


async def call_model(agent: dict, system: str, messages: list[dict], max_tokens: int = 1500,
                     cid: str = "", on_tool=None, requester_id: str = "",
                     depth: int = 0, chain: tuple = (), run=None) -> str:
    """Dispatch to the agent's configured LLM backend, retrying once on transient failure."""
    if agent_is_echo(agent):
        last = messages[-1]["content"] if messages else ""
        return f"[echo-mode] Completed task: {last[-300:]}"

    provider = agent.get("provider", "claude")
    if provider in ("claude", "gemini"):
        caps = agent_caps(agent)
        tools = tools_for(agent, cid, requester_id, depth=depth, chain=chain)
        ctx = {"agent": agent, "cid": cid, "requester": requester_id,
               "depth": depth, "chain": chain or (agent["id"],), "run": run}
        try:
            mcp_schemas, mcp_routes, mcp_down = await mcp.tools_for_agent(agent, requester_id)
        except Exception as e:
            print(f"[mcp] discovery failed: {type(e).__name__}: {e}", flush=True)
            mcp_schemas, mcp_routes, mcp_down = [], {}, []
        # Tell the model which connectors exist but aren't working, so it says
        # "your Notion connector isn't signed in" rather than "I have no integrations".
        system = system + mcp.unavailable_note(mcp_down)
        tools = tools + mcp_schemas
        if mcp.agent_connectors(agent):
            tools = tools + [mcp.CONNECT_TOOL]
        ctx["mcp_routes"] = mcp_routes
        ctx["tool_schemas"] = tools
        # The exact names offered this turn; the executor refuses anything else.
        ctx["offered_tools"] = {t["function"]["name"] for t in tools}
        print(f"[agent:{agent.get('name')}] provider={provider} model={agent.get('model')} "
              f"tools={[t['function']['name'] for t in tools]}", flush=True)
        return await call_client_provider(agent, system, messages, tools, requester_id,
                                          max_tokens=max_tokens, on_tool=on_tool, ctx=ctx)

    if provider == "ollama":
        # Local models can't browse or see on their own — hand them our tools
        # (which we execute here) and let them decide what to call.
        caps = agent_caps(agent)
        tools = tools_for(agent, cid, requester_id, depth=depth, chain=chain)
        ctx = {"agent": agent, "cid": cid, "requester": requester_id,
               "depth": depth, "chain": chain or (agent["id"],), "run": run}

        # Connector tools are discovered live from each MCP server, so they can't be
        # built by the synchronous tools_for(). One unreachable connector is skipped
        # rather than costing the agent every other tool.
        try:
            mcp_schemas, mcp_routes, mcp_down = await mcp.tools_for_agent(agent, requester_id)
        except Exception as e:
            print(f"[mcp] discovery failed: {type(e).__name__}: {e}", flush=True)
            mcp_schemas, mcp_routes, mcp_down = [], {}, []
        # Tell the model which connectors exist but aren't working, so it says
        # "your Notion connector isn't signed in" rather than "I have no integrations".
        system = system + mcp.unavailable_note(mcp_down)
        tools = tools + mcp_schemas
        # Offer the sign-in tool whenever this agent has any connector, so a 401
        # can be resolved in the same conversation instead of dead-ending.
        if mcp.agent_connectors(agent):
            tools = tools + [mcp.CONNECT_TOOL]
        ctx["mcp_routes"] = mcp_routes
        ctx["tool_schemas"] = tools
        # The exact names offered this turn; the executor refuses anything else.
        ctx["offered_tools"] = {t["function"]["name"] for t in tools}

        # --- diagnostics: printed to the server console on every agent turn ---
        print(f"[agent:{agent.get('name')}] provider=ollama model={agent.get('model')} "
              f"caps={caps} tools={[t['function']['name'] for t in tools]}", flush=True)

        async def once(use_tools: bool):
            return await call_ollama(agent["ollama_url"], agent["model"], system, messages,
                                     tools=tools if use_tools else None,
                                     on_tool=on_tool, ctx=ctx)
        try:
            return await once(bool(tools))
        except RuntimeError:
            raise  # offline / model-missing — surface the clear message to the user
        except Exception as e:
            print(f"[agent:{agent.get('name')}] tool round failed ({type(e).__name__}: {e}) "
                  f"— retrying WITHOUT tools", flush=True)
            # Tool round may have failed (e.g. model doesn't support tools) — retry plain.
            try:
                return await once(False)
            except Exception:
                await asyncio.sleep(1.5)
                return await once(False)

    # ---- Claude path (only when an agent is configured with provider="claude") ----
    tools, mcp_servers = build_tools_and_mcp(agent)

    async def call_with(use_tools: bool, use_mcp: bool):
        return await call_claude(
            agent["api_key"], agent["model"], system, messages, max_tokens,
            tools=tools if use_tools else None,
            mcp_servers=mcp_servers if use_mcp else None,
        )

    try:
        return await call_with(True, True)
    except Exception as e:
        if mcp_servers:
            # A bad/unreachable connector URL shouldn't take down the whole reply —
            # degrade to web_search-only (or plain) and tell the owner what happened.
            try:
                reply = await call_with(True, False)
                return reply + (f"\n\n⚠️ (One of this agent's connectors failed to respond: "
                                 f"{type(e).__name__}. Check its MCP connector URLs.)")
            except Exception:
                pass
        # transient-failure retry as a last resort
        await asyncio.sleep(1.5)
        return await call_with(True, True)


def build_history(cid: str, agent_user_id: str) -> list[dict]:
    history = dbx.get_messages(cid, limit=MAX_HISTORY)
    history = [m for m in history if not m.get("deleted")]
    # Attachments become referenceable lines so the model knows they exist and can
    # name one in a tool call. Ids match attachment_index() — newest is img1/doc1.
    index = attachment_index(cid)
    ref_of = {}
    for key, m in index.items():
        ref_of[m["id"]] = key
    msgs = []
    for m in history:
        role = "assistant" if m["sender_id"] == agent_user_id else "user"
        kind = m.get("kind", "text")
        if kind == "text":
            body = m["content"]
        else:
            ref = ref_of.get(m["id"], "")
            what = "image" if ref.startswith("img") else "file"
            body = (f'[{what} attached: {m.get("file_name") or what}'
                    + (f' — refer to it as {ref}]' if ref else "]"))
        text = body if role == "assistant" else f'{m["sender_name"]}: {body}'
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(chat started)"})
    if msgs[-1]["role"] == "assistant":
        msgs.append({"role": "user", "content": "(continue)"})
    return msgs


def _agent_has_web(agent: dict) -> bool:
    return bool(agent_caps(agent).get("web_scraping"))


async def agent_reply(agent: dict, cid: str, on_tool=None, requester_id: str = "",
                      run=None) -> str:
    # One run per request, shared by every agent it reaches.
    if run is None:
        run = RunContext(requester_id=requester_id, root_cid=cid,
                         on_progress=(lambda k, d, a: on_tool(d) if on_tool else None))
    owner = dbx.get_user(agent["owner_id"])
    system = agent_system_prompt(agent, owner["name"] if owner else "a user",
                                 has_web=_agent_has_web(agent),
                                 has_media=bool(attachment_index(cid)))
    try:
        reply = await call_model(agent, system, build_history(cid, agent["user_id"]),
                                 cid=cid, on_tool=on_tool, requester_id=requester_id,
                                 run=run)
    except RuntimeError as e:
        return f"⚠️ {agent['name']}: {e}. The owner can fix this in the Agents panel."
    except Exception as e:
        detail = "API key" if agent.get("provider", "claude") == "claude" else "Ollama server URL"
        return (f"⚠️ {agent['name']} couldn't reach its AI brain ({type(e).__name__}). "
                f"The owner may need to check the {detail} in the Agents panel.")
    # Never return an empty string — that looks like the agent silently ignored the user.
    if not (reply or "").strip():
        return (f"⚠️ {agent['name']} produced an empty reply. If it uses web tools, the "
                f"lookup may have returned nothing — the owner can check its setup.")
    return reply


# ============================== team orchestration ==============================

def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def mentioned_agents(content: str, agents: list[dict]) -> list[dict]:
    """Forgiving matching: @slug, @Name, @name_with_underscores, any punctuation."""
    tokens = {_norm(t) for t in re.findall(r"@([\w.\-]+)", content)}
    if not tokens:
        return []
    out = []
    for a in agents:
        if _norm(a["slug"]) in tokens or _norm(a["name"]) in tokens:
            out.append(a)
    return out


def is_team_task(content: str) -> bool:
    low = content.lower()
    return any(m in low for m in TEAM_MENTIONS)


def strip_team_mention(content: str) -> str:
    out = content
    for m in TEAM_MENTIONS:
        out = re.sub(re.escape(m), "", out, flags=re.IGNORECASE)
    return out.strip()


async def plan_subtasks(coordinator: dict, agents: list[dict], task: str,
                        run=None) -> tuple[list[dict], list[str]]:
    """Split a task among the agents that are actually needed.

    Returns (plan, problems). Two changes from the previous version:

      * It no longer demands that EVERY agent receive work. Forcing a job onto
        an unrelated agent produces filler, and filler is worse than silence.
      * A plan it cannot parse is a failure, not a licence to send the whole
        task to everybody — which is what the old fallback did, multiplying the
        cost by the size of the team and producing N vague answers.

    Each entry may name dependencies, so work that needs an earlier result waits
    for it instead of running blind.
    """
    problems: list[str] = []
    roster = "\n".join(
        f"- {a['slug']}: {a['name']} — {a['description'] or 'general assistant'} "
        f"(can: {', '.join(agent_capability_summary(a)) or 'reply only'})"
        for a in agents)

    if agent_is_echo(coordinator):
        return [{"slug": a["slug"], "task": f"Part {i+1} of: {task}", "needs": []}
                for i, a in enumerate(agents)], problems

    prompt = (
        f"Split this task among the agents that are genuinely needed.\n\n"
        f"TASK: {task}\n\nAGENTS:\n{roster}\n\n"
        'Respond with ONLY a JSON array, no markdown:\n'
        '[{"slug": "<agent slug>", "task": "<specific subtask>", '
        '"needs": ["<slug whose result this depends on>"]}]\n'
        "Use ONLY agents whose capabilities the work requires — leaving an agent "
        "out is correct when it has nothing to contribute. Give an agent at most "
        "one subtask. Use \"needs\" only for genuine dependencies.")
    raw = await call_model(coordinator,
                           "You are a project coordinator. Respond only with valid JSON.",
                           [{"role": "user", "content": prompt}], max_tokens=800, run=run)
    raw = (raw or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [], ["The coordinator did not return a usable plan."]
    if not isinstance(parsed, list):
        return [], ["The coordinator's plan was not a list of subtasks."]

    valid = {a["slug"] for a in agents}
    plan, used = [], set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        slug, sub = item.get("slug"), (item.get("task") or "").strip()
        if slug not in valid:
            problems.append(f"Plan named an agent that doesn't exist: {slug!r}.")
            continue
        if not sub:
            problems.append(f"Plan gave {slug} no actual task.")
            continue
        if slug in used:
            problems.append(f"Plan gave {slug} more than one task; kept the first.")
            continue
        needs = [n for n in (item.get("needs") or []) if n in valid and n != slug]
        used.add(slug)
        plan.append({"slug": slug, "task": sub, "needs": needs})

    # A dependency on an agent that isn't in the plan can never be satisfied.
    in_plan = {p["slug"] for p in plan}
    for p in plan:
        missing = [n for n in p["needs"] if n not in in_plan]
        if missing:
            problems.append(f"{p['slug']} depended on {missing}, which nobody is doing; "
                            "ran it without waiting.")
            p["needs"] = [n for n in p["needs"] if n in in_plan]
    return plan, problems


def _order_plan(plan: list[dict]) -> tuple[list[list[dict]], list[str]]:
    """Group the plan into waves that can run in parallel.

    Independent work goes in the first wave; anything with dependencies waits
    for the wave that satisfies them. A cycle is reported rather than deadlocking.
    """
    remaining = {p["slug"]: p for p in plan}
    done: set = set()
    waves, problems = [], []
    while remaining:
        ready = [p for p in remaining.values() if all(n in done for n in p["needs"])]
        if not ready:
            problems.append("Plan had circular dependencies: "
                            + ", ".join(sorted(remaining)) + ". Ran them together.")
            waves.append(list(remaining.values()))
            break
        waves.append(ready)
        for p in ready:
            done.add(p["slug"])
            remaining.pop(p["slug"])
    return waves, problems


async def run_subtask(agent: dict, subtask: str, context: str, cid: str = "",
                      requester_id: str = "", run=None, upstream: str = "") -> dict:
    """Run one subtask and report a STRUCTURED result.

    Returns {status, summary, artifacts, error}. The previous version returned a
    string and swallowed failures into it, so a crashed subtask was indistinguishable
    from a completed one when the summary was assembled.
    """
    owner = dbx.get_user(agent["owner_id"])
    system = agent_system_prompt(agent, owner["name"] if owner else "a user",
                                 has_web=_agent_has_web(agent))
    body = (f"You are part of a team working on this overall task:\n{context}\n\n"
            f"YOUR PART: {subtask}\n\n")
    if upstream:
        body += f"Results from the work yours depends on:\n{upstream}\n\n"
    body += ("Do your part now with your tools. If the deliverable is a file or an "
             "image, produce it — describing it is not doing it. Report what you "
             "produced, or exactly what blocked you.")

    before = _now_ts()
    try:
        out = await call_model(agent, system, [{"role": "user", "content": body}],
                               max_tokens=1500, cid=cid, requester_id=requester_id,
                               run=run)
    except Exception as e:
        detail = str(e)
        status = "waiting_for_owner" if "waiting_for_owner" in detail else "failed"
        return {"agent": agent, "subtask": subtask, "status": status,
                "summary": "", "artifacts": [], "error": detail[:300]}

    text = (out or "").strip()
    arts = _artifacts_from(cid, agent, before)
    if not text and not arts:
        return {"agent": agent, "subtask": subtask, "status": "failed",
                "summary": "", "artifacts": [], "error": "produced nothing"}
    return {"agent": agent, "subtask": subtask,
            "status": "completed" if (arts or text) else "failed",
            "summary": text, "artifacts": arts, "error": ""}


def _now_ts() -> float:
    return time.time()


def _artifacts_from(cid: str, agent: dict, since: float) -> list[str]:
    """Stable message ids this agent posted since `since`. Ids, not positions."""
    if not cid:
        return []
    try:
        rows = dbx.get_messages(cid, limit=30)
    except Exception:
        return []
    return [m["id"] for m in rows
            if m.get("sender_id") == agent.get("user_id")
            and m.get("kind") in ("image", "file")
            and (m.get("created_at") or 0) >= since]


def make_zip(cid: str, task: str, results: list[tuple[dict, str, str]]) -> tuple[str, str, int]:
    """Write each agent's output to a file and zip them. Returns (url, filename, size)."""
    fid = dbx.new_id()
    zip_name = "team_output.zip"
    zip_path = UPLOADS / f"{fid}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("TASK.md", f"# Team task\n\n{task}\n")
        for agent, subtask, output in results:
            fname = f"{agent['slug']}.md"
            z.writestr(fname, f"# {agent['name']}\n\n**Subtask:** {subtask}\n\n---\n\n{output}\n")
    size = zip_path.stat().st_size
    return f"/files/{fid}.zip", zip_name, size


async def run_team_task(agents: list[dict], task: str, cid: str = "",
                        requester_id: str = "", run=None
                        ) -> tuple[str, tuple[str, str, int] | None, dict]:
    """Plan, run in dependency order, and report honestly.

    Returns (summary, zip_info|None, result). `result["status"]` is one of
    completed / partially_completed / failed / waiting_for_owner / cancelled —
    the old version announced success regardless of what actually happened.
    """
    if run is None:
        run = RunContext(requester_id=requester_id, root_cid=cid)
    coordinator = agents[0]
    by_slug = {a["slug"]: a for a in agents}

    plan, problems = await plan_subtasks(coordinator, agents, task, run=run)
    if not plan:
        # No usable plan. Previously this sent the whole task to everyone, which
        # multiplies the cost by the team size and returns N vague answers.
        msg = ("I couldn't produce a workable plan for that. "
               + (" ".join(problems) if problems else "")
               + " Tell me which agent should do what, or narrow the task.")
        return msg, None, {"status": "failed", "problems": problems, "results": []}

    waves, order_problems = _order_plan(plan)
    problems += order_problems

    results: list[dict] = []
    done_text: dict[str, str] = {}
    for wave in waves:
        if run.cancelled:
            problems.append("Cancelled part-way through.")
            break
        # Only genuinely independent work runs together.
        upstream = {p["slug"]: "\n\n".join(
            f"[{n}] {done_text.get(n, '(nothing)')[:600]}" for n in p["needs"])
            for p in wave}
        batch = await asyncio.gather(*[
            run_subtask(by_slug[p["slug"]], p["task"], task, cid=cid,
                        requester_id=requester_id, run=run,
                        upstream=upstream.get(p["slug"], ""))
            for p in wave], return_exceptions=True)
        for p, r in zip(wave, batch):
            if isinstance(r, BaseException):
                r = {"agent": by_slug[p["slug"]], "subtask": p["task"],
                     "status": "failed", "summary": "", "artifacts": [],
                     "error": f"{type(r).__name__}: {r}"[:300]}
            results.append(r)
            if r["status"] == "completed":
                done_text[p["slug"]] = r["summary"]

    ok = [r for r in results if r["status"] == "completed"]
    waiting = [r for r in results if r["status"] == "waiting_for_owner"]
    bad = [r for r in results if r["status"] == "failed"]

    if run.cancelled:
        status = "cancelled"
    elif waiting and not bad and not ok:
        status = "waiting_for_owner"
    elif bad and ok:
        status = "partially_completed"
    elif bad and not ok:
        status = "failed"
    else:
        status = "completed"

    headline = {"completed": "Team task complete",
                "partially_completed": "Team task partly complete",
                "failed": "Team task failed",
                "waiting_for_owner": "Waiting for an agent owner to come online",
                "cancelled": "Team task cancelled"}[status]
    lines = [f"**{headline}** — {len(ok)} of {len(results)} parts finished.\n"]
    for r in results:
        mark = {"completed": "done", "failed": "FAILED",
                "waiting_for_owner": "waiting"}.get(r["status"], r["status"])
        preview = (r["summary"] or r["error"] or "")[:400]
        art = f"  [{len(r['artifacts'])} file(s)]" if r["artifacts"] else ""
        lines.append(f"— **{r['agent']['name']}** ({mark}){art}: {preview}")
    if problems:
        lines.append("\nPlanning notes: " + " ".join(problems))
    summary = "\n".join(lines)

    # Only package REAL artifacts. A zip of text descriptions presented as
    # finished image work is the thing that makes a failure look like a success.
    artifacts = [a for r in results for a in r["artifacts"]]
    zip_info = None
    if artifacts:
        zip_info = make_zip(cid, task, [(r["agent"], r["subtask"], r["summary"])
                                        for r in ok])
        summary += f"\n\n{len(artifacts)} produced file(s) are in this conversation."
    elif len(ok) >= 2:
        zip_info = make_zip(cid, task, [(r["agent"], r["subtask"], r["summary"])
                                        for r in ok])
        summary += ("\n\nThe written output is attached. Note: no images or files "
                    "were produced — this is text only.")

    return summary, zip_info, {"status": status, "problems": problems,
                               "results": [{k: v for k, v in r.items() if k != "agent"}
                                           | {"agent": r["agent"]["name"]}
                                           for r in results],
                               "artifacts": artifacts}