"""Settings validation: the fixed upstream base and the data-root boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**overrides):
    base = {
        "semantic_data_dir": Path("/data"),
        "semantic_db_path": Path("/data/semantic.db"),
        "component4_api_base_url": "http://component4.test:8100",
    }
    base.update(overrides)
    return Settings(**base)


def test_defaults_match_the_frozen_table():
    settings = _settings()
    assert settings.semantic_http_port == 8200
    assert settings.semantic_index_batch_size == 4
    assert settings.semantic_poll_interval_seconds == 10
    assert settings.semantic_overlap_seconds == 300
    assert settings.semantic_full_reconcile_seconds == 21600
    assert settings.semantic_upload_max_bytes == 8 * 1024 * 1024
    assert settings.semantic_shutdown_timeout_seconds == 15


def test_settings_are_frozen():
    settings = _settings()
    with pytest.raises(ValidationError):
        settings.semantic_torch_threads = 8


@pytest.mark.parametrize(
    "url",
    [
        "ftp://component4:8100",
        "http://user:secret@component4:8100",
        "http://component4:8100/api",
        "http://component4:8100?x=1",
        "http://component4:8100#frag",
        "http:///nohost",
        "not-a-url",
        "",
        "http://component4:8100\n",
    ],
)
def test_unsafe_component4_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        _settings(component4_api_base_url=url)


def test_trailing_slash_is_normalised_away():
    assert _settings(
        component4_api_base_url="http://component4.test:8100/"
    ).component4_api_base_url == "http://component4.test:8100"


def test_db_path_must_be_strictly_beneath_the_data_root():
    with pytest.raises(ValidationError):
        _settings(semantic_db_path=Path("/elsewhere/semantic.db"))
    with pytest.raises(ValidationError):
        _settings(semantic_db_path=Path("/data"))


def test_relative_and_traversing_paths_are_rejected():
    with pytest.raises(ValidationError):
        _settings(semantic_data_dir=Path("relative/data"))
    with pytest.raises(ValidationError):
        _settings(semantic_db_path=Path("/data/../etc/semantic.db"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("semantic_torch_threads", 0),
        ("semantic_torch_threads", 9),
        ("semantic_index_batch_size", 9),
        ("semantic_poll_interval_seconds", 1),
        ("semantic_overlap_seconds", 59),
        ("semantic_full_reconcile_seconds", 299),
        ("semantic_upload_max_bytes", 1024),
        ("semantic_http_port", 0),
        ("semantic_log_level", "TRACE"),
    ],
)
def test_out_of_range_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_log_level_is_normalised():
    assert _settings(semantic_log_level="debug").semantic_log_level == "DEBUG"
