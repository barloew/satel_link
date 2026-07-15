"""Satel Link — a rolling history of zone violations (for breach snapshots).

The breach snapshot answers "which zones were breached when the alarm went
off?" — but at the moment a partition turns `triggered`, the zone that caused it
may already read clear (a walk-through PIR), and a chain reaction may have
followed. So instead of sampling the instant of the trigger, Satel Link keeps a
short rolling history and, on trigger, reports every zone violated in the last
few seconds.

Since Satel Link holds no runtime socket, this history is built from the base
integration's zone binary_sensors — it is a reconstruction on the HA side, not
a read of Satel's own alarm memory. `max_window` caps how far back anything is
kept; individual snapshots ask for their own (possibly shorter) window.

This module is pure: it takes timestamps in, so it can be tested without Home
Assistant.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class ZoneHistory:
    """Records when zones became violated and answers window queries."""

    max_window: float = 30.0
    _events: deque[tuple[float, int]] = field(default_factory=deque)
    _violated: set[int] = field(default_factory=set)

    def record(self, zone: int, violated: bool, now: float) -> None:
        """Note a zone's transition. Only rising edges enter the history; the
        currently-violated set tracks what is open right now."""
        if violated:
            if zone not in self._violated:
                self._events.append((now, zone))
                self._violated.add(zone)
        else:
            self._violated.discard(zone)
        self._prune(now)

    def snapshot(self, window: float, now: float) -> set[int]:
        """Zones violated at any point within the last `window` seconds, plus
        anything still violated now. `window` is clamped to `max_window`."""
        window = min(window, self.max_window)
        cutoff = now - window
        breached = {zone for ts, zone in self._events if ts >= cutoff}
        breached |= self._violated
        return breached

    def _prune(self, now: float) -> None:
        cutoff = now - self.max_window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
