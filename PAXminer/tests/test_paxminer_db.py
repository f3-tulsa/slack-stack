"""Regression tests for paxminer_db.read_sql_df (pandas 3 + DictCursor)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pandas as pd
import pytest

from achievements import attendance as att
from paxminer_db import read_sql_df


class _FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description

    def execute(self, sql, params=None):
        self._sql = sql
        self._params = params

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    """Mimics pymysql connection: default DictCursor, but cursor(Cursor) returns tuples."""

    def __init__(self, dict_rows: list[dict]):
        self._dict_rows = dict_rows
        self._cols = list(dict_rows[0].keys()) if dict_rows else ["email", "date"]

    def cursor(self, cursorclass=None):
        # DictCursor path would return list[dict]; tuple Cursor returns list[tuple]
        is_dict = cursorclass is None or getattr(cursorclass, "__name__", "") == "DictCursor"
        if is_dict:
            rows = self._dict_rows
        else:
            rows = [tuple(r[c] for c in self._cols) for r in self._dict_rows]
        desc = [(c,) for c in self._cols]
        return _FakeCursor(rows, desc)


def test_read_sql_df_returns_values_not_column_names():
    """DictCursor + pandas 3 would fill every cell with the column name; we must not."""
    conn = _FakeConn(
        [
            {"email": "a@x.com", "user_id": "U1", "date": "2026-07-01"},
            {"email": "b@x.com", "user_id": "U2", "date": "2026-07-02"},
        ]
    )
    df = read_sql_df(conn, "SELECT 1")
    assert list(df.columns) == ["email", "user_id", "date"]
    assert df.iloc[0]["email"] == "a@x.com"
    assert df.iloc[0]["date"] == "2026-07-01"
    assert not (df["date"] == "date").any()


def test_read_sql_df_empty_keeps_columns():
    conn = _FakeConn([])
    # Empty dict list has fallback columns from FakeConn
    df = read_sql_df(conn, "SELECT 1")
    assert list(df.columns) == ["email", "date"]
    assert len(df) == 0


def test_no_module_calls_pd_read_sql_directly():
    """Guard: production modules must use read_sql_df, not pd.read_sql."""
    root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    skip = {"tests", ".venv", "__pycache__"}
    for path in root.rglob("*.py"):
        if any(part in skip for part in path.parts):
            continue
        if path.name == "paxminer_db.py":
            continue  # docstring only
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # pd.read_sql(...) or pandas.read_sql(...)
            if isinstance(func, ast.Attribute) and func.attr == "read_sql":
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"pd.read_sql still used at: {offenders}"


def test_paxminer_db_no_toplevel_pandas_import():
    """Slack Lambda imports paxminer_db but has no pandas — keep import lazy."""
    path = Path(__file__).resolve().parent.parent / "paxminer_db.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "pandas", "top-level import pandas breaks Slack Lambda"
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("pandas"):
            raise AssertionError("top-level from pandas import breaks Slack Lambda")


def test_attendance_regexes_compile():
    from achievements.activity import QSOURCE_AO_RE, QSOURCE_BB_RE, RUCK_AO_RE

    assert QSOURCE_BB_RE.search("Q1.1 study")
    assert QSOURCE_AO_RE.search("ao-qsource-east")
    assert RUCK_AO_RE.search("ruck-ao")


def test_qsource_and_beatdown_masks():
    from achievements.activity import classify_activity_type

    assert classify_activity_type(backblast="QSource at The Goose", ao_name="the-goose") == "qsource"
    assert classify_activity_type(backblast="Q1.1 study group", ao_name="qsource") == "qsource"
    assert classify_activity_type(backblast="Backblast — The Goose", ao_name="ao-copa") == "beatdown"
    assert classify_activity_type(backblast="Ruck AO morning", ao_name="ruck-ao") == "rucking"
    assert (
        classify_activity_type(
            json_blob='{"Event Type": "Bootcamp"}',
            ao_name="ao-qsource",
            backblast="QSource",
        )
        == "Bootcamp"
    )
    assert classify_activity_type(existing="keep-me", ao_name="ruck-ao") == "keep-me"
