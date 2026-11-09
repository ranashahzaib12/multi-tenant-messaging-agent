"""Short-lived in-memory store for images the app itself generates (booking
screenshots), served at /media/<token>.png so Twilio can attach them to a
WhatsApp message. Tokens are unguessable; entries expire after two hours and
are lost on restart, which is fine for proof-of-action images."""
import secrets
import time

TTL = 2 * 60 * 60
_items: dict[str, tuple[bytes, float]] = {}


def put(png: bytes) -> str:
    _sweep()
    token = secrets.token_hex(16)
    _items[token] = (png, time.time() + TTL)
    return token


def get(token: str) -> bytes | None:
    item = _items.get(token)
    if item and time.time() < item[1]:
        return item[0]
    _items.pop(token, None)
    return None


def _sweep() -> None:
    now = time.time()
    for token in [t for t, (_, exp) in _items.items() if exp < now]:
        _items.pop(token, None)
