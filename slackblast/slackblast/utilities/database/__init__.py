import logging
import os
import ssl
from dataclasses import dataclass
from typing import List, TypeVar
from urllib.parse import quote_plus

from sqlalchemy import and_, create_engine, pool
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from utilities import constants
from utilities.database.orm import BaseClass

_LOG = logging.getLogger(__name__)


@dataclass
class DatabaseField:
    name: str
    value: object = None


GLOBAL_ENGINE = None
GLOBAL_SESSION = None
GLOBAL_SCHEMA = None


def _database_tls_enabled() -> bool:
    v = os.environ.get(constants.DATABASE_TLS_ENABLED, "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return True


def get_engine(echo=False, schema=None) -> Engine:
    host = os.environ[constants.DATABASE_HOST]
    user = quote_plus(os.environ[constants.ADMIN_DATABASE_USER])
    passwd = quote_plus(os.environ[constants.ADMIN_DATABASE_PASSWORD])
    database = schema or os.environ[constants.ADMIN_DATABASE_SCHEMA]
    port = os.environ.get(constants.DATABASE_PORT, "3306")
    db_url = f"mysql+pymysql://{user}:{passwd}@{host}:{port}/{database}?charset=utf8mb4"
    connect_args = {}
    if _database_tls_enabled():
        connect_args["ssl"] = ssl.create_default_context()
    return create_engine(
        db_url,
        echo=echo,
        poolclass=pool.QueuePool,
        pool_size=1,
        max_overflow=1,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args=connect_args,
    )


def get_session(echo=False, schema=None):
    if GLOBAL_SESSION:
        return GLOBAL_SESSION

    global GLOBAL_ENGINE, GLOBAL_SCHEMA
    if schema != GLOBAL_SCHEMA or not GLOBAL_ENGINE:
        GLOBAL_ENGINE = get_engine(echo=echo, schema=schema)
        GLOBAL_SCHEMA = schema or os.environ[constants.ADMIN_DATABASE_SCHEMA]
        _LOG.info("Created new SQLAlchemy engine for schema=%s", GLOBAL_SCHEMA)
    return sessionmaker()(bind=GLOBAL_ENGINE)


def close_session(session):
    global GLOBAL_SESSION, GLOBAL_ENGINE
    if GLOBAL_SESSION == session:
        if GLOBAL_ENGINE:
            GLOBAL_ENGINE.dispose()
            GLOBAL_SESSION = None


def paxminer_schema_name() -> str:
    return os.environ.get(constants.PAXMINER_SCHEMA, "paxminer")


T = TypeVar("T")


class DbManager:
    def get_record(cls: T, id, schema=None) -> T:
        session = get_session(schema=schema)
        try:
            x = session.query(cls).filter(cls.get_id() == id).first()
            if x:
                session.expunge(x)
            return x
        finally:
            session.rollback()
            close_session(session)

    def find_records(cls: T, filters, schema=None) -> List[T]:
        session = get_session(schema=schema)
        try:
            records = session.query(cls).filter(and_(*filters)).all()
            for r in records:
                session.expunge(r)
            return records
        finally:
            session.rollback()
            close_session(session)

    def update_record(cls: T, id, fields, schema=None):
        session = get_session(schema=schema)
        try:
            session.query(cls).filter(cls.get_id() == id).update(fields, synchronize_session="fetch")
            session.flush()
        finally:
            session.commit()
            close_session(session)

    def update_records(cls: T, filters, fields, schema=None):
        session = get_session(schema=schema)
        try:
            session.query(cls).filter(and_(*filters)).update(fields, synchronize_session="fetch")
            session.flush()
        finally:
            session.commit()
            close_session(session)

    def create_record(record: BaseClass, schema=None) -> BaseClass:
        session = get_session(schema=schema)
        try:
            session.add(record)
            session.flush()
            session.expunge(record)
            session.commit()
            return record
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)

    def create_records(records: List[BaseClass], schema=None):
        session = get_session(schema=schema)
        try:
            session.add_all(records)
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            close_session(session)

    def delete_record(cls: T, id, schema=None):
        session = get_session(schema=schema)
        try:
            session.query(cls).filter(cls.get_id() == id).delete()
            session.flush()
        finally:
            session.commit()
            close_session(session)

    def delete_records(cls: T, filters, schema=None):
        session = get_session(schema=schema)
        try:
            session.query(cls).filter(and_(*filters)).delete()
            session.flush()
        finally:
            session.commit()
            close_session(session)

    def execute_sql_query(sql_query, schema=None):
        session = get_session(schema=schema)
        try:
            records = session.execute(sql_query)
            return records
        finally:
            close_session(session)
