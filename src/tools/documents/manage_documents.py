import asyncio

from claude_agent_sdk import tool, SdkMcpTool

from src import database as db
from src.utils.documents import embed_query


LIST_DOCUMENTS_SCHEMA = {"type": "object", "properties": {}, "required": []}

SEARCH_DOCUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to look for in the user's documents."},
        "max_results": {"type": "integer", "description": "Maximum passages to return. Default 5."},
    },
    "required": ["query"],
}

DELETE_DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_id": {"type": "integer", "description": "The id from list_documents."},
    },
    "required": ["document_id"],
}


def _canon_label(source: str) -> str:
    """Aria named uploads 'upload:<filename or Twilio media sid>' and its core
    knowledge file 'canon'."""
    if source == "canon":
        return "Core knowledge base"
    if source.startswith("upload:"):
        name = source[len("upload:"):]
        return f"file sent via WhatsApp ({name[:8]}…)" if name.startswith("MM") and len(name) > 20 else name
    return source


def _list_documents(user_id: str, legacy_user_id: str | None) -> str:
    try:
        docs = db.list_documents(user_id)
    except Exception as e:
        return f"Error listing documents: {e}"
    lines = []
    for d in docs:
        status = "" if d["status"] == "active" else f" ({d['status']}, not searched)"
        lines.append(
            f"- [id: {d['id']}] {d['filename']} — uploaded {d['created_at']:%Y-%m-%d}, "
            f"{d['char_count'] or 0:,} characters{status}"
        )
    earlier = []
    if legacy_user_id:
        try:
            earlier = db.list_canon_sources(legacy_user_id)
        except Exception as e:
            earlier = [{"error": str(e)}]
    if not lines and not earlier:
        return "The user has no saved documents: nothing sent here, and no earlier documents exist under this number."
    out = f"{len(docs)} document(s) stored here:\n" + ("\n".join(lines) if lines else "- none")
    if earlier:
        if "error" in earlier[0]:
            out += f"\n\nEarlier documents could not be read: {earlier[0]['error']}"
        else:
            out += f"\n\n{len(earlier)} earlier document(s) from before the move (searchable, read-only, can't be deleted here):\n" + "\n".join(
                f"- {_canon_label(e['source'])} — added {e['created_at']:%Y-%m-%d}, {e['char_count']:,} characters"
                for e in earlier
            )
    return out


def _search_documents(user_id: str, legacy_user_id: str | None, query: str, max_results: int) -> str:
    embedding = embed_query(query)
    parts, errors = [], []
    try:
        for r in db.search_document_chunks(user_id, query, embedding, max_results):
            parts.append(f"[{r['filename']} (id {r['document_id']}), part {r['chunk_index'] + 1}]\n{r['content']}")
    except Exception as e:
        errors.append(f"documents stored here: {e}")
    if legacy_user_id:
        try:
            for r in db.search_canon(legacy_user_id, query, embedding, max_results):
                where = f", {r['section']}" if r.get("section") else ""
                parts.append(f"[earlier document: {_canon_label(r['source'])}{where}, part {(r['chunk_index'] or 0) + 1}]\n{r['content']}")
        except Exception as e:
            errors.append(f"earlier documents: {e}")
    if not parts:
        msg = f"Nothing in the user's documents matches '{query}'."
        return msg + (" (Search errors: " + "; ".join(errors) + ")" if errors else "")
    return "\n\n---\n\n".join(parts) + ("\n\n(Search errors: " + "; ".join(errors) + ")" if errors else "")


def _delete_document(user_id: str, document_id: int) -> str:
    try:
        filename = db.delete_document(user_id, document_id)
    except Exception as e:
        return f"Error deleting document: {e}"
    if not filename:
        return f"No document with id {document_id} exists for this user. Nothing was deleted."
    return f"Deleted '{filename}' (id {document_id}) and all its stored text permanently."


def build_document_tools(user_id: str, legacy_user_id: str | None = None) -> list[SdkMcpTool]:
    """Document tools bound to one user's phone number via closure, like the
    notes tools, so a turn can only ever see or delete its own user's files."""

    @tool("list_documents", "List the user's stored documents: ones sent here and earlier ones from before the move.", LIST_DOCUMENTS_SCHEMA)
    async def list_documents(args: dict) -> dict:
        result = await asyncio.to_thread(_list_documents, user_id, legacy_user_id)
        return {"content": [{"type": "text", "text": result}]}

    @tool(
        "search_documents",
        "Find passages in the user's stored documents relevant to a question. Use before answering anything about their documents.",
        SEARCH_DOCUMENTS_SCHEMA,
    )
    async def search_documents(args: dict) -> dict:
        result = await asyncio.to_thread(_search_documents, user_id, legacy_user_id, args["query"], args.get("max_results") or 5)
        return {"content": [{"type": "text", "text": result}]}

    @tool(
        "delete_document",
        "Permanently delete one of the user's documents. Only when the user has clearly asked to delete that specific document.",
        DELETE_DOCUMENT_SCHEMA,
    )
    async def delete_document(args: dict) -> dict:
        result = await asyncio.to_thread(_delete_document, user_id, int(args["document_id"]))
        return {"content": [{"type": "text", "text": result}]}

    return [list_documents, search_documents, delete_document]


DOCUMENT_TOOL_NAMES = [
    "mcp__documents__list_documents",
    "mcp__documents__search_documents",
    "mcp__documents__delete_document",
]
