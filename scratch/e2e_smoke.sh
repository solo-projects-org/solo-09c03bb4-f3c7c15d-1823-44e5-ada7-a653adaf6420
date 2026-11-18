#!/usr/bin/env bash
# End-to-end smoke test against a real uvicorn server (2 workers).
set -u
BASE=${BASE:-http://127.0.0.1:8077}
ROOT=${DATA_ROOT:-/workspace/scratch/e2e-data}
DATA=$ROOT/files
mkdir -p "$DATA"
PASS=0; FAIL=0
check() { # desc expected actual
  if [ "$2" == "$3" ]; then echo "PASS: $1 ($3)"; PASS=$((PASS+1));
  else echo "FAIL: $1 expected $2 got $3"; FAIL=$((FAIL+1)); fi
}

sha() { /workspace/.venv/bin/python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }

printf 'red-band-v1'   > "$DATA/b04_v1.tif"
printf 'nir-band-v1'   > "$DATA/b08_v1.tif"
printf 'red-band-v2'   > "$DATA/b04_v2.tif"
printf 'nir-band-v2'   > "$DATA/b08_v2.tif"
printf 'red-bad'       > "$DATA/b04_bad.tif"
S04=$(sha "$DATA/b04_v1.tif"); S08=$(sha "$DATA/b08_v1.tif")

submit_scene() { # scene rev time west south east north f04 f08 d04 d08
  curl -s -o "$DATA/resp.json" -w "%{http_code}" -X POST "$BASE/scenes/$1/revisions" \
    -F "revision=$2" -F "acquisition_time=$3" \
    -F "west=$4" -F "south=$5" -F "east=$6" -F "north=$7" \
    -F "sha256_b04=${10}" -F "sha256_b08=${11}" \
    -F "file_b04=@${8};type=application/octet-stream" \
    -F "file_b08=@${9};type=application/octet-stream"
}

submit() { submit_scene SCENE_A "$@"; }

echo "== 1. initial submission (both bands verified) =="
code=$(submit 1 2026-06-01T10:00:00Z 116 39 117 40 "$DATA/b04_v1.tif" "$DATA/b08_v1.tif" "$S04" "$S08")
check "submit rev1 -> 201" 201 "$code"
grep -q '"submission_status":"created"' "$DATA/resp.json" && { echo "PASS: status created"; PASS=$((PASS+1)); } || { echo "FAIL: created flag"; FAIL=$((FAIL+1)); }
grep -q 'B04' "$DATA/resp.json" && grep -q 'B08' "$DATA/resp.json" && { echo "PASS: both bands in response"; PASS=$((PASS+1)); } || { echo "FAIL: bands"; FAIL=$((FAIL+1)); }

echo "== 2. identical replay =="
code=$(submit 1 2026-06-01T10:00:00Z 116 39 117 40 "$DATA/b04_v1.tif" "$DATA/b08_v1.tif" "$S04" "$S08")
check "replay -> 200" 200 "$code"
grep -q '"idempotent_replay"' "$DATA/resp.json" && { echo "PASS: idempotent_replay"; PASS=$((PASS+1)); } || { echo "FAIL: replay flag"; FAIL=$((FAIL+1)); }

echo "== 3. content conflict on same revision =="
code=$(submit 1 2026-06-01T10:00:00Z 116 39 117 40 "$DATA/b04_v2.tif" "$DATA/b08_v1.tif" "$(sha "$DATA/b04_v2.tif")" "$S08")
check "conflict -> 409" 409 "$code"

echo "== 4. stale revision after higher one exists =="
S04V2=$(sha "$DATA/b04_v2.tif"); S08V2=$(sha "$DATA/b08_v2.tif")
code=$(submit 3 2026-07-01T10:00:00Z 10 10 11 11 "$DATA/b04_v2.tif" "$DATA/b08_v2.tif" "$S04V2" "$S08V2")
check "submit rev3 (out-of-order) -> 201" 201 "$code"
# a never-seen lower revision arriving late must be rejected (not an idempotent replay)
code=$(submit 2 2026-06-15T10:00:00Z 116 39 117 40 "$DATA/b04_v2.tif" "$DATA/b08_v1.tif" "$S04V2" "$S08")
check "late stale rev2 -> 409" 409 "$code"

echo "== 5. checksum failure leaves no trace =="
before=$(find "$ROOT/storage" -name '*.part' | wc -l)
code=$(submit 1 2026-06-01T10:00:00Z 0 0 1 1 "$DATA/b04_bad.tif" "$DATA/b08_v1.tif" "0000000000000000000000000000000000000000000000000000000000000000" "$S08")
check "bad digest -> 400" 400 "$code"
after=$(find "$ROOT/storage" -name '*.part' | wc -l)
check "no temp leftovers" "$before" "$after"
hits=$(curl -s "$BASE/scenes?west=0&south=0&east=1&north=1" | /workspace/.venv/bin/python -c "import json,sys;print(len(json.load(sys.stdin)))")
check "bad upload not cataloged" 0 "$hits"

echo "== 6. query: current revision only, spatial/temporal filters =="
# A stable neighbor scene still sitting in the original area/time window
code=$(submit_scene SCENE_B 1 2026-06-02T10:00:00Z 116.5 39.5 117.5 40.5 "$DATA/b04_v1.tif" "$DATA/b08_v1.tif" "$S04" "$S08")
check "neighbor SCENE_B -> 201" 201 "$code"
# NOTE: SCENE_B also sits in the 116/39 area, so assert SCENE_A (whose current
# revision moved away / changed time) is excluded rather than asserting a count.
hits=$(curl -s "$BASE/scenes?west=116.2&south=39.2&east=116.8&north=39.8" | /workspace/.venv/bin/python -c "import json,sys;print(','.join(s['scene_id'] for s in json.load(sys.stdin)))")
check "SCENE_A old footprint excluded (B still matches)" "SCENE_B" "$hits"
hits=$(curl -s "$BASE/scenes?west=9&south=9&east=12&north=12" | /workspace/.venv/bin/python -c "import json,sys;print(','.join(s['scene_id'] for s in json.load(sys.stdin)))")
check "new footprint intersects SCENE_A" "SCENE_A" "$hits"
hits=$(curl -s "$BASE/scenes?start_time=2026-06-20T00:00:00Z&end_time=2026-07-10T00:00:00Z" | /workspace/.venv/bin/python -c "import json,sys;print(','.join(s['scene_id'] for s in json.load(sys.stdin)))")
check "time window hits SCENE_A rev3" "SCENE_A" "$hits"
hits=$(curl -s "$BASE/scenes?start_time=2026-05-01T00:00:00Z&end_time=2026-06-10T00:00:00Z" | /workspace/.venv/bin/python -c "import json,sys;print(','.join(s['scene_id'] for s in json.load(sys.stdin)))")
check "old window excludes current SCENE_A" "SCENE_B" "$hits"

echo "== 7. downloads: current and superseded revisions =="
curl -s "$BASE/scenes/SCENE_A/revisions/3/bands/B04/download" -o "$DATA/dl_b04_r2.bin"
cmp -s "$DATA/dl_b04_r2.bin" "$DATA/b04_v2.tif" && { echo "PASS: rev3 B04 bytes"; PASS=$((PASS+1)); } || { echo "FAIL: rev2 download"; FAIL=$((FAIL+1)); }
curl -s "$BASE/scenes/SCENE_A/revisions/1/bands/B08/download" -o "$DATA/dl_b08_r1.bin"
cmp -s "$DATA/dl_b08_r1.bin" "$DATA/b08_v1.tif" && { echo "PASS: superseded rev1 B08 still complete"; PASS=$((PASS+1)); } || { echo "FAIL: rev1 download"; FAIL=$((FAIL+1)); }
dlsha=$(curl -s "$BASE/scenes/SCENE_A/revisions/1/bands/B08/download" | sha256sum | cut -d' ' -f1)
check "downloaded digest matches catalog" "$S08" "$dlsha"

echo "== 8. concurrent identical submissions across 2 workers =="
rm -f "$DATA/c"*.code
for i in 1 2 3 4; do
  curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/scenes/SCENE_C/revisions" \
    -F "revision=1" -F "acquisition_time=2026-06-01T10:00:00Z" \
    -F "west=116" -F "south=39" -F "east=117" -F "north=40" \
    -F "sha256_b04=$S04" -F "sha256_b08=$S08" \
    -F "file_b04=@$DATA/b04_v1.tif;type=application/octet-stream" \
    -F "file_b08=@$DATA/b08_v1.tif;type=application/octet-stream" > "$DATA/c$i.code" &
done
wait
sort "$DATA"/c*.code | tr '\n' ' '; echo
codes=$(sort "$DATA"/c*.code | tr '\n' ',' | sed 's/,$//')
check "concurrent: one 201 + three 200" "200,200,200,201" "$codes"
rows=$(/workspace/.venv/bin/python -c "
import sqlite3
c=sqlite3.connect('$ROOT/catalog.db')
print(c.execute(\"select count(*) from scenes where scene_id='SCENE_C'\").fetchone()[0],
      c.execute(\"select count(*) from bands where scene_id='SCENE_C'\").fetchone()[0])")
check "exactly 1 scene row + 2 band rows" "1 2" "$rows"

echo
echo "RESULT: $PASS passed, $FAIL failed"
exit $FAIL
