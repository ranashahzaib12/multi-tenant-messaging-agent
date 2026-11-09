"""Headless browser sessions for the booking worker (Playwright, async API).

One Chromium process, one isolated context per user, closed after 15 idle
minutes. Nothing is persisted: no cookies, no credentials, no profile.
"""
import logging
import os
import re
import time

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

IDLE_SECONDS = 15 * 60
MAX_TEXT = 6000
MAX_ELEMENTS = 120
NAV_TIMEOUT_MS = 30_000

# Reading a page's interactive elements in one pass and tagging each with an
# index lets the model act by number ("click 12") instead of guessing CSS
# selectors, which it gets wrong far more often than it gets right.
_SNAPSHOT_JS = """
(max) => {
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=option], [role=tab], [onclick]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= max) break;
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || style.visibility === 'hidden' || style.display === 'none') continue;
    if (el.type === 'hidden') continue;
    el.setAttribute('data-agent-idx', String(i));
    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    out.push({
      idx: i++,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || el.getAttribute('role') || '',
      text,
      name: el.getAttribute('name') || el.id || '',
      href: el.tagName === 'A' ? (el.getAttribute('href') || '').slice(0, 120) : '',
      checked: (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : undefined,
      value: (el.tagName === 'INPUT' && el.type !== 'checkbox' && el.type !== 'radio' && el.type !== 'password') ? String(el.value || '').slice(0, 60) : undefined,
      options: el.tagName === 'SELECT' ? Array.from(el.options).slice(0, 30).map(o => o.value + (o.text && o.text !== o.value ? ' (' + o.text.trim().slice(0, 40) + ')' : '')) : undefined,
    });
  }
  return out;
}
"""


class BrowserSession:
    def __init__(self, context: BrowserContext, page: Page):
        self.context = context
        self.page = page
        self.last_used = time.time()
        self.elements: list[dict] = []

    def touch(self) -> None:
        self.last_used = time.time()


class BrowserPool:
    def __init__(self):
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[str, BrowserSession] = {}

    async def _browser_instance(self) -> Browser:
        if self._browser and self._browser.is_connected():
            return self._browser
        if not self._pw:
            self._pw = await async_playwright().start()
        launch: dict = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        # The container runs as root, which Chromium refuses without --no-sandbox.
        # PLAYWRIGHT_CHROMIUM_PATH is only for environments (like the dev
        # sandbox) whose browser build doesn't match the installed package.
        if os.getenv("PLAYWRIGHT_CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_PATH"]
        self._browser = await self._pw.chromium.launch(**launch)
        return self._browser

    async def session(self, user_id: str) -> BrowserSession:
        await self._sweep()
        s = self._sessions.get(user_id)
        if s and not s.page.is_closed():
            s.touch()
            return s
        browser = await self._browser_instance()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-SG",
            timezone_id="Asia/Singapore",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)
        s = BrowserSession(context, page)
        self._sessions[user_id] = s
        return s

    async def close(self, user_id: str) -> None:
        s = self._sessions.pop(user_id, None)
        if s:
            try:
                await s.context.close()
            except Exception:
                logger.debug("Context close failed", exc_info=True)

    async def _sweep(self) -> None:
        cutoff = time.time() - IDLE_SECONDS
        for uid in [u for u, s in self._sessions.items() if s.last_used < cutoff]:
            await self.close(uid)


pool = BrowserPool()


async def snapshot(s: BrowserSession) -> str:
    """Page title, URL, visible text and numbered interactive elements."""
    page = s.page
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass
    s.elements = await page.evaluate(_SNAPSHOT_JS, MAX_ELEMENTS)
    try:
        text = await page.inner_text("body")
    except Exception:
        text = ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n[... page text truncated ...]"
    lines = []
    for e in s.elements:
        desc = f"[{e['idx']}] <{e['tag']}{(' ' + e['type']) if e['type'] else ''}> {e['text']}"
        if e["name"]:
            desc += f" (name={e['name']})"
        if e.get("checked") is not None:
            desc += " [checked]" if e["checked"] else " [not checked]"
        if e.get("value"):
            desc += f" value=\"{e['value']}\""
        if e["href"]:
            desc += f" -> {e['href']}"
        if e.get("options"):
            desc += f" options: {', '.join(e['options'])}"
        lines.append(desc)
    return (
        f"Title: {await page.title()}\nURL: {page.url}\n\n--- Page text ---\n{text}\n\n"
        f"--- Interactive elements ({len(lines)}) ---\n" + "\n".join(lines)
    )


def element(s: BrowserSession, idx: int) -> dict | None:
    return next((e for e in s.elements if e["idx"] == idx), None)


def locator(s: BrowserSession, idx: int):
    return s.page.locator(f'[data-agent-idx="{idx}"]').first
