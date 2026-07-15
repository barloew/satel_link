"""Satel Link — the master panel engine (module C).

Arms and disarms several Satel partitions as one unit, through the base
integration's alarm_control_panel services (Satel Link holds no runtime socket).

Arming is sequential and verified:

    for each partition in order:
        1. run the module D blocker check; blockers -> abort + rollback
        2. call the base arm service (arm_home / arm_away) with the code
        3. wait for that partition to actually reach an armed state
        4. no confirmation -> rollback everything armed so far

Rollback disarms whatever this attempt armed and fires satel_link_arm_failed,
so the master never leaves the system half-armed while the tile claims success.

The code is never stored: Home Assistant (or, for HomeKit, the bridge's
entity_config) supplies it per command and it is passed straight through.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.const import (
    STATE_ALARM_ARMED_AWAY,
    STATE_ALARM_ARMED_HOME,
    STATE_ALARM_ARMED_NIGHT,
    STATE_ALARM_ARMED_VACATION,
    STATE_ALARM_DISARMED,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import EVENT_ARM_FAILED
from .runtime import MasterPanel

if TYPE_CHECKING:
    from . import SatelLinkConfigEntry

_LOGGER = logging.getLogger(__name__)

ALARM_DOMAIN = "alarm_control_panel"
CONFIRM_TIMEOUT = 30.0

_ARMED_STATES = {
    STATE_ALARM_ARMED_AWAY,
    STATE_ALARM_ARMED_HOME,
    STATE_ALARM_ARMED_NIGHT,
    STATE_ALARM_ARMED_VACATION,
}


class MasterEngine:
    """Orchestrates arming/disarming the underlying partitions."""

    def __init__(self, hass: HomeAssistant, entry: "SatelLinkConfigEntry") -> None:
        self._hass = hass
        self._entry = entry

    @property
    def _master(self) -> MasterPanel | None:
        return self._entry.runtime_data.master

    def partition_entities(self) -> dict[int, str]:
        """Partition number -> base alarm_control_panel entity_id, in arm order."""
        base = self._entry.runtime_data.base
        master = self._master
        if base is None or master is None:
            return {}
        by_number = base.by_number("partition")
        return {
            n: by_number[n].entity_id
            for n in master.partitions
            if n in by_number
        }

    async def async_arm(self, ha_state: str, code: str | None) -> bool:
        """Arm all partitions sequentially. Returns True on full success."""
        master = self._master
        if master is None:
            return False

        service = MasterPanel.service_for(ha_state)
        entities = self.partition_entities()
        armed: list[str] = []

        for partition, entity_id in entities.items():
            # 1. Blocker check (module D). check_arm also fires arm_blocked.
            events = self._entry.runtime_data.events
            if events is not None:
                blockers = await events.async_check_arm(partition)
                if blockers:
                    await self._rollback(armed, code)
                    self._fire_failed(partition, "blocked", blockers)
                    return False

            # 2 + 3. Arm and wait for confirmation.
            if not await self._arm_partition(entity_id, service, code):
                await self._rollback(armed, code)
                self._fire_failed(partition, "no_confirmation")
                return False
            armed.append(entity_id)

        return True

    async def async_disarm(self, code: str | None) -> None:
        """Disarm all partitions in the master."""
        for entity_id in self.partition_entities().values():
            await self._call(ALARM_DOMAIN, "alarm_disarm", entity_id, code)

    async def _arm_partition(
        self, entity_id: str, service: str, code: str | None
    ) -> bool:
        await self._call(ALARM_DOMAIN, service, entity_id, code)
        return await self._wait_for_armed(entity_id, CONFIRM_TIMEOUT)

    async def _rollback(self, armed: list[str], code: str | None) -> None:
        """Disarm everything this attempt armed, in reverse order."""
        for entity_id in reversed(armed):
            await self._call(ALARM_DOMAIN, "alarm_disarm", entity_id, code)

    async def _wait_for_armed(self, entity_id: str, timeout: float) -> bool:
        """Wait for a partition to actually reach an armed state."""
        state = self._hass.states.get(entity_id)
        if state is not None and state.state in _ARMED_STATES:
            return True

        done = asyncio.Event()

        @callback
        def _changed(event) -> None:
            new = event.data["new_state"]
            if new is not None and new.state in _ARMED_STATES:
                done.set()

        unsub = async_track_state_change_event(self._hass, [entity_id], _changed)
        try:
            await asyncio.wait_for(done.wait(), timeout)
            return True
        except TimeoutError:
            return False
        finally:
            unsub()

    async def _call(
        self, domain: str, service: str, entity_id: str, code: str | None
    ) -> None:
        data = {"entity_id": entity_id}
        if code:
            data["code"] = code
        await self._hass.services.async_call(domain, service, data, blocking=True)

    @callback
    def _fire_failed(self, partition: int, reason: str, blockers=None) -> None:
        payload = {"partition": partition, "reason": reason}
        if blockers:
            payload["zones"] = blockers
        self._hass.bus.async_fire(EVENT_ARM_FAILED, payload)
        _LOGGER.warning("Master arm failed at partition %d: %s", partition, reason)
