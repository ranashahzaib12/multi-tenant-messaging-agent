"""The last file each user sent over WhatsApp, kept in memory for a short
while so "save that to my Drive" can upload the original bytes. Documents are
otherwise stored as extracted text only (see documents.py), and photos not
at all, so this is the only place the original exists."""
import threading
import time

TTL_SECONDS = 30 * 60
MAX_BYTES = 10 * 1024 * 1024

_lock = threading.Lock()
_store: dict[str, tuple[str, str, bytes, float]] = {}


def remember(phone: str, name: str, mime: str, data: bytes) -> None:
    if len(data) > MAX_BYTES:
        return
    with _lock:
        now = time.time()
        for k in [k for k, v in _store.items() if v[3] + TTL_SECONDS < now]:
            del _store[k]
        _store[phone] = (name, mime, data, now)


def get(phone: str) -> tuple[str, str, bytes] | None:
    with _lock:
        entry = _store.get(phone)
    if not entry or entry[3] + TTL_SECONDS < time.time():
        return None
    return entry[0], entry[1], entry[2]
