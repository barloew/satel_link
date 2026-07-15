"""Satel Link — the active pre-arm blocker check (module D).

Before arming, which zones must be clear but are violated? These are the
"blockers" — Satel's denial of arming. The check runs against the base
integration's zone binary_sensors (Satel Link holds no socket), filtered by
zone function: only plain burglary zones block. 24-hour zones are always active
and entry/exit zones may be open while you leave, so neither counts.

A zone the user linked through Satel Link participates like any physical zone:
if it is a burglary zone and currently violated, it blocks too.

Pure: violation lookup is injected, so this is testable without Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .classify import blocks_arming


@dataclass(slots=True)
class Blocker:
    """A zone that must be clear to arm, but is violated."""

    number: int
    name: str
    function: int
    function_name: str


def find_blockers(
    partition: int,
    zones,
    is_violated: Callable[[int], bool],
) -> list[Blocker]:
    """Return the violated burglary zones in `partition`.

    `zones` is the discovered zone list (MergedZone-like: number, partition,
    function, function_name, display_name). `is_violated(zone_number)` reports
    the live state, sourced from the base integration.
    """
    blockers: list[Blocker] = []
    for zone in zones:
        if zone.partition != partition:
            continue
        if not blocks_arming(zone.function):
            continue
        if is_violated(zone.number):
            blockers.append(
                Blocker(
                    number=zone.number,
                    name=zone.display_name,
                    function=zone.function,
                    function_name=zone.function_name,
                )
            )
    return blockers
