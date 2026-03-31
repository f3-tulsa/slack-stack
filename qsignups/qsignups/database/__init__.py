import logging
import os
import ssl
from dataclasses import dataclass
from typing import List, TypeVar
from urllib.parse import quote_plus

from sqlalchemy import and_, create_engine, pool
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from database.orm import BaseClass


@dataclass
class DatabaseField:
    name: str
    value: object = None


DATABASE_HOST = "DATABASE_HOST"
ADMIN_DATABASE_USER = "ADMIN_DATABASE_USER"
ADMIN_DATABASE_PASSWORD = "ADMIN_DATABASE_PASSWORD"
ADMIN_DATABASE_SCHEMA = "ADMIN_DATABASE_SCHEMA"
DATABASE_PORT = "DATABASE_PORT"
DATABASE_TLS_ENABLED = "DATABASE_TLS_ENABLED"

GLOBAL_ENGINE = None
GLOBAL_SESSION = None

_LOG = logging.getLogger(__name__)


def _database_tls_enabled() -> bool:
    v = os.environ.get(DATABASE_TLS_ENABLED, "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    # Default TLS on (TiDB Cloud); local MySQL: set DATABASE_TLS_ENABLED=false
    return True


def get_engine(echo=False) -> Engine:
    """SQLAlchemy engine for the qsignups database schema (shared with OAuth stores)."""
    global GLOBAL_ENGINE
    if not GLOBAL_ENGINE:
        host = os.environ[DATABASE_HOST]
        user = quote_plus(os.environ[ADMIN_DATABASE_USER])
        passwd = quote_plus(os.environ[ADMIN_DATABASE_PASSWORD])
        database = os.environ[ADMIN_DATABASE_SCHEMA]
        port = os.environ.get(DATABASE_PORT, "3306")

        db_url = f"mysql+pymysql://{user}:{passwd}@{host}:{port}/{database}?charset=utf8mb4"
        connect_args = {}
        if _database_tls_enabled():
            connect_args["ssl"] = ssl.create_default_context()
        _LOG.info("Creating SQLAlchemy engine host=%s port=%s database=%s", host, port, database)
        GLOBAL_ENGINE = create_engine(
            db_url,
            echo=echo,
            poolclass=pool.QueuePool,
            pool_size=1,
            max_overflow=1,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args=connect_args,
        )
    return GLOBAL_ENGINE


def get_session(echo=False):
    if GLOBAL_SESSION:
        return GLOBAL_SESSION

    return sessionmaker()(bind=get_engine(echo=echo))


def close_session(session):
    global GLOBAL_SESSION, GLOBAL_ENGINE
    if GLOBAL_SESSION == session:
        if GLOBAL_ENGINE:
            GLOBAL_ENGINE.dispose()
            GLOBAL_SESSION = None


T = TypeVar("T")


class DbManager:
    def get_record(cls: T, id) -> T:
        session = get_session()
        try:
            x = session.query(cls).filter(cls.get_id() == id).first()
            if x:
                session.expunge(x)
            return x
        finally:
            session.rollback()
            close_session(session)

    def find_records(cls: T, filters) -> List[T]:
        session = get_session()
        try:
            records = session.query(cls).filter(and_(*filters)).all()
            for r in records:
                session.expunge(r)
            return records
        finally:
            session.rollback()
            close_session(session)

    def update_record(cls: T, id, fields):
        session = get_session()
        try:
            session.query(cls).filter(cls.get_id() == id).update(fields, synchronize_session="fetch")
            session.flush()
        finally:
            session.commit()
            close_session(session)

    def update_records(cls: T, filters, fields):
        session = get_session()
        try:
            session.query(cls).filter(and_(*filters)).update(fields, synchronize_session="fetch")
            session.flush()
        finally:
            session.commit()
            close_session(session)

    def create_record(record: BaseClass) -> BaseClass:
        session = get_session()
        try:
            session.add(record)
            session.flush()
            session.expunge(record)
            return record
        finally:
            session.commit()
            close_session(session)

    def create_records(records: List[BaseClass]):
        session = get_session()
        try:
            session.add_all(records)
            session.flush()
        finally:
            session.commit()
            close_session(session)

    def delete_record(cls: T, id):
        session = get_session()
        try:
            session.query(cls).filter(cls.get_id() == id).delete()
            session.flush()
        finally:
            session.commit()
            close_session(session)

    def delete_records(cls: T, filters):
        session = get_session()
        try:
            session.query(cls).filter(and_(*filters)).delete()
            session.flush()
        finally:
            session.commit()
            close_session(session)
