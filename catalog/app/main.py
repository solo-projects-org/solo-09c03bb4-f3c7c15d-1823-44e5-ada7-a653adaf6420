"""FastAPI application: scene revision catalog with verified band files."""
from __future__ import annotations

import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import storage
from .config import Settings, get_settings
from .db import check_db_alive, get_conn, init_db
from .schemas import BandOut, HealthOut, RevisionOut, SceneSummary

_SCENE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_BANDS = ("B04", "B08")
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def db_conn() -> sqlite3.Connection:
    with get_conn() as conn:
        yield conn


def _validate_scene_id(scene_id: str) -> str:
    if not _SCENE_ID_RE.match(scene_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scene_id may only contain letters, digits, '_', '.', '-' (max 128 chars)",
        )
    return scene_id


def _parse_utc(value: str, field: str) -> datetime:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} is required",
        )
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be ISO-8601, got {value!r}",
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_ts(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


def _validate_bbox(west: float, south: float, east: float, north: float) -> None:
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise HTTPException(422, "longitude must be within [-180, 180]")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise HTTPException(422, "latitude must be within [-90, 90]")
    if west > east or south > north:
        raise HTTPException(422, "bbox requires west <= east and south <= north")


def _download_url(request: Request, scene_id: str, revision: int, band: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/scenes/{scene_id}/revisions/{revision}/bands/{band}/download"


def _bands_out(
    request: Request,
    settings: Settings,
    conn: sqlite3.Connection,
    scene_id: str,
    revision: int,
) -> list[BandOut]:
    rows = conn.execute(
        "SELECT band, sha256, size_bytes, stored_path FROM bands "
        "WHERE scene_id = ? AND revision = ? ORDER BY band",
        (scene_id, revision),
    ).fetchall()
    result = []
    for row in rows:
        # Defensive: never hand out a URL whose file is not actually on disk.
        storage.band_path(settings, row["stored_path"])
        result.append(
            BandOut(
                band=row["band"],
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                download_url=_download_url(request, scene_id, revision, row["band"]),
            )
        )
    return result


def _revision_out(
    request: Request,
    settings: Settings,
    conn: sqlite3.Connection,
    scene_row: sqlite3.Row,
    submission_status: str,
) -> RevisionOut:
    bands = _bands_out(request, settings, conn, scene_row["scene_id"], scene_row["revision"])
    return RevisionOut(
        scene_id=scene_row["scene_id"],
        revision=scene_row["revision"],
        acquisition_time=scene_row["acquisition_time"],
        west=scene_row["west"],
        south=scene_row["south"],
        east=scene_row["east"],
        north=scene_row["north"],
        bands=bands,
        submission_status=submission_status,
    )


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    storage.cleanup_tmp(settings)
    # Refuse to start if the database references band files that are gone.
    storage.verify_catalog_integrity(settings)
    yield


app = FastAPI(
    title="Satellite Scene Catalog API",
    version="1.0.0",
    description=(
        "Catalog of satellite scene revisions (B04 red and B08 NIR bands). "
        "A revision becomes queryable only after BOTH band files have been "
        "verified against their declared SHA-256 digests."
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthOut, tags=["health"])
def health():
    check_db_alive()
    return HealthOut(status="ok", database="ok")


@app.post(
    "/scenes/{scene_id}/revisions",
    response_model=RevisionOut,
    status_code=status.HTTP_201_CREATED,
    tags=["scenes"],
    responses={
        409: {"description": "Conflicting content for the same revision, or stale revision number"},
        400: {"description": "SHA-256 verification failed or empty file"},
    },
)
def submit_revision(
    scene_id: str,
    request: Request,
    http_response: Response,
    conn: Annotated[sqlite3.Connection, Depends(db_conn)],
    revision: Annotated[int, Form(description="Monotonically increasing revision number")],
    acquisition_time: Annotated[str, Form(description="Acquisition time, ISO-8601 (UTC)")],
    west: Annotated[float, Form()],
    south: Annotated[float, Form()],
    east: Annotated[float, Form()],
    north: Annotated[float, Form()],
    sha256_b04: Annotated[str, Form(description="Declared SHA-256 of the B04 file")],
    sha256_b08: Annotated[str, Form(description="Declared SHA-256 of the B08 file")],
    file_b04: Annotated[UploadFile, File(description="B04 (red) band file")],
    file_b08: Annotated[UploadFile, File(description="B08 (NIR) band file")],
):
    settings = get_settings()
    scene_id = _validate_scene_id(scene_id)
    if revision < 1:
        raise HTTPException(422, "revision must be a positive integer")
    _validate_bbox(west, south, east, north)
    acq_utc = _parse_utc(acquisition_time, "acquisition_time")
    acq_str = _normalize_ts(acq_utc)
    digest_b04 = storage.validate_sha256(sha256_b04)
    digest_b08 = storage.validate_sha256(sha256_b08)
    if file_b04 is None or not file_b04.filename:
        raise HTTPException(422, "file_b04 is required")
    if file_b08 is None or not file_b08.filename:
        raise HTTPException(422, "file_b08 is required")
    if digest_b04 == digest_b08:
        raise HTTPException(400, "B04 and B08 files must be distinct (identical SHA-256)")

    # 1) Stream both uploads to private temp files and verify checksums BEFORE
    #    touching the database. A failed verification leaves no catalog trace.
    tmp_b04 = storage.spool_and_verify(file_b04, digest_b04, settings)
    try:
        tmp_b08 = storage.spool_and_verify(file_b08, digest_b08, settings)
    except BaseException:
        tmp_b04.unlink(missing_ok=True)
        raise

    try:
        # 2) Serialize concurrent writers; all decision logic is inside the lock.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM scenes WHERE scene_id = ? AND revision = ?",
            (scene_id, revision),
        ).fetchone()

        if existing is not None:
            # Same (scene, revision) already exists: either an idempotent
            # replay (everything identical) or a hard content conflict.
            differences = []
            if existing["acquisition_time"] != acq_str:
                differences.append("acquisition_time")
            for field, submitted in (
                ("west", west),
                ("south", south),
                ("east", east),
                ("north", north),
            ):
                if existing[field] != submitted:
                    differences.append(field)

            existing_bands = {
                row["band"]: row
                for row in conn.execute(
                    "SELECT band, sha256 FROM bands WHERE scene_id = ? AND revision = ?",
                    (scene_id, revision),
                ).fetchall()
            }
            for band, digest in (("B04", digest_b04), ("B08", digest_b08)):
                if band not in existing_bands or existing_bands[band]["sha256"] != digest:
                    differences.append(f"sha256_{band.lower()}")

            if differences:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"revision {revision} of scene {scene_id} already exists "
                        f"with different content; conflicting fields: {', '.join(differences)}. "
                        "Submit a new, higher revision number instead."
                    ),
                )

            # Idempotent replay: bytes were re-verified in step 1; return the
            # original result unchanged (HTTP 200).
            http_response.status_code = status.HTTP_200_OK
            response = _revision_out(request, settings, conn, existing, "idempotent_replay")
            conn.commit()
            return response

        # 3) New revision: a late, older revision number must not overwrite the
        #    current one.
        latest = conn.execute(
            "SELECT MAX(revision) AS r FROM scenes WHERE scene_id = ?", (scene_id,)
        ).fetchone()["r"]
        if latest is not None and revision <= latest:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"stale revision {revision}: scene {scene_id} already has "
                    f"revision {latest}; submit a revision number greater than {latest}"
                ),
            )

        # 4) Move verified files into the content-addressed store, then insert
        #    catalog rows. Files are in place before any row can reference them.
        stored_b04 = storage.place_band(tmp_b04, digest_b04, settings)
        stored_b08 = storage.place_band(tmp_b08, digest_b08, settings)

        conn.execute(
            "INSERT INTO scenes (scene_id, revision, acquisition_time, west, south, east, north) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scene_id, revision, acq_str, west, south, east, north),
        )
        conn.execute(
            "INSERT INTO bands (scene_id, revision, band, sha256, size_bytes, stored_path) "
            "VALUES (?, ?, 'B04', ?, ?, ?)",
            (scene_id, revision, stored_b04.sha256, stored_b04.size_bytes, stored_b04.stored_path),
        )
        conn.execute(
            "INSERT INTO bands (scene_id, revision, band, sha256, size_bytes, stored_path) "
            "VALUES (?, ?, 'B08', ?, ?, ?)",
            (scene_id, revision, stored_b08.sha256, stored_b08.size_bytes, stored_b08.stored_path),
        )
        conn.commit()

        scene_row = conn.execute(
            "SELECT * FROM scenes WHERE scene_id = ? AND revision = ?",
            (scene_id, revision),
        ).fetchone()
        return _revision_out(request, settings, conn, scene_row, "created")
    except BaseException:
        # Roll back the transaction (handled by the connection context too) and
        # make sure no verified temp file is orphaned on failure.
        conn.rollback()
        tmp_b04.unlink(missing_ok=True)
        tmp_b08.unlink(missing_ok=True)
        raise
    finally:
        tmp_b04.unlink(missing_ok=True)
        tmp_b08.unlink(missing_ok=True)


@app.get("/scenes", response_model=list[SceneSummary], tags=["scenes"])
def query_scenes(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db_conn)],
    west: Optional[float] = None,
    south: Optional[float] = None,
    east: Optional[float] = None,
    north: Optional[float] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
):
    """Return the CURRENT (highest) revision of each scene whose footprint and
    acquisition time intersect the given lon/lat rectangle and UTC time window.

    Only scenes with both verified bands are ever present, so every result
    carries B04 and B08 download URLs.
    """
    settings = get_settings()
    bbox_values = [west, south, east, north]
    has_bbox = any(v is not None for v in bbox_values)
    if has_bbox and any(v is None for v in bbox_values):
        raise HTTPException(422, "bbox query requires all of west, south, east, north")
    if has_bbox:
        _validate_bbox(west, south, east, north)

    start = _normalize_ts(_parse_utc(start_time, "start_time")) if start_time else None
    end = _normalize_ts(_parse_utc(end_time, "end_time")) if end_time else None
    if start and end and start > end:
        raise HTTPException(422, "start_time must be <= end_time")

    rows = conn.execute(
        """
        SELECT s.*
        FROM scenes s
        JOIN (
            SELECT scene_id, MAX(revision) AS revision
            FROM scenes
            GROUP BY scene_id
        ) latest
          ON latest.scene_id = s.scene_id AND latest.revision = s.revision
        WHERE (:has_bbox = 0
               OR (s.west <= :east AND s.east >= :west
                   AND s.south <= :north AND s.north >= :south))
          AND (:start IS NULL OR s.acquisition_time >= :start)
          AND (:end IS NULL OR s.acquisition_time <= :end)
        ORDER BY s.scene_id
        """,
        {
            "has_bbox": 1 if has_bbox else 0,
            "west": west,
            "south": south,
            "east": east,
            "north": north,
            "start": start,
            "end": end,
        },
    ).fetchall()

    return [
        SceneSummary(
            scene_id=row["scene_id"],
            revision=row["revision"],
            acquisition_time=row["acquisition_time"],
            west=row["west"],
            south=row["south"],
            east=row["east"],
            north=row["north"],
            bands=_bands_out(request, settings, conn, row["scene_id"], row["revision"]),
        )
        for row in rows
    ]


@app.get(
    "/scenes/{scene_id}/revisions/{revision}",
    response_model=RevisionOut,
    tags=["scenes"],
)
def get_revision(
    scene_id: str,
    revision: int,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(db_conn)],
):
    """Fetch any stored revision (including superseded ones) with download URLs."""
    settings = get_settings()
    _validate_scene_id(scene_id)
    row = conn.execute(
        "SELECT * FROM scenes WHERE scene_id = ? AND revision = ?",
        (scene_id, revision),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"scene {scene_id} revision {revision} not found")
    return _revision_out(request, settings, conn, row, None)


@app.get(
    "/scenes/{scene_id}/revisions/{revision}/bands/{band}/download",
    tags=["download"],
    responses={404: {"description": "Scene, revision or band not found"}},
)
def download_band(
    scene_id: str,
    revision: int,
    band: str,
    conn: Annotated[sqlite3.Connection, Depends(db_conn)],
):
    """Stream a band file. Band files are immutable and never removed, so a
    download started against an old revision keeps working after new revisions
    arrive."""
    settings = get_settings()
    _validate_scene_id(scene_id)
    if band not in _BANDS:
        raise HTTPException(422, "band must be one of B04, B08")

    row = conn.execute(
        "SELECT stored_path, sha256, size_bytes FROM bands "
        "WHERE scene_id = ? AND revision = ? AND band = ?",
        (scene_id, revision, band),
    ).fetchone()
    if row is None:
        raise HTTPException(
            404, f"band {band} of scene {scene_id} revision {revision} not found"
        )

    path = storage.band_path(settings, row["stored_path"])
    filename = f"{scene_id}_r{revision}_{band}.bin"
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"X-SHA-256": row["sha256"], "X-Size-Bytes": str(row["size_bytes"])},
    )
