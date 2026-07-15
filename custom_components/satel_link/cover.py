"""Satel Link — roller-shutter covers (part of module B2).

A Satel roller shutter is a pair of outputs (105 up + 106 down). The base
integration exposes them as two switches; Satel Link bundles them into one
cover with open / close / stop. Driving happens through the base integration's
switch service, because Satel Link holds no runtime socket.

The Satel Integra Panel does not report shutter position, so the cover's closed
state is unknown (None) — it is a controllable cover, not a position-reporting
one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, HaPlatform
from .runtime import Control

if TYPE_CHECKING:
    from . import SatelLinkConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: "SatelLinkConfigEntry",
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a cover per roller-shutter control."""
    runtime = entry.runtime_data
    outputs = runtime.base.by_number("output") if runtime.base else {}
    names = {c.up.number: c.name for c in (runtime.model.covers if runtime.model else [])}

    entities: list[SatelLinkCover] = []
    for control in runtime.controls:
        if control.platform != HaPlatform.COVER.value:
            continue
        up = outputs.get(control.output_number)
        down = outputs.get(control.down_number) if control.down_number else None
        entities.append(
            SatelLinkCover(
                entry_id=entry.entry_id,
                up_number=control.output_number,
                up_switch=up.entity_id if up else None,
                down_switch=down.entity_id if down else None,
                name=names.get(control.output_number) or f"Cover {control.output_number}",
            )
        )
    async_add_entities(entities)


class SatelLinkCover(CoverEntity):
    """A roller shutter driven via the base integration's up/down switches."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(
        self,
        *,
        entry_id: str,
        up_number: int,
        up_switch: str | None,
        down_switch: str | None,
        name: str,
    ) -> None:
        self._up_switch = up_switch
        self._down_switch = down_switch
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_cover_{up_number}"

    @property
    def available(self) -> bool:
        """A cover needs both halves resolved in the base integration."""
        return self._up_switch is not None and self._down_switch is not None

    @property
    def is_closed(self) -> bool | None:
        """Position is not reported by the Satel Integra Panel."""
        return None

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._drive(up=True)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._drive(down=True)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._drive()

    async def _drive(self, *, up: bool = False, down: bool = False) -> None:
        """Set the two output switches. Never drive both at once."""
        await self._switch(self._down_switch, down and not up)
        await self._switch(self._up_switch, up and not down)

    async def _switch(self, entity_id: str | None, on: bool) -> None:
        if entity_id is None:
            return
        state = self.hass.states.get(entity_id)
        if state is not None and (state.state == STATE_ON) == on:
            return
        await self.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {"entity_id": entity_id},
            blocking=False,
        )
