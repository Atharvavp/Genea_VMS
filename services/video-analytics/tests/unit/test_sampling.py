"""Monotonic cadence with no catch-up (PLAN section 16.1)."""

from __future__ import annotations

import pytest

from app.analytics.sampling import FrameSampler

pytestmark = pytest.mark.unit


def test_the_first_frame_is_always_due():
    assert FrameSampler().should_process(100.0, 5.0) is True


def test_a_frame_before_the_deadline_is_skipped():
    sampler = FrameSampler()
    assert sampler.should_process(100.0, 5.0) is True
    assert sampler.should_process(100.1, 5.0) is False
    assert sampler.should_process(100.19, 5.0) is False


def test_the_exact_boundary_is_due():
    sampler = FrameSampler()
    sampler.should_process(100.0, 5.0)
    assert sampler.should_process(100.2, 5.0) is True


def test_the_next_deadline_is_now_plus_period_not_a_catch_up_burst():
    sampler = FrameSampler()
    sampler.should_process(100.0, 5.0)
    # Ten periods late: exactly one opportunity, and the next is 0.2s from *now*.
    assert sampler.should_process(102.0, 5.0) is True
    assert sampler.next_due == pytest.approx(102.2)
    assert sampler.should_process(102.1, 5.0) is False
    assert sampler.should_process(102.2, 5.0) is True


def test_a_busy_detector_opportunity_still_advanced_the_deadline():
    sampler = FrameSampler()
    assert sampler.should_process(100.0, 5.0) is True  # caller then dropped it
    assert sampler.should_process(100.05, 5.0) is False
    assert sampler.next_due == pytest.approx(100.2)


def test_reset_clears_the_deadline():
    sampler = FrameSampler()
    sampler.should_process(100.0, 5.0)
    sampler.reset()
    assert sampler.next_due is None
    assert sampler.should_process(100.01, 5.0) is True


@pytest.mark.parametrize("fps,period", [(1.0, 1.0), (5.0, 0.2), (10.0, 0.1)])
def test_period_for_each_supported_rate(fps: float, period: float):
    sampler = FrameSampler()
    sampler.should_process(50.0, fps)
    assert sampler.next_due == pytest.approx(50.0 + period)


@pytest.mark.parametrize("fps", [0.0, -1.0])
def test_a_non_positive_rate_is_rejected(fps: float):
    with pytest.raises(ValueError):
        FrameSampler().should_process(1.0, fps)


def test_the_sampler_uses_only_the_supplied_clock():
    """A backward wall clock is irrelevant: the caller passes monotonic time."""
    sampler = FrameSampler()
    assert sampler.should_process(1000.0, 5.0) is True
    assert sampler.should_process(1000.1, 5.0) is False
    assert sampler.should_process(1000.3, 5.0) is True
