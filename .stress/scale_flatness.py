"""Grow revision depth on a FIXED set of scenes; narrow query must stay flat."""
import os
import sqlite3
import statistics
import subprocess
import sys
import time

BASE = "/workspace/.stress/scale"
PYLIBS = "/workspace/.pylibs"
CATALOG = "/workspace/catalog"

def run(cmd, env_extra=None, cwd=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = PYLIBS
    if env_extra:
        env.update(env_extra)
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)

def measure(db, label):
    c = sqlite3.connect(db)
    n_hist = c.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
    n_cur = c.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0]
    # Empty corner of the globe -> zero candidates. Tuples bind in
    # (east, west, north, south) predicate order for both SQL forms.
    args = (-178.0, -179.0, -88.0, -89.0)
    old_sql = ("SELECT s.scene_id FROM scenes s JOIN ("
               "SELECT scene_id, MAX(revision) revision FROM scenes GROUP BY scene_id"
               ") l ON l.scene_id=s.scene_id AND l.revision=s.revision "
               "WHERE s.west<=? AND s.east>=? AND s.south<=? AND s.north>=?")
    new_sql = ("SELECT c.scene_id FROM current_scenes_rtree r "
               "JOIN current_scenes c ON c.rowid=r.id "
               "WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=? "
               "AND c.west<=? AND c.east>=? AND c.south<=? AND c.north>=?")
    def med(sql, params, reps=100):
        ts = []
        for _ in range(reps):
            t0 = time.perf_counter(); c.execute(sql, params).fetchall()
            ts.append((time.perf_counter() - t0) * 1000)
        return statistics.median(ts)
    old_ms = med(old_sql, args)
    new_ms = med(new_sql, args + args)
    # Both SQL forms bind predicates in (east, west, north, south) order.
    broad = (100.0, -100.0, 60.0, -60.0)
    o = {r[0] for r in c.execute(old_sql, broad)}
    n = {r[0] for r in c.execute(new_sql, broad + broad)}
    if o != n:
        print(f"   [debug] old={len(o)} new={len(n)} o-n={list(o-n)[:3]} n-o={list(n-o)[:3]}")
    c.close()
    print(f"{label:>14} | history={n_hist:>6} current={n_cur:>4} | "
          f"old={old_ms:7.3f}ms new={new_ms:6.3f}ms | equivalent={o == n}")
    return new_ms

print(f"{'depth':>14} | {'rows':>20} | {'narrow-window median latency':>32}")
results = []
for depth in (5, 10, 20, 40):
    d = f"{BASE}/d{depth}"
    run(["rm", "-rf", d])
    os.makedirs(f"{d}/storage", exist_ok=True)
    r = run([sys.executable, "-I", f"{BASE}/seed_legacy.py", d, "300", str(depth)])
    if r.returncode != 0:
        print("seed failed", r.stderr); sys.exit(1)
    # Migrate by importing the app's init path directly (no server needed).
    migrate = (
        "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
        "from app.db import init_db; print('rtree', init_db())" % (PYLIBS, CATALOG))
    r = run([sys.executable, "-c", migrate],
            {"CATALOG_DB_PATH": f"{d}/catalog.db",
             "CATALOG_STORAGE_DIR": f"{d}/storage"})
    if r.returncode != 0:
        print("migrate failed", r.stderr, r.stdout); sys.exit(1)
    results.append(measure(f"{d}/catalog.db", f"depth={depth}"))

# Flatness assertion: despite 8x history growth, new latency stays within a
# small constant band (allow scheduling noise; never an 8x linear climb).
lo, hi = min(results), max(results)
print(f"\nnew-query latency range across 8x history growth: {lo:.4f}..{hi:.4f} ms")
print("FLAT (no linear growth with revision count):", hi <= max(lo * 3.0, lo + 0.05))
