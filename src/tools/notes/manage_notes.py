import asyncio

from claude_agent_sdk import tool, SdkMcpTool

from src import database as db


ADD_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Note title."},
        "content": {"type": "string", "description": "Note content."},
        "tags": {
            "type": "string",
            "description": "Comma-separated tags for the note, e.g. 'work,ideas'.",
        },
    },
    "required": ["title", "content"],
}

GET_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "description": "Maximum number of notes to return. Default 10."},
    },
    "required": [],
}

SEARCH_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query to find notes by title or content."},
    },
    "required": ["query"],
}


def _add_note(user_id: str, title: str, content: str, tags: str | None) -> str:
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    try:
        note_id = db.save_note(user_id, title, content, tag_list)
        return f"Note saved: '{title}' [id: {note_id}]"
    except Exception as e:
        return f"Error saving note: {e}"


def _get_notes(user_id: str, limit: int) -> str:
    try:
        notes = db.get_notes(user_id, limit)
        if not notes:
            return "No notes found."

        lines = []
        for n in notes:
            tags_str = f" [{', '.join(n['tags'])}]" if n.get("tags") else ""
            lines.append(f"- {n['title']}{tags_str}: {n['content'][:100]}... [id: {n['id']}]")

        return f"Found {len(notes)} note(s):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error fetching notes: {e}"


def _search_notes(user_id: str, query: str) -> str:
    try:
        notes = db.search_notes(user_id, query)
        if not notes:
            return f"No notes found matching '{query}'."

        lines = []
        for n in notes:
            lines.append(f"- {n['title']}: {n['content'][:150]}... [id: {n['id']}]")

        return f"Found {len(notes)} note(s):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error searching notes: {e}"


def build_notes_tools(user_id: str) -> list[SdkMcpTool]:
    """Build fresh notes tools bound to a specific user's phone number.

    Each incoming message gets its own tool set via closure over user_id,
    rather than relying on the tool's args or a contextvar, since the SDK's
    subprocess/MCP boundary doesn't guarantee our request-local context
    propagates into the tool call. This keeps per-user note isolation exact.
    """

    @tool("add_note", "Save a new note for the user. Call the moment you have the title and content.", ADD_NOTE_SCHEMA)
    async def add_note(args: dict) -> dict:
        result = await asyncio.to_thread(_add_note, user_id, args["title"], args["content"], args.get("tags"))
        return {"content": [{"type": "text", "text": result}]}

    @tool("get_notes", "List the user's recent notes.", GET_NOTES_SCHEMA)
    async def get_notes(args: dict) -> dict:
        limit = args.get("limit") or 10
        result = await asyncio.to_thread(_get_notes, user_id, limit)
        return {"content": [{"type": "text", "text": result}]}

    @tool("search_notes", "Search notes by title or content.", SEARCH_NOTES_SCHEMA)
    async def search_notes(args: dict) -> dict:
        result = await asyncio.to_thread(_search_notes, user_id, args["query"])
        return {"content": [{"type": "text", "text": result}]}

    return [add_note, get_notes, search_notes]


NOTES_TOOL_NAMES = ["mcp__notes__add_note", "mcp__notes__get_notes", "mcp__notes__search_notes"]
