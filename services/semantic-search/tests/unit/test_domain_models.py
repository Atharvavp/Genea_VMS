"""Identifier, timestamp, filter, and event-state semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.models import (
    Direction,
    EventRecord,
    ObjectCategory,
    ObjectClass,
    SearchFilters,
    SearchFiltersModel,
    format_utc,
    is_event_id,
    parse_utc,
    utc_now,
)
from tests.fakes.factories import make_event


def test_event_ids_follow_component4_shape():
    assert is_event_id("evt_" + "a" * 32)
    assert not is_event_id("evt_" + "A" * 32)
    assert not is_event_id("evt_" + "a" * 31)
    assert not is_event_id("cam_0123abcd")
    assert not is_event_id(None)
    assert not is_event_id("evt_" + "a" * 32 + "/../etc")


def test_timestamps_round_trip_at_millisecond_precision():
    moment = datetime(2026, 9, 7, 9, 15, 14, 123_456, tzinfo=timezone.utc)
    text = format_utc(moment)
    assert text == "2026-09-07T09:15:14.123Z"
    assert format_utc(parse_utc(text)) == text


def test_offset_timestamps_are_converted_to_utc():
    assert format_utc(parse_utc("2026-09-07T11:15:14.500+02:00")) == "2026-09-07T09:15:14.500Z"


def test_naive_timestamps_are_rejected_rather_than_assumed_utc():
    with pytest.raises(ValueError):
        parse_utc("2026-09-07T09:15:14.123")
    with pytest.raises(ValueError):
        parse_utc(datetime(2026, 9, 7, 9, 15, 14))


@pytest.mark.parametrize("value", ["", "not-a-time", "2026-13-01T00:00:00Z", "09/07/2026"])
def test_malformed_timestamps_are_rejected(value):
    with pytest.raises(ValueError):
        parse_utc(value)


def test_a_blank_camera_name_is_refused():
    with pytest.raises(ValueError):
        EventRecord(
            event_id=make_event(1).event_id,
            camera_id="acam_0123abcd",
            vms_camera_id="cam_0123abcd",
            camera_name="",
            crossed_at=utc_now(),
            object_category=ObjectCategory.VEHICLE,
            object_class=ObjectClass.CAR,
            direction=Direction.A_TO_B,
            discovered_at=utc_now(),
            source_last_seen_at=utc_now(),
        )


def test_event_record_rejects_foreign_identifiers():
    with pytest.raises(ValueError):
        EventRecord(
            event_id="not-an-event",
            camera_id="acam_0123abcd",
            vms_camera_id="cam_0123abcd",
            camera_name="Bay",
            crossed_at=utc_now(),
            object_category=ObjectCategory.VEHICLE,
            object_class=ObjectClass.CAR,
            direction=Direction.A_TO_B,
            discovered_at=utc_now(),
            source_last_seen_at=utc_now(),
        )


def test_contradictory_category_and_class_are_reported_not_rewritten():
    filters = SearchFilters(
        object_category=ObjectCategory.PERSON, object_class=ObjectClass.CAR
    )
    assert filters.is_contradictory
    assert not SearchFilters(
        object_category=ObjectCategory.VEHICLE, object_class=ObjectClass.CAR
    ).is_contradictory
    assert not SearchFilters(object_class=ObjectClass.CAR).is_contradictory


def test_from_must_be_strictly_before_to():
    now = utc_now()
    with pytest.raises(ValueError):
        SearchFilters(start=now, end=now)
    with pytest.raises(ValueError):
        SearchFilters(start=now, end=now - timedelta(seconds=1))
    SearchFilters(start=now, end=now + timedelta(seconds=1))


def test_filters_model_requires_aware_timestamps_and_valid_camera():
    with pytest.raises(ValueError):
        SearchFiltersModel(**{"from": "2026-09-07T00:00:00"})
    with pytest.raises(ValueError):
        SearchFiltersModel(camera_id="acam_XYZ")
    model = SearchFiltersModel(
        **{"from": "2026-09-01T00:00:00Z", "to": "2026-09-08T00:00:00Z"}
    )
    assert model.to_filters().start < model.to_filters().end


def test_filters_model_rejects_unknown_fields():
    with pytest.raises(ValueError):
        SearchFiltersModel(**{"line_id": "line_0123abcd"})


def test_empty_filters_are_recognised():
    assert SearchFilters().is_empty
    assert not SearchFilters(direction=Direction.A_TO_B).is_empty
