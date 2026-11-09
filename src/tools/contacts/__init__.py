"""Google Contacts (read-only), built per user (closure over the phone)."""
import asyncio

from claude_agent_sdk import tool, SdkMcpTool

from src.tools.contacts.people import SEARCH_CONTACTS_SCHEMA, search_contacts as _search_contacts


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


def build_contacts_tools(user_phone: str) -> list[SdkMcpTool]:
    @tool("search_contacts", "Look someone up in the user's Google Contacts by name, email, phone or company. Returns names with emails and phones. Use it before asking the user for an email address.", SEARCH_CONTACTS_SCHEMA)
    async def search_contacts(args: dict) -> dict:
        return _text(await asyncio.to_thread(_search_contacts, user_phone, args["query"], args.get("max_results") or 8))

    return [search_contacts]


CONTACTS_TOOL_NAMES = ["mcp__contacts__search_contacts"]
