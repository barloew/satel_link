"""Satel Link — the event engine (module D): blockers and breach snapshots.

Runs alongside the link engine. It:

  * feeds the rolling zone history from the base integration's zone
    binary_sensors;
  * watches the partition alarm_control_panel entities and, when one turns
    `triggered`, fires `satel_link_breach` with the zones breached in the
    lookback window;
  * answers the active pre-arm check (a service) with the zones blocking an
    arm, and fires `satel_link_arm_blocked`.

All state comes from the base integration's entities — Satel Link holds no
runtime socket. Events carry function names so automations can react
meaningfully ("a perimeter zone blocked arming", not "zone 12").
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable

from homeassistant.const import STATE_ON
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .blockers import find_blockers
from .const import EVENT_ARM_BLOCKED, EVENT_BREACH
from .history import ZoneHistory

if TYPE_CHECKING:
    from . import SatelLinkConfigEntry

_LOGGER = logging.getLogger(__name__)

STATE_TRIGGERED = "triggered"


class EventEngine:
    """Blocker checks and breach snapshots, driven by base-integration state."""

    def __init__(self, hass: HomeAssistant, entry: "SatelLinkConfigEntry") -> None:
        self._hass = hass
        self._entry = entry
        self._history = ZoneHistory()
        self._unsubscribe: list[Callable[[], None]] = []
        # entity_id -> zone number, and partition entity_id -> partition number
        self._zone_of: dict[str, int] = {}
        self._partition_of: dict[str, int] = {}
        self._partition_state: dict[str, str] = {}

    async def async_start(self) -> None:
        runtime = self._entry.runtime_data
        if runtime.base is None:
            _LOGGER.warning("Event engine idle: no base integration resolved")
            return

        # Size the history to the largest lookback anyone might ask for.
        settings = runtime.settings
        self._history.max_window = max(
            [settings.breach_lookback_s, *settings.partition_lookback.values(), 5.0]
        )

        zones = runtime.base.by_number("zone")
        partitions = runtime.base.by_number("partition")
        self._zone_of = {e.entity_id: n for n, e in zones.items()}
        self._partition_of = {e.entity_id: n for n, e in partitions.items()}

        # Seed the current partition states so we only react to *transitions*
        # into triggered.
        for entity_id in self._partition_of:
            if (state := self._hass.states.get(entity_id)) is not None:
                self._partition_state[entity_id] = state.state

        watched = list(self._zone_of) + list(self._partition_of)
        if watched:
            self._unsubscribe.append(
                async_track_state_change_event(
                    self._hass, watched, self._handle_change
                )
            )

    async def async_stop(self) -> None:
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()

    @callback
    def _handle_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new = event.data["new_state"]
        if new is None:
            return
        if entity_id in self._zone_of:
            self._history.record(
                self._zone_of[entity_id], new.state == STATE_ON, time.monotonic()
            )
        elif entity_id in self._partition_of:
            self._partition_changed(entity_id, new.state)

    @callback
    def _partition_changed(self, entity_id: str, new_state: str) -> None:
        previous = self._partition_state.get(entity_id)
        self._partition_state[entity_id] = new_state
        if new_state == STATE_TRIGGERED and previous != STATE_TRIGGERED:
            self._fire_breach(self._partition_of[entity_id])

    @callback
    def _fire_breach(self, partition: int) -> None:
        runtime = self._entry.runtime_data
        window = runtime.settings.window_for(partition)
        breached = self._history.snapshot(window, time.monotonic())

        zones = {z.number: z for z in runtime.model.zones} if runtime.model else {}
        payload_zones = [
            {
                "number": n,
                "name": zones[n].display_name if n in zones else f"Zone {n}",
                "function": zones[n].function_name if n in zones else None,
            }
            for n in sorted(breached)
            if zones.get(n) is None or zones[n].partition in (partition, None)
        ]
        self._hass.bus.async_fire(
            EVENT_BREACH,
            {"partition": partition, "window_s": window, "zones": payload_zones},
        )
        _LOGGER.info(
            "Breach in partition %d: %d zone(s) in the last %.1fs",
            partition,
            len(payload_zones),
            window,
        )

    async def async_check_arm(self, partition: int) -> list[dict]:
        """Active pre-arm check: return the zones blocking an arm, and fire
        `satel_link_arm_blocked` if there are any."""
        runtime = self._entry.runtime_data
        if runtime.model is None or runtime.base is None:
            return []

        zone_entities = runtime.base.by_number("zone")

        def is_violated(number: int) -> bool:
            entity = zone_entities.get(number)
            if entity is None:
                return False
            state = self._hass.states.get(entity.entity_id)
            return state is not None and state.state == STATE_ON

        blockers = find_blockers(partition, runtime.model.zones, is_violated)
        payload = [
            {"number": b.number, "name": b.name, "function": b.function_name}
            for b in blockers
        ]
        if payload:
            self._hass.bus.async_fire(
                EVENT_ARM_BLOCKED, {"partition": partition, "zones": payload}
            )
        return payload
