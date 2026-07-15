"""Satel Link — a companion for the Satel Integra Panel.

Satel Link links Home Assistant sensors into the Satel Integra Panel as real,
armed Satel zones, and exposes Satel outputs (switches, roller-shutter covers)
in Home Assistant.

Runtime model: the ETHM accepts one client on the integration port, and the
base integration (satel_integra / ha_satel_integra_ext) owns it. Satel Link
therefore holds no socket of its own at runtime — it drives the base
integration's output switches and reads arm state from its alarm_control_panel
entities. The only direct connection is the one-off discovery scan, which runs
with the base integration briefly unloaded.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.helpers import config_validation as cv

from .const import ATTR_PARTITION, DOMAIN, SERVICE_CHECK_ARM
from .events_engine import EventEngine
from .link_engine import LinkEngine
from .registry_ha import find_base_entry, read_existing
from .runtime import (
    OPT_MODEL,
    RuntimeData,
    load_controls,
    load_links,
    load_master,
    load_model,
    load_settings,
)

_LOGGER = logging.getLogger(__name__)

CONF_BASE_ENTRY = "base_entry_id"

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.SWITCH,
]

type SatelLinkConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SatelLinkConfigEntry) -> bool:
    """Set up Satel Link from a config entry."""
    options = dict(entry.options)

    runtime = RuntimeData(
        model=load_model(options[OPT_MODEL]) if options.get(OPT_MODEL) else None,
        links=load_links(options),
        controls=load_controls(options),
        settings=load_settings(options),
        master=load_master(options),
    )

    # Resolve the base integration's entities so we can drive its output
    # switches and read its arm state. Without it, links cannot forward.
    base_entry = None
    if base_id := {**entry.data, **options}.get(CONF_BASE_ENTRY):
        base_entry = hass.config_entries.async_get_entry(base_id)
    if base_entry is None:
        base_entry = find_base_entry(hass)
    if base_entry is not None:
        runtime.base = read_existing(hass, base_entry)
    else:
        _LOGGER.warning(
            "No Satel base integration found; links cannot forward until one is set up"
        )

    entry.runtime_data = runtime

    # Start the forwarding engine (links) and the event engine (blockers +
    # breach snapshots). Controls are entity platforms.
    runtime.engine = LinkEngine(hass, entry)
    await runtime.engine.async_start()
    runtime.events = EventEngine(hass, entry)
    await runtime.events.async_start()

    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_change))
    return True


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain-wide pre-arm check service, once."""
    if hass.services.has_service(DOMAIN, SERVICE_CHECK_ARM):
        return

    async def _check_arm(call: ServiceCall) -> ServiceResponse:
        partition = call.data[ATTR_PARTITION]
        for entry in hass.config_entries.async_entries(DOMAIN):
            runtime = getattr(entry, "runtime_data", None)
            if runtime and runtime.events:
                zones = await runtime.events.async_check_arm(partition)
                return {"blocked": bool(zones), "zones": zones}
        return {"blocked": False, "zones": []}

    hass.services.async_register(
        DOMAIN,
        SERVICE_CHECK_ARM,
        _check_arm,
        schema=vol.Schema({vol.Required(ATTR_PARTITION): cv.positive_int}),
        supports_response=SupportsResponse.ONLY,
    )


async def async_unload_entry(hass: HomeAssistant, entry: SatelLinkConfigEntry) -> bool:
    """Tear down Satel Link."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and (runtime := entry.runtime_data):
        if runtime.engine:
            await runtime.engine.async_stop()
        if runtime.events:
            await runtime.events.async_stop()
    return unloaded


async def _async_reload_on_change(hass: HomeAssistant, entry: SatelLinkConfigEntry) -> None:
    """Reload when links/controls change in the options flow."""
    await hass.config_entries.async_reload(entry.entry_id)
