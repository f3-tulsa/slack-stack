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


def counted_noun(n: int, singular: str, plural: str | None = None) -> str:
    """``1 award``, ``27 awards``. Pass *plural* when it is not ``singular + 's'`` (e.g. PAX)."""
    word = singular if int(n) == 1 else (plural if plural is not None else f"{singular}s")
    return f"{int(n)} {word}"


OVERFLOW_EDIT = "edit"
OVERFLOW_DUPLICATE = "duplicate"
OVERFLOW_DISABLE = "disable"
OVERFLOW_ENABLE = "enable"
OVERFLOW_DELETE = "delete"


def overflow_option(label: str, value: str) -> dict:
    return {
        "text": {"type": "plain_text", "text": label[:75], "emoji": True},
        "value": str(value)[:75],
    }


def parse_overflow_action(action: dict | None) -> tuple[str | None, int | None]:
    """Return ``(verb, row_id)`` from an overflow accessory payload."""
    action = action or {}
    opt = action.get("selected_option") or {}
    raw = opt.get("value") or action.get("value") or ""
    if ":" not in str(raw):
        return None, None
    verb, _, rest = str(raw).partition(":")
    try:
        return verb, int(rest)
    except (TypeError, ValueError):
        return None, None


def overflow_row(title: str, action_id: str, row_id, *, enabled: bool = True) -> dict:
    """Name on the left, Slack overflow (⋯ / More) on the right."""
    rid = str(row_id)
    toggle_label = "Disable" if enabled else "Enable"
    toggle_verb = OVERFLOW_DISABLE if enabled else OVERFLOW_ENABLE
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*{title}*"[:MAX_SECTION_TEXT]},
        "accessory": {
            "type": "overflow",
            "action_id": action_id,
            "options": [
                overflow_option("Edit", f"{OVERFLOW_EDIT}:{rid}"),
                overflow_option("Duplicate", f"{OVERFLOW_DUPLICATE}:{rid}"),
                overflow_option(toggle_label, f"{toggle_verb}:{rid}"),
                overflow_option("Delete", f"{OVERFLOW_DELETE}:{rid}"),
            ],
        },
    }


def delete_confirm_modal(
    *,
    callback_id: str,
    title: str,
    warning: str,
    metadata: str,
) -> dict:
    """Pushed confirm view; same warning copy as the old button ``confirm`` dialog."""
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": "Delete"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": warning[:3000]},
            }
        ],
    }


def page_nav_elements(
    page: int, total: int, prev_id: str, next_id: str, *, page_size: int
) -> list[dict]:
    start = max(page, 0) * page_size
    elements: list[dict] = []
    if page > 0:
        elements.append(
            {
                "type": "button",
                "action_id": prev_id,
                "text": {"type": "plain_text", "text": "← Prev"},
                "value": str(page - 1),
            }
        )
    if start + page_size < total:
        elements.append(
            {
                "type": "button",
                "action_id": next_id,
                "text": {"type": "plain_text", "text": "Next →"},
                "value": str(page + 1),
            }
        )
    return elements


def confirm_dialog(title: str, text: str, confirm: str = "Delete") -> dict:
    dialog = {
        "title": {"type": "plain_text", "text": title[:100]},
        "text": {"type": "mrkdwn", "text": text[:300]},
        "confirm": {"type": "plain_text", "text": confirm[:30]},
        "deny": {"type": "plain_text", "text": "Cancel"},
    }
    if confirm.lower().startswith("delete"):
        dialog["style"] = "danger"
    return dialog
