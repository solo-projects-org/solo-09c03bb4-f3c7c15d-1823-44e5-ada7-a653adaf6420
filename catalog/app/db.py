"""SQLite connection management and schema.

A new connection is opened per request (SQLite connections are cheap and this
keeps the synchronous request handlers thread-safe under uvicorn workers).
WAL + a busy timeout let concurrent writers wait instead of failing outright;
critical sections additionally use ``BEGIN IMMEDIATE`` to serialize writers.

Current-revision acceleration
------------------------------
``scenes`` keeps *every* revision (history must stay downloadable forever), so
querying it directly forces a ``GROUP BY scene_id / MAX(revision)`` scan over
the whole revision history on every ``GET /scenes`` call.

The :class:`current_scenes` table instead holds exactly one row per scene: its
current (highest) revision. It is maintained incrementally, inside the same
write transaction as the revision itself, so its size is O(number of scenes),
not O(number of revisions). When the SQLite build ships the R*Tree module,
``current_scenes_rtree`` mirrors the footprints and turns bbox queries into
indexed tree searches; a plain bbox index is used otherwise. Either way,
narrow-window queries never scan historical revisions.
"""
from __future__ import annotations

import sqlite3
import struct
from contextlib import contextmanager
from typing import Iterator

from .config import get_settings


def _as_float32(value: float) -> float:
    """Round a Python float to the nearest 32-bit float (ties to even).

    The R*Tree module stores coordinates at single precision, so this is the
    baseline value an inserted bbox corner comes back as.
    """
    return struct.unpack("f", struct.pack("f", float(value)))[0]


def _ulp32(value: float) -> float:
    """Magnitude of one unit in the last place at 32-bit precision near
    ``value`` (validated bboxes are finite and well inside the float32 range).
    """
    return abs(_step32(_as_float32(value), 1) - _as_float32(value))


def _step32(value: float, direction: int) -> float:
    """Return the 32-bit float one bin away from ``value``.

    ``direction`` < 0 moves toward -infinity, > 0 toward +infinity. Bboxes are
    finite validated numbers, so zero/NaN bit patterns are out of scope.
    """
    bits = struct.unpack("I", struct.pack("f", float(value)))[0]
    if direction < 0:  # toward more negative values (sign-magnitude layout)
        bits = bits + 1 if bits >= 0x80000000 else bits - 1
    else:
        bits = bits + 1 if bits < 0x80000000 else bits - 1
    return struct.unpack("f", struct.pack("I", bits & 0xFFFFFFFF))[0]

# Bump when a migration must run. An existing catalog starts at user_version 0.
SCHEMA_VERSION = 1

# Base, revision-history tables (unchanged on-disk shape, so existing catalogs
# open without any conversion) plus the current-revision accelerator.
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

-- One row per scene: the currently active revision. Rowid is the stable
-- surrogate referenced by the R*Tree shadow table; it never changes because
-- rows are only inserted once and then updated in place.
CREATE TABLE IF NOT EXISTS current_scenes (
    scene_id            TEXT NOT NULL PRIMARY KEY,
    revision            INTEGER NOT NULL,
    acquisition_time    TEXT NOT NULL,
    west                REAL NOT NULL,
    south               REAL NOT NULL,
    east                REAL NOT NULL,
    north               REAL NOT NULL,
    FOREIGN KEY (scene_id, revision) REFERENCES scenes(scene_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_current_acq_time
    ON current_scenes(acquisition_time);
"""

RTREE_NAME = "current_scenes_rtree"

_RTREE_DDL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {RTREE_NAME} USING rtree(
    id, minx, maxx, miny, maxy
)
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


def _rtree_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RTREE_NAME,),
    ).fetchone()
    return row is not None


def _create_rtree_if_supported(conn: sqlite3.Connection) -> bool:
    """Create the R*Tree shadow table when the module is available."""
    if _rtree_exists(conn):
        return True
    try:
        conn.execute(_RTREE_DDL)
    except sqlite3.DatabaseError:
        # R*Tree is a compile-time option (SQLITE_ENABLE_RTREE); fall back to
        # the plain current_scenes table, which still decouples query cost
        # from the number of historical revisions.
        return False
    return _rtree_exists(conn)


def rebuild_current_index(conn: sqlite3.Connection, use_rtree: bool) -> None:
    """Rebuild the current-revision accelerator from the revision history.

    Idempotent and crash-safe: every statement runs inside the caller's
    transaction, so an interrupted rebuild is simply repeated on next startup.
    """
    conn.execute("DELETE FROM current_scenes")
    # Re-derive the current revision of every scene from the source of truth.
    conn.execute(
        """
        INSERT INTO current_scenes
            (scene_id, revision, acquisition_time, west, south, east, north)
        SELECT s.scene_id, s.revision, s.acquisition_time,
               s.west, s.south, s.east, s.north
        FROM scenes s
        JOIN (
            SELECT scene_id, MAX(revision) AS revision
            FROM scenes
            GROUP BY scene_id
        ) latest
          ON latest.scene_id = s.scene_id AND latest.revision = s.revision
        """
    )
    if use_rtree:
        conn.execute(f"DELETE FROM {RTREE_NAME}")
        conn.execute(
            f"""
            INSERT INTO {RTREE_NAME} (id, minx, maxx, miny, maxy)
            SELECT rowid, west, east, south, north FROM current_scenes
            """
        )


def current_index_drift(conn: sqlite3.Connection, use_rtree: bool) -> int:
    """Count ways in which the accelerator disagrees with ``scenes``.

    A return value of 0 means: one index row per scene, pointing at exactly
    the highest revision, with identical metadata, and (when present) an
    R*Tree entry matching every footprint.
    """
    drift = 0

    # Scene ids present in history but missing from the accelerator.
    drift += conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT scene_id FROM scenes
            EXCEPT
            SELECT scene_id FROM current_scenes
        )
        """
    ).fetchone()[0]

    # Phantom accelerator rows whose scene no longer exists in history.
    drift += conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT scene_id FROM current_scenes
            EXCEPT
            SELECT DISTINCT scene_id FROM scenes
        )
        """
    ).fetchone()[0]

    # Duplicate rows can never occur (scene_id is PRIMARY KEY), but verify the
    # row counts agree as a belt-and-braces size check.
    drift += abs(
        conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM current_scenes)
                 - (SELECT COUNT(DISTINCT scene_id) FROM scenes)
            """
        ).fetchone()[0]
    )

    # Rows pointing at a non-current revision, or carrying stale metadata.
    drift += conn.execute(
        """
        SELECT COUNT(*) FROM current_scenes c
        WHERE c.revision != (
                SELECT MAX(revision) FROM scenes s WHERE s.scene_id = c.scene_id
              )
           OR EXISTS (
                SELECT 1 FROM scenes s
                WHERE s.scene_id = c.scene_id AND s.revision = c.revision
                  AND (s.acquisition_time != c.acquisition_time
                       OR s.west  != c.west  OR s.south != c.south
                       OR s.east  != c.east  OR s.north != c.north)
              )
        """
    ).fetchone()[0]

    if use_rtree:
        current = {
            r["rowid"]: (r["west"], r["east"], r["south"], r["north"])
            for r in conn.execute(
                "SELECT rowid, west, east, south, north FROM current_scenes"
            )
        }
        tree = {
            r["id"]: (r["minx"], r["maxx"], r["miny"], r["maxy"])
            for r in conn.execute(
                f"SELECT id, minx, maxx, miny, maxy FROM {RTREE_NAME}"
            )
        }
        # R*Tree stores coordinates at 32-bit precision using its own
        # conservative enclosure rounding (whose exact negative-number edge
        # behaviour is an implementation detail). We therefore verify the
        # stable contract instead of bit-exact values:
        #   * every current row has exactly one tree entry and vice versa;
        #   * the stored box encloses the true 64-bit box;
        #   * each corner is within a few float32 ULPs of the true value, so
        #     a stale footprint left from an older revision is still detected.
        for rowid, box in current.items():
            stored = tree.pop(rowid, None)
            if stored is None:
                drift += 1  # current row missing its rtree entry
                continue
            minx, maxx, miny, maxy = stored
            west, east, south, north = box
            enclosed = (
                minx <= west + _ulp32(west) and maxx >= east - _ulp32(east)
                and miny <= south + _ulp32(south) and maxy >= north - _ulp32(north)
            )
            tight = (
                abs(minx - west) <= 8 * _ulp32(west)
                and abs(maxx - east) <= 8 * _ulp32(east)
                and abs(miny - south) <= 8 * _ulp32(south)
                and abs(maxy - north) <= 8 * _ulp32(north)
            )
            if not (enclosed and tight):
                drift += 1  # footprint missing, stale or grossly misaligned
        drift += len(tree)  # dangling rtree entries with no current row

    return max(drift, 0)


def upsert_current_revision(
    conn: sqlite3.Connection,
    use_rtree: bool,
    *,
    scene_id: str,
    revision: int,
    acquisition_time: str,
    west: float,
    south: float,
    east: float,
    north: float,
    is_first_revision: bool,
) -> None:
    """Reflect a newly committed revision in the accelerator.

    Must run inside the submitter's write transaction, *after* the matching
    ``scenes`` row has been inserted, so commit/rollback apply atomically to
    history and accelerator together.
    """
    if is_first_revision:
        cursor = conn.execute(
            """
            INSERT INTO current_scenes
                (scene_id, revision, acquisition_time, west, south, east, north)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (scene_id, revision, acquisition_time, west, south, east, north),
        )
        rowid = cursor.lastrowid
    else:
        conn.execute(
            """
            UPDATE current_scenes
               SET revision = ?, acquisition_time = ?,
                   west = ?, south = ?, east = ?, north = ?
             WHERE scene_id = ?
            """,
            (revision, acquisition_time, west, south, east, north, scene_id),
        )
        rowid = conn.execute(
            "SELECT rowid FROM current_scenes WHERE scene_id = ?", (scene_id,)
        ).fetchone()[0]

    if use_rtree:
        # Single-entry replace; the rest of the tree is untouched, so other
        # scenes never pay for this scene's revision.
        conn.execute(f"DELETE FROM {RTREE_NAME} WHERE id = ?", (rowid,))
        conn.execute(
            f"INSERT INTO {RTREE_NAME} (id, minx, maxx, miny, maxy) "
            "VALUES (?, ?, ?, ?, ?)",
            (rowid, west, east, south, north),
        )


def init_db() -> bool:
    """Create or upgrade the schema. Returns whether R*Tree is active."""
    settings = get_settings()
    conn = _connect(settings.db_path)
    try:
        conn.executescript(SCHEMA)
        use_rtree = _create_rtree_if_supported(conn)

        conn.execute("BEGIN IMMEDIATE")
        try:
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            needs_rebuild = user_version < SCHEMA_VERSION
            if not needs_rebuild:
                # Defensive: a crash or a hand-edited database must never leave
                # the accelerator missing or wrong; repair instead of serving
                # stale locations.
                needs_rebuild = current_index_drift(conn, use_rtree) > 0
            if needs_rebuild:
                rebuild_current_index(conn, use_rtree)
            # A version written for an rtree-enabled build must still work on
            # a build without rtree: verify (and repair) against the active
            # shape every startup, which the drift check above already does.
            if current_index_drift(conn, use_rtree) != 0:
                raise RuntimeError(
                    "current-scene index could not be rebuilt consistently"
                )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return use_rtree
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
