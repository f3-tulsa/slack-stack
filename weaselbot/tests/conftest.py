"""Pytest hooks and shared fixtures for weaselbot tests."""

import os

# achievement_tables.py requires REGION_SCHEMA at import time for DDL tooling.
os.environ.setdefault("REGION_SCHEMA", "test_unit_paxminer_schema")
