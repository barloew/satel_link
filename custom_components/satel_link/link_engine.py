"""Satel Link — the forwarding engine (module B).

A link is: HA sensor -> switchable output -> zone that follows it. Since Satel
Link holds no runtime socket, "driving the output" means calling the base
integration's switch service for that output; the Satel zone then follows.

Two things decide *when* a sensor state reaches the Satel Integra Panel:

  Status forwarding — the gate:
    ALWAYS       continuously monitored (smoke/CO/gas/water) — always forward
    ARMED_ONLY   only while the zone's partition is armed
    ENTRY_DELAY  only while armed, and a violation is held back for
                 `entry_delay_s` before it is forwarded, so disarming in time
                 prevents the alarm

  Short-peak suppression — the hold:
    `min_on_s` keeps the output on at least that long after the source clears,
    so the Satel Integra Panel reliably registers a brief pulse (e.g. a PIR
    that flickers on and off within a second).

Entry delay defers turning ON; the hold defers turning OFF. Both can apply to
one link, so the per-link timing lives in a small _LinkState with its own
timers rather than in scattered callbacks.

Polarity: if the output's polarity is inverted, a violated zone corresponds to
the output being OFF. The link's `invert` flag flips the driven state so the
zone still reads correctly. (Polarity is not readable over the protocol; see
verify.py.)
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable

from homeassistant.const import (
    STATE_ALARM_ARMED_AWAY,
    STATE_ALARM_ARMED_HOME,
    STATE_ALARM_ARMED_NIGHT,
    STATE_ALARM_ARMED_VACATION,
    STATE_ON,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import StatusForwarding
from .runtime import Link

if TYPE_CHECKING:
    from . import SatelLinkConfigEntry

_LOGGER = logging.getLogger(__name__)

_ARMED_STATES = {
    STATE_ALARM_ARMED_AWAY,
    STATE_ALARM_ARMED_HOME,
    STATE_ALARM_ARMED_NIGHT,
    STATE_ALARM_ARMED_VACATION,
}


class _LinkState:
    """One link plus its resolved base entities and timing state.

    `committed` is the logical "violated" state we have currently forwarded to
    the Satel Integra Panel (before the invert flip). Timers defer the
    transitions: `_entry_timer` delays committing a violation, `_hold_timer`
    delays clearing one.
    """

    __slots__ = (
        "link",
        "output_switch",
        "partition_entity",
        "committed",
        "_on_since",
        "_entry_timer",
        "_hold_timer",
    )

    def __init__(
        self, link: Link, output_switch: str | None, partition_entity: str | None
    ) -> None:
        self.link = link
        self.output_switch = output_switch
        self.partition_entity = partition_entity
        self.committed: bool = False
        self._on_since: float = 0.0
        self._entry_timer: Callable[[], None] | None = None
        self._hold_timer: Callable[[], None] | None = None

    def cancel_timers(self) -> None:
        for attr in ("_entry_timer", "_hold_timer"):
            timer = getattr(self, attr)
            if timer is not None:
                timer()
                setattr(self, attr, None)

    def clear_entry_timer(self) -> None:
        if self._entry_timer is not None:
            self._entry_timer()
            self._entry_timer = None

    def clear_hold_timer(self) -> None:
        if self._hold_timer is not None:
            self._hold_timer()
            self._hold_timer = None

    def mark_committed(self, violated: bool) -> None:
        self.committed = violated
        if violated:
            self._on_since = time.monotonic()

    def hold_remaining(self) -> float:
        """Seconds the output must still stay on to satisfy min_on_s."""
        if self.link.min_on_s <= 0:
            return 0.0
        return max(0.0, self._on_since + self.link.min_on_s - time.monotonic())


class LinkEngine:
    """Watches source sensors and mirrors them onto Satel outputs."""

    def __init__(self, hass: HomeAssistant, entry: "SatelLinkConfigEntry") -> None:
        self._hass = hass
        self._entry = entry
        self._states: list[_LinkState] = []
        self._unsubscribe: list[Callable[[], None]] = []

    async def async_start(self) -> None:
        runtime = self._entry.runtime_data
        if runtime.base is None:
            _LOGGER.warning("Link engine idle: no base integration resolved")
            return

        outputs = runtime.base.by_number("output")
        partitions = runtime.base.by_number("partition")
        zones = {z.number: z for z in runtime.model.zones} if runtime.model else {}

        watched: set[str] = set()
        for link in runtime.links:
            output = outputs.get(link.output_number)
            zone = zones.get(link.zone_number)
            partition = zone.partition if zone else None
            part_entity = (
                partitions[partition].entity_id if partition in partitions else None
            )
            state = _LinkState(
                link=link,
                output_switch=output.entity_id if output else None,
                partition_entity=part_entity,
            )
            self._states.append(state)

            if state.output_switch is None:
                _LOGGER.warning(
                    "Link on output %d has no switch in the base integration; "
                    "expose it there first",
                    link.output_number,
                )
                continue

            watched.add(link.source_entity_id)
            if part_entity:
                watched.add(part_entity)

        if watched:
            self._unsubscribe.append(
                async_track_state_change_event(
                    self._hass, list(watched), self._handle_change
                )
            )
        # Sync once to reality; startup skips the entry delay (it is not a fresh
        # entry event) but honours the gate.
        for state in self._states:
            if state.output_switch is not None:
                self._evaluate(state, initial=True)

    async def async_stop(self) -> None:
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()
        for state in self._states:
            state.cancel_timers()

    @callback
    def _handle_change(self, event: Event[EventStateChangedData]) -> None:
        for state in self._states:
            if state.output_switch is not None:
                self._evaluate(state)

    @callback
    def _evaluate(self, state: _LinkState, *, initial: bool = False) -> None:
        """Bring the committed (logical) state toward what the source wants,
        applying the entry delay on the way up and the hold on the way down."""
        if initial:
            # At startup drive the output to match reality, even if the logical
            # state already "matches" — the physical output (with invert) has
            # not been set yet. No entry delay, no hold.
            self._commit(state, self._wants(state))
            return

        want_violated = self._wants(state)

        if want_violated == state.committed:
            state.clear_entry_timer()  # target reached; drop any pending rise
            return

        if want_violated:
            self._rise(state, initial=initial)
        else:
            self._fall(state)

    def _wants(self, state: _LinkState) -> bool:
        """True when the source is on and the gate is open."""
        source = self._hass.states.get(state.link.source_entity_id)
        source_on = source is not None and source.state == STATE_ON
        return source_on and self._gate_open(state)

    def _rise(self, state: _LinkState, *, initial: bool) -> None:
        """Source wants a violation. Commit now, unless an entry delay applies."""
        link = state.link
        state.clear_hold_timer()
        if not initial and link.forwarding is StatusForwarding.ENTRY_DELAY and (
            link.entry_delay_s > 0
        ):
            if state._entry_timer is None:
                state._entry_timer = async_call_later(
                    self._hass, link.entry_delay_s, self._entry_elapsed(state)
                )
            return
        self._commit(state, True)

    def _fall(self, state: _LinkState) -> None:
        """Source cleared. Cancel a pending rise; honour the minimum on-time."""
        state.clear_entry_timer()
        remaining = state.hold_remaining()
        if remaining > 0:
            if state._hold_timer is None:
                state._hold_timer = async_call_later(
                    self._hass, remaining, self._hold_elapsed(state)
                )
            return
        self._commit(state, False)

    def _entry_elapsed(self, state: _LinkState) -> Callable:
        @callback
        def _fire(_now) -> None:
            state._entry_timer = None
            # The delay is over: forward the violation only if it still stands.
            if self._wants(state) and not state.committed:
                self._commit(state, True)

        return _fire

    def _hold_elapsed(self, state: _LinkState) -> Callable:
        @callback
        def _fire(_now) -> None:
            state._hold_timer = None
            self._evaluate(state)

        return _fire

    def _commit(self, state: _LinkState, violated: bool) -> None:
        state.mark_committed(violated)
        output_active = violated ^ state.link.invert
        current = self._hass.states.get(state.output_switch)
        if current is not None and (current.state == STATE_ON) == output_active:
            return
        self._hass.async_create_task(
            self._hass.services.async_call(
                "switch",
                "turn_on" if output_active else "turn_off",
                {"entity_id": state.output_switch},
                blocking=False,
            )
        )

    def _gate_open(self, state: _LinkState) -> bool:
        forwarding = state.link.forwarding
        if forwarding is StatusForwarding.ALWAYS:
            return True
        if state.partition_entity is None:
            return False
        panel = self._hass.states.get(state.partition_entity)
        return panel is not None and panel.state in _ARMED_STATES
