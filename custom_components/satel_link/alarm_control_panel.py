"""Satel Link — the master alarm panel entity (module C).

One tile that arms several partitions as a unit. HomeKit couples one accessory
to one alarm panel, so a master lets HomeKit (and the normal HA UI) operate
several partitions at once.

Shown state comes from the last command the user gave (there is no way to read
back whether "night" or "away" was meant — both arm to Satel mode 0). Whether
the system is *actually* armed is cross-checked against the underlying
partitions: if every partition is disarmed (e.g. from a keypad), the tile falls
back to disarmed so it never lies about being armed.

The code is supplied per command by Home Assistant, or for HomeKit by the
bridge's entity_config; it is passed through, never stored.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .master_engine import MasterEngine

if TYPE_CHECKING:
    from . import SatelLinkConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: "SatelLinkConfigEntry",
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the master panel, if one is configured."""
    if entry.runtime_data.master is None:
        return
    async_add_entities([SatelLinkMasterPanel(entry)])


class SatelLinkMasterPanel(AlarmControlPanelEntity):
    """Arms several partitions as one, via the base integration."""

    _attr_has_entity_name = True
    _attr_code_arm_required = True
    _attr_code_format = CodeFormat.NUMBER
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(self, entry: "SatelLinkConfigEntry") -> None:
        self._entry = entry
        self._engine = MasterEngine(entry.hass if hasattr(entry, "hass") else None, entry)
        master = entry.runtime_data.master
        self._attr_name = master.name if master else "Alarm master"
        self._attr_unique_id = f"{entry.entry_id}_master"
        self._commanded: AlarmControlPanelState = AlarmControlPanelState.DISARMED

    async def async_added_to_hass(self) -> None:
        # Rebind the engine to the live hass and follow the partitions so the
        # tile falls back to disarmed if they are all disarmed externally.
        self._engine = MasterEngine(self.hass, self._entry)
        entities = list(self._engine.partition_entities().values())
        if entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, entities, self._partitions_changed
                )
            )

    @property
    def alarm_state(self) -> AlarmControlPanelState:
        """Last commanded state, unless every partition is disarmed."""
        if self._commanded is AlarmControlPanelState.DISARMED:
            return AlarmControlPanelState.DISARMED
        if self._all_partitions_disarmed():
            return AlarmControlPanelState.DISARMED
        return self._commanded

    def _all_partitions_disarmed(self) -> bool:
        entities = self._engine.partition_entities().values()
        if not entities:
            return False
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state != AlarmControlPanelState.DISARMED:
                return False
        return True

    @callback
    def _partitions_changed(self, event) -> None:
        self.async_write_ha_state()

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._engine.async_disarm(code)
        self._commanded = AlarmControlPanelState.DISARMED
        self.async_write_ha_state()

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._arm(AlarmControlPanelState.ARMED_AWAY, "armed_away", code)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._arm(AlarmControlPanelState.ARMED_HOME, "armed_home", code)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._arm(AlarmControlPanelState.ARMED_NIGHT, "armed_night", code)

    async def _arm(
        self, target: AlarmControlPanelState, ha_state: str, code: str | None
    ) -> None:
        self._commanded = AlarmControlPanelState.ARMING
        self.async_write_ha_state()
        ok = await self._engine.async_arm(ha_state, code)
        # On failure the engine has already rolled back and fired the event.
        self._commanded = target if ok else AlarmControlPanelState.DISARMED
        self.async_write_ha_state()
