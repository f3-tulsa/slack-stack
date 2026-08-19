#!/usr/bin/env python3
'''
This script was written by Beaker from F3STL. Questions? @srschaecher on twitter or srschaecher@gmail.com.
This script queries the AWS F3(region) database for attendance records. It then generates leaderboard bar graphs
for each AO for the current month and YTD on total attendance.
The graph then is sent to each AO in a Slack message.
'''

from __future__ import annotations

import logging
import sys
import time
from datetime import date
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

from scheduling import (  # noqa: E402
    default_chart_window,
    format_window_label,
    is_calendar_month,
    window_file_tag,
)

_LOG = logging.getLogger(__name__)


def run_ao_leaderboard(
    mydb,
    slack_token: str,
    schema: str,
    region: str,
    firstf: str,
    plot_dir: str | Path = "/tmp/paxminer_plots",
    destinations: list[str] | None = None,
    post_per_ao: bool = True,
    window: tuple[date, date] | None = None,
    title: str | None = None,
    top_n: int = 20,
) -> dict:
    _ = region, firstf
    plot_base = Path(plot_dir) / schema
    plot_base.mkdir(parents=True, exist_ok=True)

    slack = WebClient(token=slack_token)
    rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=7)
    slack.retry_handlers.append(rate_limit_handler)

    start, end = window or default_chart_window()
    label = format_window_label(start, end)
    tag = window_file_tag(start, end)
    heading = title or f"Leaderboard - {label}"
    limit = max(1, int(top_n or 20))
    include_ytd = False
    total_graphs = 0
    fallback_channels = list(destinations) if destinations else []
    posted_channels: list[dict] = []
    failed_channels: list[dict] = []
    skipped_no_data: list[dict] = []

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
        # Per-AO posts go to the AO channel; otherwise fan-out to schedule destinations.
        upload_channels = [row["channel_id"]] if post_per_ao else list(fallback_channels)
        if not upload_channels:
            continue
        channel_id = upload_channels[0]
        try:
            with mydb.cursor() as cursor:
                sql = """
            select PAX, count(1) as Posts FROM (
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
            where
            Date BETWEEN %s AND %s
            AND ao= %s
            group by PAX
            order by count(1) desc
            limit %s
            """
                val = (start.isoformat(), end.isoformat(), ao, limit)
                cursor.execute(sql, val)
                posts = cursor.fetchall()
                posts_df = pd.DataFrame(posts, columns=["PAX", "Posts"])
        finally:
            pass

        if posts_df.empty:
            skipped_no_data.append({"ao": ao, "channel_id": channel_id})
        else:
            posts_df.plot.bar(x="PAX", color={"Posts": "orange"})
            plt.title(heading)
            plt.xlabel("")
            plt.ylabel("# Posts for " + label)
            out_m = plot_base / f"PAX_Leaderboard_{ao}{tag}.jpg"
            plt.savefig(str(out_m), bbox_inches="tight")
            comment = (
                "Hey "
                + ao
                + "! Here is "
                + heading
                + " with the top "
                + str(limit)
                + " posters! T-CLAPS to these HIMs."
            )
            for ch in upload_channels:
                max_attempts = 5
                for attempt in range(max_attempts):
                    try:
                        try:
                            slack.conversations_join(channel=ch)
                        except Exception:
                            pass
                        slack.files_upload_v2(
                            channel=ch,
                            initial_comment=comment,
                            file=str(out_m),
                        )
                        total_graphs += 1
                        if not any(p.get("channel_id") == ch and p.get("ao") == ao for p in posted_channels):
                            posted_channels.append({"ao": ao, "channel_id": ch})
                        break
                    except SlackApiError as e:
                        if e.response.status_code == 429:
                            delay = int(e.response.headers["Retry-After"])
                            _LOG.info(
                                "AO leaderboard: rate limited, retrying in %s seconds (ao=%s)",
                                delay,
                                ao,
                            )
                            time.sleep(delay)
                        else:
                            _LOG.exception("AO leaderboard upload failed ao=%s channel=%s", ao, ch)
                            failed_channels.append(
                                {"ao": ao, "channel_id": ch, "reason": str(e)[:200]}
                            )
                            break
                    except Exception as exc:
                        _LOG.exception("AO leaderboard upload failed ao=%s channel=%s", ao, ch)
                        failed_channels.append(
                            {"ao": ao, "channel_id": ch, "reason": str(exc)[:200]}
                        )
                        break
            plt.close("all")

        if include_ytd:
            ytd_start = date(end.year, 1, 1)
            ytd_end = end
            yearnum = str(end.year)
            try:
                with mydb.cursor() as cursor:
                    sql = """
                select PAX, count(1) as Posts FROM (
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
                where
                Date BETWEEN %s AND %s
                AND ao= %s
                group by PAX
                order by count(1) desc
                limit 20
                """
                    val = (ytd_start.isoformat(), ytd_end.isoformat(), ao)
                    cursor.execute(sql, val)
                    posts = cursor.fetchall()
                    posts_df = pd.DataFrame(posts, columns=["PAX", "Posts"])
            finally:
                pass

            if not posts_df.empty:
                posts_df.plot.bar(x="PAX", color={"Posts": "green"})
                plt.title("Year to Date Leaderboard - " + yearnum)
                plt.xlabel("")
                plt.ylabel("# Posts for " + yearnum + " - Year To Date")
                out_y = plot_base / f"PAX_Leaderboard_YTD_{ao}{yearnum}.jpg"
                plt.savefig(str(out_y), bbox_inches="tight")
                for ch in upload_channels:
                    max_attempts = 5
                    for attempt in range(max_attempts):
                        try:
                            try:
                                slack.conversations_join(channel=ch)
                            except Exception:
                                pass
                            slack.files_upload_v2(file=str(out_y), channel=ch)
                            total_graphs += 1
                            if not any(p.get("channel_id") == ch and p.get("ao") == ao for p in posted_channels):
                                posted_channels.append({"ao": ao, "channel_id": ch})
                            break
                        except SlackApiError as e:
                            if e.response.status_code == 429:
                                delay = int(e.response.headers["Retry-After"])
                                _LOG.info(
                                    "AO leaderboard YTD: rate limited, retrying in %s seconds (ao=%s)",
                                    delay,
                                    ao,
                                )
                                time.sleep(delay)
                            else:
                                _LOG.exception("AO leaderboard YTD upload failed ao=%s channel=%s", ao, ch)
                                if not any(f.get("channel_id") == ch and f.get("ao") == ao for f in failed_channels):
                                    failed_channels.append(
                                        {"ao": ao, "channel_id": ch, "reason": str(e)[:200]}
                                    )
                                break
                        except Exception as exc:
                            _LOG.exception("AO leaderboard YTD upload failed ao=%s channel=%s", ao, ch)
                            if not any(f.get("channel_id") == ch and f.get("ao") == ao for f in failed_channels):
                                failed_channels.append(
                                    {"ao": ao, "channel_id": ch, "reason": str(exc)[:200]}
                                )
                            break
                plt.close("all")

    return {
        "schema": schema,
        "graphs": total_graphs,
        "posted_channels": posted_channels,
        "failed_channels": failed_channels,
        "skipped_no_data": skipped_no_data,
        "channel_count": len(posted_channels),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }


if __name__ == "__main__":
    from paxminer_db import connect_from_credentials_ini

    db = sys.argv[1]
    key = sys.argv[2]
    region = sys.argv[3]
    firstf = sys.argv[4]
    mydb = connect_from_credentials_ini(db)
    try:
        run_ao_leaderboard(mydb, key, db, region, firstf, plot_dir="../plots")
    finally:
        mydb.close()
