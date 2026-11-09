from src.config import WHATSAPP_CHAR_LIMIT


def split_message(text: str, limit: int = WHATSAPP_CHAR_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break

        tag_reserve = len(f" ({len(parts) + 1}/99)")
        cut_at = limit - tag_reserve

        split_pos = remaining.rfind("\n\n", 0, cut_at)
        if split_pos == -1:
            split_pos = remaining.rfind("\n", 0, cut_at)
        if split_pos == -1:
            split_pos = remaining.rfind(". ", 0, cut_at)
        if split_pos == -1:
            split_pos = remaining.rfind(" ", 0, cut_at)
        if split_pos == -1:
            split_pos = cut_at

        chunk = remaining[:split_pos].rstrip()
        remaining = remaining[split_pos:].lstrip()

        if chunk:
            parts.append(chunk)

    if len(parts) > 1:
        total = len(parts)
        parts = [f"{p} ({i + 1}/{total})" for i, p in enumerate(parts)]

    return parts
