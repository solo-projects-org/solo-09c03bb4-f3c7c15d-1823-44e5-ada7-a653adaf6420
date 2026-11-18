"""SQLite connection management and schema.

A new connection is opened per request (SQLite connections are cheap and this
keeps the synchronous request handlers thread-safe under uvicorn workers).
WAL + a busy timeout let concurrent writers wait instead of failing outright;
critical sections additionally use ``BEGIN IMMEDIATE`` to serialize writers.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import get_settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS scenes (
    scene_id            TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    acquisition_time    TEXT NOT NULL,   -- UTC ISO-8601
    west                REAL NOT NULL,
    south               REAL NOT NULL,
    east                REAL NOT NULL,
    north               REAL NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (scene_id, revision)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bands (
    scene_id        TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    band            TEXT NOT NULL CHECK (band IN ('B04', 'B08')),
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    stored_path     TEXT NOT NULL,      -- relative path inside storage dir
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (scene_id, revision, band),
    FOREIGN KEY (scene_id, revision) REFERENCES scenes(scene_id, revision)
) WITHOUT ROWID;
"""


def _connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None => autocommit at the driver level; every write
    # transaction is opened explicitly with BEGIN IMMEDIATE in the handlers,
    # which avoids any ambiguity with the driver's implicit transactions.
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db() -> None:
    """Create schema (idempotent, runs on every startup)."""
    settings = get_settings()
    conn = _connect(settings.db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a connection for the duration of one request."""
    settings = get_settings()
    conn = _connect(settings.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_db_alive() -> None:
    """Raise if the database cannot be reached (used by /health)."""
    settings = get_settings()
    conn = _connect(settings.db_path)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
