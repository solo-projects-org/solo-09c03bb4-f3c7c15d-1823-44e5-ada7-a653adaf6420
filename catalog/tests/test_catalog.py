"""End-to-end tests for the satellite scene catalog API."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    storage_dir = tmp_path / "storage"
    monkeypatch.setenv("CATALOG_DB_PATH", str(db_path))
    monkeypatch.setenv("CATALOG_STORAGE_DIR", str(storage_dir))
    # settings is cached via lru_cache; each test gets fresh env values
    from app import config
    config.get_settings.cache_clear()

    from app.main import app
    with TestClient(app) as c:
        yield c


def _data(payload: bytes) -> bytes:
    return payload


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _submit(client, scene_id="S1", revision=1, *, b04=b"red-band", b08=b"nir-band",
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


# --------------------------------------------------------------------------- #
def test_health_and_swagger(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/").status_code in (200, 307)


def test_submit_requires_both_bands_verified(client):
    r = _submit(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["submission_status"] == "created"
    assert body["revision"] == 1
    assert {b["band"] for b in body["bands"]} == {"B04", "B08"}
    for band in body["bands"]:
        assert band["download_url"].endswith(f"bands/{band['band']}/download")


def test_checksum_mismatch_rejected_and_no_catalog_trace(client, tmp_path):
    r = _submit(client, bad_digest=True)
    assert r.status_code == 400
    assert "checksum mismatch" in r.json()["detail"]
    # nothing was cataloged
    assert client.get("/scenes").json() == []
    # no temp leftovers, no band files
    leftovers = list((tmp_path / "storage").rglob("*.part"))
    assert leftovers == []
    stored = list((tmp_path / "storage" / "bands").rglob("*.bin"))
    assert stored == []


def test_missing_band_file_rejected(client):
    r = client.post(
        "/scenes/S1/revisions",
        data={
            "revision": "1", "acquisition_time": "2026-06-01T10:00:00Z",
            "west": "1", "south": "1", "east": "2", "north": "2",
            "sha256_b04": _sha(b"x"), "sha256_b08": _sha(b"y"),
        },
        files={"file_b04": ("b04.tif", b"x", "application/octet-stream")},
    )
    assert r.status_code == 422


def test_idempotent_replay_returns_original(client):
    r1 = _submit(client)
    assert r1.status_code == 201
    r2 = _submit(client)  # identical resubmission
    assert r2.status_code == 200
    assert r2.json()["submission_status"] == "idempotent_replay"
    assert r2.json()["bands"] == r1.json()["bands"]


def test_same_revision_content_conflict_rejected(client):
    assert _submit(client, b04=b"red-v1", b08=b"nir-v1").status_code == 201
    # different bytes -> different declared digest, same revision
    r = _submit(client, b04=b"red-v2-DIFFERENT", b08=b"nir-v1")
    assert r.status_code == 409
    assert "conflicting fields" in r.json()["detail"]
    # conflict on metadata only is also rejected
    r2 = client.post(
        "/scenes/S1/revisions",
        data={
            "revision": "1", "acquisition_time": "2026-06-01T11:00:00Z",
            "west": "116", "south": "39", "east": "117", "north": "40",
            "sha256_b04": _sha(b"red-v1"), "sha256_b08": _sha(b"nir-v1"),
        },
        files={
            "file_b04": ("b04.tif", b"red-v1", "application/octet-stream"),
            "file_b08": ("b08.tif", b"nir-v1", "application/octet-stream"),
        },
    )
    assert r2.status_code == 409
    # original stays intact and queryable
    meta = client.get("/scenes/S1/revisions/1").json()
    assert meta["acquisition_time"].startswith("2026-06-01T10:00:00")


def test_stale_revision_cannot_overwrite_newer(client):
    assert _submit(client, revision=2, b04=b"r2", b08=b"n2").status_code == 201
    r = _submit(client, revision=1, b04=b"r1", b08=b"n1")
    assert r.status_code == 409
    assert "stale revision" in r.json()["detail"]
    assert client.get("/scenes/S1/revisions/1").status_code == 404
    assert client.get("/scenes/S1/revisions/2").status_code == 200


def test_query_spatial_temporal_filtering_and_latest_only(client):
    # scene A: two revisions, revision 2 moves footprint far away and later time
    assert _submit(client, scene_id="A", revision=1,
                   b04=b"a1r", b08=b"a1n",
                   acquisition_time="2026-06-01T10:00:00Z",
                   bbox=(116.0, 39.0, 117.0, 40.0)).status_code == 201
    assert _submit(client, scene_id="A", revision=2,
                   b04=b"a2r", b08=b"a2n",
                   acquisition_time="2026-06-15T10:00:00Z",
                   bbox=(10.0, 10.0, 11.0, 11.0)).status_code == 201
    # scene B: stays in the original area, slightly later
    assert _submit(client, scene_id="B", revision=1,
                   b04=b"b1r", b08=b"b1n",
                   acquisition_time="2026-06-02T10:00:00Z",
                   bbox=(116.5, 39.5, 117.5, 40.5)).status_code == 201

    # no filter: only the CURRENT revision of each scene
    all_scenes = client.get("/scenes").json()
    assert {s["scene_id"]: s["revision"] for s in all_scenes} == {"A": 2, "B": 1}

    # spatial query near the original area: A's new footprint does not intersect,
    # old revision 1 must not leak back
    hits = client.get("/scenes", params={
        "west": 116.2, "south": 39.2, "east": 116.8, "north": 39.8,
    }).json()
    assert [s["scene_id"] for s in hits] == ["B"]

    # temporal window before A's new acquisition
    hits = client.get("/scenes", params={
        "start_time": "2026-06-01T00:00:00Z",
        "end_time": "2026-06-03T00:00:00Z",
    }).json()
    assert [s["scene_id"] for s in hits] == ["B"]

    # combined window covering everything: A rev2 and B
    hits = client.get("/scenes", params={
        "west": 0, "south": 0, "east": 180, "north": 90,
        "start_time": "2026-06-01T00:00:00Z",
        "end_time": "2026-06-30T00:00:00Z",
    }).json()
    assert {s["scene_id"] for s in hits} == {"A", "B"}


def test_download_current_and_superseded_revisions(client):
    assert _submit(client, scene_id="D", revision=1, b04=b"red-1", b08=b"nir-1").status_code == 201
    assert _submit(client, scene_id="D", revision=2, b04=b"red-2", b08=b"nir-2").status_code == 201

    # current revision files
    for band, expected in (("B04", b"red-2"), ("B08", b"nir-2")):
        r = client.get(f"/scenes/D/revisions/2/bands/{band}/download")
        assert r.status_code == 200
        assert r.content == expected
        assert r.headers["X-SHA-256"] == _sha(expected)

    # superseded revision remains fully downloadable
    r = client.get("/scenes/D/revisions/1/bands/B04/download")
    assert r.status_code == 200
    assert r.content == b"red-1"

    # unknown band / revision
    assert client.get("/scenes/D/revisions/1/bands/B99/download").status_code == 422
    assert client.get("/scenes/D/revisions/9/bands/B04/download").status_code == 404


def test_interleaved_downloads_survive_new_revision(client):
    """Simulate: a download of rev 1 starts, rev 2 is submitted, both finish."""
    assert _submit(client, scene_id="I", revision=1, b04=b"old-red" * 1000,
                   b08=b"old-nir" * 1000).status_code == 201

    started = threading.Event()
    result = {}

    def slow_download():
        with client.stream("GET", "/scenes/I/revisions/1/bands/B04/download") as resp:
            result["status"] = resp.status_code
            started.set()
            chunks = [chunk for chunk in resp.iter_bytes()]
            result["body"] = b"".join(chunks)
            result["header_sha"] = resp.headers["X-SHA-256"]

    t = threading.Thread(target=slow_download)
    t.start()
    assert started.wait(timeout=5)
    # while the old download is in flight, publish a new revision
    r = _submit(client, scene_id="I", revision=2, b04=b"new-red" * 1000,
                b08=b"new-nir" * 1000)
    assert r.status_code == 201
    t.join(timeout=10)

    assert result["status"] == 200
    assert result["body"] == b"old-red" * 1000
    assert result["header_sha"] == _sha(b"old-red" * 1000)
    # and the new revision downloads correctly too
    nr = client.get("/scenes/I/revisions/2/bands/B04/download")
    assert nr.content == b"new-red" * 1000


def test_concurrent_identical_submissions_single_revision(client):
    def one(_):
        return _submit(client, scene_id="C", revision=1, b04=b"cc-r", b08=b"cc-n")

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(one, range(4)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 200, 200, 201]
    bodies = [r.json() for r in responses]
    urls = {b["bands"][0]["download_url"] for b in bodies}
    assert len(urls) == 1  # all point at the same stored file
    # exactly one revision row
    assert client.get("/scenes/C/revisions/1").status_code == 200
    assert client.get("/scenes/C/revisions/2").status_code == 404


def test_concurrent_conflicting_submissions_one_wins(client):
    def submit(payload):
        return _submit(client, scene_id="K", revision=1, b04=payload, b08=b"common-nir")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, [b"payload-alpha", b"payload-beta"]))

    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409]
    winner = next(r for r in responses if r.status_code == 201).json()
    stored = client.get("/scenes/K/revisions/1").json()
    assert stored["bands"] == winner["bands"]


def test_persistence_across_restart(client, tmp_path, monkeypatch):
    assert _submit(client, scene_id="P", revision=1,
                   b04=b"persist-r", b08=b"persist-n").status_code == 201

    # simulate full service restart: rebuild app against the same on-disk state
    from app import config
    config.get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as second:
        r = second.get("/scenes")
        assert r.status_code == 200
        assert [s["scene_id"] for s in r.json()] == ["P"]
        dl = second.get("/scenes/P/revisions/1/bands/B08/download")
        assert dl.status_code == 200
        assert dl.content == b"persist-n"


def test_startup_refuses_missing_cataloged_file(client, tmp_path, monkeypatch):
    assert _submit(client, scene_id="Z", revision=1).status_code == 201
    # delete a stored band file behind the catalog's back
    stored = list((tmp_path / "storage" / "bands").rglob("*.bin"))
    assert len(stored) == 2
    stored[0].unlink()

    from app import config
    config.get_settings.cache_clear()
    from app.main import app
    with pytest.raises(RuntimeError, match="catalog integrity check failed"):
        with TestClient(app):
            pass


def test_orphan_temp_files_cleaned_on_startup(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    storage_dir = tmp_path / "storage"
    (storage_dir / "tmp").mkdir(parents=True)
    junk = storage_dir / "tmp" / "upload-deadbeef.part"
    junk.write_bytes(b"partial")
    monkeypatch.setenv("CATALOG_DB_PATH", str(db_path))
    monkeypatch.setenv("CATALOG_STORAGE_DIR", str(storage_dir))
    from app import config
    config.get_settings.cache_clear()
    from app.main import app
    with TestClient(app):
        assert not junk.exists()
