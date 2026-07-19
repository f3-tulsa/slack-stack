#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script queries the AWS F3(region) database for attendance records. It then generates leaderboard bar graphs
for each region across all AOs for the current month and YTD on total attendance.
The graph then is sent it to the 1st F channel in a Slack message.
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
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

_PAX_ROOT = Path(__file__).resolve().parent.parent
if str(_PAX_ROOT) not in sys.path:
    sys.path.insert(0, str(_PAX_ROOT))


def _leaderboard_period():
    off = int(os.environ.get("CHART_PERIOD_OFFSET_DAYS", "7"))
    d = datetime.datetime.now() - datetime.timedelta(days=off)
    return d.strftime("%m"), d.strftime("%b"), d.strftime("%B"), d.strftime("%Y")


def run_region_leaderboard(
    mydb,
    slack_token: str,
    schema: str,
    region: str,
    firstf: str,
    plot_dir: str | Path = "/tmp/paxminer_plots",
    destinations: list[str] | None = None,
) -> dict:
    plot_base = Path(plot_dir) / schema
    plot_base.mkdir(parents=True, exist_ok=True)

    slack = WebClient(token=slack_token)
    rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=5)
    slack.retry_handlers.append(rate_limit_handler)

    thismonth, thismonthname, thismonthnamelong, yearnum = _leaderboard_period()
    total_graphs = 0
    channels = list(destinations) if destinations else ([firstf] if firstf else [])

    try:
        with mydb.cursor() as cursor:
            sql = """
        select PAX, count(distinct AO) as UniqueAOs, count(1) as Posts FROM (
            select
                `bd`.`date` AS `Date`,
                `ao`.`ao` AS `AO`,
                `u`.`user_name` AS `PAX`
            from
                (((`bd_attendance` `bd`
            left join `aos` `ao` on
                ((`bd`.`ao_id` = `ao`.`channel_id`)))
            left join `users` `u` on
                ((`bd`.`user_id` = `u`.`user_id`))))
            where `u`.app != 1
            order by
                `bd`.`date` desc,
                `ao`.`ao`
        ) a
        where MONTH(Date) = %s
        AND YEAR(Date) = %s
        group by PAX
        order by count(1) desc
        limit 20
        """
            val = (thismonth, yearnum)
            cursor.execute(sql, val)
            posts = cursor.fetchall()
            posts_df = pd.DataFrame(posts, columns=["PAX", "UniqueAOs", "Posts"])
    finally:
        pass

    if not posts_df.empty:
        posts_df.plot.bar(x="PAX", color={"UniqueAOs": "blue", "Posts": "orange"})
        plt.title("Monthly Leaderboard - " + thismonthnamelong + ", " + yearnum)
        plt.xlabel("")
        plt.ylabel("# Posts for " + thismonthname + ", " + yearnum)
        out_m = plot_base / f"PAX_Leaderboard_{region}{thismonthname}{yearnum}.jpg"
        plt.savefig(str(out_m), bbox_inches="tight")
        comment = (
            "Hey "
            + region
            + "! Check out the current posting leaderboards for "
            + thismonthnamelong
            + ", "
            + yearnum
            + " as well as for Year to Date (includes all beatdowns, rucks, Qsource, etc.). "
            "Here are the top 20 posters! T-CLAPS to these HIMs."
        )
        for ch in channels:
            try:
                slack.files_upload_v2(channel=ch, initial_comment=comment, file=str(out_m))
                total_graphs += 1
            except Exception:
                logging.exception("Region leaderboard upload failed channel=%s", ch)
        plt.close("all")

    try:
        with mydb.cursor() as cursor:
            sql = """
        select PAX, count(distinct AO) as UniqueAOs, count(1) as Posts FROM (
            select
                `bd`.`date` AS `Date`,
                `ao`.`ao` AS `AO`,
                `u`.`user_name` AS `PAX`
            from
                (((`bd_attendance` `bd`
            left join `aos` `ao` on
                ((`bd`.`ao_id` = `ao`.`channel_id`)))
            left join `users` `u` on
                ((`bd`.`user_id` = `u`.`user_id`))))
            where `u`.app != 1
            order by
                `bd`.`date` desc,
                `ao`.`ao`
        ) a
        where YEAR(Date) = %s
        group by PAX
        order by count(1) desc
        limit 20
        """
            val = (yearnum,)
            cursor.execute(sql, val)
            posts = cursor.fetchall()
            posts_df = pd.DataFrame(posts, columns=["PAX", "UniqueAOs", "Posts"])
    finally:
        pass

    if not posts_df.empty:
        posts_df.plot.bar(x="PAX", color={"UniqueAOs": "purple", "Posts": "green"})
        plt.title("Year to Date Leaderboard - " + yearnum)
        plt.xlabel("")
        plt.ylabel("# Posts for " + yearnum + " - Year To Date")
        out_y = plot_base / f"PAX_Leaderboard_YTD_{region}{yearnum}.jpg"
        plt.savefig(str(out_y), bbox_inches="tight")
        for ch in channels:
            try:
                slack.files_upload_v2(file=str(out_y), channel=ch)
                total_graphs += 1
            except Exception:
                logging.exception("Region leaderboard YTD upload failed channel=%s", ch)
        plt.close("all")

    return {"schema": schema, "graphs": total_graphs}


if __name__ == "__main__":
    from paxminer_db import connect_from_credentials_ini

    db = sys.argv[1]
    key = sys.argv[2]
    region = sys.argv[3]
    firstf = sys.argv[4]
    mydb = connect_from_credentials_ini(db)
    try:
        run_region_leaderboard(mydb, key, db, region, firstf, plot_dir="../plots")
    finally:
        mydb.close()
