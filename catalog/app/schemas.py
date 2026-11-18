"""Pydantic response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

BandName = Literal["B04", "B08"]


class BandOut(BaseModel):
    band: BandName
    sha256: str
    size_bytes: int
    download_url: str


class RevisionOut(BaseModel):
    scene_id: str
    revision: int
    acquisition_time: str
    west: float
    south: float
    east: float
    north: float
    bands: list[BandOut]
    # Only meaningful on the POST that stored the revision; null on GET.
    submission_status: Optional[
        Literal["created", "idempotent_replay"]
    ] = Field(default=None,
              description="created = stored now; idempotent_replay = identical revision already existed; null = fetched via GET")


class SceneSummary(BaseModel):
    scene_id: str
    revision: int
    acquisition_time: str
    west: float
    south: float
    east: float
    north: float
    bands: list[BandOut]


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
