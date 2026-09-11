"""Nova — Sportstech's own in-app AI assistant (an original agent; not connected to any
external "Buzz" product — just inspired by the idea of an AI member in the chat).

Replies:
  - always, in a DM with Nova
  - when mentioned with @nova (or @ai) in groups/channels

Brain: Anthropic API if ANTHROPIC_API_KEY is set, otherwise an offline fallback
so local/LAN testing works without any keys or internet.
"""
import os

import database as dbx

BOT_EMAIL = "nova@sportstech.local"
BOT_NAME = "Nova AI"
MENTIONS = ("@nova", "@ai")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

_client = None
if ANTHROPIC_API_KEY:
    try:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    except Exception:
        _client = None

SYSTEM_PROMPT = (
    "You are Nova, a friendly AI member of a chat app called Sportstech. "
    "You chat naturally like a helpful teammate. Keep replies short and conversational "
    "(1-3 sentences unless asked for more). You can help plan things, answer questions, "
    "summarize the chat, and keep the vibe positive."
)


def ensure_bot_user() -> dict:
    return dbx.upsert_user(BOT_EMAIL, BOT_NAME, avatar="", title="AI Assistant", is_bot=True)


def should_reply(conversation: dict, content: str, bot_id: str) -> bool:
    if conversation["type"] == "dm" and dbx.is_member(conversation["id"], bot_id):
        return True
    low = content.lower()
    return any(m in low for m in MENTIONS)


async def generate_reply(conversation_id: str, bot_id: str) -> str:
    history = dbx.get_messages(conversation_id, limit=20)
    history = [m for m in history if not m.get("deleted") and m.get("kind", "text") == "text"]
    if _client:
        msgs = []
        for m in history:
            role = "assistant" if m["sender_id"] == bot_id else "user"
            text = m["content"] if role == "assistant" else f'{m["sender_name"]}: {m["content"]}'
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + text
            else:
                msgs.append({"role": role, "content": text})
        if not msgs or msgs[0]["role"] != "user":
            msgs.insert(0, {"role": "user", "content": "(chat started)"})
        if msgs[-1]["role"] == "assistant":
            msgs.append({"role": "user", "content": "(continue)"})
        try:
            resp = await _client.messages.create(
                model=ANTHROPIC_MODEL, max_tokens=400,
                system=SYSTEM_PROMPT, messages=msgs,
            )
            return "".join(b.text for b in resp.content if b.type == "text").strip() or "✨ ..."
        except Exception as e:
            return f"✨ Nova here — my AI brain hit an error ({type(e).__name__}). Check the API key/config."
    # Offline fallback for local/LAN testing without an API key
    last = history[-1]["content"] if history else ""
    return (
        f"✨ Nova here! I got your message: \"{last[:120]}\". "
        "I'm running in offline mode — set ANTHROPIC_API_KEY in .env to unlock my full AI replies."
    )