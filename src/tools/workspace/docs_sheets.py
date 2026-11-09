"""Google Docs and Sheets: create a document or spreadsheet, append to one,
read a sheet range. Editing is limited to what the user asks for in the
turn; the scopes cover Docs/Sheets content only, not the rest of Drive."""
import re

from src.utils.google_auth import get_docs_service, get_sheets_service, scope_not_granted

MAX_TEXT_CHARS = 12_000


def file_id(ref: str) -> str:
    """Accept a bare id or a Docs/Sheets URL."""
    m = re.search(r"/d/([A-Za-z0-9_-]+)", ref or "")
    return m.group(1) if m else (ref or "").strip()


def _doc_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def _sheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def create_google_doc(phone: str, title: str, content: str | None) -> str:
    service = get_docs_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Docs")
    if not (title or "").strip():
        return "The document needs a title."
    try:
        doc = service.documents().create(body={"title": title.strip()}).execute()
        doc_id = doc["documentId"]
        if content and content.strip():
            service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
            ).execute()
    except Exception as e:
        return f"Error creating the Google Doc: {e}"
    return f"Created Google Doc '{doc.get('title', title)}': {_doc_url(doc_id)} [id: {doc_id}]"


def append_to_google_doc(phone: str, document: str, text: str) -> str:
    service = get_docs_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Docs")
    doc_id = file_id(document)
    if not text:
        return "Nothing to add."
    try:
        doc = service.documents().get(documentId=doc_id, fields="title,body(content(endIndex))").execute()
        # The body always ends with a newline the API won't let us write past.
        end = doc["body"]["content"][-1]["endIndex"] - 1
        service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": max(1, end)}, "text": "\n" + text}}]},
        ).execute()
    except Exception as e:
        return f"Error updating the Google Doc: {e}"
    return f"Added {len(text):,} characters to '{doc.get('title', doc_id)}': {_doc_url(doc_id)}"


def create_google_sheet(phone: str, title: str, rows: list[list] | None) -> str:
    service = get_sheets_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Sheets")
    if not (title or "").strip():
        return "The spreadsheet needs a title."
    try:
        sheet = service.spreadsheets().create(
            body={"properties": {"title": title.strip()}}, fields="spreadsheetId,spreadsheetUrl"
        ).execute()
        sheet_id = sheet["spreadsheetId"]
        written = 0
        if rows:
            resp = service.spreadsheets().values().update(
                spreadsheetId=sheet_id, range="A1", valueInputOption="USER_ENTERED", body={"values": rows}
            ).execute()
            written = resp.get("updatedRows", 0)
    except Exception as e:
        return f"Error creating the Google Sheet: {e}"
    return f"Created Google Sheet '{title.strip()}' with {written} row(s): {sheet.get('spreadsheetUrl', _sheet_url(sheet_id))} [id: {sheet_id}]"


def append_sheet_rows(phone: str, spreadsheet: str, rows: list[list], sheet_name: str | None) -> str:
    service = get_sheets_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Sheets")
    sheet_id = file_id(spreadsheet)
    if not rows:
        return "No rows to add."
    rng = f"'{sheet_name}'!A1" if sheet_name else "A1"
    try:
        resp = service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range=rng, valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS", body={"values": rows},
        ).execute()
    except Exception as e:
        return f"Error adding rows: {e}"
    upd = resp.get("updates", {})
    return f"Added {upd.get('updatedRows', len(rows))} row(s) at {upd.get('updatedRange', rng)}: {_sheet_url(sheet_id)}"


def read_google_sheet(phone: str, spreadsheet: str, cell_range: str | None) -> str:
    service = get_sheets_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Sheets")
    sheet_id = file_id(spreadsheet)
    try:
        meta = service.spreadsheets().get(spreadsheetId=sheet_id, fields="properties.title,sheets.properties.title").execute()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        rng = cell_range or (f"'{tabs[0]}'" if tabs else "A1:Z200")
        resp = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    except Exception as e:
        return f"Error reading the Google Sheet: {e}"
    values = resp.get("values", [])
    text = "\n".join("\t".join(str(c) for c in row) for row in values)
    truncated = len(text) > MAX_TEXT_CHARS
    out = f"Sheet '{meta.get('properties', {}).get('title', sheet_id)}' (tabs: {', '.join(tabs) or '?'}), range {resp.get('range', rng)}, {len(values)} row(s):\n{text[:MAX_TEXT_CHARS] or '(empty)'}"
    if truncated:
        out += f"\n[Truncated at {MAX_TEXT_CHARS:,} characters; ask for a narrower range]"
    return out


_ROWS = {
    "type": "array",
    "description": "Rows, each a list of cell values (strings or numbers).",
    "items": {"type": "array", "items": {"type": ["string", "number"]}},
}

CREATE_DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Document title."},
        "content": {"type": "string", "description": "Optional body text (plain text; blank lines separate paragraphs)."},
    },
    "required": ["title"],
}

APPEND_DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "document": {"type": "string", "description": "Google Doc id or URL (from search_drive_files or an earlier result)."},
        "text": {"type": "string", "description": "Text to add at the end."},
    },
    "required": ["document", "text"],
}

CREATE_SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Spreadsheet title."},
        "rows": _ROWS,
    },
    "required": ["title"],
}

APPEND_ROWS_SCHEMA = {
    "type": "object",
    "properties": {
        "spreadsheet": {"type": "string", "description": "Google Sheet id or URL."},
        "rows": _ROWS,
        "sheet_name": {"type": "string", "description": "Tab name. Omit for the first tab."},
    },
    "required": ["spreadsheet", "rows"],
}

READ_SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        "spreadsheet": {"type": "string", "description": "Google Sheet id or URL."},
        "range": {"type": "string", "description": "A1 range such as 'Sheet1!A1:D50'. Omit for the whole first tab."},
    },
    "required": ["spreadsheet"],
}
