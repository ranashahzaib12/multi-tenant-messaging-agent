"""Answers from the database: the user's own legacy records (health log,
personas, past reminders, past chats) for everyone, and the business tables
(leads, products, knowledge base) for the owner numbers only."""
import asyncio
import json
from datetime import datetime
from decimal import Decimal

from claude_agent_sdk import tool, SdkMcpTool

from src import database as db

LOG_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "description": "Filter by kind, e.g. bp, weight, sleep, meds, gym. Omit for all."},
        "days": {"type": "integer", "description": "How many days back. Default 30."},
    },
    "required": [],
}
PERSONA_SCHEMA = {
    "type": "object",
    "properties": {"slug": {"type": "string", "description": "Persona slug from my_personas."}},
    "required": ["slug"],
}
PAST_REMINDERS_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "description": "e.g. pending, done. Omit for all."}},
    "required": [],
}
CHAT_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "Words to look for in earlier conversations."}},
    "required": ["query"],
}
EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": []}
QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": "One SELECT over leads, products or documents. Aggregate where you can; rows are capped at 50.",
        },
    },
    "required": ["sql"],
}


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    if isinstance(v, Decimal):
        return float(v)
    return v


def _rows(rows: list[dict], max_chars: int = 6000) -> str:
    text = json.dumps(
        [{k: _jsonable(v) for k, v in r.items() if k != "embedding"} for r in rows],
        ensure_ascii=False,
        default=str,
    )
    return text if len(text) <= max_chars else text[:max_chars] + " …[truncated]"


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


def build_data_tools(user_phone: str, legacy_user_id: str | None, owner: bool) -> list[SdkMcpTool]:
    """Per-user closure. The business tools are only built for owner numbers,
    so for everyone else they don't exist rather than merely refusing."""
    no_history = "This number has no records from before the move."

    @tool("my_health_log", "The user's own log entries from before (blood pressure, weight, sleep, meds, gym…).", LOG_SCHEMA)
    async def my_health_log(args: dict) -> dict:
        if not legacy_user_id:
            return _text(no_history)
        rows = await asyncio.to_thread(db.list_cue_logs, legacy_user_id, args.get("kind"), int(args.get("days") or 30), 100)
        return _text(_rows(rows) if rows else "No log entries in that window.")

    @tool("my_personas", "The user's saved personas/modes (name, slug, description).", EMPTY_SCHEMA)
    async def my_personas(args: dict) -> dict:
        if not legacy_user_id:
            return _text(no_history)
        rows = await asyncio.to_thread(db.list_cue_personas, legacy_user_id)
        return _text(_rows(rows) if rows else "No personas saved.")

    @tool("read_persona", "Full text of one of the user's personas, by slug.", PERSONA_SCHEMA)
    async def read_persona(args: dict) -> dict:
        if not legacy_user_id:
            return _text(no_history)
        row = await asyncio.to_thread(db.get_cue_persona, legacy_user_id, args["slug"])
        if not row:
            return _text("No persona with that slug.")
        return _text(f"{row['name']} ({row['slug']}): {row['description']}\n\n{row['content'][:12000]}")

    @tool("my_past_reminders", "Reminders the user set before the move (done and pending), newest first.", PAST_REMINDERS_SCHEMA)
    async def my_past_reminders(args: dict) -> dict:
        if not legacy_user_id:
            return _text(no_history)
        rows = await asyncio.to_thread(db.list_cue_reminders, legacy_user_id, args.get("status"), 50)
        return _text(_rows(rows) if rows else "No earlier reminders.")

    @tool("search_past_chats", "Search what the user and the assistant said in earlier conversations (before the move).", CHAT_SEARCH_SCHEMA)
    async def search_past_chats(args: dict) -> dict:
        rows = await asyncio.to_thread(db.search_cue_chat_history, user_phone, args["query"], 20)
        return _text(_rows(rows) if rows else "Nothing in earlier conversations matches that.")

    tools = [my_health_log, my_personas, read_persona, my_past_reminders, search_past_chats]
    if not owner:
        return tools

    @tool("describe_business_data", "Tables and columns of the business database (leads, products, knowledge base). Call before writing SQL.", EMPTY_SCHEMA)
    async def describe_business_data(args: dict) -> dict:
        try:
            schema = await asyncio.to_thread(db.describe_business_tables)
        except Exception as e:
            return _text(f"Business data is not available right now: {e}")
        lines = [f"{t}: " + ", ".join(cols) for t, cols in schema.items()]
        lines.append(
            "Notes: leads = WhatsApp sales conversations (status, interest_level, purchase_status, timestamps); "
            "products = catalogue (content, price as text, metadata.url); "
            "documents = product Q&A knowledge base (content, metadata.category)."
        )
        return _text("\n".join(lines))

    @tool("query_business_data", "Run one read-only SELECT over the business tables and get rows back (max 50).", QUERY_SCHEMA)
    async def query_business_data(args: dict) -> dict:
        try:
            rows = await asyncio.to_thread(db.run_business_query, args["sql"], 50)
        except Exception as e:
            return _text(f"Query failed: {str(e).splitlines()[0][:300]}")
        return _text(_rows(rows) if rows else "No rows.")

    return tools + [describe_business_data, query_business_data]


DATA_TOOL_NAMES = [
    f"mcp__data__{n}" for n in ("my_health_log", "my_personas", "read_persona", "my_past_reminders", "search_past_chats")
]
BUSINESS_TOOL_NAMES = ["mcp__data__describe_business_data", "mcp__data__query_business_data"]
