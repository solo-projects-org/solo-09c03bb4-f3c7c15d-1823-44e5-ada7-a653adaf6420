"""Runtime settings, overridable through environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    db_path: str
    storage_dir: str

    @property
    def tmp_dir(self) -> str:
        return os.path.join(self.storage_dir, "tmp")

    @property
    def bands_dir(self) -> str:
        return os.path.join(self.storage_dir, "bands")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    db_path = os.environ.get("CATALOG_DB_PATH", "/data/catalog.db")
    storage_dir = os.environ.get("CATALOG_STORAGE_DIR", "/data/storage")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    os.makedirs(storage_dir, exist_ok=True)
    return Settings(db_path=db_path, storage_dir=storage_dir)
