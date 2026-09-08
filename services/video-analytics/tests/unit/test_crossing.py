"""Deterministic crossing semantics (PLAN section 18)."""

from __future__ import annotations

import pytest

from app.analytics.crossing import (
    SIDE_EPSILON,
    CrossingEngine,
    Side,
    TrackerStateOverflow,
    classify_side,
    direction_for,
    segments_intersect,
    signed_distance,
)
from app.domain.models import CrossingDirection, LineDirection
from tests.fakes import factories

pytestmark = pytest.mark.unit

CAMERA = "acam_0123abcd"
SESSION = "ws_" + "1" * 32

# A horizontal line drawn A-left to B-right across the middle of the frame.
H_A = (0.2, 0.5)
H_B = (0.8, 0.5)


def engine(direction=LineDirection.BOTH, a=H_A, b=H_B) -> CrossingEngine:
    return CrossingEngine(
        camera_id=CAMERA,
        line=factories.line(a=a, b=b, direction=direction),
        session_id=SESSION,
    )


# -- side classification ----------------------------------------------------


def test_above_a_left_to_right_line_is_the_a_side():
    """Screen y grows downward, so 'above' means a smaller y."""
    assert classify_side((0.5, 0.2), H_A, H_B) is Side.A_SIDE
    assert classify_side((0.5, 0.8), H_A, H_B) is Side.B_SIDE


def test_reversing_the_endpoints_swaps_the_half_planes():
    assert classify_side((0.5, 0.2), H_B, H_A) is Side.B_SIDE
    assert classify_side((0.5, 0.8), H_B, H_A) is Side.A_SIDE


def test_a_vertical_line_splits_left_and_right():
    a, b = (0.5, 0.2), (0.5, 0.8)
    assert classify_side((0.8, 0.5), a, b) is Side.A_SIDE
    assert classify_side((0.2, 0.5), a, b) is Side.B_SIDE


def test_a_diagonal_line():
    a, b = (0.1, 0.1), (0.9, 0.9)
    assert classify_side((0.9, 0.1), a, b) is Side.A_SIDE
    assert classify_side((0.1, 0.9), a, b) is Side.B_SIDE


def test_points_inside_the_deadband_are_on_line():
    assert classify_side((0.5, 0.5), H_A, H_B) is Side.ON_LINE
    assert classify_side((0.5, 0.5 + SIDE_EPSILON / 2), H_A, H_B) is Side.ON_LINE


def test_exactly_epsilon_away_is_stable():
    assert classify_side((0.5, 0.5 + SIDE_EPSILON), H_A, H_B) is Side.B_SIDE
    assert classify_side((0.5, 0.5 - SIDE_EPSILON), H_A, H_B) is Side.A_SIDE


def test_signed_distance_is_normalised_by_line_length():
    assert signed_distance((0.5, 0.6), H_A, H_B) == pytest.approx(0.1)
    assert signed_distance((0.5, 0.4), H_A, H_B) == pytest.approx(-0.1)


def test_coincident_endpoints_are_rejected():
    with pytest.raises(ValueError):
        signed_distance((0.5, 0.5), (0.5, 0.5), (0.5, 0.5))


def test_the_positive_normal_agrees_with_the_ui_arrow_formula():
    """n = (-dy, dx) / |v| must point into the B half-plane."""
    import math

    dx, dy = H_B[0] - H_A[0], H_B[1] - H_A[1]
    length = math.hypot(dx, dy)
    normal = (-dy / length, dx / length)
    midpoint = ((H_A[0] + H_B[0]) / 2, (H_A[1] + H_B[1]) / 2)
    tip = (midpoint[0] + normal[0] * 0.1, midpoint[1] + normal[1] * 0.1)
    assert classify_side(tip, H_A, H_B) is Side.B_SIDE


# -- finite segment intersection --------------------------------------------


def test_a_crossing_movement_intersects():
    assert segments_intersect((0.5, 0.3), (0.5, 0.7), H_A, H_B) is True


def test_an_extension_only_crossing_does_not_intersect():
    assert segments_intersect((0.95, 0.3), (0.95, 0.7), H_A, H_B) is False


def test_touching_an_endpoint_counts_as_an_intersection():
    assert segments_intersect((0.2, 0.3), (0.2, 0.7), H_A, H_B) is True
    assert segments_intersect((0.8, 0.3), (0.8, 0.7), H_A, H_B) is True


def test_a_parallel_movement_does_not_intersect():
    assert segments_intersect((0.3, 0.4), (0.7, 0.4), H_A, H_B) is False


def test_direction_for_requires_opposite_sides():
    assert direction_for(Side.A_SIDE, Side.B_SIDE) is CrossingDirection.A_TO_B
    assert direction_for(Side.B_SIDE, Side.A_SIDE) is CrossingDirection.B_TO_A
    with pytest.raises(ValueError):
        direction_for(Side.A_SIDE, Side.A_SIDE)
    with pytest.raises(ValueError):
        direction_for(Side.ON_LINE, Side.B_SIDE)


# -- the engine -------------------------------------------------------------


def test_the_first_observation_never_fires():
    assert engine().observe(1, (0.5, 0.2), 100.0) is None


def test_a_same_side_update_never_fires_and_refreshes_the_anchor():
    eng = engine()
    eng.observe(1, (0.5, 0.2), 100.0)
    assert eng.observe(1, (0.4, 0.25), 100.2) is None
    # The refreshed anchor is the one used for the next transition.
    observation = eng.observe(1, (0.4, 0.8), 100.4)
    assert observation is not None


def test_an_on_line_point_neither_fires_nor_moves_the_anchor():
    eng = engine()
    eng.observe(1, (0.5, 0.2), 100.0)
    assert eng.observe(1, (0.95, 0.5), 100.2) is None
    # The anchor is still (0.5, 0.2), so this movement crosses the finite line.
    assert eng.observe(1, (0.5, 0.8), 100.4) is not None


def test_a_full_a_to_b_crossing_fires_once():
    eng = engine()
    eng.observe(1, (0.5, 0.2), 100.0)
    observation = eng.observe(1, (0.5, 0.8), 100.2)
    assert observation is not None
    assert observation.direction is CrossingDirection.A_TO_B
    assert observation.dedupe_key == (CAMERA, "line_00000001", SESSION, 1, "A_TO_B")


def test_a_full_b_to_a_crossing_fires_once():
    eng = engine()
    eng.observe(1, (0.5, 0.8), 100.0)
    observation = eng.observe(1, (0.5, 0.2), 100.2)
    assert observation.direction is CrossingDirection.B_TO_A


@pytest.mark.parametrize(
    "policy,first,second,expected",
    [
        (LineDirection.A_TO_B, (0.5, 0.2), (0.5, 0.8), CrossingDirection.A_TO_B),
        (LineDirection.A_TO_B, (0.5, 0.8), (0.5, 0.2), None),
        (LineDirection.B_TO_A, (0.5, 0.8), (0.5, 0.2), CrossingDirection.B_TO_A),
        (LineDirection.B_TO_A, (0.5, 0.2), (0.5, 0.8), None),
        (LineDirection.BOTH, (0.5, 0.2), (0.5, 0.8), CrossingDirection.A_TO_B),
        (LineDirection.BOTH, (0.5, 0.8), (0.5, 0.2), CrossingDirection.B_TO_A),
    ],
)
def test_the_configured_policy_gates_the_actual_direction(
    policy, first, second, expected
):
    eng = engine(policy)
    eng.observe(1, first, 100.0)
    observation = eng.observe(1, second, 100.2)
    if expected is None:
        assert observation is None
    else:
        assert observation is not None and observation.direction is expected


def test_a_disallowed_direction_still_advances_the_stable_state():
    eng = engine(LineDirection.A_TO_B)
    eng.observe(1, (0.5, 0.8), 100.0)
    assert eng.observe(1, (0.5, 0.2), 100.2) is None  # B->A blocked by policy
    # Now genuinely A->B: it fires because the anchor moved to the A side.
    assert eng.observe(1, (0.5, 0.8), 100.4) is not None


def test_an_extension_only_crossing_advances_state_without_firing():
    eng = engine()
    eng.observe(1, (0.95, 0.2), 100.0)
    assert eng.observe(1, (0.95, 0.8), 100.2) is None
    # It must not fire later without another genuine side change.
    assert eng.observe(1, (0.95, 0.85), 100.4) is None
    assert eng.observe(1, (0.5, 0.2), 100.6) is not None


def test_a_crossing_across_skipped_frames_still_fires():
    """The exact on-line frame need never be observed."""
    eng = engine()
    eng.observe(1, (0.3, 0.1), 100.0)
    observation = eng.observe(1, (0.7, 0.9), 105.0)
    assert observation is not None
    assert observation.direction is CrossingDirection.A_TO_B


def test_deadband_oscillation_is_suppressed():
    eng = engine()
    eng.observe(1, (0.5, 0.495), 100.0)
    for index, y in enumerate((0.503, 0.497, 0.502, 0.5)):
        assert eng.observe(1, (0.5, y), 100.2 * (index + 1)) is None


def test_the_same_direction_fires_only_once_per_track_and_session():
    eng = engine()
    eng.observe(1, (0.5, 0.2), 100.0)
    first = eng.observe(1, (0.5, 0.8), 100.2)
    eng.mark_persisted(first)
    eng.observe(1, (0.5, 0.2), 100.4)  # back (fires B_TO_A under BOTH)
    second = eng.observe(1, (0.5, 0.8), 100.6)
    assert second is None


def test_the_reverse_direction_may_fire_once_when_both_is_allowed():
    eng = engine(LineDirection.BOTH)
    eng.observe(1, (0.5, 0.2), 100.0)
    forward = eng.observe(1, (0.5, 0.8), 100.2)
    eng.mark_persisted(forward)
    backward = eng.observe(1, (0.5, 0.2), 100.4)
    assert backward is not None
    assert backward.direction is CrossingDirection.B_TO_A


def test_a_key_is_not_marked_until_persistence_succeeds():
    eng = engine()
    eng.observe(1, (0.5, 0.2), 100.0)
    observation = eng.observe(1, (0.5, 0.8), 100.2)
    assert eng.fired_keys() == frozenset()  # write failed: nothing marked
    eng.observe(1, (0.5, 0.2), 100.4)
    retry = eng.observe(1, (0.5, 0.8), 100.6)
    assert retry is not None and retry.dedupe_key == observation.dedupe_key
    eng.mark_persisted(retry)
    assert retry.dedupe_key in eng.fired_keys()


def test_separate_tracks_have_separate_state():
    eng = engine()
    eng.observe(1, (0.5, 0.2), 100.0)
    eng.observe(2, (0.5, 0.8), 100.0)
    assert eng.observe(1, (0.5, 0.8), 100.2) is not None
    assert eng.observe(2, (0.5, 0.2), 100.2) is not None
    assert eng.tracked_count == 2


def test_pruning_removes_state_for_removed_tracks_only():
    eng = engine()
    eng.observe(1, (0.5, 0.2), 100.0)
    eng.observe(2, (0.5, 0.2), 100.0)
    fired = eng.observe(1, (0.5, 0.8), 100.2)
    eng.mark_persisted(fired)
    assert eng.prune({2}) == 1
    assert eng.tracked_count == 1
    assert eng.fired_keys() == frozenset()


def test_the_history_cap_is_an_invariant_failure():
    eng = CrossingEngine(
        camera_id=CAMERA, line=factories.line(), session_id=SESSION, max_histories=3
    )
    for track_id in range(3):
        eng.observe(track_id, (0.5, 0.2), 100.0)
    with pytest.raises(TrackerStateOverflow):
        eng.observe(99, (0.5, 0.2), 100.0)


def test_reset_clears_history_and_changes_the_dedupe_scope():
    eng = engine()
    eng.observe(1, (0.5, 0.2), 100.0)
    fired = eng.observe(1, (0.5, 0.8), 100.2)
    eng.mark_persisted(fired)
    new_session = "ws_" + "2" * 32
    eng.reset(line=factories.line(), session_id=new_session)
    assert eng.tracked_count == 0
    assert eng.fired_keys() == frozenset()
    eng.observe(1, (0.5, 0.2), 200.0)
    again = eng.observe(1, (0.5, 0.8), 200.2)
    assert again is not None
    assert again.dedupe_key[2] == new_session


def test_a_reversed_line_definition_produces_the_mirrored_direction():
    eng = engine(a=H_B, b=H_A)
    eng.observe(1, (0.5, 0.2), 100.0)
    observation = eng.observe(1, (0.5, 0.8), 100.2)
    assert observation.direction is CrossingDirection.B_TO_A


def test_a_disabled_line_is_never_reached_through_the_engine():
    """The worker filters a disabled line before the engine exists."""
    from app.analytics.types import WorkerConfig

    config = factories.worker_config(line_record=factories.line(enabled=False))
    assert isinstance(config, WorkerConfig)
    assert config.active_line is None
