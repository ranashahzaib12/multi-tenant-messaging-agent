"""Google OAuth for Calendar and Gmail, per WhatsApp user.

Each user connects their own Google account through a personal link; the
resulting tokens are stored in that user's profile row. The one shared
token file from the single-account era is kept only as a fallback for the
owner numbers (BUSINESS_DATA_PHONES), so the client's existing connection
keeps working while other users connect their own.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import logging
import threading

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

from src import database as db
from src.config import (
    BUSINESS_DATA_PHONES,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    PUBLIC_BASE_URL,
    TEST_WEBHOOK_TOKEN,
    TWILIO_AUTH_TOKEN,
)

logger = logging.getLogger(__name__)

_G = "https://www.googleapis.com/auth/"
CALENDAR_SCOPE = _G + "calendar"
GMAIL_READ_SCOPE = _G + "gmail.readonly"
GMAIL_SEND_SCOPE = _G + "gmail.send"
GMAIL_MODIFY_SCOPE = _G + "gmail.modify"       # archive, labels, trash (never permanent delete)
DRIVE_SCOPE = _G + "drive.readonly"
DRIVE_FILE_SCOPE = _G + "drive.file"           # create files; no access to files Aria didn't make
DOCS_SCOPE = _G + "documents"
SHEETS_SCOPE = _G + "spreadsheets"
TASKS_SCOPE = _G + "tasks"
CONTACTS_SCOPE = _G + "contacts.readonly"
OTHER_CONTACTS_SCOPE = _G + "contacts.other.readonly"  # people emailed but never saved
SCOPES = [
    CALENDAR_SCOPE, GMAIL_READ_SCOPE, GMAIL_SEND_SCOPE, GMAIL_MODIFY_SCOPE,
    DRIVE_SCOPE, DRIVE_FILE_SCOPE, DOCS_SCOPE, SHEETS_SCOPE, TASKS_SCOPE,
    CONTACTS_SCOPE, OTHER_CONTACTS_SCOPE,
]
# What google_connection_status reports, service by service. Tools check
# their own scope at call time, so a token granted before a service was
# added keeps working for everything it does have.
SERVICES: list[tuple[str, str]] = [
    ("Calendar", CALENDAR_SCOPE),
    ("Gmail read", GMAIL_READ_SCOPE),
    ("Gmail send", GMAIL_SEND_SCOPE),
    ("Gmail organise (archive/labels/trash)", GMAIL_MODIFY_SCOPE),
    ("Drive read", DRIVE_SCOPE),
    ("Drive save", DRIVE_FILE_SCOPE),
    ("Docs", DOCS_SCOPE),
    ("Sheets", SHEETS_SCOPE),
    ("Tasks", TASKS_SCOPE),
    ("Contacts", CONTACTS_SCOPE),
]
# Drive was added after the first users connected. A user may untick it on
# the consent screen, and oauthlib refuses a grant whose scopes differ from
# the request unless told to relax; we would rather store what was granted
# and let each tool check its own scope.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Shared (legacy) token: configurable so a persistent volume can be mounted
# (e.g. /data/token.json) on hosts with an ephemeral filesystem.
_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")

# credentials.json is never committed (it holds the OAuth client secret), so
# rather than requiring that file to exist on the deployed host, the client
# config is built in-memory from env vars. from_client_config() takes the
# exact same "web": {...} shape as the downloaded credentials.json.
_CREDENTIALS_FILE = "credentials.json"

# google-auth-oauthlib's Flow defaults autogenerate_code_verifier=True, so
# authorization_url() silently turns on PKCE: it generates flow.code_verifier
# and embeds the matching code_challenge in the URL sent to Google. That
# verifier lives only on that one Flow instance in memory. /oauth/google/start
# and /oauth/google/callback are separate HTTP requests that each build a
# fresh Flow, so without carrying the verifier over, Google's token endpoint
# rejects the exchange with "invalid_grant: Missing code verifier". Stash it
# here keyed by the flow's `state` (which round-trips through Google
# unchanged), together with the phone number the link was issued to, so the
# callback knows whose tokens it is saving.
_PKCE_TTL_SECONDS = 600
_pkce_lock = threading.Lock()
_pkce_store: dict[str, tuple[str, str | None, float]] = {}


def _save_pkce(state: str, code_verifier: str, phone: str | None) -> None:
    with _pkce_lock:
        now = time.time()
        for k in [k for k, (_, _, saved_at) in _pkce_store.items() if now - saved_at > _PKCE_TTL_SECONDS]:
            del _pkce_store[k]
        _pkce_store[state] = (code_verifier, phone, now)


def _pop_pkce(state: str | None) -> tuple[str | None, str | None]:
    if not state:
        return None, None
    with _pkce_lock:
        entry = _pkce_store.pop(state, None)
    if not entry:
        return None, None
    code_verifier, phone, saved_at = entry
    if time.time() - saved_at > _PKCE_TTL_SECONDS:
        return None, None
    return code_verifier, phone


# --- Personal connect links ---------------------------------------------------
# /oauth/google/start connects whichever Google account completes the consent
# screen to the WhatsApp number the link was issued for, so the link must be
# unguessable and short-lived, and must not carry the phone number itself
# (URLs end up in proxy logs). An opaque nonce maps to the phone in memory;
# the nonce is signed so a lost map can only mean "ask for a fresh link".
_LINK_TTL_SECONDS = 30 * 60
_LINK_SECRET = (TEST_WEBHOOK_TOKEN or TWILIO_AUTH_TOKEN or "").encode()
_link_lock = threading.Lock()
_links: dict[str, tuple[str, float]] = {}


def make_link_token(phone: str) -> str:
    nonce = secrets.token_hex(12)
    exp = str(int(time.time()) + _LINK_TTL_SECONDS)
    sig = hmac.new(_LINK_SECRET, f"{nonce}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    with _link_lock:
        now = time.time()
        for k in [k for k, (_, e) in _links.items() if e < now]:
            del _links[k]
        _links[nonce] = (phone, float(exp))
    return f"{nonce}.{exp}.{sig}"


def resolve_link_token(token: str | None) -> str | None:
    """The phone a valid, unexpired link was issued to, else None."""
    try:
        nonce, exp, sig = (token or "").split(".", 2)
        expected = hmac.new(_LINK_SECRET, f"{nonce}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected) or int(exp) <= time.time():
            return None
    except (ValueError, AttributeError):
        return None
    with _link_lock:
        entry = _links.get(nonce)
    return entry[0] if entry else None


def connect_link(phone: str) -> str:
    return f"{PUBLIC_BASE_URL}/oauth/google/start?t={make_link_token(phone)}"


def not_connected(service_name: str, phone: str) -> str:
    """The message a Google tool returns when this user has no usable token:
    it carries their personal connect link so they can fix it themselves."""
    if not PUBLIC_BASE_URL:
        return f"{service_name} is not connected for this user, and no public URL is configured to offer a connect link."
    return (
        f"{service_name} is not connected for this user. Tell them to connect their Google account by "
        f"opening this personal link (valid 30 minutes) and signing in: {connect_link(phone)}"
    )


# --- Credentials --------------------------------------------------------------

def _client_config() -> dict:
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        return {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
    with open(_CREDENTIALS_FILE) as f:
        return json.load(f)


def _refresh(creds: Credentials | None, save) -> Credentials | None:
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save(creds.to_json())
        except Exception:
            logger.warning("Token refresh failed", exc_info=True)
            return None
    return creds if creds and creds.valid else None


def _shared_credentials() -> Credentials | None:
    if not os.path.exists(_TOKEN_FILE):
        return None
    # Stored tokens keep the scopes they were actually granted (read from the
    # JSON), so has_scopes() tells the truth and pre-Drive tokens keep working.
    creds = Credentials.from_authorized_user_file(_TOKEN_FILE)

    def save(text: str) -> None:
        with open(_TOKEN_FILE, "w") as f:
            f.write(text)

    return _refresh(creds, save)


def get_google_credentials(phone: str | None = None) -> Credentials | None:
    """This user's own Google credentials, refreshed if needed. Owner numbers
    fall back to the shared token; anyone else without their own gets None
    and the calling tool reports "not connected" with a personal link."""
    if phone:
        try:
            stored = db.get_google_tokens(phone)
        except Exception:
            logger.debug("Google token lookup skipped (no DB)", exc_info=True)
            stored = None
        if stored:
            creds = Credentials.from_authorized_user_info(json.loads(stored))
            creds = _refresh(creds, lambda text: db.set_google_tokens(phone, text))
            if creds:
                return creds
        if phone not in BUSINESS_DATA_PHONES:
            return None
    return _shared_credentials()


def google_connection_status(phone: str) -> str:
    creds = get_google_credentials(phone)
    if not creds:
        return not_connected("Google (Calendar, Gmail and Drive)", phone)
    try:
        stored = db.get_google_tokens(phone)
    except Exception:
        stored = None
    which = "their own Google account" if stored else "the shared Google account"
    granted = [name for name, scope in SERVICES if has_scope(creds, scope)]
    missing = [name for name, scope in SERVICES if not has_scope(creds, scope)]
    text = f"Google is connected for this user via {which}. Granted: {', '.join(granted) or 'nothing'}."
    if missing:
        text += f" Not granted yet: {', '.join(missing)}. To add them (one tap, keeps what is granted), they open: {connect_link(phone)}"
    return text + f" To switch accounts, open: {connect_link(phone)}"


def has_scope(creds: Credentials | None, scope: str) -> bool:
    return bool(creds and creds.has_scopes([scope]))


def has_drive(creds: Credentials | None) -> bool:
    return has_scope(creds, DRIVE_SCOPE)


def scope_not_granted(phone: str, service_name: str) -> str:
    """Services were added after the first users connected, so a valid Google
    token may lack one. The fix is the same personal link: Google keeps the
    scopes already granted and adds the missing ones."""
    if not get_google_credentials(phone):
        return not_connected(service_name, phone)
    if not PUBLIC_BASE_URL:
        return f"{service_name} access has not been granted for this user, and no public URL is configured to offer a link."
    return (
        f"{service_name} access has not been granted for this user yet. Tell them to open this personal "
        f"link (valid 30 minutes), sign in and allow it; what they already granted stays: {connect_link(phone)}"
    )


def drive_not_connected(phone: str) -> str:
    return scope_not_granted(phone, "Google Drive")


# --- Web OAuth flow -----------------------------------------------------------

def get_authorization_url(redirect_uri: str, phone: str | None) -> str:
    """Build the Google consent screen URL for the web OAuth flow, for the
    given WhatsApp user (None = the shared account)."""
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    _save_pkce(state, flow.code_verifier or "", phone)
    return auth_url


def exchange_code_for_token(code: str, redirect_uri: str, state: str | None = None) -> str | None:
    """Complete the web OAuth flow and store the tokens for the user the
    link was issued to (or the shared file when there is none). Returns the
    phone the tokens were saved for."""
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri)
    code_verifier, phone = _pop_pkce(state)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    text = flow.credentials.to_json()
    if phone:
        db.set_google_tokens(phone, text)
    else:
        with open(_TOKEN_FILE, "w") as f:
            f.write(text)
    return phone


def run_local_console_auth() -> None:
    """One-time interactive helper for a machine WITH a browser to produce a
    shared token.json. Not used by the running server."""
    flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())


def get_calendar_service(phone: str | None = None):
    creds = get_google_credentials(phone)
    return build("calendar", "v3", credentials=creds) if creds else None


def get_gmail_service(phone: str | None = None):
    creds = get_google_credentials(phone)
    return build("gmail", "v1", credentials=creds) if creds else None


def _scoped_service(phone: str | None, api: str, version: str, scope: str):
    """None when Google is not connected *or* the token lacks this scope, so
    the calling tool answers with the reconnect link instead of a 403."""
    creds = get_google_credentials(phone)
    return build(api, version, credentials=creds) if has_scope(creds, scope) else None


def get_drive_service(phone: str | None = None):
    return _scoped_service(phone, "drive", "v3", DRIVE_SCOPE)


def get_drive_write_service(phone: str | None = None):
    return _scoped_service(phone, "drive", "v3", DRIVE_FILE_SCOPE)


def get_docs_service(phone: str | None = None):
    return _scoped_service(phone, "docs", "v1", DOCS_SCOPE)


def get_sheets_service(phone: str | None = None):
    return _scoped_service(phone, "sheets", "v4", SHEETS_SCOPE)


def get_tasks_service(phone: str | None = None):
    return _scoped_service(phone, "tasks", "v1", TASKS_SCOPE)


def get_people_service(phone: str | None = None):
    return _scoped_service(phone, "people", "v1", CONTACTS_SCOPE)


def get_gmail_modify_service(phone: str | None = None):
    return _scoped_service(phone, "gmail", "v1", GMAIL_MODIFY_SCOPE)
