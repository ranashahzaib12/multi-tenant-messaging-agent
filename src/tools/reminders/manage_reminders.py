"""Reminders and proactive follow-ups, stored per user in agent_reminders
and delivered by the scheduler loop in app.py."""
import asyncio
import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from claude_agent_sdk import tool, SdkMcpTool

from src import database as db

DEFAULT_TZ = "Asia/Singapore"


def user_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)
RECURRENCES = ("none", "interval", "daily", "weekly", "monthly")
MIN_INTERVAL_MINUTES = 10
KINDS = ("reminder", "followup")

CREATE_REMINDER_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "For kind 'reminder': what to remind the user about, in their words. "
                "For kind 'followup': the instruction to carry out at that time, e.g. "
                "'Check Gmail for a reply from Sarah about the contract; if none, tell the user and offer to send a nudge.'"
            ),
        },
        "due_at": {
            "type": "string",
            "description": "When to fire, ISO 8601 with a UTC offset, e.g. 2026-09-24T09:00:00+08:00.",
        },
        "kind": {
            "type": "string",
            "enum": list(KINDS),
            "description": "'reminder' just nudges the user. 'followup' first does the work in text (checks email, calendar, the web), then messages the user with the outcome. Default 'reminder'.",
        },
        "recurrence": {
            "type": "string",
            "enum": list(RECURRENCES),
            "description": "Repeat schedule. Default 'none'. 'interval' repeats every interval_minutes until the user stops it.",
        },
        "interval_minutes": {
            "type": "integer",
            "description": "With recurrence 'interval': minutes between repeats, minimum 10.",
        },
    },
    "required": ["text", "due_at"],
}

STOP_REMINDERS_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "enum": ["repeating", "all"],
            "description": "'repeating' stops only repeating reminders (default); 'all' cancels every upcoming reminder.",
        }
    },
    "required": [],
}

LIST_REMINDERS_SCHEMA = {"type": "object", "properties": {}, "required": []}

CANCEL_REMINDER_SCHEMA = {
    "type": "object",
    "properties": {"reminder_id": {"type": "integer", "description": "The reminder's id from list_reminders."}},
    "required": ["reminder_id"],
}


def _parse_due(value: str, tz: ZoneInfo) -> datetime:
    due = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if due.tzinfo is None:
        # The model was told to include an offset; if it didn't, the user's
        # local time is the only sensible reading.
        due = due.replace(tzinfo=tz)
    return due


def next_due(due: datetime, recurrence: str, tz: ZoneInfo) -> datetime | None:
    """Next occurrence after `due`, stepping in the user's timezone so a
    9:00 reminder stays at 9:00 local. Returns None for one-off reminders.
    `every:N` repeats every N minutes (N >= 10, clamped here as well as at
    creation so a repeating reminder can never turn into a flood)."""
    if recurrence == "none":
        return None
    local = due.astimezone(tz)
    if recurrence.startswith("every:"):
        minutes = max(int(recurrence.split(":", 1)[1]), MIN_INTERVAL_MINUTES)
        nxt = local + timedelta(minutes=minutes)
    elif recurrence == "daily":
        nxt = local + timedelta(days=1)
    elif recurrence == "weekly":
        nxt = local + timedelta(weeks=1)
    elif recurrence == "monthly":
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        day = min(local.day, calendar.monthrange(year, month)[1])
        nxt = local.replace(year=year, month=month, day=day)
    else:
        return None
    # A reminder that fires late (app was down) must not replay every missed slot.
    now = datetime.now(tz)
    while nxt <= now:
        nxt = next_due(nxt, recurrence, tz)
    return nxt


def _fmt(due: datetime, tz: ZoneInfo) -> str:
    return due.astimezone(tz).strftime("%a %d %b %Y, %H:%M")


def describe_recurrence(recurrence: str) -> str:
    if recurrence.startswith("every:"):
        return f"every {recurrence.split(':', 1)[1]} minutes"
    return recurrence


def _create(user_id: str, tz: ZoneInfo, text: str, due_at: str, kind: str, recurrence: str, interval_minutes: int | None) -> str:
    if kind not in KINDS or recurrence not in RECURRENCES:
        return f"Invalid kind or recurrence. kind: {KINDS}, recurrence: {RECURRENCES}."
    if recurrence == "interval":
        if not interval_minutes or interval_minutes < MIN_INTERVAL_MINUTES:
            return f"Repeating reminders can't be more frequent than every {MIN_INTERVAL_MINUTES} minutes. Ask the user for a longer interval."
        recurrence = f"every:{int(interval_minutes)}"
    try:
        due = _parse_due(due_at, tz)
    except ValueError:
        return f"Could not parse due_at '{due_at}'. Use ISO 8601 with an offset, e.g. 2026-09-24T09:00:00+08:00."
    if due <= datetime.now(tz):
        return f"That time ({_fmt(due, tz)}) is in the past. Ask the user for a future time."
    try:
        reminder_id = db.create_reminder(user_id, kind, text.strip(), due, recurrence)
    except Exception as e:
        return f"Error saving reminder: {e}"
    repeat = "" if recurrence == "none" else f", repeats {describe_recurrence(recurrence)} until the user says stop"
    return f"Scheduled {kind} [id: {reminder_id}] for {_fmt(due, tz)}{repeat}: {text.strip()}"


def _list(user_id: str, tz: ZoneInfo) -> str:
    try:
        rows = db.list_reminders(user_id)
    except Exception as e:
        return f"Error listing reminders: {e}"
    if not rows:
        return "No upcoming reminders."
    lines = []
    for r in rows:
        repeat = "" if r["recurrence"] == "none" else f" (repeats {describe_recurrence(r['recurrence'])})"
        lines.append(f"- [id: {r['id']}] {_fmt(r['due_at'], tz)}{repeat} — {r['kind']}: {r['text']}")
    return f"{len(rows)} upcoming:\n" + "\n".join(lines)


def _cancel(user_id: str, tz: ZoneInfo, reminder_id: int) -> str:
    try:
        row = db.cancel_reminder(user_id, reminder_id)
    except Exception as e:
        return f"Error cancelling reminder: {e}"
    if not row:
        return f"No pending reminder with id {reminder_id}."
    return f"Cancelled reminder {row['id']} ({_fmt(row['due_at'], tz)}): {row['text']}"


def _stop(user_id: str, scope: str) -> str:
    try:
        n = db.stop_reminders(user_id, repeating_only=(scope != "all"))
    except Exception as e:
        return f"Error stopping reminders: {e}"
    what = "repeating reminders" if scope != "all" else "upcoming reminders"
    return f"Stopped {n} {what}." if n else f"No {what} to stop."


def build_reminder_tools(user_id: str, timezone: str | None = None) -> list[SdkMcpTool]:
    """Per-user tools via closure, same reason as build_notes_tools. The
    timezone is the user's own (from their profile), so "9am" is their 9am."""
    tz = user_tz(timezone)

    @tool(
        "create_reminder",
        "Schedule a reminder or a proactive follow-up for the user at a future time. Call it as soon as you know what and when.",
        CREATE_REMINDER_SCHEMA,
    )
    async def create_reminder(args: dict) -> dict:
        result = await asyncio.to_thread(
            _create, user_id, tz, args["text"], args["due_at"], args.get("kind") or "reminder",
            args.get("recurrence") or "none", args.get("interval_minutes"),
        )
        return {"content": [{"type": "text", "text": result}]}

    @tool("list_reminders", "List the user's upcoming reminders and follow-ups.", LIST_REMINDERS_SCHEMA)
    async def list_reminders(args: dict) -> dict:
        result = await asyncio.to_thread(_list, user_id, tz)
        return {"content": [{"type": "text", "text": result}]}

    @tool("cancel_reminder", "Cancel an upcoming reminder by id.", CANCEL_REMINDER_SCHEMA)
    async def cancel_reminder(args: dict) -> dict:
        result = await asyncio.to_thread(_cancel, user_id, tz, int(args["reminder_id"]))
        return {"content": [{"type": "text", "text": result}]}

    @tool("stop_reminders", "Stop the user's repeating reminders (or all upcoming ones) at once.", STOP_REMINDERS_SCHEMA)
    async def stop_reminders(args: dict) -> dict:
        result = await asyncio.to_thread(_stop, user_id, args.get("scope") or "repeating")
        return {"content": [{"type": "text", "text": result}]}

    return [create_reminder, list_reminders, cancel_reminder, stop_reminders]


REMINDER_TOOL_NAMES = [
    "mcp__reminders__create_reminder",
    "mcp__reminders__list_reminders",
    "mcp__reminders__cancel_reminder",
    "mcp__reminders__stop_reminders",
]
