"""Local Block Kit helpers for PAXMiner outbound Slack messages.

Kept in-tree (no shared package) because PAXMiner, Slackblast, and QSignups
deploy as separate images. Chart PNG uploads stay on ``files_upload_v2``.
"""

from __future__ import annotations

MAX_SECTION_TEXT = 3000
MAX_BLOCKS = 50


def header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:MAX_SECTION_TEXT]}}


def context(text: str) -> dict:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text[:MAX_SECTION_TEXT]}],
    }


def divider() -> dict:
    return {"type": "divider"}


def fallback_text(blocks: list[dict]) -> str:
    """Flatten block mrkdwn/plain_text into a notification fallback string."""
    parts: list[str] = []
    for block in blocks or []:
        btype = block.get("type")
        if btype in ("section", "header"):
            text = (block.get("text") or {}).get("text") or ""
            if text:
                parts.append(text)
        elif btype == "context":
            for el in block.get("elements") or []:
                t = el.get("text") or ""
                if t:
                    parts.append(t)
    return "\n".join(parts).strip() or "PAXMiner update"


def chunk_sections(lines: list[str], *, max_chars: int = MAX_SECTION_TEXT) -> list[dict]:
    """Join lines into section blocks, each under ``max_chars``."""
    sections: list[dict] = []
    buf = ""
    for line in lines:
        candidate = f"{buf}{line}" if buf else line
        if len(candidate) > max_chars and buf:
            sections.append(section(buf))
            buf = line
            while len(buf) > max_chars:
                sections.append(section(buf[:max_chars]))
                buf = buf[max_chars:]
        else:
            buf = candidate
    if buf:
        sections.append(section(buf))
    return sections


def chunk_messages(blocks: list[dict], *, max_blocks: int = MAX_BLOCKS) -> list[list[dict]]:
    """Split a block list into messages of at most ``max_blocks`` blocks."""
    if not blocks:
        return []
    return [blocks[i : i + max_blocks] for i in range(0, len(blocks), max_blocks)]
