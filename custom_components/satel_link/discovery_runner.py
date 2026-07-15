"""Satel Link — running discovery against a live Satel Integra Panel.

The ETHM accepts only one client on the integration port. If a base integration
is connected it owns that socket, so the one-off scan must:

    1. unload the base integration        (free the socket)
    2. connect, scan, disconnect          (protocol.discover)
    3. reload the base integration        (restore normal operation)

The interruption is brief and one-off; the result is merged with the existing
HA entities and cached. Kept out of config_flow.py so the flow stays importable
without the Satel Integra Panel library.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CODE, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import DEFAULT_PORT
from .model import SystemModel, build_model
from .protocol import discover
from .registry import ExistingConfig
from .registry_ha import find_base_entry, read_existing

_LOGGER = logging.getLogger(__name__)

CONF_BASE_ENTRY = "base_entry_id"

# The ETHM integration port serves one client at a time. After the base
# integration is unloaded (or the discovery socket is closed), the panel needs
# a moment to release its single slot before the next client connects. Too
# short and discovery connects while the panel still holds the base's slot,
# which the panel silently ignores — producing an empty scan.
SETTLE_SECONDS = 5


async def run_discovery(hass: HomeAssistant, entry: ConfigEntry) -> SystemModel:
    """Unload the base integration, scan the Satel Integra Panel, reload, and merge."""
    data = {**entry.data, **entry.options}
    host: str | None = data.get(CONF_HOST)
    port: int = data.get(CONF_PORT, DEFAULT_PORT)
    code: str = data.get(CONF_CODE) or ""

    if not host:
        raise ValueError("No host configured for discovery")

    existing: ExistingConfig | None = None
    base_entry: ConfigEntry | None = None
    if base_entry_id := data.get(CONF_BASE_ENTRY):
        base_entry = hass.config_entries.async_get_entry(base_entry_id)
    if base_entry is None:
        base_entry = find_base_entry(hass)
    if base_entry is not None:
        existing = read_existing(hass, base_entry)

    base_was_loaded = False
    try:
        # 1. Free the socket.
        if base_entry is not None and base_entry.state.recoverable:
            _LOGGER.info("Unloading %s for discovery", base_entry.domain)
            base_was_loaded = await hass.config_entries.async_unload(
                base_entry.entry_id
            )
            if base_was_loaded:
                # Let the panel release the base integration's slot before we
                # connect as the (now sole) client.
                _LOGGER.debug("Waiting %ss for the panel to free its slot", SETTLE_SECONDS)
                await asyncio.sleep(SETTLE_SECONDS)

        # 2. Scan.
        result = await discover(host, port, integration_key=code)

    finally:
        # 3. Always restore the base integration, even if the scan failed.
        if base_entry is not None and base_was_loaded:
            # Let the discovery socket fully release before the base reconnects,
            # so the panel does not report "busy".
            await asyncio.sleep(SETTLE_SECONDS)
            _LOGGER.info("Reloading %s after discovery", base_entry.domain)
            await hass.config_entries.async_setup(base_entry.entry_id)

    model = build_model(result, existing)
    _LOGGER.info("Discovery model built: %s", model_summary(model))
    return model


def model_summary(model: SystemModel) -> dict[str, int]:
    return {
        "zones": len(model.zones),
        "outputs": len(model.outputs),
        "linkable_outputs": len(model.linkable_outputs()),
        "controllable_outputs": len(model.controllable_outputs()),
        "covers": len(model.covers),
        "zones_without_partition": len(model.zones_without_partition()),
    }
