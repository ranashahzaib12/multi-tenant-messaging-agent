"""Voice notes, photos and generated images.

Everything here is blocking (HTTP calls, image encoding); callers run it via
asyncio.to_thread. Transcription and image generation use the OpenAI key
that already exists for embeddings; photos are handed to Claude itself as
image blocks, so they need no extra service.
"""
import base64
import io
import logging
import os

import httpx

from src.config import OPENAI_API_KEY
from src.utils import media_store

logger = logging.getLogger(__name__)

TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
MAX_IMAGE_EDGE = 1568  # Claude's effective ceiling; bigger only costs tokens
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class MediaError(Exception):
    """A problem the user should be told about in plain words."""


def transcribe(data: bytes, content_type: str) -> str:
    """Speech to text for a WhatsApp voice note (audio/ogg opus)."""
    if not OPENAI_API_KEY:
        raise MediaError("voice notes can't be transcribed because no transcription key is configured")
    if len(data) > MAX_AUDIO_BYTES:
        raise MediaError("that voice note is too long to transcribe (25 MB limit)")
    ext = {
        "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
        "audio/m4a": "m4a", "audio/aac": "m4a", "audio/amr": "amr", "audio/wav": "wav", "audio/x-wav": "wav",
        "audio/flac": "flac", "audio/x-flac": "flac", "audio/webm": "webm",
    }.get(content_type.split(";")[0].strip().lower(), "ogg")
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (f"voice.{ext}", data, content_type)},
            data={"model": TRANSCRIBE_MODEL},
        )
    if resp.status_code >= 300:
        logger.warning("Transcription refused: %s %s", resp.status_code, resp.text[:200])
        raise MediaError("the voice note couldn't be transcribed right now")
    text = (resp.json().get("text") or "").strip()
    if not text:
        raise MediaError("the voice note had no recognisable speech")
    return text


def prepare_image(data: bytes) -> tuple[str, str]:
    """Downscale a photo for the model. Returns (media_type, base64)."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception:
        raise MediaError("that image couldn't be opened")
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode()


def generate_image(prompt: str, size: str = "1024x1024") -> bytes:
    """Text to image via OpenAI. Returns PNG bytes."""
    if not OPENAI_API_KEY:
        raise MediaError("image generation isn't configured (no OpenAI key)")
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1},
        )
    if resp.status_code >= 300:
        logger.warning("Image generation refused: %s %s", resp.status_code, resp.text[:300])
        detail = ""
        try:
            detail = resp.json()["error"]["message"][:200]
        except Exception:
            pass
        raise MediaError(f"the image couldn't be generated{': ' + detail if detail else ''}")
    item = resp.json()["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with httpx.Client(timeout=60.0) as client:
            return client.get(item["url"]).content
    raise MediaError("the image service returned nothing")


def render_chart(title: str, kind: str, labels: list[str], series: list[dict], y_label: str = "") -> bytes:
    """Bar/line chart to PNG with matplotlib (headless)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not labels or not series:
        raise MediaError("a chart needs labels and at least one series")
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    n = len(series)
    x = list(range(len(labels)))
    for i, s in enumerate(series):
        values = [float(v) if v is not None else float("nan") for v in s.get("values", [])]
        values += [float("nan")] * (len(labels) - len(values))
        if kind == "bar":
            width = 0.8 / n
            ax.bar([xi + (i - (n - 1) / 2) * width for xi in x], values[: len(labels)], width, label=s.get("name"))
        else:
            ax.plot(x, values[: len(labels)], marker="o", label=s.get("name"))
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45 if len(labels) > 8 else 0, ha="right" if len(labels) > 8 else "center")
    ax.set_title(title)
    if y_label:
        ax.set_ylabel(y_label)
    if n > 1 or any(s.get("name") for s in series):
        ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def publish(png: bytes, base_url: str) -> str:
    return f"{base_url}/media/{media_store.put(png)}.png"
