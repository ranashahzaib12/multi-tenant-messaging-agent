"""Make pictures for the user: generated images and data charts. Each tool
returns a /media/ link; app.py attaches such links to the WhatsApp reply as
an image, so the user sees the picture, not a URL."""
import asyncio

from claude_agent_sdk import tool

from src.config import PUBLIC_BASE_URL
from src.utils import media_ai

GENERATE_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "What the image should show, in detail: subject, style, setting, mood."},
        "size": {
            "type": "string",
            "enum": ["1024x1024", "1536x1024", "1024x1536"],
            "description": "Square, landscape or portrait. Default square.",
        },
    },
    "required": ["prompt"],
}

CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "kind": {"type": "string", "enum": ["line", "bar"], "description": "line for trends over time, bar for comparisons."},
        "labels": {"type": "array", "items": {"type": "string"}, "description": "X-axis labels, e.g. dates."},
        "series": {
            "type": "array",
            "description": "One or more series, each {name, values}; values align with labels.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "values": {"type": "array", "items": {"type": ["number", "null"]}},
                },
                "required": ["values"],
            },
        },
        "y_label": {"type": "string", "description": "Unit for the Y axis, e.g. mmHg."},
    },
    "required": ["title", "kind", "labels", "series"],
}


def _text(t: str) -> dict:
    return {"content": [{"type": "text", "text": t}]}


def _generate(prompt: str, size: str) -> str:
    try:
        png = media_ai.generate_image(prompt, size)
    except media_ai.MediaError as e:
        return f"Not done: {e}."
    except Exception as e:
        return f"Not done: image generation failed ({type(e).__name__})."
    return f"Image ready: {media_ai.publish(png, PUBLIC_BASE_URL)}\nInclude this exact link in your reply so the user receives the picture."


def _chart(args: dict) -> str:
    try:
        png = media_ai.render_chart(args["title"], args.get("kind") or "line", args["labels"], args["series"], args.get("y_label") or "")
    except media_ai.MediaError as e:
        return f"Not done: {e}."
    except Exception as e:
        return f"Not done: chart rendering failed ({type(e).__name__}: {str(e)[:120]})."
    return f"Chart ready: {media_ai.publish(png, PUBLIC_BASE_URL)}\nInclude this exact link in your reply so the user receives the picture."


@tool("generate_image", "Create a picture from a description (illustration, mockup, poster, product shot idea).", GENERATE_SCHEMA)
async def generate_image(args: dict) -> dict:
    return _text(await asyncio.to_thread(_generate, args["prompt"], args.get("size") or "1024x1024"))


@tool("render_chart", "Draw a line or bar chart from numbers (readings over time, comparisons) as an image.", CHART_SCHEMA)
async def render_chart(args: dict) -> dict:
    return _text(await asyncio.to_thread(_chart, args))


image_tools = [generate_image, render_chart]
IMAGE_TOOL_NAMES = ["mcp__images__generate_image", "mcp__images__render_chart"]
