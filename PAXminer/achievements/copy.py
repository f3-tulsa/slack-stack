"""Pandas-free achievement copy. Safe to import from SlackFunction."""

from __future__ import annotations

from slack_util import format_log_message


def achievement_rule_phrase(row: dict) -> str:
    """Compact rule wording shared by the edit-modal summary and the admin log."""
    from achievements.activity import activity_filter_from_rule

    spec = activity_filter_from_rule(row)
    include = spec["include"]
    exclude = spec["exclude"]
    if not include and not exclude:
        activity_label = "all activities"
    elif include and not exclude:
        activity_label = ", ".join(include)
    elif not include and exclude:
        activity_label = f"all except {', '.join(exclude)}"
    else:
        activity_label = f"{', '.join(include)} (excluding {', '.join(exclude)})"
    return f"{row.get('metric')}/{row.get('period')} ≥ {row.get('threshold')} · {activity_label}"


def admin_channel_line(
    action: str,
    name: str,
    admin_mention: str,
    *,
    granted: int = 0,
    revoked: int = 0,
    unchanged: int = 0,
) -> str | None:
    """Public achievement-channel line for one admin action. None means do not post."""
    if action == "created":
        return (
            f"Achievement *{name}* was created by {admin_mention} "
            f"({granted} granted)."
        )
    if action == "deleted":
        return (
            f"Achievement *{name}* was deleted by {admin_mention} "
            f"({revoked} revoked)."
        )
    if action in ("changed", "re-evaluated"):
        if not granted and not revoked:
            return None
        verb = "changed" if action == "changed" else "re-evaluated"
        return (
            f"Achievement *{name}* was {verb} by {admin_mention} "
            f"({granted} granted, {revoked} revoked, {unchanged} unchanged)."
        )
    return None


def achievement_admin_log_line(
    *,
    name: str,
    action: str,
    author: str | None = None,
    status: str = "success",
    duration_s: float | None = None,
    code: str | None = None,
    version: str | None = None,
    rules: str | None = None,
    period: str | None = None,
    granted: int = 0,
    revoked: int = 0,
    unchanged: int = 0,
    affected_pax: int | None = None,
    held_detail: str = "",
) -> str:
    """paxminer_logs envelope for created / changed / deleted / re-evaluated."""
    awards = f"{granted} granted, {revoked} revoked, {unchanged} unchanged"
    if held_detail:
        awards = f"{awards}{held_detail}"
    fields = [
        ("Author", author or ""),
        ("Action", action),
        ("Code", code or ""),
        ("Version", version or ""),
        ("Rules", rules or ""),
        ("Period", period or ""),
        ("Awards", awards),
        (
            "Affected PAX",
            "" if affected_pax is None else str(affected_pax),
        ),
    ]
    return format_log_message(
        f"Achievement *{name}* was processed.",
        status=status,
        duration_s=duration_s,
        fields=fields,
        code_block=True,
    )
