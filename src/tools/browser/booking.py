"""Browser tools for guest bookings: browse public sites, fill forms, and
complete a booking only after the user approved it.

Two rules are enforced here in code, not left to the prompt:
- No logins, no payments: password and card fields can't be typed into,
  login pages can't be opened, login buttons can't be clicked.
- The final commit click (Book / Confirm / Reserve / Submit) is blocked until
  the user's own message approved it (see src/utils/approvals.py).
"""
import logging
import re
from urllib.parse import urlparse

from claude_agent_sdk import tool, SdkMcpTool

from src.config import PUBLIC_BASE_URL
from src.utils import browser, media_store
from src.utils.approvals import booking_gate

logger = logging.getLogger(__name__)

_COMMIT = re.compile(
    r"\b(book|reserve|reservation|confirm|complete|place order|pay|purchase|submit|check ?out|finish|schedule|sign up|register|send)\b",
    re.IGNORECASE,
)
_LOGIN_TEXT = re.compile(r"\b(log ?in|sign ?in|create account|my account)\b", re.IGNORECASE)
_LOGIN_URL = re.compile(r"(login|signin|sign-in|sign_in|/auth\b|/account\b)", re.IGNORECASE)
_SECRET_FIELD = re.compile(
    r"(password|passwd|pwd|card|cc-|cvc|cvv|ccv|expir|iban|account.?number|routing|otp|one.?time|verification.?code)",
    re.IGNORECASE,
)

OPEN_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "Full http(s) URL to open."}},
    "required": ["url"],
}
READ_SCHEMA = {"type": "object", "properties": {}, "required": []}
CLICK_SCHEMA = {
    "type": "object",
    "properties": {"index": {"type": "integer", "description": "Element number from the last page read."}},
    "required": ["index"],
}
TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {"type": "integer", "description": "Element number of the input from the last page read."},
        "text": {"type": "string", "description": "Text to type. Replaces the field's current value."},
    },
    "required": ["index", "text"],
}
SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {"type": "integer", "description": "Element number of the <select> from the last page read."},
        "value": {"type": "string", "description": "Option value or visible label."},
    },
    "required": ["index", "value"],
}
APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Exactly what will be booked, for the user to approve: place, date, time, party size, name and contact used, price if any.",
        }
    },
    "required": ["summary"],
}


def _text(result: str) -> dict:
    return {"content": [{"type": "text", "text": result}]}


def _field_looks_secret(e: dict) -> bool:
    return bool(_SECRET_FIELD.search(f"{e.get('name', '')} {e.get('text', '')} {e.get('type', '')}"))


async def _screenshot_url(s: browser.BrowserSession) -> str:
    png = await s.page.screenshot(full_page=False)
    token = media_store.put(png)
    return f"{PUBLIC_BASE_URL}/media/{token}.png"


def build_browser_tools(user_id: str) -> list[SdkMcpTool]:
    """Per-user closure: the browser session and the approval gate are keyed
    by the user, same reason as build_notes_tools."""

    @tool("open_page", "Open a public web page in the browser and read it.", OPEN_SCHEMA)
    async def open_page(args: dict) -> dict:
        url = args["url"].strip()
        if urlparse(url).scheme not in ("http", "https"):
            return _text("Only http(s) URLs can be opened.")
        if _LOGIN_URL.search(url):
            return _text("Blocked: that looks like a login or account page. I can only make guest bookings without signing in.")
        s = await browser.pool.session(user_id)
        try:
            await s.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            return _text(f"Could not open {url}: {type(e).__name__}: {str(e)[:200]}")
        return _text(await browser.snapshot(s))

    @tool("read_page", "Re-read the current page (after it changed, or to see fresh element numbers).", READ_SCHEMA)
    async def read_page(args: dict) -> dict:
        s = await browser.pool.session(user_id)
        if s.page.url == "about:blank":
            return _text("No page is open. Use open_page first.")
        return _text(await browser.snapshot(s))

    @tool(
        "click",
        "Click an element by its number. The final booking button is refused until the user has approved via request_booking_approval.",
        CLICK_SCHEMA,
    )
    async def click(args: dict) -> dict:
        s = await browser.pool.session(user_id)
        e = browser.element(s, int(args["index"]))
        if not e:
            return _text("No such element number. Call read_page and use a number from the new list.")
        label = e["text"] or e["name"]
        if _LOGIN_TEXT.search(label):
            return _text(f"Blocked: '{label}' is a login control. Guest bookings only; no signing in.")
        commit = (bool(_COMMIT.search(label)) and e["tag"] in ("button", "input", "a")) or e["type"] == "submit"
        if commit and not booking_gate.is_approved(user_id):
            return _text(
                f"Blocked: '{label}' looks like the final booking step. Call request_booking_approval with a "
                "summary of exactly what will be booked, tell the user, and wait for their yes. Only then click this."
            )
        try:
            await browser.locator(s, e["idx"]).click()
            await s.page.wait_for_timeout(1500)
        except Exception as ex:
            return _text(f"Click failed on '{label}': {type(ex).__name__}: {str(ex)[:200]}")
        result = await browser.snapshot(s)
        if commit:
            # Approval covers one commit click. The screenshot is the proof the
            # user gets, taken by code right after the click.
            booking_gate.consume(user_id)
            try:
                result = f"Committed '{label}'. Screenshot of the result: {await _screenshot_url(s)}\n\n{result}"
            except Exception:
                logger.warning("Screenshot failed", exc_info=True)
        return _text(result)

    @tool("type_text", "Type into an input or textarea by its number (replaces its current value).", TYPE_SCHEMA)
    async def type_text(args: dict) -> dict:
        s = await browser.pool.session(user_id)
        e = browser.element(s, int(args["index"]))
        if not e:
            return _text("No such element number. Call read_page first.")
        if e["type"] == "password" or _field_looks_secret(e):
            return _text(f"Blocked: '{e['text'] or e['name']}' is a password or payment field. I never enter credentials or card details.")
        try:
            await browser.locator(s, e["idx"]).fill(args["text"])
        except Exception as ex:
            return _text(f"Typing failed: {type(ex).__name__}: {str(ex)[:200]}")
        return _text(f"Typed into [{e['idx']}] {e['text'] or e['name']}: {args['text']}")

    @tool("select_option", "Choose an option in a dropdown by its number.", SELECT_SCHEMA)
    async def select_option(args: dict) -> dict:
        s = await browser.pool.session(user_id)
        e = browser.element(s, int(args["index"]))
        if not e or e["tag"] != "select":
            return _text("That element number is not a dropdown. Call read_page first.")
        value = args["value"]
        loc = browser.locator(s, e["idx"])
        try:
            try:
                await loc.select_option(value=value)
            except Exception:
                await loc.select_option(label=value)
        except Exception as ex:
            return _text(f"Select failed: {type(ex).__name__}: {str(ex)[:200]}")
        await s.page.wait_for_timeout(800)
        return _text(f"Selected '{value}' in [{e['idx']}].\n\n{await browser.snapshot(s)}")

    @tool(
        "request_booking_approval",
        "Register what is about to be booked so the user can approve it. Returns the question to put to the user; the booking can't be completed until they answer yes.",
        APPROVAL_SCHEMA,
    )
    async def request_booking_approval(args: dict) -> dict:
        summary = args["summary"].strip()
        booking_gate.request(user_id, summary)
        s = await browser.pool.session(user_id)
        shot = ""
        try:
            shot = f"\nScreenshot of the form as filled: {await _screenshot_url(s)}"
        except Exception:
            pass
        return _text(
            f"Approval requested. Ask the user, in these words or close to them: "
            f"\"Ready to book: {summary}. Reply *yes* to confirm or *no* to cancel.\"{shot}\n"
            "Stop here and wait for their reply; do not click anything else this turn."
        )

    @tool("screenshot", "Take a screenshot of the current page; returns a link the user can open.", READ_SCHEMA)
    async def screenshot(args: dict) -> dict:
        s = await browser.pool.session(user_id)
        try:
            return _text(f"Screenshot: {await _screenshot_url(s)}")
        except Exception as ex:
            return _text(f"Screenshot failed: {type(ex).__name__}")

    return [open_page, read_page, click, type_text, select_option, request_booking_approval, screenshot]


BROWSER_TOOL_NAMES = [
    f"mcp__browser__{n}"
    for n in ("open_page", "read_page", "click", "type_text", "select_option", "request_booking_approval", "screenshot")
]
