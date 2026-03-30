#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script queries Slack for all PAX Users and their respective beatdown attendance. It then generates bar graphs
on attendance for each member and sends it to them in a private Slack message.
'''

from __future__ import annotations

import datetime
import logging
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

_PAX_ROOT = Path(__file__).resolve().parent.parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))


def _pax_charter_period():
    off = int(os.environ.get("CHART_PERIOD_OFFSET_DAYS", "7"))
    d = datetime.datetime.now() - datetime.timedelta(days=off)
    return d.strftime("%m"), d.strftime("%b"), d.strftime("%B"), d.strftime("%Y")


def run_pax_charter(
    mydb,
    slack_token: str,
    schema: str,
    plot_dir: str | Path = "/tmp/paxminer_plots",
    region_method: str = "v2",
    log_to_file: bool = False,
) -> dict:
    """
    Build per-PAX attendance charts and DM via Slack (v2) or legacy channel upload (v1).
    """
    plot_base = Path(plot_dir) / schema
    plot_base.mkdir(parents=True, exist_ok=True)

    if log_to_file:
        log_dir = _PAX_ROOT / "logs" / schema
        log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(log_dir / "PAXcharter_error.log"),
            filemode="a",
            format="%(asctime)s %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            level=logging.INFO,
            force=True,
        )
    slack = WebClient(token=slack_token)
    rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=5)
    slack.retry_handlers.append(rate_limit_handler)

    thismonthname, _, _, yearnum = _pax_charter_period()

    column_names = ["user_id", "user_name", "real_name"]
    users_df = pd.DataFrame(columns=column_names)
    users_df.loc[len(users_df.index)] = ["APP", "BackblastApp", "BackblastApp"]
    data = ""
    while True:
        users_response = slack.users_list(limit=1000, cursor=data)
        response_metadata = users_response.get("response_metadata", {})
        next_cursor = response_metadata.get("next_cursor")
        users = users_response.data["members"]
        users_df_tmp = pd.json_normalize(users)
        users_df_tmp = users_df_tmp[["id", "profile.display_name", "profile.real_name"]]
        users_df_tmp = users_df_tmp.rename(
            columns={"id": "user_id", "profile.display_name": "user_name", "profile.real_name": "real_name"}
        )
        users_df = pd.concat([users_df, users_df_tmp], ignore_index=True)
        if next_cursor:
            data = next_cursor
        else:
            break

    for _index, row in users_df.iterrows():
        un_tmp = row["user_name"]
        rn_tmp = row["real_name"]
        if un_tmp == "":
            row["user_name"] = rn_tmp

    def send_slack_message(channel, message, file):
        return slack.files_upload(channels=channel, initial_comment=message, file=file)

    def send_slack_message_v2(user_id, message, file):
        response = slack.conversations_open(users=user_id)
        channel = response["channel"]["id"]
        return slack.files_upload_v2(channel=channel, initial_comment=message, file=file)

    def log_message_sent_error(user_id_tmp, db_name, pax, exc: Exception):
        err = getattr(exc, "response", None)
        if isinstance(err, dict) and "error" in err:
            logging.warning("Error initiating conversation: %s", err["error"])
        else:
            logging.warning("Slack error: %s", exc, exc_info=True)
        log_path = plot_base.parent.parent / "logs" / db_name / "PAXcharter.log"
        if log_to_file:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"Error: {user_id_tmp}\n")
        logging.warning("Slack Error - Message not sent: pax=%s user_id=%s", pax, user_id_tmp)

    def success_message_sent(user_id_tmp, pax, db_name):
        if log_to_file:
            log_path = plot_base.parent.parent / "logs" / db_name / "PAXcharter.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{user_id_tmp} {pax}\n")

    total_graphs = 0
    current_method = region_method
    for user_id in users_df["user_id"]:
        try:
            attendance_tmp_df = pd.DataFrame([])
            with mydb.cursor() as cursor:
                sql = "SELECT * FROM attendance_view WHERE PAX = (SELECT user_name FROM users WHERE user_id = %s) AND YEAR(Date) = %s ORDER BY Date"
                user_id_tmp = user_id
                val = (user_id_tmp, yearnum)
                cursor.execute(sql, val)
                attendance_tmp = cursor.fetchall()
                attendance_tmp_df = pd.DataFrame(attendance_tmp)
                month = []
                day = []
                year = []
                count = attendance_tmp_df.shape[0]
                if count > 0:
                    for Date in attendance_tmp_df["Date"]:
                        datee = datetime.datetime.strptime(str(Date), "%Y-%m-%d")
                        month.append(datee.strftime("%B"))
                        day.append(datee.day)
                        year.append(datee.year)
                    pax = attendance_tmp_df.iloc[0]["PAX"]
                    attendance_tmp_df["Month"] = month
                    attendance_tmp_df["Day"] = day
                    attendance_tmp_df["Year"] = year
                    attendance_tmp_df.sort_values(by=["Date"], inplace=True)
                    ax = attendance_tmp_df.groupby(["Month", "AO"], sort=False).size().unstack().plot(
                        kind="bar", stacked=True
                    )
                    total_count_for_year = attendance_tmp_df.shape[0]
                    ax.text(
                        0.95,
                        0.95,
                        f"Total: {total_count_for_year}",
                        transform=ax.transAxes,
                        fontsize=12,
                        verticalalignment="top",
                        horizontalalignment="right",
                    )
                    plt.title("Number of posts by " + pax + " by AO/Month for " + yearnum)
                    plt.legend(loc="center left", bbox_to_anchor=(1, 0.5), frameon=False)
                    plt.ioff()
                    out_jpg = plot_base / f"{user_id_tmp}_{thismonthname}{yearnum}.jpg"
                    plt.savefig(str(out_jpg), bbox_inches="tight")
                    total_graphs += 1
                    message = (
                        "Hey "
                        + pax
                        + "! Here is your monthly posting summary for "
                        + yearnum
                        + ". \nPush yourself, get those bars higher every month! SYITG!"
                    )
                    file = str(out_jpg)
                    if total_graphs > 0:
                        if current_method == "v2":
                            try:
                                send_slack_message_v2(user_id_tmp, message, file)
                                success_message_sent(user_id_tmp, pax, schema)
                            except SlackApiError as e:
                                err = e.response.get("error") if e.response else None
                                if err == "missing_scope":
                                    logging.error(
                                        "PAX charter: missing_scope — add im:write to Slack app; falling back to v1"
                                    )
                                    current_method = "v1"
                                else:
                                    log_message_sent_error(user_id_tmp, schema, pax, e)
                                    raise e
                            except Exception as e:
                                log_message_sent_error(user_id_tmp, schema, pax, e)
                                raise e
                        if current_method != "v2":
                            try:
                                channel = user_id_tmp
                                send_slack_message(channel, message, file)
                                success_message_sent(user_id_tmp, pax, schema)
                            except Exception as e:
                                log_message_sent_error(user_id_tmp, schema, pax, e)
                                raise e
                    else:
                        logging.debug("PAX charter skipped (no graphs): %s", pax)
        except Exception:
            logging.exception("PAX charter: exception for user_id=%s", user_id)
        finally:
            plt.close("all")

    return {"schema": schema, "graphs": total_graphs}


if __name__ == "__main__":
    from paxminer_db import connect_from_credentials_ini

    db = sys.argv[1]
    key = sys.argv[2]
    mydb = connect_from_credentials_ini(db)
    try:
        run_pax_charter(mydb, key, db, plot_dir="../plots", log_to_file=True)
    finally:
        mydb.close()
