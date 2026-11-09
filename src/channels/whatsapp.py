import logging

import httpx
from twilio.rest import Client

from src.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, FROM_WHATSAPP_NUMBER
from src.utils.message_splitter import split_message

logger = logging.getLogger(__name__)


# WhatsApp shows "typing…" for up to 25 s per call (Twilio public beta), so
# a long agent turn needs the call repeated while it runs.
TYPING_URL = "https://messaging.twilio.com/v3/Indicators/Typing.json"
TYPING_REFRESH_SECONDS = 20


class WhatsAppChannel:
    def __init__(self):
        self.client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        self.from_number = FROM_WHATSAPP_NUMBER

    def send_typing(self, inbound_message_sid: str) -> bool:
        """Show the typing indicator (and read receipt) on the user's chat for
        the message they just sent. Best effort: a failure here must never
        stop the reply itself, so it is logged and swallowed."""
        try:
            resp = httpx.post(
                TYPING_URL,
                json={"channel": "WHATSAPP", "messageId": inbound_message_sid},
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                timeout=10.0,
            )
            if resp.status_code >= 300:
                logger.warning("Typing indicator refused: HTTP %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as e:
            logger.warning("Typing indicator failed: %s", e)
            return False

    def send_message(self, to_number: str, body: str, media_urls: list[str] | None = None):
        parts = split_message(body)
        for i, part in enumerate(parts):
            try:
                kwargs = {"from_": self.from_number, "to": to_number, "body": part}
                if media_urls and i == 0:
                    # WhatsApp takes one media item per message.
                    kwargs["media_url"] = media_urls[:1]
                self.client.messages.create(**kwargs)
            except Exception as e:
                logger.error("Failed to send WhatsApp message to %s: %s", to_number, e)
                raise
