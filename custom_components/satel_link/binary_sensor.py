"""Satel Link — binary sensors: read-only outputs and link status.

Two kinds:

  * Read-only output sensors — panel-driven outputs (fire, siren, trouble,
    tamper) that Home Assistant can only observe. The base integration may
    expose these as switches, which is semantically wrong (you cannot switch a
    panel-driven output); Satel Link re-presents them as binary_sensors with the
    right device class. State is mirrored from the base switch.

  * Link status sensors — one per configured link, showing whether the link is
    currently forwarding a violation to the Satel Integra Panel. That is the
    driven output state corrected for polarity (invert), so it reads as the
    logical "violated" the panel sees. Diagnostic.

Both read state through the base integration — Satel Link holds no runtime
socket.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .classify import output_device_class
from .const import HaPlatform
from .runtime import Control, Link

if TYPE_CHECKING:
    from . import SatelLinkConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: "SatelLinkConfigEntry",
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create read-only output sensors (from controls) and link status sensors."""
    runtime = entry.runtime_data
    outputs = runtime.base.by_number("output") if runtime.base else {}
    model_outputs = {o.number: o for o in (runtime.model.outputs if runtime.model else [])}

    entities: list[BinarySensorEntity] = []

    # Read-only output observation (Control with the binary_sensor platform).
    for control in runtime.controls:
        if control.platform != HaPlatform.BINARY_SENSOR.value:
            continue
        base = outputs.get(control.output_number)
        model_out = model_outputs.get(control.output_number)
        entities.append(
            SatelLinkOutputSensor(
                entry_id=entry.entry_id,
                number=control.output_number,
                base_switch=base.entity_id if base else None,
                name=(model_out.display_name if model_out else None)
                or f"Output {control.output_number}",
                function=model_out.function if model_out else 0,
            )
        )

    # Link status, one per configured link.
    for link in runtime.links:
        base = outputs.get(link.output_number)
        entities.append(
            SatelLinkLinkSensor(
                entry_id=entry.entry_id,
                link=link,
                output_switch=base.entity_id if base else None,
            )
        )

    async_add_entities(entities)


class _BaseMirrorSensor(BinarySensorEntity):
    """Shared: mirror a base entity's state and follow its changes."""

    _attr_has_entity_name = True

    def __init__(self, *, watched: str | None) -> None:
        self._watched = watched

    @property
    def available(self) -> bool:
        if self._watched is None:
            return False
        state = self.hass.states.get(self._watched)
        return state is not None and state.state != STATE_UNAVAILABLE

    async def async_added_to_hass(self) -> None:
        if self._watched is None:
            return

        @callback
        def _changed(event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._watched], _changed)
        )


class SatelLinkOutputSensor(_BaseMirrorSensor):
    """A read-only Satel output, mirrored from the base integration's switch."""

    def __init__(
        self,
        *,
        entry_id: str,
        number: int,
        base_switch: str | None,
        name: str,
        function: int,
    ) -> None:
        super().__init__(watched=base_switch)
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_output_{number}"
        if (device_class := output_device_class(function)) is not None:
            self._attr_device_class = BinarySensorDeviceClass(device_class)

    @property
    def is_on(self) -> bool | None:
        if self._watched is None:
            return None
        state = self.hass.states.get(self._watched)
        return None if state is None else state.state == STATE_ON


class SatelLinkLinkSensor(_BaseMirrorSensor):
    """Whether a link is currently forwarding a violation to the panel."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, *, entry_id: str, link: Link, output_switch: str | None
    ) -> None:
        super().__init__(watched=output_switch)
        self._invert = link.invert
        self._attr_name = f"Link {link.output_number}"
        self._attr_unique_id = f"{entry_id}_link_{link.output_number}"

    @property
    def is_on(self) -> bool | None:
        """Forwarding a violation = base output active, corrected for polarity."""
        if self._watched is None:
            return None
        state = self.hass.states.get(self._watched)
        if state is None:
            return None
        output_active = state.state == STATE_ON
        return output_active ^ self._invert
