#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script queries the AWS F3(region) database for all beatdown records. It then generates bar graphs
on Q's for each AO and sends it to the AO channel in a Slack message.
'''

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

_PAX_ROOT = Path(__file__).resolve().parent.parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))


def _q_charter_period():
    off = int(os.environ.get("CHART_PERIOD_OFFSET_DAYS", "7"))
    d = datetime.datetime.now() - datetime.timedelta(days=off)
    return d.strftime("%m"), d.strftime("%b"), d.strftime("%B"), d.strftime("%Y")


def run_q_charter(
    mydb,
    slack_token: str,
    schema: str,
    region: str,
    firstf: str,
    plot_dir: str | Path = "/tmp/paxminer_plots",
) -> dict:
    """
    Generate per-AO and region-wide Q charts and upload to Slack.

    ``mydb`` is an open PyMySQL connection to the regional schema.
    """
    plot_base = Path(plot_dir) / schema
    plot_base.mkdir(parents=True, exist_ok=True)

    slack = WebClient(token=slack_token)
    rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=5)
    slack.retry_handlers.append(rate_limit_handler)

    thismonth, thismonthname, thismonthnamelong, yearnum = _q_charter_period()
    total_ao_graphs = 0

    try:
        with mydb.cursor() as cursor:
            sql = "SELECT ao, channel_id FROM aos WHERE backblast = 1 and archived = 0"
            cursor.execute(sql)
            aos = cursor.fetchall()
            aos_df = pd.DataFrame(aos, columns=["ao", "channel_id"])
    finally:
        pass

    for _index, row in aos_df.iterrows():
        ao = row["ao"]
        channel_id = row["channel_id"]
        month: list = []
        day: list = []
        year: list = []
        with mydb.cursor() as cursor:
            sql = """
        select
            `B`.`bd_date` AS `Date`,
            `a`.`ao` AS `AO`,
            `U1`.`user_name` AS `Q`,
            `U1`.`app` AS `Q_Is_App`,
            `U2`.`user_name` AS `CoQ`,
            `B`.`pax_count` AS `pax_count`,
            `B`.`fngs` AS `fngs`,
            `B`.`fng_count` AS `fng_count`
        from
            (((`beatdowns` `B`
        left join `users` `U1` on
            ((`U1`.`user_id` = `B`.`q_user_id`)))
        left join `users` `U2` on
            ((`U2`.`user_id` = `B`.`coq_user_id`)))
        left join `aos` `a` on
            ((`a`.`channel_id` = `B`.`ao_id`)))
        WHERE `a`.`ao` = %s AND YEAR(`bd_date`) = %s AND MONTH(`bd_date`) = %s and `U1`.`app` != 1
        order by
            `B`.`bd_date`,
            `a`.`ao`
        """

            val = (ao, yearnum, thismonth)
            cursor.execute(sql, val)
            bd_tmp = cursor.fetchall()
            bd_tmp_df = pd.DataFrame(bd_tmp)
            if not bd_tmp_df.empty:
                for Date in bd_tmp_df["Date"]:
                    datee = datetime.datetime.strptime(str(Date), "%Y-%m-%d")
                    month.append(datee.strftime("%B"))
                    day.append(datee.day)
                    year.append(datee.year)
                bd_tmp_df["Month"] = month
                bd_tmp_df["Day"] = day
                bd_tmp_df["Year"] = year
                try:
                    melted_df = pd.melt(
                        bd_tmp_df, id_vars=["Month"], value_vars=["Q", "CoQ"], var_name="Role", value_name="TempQ"
                    )
                    melted_df = melted_df.dropna()
                    melted_df = melted_df.rename(columns={"TempQ": "Q"})
                    melted_df.groupby(["Q", "Month"]).size().unstack().sort_values(["Q"], ascending=True).plot(
                        kind="bar"
                    )
                    plt.title("Number of Qs by individual at " + ao + " for " + thismonthnamelong + ", " + yearnum)
                    plt.legend("")
                    plt.ioff()
                    out_path = plot_base / f"Q_Counts_{ao}_{thismonthname}{yearnum}.jpg"
                    plt.savefig(str(out_path), bbox_inches="tight")
                    slack.files_upload_v2(
                        channel=channel_id,
                        initial_comment="Hey "
                        + ao
                        + "! Here is a look at who has been stepping up to Q at this AO. Is your name on this list? Remember Core Principle #4 - F3 is peer led in a rotating fashion. Exercise your leadership muscles. Sign up to Q!",
                        file=str(out_path),
                        title="Test upload",
                    )
                    total_ao_graphs += 1
                    plt.close()
                except Exception as e:
                    print(e)
                    print("An Error Occurred in Sending")
                finally:
                    plt.close("all")

    summary_graphs = 0
    try:
        month = []
        day = []
        year = []
        with mydb.cursor() as cursor:
            sql = """
        select
            `B`.`bd_date` AS `Date`,
            `a`.`ao` AS `AO`,
            `U1`.`user_name` AS `Q`,
            `U1`.`app` AS `Q_Is_App`,
            `U2`.`user_name` AS `CoQ`,
            `B`.`pax_count` AS `pax_count`,
            `B`.`fngs` AS `fngs`,
            `B`.`fng_count` AS `fng_count`
        from
            (((`beatdowns` `B`
        left join `users` `U1` on
            ((`U1`.`user_id` = `B`.`q_user_id`)))
        left join `users` `U2` on
            ((`U2`.`user_id` = `B`.`coq_user_id`)))
        left join `aos` `a` on
            ((`a`.`channel_id` = `B`.`ao_id`)))
        WHERE YEAR(`bd_date`) = %s AND MONTH(`bd_date`) = %s and `U1`.`app` != 1
        order by
            `B`.`bd_date`,
            `a`.`ao`
        """
            val = (yearnum, thismonth)
            cursor.execute(sql, val)
            bd_tmp2 = cursor.fetchall()
            bd_tmp_df2 = pd.DataFrame(bd_tmp2)
            if not bd_tmp_df2.empty:
                for Date in bd_tmp_df2["Date"]:
                    datee = datetime.datetime.strptime(str(Date), "%Y-%m-%d")
                    month.append(datee.strftime("%B"))
                    day.append(datee.day)
                    year.append(datee.year)
                bd_tmp_df2["Month"] = month
                bd_tmp_df2["Day"] = day
                bd_tmp_df2["Year"] = year
                melted_df = pd.melt(
                    bd_tmp_df2, id_vars=["AO"], value_vars=["Q", "CoQ"], var_name="Role", value_name="TempQ"
                )
                melted_df = melted_df.dropna()
                melted_df = melted_df.rename(columns={"TempQ": "Q"})
                melted_df.groupby(["Q", "AO"]).size().unstack().plot(kind="bar", stacked=True, figsize=(25, 4))
                plt.title(
                    "Number of Qs by individual across all AOs for " + thismonthnamelong + ", " + yearnum
                )
                plt.legend(loc="center left", bbox_to_anchor=(1, 0.5), frameon=False)
                plt.ioff()
                out_path = plot_base / f"Q_Counts_{schema}_{thismonthname}{yearnum}.jpg"
                plt.savefig(str(out_path), bbox_inches="tight")
                slack.conversations_join(channel=firstf)
                slack.files_upload_v2(
                    channel=firstf,
                    initial_comment="Hey "
                    + region
                    + "! Here is a look at who has been stepping up to Q across all AOs for the month. Is your name on this list? Remember Core Principle #4 - F3 is peer led in a rotating fashion. Exercise your leadership muscles. Sign up to Q!",
                    file=str(out_path),
                )
                summary_graphs += 1
    finally:
        plt.close("all")

    return {"schema": schema, "ao_charts": total_ao_graphs, "summary_charts": summary_graphs}


if __name__ == "__main__":
    from paxminer_db import connect_from_credentials_ini

    db = sys.argv[1]
    key = sys.argv[2]
    region = sys.argv[3]
    firstf = sys.argv[4]
    mydb = connect_from_credentials_ini(db)
    try:
        run_q_charter(mydb, key, db, region, firstf, plot_dir="../plots")
    finally:
        mydb.close()
