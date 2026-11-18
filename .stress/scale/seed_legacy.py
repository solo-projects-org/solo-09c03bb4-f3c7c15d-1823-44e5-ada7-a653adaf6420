"""Build a large LEGACY-format catalog (scenes/bands only, no accelerator).

Simulates a long-running catalog that has accumulated many revisions, so the
new service must migrate it on first startup. Band files are materialized on
disk because startup runs the catalog integrity check.
"""
import hashlib
import os
import sqlite3
import sys
import time

DATA = sys.argv[1]
N_SCENES = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
N_REVS = int(sys.argv[3]) if len(sys.argv) > 3 else 20

db_path = os.path.join(DATA, "catalog.db")
storage = os.path.join(DATA, "storage")
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

t0 = time.perf_counter()
n_files = 0
for si in range(N_SCENES):
    scene = f"S{si:05d}"
    for rev in range(1, N_REVS + 1):
        # Each revision shifts the footprint and acquisition time, so only the
        # final revision's location is "current".
        lon = (si * 7 + rev * 3) % 170 - 85
        lat = (si * 13 + rev * 5) % 80 - 40
        west, east = lon, lon + 0.4
        south, north = lat, lat + 0.4
        acq = f"2026-01-{(rev % 27) + 1:02d}T10:00:00.000000Z"
        conn.execute(
            "INSERT INTO scenes VALUES (?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (scene, rev, acq, west, south, east, north),
        )
        for band, tag in (("B04", "r"), ("B08", "n")):
            payload = f"{scene}-{rev}-{tag}".encode()
            digest = hashlib.sha256(payload).hexdigest()
            rel = f"bands/{digest[:2]}/{digest}.bin"
            apath = os.path.join(storage, rel)
            if not os.path.exists(apath):
                os.makedirs(os.path.dirname(apath), exist_ok=True)
                with open(apath, "wb") as fh:
                    fh.write(payload)
                n_files += 1
            conn.execute(
                "INSERT INTO bands VALUES (?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (scene, rev, band, digest, len(payload), rel),
            )
conn.commit()
n_scenes = conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
n_bands = conn.execute("SELECT COUNT(*) FROM bands").fetchone()[0]
# Guarantee a legacy catalog with no trace of the new accelerator.
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
conn.close()
dt = time.perf_counter() - t0
print(f"seeded {n_scenes} scene rows / {n_bands} band rows / {n_files} files "
      f"in {dt:.1f}s; tables={sorted(tables)}")
assert "current_scenes" not in tables and "current_scenes_rtree" not in tables
