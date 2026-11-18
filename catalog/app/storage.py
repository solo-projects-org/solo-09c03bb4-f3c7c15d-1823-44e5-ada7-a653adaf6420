"""Local content-addressed file storage for band files.

Every band file is stored under ``<storage>/bands/ab/<full-sha256>.bin`` keyed
by its verified SHA-256, so identical content is shared and distinct content can
never overwrite another file. Uploads first land in ``<storage>/tmp`` and are
hashed while streaming; they are only moved into the content store after the
checksum is proven correct, and database rows are only inserted afterwards.
Thus the catalog can never point at a missing or unverified file.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from .config import Settings

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StoredBand:
    sha256: str
    size_bytes: int
    stored_path: str  # relative to storage_dir


def validate_sha256(value: str) -> str:
    value = (value or "").strip().lower()
    if not _SHA256_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid SHA-256 hex digest: {value!r}",
        )
    return value


def _absolute(settings: Settings, stored_path: str) -> Path:
    """Resolve a stored relative path, rejecting any traversal attempt."""
    base = Path(settings.storage_dir).resolve()
    candidate = (base / stored_path).resolve()
    if base not in candidate.parents and candidate != base:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid stored path",
        )
    return candidate


def band_path(settings: Settings, stored_path: str) -> Path:
    path = _absolute(settings, stored_path)
    if not path.is_file():
        # The catalog's own integrity rule says this must not happen; surface
        # it as a server error rather than a misleading 404.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="cataloged band file is missing from storage",
        )
    return path


def spool_and_verify(upload: UploadFile, expected_sha256: str, settings: Settings) -> Path:
    """Stream an upload to a private temp file while computing SHA-256.

    Raises HTTP 400 when the digest does not match the declared one. The temp
    file lives in the storage volume so the later move is atomic.
    """
    os.makedirs(settings.tmp_dir, exist_ok=True)
    tmp_path = Path(settings.tmp_dir) / f"upload-{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = upload.file.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
    finally:
        upload.file.close()

    actual = digest.hexdigest()
    if actual != expected_sha256:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"checksum mismatch for {upload.filename!r}: "
                f"declared {expected_sha256}, computed {actual}"
            ),
        )
    if size == 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"band file {upload.filename!r} is empty",
        )
    return tmp_path


def place_band(tmp_path: Path, sha256: str, settings: Settings) -> StoredBand:
    """Move a verified temp file into the content-addressed store (atomic)."""
    rel_dir = Path("bands") / sha256[:2]
    abs_dir = Path(settings.storage_dir) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    rel_path = rel_dir / f"{sha256}.bin"
    final_path = Path(settings.storage_dir) / rel_path

    if not final_path.exists():
        # os.replace is atomic on the same filesystem; a concurrent identical
        # upload simply loses the race and keeps the shared copy.
        os.replace(tmp_path, final_path)
    else:
        tmp_path.unlink(missing_ok=True)

    return StoredBand(
        sha256=sha256,
        size_bytes=final_path.stat().st_size,
        stored_path=rel_path.as_posix(),
    )


def cleanup_tmp(settings: Settings) -> int:
    """Remove leftover partial uploads (e.g. after a crash) at startup."""
    tmp = Path(settings.tmp_dir)
    if not tmp.exists():
        return 0
    removed = 0
    for part in tmp.glob("upload-*.part"):
        try:
            part.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def verify_catalog_integrity(settings: Settings) -> None:
    """Fail startup loudly if any cataloged band file is missing."""
    from .db import _connect  # local import to avoid a cycle

    conn = _connect(settings.db_path)
    try:
        rows = conn.execute("SELECT stored_path FROM bands").fetchall()
    finally:
        conn.close()
    missing = [
        row["stored_path"]
        for row in rows
        if not (Path(settings.storage_dir) / row["stored_path"]).is_file()
    ]
    if missing:
        raise RuntimeError(
            "catalog integrity check failed; missing band files: " + ", ".join(missing[:10])
        )
