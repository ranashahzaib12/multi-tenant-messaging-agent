"""Read-only Google Contacts lookup, so "schedule a call with Sarah" can find
Sarah's email without asking. Searches saved contacts and, when granted,
"other contacts" (people the user has emailed but never saved)."""
import re

from src.utils.google_auth import (
    OTHER_CONTACTS_SCOPE,
    get_google_credentials,
    get_people_service,
    has_scope,
    scope_not_granted,
)

_FIELDS = "names,emailAddresses,phoneNumbers,organizations"
_MAX_PAGES = 5  # 1000 people per page; enough for any personal address book


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _matches(person: dict, q: str) -> bool:
    qd = _digits(q)
    for n in person.get("names", []):
        if q in (n.get("displayName") or "").lower():
            return True
    for e in person.get("emailAddresses", []):
        if q in (e.get("value") or "").lower():
            return True
    if len(qd) >= 5:
        for p in person.get("phoneNumbers", []):
            if qd in _digits(p.get("value")):
                return True
    for o in person.get("organizations", []):
        if q in (o.get("name") or "").lower():
            return True
    return False


def _format(person: dict) -> str:
    name = next((n.get("displayName") for n in person.get("names", []) if n.get("displayName")), None) or "(no name)"
    emails = [e["value"] for e in person.get("emailAddresses", []) if e.get("value")]
    phones = [p["value"] for p in person.get("phoneNumbers", []) if p.get("value")]
    org = next((o.get("name") for o in person.get("organizations", []) if o.get("name")), None)
    bits = [name]
    if emails:
        bits.append("email: " + ", ".join(emails))
    if phones:
        bits.append("phone: " + ", ".join(phones))
    if org:
        bits.append(org)
    return "- " + " | ".join(bits)


def _pages(call, key: str, **kwargs):
    token = None
    for _ in range(_MAX_PAGES):
        resp = call(**({"pageToken": token} if token else {}), **kwargs).execute()
        yield from resp.get(key, [])
        token = resp.get("nextPageToken")
        if not token:
            break


def search_contacts(phone: str, query: str, max_results: int) -> str:
    service = get_people_service(phone)
    if not service:
        return scope_not_granted(phone, "Google Contacts")
    q = (query or "").strip().lower()
    if len(q) < 2:
        return "Give me at least two characters of a name, email or number to look up."
    try:
        found: list[dict] = []
        for person in _pages(service.people().connections().list, "connections",
                             resourceName="people/me", pageSize=1000, personFields=_FIELDS):
            if _matches(person, q):
                found.append(person)
        if has_scope(get_google_credentials(phone), OTHER_CONTACTS_SCOPE):
            for person in _pages(service.otherContacts().list, "otherContacts",
                                 pageSize=1000, readMask="names,emailAddresses,phoneNumbers"):
                if _matches(person, q):
                    found.append(person)
    except Exception as e:
        return f"Error searching contacts: {e}"

    seen: set[str] = set()
    lines = []
    for person in found:
        key = _format(person)
        if key not in seen:
            seen.add(key)
            lines.append(key)
    if not lines:
        return f"No contact matches {query!r}. Ask the user for the email address."
    limit = max(1, min(int(max_results or 8), 25))
    more = f"\n(+{len(lines) - limit} more; refine the search)" if len(lines) > limit else ""
    return f"Found {len(lines)} contact(s):\n" + "\n".join(lines[:limit]) + more


SEARCH_CONTACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Part of a name, email address, phone number or company."},
        "max_results": {"type": "integer", "description": "Maximum contacts to return (1-25). Default 8."},
    },
    "required": ["query"],
}
