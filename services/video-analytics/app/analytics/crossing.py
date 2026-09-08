"""Directional finite-segment line-crossing detection.

Vocabulary, shared verbatim with the browser overlay in ``app/static/app.js``:

* The configured line is directed ``A = (x1, y1) -> B = (x2, y2)`` in normalized
  image coordinates. Both ends use the same screen coordinate system: origin
  top-left, x grows right, **y grows down**.
* ``signed_distance(P) = cross(B - A, P - A) / |B - A|``.
* ``A_SIDE`` is the negative half-plane, ``B_SIDE`` the positive one. These name
  half-planes, **not** proximity to endpoint A or B. For a horizontal line drawn
  A-left to B-right, points visually above it are ``A_SIDE``.
* ``A_TO_B`` is a movement from a stable ``A_SIDE`` to a stable ``B_SIDE``.

An event requires all of: a confirmed track, two opposite *stable* sides, a
movement segment that intersects the **finite** segment A-B, a direction the
line policy allows, and a durable write. Detection alone is never an event.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final

from app.domain.models import CrossingDirection, LineDirection, LineRecord

__all__ = [
    "Side",
    "SIDE_EPSILON",
    "INTERSECTION_TOLERANCE",
    "MAX_TRACK_HISTORIES",
    "classify_side",
    "signed_distance",
    "segments_intersect",
    "direction_for",
    "TrackCrossingState",
    "CrossingEngine",
    "CrossingObservation",
    "TrackerStateOverflow",
]

#: Deadband in normalized image units. A centroid must be at least this far from
#: the line before its side counts as stable, which suppresses edge jitter.
SIDE_EPSILON: Final[float] = 0.01

#: Numeric tolerance for the orientation tests.
INTERSECTION_TOLERANCE: Final[float] = 1e-9

#: Invariant safeguard; exceeding it stops that worker rather than growing.
MAX_TRACK_HISTORIES: Final[int] = 10_000


class Side(str, Enum):
    A_SIDE = "A_SIDE"
    B_SIDE = "B_SIDE"
    ON_LINE = "ON_LINE"


class TrackerStateOverflow(RuntimeError):
    """Per-camera track history exceeded its hard cap."""


Point = tuple[float, float]


def signed_distance(point: Point, a: Point, b: Point) -> float:
    """Normalized perpendicular distance; sign selects the half-plane."""
    vx, vy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(vx, vy)
    if length <= 0.0:
        raise ValueError("line endpoints must not coincide")
    wx, wy = point[0] - a[0], point[1] - a[1]
    return (vx * wy - vy * wx) / length


def classify_side(
    point: Point, a: Point, b: Point, epsilon: float = SIDE_EPSILON
) -> Side:
    distance = signed_distance(point, a, b)
    if distance <= -epsilon:
        return Side.A_SIDE
    if distance >= epsilon:
        return Side.B_SIDE
    return Side.ON_LINE


def _orientation(p: Point, q: Point, r: Point) -> float:
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _on_segment(p: Point, q: Point, r: Point) -> bool:
    """``q`` lies inside the bounding box of ``p``-``r`` (endpoints included)."""
    return (
        min(p[0], r[0]) - INTERSECTION_TOLERANCE
        <= q[0]
        <= max(p[0], r[0]) + INTERSECTION_TOLERANCE
        and min(p[1], r[1]) - INTERSECTION_TOLERANCE
        <= q[1]
        <= max(p[1], r[1]) + INTERSECTION_TOLERANCE
    )


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Do the finite segments ``p1-p2`` and ``q1-q2`` intersect?

    Touching an endpoint counts. Collinear overlap is handled for completeness
    even though the caller's stable sides are always opposite.
    """
    d1 = _orientation(q1, q2, p1)
    d2 = _orientation(q1, q2, p2)
    d3 = _orientation(p1, p2, q1)
    d4 = _orientation(p1, p2, q2)

    if ((d1 > INTERSECTION_TOLERANCE and d2 < -INTERSECTION_TOLERANCE)
            or (d1 < -INTERSECTION_TOLERANCE and d2 > INTERSECTION_TOLERANCE)) and (
        (d3 > INTERSECTION_TOLERANCE and d4 < -INTERSECTION_TOLERANCE)
        or (d3 < -INTERSECTION_TOLERANCE and d4 > INTERSECTION_TOLERANCE)
    ):
        return True

    if abs(d1) <= INTERSECTION_TOLERANCE and _on_segment(q1, p1, q2):
        return True
    if abs(d2) <= INTERSECTION_TOLERANCE and _on_segment(q1, p2, q2):
        return True
    if abs(d3) <= INTERSECTION_TOLERANCE and _on_segment(p1, q1, p2):
        return True
    if abs(d4) <= INTERSECTION_TOLERANCE and _on_segment(p1, q2, p2):
        return True
    return False


def direction_for(previous: Side, current: Side) -> CrossingDirection:
    if previous == Side.A_SIDE and current == Side.B_SIDE:
        return CrossingDirection.A_TO_B
    if previous == Side.B_SIDE and current == Side.A_SIDE:
        return CrossingDirection.B_TO_A
    raise ValueError("a crossing requires two opposite stable sides")


def direction_allowed(policy: LineDirection, actual: CrossingDirection) -> bool:
    if policy == LineDirection.BOTH:
        return True
    return policy.value == actual.value


@dataclass(slots=True)
class TrackCrossingState:
    """The minimum needed to bridge skipped and deadband observations."""

    last_stable_side: Side | None = None
    last_stable_point: Point | None = None
    last_seen_monotonic: float = 0.0


@dataclass(frozen=True, slots=True)
class CrossingObservation:
    """A crossing that passed every geometric and policy test.

    It is not yet an event: the caller must persist it durably first, then call
    :meth:`CrossingEngine.mark_persisted`.
    """

    track_id: int
    direction: CrossingDirection
    dedupe_key: tuple[str, str, str, int, str]


class CrossingEngine:
    """Per-camera-session crossing state. Confined to one worker thread."""

    def __init__(
        self,
        *,
        camera_id: str,
        line: LineRecord,
        session_id: str,
        epsilon: float = SIDE_EPSILON,
        max_histories: int = MAX_TRACK_HISTORIES,
    ):
        self._camera_id = camera_id
        self._line = line
        self._session_id = session_id
        self._epsilon = float(epsilon)
        self._max_histories = int(max_histories)
        self._histories: dict[int, TrackCrossingState] = {}
        self._fired: set[tuple[str, str, str, int, str]] = set()

    @property
    def line(self) -> LineRecord:
        return self._line

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def tracked_count(self) -> int:
        return len(self._histories)

    def fired_keys(self) -> frozenset[tuple[str, str, str, int, str]]:
        return frozenset(self._fired)

    def dedupe_key(
        self, track_id: int, direction: CrossingDirection
    ) -> tuple[str, str, str, int, str]:
        return (
            self._camera_id,
            self._line.id,
            self._session_id,
            int(track_id),
            direction.value,
        )

    def observe(
        self, track_id: int, point: Point, now_monotonic: float
    ) -> CrossingObservation | None:
        """Process one confirmed track's centroid exactly once per update."""
        a, b = self._line.a, self._line.b
        current_side = classify_side(point, a, b, self._epsilon)

        state = self._histories.get(track_id)
        if state is None:
            if len(self._histories) >= self._max_histories:
                raise TrackerStateOverflow(
                    f"track history cap of {self._max_histories} reached"
                )
            state = TrackCrossingState()
            self._histories[track_id] = state
        state.last_seen_monotonic = now_monotonic

        if current_side == Side.ON_LINE:
            # Neither fires nor moves the stable anchor.
            return None

        if state.last_stable_side is None:
            state.last_stable_side = current_side
            state.last_stable_point = point
            return None

        previous_side = state.last_stable_side
        previous_point = state.last_stable_point

        if current_side == previous_side:
            state.last_stable_point = point
            return None

        actual = direction_for(previous_side, current_side)
        intersects = (
            previous_point is not None
            and segments_intersect(previous_point, point, a, b)
        )

        # Always advance the stable anchor after evaluating the transition, so
        # an extension-only crossing cannot fire later without another genuine
        # side change.
        state.last_stable_side = current_side
        state.last_stable_point = point

        if not intersects:
            return None
        if not direction_allowed(self._line.direction, actual):
            return None

        key = self.dedupe_key(track_id, actual)
        if key in self._fired:
            return None
        return CrossingObservation(track_id=track_id, direction=actual, dedupe_key=key)

    def mark_persisted(self, observation: CrossingObservation) -> None:
        """Record the dedupe key only after a durable write proved it."""
        self._fired.add(observation.dedupe_key)

    def prune(self, active_track_ids: set[int]) -> int:
        """Drop histories and fired keys for conclusively removed tracks.

        Database uniqueness still protects rows already written, so forgetting a
        removed track's key cannot resurrect a duplicate within this session.
        """
        removed = [
            track_id for track_id in self._histories if track_id not in active_track_ids
        ]
        for track_id in removed:
            self._histories.pop(track_id, None)
        if removed:
            gone = set(removed)
            self._fired = {key for key in self._fired if key[3] not in gone}
        return len(removed)

    def reset(self, *, line: LineRecord, session_id: str) -> None:
        self._line = line
        self._session_id = session_id
        self._histories.clear()
        self._fired.clear()
