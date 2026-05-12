import hashlib
import logging
import os
from datetime import date, timedelta

import polars as pl
from common.encryption import decrypt_field
from slack_sdk.errors import SlackApiError
from sqlalchemy import MetaData, Table, and_, case, func, literal_column, or_, select, text, union_all
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import SQLAlchemyError

from .utils import home_region_date_tiers, mysql_connection, paxminer_schema_name, slack_client, weaselbot_schema_name


def _target_schema_prefix() -> str:
    return (os.environ.get("WEASELBOT_DOWNRANGE_HOME_SCHEMA_PREFIX") or "f3ttown_").strip()


def _lookback_days() -> int:
    raw = (os.environ.get("WEASELBOT_DOWNRANGE_LOOKBACK_DAYS") or "14").strip()
    try:
        return max(int(raw), 1)
    except ValueError:
        return 14


def _report_limit() -> int:
    raw = (os.environ.get("WEASELBOT_DOWNRANGE_REPORT_LIMIT") or "50").strip()
    try:
        return max(int(raw), 1)
    except ValueError:
        return 50


def _sync_enabled() -> bool:
    raw = (os.environ.get("WEASELBOT_DOWNRANGE_SYNC_ENABLED") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _chunks(lines: list[str], size: int = 20) -> list[str]:
    if not lines:
        return []
    return ["\n".join(lines[i : i + size]) for i in range(0, len(lines), size)]


def _source_event_id(source_region: str, source_ts: str, source_ao_id: str, bd_date: date) -> str:
    return f"{source_region}:{source_ts}:{source_ao_id}:{bd_date.isoformat()}"


def _downrange_ao_id(source_region: str, source_ao_id: str) -> str:
    digest = hashlib.sha1(f"{source_region}:{source_ao_id}".encode("utf-8")).hexdigest()[:16]
    return f"dr_{digest}"


def _downrange_q_user_id(source_event_id: str) -> str:
    digest = hashlib.sha1(source_event_id.encode("utf-8")).hexdigest()[:20]
    return f"drq_{digest}"


def _source_timestamp(source_ts: str, source_region: str) -> str:
    ts = (source_ts or "").strip()
    if len(ts) <= 45:
        return ts
    digest = hashlib.sha1(f"{source_region}:{ts}".encode("utf-8")).hexdigest()[:36]
    return f"drts_{digest}"[:45]


def home_region_sub_query(u: Table, a: Table, b: Table, ao: Table, date_range: int):
    return (
        select(u.c.email, func.count(a.c.user_id).label("attendance"))
        .select_from(
            u.join(a, a.c.user_id == u.c.user_id)
            .join(b, and_(a.c.q_user_id == b.c.q_user_id, a.c.ao_id == b.c.ao_id, a.c.date == b.c.bd_date))
            .join(ao, b.c.ao_id == ao.c.channel_id)
        )
        .where(func.datediff(func.curdate(), b.c.bd_date) < date_range)
        .group_by(u.c.email)
        .subquery()
    )


def build_home_regions(schemas: pl.DataFrame, metadata: MetaData, engine):
    queries = []
    for row in schemas.iter_rows():
        schema = row[0]
        try:
            u = Table("users", metadata, autoload_with=engine, schema=schema)
            a = Table("bd_attendance", metadata, autoload_with=engine, schema=schema)
            b = Table("beatdowns", metadata, autoload_with=engine, schema=schema)
            ao = Table("aos", metadata, autoload_with=engine, schema=schema)

            d1, d2, d3, d4 = home_region_date_tiers()
            s1, s2, s3, s4 = (home_region_sub_query(u, a, b, ao, dr) for dr in (d1, d2, d3, d4))

            sql = (
                select(
                    literal_column(f"'{schema}'").label("region"),
                    u.c.email,
                    u.c.user_id,
                    case(
                        (s1.c.attendance.is_not(None), s1.c.attendance),
                        (s2.c.attendance.is_not(None), s2.c.attendance),
                        (s3.c.attendance.is_not(None), s3.c.attendance),
                        (s4.c.attendance.is_not(None), s4.c.attendance),
                        else_=func.count(a.c.user_id),
                    ).label("attendance"),
                )
                .select_from(
                    u.join(a, a.c.user_id == u.c.user_id)
                    .join(b, and_(a.c.q_user_id == b.c.q_user_id, a.c.ao_id == b.c.ao_id, a.c.date == b.c.bd_date))
                    .join(ao, b.c.ao_id == ao.c.channel_id)
                    .outerjoin(s1, u.c.email == s1.c.email)
                    .outerjoin(s2, u.c.email == s2.c.email)
                    .outerjoin(s3, u.c.email == s3.c.email)
                    .outerjoin(s4, u.c.email == s4.c.email)
                )
                .where(func.year(b.c.bd_date) == func.year(func.curdate()))
                .group_by(u.c.email, u.c.user_id)
            )
            queries.append(sql)
        except SQLAlchemyError as exc:
            logging.error("Schema %s error building home regions: %s", schema, exc)
        except Exception as exc:
            logging.error("Unexpected schema error %s while building home regions: %s", schema, exc)

    return union_all(*queries)


def nation_posts_sql(schemas: pl.DataFrame, engine, metadata: MetaData):
    queries = []
    for row in schemas.iter_rows():
        schema = row[0]
        try:
            u = Table("users", metadata, autoload_with=engine, schema=schema)
            a = Table("bd_attendance", metadata, autoload_with=engine, schema=schema)
            b = Table("beatdowns", metadata, autoload_with=engine, schema=schema)
            ao = Table("aos", metadata, autoload_with=engine, schema=schema)

            sql = (
                select(
                    literal_column(f"'{schema}'").label("source_region"),
                    u.c.email,
                    a.c.ao_id.label("source_ao_id"),
                    ao.c.ao.label("source_ao"),
                    b.c.bd_date.label("date"),
                    case((or_(a.c.user_id == b.c.q_user_id, a.c.user_id == b.c.coq_user_id), 1), else_=0).label("q_flag"),
                    b.c.timestamp.label("source_ts"),
                )
                .select_from(
                    u.join(a, a.c.user_id == u.c.user_id)
                    .join(
                        b,
                        and_(
                            or_(a.c.q_user_id == b.c.q_user_id, a.c.q_user_id == b.c.coq_user_id),
                            a.c.ao_id == b.c.ao_id,
                            a.c.date == b.c.bd_date,
                        ),
                    )
                    .join(ao, b.c.ao_id == ao.c.channel_id)
                )
                .where(
                    b.c.bd_date <= func.curdate(),
                    u.c.email != "none",
                    u.c.user_name != "PAXminer",
                    b.c.q_user_id.is_not(None),
                )
            )
            queries.append(sql)
        except SQLAlchemyError as exc:
            logging.error("Schema %s error building nation posts: %s", schema, exc)
        except Exception as exc:
            logging.error("Unexpected schema error %s while building nation posts: %s", schema, exc)

    return union_all(*queries)


def _build_report_messages(schema: str, df: pl.DataFrame) -> list[str]:
    if df.is_empty():
        return []

    lines = [
        f"*Downrange report for {schema}*",
        "T-town home-region PAX posting outside T-town:",
    ]
    for row in df.sort("date", descending=True).head(_report_limit()).iter_rows(named=True):
        lines.append(
            f"- <@{row['home_user_id']}> ({row['email']}) | src={row['source_region']} | AO={row['source_ao']} "
            f"| date={row['date']} | Q={row['q_flag']}"
        )
    if df.height > _report_limit():
        lines.append(f"...and {df.height - _report_limit()} more rows")

    return _chunks(lines, size=25)


def _send_report(engine, schema: str, df: pl.DataFrame) -> None:
    wb = weaselbot_schema_name()
    md = MetaData()
    aos = Table("aos", md, autoload_with=engine, schema=schema)

    with engine.begin() as conn:
        slack_token = conn.execute(
            text(f"SELECT slack_token FROM `{wb}`.regions WHERE paxminer_schema = :schema LIMIT 1"),
            {"schema": schema},
        ).scalar()

    if not slack_token:
        logging.info("No weaselbot token configured for schema=%s; skipping report send", schema)
        return

    with engine.begin() as conn:
        channel_id = conn.execute(select(aos.c.channel_id).where(aos.c.ao == "paxminer_logs")).scalar()
    if not channel_id:
        logging.info("No paxminer_logs AO found for schema=%s; skipping report send", schema)
        return

    client = slack_client(decrypt_field(slack_token))
    for message in _build_report_messages(schema, df):
        try:
            client.chat_postMessage(channel=channel_id, text=message, link_names=True)
        except SlackApiError:
            logging.exception("Failed sending downrange report to schema=%s channel=%s", schema, channel_id)


def _sync_schema_rows(engine, schema: str, rows: pl.DataFrame) -> tuple[int, int]:
    if rows.is_empty():
        return (0, 0)

    metadata = MetaData()
    beatdowns = Table("beatdowns", metadata, autoload_with=engine, schema=schema)
    attendance = Table("bd_attendance", metadata, autoload_with=engine, schema=schema)
    aos = Table("aos", metadata, autoload_with=engine, schema=schema)

    beatdowns_inserted = 0
    attendance_inserted = 0

    for event in (
        rows.group_by(["source_region", "source_ts", "source_ao_id", "source_ao", "date"])
        .agg(pl.col("home_user_id"))
        .iter_rows(named=True)
    ):
        source_event_id = _source_event_id(
            source_region=event["source_region"],
            source_ts=event["source_ts"] or "",
            source_ao_id=event["source_ao_id"],
            bd_date=event["date"],
        )
        ao_id = _downrange_ao_id(event["source_region"], event["source_ao_id"])
        q_user_id = _downrange_q_user_id(source_event_id)
        source_ts = _source_timestamp(event["source_ts"] or "", event["source_region"])

        with engine.begin() as conn:
            conn.execute(
                insert(aos)
                .values(
                    channel_id=ao_id,
                    ao=f"DR {event['source_region']}:{(event['source_ao'] or event['source_ao_id'])}"[:45],
                    channel_created=0,
                    archived=0,
                    backblast=1,
                )
                .prefix_with("IGNORE")
            )

            existing_bd = conn.execute(
                select(beatdowns.c.ao_id).where(
                    beatdowns.c.ao_id == ao_id,
                    beatdowns.c.bd_date == event["date"],
                    beatdowns.c.q_user_id == q_user_id,
                )
            ).first()

            if not existing_bd:
                conn.execute(
                    insert(beatdowns).values(
                        timestamp=source_ts,
                        ts_edited=None,
                        ao_id=ao_id,
                        bd_date=event["date"],
                        q_user_id=q_user_id,
                        coq_user_id=None,
                        pax_count=len(event["home_user_id"]),
                        backblast=(
                            "Downrange import: "
                            f"{event['source_region']} | {event['source_ao']} | {event['date']}"
                        ),
                        backblast_parsed=(
                            "Downrange import\n"
                            f"Source: {event['source_region']}\n"
                            f"AO: {event['source_ao']}\n"
                            f"Date: {event['date']}"
                        ),
                        fngs="None listed",
                        fng_count=0,
                        json={
                            "source_region": event["source_region"],
                            "source_ao_id": event["source_ao_id"],
                            "source_ao": event["source_ao"],
                            "source_ts": event["source_ts"],
                            "source_event_id": source_event_id,
                            "downrange_import": True,
                        },
                    )
                )
                beatdowns_inserted += 1

            existing_attendance = {
                row[0]
                for row in conn.execute(
                    select(attendance.c.user_id).where(
                        attendance.c.ao_id == ao_id,
                        attendance.c.date == event["date"],
                        attendance.c.q_user_id == q_user_id,
                    )
                ).fetchall()
            }

            new_rows = [
                {
                    "timestamp": source_ts,
                    "ts_edited": None,
                    "user_id": user_id,
                    "ao_id": ao_id,
                    "date": event["date"],
                    "q_user_id": q_user_id,
                    "json": {
                        "source_region": event["source_region"],
                        "source_ao_id": event["source_ao_id"],
                        "source_ts": event["source_ts"],
                        "source_event_id": source_event_id,
                        "downrange_import": True,
                    },
                }
                for user_id in set(event["home_user_id"])
                if user_id and user_id not in existing_attendance
            ]

            if new_rows:
                conn.execute(insert(attendance), new_rows)
                attendance_inserted += len(new_rows)

    return (beatdowns_inserted, attendance_inserted)


def main() -> None:
    logging.basicConfig(format="%(asctime)s [%(levelname)s]:%(message)s", level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")

    engine = mysql_connection()
    metadata = MetaData()
    pm = paxminer_schema_name()
    target_prefix = _target_schema_prefix()
    lookback_cutoff = date.today() - timedelta(days=_lookback_days())

    schemas = pl.read_database(
        f"SELECT schema_name FROM `{pm}`.regions WHERE schema_name LIKE 'f3%'",
        connection=engine,
    )

    if schemas.is_empty():
        logging.info("No regional schemas found in paxminer.regions")
        engine.dispose()
        return

    home_regions_sql = str(build_home_regions(schemas, metadata, engine).compile(engine, compile_kwargs={"literal_binds": True}))
    posts_sql = str(nation_posts_sql(schemas, engine, metadata).compile(engine, compile_kwargs={"literal_binds": True}))

    home_regions = pl.read_database(home_regions_sql, connection=engine)
    posts_df = pl.read_database(posts_sql, connection=engine)

    if home_regions.is_empty() or posts_df.is_empty():
        logging.info("No home region or posts data available for downrange sync")
        engine.dispose()
        return

    home_regions = (
        home_regions.group_by("email")
        .agg(pl.all().sort_by("attendance").last())
        .rename({"region": "home_region", "user_id": "home_user_id"})
    )

    downrange = (
        posts_df.join(home_regions.select(["email", "home_region", "home_user_id"]), on="email", how="inner")
        .filter(pl.col("home_region").str.starts_with(target_prefix))
        .filter(~pl.col("source_region").str.starts_with(target_prefix))
        .filter(pl.col("date") >= lookback_cutoff)
    )

    if downrange.is_empty():
        logging.info("No downrange rows for home prefix=%s in lookback window", target_prefix)
        engine.dispose()
        return

    for row in (
        downrange.select("home_region")
        .unique()
        .sort("home_region")
        .iter_rows(named=True)
    ):
        schema = row["home_region"]
        schema_df = downrange.filter(pl.col("home_region") == schema)
        _send_report(engine, schema, schema_df)

        if _sync_enabled():
            bd_count, att_count = _sync_schema_rows(engine, schema, schema_df)
            logging.info(
                "downrange sync schema=%s beatdowns_inserted=%s attendance_inserted=%s",
                schema,
                bd_count,
                att_count,
            )

    engine.dispose()


if __name__ == "__main__":
    main()
