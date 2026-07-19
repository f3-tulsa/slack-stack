"""
AWS Lambda entry points for PAXminer user/channel sync, monthly charts,
achievements (daily + webhook), and Kotter reports.

Slack interactivity (/config-paxminer and config modals) lives in slack_app.py
on the lightweight SlackFunction.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_ROOT = Path(__file__).resolve().parent
_CHART_DIR = Path(_ROOT, "monthly_charts")

for _p in (_ROOT, _ROOT / "database_management", _ROOT / "achievements", _ROOT / "kotter"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from common.encryption import decrypt_field, require_encryption_key
from paxminer_db import connect_from_env, paxminer_schema_from_env
from slack_http import (
    http_response,
    is_http_request,
    raw_body,
    verify_achievements_webhook_secret,
)

require_encryption_key()


def _registry_database() -> str:
    return (
        os.environ.get("PAXMINER_REGISTRY_DATABASE")
        or os.environ.get("ADMIN_DATABASE_SCHEMA")
        or os.environ.get("PAXMINER_SCHEMA")
        or "paxminer"
    ).strip()


def _pm_schema() -> str:
    return paxminer_schema_from_env()


def _try_bootstrap_pm_slack_token() -> None:
    f3 = os.environ.get("F3_REGION_NAME", "").strip()
    st = os.environ.get("STAGE", "").strip()
    tok = os.environ.get("PM_SLACK_TOKEN", "").strip()
    if not (f3 and st and tok):
        return
    from common.token_bootstrap import upsert_paxminer_slack_token

    registry = _pm_schema()
    regional = f"{f3}_{st}"
    upsert_paxminer_slack_token(
        registry_schema=registry,
        region_key=f3,
        regional_schema_name=regional,
        plaintext_token=tok,
    )


try:
    _try_bootstrap_pm_slack_token()
except Exception:
    logging.exception("Token bootstrap failed (non-fatal)")


def _chart_plot_dir() -> str:
    return os.environ.get("CHART_PLOT_DIR", "/tmp/paxminer_plots")


def _load_charter_module(module_file_stem: str):
    path = _CHART_DIR / f"{module_file_stem}.py"
    spec = importlib.util.spec_from_file_location(f"paxminer_charter_{module_file_stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load charter module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sync_handler(event, context):
    logging.info("PAXminer sync_handler start")
    os.environ.setdefault("host", os.environ.get("DATABASE_HOST", ""))
    os.environ.setdefault("user", os.environ.get("DATABASE_USER", ""))
    os.environ.setdefault("password", os.environ.get("DATABASE_PASSWORD", ""))
    try:
        from PAXminer_SlackUserUpdate import database_management_update

        database_management_update()
        return {"statusCode": 200, "body": json.dumps({"ok": True, "mode": "sync"})}
    except Exception:
        logging.exception("PAXminer sync failed")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": traceback.format_exc()})}


def chart_handler(event, context):
    logging.info("PAXminer chart_handler start")
    from paxminer_db import connect_from_env
    from schedule_runner import use_schedule_dispatcher

    if use_schedule_dispatcher():
        logging.info("PM_USE_SCHEDULE_DISPATCHER set — skipping legacy chart_handler")
        return {
            "statusCode": 200,
            "body": json.dumps({"ok": True, "skipped": "schedule_dispatcher"}),
        }

    pm = _pm_schema()
    registry_db = _registry_database()
    plot_dir = _chart_plot_dir()
    region_regex = os.environ.get("CHART_REGION_REGEX", "").strip() or None

    pax_mod = _load_charter_module("PAXcharter")
    q_mod = _load_charter_module("Qcharter")
    lb_mod = _load_charter_module("Leaderboard_Charter")
    lb_ao_mod = _load_charter_module("LeaderboardByAO_Charter")

    results: list[dict] = []
    registry = None
    try:
        registry = connect_from_env(registry_db)
        with registry.cursor() as cur:
            cur.execute(f"SELECT * FROM `{pm}`.`regions` WHERE active = 1")
            regions = cur.fetchall()

        for row in regions:
            region = row.get("region")
            _tok = row.get("slack_token")
            schema_name = row.get("schema_name")
            firstf = row.get("firstf_channel")
            try:
                key = decrypt_field(_tok) if _tok else None
            except Exception as e:
                logging.warning("Skipping region %s: cannot decrypt token: %s", region, e)
                continue
            if not key or not schema_name:
                continue
            regional = None
            try:
                regional = connect_from_env(schema_name)
                if row.get("send_pax_charts") and firstf:
                    if region_regex:
                        pat = rf"^[{region_regex}]"
                        if re.match(pat, str(region or ""), re.I):
                            r = pax_mod.run_pax_charter(regional, key, schema_name, plot_dir=plot_dir)
                            results.append({"region": region, "pax_charts": r})
                    else:
                        r = pax_mod.run_pax_charter(regional, key, schema_name, plot_dir=plot_dir)
                        results.append({"region": region, "pax_charts": r})
                if row.get("send_q_charts") and firstf:
                    r = q_mod.run_q_charter(regional, key, schema_name, region, firstf, plot_dir=plot_dir)
                    results.append({"region": region, "q_charts": r})
                if row.get("send_region_leaderboard") and firstf:
                    r = lb_mod.run_region_leaderboard(regional, key, schema_name, region, firstf, plot_dir=plot_dir)
                    results.append({"region": region, "region_leaderboard": r})
                if row.get("send_ao_leaderboard") and firstf:
                    r = lb_ao_mod.run_ao_leaderboard(regional, key, schema_name, region, firstf, plot_dir=plot_dir)
                    results.append({"region": region, "ao_leaderboard": r})
                if row.get("send_achievement_leaderboard") and row.get("achievement_channel"):
                    from achievements.leaderboard import run_leaderboard_for_region

                    lb = run_leaderboard_for_region(registry, pm, row)
                    results.append({"region": region, "achievement_leaderboard": lb})
            except Exception as e:
                logging.exception("Chart error for region %s: %s", region, e)
                results.append({"region": region, "error": str(e)})
            finally:
                if regional:
                    regional.close()
        return {"statusCode": 200, "body": json.dumps({"ok": True, "results": results})}
    except Exception:
        logging.exception("PAXminer chart_handler failed")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": traceback.format_exc()})}
    finally:
        if registry:
            registry.close()


def achievements_handler(event, context):
    logging.info("PAXminer achievements_handler start")
    from achievements.leaderboard import run_leaderboard
    from achievements.runner import run_achievements_for_region, run_daily

    pm = _pm_schema()
    registry_db = _registry_database()

    if is_http_request(event):
        headers = (event or {}).get("headers") or {}
        if not verify_achievements_webhook_secret(headers):
            return http_response(401, {"ok": False, "error": "Unauthorized"})
        try:
            body = json.loads(raw_body(event) or "{}")
        except json.JSONDecodeError:
            return http_response(400, {"ok": False, "error": "Invalid JSON"})
        schema = body.get("schema")
        pax_ids = set(body.get("pax_user_ids") or [])
        post_to_ao = bool(body.get("post_to_ao"))
        ao_channel_id = body.get("ao_channel_id")
        conn = connect_from_env(registry_db)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM `{pm}`.`regions` WHERE schema_name=%s LIMIT 1", (schema,))
                region_row = cur.fetchone()
            if not region_row:
                return http_response(404, {"ok": False, "error": "Region not found"})
            result = run_achievements_for_region(
                conn,
                pm_schema=pm,
                regional_schema=schema,
                region_row=region_row,
                pax_user_ids=pax_ids or None,
                post_to_ao=post_to_ao,
                ao_channel_id=ao_channel_id,
            )
            return http_response(200, {"ok": True, "result": result})
        finally:
            conn.close()

    event = event or {}
    if event.get("source") == "smoke" and event.get("feature") == "achievement_leaderboard":
        conn = connect_from_env(registry_db)
        try:
            results = run_leaderboard(conn, pm, dry_run=True)
            return {"statusCode": 200, "body": json.dumps({"ok": True, "results": results})}
        finally:
            conn.close()

    dry_run = event.get("source") == "smoke"
    conn = connect_from_env(registry_db)
    try:
        results = run_daily(conn, pm, dry_run=dry_run)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "results": results})}
    except Exception:
        logging.exception("achievements failed")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": traceback.format_exc()})}
    finally:
        conn.close()


def kotter_handler(event, context):
    """Scheduled / async-invoked Kotter runner (no Slack HTTP — see slack_app)."""
    logging.info("PAXminer kotter_handler start")
    from kotter.kotter_report import run_kotter
    from schedule_runner import use_schedule_dispatcher

    # Smoke tests still run; monthly EventBridge is skipped when the unified
    # schedule dispatcher owns cadence. Manual Kotter is via Schedule Run Now.
    event = event or {}
    source = str(event.get("source") or "")
    if use_schedule_dispatcher() and source != "smoke":
        logging.info("PM_USE_SCHEDULE_DISPATCHER set — skipping legacy kotter schedule")
        return http_response(200, {"ok": True, "skipped": "schedule_dispatcher"})

    pm = _pm_schema()
    registry_db = _registry_database()

    dry_run = event.get("source") == "smoke"
    conn = connect_from_env(registry_db)
    try:
        results = run_kotter(conn, pm, dry_run=dry_run)
        return http_response(200, {"ok": True, "results": results})
    except Exception:
        logging.exception("kotter failed")
        return http_response(500, {"ok": False, "error": traceback.format_exc()})
    finally:
        conn.close()


def schedule_handler(event, context):
    """
    Unified schedule dispatcher (EventBridge rate(15 minutes) + Run Now fan-out).

    Modes:
      - tick / empty: evaluate due schedules and async-invoke per heavy item
      - {schedule_id, force?}: run one item inline
      - {source: smoke, dry_run: true}: dry-run due list
    """
    logging.info("PAXminer schedule_handler start")
    from schedule_runner import (
        async_invoke_schedule_item,
        list_due_schedules,
        run_one_schedule_item,
        use_schedule_dispatcher,
    )

    event = event or {}
    pm = _pm_schema()
    registry_db = _registry_database()
    dry_run = bool(event.get("dry_run")) or event.get("source") == "smoke"

    # Fan-out / Run Now path
    if event.get("schedule_id") is not None:
        conn = connect_from_env(registry_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM `{pm}`.`region_schedules` WHERE id=%s",
                    (int(event["schedule_id"]),),
                )
                row = cur.fetchone()
            if not row:
                return {"statusCode": 404, "body": json.dumps({"ok": False, "error": "not found"})}
            result = run_one_schedule_item(
                conn,
                pm,
                row,
                dry_run=dry_run,
                force=bool(event.get("force")),
            )
            return {"statusCode": 200, "body": json.dumps({"ok": True, "result": result})}
        except Exception:
            logging.exception("schedule item failed")
            return {"statusCode": 500, "body": json.dumps({"ok": False, "error": traceback.format_exc()})}
        finally:
            conn.close()

    if not use_schedule_dispatcher() and event.get("source") != "smoke":
        logging.info("PM_USE_SCHEDULE_DISPATCHER off — schedule tick no-op")
        return {"statusCode": 200, "body": json.dumps({"ok": True, "skipped": "dispatcher_off"})}

    conn = connect_from_env(registry_db)
    try:
        due = list_due_schedules(conn, pm)
        results: list[dict] = []
        for row in due:
            report_type = None
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT report_type FROM `{pm}`.`region_report_definitions` WHERE id=%s",
                    (row["report_definition_id"],),
                )
                d = cur.fetchone()
                report_type = (d or {}).get("report_type")
            heavy = report_type in ("pax_charts", "q_charts", "ao_leaderboard")
            if dry_run:
                results.append(
                    run_one_schedule_item(conn, pm, row, dry_run=True, force=True)
                )
            elif heavy and os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
                try:
                    async_invoke_schedule_item(int(row["id"]), force=False)
                    results.append({"schedule_id": row["id"], "ok": True, "queued": True})
                except Exception as e:
                    logging.exception("fanout failed schedule_id=%s", row["id"])
                    results.append({"schedule_id": row["id"], "ok": False, "error": str(e)})
            else:
                results.append(run_one_schedule_item(conn, pm, row, dry_run=False, force=False))
        return {
            "statusCode": 200,
            "body": json.dumps({"ok": True, "due": len(due), "results": results}),
        }
    except Exception:
        logging.exception("schedule_handler failed")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": traceback.format_exc()})}
    finally:
        conn.close()
