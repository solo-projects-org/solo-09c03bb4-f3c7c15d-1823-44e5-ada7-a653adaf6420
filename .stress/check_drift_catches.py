"""Confirm current_index_drift catches every kind of rtree corruption."""
import os
import tempfile

from app import db as d

tmp = tempfile.mkdtemp()
os.environ["CATALOG_DB_PATH"] = os.path.join(tmp, "c.db")
os.environ["CATALOG_STORAGE_DIR"] = os.path.join(tmp, "storage")
from app import config
config.get_settings.cache_clear()

conn = d._connect(config.get_settings().db_path)
conn.executescript(d.SCHEMA)
use = d._create_rtree_if_supported(conn)
conn.execute("BEGIN IMMEDIATE")
d.rebuild_current_index(conn, use)
assert d.current_index_drift(conn, use) == 0

# Seed two current rows through the helper (FK needs the scenes rows).
for sid in ("A", "B"):
    conn.execute(
        "INSERT INTO scenes (scene_id, revision, acquisition_time,"
        " west, south, east, north) VALUES (?,?,?,?,?,?,?)",
        (sid, 1, "2026-01-01T00:00:00.000000Z",
         100.0 if sid == "A" else 20.0,
         10.0 if sid == "A" else 20.0,
         100.4 if sid == "A" else 20.4,
         10.4 if sid == "A" else 20.4),
    )
d.upsert_current_revision(conn, use, scene_id="A", revision=1,
                          acquisition_time="2026-01-01T00:00:00.000000Z",
                          west=100.0, south=10.0, east=100.4, north=10.4,
                          is_first_revision=True)
d.upsert_current_revision(conn, use, scene_id="B", revision=1,
                          acquisition_time="2026-01-01T00:00:00.000000Z",
                          west=20.0, south=20.0, east=20.4, north=20.4,
                          is_first_revision=True)
assert d.current_index_drift(conn, use) == 0, "clean state must have zero drift"

rowid_a = conn.execute(
    "SELECT rowid FROM current_scenes WHERE scene_id='A'").fetchone()[0]

# 1. stale box on an existing entry (old location far away)
conn.execute("UPDATE current_scenes_rtree SET minx=0, maxx=1, miny=0, maxy=1 WHERE id=?",
             (rowid_a,))
print("stale box drift:", d.current_index_drift(conn, use))
assert d.current_index_drift(conn, use) >= 1

# repair
conn.execute("UPDATE current_scenes_rtree SET minx=100.0, maxx=100.4, miny=10.0, maxy=10.4 WHERE id=?",
             (rowid_a,))
assert d.current_index_drift(conn, use) == 0

# 2. slightly-stale box (one revision's small move, ~0.5 deg) must still trip
conn.execute("UPDATE current_scenes_rtree SET minx=99.5, maxx=99.9 WHERE id=?",
             (rowid_a,))
print("small-move drift:", d.current_index_drift(conn, use))
assert d.current_index_drift(conn, use) >= 1
conn.rollback()
conn.close()
print("drift detection OK")
