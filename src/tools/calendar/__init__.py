"""Calendar tools, built per user so each turn works on that user's own
Google account (closure over the phone, like the notes tools)."""
import asyncio

from claude_agent_sdk import tool, SdkMcpTool

from src.tools.calendar.get_events import get_calendar_events as _get_events, GET_EVENTS_SCHEMA
from src.tools.calendar.create_event import create_calendar_event as _create_event, CREATE_EVENT_SCHEMA
from src.tools.calendar.update_event import update_calendar_event as _update_event, UPDATE_EVENT_SCHEMA
from src.tools.calendar.delete_event import delete_calendar_event as _delete_event, DELETE_EVENT_SCHEMA
from src.utils.google_auth import google_connection_status

EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": []}


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


def build_calendar_tools(user_phone: str) -> list[SdkMcpTool]:
    @tool("get_calendar_events", "Fetch upcoming Google Calendar events: times, locations, invitees, Meet links.", GET_EVENTS_SCHEMA)
    async def get_calendar_events(args: dict) -> dict:
        return _text(await asyncio.to_thread(_get_events, user_phone, args.get("days_ahead") or 7, args.get("query") or None))

    @tool(
        "create_calendar_event",
        "Create a Google Calendar event, optionally inviting people by email (they get the invitation) and with a Google Meet link. Call this the moment you have all required fields.",
        CREATE_EVENT_SCHEMA,
    )
    async def create_calendar_event(args: dict) -> dict:
        attendees = args.get("attendees") or []
        add_meet = args.get("add_meet_link")
        if add_meet is None:
            add_meet = bool(attendees) and not args.get("location")
        return _text(await asyncio.to_thread(
            _create_event, user_phone, args["summary"], args["start_time"], args["end_time"],
            args.get("location"), args.get("description"), attendees, bool(add_meet),
        ))

    @tool(
        "update_calendar_event",
        "Change an existing event: move it, rename it, or add/remove invitees. Attendees are notified of the change.",
        UPDATE_EVENT_SCHEMA,
    )
    async def update_calendar_event(args: dict) -> dict:
        return _text(await asyncio.to_thread(
            _update_event, user_phone, args["event_id"], args.get("summary"), args.get("start_time"),
            args.get("end_time"), args.get("add_attendees"), args.get("remove_attendees"), args.get("location"),
        ))

    @tool("delete_calendar_event", "Delete a Google Calendar event by its ID.", DELETE_EVENT_SCHEMA)
    async def delete_calendar_event(args: dict) -> dict:
        return _text(await asyncio.to_thread(_delete_event, user_phone, args["event_id"]))

    @tool(
        "google_connect_link",
        "Whether this user's Google (Calendar + Gmail, and Drive) is connected, and their personal link to connect, add Drive, or switch accounts.",
        EMPTY_SCHEMA,
    )
    async def google_connect_link(args: dict) -> dict:
        return _text(await asyncio.to_thread(google_connection_status, user_phone))

    return [get_calendar_events, create_calendar_event, update_calendar_event, delete_calendar_event, google_connect_link]


CALENDAR_TOOL_NAMES = [
    f"mcp__calendar__{n}"
    for n in ("get_calendar_events", "create_calendar_event", "update_calendar_event", "delete_calendar_event", "google_connect_link")
]
