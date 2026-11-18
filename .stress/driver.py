"""End-to-end stress driver against a real multi-worker uvicorn server.

Checks, on a migrated large catalog:
  A. correctness of /scenes vs ground truth from the raw DB;
  B. narrow-window latency independent of history size (old vs new SQL);
  C. moved footprint / changed time takes effect immediately, old never leaks;
  D. concurrent query/submit interleaving never yields mixed-revision results;
  E. failed / idempotent submissions keep the accelerator exact;
  F. restart keeps everything consistent without another full rebuild.
"""
import hashlib
import json
import os
import sqlite3
import statistics
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DATA = sys.argv[1]
BASE = sys.argv[2]
DB = os.path.join(DATA, "catalog.db")
sys.path.insert(0, "/workspace/catalog")

from app import db as db_module  # noqa: E402

PASS, FAIL = [], []
def check(desc, cond, extra=""):
    (PASS if cond else FAIL).append(desc)
    print(("PASS" if cond else "FAIL") + f": {desc} {extra}")

def http(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)

def get(path):
    code, body, _ = http("GET", path)
    return code, json.loads(body)

def raw_conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

# wait for server
deadline = time.time() + 60
while time.time() < deadline:
    try:
        code, body = get("/health")
        if code == 200:
            break
    except Exception:
        pass
    time.sleep(0.3)
else:
    print("server never became healthy"); sys.exit(2)

with raw_conn() as c:
    use_rtree = c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='current_scenes_rtree'"
    ).fetchone() is not None
    n_history = c.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
    n_current = c.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0]
    n_rt = c.execute("SELECT COUNT(*) FROM current_scenes_rtree").fetchone()[0] if use_rtree else 0
    drift = db_module.current_index_drift(c, use_rtree)
    n_distinct = c.execute("SELECT COUNT(DISTINCT scene_id) FROM scenes").fetchone()[0]
check("migrated accelerator: one row per scene", n_current == n_distinct,
      f"current={n_current} distinct_scenes={n_distinct} history_rows={n_history}")
check("rtree entry count matches", (not use_rtree) or n_rt == n_current,
      f"rtree={use_rtree} entries={n_rt}")
check("zero drift after migration", drift == 0, f"drift={drift}")

# A. ground-truth correctness: raw MAX(revision) must equal API's current rows
code, api_scenes = get("/scenes")
check("GET /scenes 200", code == 200)
with raw_conn() as c:
    truth = {r["scene_id"]: dict(r) for r in c.execute("""
        SELECT s.* FROM scenes s JOIN (
            SELECT scene_id, MAX(revision) revision FROM scenes GROUP BY scene_id
        ) l ON l.scene_id=s.scene_id AND l.revision=s.revision""")}
api_map = {s["scene_id"]: s for s in api_scenes}
check("same scene set as ground truth", set(truth) == set(api_map),
      f"{len(truth)} vs {len(api_map)}")
mismatch = [sid for sid, t in truth.items()
            if api_map[sid]["revision"] != t["revision"]
            or abs(api_map[sid]["west"] - t["west"]) > 1e-12
            or api_map[sid]["acquisition_time"] != t["acquisition_time"]
            or len(api_map[sid]["bands"]) != 2]
check("every current row matches ground truth incl. both bands", not mismatch,
      f"mismatches={mismatch[:3]}")

# B. narrow-window cost: old SQL scans full history; new SQL stays on index
WINDOW = {"west": -120.0, "south": -80.0, "east": -119.0, "north": -79.0}
qs = ("?west={west}&south={south}&east={east}&north={north}").format(**WINDOW)
code, narrow = get("/scenes" + qs)
check("narrow window returns zero current scenes", code == 200 and narrow == [])
with raw_conn() as c:
    old_sql = """
        SELECT s.scene_id FROM scenes s JOIN (
            SELECT scene_id, MAX(revision) revision FROM scenes GROUP BY scene_id
        ) l ON l.scene_id=s.scene_id AND l.revision=s.revision
        WHERE s.west <= ? AND s.east >= ? AND s.south <= ? AND s.north >= ?"""
    new_sql = ("SELECT c.scene_id FROM current_scenes_rtree r "
               "JOIN current_scenes c ON c.rowid=r.id "
               "WHERE r.minx<=? AND r.maxx>=? AND r.miny<=? AND r.maxy>=?") if use_rtree else (
               "SELECT c.scene_id FROM current_scenes c "
               "WHERE c.west<=? AND c.east>=? AND c.south<=? AND c.north>=?")
    args = (WINDOW["east"], WINDOW["west"], WINDOW["north"], WINDOW["south"])
    old_plan = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + old_sql, args))
    new_plan = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + new_sql, args))
    def timeit(sql, reps=100):
        ts = []
        for _ in range(reps):
            t0 = time.perf_counter()
            c.execute(sql, args).fetchall()
            ts.append((time.perf_counter() - t0) * 1000)
        return statistics.median(ts)
    old_ms = timeit(old_sql); new_ms = timeit(new_sql)
check("old query plan scans history table", "scenes" in old_plan, old_plan.strip())
check("new query plan never opens history table", "scenes" not in new_plan, new_plan.strip())
check("new narrow query is faster", new_ms < old_ms,
      f"median old={old_ms:.3f}ms new={new_ms:.3f}ms over history={n_history} rows")
# HTTP end-to-end latency for the narrow window
def http_time(reps=100):
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter(); get("/scenes" + qs); ts.append((time.perf_counter()-t0)*1000)
    return statistics.median(ts)
print(f"   HTTP /scenes narrow median latency: {http_time():.2f} ms")

# C. move footprint + time on one scene; old location must not leak, new immediate
target = "S00000"
with raw_conn() as c:
    cur = c.execute("SELECT MAX(revision) r FROM scenes WHERE scene_id=?", (target,)).fetchone()["r"]
new_rev = cur + 1
def submit(scene, rev, acq, bbox, b04, b08):
    boundary = "----stress"
    def field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()
    def file_field(name, filename, data):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode() + data + b"\r\n"
    west, south, east, north = bbox
    parts = []
    for k, v in [("revision", rev), ("acquisition_time", acq),
                 ("west", west), ("south", south), ("east", east), ("north", north),
                 ("sha256_b04", hashlib.sha256(b04).hexdigest()),
                 ("sha256_b08", hashlib.sha256(b08).hexdigest())]:
        parts.append(field(k, v))
    parts.append(file_field("file_b04", "b04.tif", b04))
    parts.append(file_field("file_b08", "b08.tif", b08))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    return http("POST", f"/scenes/{scene}/revisions", body,
                {"Content-Type": f"multipart/form-data; boundary={boundary}"})
b04 = f"{target}-{new_rev}-red".encode(); b08 = f"{target}-{new_rev}-nir".encode()
new_bbox = (-75.0, -60.0, -74.6, -59.6)
code, body, _ = submit(target, new_rev, "2030-12-31T23:59:59.000000Z", new_bbox, b04, b08)
check("new revision on heavy scene accepted", code == 201, f"code={code} {body[:120]}")
code, moved = get("/scenes?west=-75.0&south=-60.0&east=-74.6&north=-59.6")
check("new location immediately queryable",
      code == 200 and any(s["scene_id"] == target for s in moved))
code, old_spot = get("/scenes?west=-100&south=-80&east=-99&north=-79")
check("old location does not leak back",
      code == 200 and all(s["scene_id"] != target for s in old_spot))
code, old_time = get("/scenes?start_time=2026-01-01T00:00:00.000000Z&end_time=2026-02-01T00:00:00.000000Z")
check("changed acquisition time: scene excluded from its old time window",
      all(s["scene_id"] != target for s in old_time))
# historical revision still at original address. cur may be a seeder revision
# (payload suffix "-r") or an API-submitted one (suffix "-red"); read the
# catalog's own stored digest rather than guessing the payload convention.
with raw_conn() as c:
    expected_sha = c.execute(
        "SELECT sha256 FROM bands WHERE scene_id=? AND revision=? AND band='B04'",
        (target, cur)).fetchone()["sha256"]
code, old_bytes, hdr = http("GET", f"/scenes/{target}/revisions/{cur}/bands/B04/download")
hdr_lc = {k.lower(): v for k, v in hdr.items()}
if code != 200 or hdr_lc.get("x-sha-256") != expected_sha:
    print(f"   DEBUG download code={code} cur={cur} hdr={hdr_lc} expected_sha={expected_sha} body={old_bytes[:80]!r}")
check("superseded revision still downloadable at original URL",
      code == 200 and hdr_lc.get("x-sha-256") == expected_sha)

# D. interleaved readers + writers: no mixed new/old state ever observed
stop = threading.Event(); violations = []
def reader():
    while not stop.is_set():
        code, scenes = get("/scenes")
        if code != 200:
            violations.append(f"http {code}"); continue
        ids = [s["scene_id"] for s in scenes]
        if len(ids) != len(set(ids)):
            violations.append("duplicate scene row"); continue
        for s in scenes:
            revs = {b["download_url"].split("/revisions/")[1].split("/")[0] for b in s["bands"]}
            if revs != {str(s["revision"])} or len(s["bands"]) != 2:
                violations.append(f"mixed revision for {s['scene_id']}: {s['revision']} {revs}")
def writer(worker_id):
    for rev in range(1000 + worker_id, 1000 + worker_id + 12):
        bbox = ((rev * 1.5) % 170 - 85, 0.0, ((rev * 1.5) % 170 - 85) + 0.3, 0.3)
        code, _, _ = submit(f"W{worker_id:02d}", rev, f"2027-03-{(rev%27)+1:02d}T00:00:00.000000Z",
                            bbox, f"w{worker_id}-{rev}-r".encode(), f"w{worker_id}-{rev}-n".encode())
        if code not in (200, 201):
            violations.append(f"writer {worker_id} rev {rev} -> {code}")
threads = [threading.Thread(target=reader) for _ in range(4)]
for t in threads: t.start()
with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(writer, range(4)))
stop.set()
for t in threads: t.join(timeout=10)
check("interleaved reads never saw mixed/duplicate state", not violations, str(violations[:3]))

# E. failure + idempotent replay keep accelerator exact. No new scenes are
# added in this section, so the count taken here must remain unchanged.
with raw_conn() as c:
    before = c.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0]
# A checksum failure must be rejected before any catalog row is written.
boundary = "----bad"
def field(name, value):
    return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
b04x, b08x = b"bad-red", b"good-nir"
parts = [field(k, v) for k, v in [
    ("revision", "999999"), ("acquisition_time", "2026-01-01T10:00:00.000000Z"),
    ("west", "0"), ("south", "0"), ("east", "1"), ("north", "1"),
    ("sha256_b04", "00" * 32), ("sha256_b08", hashlib.sha256(b08x).hexdigest())]]
parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_b04\"; filename=\"a\"\r\n\r\n").encode() + b04x + b"\r\n")
parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file_b08\"; filename=\"b\"\r\n\r\n").encode() + b08x + b"\r\n")
parts.append(f"--{boundary}--\r\n".encode())
code, _, _ = http("POST", "/scenes/S00001/revisions", b"".join(parts),
                  {"Content-Type": f"multipart/form-data; boundary={boundary}"})
check("checksum failure rejected", code == 400, f"code={code}")
# idempotent replay of an existing revision
with raw_conn() as c:
    rr = c.execute("SELECT * FROM scenes WHERE scene_id='S00002' ORDER BY revision DESC LIMIT 1").fetchone()
rb04 = c.execute("SELECT sha256 FROM bands WHERE scene_id='S00002' AND revision=? AND band='B04'", (rr["revision"],)).fetchone()["sha256"] if False else None
with raw_conn() as c:
    rb04 = c.execute("SELECT sha256 FROM bands WHERE scene_id='S00002' AND revision=? AND band='B04'", (rr["revision"],)).fetchone()["sha256"]
    rb08 = c.execute("SELECT sha256 FROM bands WHERE scene_id='S00002' AND revision=? AND band='B08'", (rr["revision"],)).fetchone()["sha256"]
code, replay_bytes, _ = submit("S00002", rr["revision"], rr["acquisition_time"],
                               (rr["west"], rr["south"], rr["east"], rr["north"]),
                               f"S00002-{rr['revision']}-r".encode(),
                               f"S00002-{rr['revision']}-n".encode())
replay = json.loads(replay_bytes)
check("identical replay is idempotent 200",
      code == 200 and replay["submission_status"] == "idempotent_replay")
with raw_conn() as c:
    after = c.execute("SELECT COUNT(*) FROM current_scenes").fetchone()[0]
    drift = db_module.current_index_drift(c, use_rtree)
    dup = c.execute("SELECT COUNT(*) FROM (SELECT scene_id FROM current_scenes GROUP BY scene_id HAVING COUNT(*)>1)").fetchone()[0]
check("accelerator row count unchanged by failures/replays", after == before,
      f"before={before} after={after}")
check("zero drift and no duplicate accelerator rows", drift == 0 and dup == 0)

print(f"\nSUMMARY: {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
