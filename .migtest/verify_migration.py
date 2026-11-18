"""Standalone migration check against real legacy catalog.db files.

Run with: python -I verify_migration.py <case_dir>
The script never writes into the source copies; case_dir holds a copied DB.
"""
import os
import sqlite3
import sys

case_dir = sys.argv[1]
db_path = os.path.join(case_dir, "catalog.db")
storage_dir = os.path.join(case_dir, "storage")
os.environ["CATALOG_DB_PATH"] = db_path
os.environ["CATALOG_STORAGE_DIR"] = storage_dir

sys.path.insert(0, "/workspace/.pylibs")
sys.path.insert(0, "/workspace/catalog")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import db as db_module  # noqa: E402

raw0 = sqlite3.connect(db_path)
before_scenes = raw0.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
before_bands = raw0.execute("SELECT COUNT(*) FROM bands").fetchone()[0]
legacy_current = dict(raw0.execute(
    "SELECT s.scene_id, MAX(s.revision) FROM scenes s GROUP BY s.scene_id"
).fetchall())
raw0.close()

with TestClient(app) as client:
    rtree_on = app.state.rtree_enabled
    health = client.get("/health").json()
    scenes = client.get("/scenes").json()
    api_current = {s["scene_id"]: s["revision"] for s in scenes}
    # exercise spatial and temporal filter paths
    spatial = client.get("/scenes", params={
        "west": 116.2, "south": 39.2, "east": 116.8, "north": 39.8}).json()
    temporal = client.get("/scenes", params={
        "start_time": "2026-06-20T00:00:00Z",
        "end_time": "2026-07-10T00:00:00Z"}).json()
    # every historical revision still reachable at its original address
    old_dl = client.get("/scenes/SCENE_A/revisions/1/bands/B08/download")
    cur_dl = client.get("/scenes/SCENE_A/revisions/3/bands/B04/download")

raw = sqlite3.connect(db_path)
raw.row_factory = sqlite3.Row
after_scenes = raw.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
after_bands = raw.execute("SELECT COUNT(*) FROM bands").fetchone()[0]
user_version = raw.execute("PRAGMA user_version").fetchone()[0]
drift = db_module.current_index_drift(raw, rtree_on)
idx_rows = {r["scene_id"]: r["revision"]
            for r in raw.execute("SELECT scene_id, revision FROM current_scenes")}
rt_count = None
if rtree_on:
    rt_count = raw.execute(
        "SELECT COUNT(*) FROM current_scenes_rtree").fetchone()[0]
raw.close()

print(f"case={case_dir}")
print(f"  rtree_enabled       : {rtree_on}")
print(f"  health              : {health}")
print(f"  scenes before/after : {before_scenes} / {after_scenes} (history preserved)")
print(f"  bands before/after  : {before_bands} / {after_bands}")
print(f"  user_version        : {user_version}")
print(f"  legacy MAX(rev)     : {legacy_current}")
print(f"  accelerator rows    : {idx_rows}")
print(f"  /scenes current     : {api_current}")
print(f"  drift after startup : {drift}")
print(f"  rtree entries       : {rt_count}")
print(f"  spatial hit ids     : {[s['scene_id'] for s in spatial]}")
print(f"  temporal hit ids    : {[s['scene_id'] for s in temporal]}")
print(f"  old rev1 B08 dl     : {old_dl.status_code} "
      f"sha={old_dl.headers.get('x-sha-256')}")
print(f"  cur rev3 B04 dl     : {cur_dl.status_code} "
      f"sha={cur_dl.headers.get('x-sha-256')}")

ok = (
    health == {"status": "ok", "database": "ok"}
    and before_scenes == after_scenes
    and before_bands == after_bands
    and user_version >= 1
    and drift == 0
    and legacy_current == idx_rows == api_current
    and (rt_count is None or rt_count == len(idx_rows))
    and old_dl.status_code == 200
    and cur_dl.status_code == 200
)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
