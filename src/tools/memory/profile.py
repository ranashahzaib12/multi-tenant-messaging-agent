"""What the assistant remembers about its user: name, age, weight, family,
preferences, timezone — anything they say about themselves. Stored per
phone number in agent_users.profile and injected into every turn's prompt,
so the assistant never has to ask twice."""
import asyncio
from zoneinfo import ZoneInfo

from claude_agent_sdk import tool, SdkMcpTool

from src import database as db

REMEMBER_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "object",
            "description": (
                "Facts about the user as short snake_case keys with plain-text values, e.g. "
                "{\"name\": \"Rich\", \"age\": \"52\", \"weight_kg\": \"84\", \"partner\": \"Mei\", "
                "\"timezone\": \"Asia/Singapore\", \"coffee\": \"flat white, no sugar\"}. "
                "A key that already exists is overwritten."
            ),
            "additionalProperties": {"type": "string"},
        }
    },
    "required": ["facts"],
}
FORGET_SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string", "description": "The fact's key to remove."}},
    "required": ["key"],
}
RECALL_SCHEMA = {"type": "object", "properties": {}, "required": []}


def format_profile(profile: dict) -> str:
    """Prompt-ready block. Empty string when nothing is known."""
    lines = []
    if profile.get("name"):
        lines.append(f"name: {profile['name']}")
    if profile.get("timezone"):
        lines.append(f"timezone: {profile['timezone']}")
    for k, v in sorted(profile.get("facts", {}).items()):
        if k in ("name", "timezone") or k.startswith("_") or not str(v).strip():
            continue
        lines.append(f"{k.replace('_', ' ')}: {v}")
    core = (profile.get("core_prompt") or "").strip()
    block = "\n".join(f"- {l}" for l in lines)
    if core:
        block += ("\n" if block else "") + "Their standing instructions to you (set up earlier):\n" + core[:3000]
    return block


def _remember(user_id: str, facts: dict) -> str:
    clean = {
        str(k).strip().lower().replace(" ", "_"): str(v).strip()
        for k, v in facts.items()
        if str(v).strip() and not str(k).strip().startswith("_")
    }
    if not clean:
        return "Nothing to save."
    if "timezone" in clean:
        try:
            ZoneInfo(clean["timezone"])
        except Exception:
            return f"'{clean['timezone']}' is not a valid IANA timezone (use e.g. Asia/Singapore or Asia/Karachi)."
    try:
        db.remember_facts(user_id, clean)
    except Exception as e:
        return f"Error saving: {e}"
    return "Saved: " + "; ".join(f"{k}={v}" for k, v in clean.items())


def _forget(user_id: str, key: str) -> str:
    try:
        ok = db.forget_fact(user_id, key.strip().lower().replace(" ", "_"))
    except Exception as e:
        return f"Error: {e}"
    return f"Forgot '{key}'." if ok else f"Nothing saved under '{key}'."


def _recall(user_id: str) -> str:
    try:
        block = format_profile(db.get_profile(user_id))
    except Exception as e:
        return f"Error: {e}"
    return block or "Nothing is saved about the user yet."


def build_memory_tools(user_id: str) -> list[SdkMcpTool]:
    @tool("remember", "Save facts the user shared about themselves. Call it the moment they mention one.", REMEMBER_SCHEMA)
    async def remember(args: dict) -> dict:
        result = await asyncio.to_thread(_remember, user_id, args.get("facts") or {})
        return {"content": [{"type": "text", "text": result}]}

    @tool("forget", "Remove one saved fact by key.", FORGET_SCHEMA)
    async def forget(args: dict) -> dict:
        result = await asyncio.to_thread(_forget, user_id, args["key"])
        return {"content": [{"type": "text", "text": result}]}

    @tool("recall", "Show everything saved about the user.", RECALL_SCHEMA)
    async def recall(args: dict) -> dict:
        result = await asyncio.to_thread(_recall, user_id)
        return {"content": [{"type": "text", "text": result}]}

    return [remember, forget, recall]


MEMORY_TOOL_NAMES = ["mcp__memory__remember", "mcp__memory__forget", "mcp__memory__recall"]
