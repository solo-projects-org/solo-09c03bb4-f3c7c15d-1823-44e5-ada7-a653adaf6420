"""Two processes must be able to migrate/open the same catalog concurrently."""
import os
import subprocess
import sys
import tempfile

CATALOG = "/workspace/catalog"
PYLIBS = "/workspace/.pylibs"

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "catalog.db")
storage = os.path.join(tmp, "storage")
os.makedirs(storage)

# Legacy-format DB with a few revisions.
import sqlite3
conn = sqlite3.connect(db)
conn.executescript(
    "CREATE TABLE scenes (scene_id TEXT, revision INTEGER, acquisition_time TEXT,"
    " west REAL, south REAL, east REAL, north REAL,"
    " created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), PRIMARY KEY(scene_id,revision)) WITHOUT ROWID;"
    "CREATE TABLE bands (scene_id TEXT, revision INTEGER, band TEXT, sha256 TEXT,"
    " size_bytes INTEGER, stored_path TEXT, created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),"
    " PRIMARY KEY(scene_id,revision,band)) WITHOUT ROWID;")
conn.execute("INSERT INTO scenes (scene_id,revision,acquisition_time,west,south,east,north) "
             "VALUES ('A',1,'2026-01-01T00:00:00.000000Z',0,0,1,1)")
conn.commit()
conn.close()

migrate = (
    "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
    "from app.db import init_db; print('rtree', init_db())" % (PYLIBS, CATALOG))
env = dict(os.environ, CATALOG_DB_PATH=db, CATALOG_STORAGE_DIR=storage,
           PYTHONPATH=f"{PYLIBS}:{CATALOG}")

procs = [subprocess.Popen([sys.executable, "-c", migrate], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
         for _ in range(4)]
outs = [p.communicate() for p in procs]
codes = [p.returncode for p in procs]
print("exit codes:", codes)
for i, (out, err) in enumerate(outs):
    print(f"  proc{i}: out={out.strip().decode()!r} err={err.strip().decode()[:200]!r}")

# Final state must be consistent no matter how the startups interleaved.
sys.path.insert(0, PYLIBS)
sys.path.insert(0, CATALOG)
from app import db as d
c = d._connect(db)
use = d._rtree_exists(c)
print("drift after concurrent startup:", d.current_index_drift(c, use))
print("current rows:", c.execute("SELECT scene_id, revision FROM current_scenes").fetchall())
ok = all(code == 0 for code in codes) and d.current_index_drift(c, use) == 0
c.close()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
