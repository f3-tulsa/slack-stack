"""Grouped achievement channel, DM, and paxminer_logs copy."""

from __future__ import annotations

from datetime import date

from achievements.period import backblast_archive_url, format_date_label, spoken_period
from scheduling import format_iso_range
from slack_blocks import chunk_messages, chunk_sections, fallback_text, section
from slack_util import format_log_message, mention, ordinal_suffix, plain_name, ticked_display_name


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def ao_tag(ao_id: str | None) -> str | None:
    if not ao_id:
        return None
    text = str(ao_id).strip()
    if text.startswith("C") or text.startswith("#"):
        cid = text[1:] if text.startswith("#") else text
        return f"<#{cid}>"
    return None


def date_link(
    awarded_on,
    ao_id: str | None,
    timestamp: str | None,
    *,
    label: str | None = None,
) -> str:
    d = _as_date(awarded_on)
    text = label or (format_date_label(d) if d else "this event")
    url = backblast_archive_url(ao_id, timestamp)
    if url:
        return f"<{url}|{text}>"
    return text


def _earned_at(grant: dict) -> str:
    ao = ao_tag(grant.get("ao_id"))
    dlink = date_link(grant.get("date_awarded"), grant.get("ao_id"), grant.get("timestamp"))
    if ao:
        return f"at {ao} on {dlink}"
    return f"on {dlink}"


def _family_label(rule: dict) -> str:
    name = rule.get("name") or "achievement"
    verb = rule.get("verb") or ""
    if verb:
        return f"*{name}* for {verb}"
    return f"*{name}*"


def channel_grant_messages(
    grants: list[dict],
    *,
    year: int,
    names: dict[str, str],
    known_ids: set[str] | None,
    ytd_totals: dict[str, int],
    ytd_family: dict[tuple[str, int], int],
) -> list[tuple[str, list[dict], bool]]:
    """Return (text, blocks, add_reaction) per channel message, grouped by achievement."""
    by_aid: dict[int, list[dict]] = {}
    for g in grants:
        by_aid.setdefault(int(g["achievement_id"]), []).append(g)
    out: list[tuple[str, list[dict], bool]] = []
    for _aid, group in by_aid.items():
        rule = group[0]["rule"]
        label = _family_label(rule)
        if len(group) == 1:
            g = group[0]
            tag = mention(g["pax_id"], names.get(g["pax_id"]), known_ids=known_ids)
            total = ytd_totals.get(g["pax_id"], 0)
            idx = ytd_family.get((g["pax_id"], int(g["achievement_id"])), 0)
            ending = ordinal_suffix(idx)
            text = (
                f"Congrats to our man {tag} who just unlocked the achievement {label}! "
                f"He earned this {_earned_at(g)}. "
                f"This is achievement #{total} in {year} for {tag} and the {idx}{ending} "
                f"time this year he's earned this award. Encourage this HIM to keep it up!"
            )
            out.append((text, [section(text)], True))
            continue
        header = (
            f"T-Claps :clap: for these men who just unlocked the achievement {label}! "
            f"Encourage these HIM to keep it up!"
        )
        lines = [
            f"{mention(g['pax_id'], names.get(g['pax_id']), known_ids=known_ids)} "
            f"earned {_earned_at(g)}"
            for g in group
        ]
        body = header + "\n" + "\n".join(lines)
        blocks = [section(header)]
        blocks.extend(chunk_sections(["\n".join(lines)]))
        first = True
        for chunk in chunk_messages(blocks):
            text = fallback_text(chunk)
            if not first:
                cont = f"{label} (continued)"
                chunk = [section(cont), *chunk]
                text = fallback_text(chunk)
            out.append((text, chunk, first))
            first = False
    return out


def dm_grant_messages(
    grants: list[dict],
    *,
    year: int,
    names: dict[str, str],
    known_ids: set[str] | None,
    ytd_totals: dict[str, int],
    ytd_family: dict[tuple[str, int], int],
) -> dict[str, tuple[str, list[dict]]]:
    """One DM per PAX: personal single vs multi-award list."""
    by_pax: dict[str, list[dict]] = {}
    for g in grants:
        by_pax.setdefault(str(g["pax_id"]), []).append(g)
    out: dict[str, tuple[str, list[dict]]] = {}
    for pax_id, group in by_pax.items():
        tag = mention(pax_id, names.get(pax_id), known_ids=known_ids)
        if len(group) == 1:
            g = group[0]
            label = _family_label(g["rule"])
            total = ytd_totals.get(pax_id, 0)
            idx = ytd_family.get((pax_id, int(g["achievement_id"])), 0)
            ending = ordinal_suffix(idx)
            text = (
                f"Hey {tag} you just unlocked the achievement {label}! "
                f"You earned this {_earned_at(g)}. "
                f"This is achievement #{total} in {year} for you and the {idx}{ending} "
                f"time this year you've earned this award. Keep up the good work!"
            )
        else:
            bullets = [
                f"{_family_label(g['rule'])}, earned {_earned_at(g)}" for g in group
            ]
            text = (
                f"Hey {tag} you just earned {len(group)} achievements! Keep up the good work!\n"
                + "\n".join(bullets)
            )
        out[pax_id] = (text, [section(text)])
    return out


def _revoke_period_label(row: dict) -> str:
    return spoken_period(row.get("period_start"), row.get("period_end"), row.get("period") or "year")


def _revoke_backblast_link(row: dict, *, webhook: bool) -> str | None:
    ao_id = row.get("trigger_ao_id") or row.get("ao_id")
    ts = row.get("trigger_timestamp") or row.get("timestamp")
    d = row.get("trigger_date") or row.get("date_awarded")
    if not (ao_id and ts and d):
        return None
    label = format_date_label(_as_date(d))
    return date_link(d, ao_id, ts, label=label)


def channel_revoke_message(
    row: dict,
    *,
    names: dict[str, str],
    known_ids: set[str] | None,
    webhook: bool,
) -> tuple[str, list[dict]]:
    tag = mention(row["pax_id"], names.get(row["pax_id"]), known_ids=known_ids)
    name = (row.get("rule") or {}).get("name") or "achievement"
    period = _revoke_period_label(row)
    link = _revoke_backblast_link(row, webhook=webhook)
    if link:
        text = (
            f"Correction: {tag}'s award for *{name}* during period {period} "
            f"was revoked after attendance was updated on {link}."
        )
    else:
        text = (
            f"Correction: {tag}'s award for *{name}* during period {period} "
            f"was revoked after attendance was updated."
        )
    return text, [section(text)]


def dm_revoke_messages(
    revokes: list[dict],
    *,
    names: dict[str, str],
    known_ids: set[str] | None,
    webhook: bool,
) -> dict[str, tuple[str, list[dict]]]:
    by_pax: dict[str, list[dict]] = {}
    for row in revokes:
        by_pax.setdefault(str(row["pax_id"]), []).append(row)
    out: dict[str, tuple[str, list[dict]]] = {}
    for pax_id, group in by_pax.items():
        tag = mention(pax_id, names.get(pax_id), known_ids=known_ids)
        lines = []
        for row in group:
            name = (row.get("rule") or {}).get("name") or "achievement"
            period = _revoke_period_label(row)
            link = _revoke_backblast_link(row, webhook=webhook)
            if link:
                lines.append(
                    f"your award for *{name}* during period {period} was revoked "
                    f"after attendance was updated for {link}"
                )
            else:
                lines.append(
                    f"your award for *{name}* during period {period} was revoked "
                    f"after attendance was updated"
                )
        if len(lines) == 1:
            text = (
                f"Correction: Hey {tag}, just letting you know that {lines[0]}. "
                f"Keep showing up and you'll get it back!"
            )
        else:
            bullets = "\n".join(lines)
            text = (
                f"Correction: Hey {tag}, just letting you know that {len(lines)} awards "
                f"were revoked after attendance was updated. Keep showing up and you'll get them back!\n"
                f"{bullets}"
            )
        out[pax_id] = (text, [section(text)])
    return out


def grant_log_line(grant: dict, display_name: str | None) -> str:
    name = (grant.get("rule") or {}).get("name") or "achievement"
    period = spoken_period(
        grant.get("period_start"), grant.get("period_end"), grant.get("period") or "year"
    )
    d = _as_date(grant.get("date_awarded"))
    label = format_date_label(d) if d else "this event"
    link = date_link(grant.get("date_awarded"), grant.get("ao_id"), grant.get("timestamp"), label=label)
    who = plain_name(grant.get("pax_id"), display_name)
    return (
        f"Achievement *{name}* was granted to `{who}` for period {period} "
        f"after posting at {link}"
    )


def revoke_log_line(
    row: dict,
    display_name: str | None,
    *,
    webhook: bool,
) -> str:
    name = (row.get("rule") or {}).get("name") or "achievement"
    period = _revoke_period_label(row)
    who = plain_name(row.get("pax_id"), display_name)
    d = _as_date(row.get("trigger_date") or row.get("date_awarded"))
    ao_id = row.get("trigger_ao_id") or row.get("ao_id")
    ts = row.get("trigger_timestamp") or row.get("timestamp")
    if webhook:
        label = format_date_label(d) if d else "this event"
        link = date_link(d, ao_id, ts, label=label) if (ao_id and ts) else None
        suffix = f" after an edit on {link}" if link else " after an edit"
    elif ao_id and ts and d:
        label = format_date_label(d)
        link = date_link(d, ao_id, ts, label=label)
        suffix = f" after attendance no longer qualified at {link}"
    else:
        suffix = " after attendance no longer qualified"
    return f"Achievement *{name}* was revoked from `{who}` for period {period}{suffix}"


def run_summary_line(
    *,
    kind: str,
    granted: int,
    revoked: int,
    held: int,
    held_grandfathered: int,
    held_older_version: int,
    held_out_of_range: int,
    rules: int,
    start: date | None,
    end: date | None,
    channel: str | None,
    dms: int,
    dm_failed: int,
    duration_s: float | None,
    actor: str | None = None,
    achievement_name: str | None = None,
    automatic: bool = False,
) -> str:
    held_bits = []
    if held_grandfathered:
        held_bits.append(f"{held_grandfathered} grandfathered")
    if held_older_version:
        held_bits.append(f"{held_older_version} older version")
    if held_out_of_range:
        held_bits.append(f"{held_out_of_range} out of range")
    held_detail = f" ({', '.join(held_bits)})" if held_bits else ""
    span = format_iso_range(start, end)
    if kind == "backfill":
        who = ticked_display_name(actor, fallback="admin")
        name = achievement_name or "achievement"
        span = format_iso_range(start, end) or (
            f"{start.isoformat() if start else 'all-time'} to {end.isoformat() if end else 'present'}"
        )
        unchanged = held
        header = (
            "Achievement re-evaluate ran automatically after a rule change"
            if automatic
            else f"Achievement re-evaluate triggered by {who}"
        )
        return format_log_message(
            header,
            status="success",
            duration_s=duration_s,
            fields=[
                ("Achievement", name),
                ("Results", f"{granted} granted, {revoked} revoked, {unchanged} unchanged{held_detail}"),
                ("Period", span),
            ],
        )

    results = (
        f"{rules} rules, {granted} granted, {revoked} revoked, "
        f"{held} held{held_detail}"
    )
    dest_parts = []
    if channel:
        dest_parts.append(channel)
    if dms or dm_failed:
        fail = f" ({dm_failed} failed)" if dm_failed else ""
        dest_parts.append(f"{dms} DMs{fail}")
    return format_log_message(
        f"The *Achievements ({kind})* job was run as scheduled",
        status="success",
        duration_s=duration_s,
        fields=[
            ("Results", results),
            ("Period", span or ""),
        ],
        destinations=", ".join(dest_parts) if dest_parts else None,
    )


def reconcile_channel_line(name: str, granted: int, revoked: int, unchanged: int) -> str:
    del unchanged  # public copy no longer reports the unchanged count
    return (
        f"Achievement *{name}* was corrected "
        f"({granted} granted, {revoked} revoked)."
    )
