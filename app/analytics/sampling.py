"""Monotonic inference cadence with no catch-up and no queue."""

from __future__ import annotations

__all__ = ["FrameSampler"]


class FrameSampler:
    """Decides whether a frame becomes an analytics opportunity.

    The deadline is always set to ``now + period``, never advanced from the old
    deadline. A worker that fell behind therefore resumes at the requested rate
    instead of firing a catch-up burst.
    """

    __slots__ = ("_next_due",)

    def __init__(self) -> None:
        self._next_due: float | None = None

    @property
    def next_due(self) -> float | None:
        return self._next_due

    def should_process(self, now: float, fps: float) -> bool:
        if fps <= 0.0:
            raise ValueError("inference fps must be positive")
        period = 1.0 / fps
        if self._next_due is None or now >= self._next_due:
            self._next_due = now + period
            return True
        return False

    def reset(self) -> None:
        """Called on every configuration change and every new attempt."""
        self._next_due = None
