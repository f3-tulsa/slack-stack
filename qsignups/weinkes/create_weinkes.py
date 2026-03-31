#!/usr/bin/env python3
"""
Generate weekly weinke PNGs from qsignups data, upload to S3, update DB URLs.

Run from repo with env vars set (see qsignups/.env or root .env.deploy.test). Requires
AWS credentials with s3:PutObject on IMAGE_S3_BUCKET (same bucket as slackblast images).
"""
from __future__ import annotations

import logging
import os
import ssl
import sys
from datetime import date, timedelta
from pathlib import Path

import boto3
import dataframe_image as dfi
import pandas as pd
import pymysql
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

_WEINKES_DIR = Path(__file__).resolve().parent / "weinkes"
_PKG = Path(__file__).resolve().parent.parent / "qsignups" / "qsignups"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))
from field_encryption import decrypt_field  # noqa: E402

load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _db_connect():
    host = os.environ["DATABASE_HOST"]
    user = os.environ["DATABASE_USER"]
    password = os.environ["DATABASE_PASSWORD"]
    database = os.environ["DATABASE_SCHEMA"]
    port = int(os.environ.get("DATABASE_PORT", "4000"))
    tls = _env_bool("DATABASE_TLS_ENABLED", True)
    kw: dict = {
        "host": host,
        "user": user,
        "password": password,
        "database": database,
        "port": port,
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
    }
    if tls:
        kw["ssl"] = ssl.create_default_context()
    return pymysql.connect(**kw)


def upload_weinke_s3(weinke_name: str) -> str:
    """Upload weinkes/{weinke_name}.png to IMAGE_S3_BUCKET; return public HTTPS URL."""
    bucket = (os.environ.get("IMAGE_S3_BUCKET") or "").strip()
    if not bucket:
        raise RuntimeError("IMAGE_S3_BUCKET must be set for weinke S3 upload")
    local_path = _WEINKES_DIR / f"{weinke_name}.png"
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    key = f"weinkes/{weinke_name}.png"
    s3 = boto3.client("s3")
    s3.upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={"ContentType": "image/png"},
    )
    return f"https://{bucket}.s3.amazonaws.com/{key}"


def highlight_cells(s):
    highlight_cells_list = []
    for cell in s:
        if cell is None:
            highlight_cells_list.append("background-color: #000000")
        elif "The Forge" in cell:
            highlight_cells_list.append("background-color: #c43b01")
        elif ("VQ" in cell) or ("AO Launch" in cell) or ("24 Hr Beatdown" in cell):
            highlight_cells_list.append("background-color: #004dcf")
        elif cell[0:4] == "OPEN":
            highlight_cells_list.append("background-color: #194D33")
        else:
            highlight_cells_list.append("background-color: #000000")
    return pd.Series(highlight_cells_list)


def main() -> None:
    _WEINKES_DIR.mkdir(parents=True, exist_ok=True)

    tomorrow_day_of_week = (date.today() + timedelta(days=1)).weekday()
    current_week_start = date.today() + timedelta(days=-tomorrow_day_of_week + 1)
    current_week_end = date.today() + timedelta(days=7 - tomorrow_day_of_week)
    next_week_start = current_week_start + timedelta(weeks=1)
    next_week_end = current_week_end + timedelta(weeks=1)

    with _db_connect() as mydb:
        df_regions = pd.read_sql("SELECT * FROM qsignups_regions", mydb)

    for _index, row in df_regions.iterrows():
        team_id = row["team_id"]
        logging.info("working on team %s...", team_id)

        sql_current = """
        SELECT m.*, a.ao_display_name, a.ao_location_subtitle
        FROM qsignups_master m
        LEFT JOIN qsignups_aos a
        ON m.team_id = a.team_id
          AND m.ao_channel_id = a.ao_channel_id
        WHERE m.team_id = %s
          AND m.event_date >= DATE(%s)
          AND m.event_date <= DATE(%s)
        ORDER BY m.ao_channel_id, m.event_date, m.event_time
        """

        sql_next = """
        SELECT m.*, a.ao_display_name, a.ao_location_subtitle
        FROM qsignups_master m
        LEFT JOIN qsignups_aos a
        ON m.team_id = a.team_id
          AND m.ao_channel_id = a.ao_channel_id
        WHERE m.team_id = %s
          AND m.event_date >= DATE(%s)
          AND m.event_date <= DATE(%s)
        ORDER BY m.ao_channel_id, m.event_date, m.event_time
        """

        try:
            with _db_connect() as mydb:
                df_current = pd.read_sql(
                    sql_current,
                    mydb,
                    params=(team_id, str(current_week_start), str(current_week_end)),
                    parse_dates=["event_date"],
                )
                df_next = pd.read_sql(
                    sql_next,
                    mydb,
                    params=(team_id, str(next_week_start), str(next_week_end)),
                    parse_dates=["event_date"],
                )
        except Exception:
            logging.exception("There was a problem pulling from the db")
            continue

        df_list = [
            [df_current, "current_week_weinke"],
            [df_next, "next_week_weinke"],
        ]

        for week in df_list:
            df = week[0].copy()
            output_name = week[1]
            if df.empty:
                continue

            try:
                df_prior = pd.read_csv(_WEINKES_DIR / f"{team_id}_{output_name}.csv")
                df_prior["event_time"] = df_prior["event_time"].astype(str).str.zfill(4)
                df_compare = df.compare(df_prior)
            except Exception as csv_exc:
                logging.debug(
                    "Prior weinke CSV missing or not comparable for %s (assume changed): %s",
                    output_name,
                    csv_exc,
                    exc_info=True,
                )
                df_compare = [1, 2, 3]

            if len(df_compare) < 1:
                continue

            df.to_csv(_WEINKES_DIR / f"{team_id}_{output_name}.csv", index=False)

            df["event_date_fmt"] = df["event_date"].dt.strftime("%m/%d")
            df.reset_index(inplace=True)

            df.loc[df["q_pax_name"].isna(), "q_pax_name"] = "OPEN!"
            df["q_pax_name"].replace(r"\s\(([\s\S]*?\))", "", regex=True, inplace=True)
            df["label"] = df["q_pax_name"] + "\n" + df["event_time"].astype(str)
            spec_ok = df["event_special"].notna()
            df.loc[spec_ok, "label"] = (
                df.loc[spec_ok, "q_pax_name"].astype(str)
                + "\n"
                + df.loc[spec_ok, "event_special"].astype(str)
                + "\n"
                + df.loc[spec_ok, "event_time"].astype(str)
            )
            df["AO\nLocation"] = df["ao_display_name"] + "\n" + df["ao_location_subtitle"]
            df["AO\nLocation2"] = df["AO\nLocation"].str.replace("The ", "")

            df2 = df.pivot(
                index="AO\nLocation",
                columns=["event_day_of_week", "event_date_fmt"],
                values="label",
            ).fillna("")

            df2.sort_index(axis=1, level=["event_date_fmt"], inplace=True)
            df2.columns = df2.columns.map("\n".join).str.strip("\n")
            df2.reset_index(inplace=True)

            df2["AO\nLocation2"] = df2["AO\nLocation"].str.replace("The ", "")
            df2.sort_values(by=["AO\nLocation2"], axis=0, inplace=True)
            df2.drop(["AO\nLocation2"], axis=1, inplace=True)
            df2.reset_index(inplace=True, drop=True)

            th_props = [
                ("font-size", "15px"),
                ("text-align", "center"),
                ("font-weight", "bold"),
                ("color", "#F0FFFF"),
                ("background-color", "#000000"),
                ("white-space", "pre-wrap"),
                ("border", "1px solid #F0FFFF"),
            ]
            td_props = [
                ("font-size", "15px"),
                ("text-align", "center"),
                ("white-space", "pre-wrap"),
                ("color", "#F0FFFF"),
                ("border", "1px solid #F0FFFF"),
            ]
            styles = [
                dict(selector="th", props=th_props),
                dict(selector="td", props=td_props),
            ]

            df_styled = df2.style.set_table_styles(styles).apply(highlight_cells).hide_index()
            png_name = f"{team_id}_{output_name}"
            dfi.export(df_styled, str(_WEINKES_DIR / f"{png_name}.png"))

            img_url = upload_weinke_s3(png_name)

            region_weinke_created = False
            region_upload_ts = None
            ssl_context = ssl.create_default_context()
            try:
                if (row["weekly_weinke_channel"] is not None) and (output_name == "current_week_weinke"):
                    token = decrypt_field(row["bot_token"])
                    slack_client = WebClient(token, ssl=ssl_context)
                    slack_client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
                    try:
                        if row["weekly_weinke_updated"] is not None:
                            slack_client.chat_delete(
                                channel=row["weekly_weinke_channel"],
                                ts=row["weekly_weinke_updated"],
                            )
                    except Exception:
                        logging.debug(
                            "weinke chat_delete skipped (post missing or not deletable)",
                            exc_info=True,
                        )
                    response = slack_client.files_upload(
                        file=str(_WEINKES_DIR / f"{png_name}.png"),
                        initial_comment="This week's schedule",
                        channels=row["weekly_weinke_channel"],
                    )
                    region_upload_ts = response["file"]["shares"]["public"][row["weekly_weinke_channel"]][0]["ts"]
                    region_weinke_created = True
            except Exception:
                logging.exception("There was a problem updating the weinke channel")

            if output_name not in ("current_week_weinke", "next_week_weinke"):
                continue

            try:
                with _db_connect() as mydb:
                    with mydb.cursor() as cur:
                        if region_weinke_created and region_upload_ts is not None:
                            cur.execute(
                                f"UPDATE qsignups_regions SET `{output_name}` = %s, "
                                "weekly_weinke_updated = %s WHERE team_id = %s",
                                (img_url, region_upload_ts, team_id),
                            )
                        else:
                            cur.execute(
                                f"UPDATE qsignups_regions SET `{output_name}` = %s WHERE team_id = %s",
                                (img_url, team_id),
                            )
                    mydb.commit()
            except Exception:
                logging.exception("There was a problem updating the database")


if __name__ == "__main__":
    main()
