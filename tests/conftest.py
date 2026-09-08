"""Shared fixtures. Nothing here contacts Component 4, the VMS, or the network."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The model directory is only read by the real_model tier; unit and integration
# tiers must never load it.
MODEL_DIR = Path(os.environ.get("SEMANTIC_MODEL_DIR", "/opt/models/siglip"))


@pytest.fixture()
def settings(tmp_path: Path):
    from app.config import Settings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        semantic_data_dir=data_dir,
        semantic_db_path=data_dir / "semantic.db",
        semantic_model_dir=MODEL_DIR,
        component4_api_base_url="http://component4.test:8100",
    )


@pytest.fixture()
def database(settings):
    from app.persistence.database import Database

    db = Database(settings.semantic_db_path)
    db.ensure_parent()
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def repositories(database):
    from app.persistence.repositories import (
        EventRepository,
        RepresentationRepository,
        SyncRepository,
    )

    return (
        EventRepository(database),
        RepresentationRepository(database),
        SyncRepository(database),
    )
