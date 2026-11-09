"""Google Drive tools, built per user so each turn reads that user's own
Drive (closure over the phone, like the calendar and email tools)."""
import asyncio

from claude_agent_sdk import tool, SdkMcpTool

from src.tools.drive.files import (
    READ_FILE_SCHEMA,
    SAVE_LAST_FILE_SCHEMA,
    SEARCH_FILES_SCHEMA,
    read_file as _read_file,
    save_last_file as _save_last_file,
    search_files as _search_files,
)


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


def build_drive_tools(user_phone: str) -> list[SdkMcpTool]:
    @tool("search_drive_files", "Find files in the user's Google Drive by name or content (shared files included). Returns name, type, modified date, link and id.", SEARCH_FILES_SCHEMA)
    async def search_drive_files(args: dict) -> dict:
        return _text(await asyncio.to_thread(_search_files, user_phone, args["query"], args.get("max_results") or 10))

    @tool("read_drive_file", "Read the text of one Drive file by id: Google Docs, Sheets, Slides, PDF, Word or text.", READ_FILE_SCHEMA)
    async def read_drive_file(args: dict) -> dict:
        return _text(await asyncio.to_thread(_read_file, user_phone, args["file_id"]))

    @tool("save_last_file_to_drive", "Save the file or photo the user most recently sent on WhatsApp (within 30 minutes) into their Google Drive, optionally into a named folder.", SAVE_LAST_FILE_SCHEMA)
    async def save_last_file_to_drive(args: dict) -> dict:
        return _text(await asyncio.to_thread(_save_last_file, user_phone, args.get("name"), args.get("folder_name")))

    return [search_drive_files, read_drive_file, save_last_file_to_drive]


DRIVE_TOOL_NAMES = ["mcp__drive__search_drive_files", "mcp__drive__read_drive_file", "mcp__drive__save_last_file_to_drive"]
