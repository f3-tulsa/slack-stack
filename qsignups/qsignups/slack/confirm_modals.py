"""Helpers for edit confirmation modals (Slack views) — used by app.py and tests."""

from __future__ import annotations

import json
from typing import Any


RECURRING_Q_SLOT_WARNING = (
    ":warning: *All existing Q signups for this series under the current schedule will be deleted.*"
)


def confirm_modal_view(
    callback_id: str,
    private_metadata: dict[str, Any],
    summary_lines: list[str],
    *,
    warning_markdown: str | None = None,
    title: str = "Confirm changes",
) -> dict[str, Any]:
    """Build a Slack modal view dict for views_open / view_submission."""
    blocks: list[dict[str, Any]] = []
    intro = "*You are about to apply these changes:*"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": intro}})
    for line in summary_lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})
    if warning_markdown:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": warning_markdown}],
            }
        )
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": "Confirm"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(private_metadata),
        "blocks": blocks,
    }


def delete_confirm_modal_view(
    callback_id: str,
    private_metadata: dict[str, Any],
    summary_lines: list[str],
    *,
    warning_markdown: str | None = None,
    title: str = "Confirm delete",
) -> dict[str, Any]:
    """Build a Slack modal for delete confirmation (views_open / view_submission)."""
    blocks: list[dict[str, Any]] = []
    intro = "*You are about to delete:*"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": intro}})
    for line in summary_lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})
    if warning_markdown:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": warning_markdown}],
            }
        )
    return {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": "Delete"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(private_metadata),
        "blocks": blocks,
    }


def format_field_change(label: str, old: str | None, new: str | None) -> str | None:
    """Return a mrkdwn line if old != new, else None."""
    old_s = (old or "").strip() or "(empty)"
    new_s = (new or "").strip() or "(empty)"
    if old_s == new_s:
        return None
    return f"*{label}:* `{old_s}` → `{new_s}`"


def load_modal_metadata(metadata: str) -> dict[str, Any]:
    return json.loads(metadata or "{}")
