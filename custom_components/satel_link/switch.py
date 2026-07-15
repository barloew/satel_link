"""Satel Link — switch entities for controllable outputs (part of module B2).

A switch-type control (MONO/BI or a remote switch) is exposed as a Home
Assistant switch. Since Satel Link holds no runtime socket, on/off is driven
through the base integration's own switch for that output, and the state is
mirrored from it. The value Satel Link adds is a discovery-managed entity with
a proper name and area — and, for base integrations that only expose outputs via
YAML, a first-class entity at all.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import HaPlatform
from .runtime import Control

if TYPE_CHECKING:
    from . import SatelLinkConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: "SatelLinkConfigEntry",
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a switch per switch-type control."""
    runtime = entry.runtime_data
    outputs = runtime.base.by_number("output") if runtime.base else {}
    model_outputs = {o.number: o for o in (runtime.model.outputs if runtime.model else [])}

    entities: list[SatelLinkSwitch] = []
    for control in runtime.controls:
        if control.platform != HaPlatform.SWITCH.value:
            continue
        base = outputs.get(control.output_number)
        model_out = model_outputs.get(control.output_number)
        entities.append(
            SatelLinkSwitch(
                entry_id=entry.entry_id,
                number=control.output_number,
                base_switch=base.entity_id if base else None,
                name=(model_out.display_name if model_out else None)
                or f"Output {control.output_number}",
            )
        )
    async_add_entities(entities)


class SatelLinkSwitch(SwitchEntity):
    """A controllable Satel output, driven via the base integration's switch."""

    _attr_has_entity_name = True

    def __init__(
        self, *, entry_id: str, number: int, base_switch: str | None, name: str
    ) -> None:
        self._base_switch = base_switch
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_switch_{number}"

    @property
    def available(self) -> bool:
        if self._base_switch is None:
            return False
        state = self.hass.states.get(self._base_switch)
        return state is not None and state.state != "unavailable"

    @property
    def is_on(self) -> bool | None:
        state = self.hass.states.get(self._base_switch) if self._base_switch else None
        return None if state is None else state.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._call("turn_on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._call("turn_off")

    async def _call(self, service: str) -> None:
        if self._base_switch is None:
            return
        await self.hass.services.async_call(
            "switch", service, {"entity_id": self._base_switch}, blocking=False
        )

    async def async_added_to_hass(self) -> None:
        """Mirror the base switch's state changes onto this entity."""
        if self._base_switch is None:
            return

        @callback
        def _base_changed(event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._base_switch], _base_changed
            )
        )
