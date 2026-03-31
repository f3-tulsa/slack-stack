"""
AWS Lambda entry points for PAXminer user/channel sync and monthly charts.

Environment variables (TiDB / SAM):
  DATABASE_HOST, DATABASE_PORT, DATABASE_USER, DATABASE_PASSWORD
  DATABASE_TLS_ENABLED (default true)
  PAXMINER_SCHEMA (registry schema containing `regions` table)
  PAXMINER_REGISTRY_DATABASE — optional pymysql db name; defaults to PAXMINER_SCHEMA
  DATABASE_FULL_RUN — optional; truthy => full Slack user history pull (first-time style)
  CHART_PLOT_DIR — optional; default /tmp/paxminer_plots
  CHART_REGION_REGEX — optional; restrict PAX charts to region names matching ^[CHAR] (see PAXcharter_Monthly_Execution)
  STAGE, F3_REGION_NAME, PM_SLACK_TOKEN — optional; when set, encrypted bot token is upserted into paxminer.regions at cold start
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

# Configure root logging before cold-start bootstrap (token_bootstrap uses LOG.info).
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_ROOT = Path(__file__).resolve().parent
_CHART_DIR = Path(_ROOT, "monthly_charts")

for _p in (_ROOT, _ROOT / "database_management"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from common.encryption import decrypt_field, require_encryption_key

require_encryption_key()


def _registry_database() -> str:
    return (
        os.environ.get("PAXMINER_REGISTRY_DATABASE")
        or os.environ.get("ADMIN_DATABASE_SCHEMA")
        or os.environ.get("PAXMINER_SCHEMA")
        or "paxminer"
    ).strip()


def _pm_schema() -> str:
    return os.environ.get("PAXMINER_SCHEMA", "paxminer").strip() or "paxminer"


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
    logging.exception(
        "Token bootstrap failed (non-fatal); paxminer.regions slack_token may need manual upsert"
    )


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
    """Daily user + channel sync for all active regions (Slackblast / TiDB env names)."""
    os.environ.setdefault("host", os.environ.get("DATABASE_HOST", ""))
    os.environ.setdefault("user", os.environ.get("DATABASE_USER", ""))
    os.environ.setdefault("password", os.environ.get("DATABASE_PASSWORD", ""))
    try:
        from PAXminer_SlackUserUpdate import database_management_update

        database_management_update()
        return {"statusCode": 200, "body": json.dumps({"ok": True, "mode": "sync"})}
    except Exception:
        logging.exception("PAXminer sync failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"ok": False, "error": traceback.format_exc()}),
        }


def chart_handler(event, context):
    """Monthly charts: PAX, Q, region leaderboard, AO leaderboard per `regions` flags."""
    from paxminer_db import connect_from_env

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
                logging.warning("Skipping region missing token or schema: %s", region)
                continue

            regional = None
            try:
                regional = connect_from_env(schema_name)

                if row.get("send_pax_charts") and firstf:
                    if region_regex:
                        pat = rf"^[{region_regex}]"
                        if not re.match(pat, str(region or ""), re.I):
                            logging.info("Skip PAX charts (regex): %s", region)
                        else:
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
            except Exception as e:
                logging.exception("Chart error for region %s: %s", region, e)
                results.append({"region": region, "error": str(e)})
            finally:
                if regional:
                    try:
                        regional.close()
                    except Exception:
                        pass

        return {"statusCode": 200, "body": json.dumps({"ok": True, "results": results})}
    except Exception:
        logging.exception("PAXminer chart_handler failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"ok": False, "error": traceback.format_exc()}),
        }
    finally:
        if registry:
            try:
                registry.close()
            except Exception:
                pass
