"""Slack OAuth state store backed by SQLAlchemy.

Workaround for slack_sdk SQLAlchemyOAuthStateStore passing ``metadata`` twice to
``sqlalchemy.Table()`` (breaks table creation). See slack-sdk issue #1548.
"""

from __future__ import annotations

import sqlalchemy
from slack_sdk.oauth.state_store.sqlalchemy import SQLAlchemyOAuthStateStore
from sqlalchemy import Column, DateTime, Integer, MetaData, String


class FixedSQLAlchemyOAuthStateStore(SQLAlchemyOAuthStateStore):
    @classmethod
    def build_oauth_states_table(cls, metadata: MetaData, table_name: str):
        return sqlalchemy.Table(
            table_name,
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("state", String(200), nullable=False),
            Column("expire_at", DateTime, nullable=False),
        )
