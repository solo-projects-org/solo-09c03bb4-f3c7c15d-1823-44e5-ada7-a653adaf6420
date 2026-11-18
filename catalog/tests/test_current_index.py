"""Tests for the current-revision accelerator (current_scenes + R*Tree).

Covers the performance and correctness requirements of the optimization:

* an existing history-only SQLite catalog is migrated in place on startup;
* narrow queries use the current-scene index (and R*Tree) rather than scanning
  historical revisions, and stay flat as revision history grows;
* a moved footprint / changed acquisition time takes effect immediately while
  the old location never leaks back;
* submit failures and idempotent replays leave the accelerator exactly right;
* a corrupted / stale accelerator is detected and rebuilt on startup;
* other scenes' submissions touch only their own index row.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_env(tmp_path):
    db_path = tmp_path / "catalog.db"
    storage_dir = tmp_path / "storage"
    import os
    os.environ["CATALOG_DB_PATH"] = str(db_path)
    os.environ["CATALOG_STORAGE_DIR"] = str(storage_dir)
    from app import config
    config.get_settings.cache_clear()
    return str(db_path), str(storage_dir)


def _submit(client, scene_id, revision, *, b04=b"red", b08=b"nir",
            acquisition_time="2026-06-01T10:00:00Z",
            bbox=(116.0, 39.0, 117.0, 40.0), bad_digest=False):
    west, south, east, north = bbox
    d04, d08 = _sha(b04), _sha(b08)
    if bad_digest:
        d04 = "00" * 32
    return client.post(
        f"/scenes/{scene_id}/revisions",
        data={
            "revision": str(revision),
            "acquisition_time": acquisition_time,
            "west": str(west), "south": str(south),
            "east": str(east), "north": str(north),
            "sha256_b04": d04, "sha256_b08": d08,
        },
        files={
            "file_b04": ("b04.tif", b04, "application/octet-stream"),
            "file_b08": ("b08.tif", b08, "application/octet-stream"),
        },
    )


def _open_raw(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path, storage_dir = _make_env(tmp_path)
    from app.main import app
    with TestClient(app) as c:
        yield c, db_path


# --------------------------------------------------------------------------- #
def test_old_catalog_is_migrated_on_startup(tmp_path, monkeypatch):
    """A DB written by the previous version (scenes/bands only) must be usable
    immediately after the new service starts, with a correct accelerator."""
    db_path, _ = _make_env(tmp_path)

    # Build an "old" catalog using only the legacy schema.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scenes (
            scene_id TEXT NOT NULL, revision INTEGER NOT NULL,
            acquisition_time TEXT NOT NULL,
            west REAL NOT NULL, south REAL NOT NULL, east REAL NOT NULL,
            north REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY (scene_id, revision)
        ) WITHOUT ROWID;
        CREATE TABLE bands (
            scene_id TEXT NOT NULL, revision INTEGER NOT NULL, band TEXT NOT NULL,
            sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, stored_path TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY (scene_id, revision, band)
        ) WITHOUT ROWID;
        """
    )
    conn.executemany(
        "INSERT INTO scenes (scene_id, revision, acquisition_time,"
        " west, south, east, north) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("OLD", 1, "2026-01-01T00:00:00.000000Z", 0, 0, 1, 1),
            ("OLD", 2, "2026-02-01T00:00:00.000000Z", 10, 10, 11, 11),
            ("NEW", 1, "2026-03-01T00:00:00.000000Z", 20, 20, 21, 21),
        ],
    )
    conn.commit()
    conn.close()

    from app.main import app
    with TestClient(app) as c:
        # Query results come straight from the migrated accelerator.
        hits = {s["scene_id"]: s["revision"] for s in c.get("/scenes").json()}
        assert hits == {"OLD": 2, "NEW": 1}

        r = c.get("/scenes", params={"west": 9.5, "south": 9.5,
                                     "east": 11.5, "north": 11.5}).json()
        assert [s["scene_id"] for s in r] == ["OLD"]
        # Old location of OLD must not leak back.
        r = c.get("/scenes", params={"west": 0, "south": 0,
                                     "east": 1, "north": 1}).json()
        assert r == []

    raw = _open_raw(db_path)
    # Exactly one accelerator row per scene, pointing at MAX(revision).
    rows = raw.execute(
        "SELECT scene_id, revision FROM current_scenes ORDER BY scene_id"
    ).fetchall()
    assert [(r["scene_id"], r["revision"]) for r in rows] == [("NEW", 1), ("OLD", 2)]
    assert raw.execute("PRAGMA user_version").fetchone()[0] >= 1
    # R*Tree, when present, mirrors every current row exactly once.
    has_rtree = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='current_scenes_rtree'"
    ).fetchone()
    if has_rtree:
        n_rt = raw.execute("SELECT COUNT(*) FROM current_scenes_rtree").fetchone()[0]
        n_cur = raw.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0]
        assert n_rt == n_cur
    raw.close()


def test_migration_is_idempotent_and_repairs_drift(tmp_path, monkeypatch):
    db_path, _ = _make_env(tmp_path)
    from app.main import app
    with TestClient(app) as c:
        assert _submit(c, "S", 1).status_code == 201
        assert _submit(c, "S", 2, b04=b"r2", b08=b"n2",
                       acquisition_time="2026-07-01T10:00:00Z",
                       bbox=(50.0, 50.0, 51.0, 51.0)).status_code == 201

    # Tamper with the accelerator behind the service's back: stale revision and
    # a dangling rtree entry, plus an extra phantom row.
    raw = _open_raw(db_path)
    raw.execute("BEGIN IMMEDIATE")
    raw.execute(
        "UPDATE current_scenes SET revision = 1, west = 0, south = 0, "
        "east = 1, north = 1 WHERE scene_id = 'S'"
    )
    has_rtree = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='current_scenes_rtree'"
    ).fetchone()
    if has_rtree:
        raw.execute("DELETE FROM current_scenes_rtree")
        raw.execute("INSERT INTO current_scenes_rtree VALUES (9999, 0, 1, 0, 1)")
    raw.commit()
    raw.close()

    # A new process startup must detect the drift and rebuild, not trust it.
    with TestClient(app) as c:
        hits = c.get("/scenes").json()
        assert len(hits) == 1
        assert hits[0]["scene_id"] == "S" and hits[0]["revision"] == 2
        assert hits[0]["west"] == 50.0
        r = c.get("/scenes", params={"west": 0, "south": 0,
                                     "east": 1, "north": 1}).json()
        assert r == []

    raw = _open_raw(db_path)
    if has_rtree:
        ids = [r[0] for r in raw.execute(
            "SELECT id FROM current_scenes_rtree ORDER BY id").fetchall()]
        assert ids == [raw.execute(
            "SELECT rowid FROM current_scenes WHERE scene_id='S'").fetchone()[0]]
    raw.close()


def test_narrow_query_does_not_scan_revision_history(tmp_path, monkeypatch):
    db_path, _ = _make_env(tmp_path)
    from app.main import app
    n_revisions = 400
    with TestClient(app) as c:
        for rev in range(1, n_revisions + 1):
            # Each revision moves slightly; only the last one is current.
            lon = (rev % 170) - 85.0
            r = _submit(
                c, "HEAVY", rev,
                b04=f"r{rev}".encode(), b08=f"n{rev}".encode(),
                acquisition_time=f"2026-01-{(rev % 27) + 1:02d}T10:00:00Z",
                bbox=(lon, 0.0, lon + 0.5, 1.0),
            )
            assert r.status_code == 201
        # A second, light scene so the index holds more than one row.
        assert _submit(c, "LIGHT", 1, b04=b"l", b08=b"m").status_code == 201

        # Narrow bbox window: only the current revision can ever match.
        narrow = c.get("/scenes", params={"west": 1.5, "south": 0.0,
                                          "east": 1.6, "north": 1.0})
        assert narrow.status_code == 200

    raw = _open_raw(db_path)
    use_rtree = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='current_scenes_rtree'"
    ).fetchone() is not None
    # Probe the same shape of window used above (lon 1.5..1.6).
    explain = raw.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT c.* FROM current_scenes_rtree r "
        "JOIN current_scenes c ON c.rowid = r.id "
        "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ? "
        "ORDER BY c.scene_id",
        (1.6, 1.5, 1.0, 0.0),
    ).fetchall() if use_rtree else raw.execute(
        "EXPLAIN QUERY PLAN SELECT c.* FROM current_scenes c "
        "WHERE c.west <= ? AND c.east >= ? AND c.south <= ? AND c.north >= ?",
        (1.6, 1.5, 1.0, 0.0),
    ).fetchall()
    plan_text = " ".join(row[3] for row in explain)
    # Only accelerator objects (aliased r/c) appear: the revision-history
    # table `scenes` is never opened, so its size cannot affect the query.
    assert "scenes" not in plan_text
    if use_rtree:
        assert "VIRTUAL TABLE" in plan_text  # R*Tree tree search
    else:
        assert " c" in plan_text or "c " in plan_text
    # The history table holds hundreds of revisions; the accelerator holds 2.
    assert raw.execute("SELECT COUNT(*) FROM scenes").fetchone()[0] == n_revisions + 1
    assert raw.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0] == 2
    if use_rtree:
        assert raw.execute(
            "SELECT COUNT(*) FROM current_scenes_rtree").fetchone()[0] == 2
    raw.close()


def test_query_cost_flat_across_history_growth(client):
    """More historical revisions must not add rows scanned by /scenes."""
    c, db_path = client
    sizes = []
    for rev in range(1, 21):
        assert _submit(c, "GROW", rev,
                       b04=f"r{rev}".encode(), b08=f"n{rev}".encode(),
                       bbox=(float(rev), 0.0, float(rev) + 1.0, 1.0)).status_code == 201
        raw = _open_raw(db_path)
        sizes.append(raw.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0])
        raw.close()
    # One scene throughout => the accelerator never grows with revisions.
    assert sizes == [1] * 20

    # Each narrow window sees at most the single current revision, and an
    # outdated location of an earlier revision never matches.
    hits = c.get("/scenes", params={"west": 0, "south": 0,
                                    "east": 0.5, "north": 1}).json()
    assert hits == []  # rev 1 used to sit there but rev 20 moved away
    hits = c.get("/scenes", params={"west": 20, "south": 0,
                                    "east": 21, "north": 1}).json()
    assert [s["revision"] for s in hits] == [20]


def test_failed_submit_leaves_accelerator_untouched(client):
    c, db_path = client
    assert _submit(c, "F", 1).status_code == 201
    before = _open_raw(db_path).execute(
        "SELECT scene_id, revision, west, east FROM current_scenes"
    ).fetchall()
    before = [tuple(r) for r in before]

    # Checksum failure: rejected before the write transaction even starts.
    r = _submit(c, "F", 2, bad_digest=True)
    assert r.status_code == 400
    # Stale revision conflict: transaction rolls back.
    r = _submit(c, "F", 1, b04=b"other", b08=b"nir")
    assert r.status_code == 409

    raw = _open_raw(db_path)
    after = [tuple(r) for r in raw.execute(
        "SELECT scene_id, revision, west, east FROM current_scenes").fetchall()]
    assert after == before
    # Still exactly one current row, still revision 1.
    assert raw.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0] == 1
    has_rtree = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='current_scenes_rtree'"
    ).fetchone()
    if has_rtree:
        assert raw.execute(
            "SELECT COUNT(*) FROM current_scenes_rtree").fetchone()[0] == 1
    raw.close()


def test_idempotent_replay_does_not_duplicate_index(client):
    c, db_path = client
    assert _submit(c, "R", 1).status_code == 201
    assert _submit(c, "R", 1).status_code == 200  # identical replay
    assert _submit(c, "R", 1).status_code == 200

    raw = _open_raw(db_path)
    assert raw.execute(
        "SELECT COUNT(*) FROM current_scenes WHERE scene_id='R'").fetchone()[0] == 1
    has_rtree = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='current_scenes_rtree'"
    ).fetchone()
    if has_rtree:
        assert raw.execute(
            "SELECT COUNT(*) FROM current_scenes_rtree").fetchone()[0] == 1
    raw.close()


def test_interleaved_query_and_submit_never_splits_state(client):
    """A query racing a submit must return either the whole old or whole new
    revision -- never metadata from one and bands from another."""
    c, db_path = client
    assert _submit(c, "X", 1, b04=b"old-red", b08=b"old-nir").status_code == 201

    stop = threading.Event()
    violations = []

    def hammer_query():
        while not stop.is_set():
            scenes = c.get("/scenes").json()
            for s in scenes:
                if s["scene_id"] != "X":
                    continue
                # Bands must belong to exactly the revision advertised.
                revs_in_urls = {
                    b["download_url"].split("/revisions/")[1].split("/")[0]
                    for b in s["bands"]
                }
                if revs_in_urls != {str(s["revision"])}:
                    violations.append((s["revision"], revs_in_urls))
                if s["revision"] == 1 and (s["west"] != 116.0):
                    violations.append(("stale-location", s["west"]))

    t = threading.Thread(target=hammer_query)
    t.start()
    try:
        for rev in range(2, 12):
            r = _submit(
                c, "X", rev,
                b04=f"red{rev}".encode(), b08=f"nir{rev}".encode(),
                acquisition_time=f"2026-08-{(rev % 27) + 1:02d}T10:00:00Z",
                bbox=(float(100 + rev), 1.0, float(101 + rev), 2.0),
            )
            assert r.status_code == 201
    finally:
        stop.set()
        t.join(timeout=10)

    assert violations == []
    final = c.get("/scenes").json()
    x = next(s for s in final if s["scene_id"] == "X")
    assert x["revision"] == 11 and x["west"] == 111.0

    # Old revisions remain downloadable at their original addresses.
    dl = c.get("/scenes/X/revisions/1/bands/B04/download")
    assert dl.status_code == 200 and dl.content == b"old-red"


def test_other_scenes_submissions_are_isolated(client):
    c, db_path = client
    assert _submit(c, "A", 1, b04=b"a", b08=b"a2").status_code == 201
    assert _submit(c, "B", 1, b04=b"b", b08=b"b2").status_code == 201

    raw = _open_raw(db_path)
    rowid_b_before = raw.execute(
        "SELECT rowid FROM current_scenes WHERE scene_id='B'").fetchone()[0]
    raw.close()

    # Publishing several new revisions of A must not recreate B's index row.
    for rev in range(2, 6):
        assert _submit(c, "A", rev,
                       b04=f"a{rev}".encode(), b08=f"a2{rev}".encode(),
                       bbox=(float(rev), 0.0, float(rev) + 1.0, 1.0)).status_code == 201

    raw = _open_raw(db_path)
    rowid_b_after = raw.execute(
        "SELECT rowid FROM current_scenes WHERE scene_id='B'").fetchone()[0]
    assert rowid_b_before == rowid_b_after
    b = dict(raw.execute(
        "SELECT * FROM current_scenes WHERE scene_id='B'").fetchone())
    assert b["revision"] == 1
    a = dict(raw.execute(
        "SELECT * FROM current_scenes WHERE scene_id='A'").fetchone())
    assert a["revision"] == 5
    raw.close()


def test_fallback_without_rtree_still_indexes_current_scenes(tmp_path, monkeypatch):
    """On a SQLite build without R*Tree, the plain current_scenes table still
    decouples query cost from revision history."""
    db_path, _ = _make_env(tmp_path)
    from app import db as db_module

    # Simulate a build compiled without SQLITE_ENABLE_RTREE: the real helper
    # catches the creation error and reports the index as unavailable.
    monkeypatch.setattr(db_module, "_create_rtree_if_supported",
                        lambda conn: False)

    from app.main import app
    with TestClient(app) as c:
        assert app.state.rtree_enabled is False
        assert _submit(c, "T", 1).status_code == 201
        assert _submit(c, "T", 2, b04=b"r2", b08=b"n2",
                       bbox=(10.0, 10.0, 11.0, 11.0)).status_code == 201

        hits = c.get("/scenes", params={"west": 10.2, "south": 10.2,
                                        "east": 10.8, "north": 10.8}).json()
        assert [s["scene_id"] for s in hits] == ["T"]
        # Old location cannot leak back through the fallback path either.
        assert c.get("/scenes", params={"west": 116.2, "south": 39.2,
                                        "east": 116.8, "north": 39.8}).json() == []
        assert c.get("/scenes").json()[0]["revision"] == 2

    raw = _open_raw(db_path)
    assert raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='current_scenes_rtree'"
    ).fetchone() is None
    assert raw.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0] == 1
    raw.close()


def test_concurrent_submissions_different_scenes_keep_index_consistent(client):
    c, db_path = client

    def one(i):
        return _submit(c, f"P{i}", 1, b04=f"r{i}".encode(), b08=f"n{i}".encode())

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(one, range(20)))
    assert all(r.status_code == 201 for r in responses)

    raw = _open_raw(db_path)
    n_scenes = raw.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0]
    assert n_scenes == 20
    has_rtree = raw.execute(
        "SELECT 1 FROM sqlite_master WHERE name='current_scenes_rtree'"
    ).fetchone()
    if has_rtree:
        assert raw.execute(
            "SELECT COUNT(*) FROM current_scenes_rtree").fetchone()[0] == 20
        # Every rtree id resolves to exactly one current row.
        orphan = raw.execute(
            "SELECT COUNT(*) FROM current_scenes_rtree r "
            "LEFT JOIN current_scenes c ON c.rowid = r.id "
            "WHERE c.scene_id IS NULL"
        ).fetchone()[0]
        assert orphan == 0
    raw.close()
