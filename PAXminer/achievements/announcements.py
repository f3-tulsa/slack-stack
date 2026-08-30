"""Grouped achievement channel, DM, and paxminer_logs copy."""

from __future__ import annotations

import random
from datetime import date

from achievements.copy import (
    achievement_admin_log_line,
    admin_channel_line,
)
from achievements.period import backblast_archive_url, format_date_label, spoken_period
from scheduling import format_iso_range
from slack_blocks import section
from slack_util import (
    format_log_message,
    log_header,
    mention,
    ordinal_suffix,
    plain_name,
    ticked_display_name,
)

AWARD_OPENERS = (
    "Congrats :raised_hands: to our man",
    "T-Claps :clap: for our man",
    "Give a fist bump :punch: to our man",
    "Props :muscle: to our man",
    "Check out :eyes: our man",
    "Shout out :mega: to our man",
    "Hardware alert :trophy: for our man",
    "Nice work :100: by our man",
    "Ring the bell :bell: for our man",
    "Big day :tada: for our man",
    "Hats off :tophat: to our man",
    "Beast mode :weight_lifter: by our man",
    "Salute :saluting_face: our man",
)


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
    archive_base: str | None = None,
) -> str:
    d = _as_date(awarded_on)
    text = label or (format_date_label(d) if d else "this event")
    url = backblast_archive_url(ao_id, timestamp, archive_base=archive_base)
    if url:
        return f"<{url}|{text}>"
    return text


def _earned_at(grant: dict, archive_base: str | None = None) -> str:
    ao = ao_tag(grant.get("ao_id"))
    dlink = date_link(
        grant.get("date_awarded"),
        grant.get("ao_id"),
        grant.get("timestamp"),
        archive_base=archive_base,
    )
    if ao:
        return f"at {ao} on {dlink}"
    return f"on {dlink}"


def _family_label(rule: dict) -> str:
    name = rule.get("name") or "achievement"
    verb = rule.get("verb") or ""
    if verb:
        return f"*{name}* for {verb}"
    return f"*{name}*"


def pick_award_openers(n: int, rng: random.Random) -> list[str]:
    """Distinct openers for a batch, then with-replacement once the list is exhausted."""
    pool = list(AWARD_OPENERS)
    if n <= 0:
        return []
    if n <= len(pool):
        return rng.sample(pool, n)
    chosen = rng.sample(pool, len(pool))
    chosen.extend(rng.choices(pool, k=n - len(pool)))
    return chosen


def _award_reactions(rule: dict | None) -> list[str]:
    names = ["fire"]
    extra = str((rule or {}).get("emoji") or "").strip().strip(":")
    if extra and extra not in names:
        names.append(extra)
    return names


def channel_grant_messages(
    grants: list[dict],
    *,
    year: int,
    names: dict[str, str],
    known_ids: set[str] | None,
    ytd_totals: dict[str, int],
    ytd_family: dict[tuple[str, int], int] | None = None,
    rng: random.Random | None = None,
    archive_base: str | None = None,
) -> list[tuple[str, list[dict], list[str]]]:
    """One public message per grant. ``rng`` pins opener selection in tests."""
    del year, ytd_family
    picker = rng or random.Random()
    openers = pick_award_openers(len(grants), picker)
    out: list[tuple[str, list[dict], list[str]]] = []
    for g, opener in zip(grants, openers):
        rule = g.get("rule") or {}
        label = _family_label(rule)
        tag = mention(g["pax_id"], names.get(g["pax_id"]), known_ids=known_ids)
        total = ytd_totals.get(g["pax_id"], 0)
        text = (
            f"{opener} {tag} who just unlocked the achievement {label}! "
            f"He earned this {_earned_at(g, archive_base)}. "
            f"This is achievement #{total} this year. Encourage this HIM to keep it up!"
        )
        out.append((text, [section(text)], _award_reactions(rule)))
    return out


def dm_grant_messages(
    grants: list[dict],
    *,
    year: int,
    names: dict[str, str],
    known_ids: set[str] | None,
    ytd_totals: dict[str, int],
    ytd_family: dict[tuple[str, int], int],
    archive_base: str | None = None,
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
                f"You earned this {_earned_at(g, archive_base)}. "
                f"This is achievement #{total} in {year} for you and the {idx}{ending} "
                f"time this year you've earned this award. Keep up the good work!"
            )
        else:
            bullets = [
                f"{_family_label(g['rule'])}, earned {_earned_at(g, archive_base)}"
                for g in group
            ]
            text = (
                f"Hey {tag} you just earned {len(group)} achievements! Keep up the good work!\n"
                + "\n".join(bullets)
            )
        out[pax_id] = (text, [section(text)])
    return out


def _revoke_period_label(row: dict) -> str:
    return spoken_period(row.get("period_start"), row.get("period_end"), row.get("period") or "year")


def dm_revoke_messages(
    revokes: list[dict],
    *,
    names: dict[str, str],
    known_ids: set[str] | None,
    archive_base: str | None = None,
) -> dict[str, tuple[str, list[dict]]]:
    """One DM per PAX. The revoke is private, names the period, and points at the ITQ."""
    del archive_base
    by_pax: dict[str, list[dict]] = {}
    for row in revokes:
        by_pax.setdefault(str(row["pax_id"]), []).append(row)
    out: dict[str, tuple[str, list[dict]]] = {}
    for pax_id, group in by_pax.items():
        tag = mention(pax_id, names.get(pax_id), known_ids=known_ids)
        contact = "If you believe this is an error, please contact your ITQ."
        if len(group) == 1:
            row = group[0]
            name = (row.get("rule") or {}).get("name") or "achievement"
            text = (
                f"Correction: {tag}, your award for *{name}* ({_revoke_period_label(row)}) "
                f"was revoked during a re-evaluation. {contact}"
            )
        else:
            bullets = "\n".join(
                f"• *{(row.get('rule') or {}).get('name') or 'achievement'}* "
                f"({_revoke_period_label(row)})"
                for row in group
            )
            text = (
                f"Correction: {tag}, {len(group)} of your awards were revoked during a "
                f"re-evaluation. {contact}\n{bullets}"
            )
        out[pax_id] = (text, [section(text)])
    return out


def award_log_line(
    row: dict,
    display_name: str | None,
    *,
    granted: bool,
    archive_base: str | None = None,
) -> str:
    name = (row.get("rule") or {}).get("name") or "achievement"
    period = spoken_period(
        row.get("period_start"), row.get("period_end"), row.get("period") or "year"
    )
    who = plain_name(row.get("pax_id"), display_name)
    verb = "granted to" if granted else "revoked from"
    ao_id = row.get("trigger_ao_id") or row.get("ao_id")
    ts = row.get("trigger_timestamp") or row.get("timestamp")
    d = row.get("trigger_date") or row.get("date_awarded")
    label = format_date_label(_as_date(d)) if _as_date(d) else None
    link = (
        date_link(d, ao_id, ts, label=label, archive_base=archive_base)
        if (ao_id and ts and label)
        else None
    )
    suffix = f" after evaluating the Backblast from {link}" if link else ""
    return f"Achievement *{name}* was {verb} `{who}` for period {period}{suffix}."


def grant_log_line(
    grant: dict, display_name: str | None, *, archive_base: str | None = None
) -> str:
    return award_log_line(grant, display_name, granted=True, archive_base=archive_base)


def revoke_log_line(
    row: dict,
    display_name: str | None,
    *,
    webhook: bool = False,
    archive_base: str | None = None,
) -> str:
    del webhook
    return award_log_line(row, display_name, granted=False, archive_base=archive_base)


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
    action: str | None = None,
    code: str | None = None,
    version: str | None = None,
    rules_text: str | None = None,
    affected_pax: int | None = None,
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
        del automatic
        name = achievement_name or "achievement"
        span = format_iso_range(start, end) or (
            f"{start.isoformat() if start else 'all-time'} to {end.isoformat() if end else 'present'}"
        )
        return achievement_admin_log_line(
            name=name,
            action=action or "re-evaluated",
            author=actor,
            status="success",
            duration_s=duration_s,
            code=code,
            version=version,
            rules=rules_text,
            period=span,
            granted=granted,
            revoked=revoked,
            unchanged=held,
            affected_pax=affected_pax,
            held_detail=held_detail,
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
        log_header("Achievements"),
        status="success",
        duration_s=duration_s,
        fields=[
            ("Mode", kind),
            ("Results", results),
            ("Period", span or ""),
        ],
        destinations=", ".join(dest_parts) if dest_parts else None,
    )


def reconcile_channel_line(
    name: str,
    granted: int,
    revoked: int,
    unchanged: int,
    *,
    action: str = "changed",
    admin_mention: str = "`admin`",
) -> str | None:
    """Back-compat wrapper around admin_channel_line."""
    return admin_channel_line(
        action,
        name,
        admin_mention,
        granted=granted,
        revoked=revoked,
        unchanged=unchanged,
    )
