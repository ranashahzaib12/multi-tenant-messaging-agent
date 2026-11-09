"""Google Docs and Sheets tools, built per user (closure over the phone)."""
import asyncio

from claude_agent_sdk import tool, SdkMcpTool

from src.tools.workspace.docs_sheets import (
    APPEND_DOC_SCHEMA,
    APPEND_ROWS_SCHEMA,
    CREATE_DOC_SCHEMA,
    CREATE_SHEET_SCHEMA,
    READ_SHEET_SCHEMA,
    append_sheet_rows as _append_rows,
    append_to_google_doc as _append_doc,
    create_google_doc as _create_doc,
    create_google_sheet as _create_sheet,
    read_google_sheet as _read_sheet,
)


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


def build_workspace_tools(user_phone: str) -> list[SdkMcpTool]:
    @tool("create_google_doc", "Create a new Google Doc with a title and optional body text. Returns the link.", CREATE_DOC_SCHEMA)
    async def create_google_doc(args: dict) -> dict:
        return _text(await asyncio.to_thread(_create_doc, user_phone, args["title"], args.get("content")))

    @tool("append_to_google_doc", "Add text to the end of an existing Google Doc (by id or URL).", APPEND_DOC_SCHEMA)
    async def append_to_google_doc(args: dict) -> dict:
        return _text(await asyncio.to_thread(_append_doc, user_phone, args["document"], args["text"]))

    @tool("create_google_sheet", "Create a new Google Sheet, optionally with rows (first row as headers). Returns the link.", CREATE_SHEET_SCHEMA)
    async def create_google_sheet(args: dict) -> dict:
        return _text(await asyncio.to_thread(_create_sheet, user_phone, args["title"], args.get("rows")))

    @tool("append_sheet_rows", "Add rows to the bottom of an existing Google Sheet (by id or URL), e.g. a new reading in a log.", APPEND_ROWS_SCHEMA)
    async def append_sheet_rows(args: dict) -> dict:
        return _text(await asyncio.to_thread(_append_rows, user_phone, args["spreadsheet"], args["rows"], args.get("sheet_name")))

    @tool("read_google_sheet", "Read cells from a Google Sheet (by id or URL), a range or the whole first tab.", READ_SHEET_SCHEMA)
    async def read_google_sheet(args: dict) -> dict:
        return _text(await asyncio.to_thread(_read_sheet, user_phone, args["spreadsheet"], args.get("range")))

    return [create_google_doc, append_to_google_doc, create_google_sheet, append_sheet_rows, read_google_sheet]


WORKSPACE_TOOL_NAMES = [
    f"mcp__workspace__{n}"
    for n in ("create_google_doc", "append_to_google_doc", "create_google_sheet", "append_sheet_rows", "read_google_sheet")
]
