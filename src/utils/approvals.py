"""Confirm-before-act gate for bookings.

The final step of a booking (the "Confirm reservation" click) may only happen
after the user said yes. The yes is detected here, in code, from the user's
own WhatsApp message — the model cannot grant it to itself by calling a tool.
"""
import re
import time

APPROVAL_TTL = 10 * 60
PENDING_TTL = 30 * 60

_YES = re.compile(
    r"^\s*(yes|yes please|yep|yeah|ya|y|ok|okay|confirm|confirmed|go ahead|go for it|book it|do it|proceed|approve|approved|sure)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_NO = re.compile(r"^\s*(no|nope|cancel|stop|don'?t|do not|decline|leave it|hold on|wait)\b", re.IGNORECASE)


class BookingGate:
    def __init__(self):
        self._pending: dict[str, tuple[str, float]] = {}
        self._approved: dict[str, tuple[str, float]] = {}

    def request(self, user_id: str, summary: str) -> None:
        self._pending[user_id] = (summary, time.time())
        self._approved.pop(user_id, None)

    def pending(self, user_id: str) -> str | None:
        item = self._pending.get(user_id)
        if item and time.time() - item[1] < PENDING_TTL:
            return item[0]
        self._pending.pop(user_id, None)
        return None

    def resolve_from_message(self, user_id: str, message: str) -> str | None:
        """Called on every inbound user message. Returns a system line for the
        agent when the message settles a pending approval, else None."""
        summary = self.pending(user_id)
        if not summary:
            return None
        if _YES.match(message):
            self._pending.pop(user_id, None)
            self._approved[user_id] = (summary, time.time() + APPROVAL_TTL)
            return (
                f"[SYSTEM: the user approved the pending booking: {summary}. "
                "The final booking step is unlocked for 10 minutes; have browser_agent complete it now.]"
            )
        if _NO.match(message):
            self._pending.pop(user_id, None)
            return f"[SYSTEM: the user declined the pending booking: {summary}. Do not complete it.]"
        return None

    def is_approved(self, user_id: str) -> bool:
        item = self._approved.get(user_id)
        if item and time.time() < item[1]:
            return True
        self._approved.pop(user_id, None)
        return False

    def consume(self, user_id: str) -> None:
        self._approved.pop(user_id, None)


booking_gate = BookingGate()
