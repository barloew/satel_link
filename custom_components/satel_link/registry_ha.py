"""Satel Link — Home Assistant side of reading the existing base integration.

Kept separate from registry.py so the data model and the merge logic stay
importable (and testable) without Home Assistant installed.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CODE, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DEFAULT_PORT
from .registry import (
    BASE_DOMAINS,
    _PLATFORM_TO_KIND,
    ExistingConfig,
    ExistingEntity,
    _parse_number,
)

_LOGGER = logging.getLogger(__name__)


def find_base_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the config entry of a supported base integration, if present."""
    for domain in BASE_DOMAINS:
        entries = hass.config_entries.async_entries(domain)
        if entries:
            if len(entries) > 1:
                _LOGGER.info(
                    "Multiple %s entries; adopting the first (%s)",
                    domain,
                    entries[0].entry_id,
                )
            return entries[0]
    return None


def read_existing(hass: HomeAssistant, entry: ConfigEntry) -> ExistingConfig:
    """Harvest partitions/zones/outputs and connection details from HA."""
    data = {**entry.data, **entry.options}
    config = ExistingConfig(
        base_domain=entry.domain,
        entry_id=entry.entry_id,
        host=data.get(CONF_HOST),
        port=data.get(CONF_PORT, DEFAULT_PORT),
        code=data.get(CONF_CODE),
    )

    ent_reg = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        kind = _PLATFORM_TO_KIND.get(entity.domain)
        if kind is None:
            continue
        config.entities.append(
            ExistingEntity(
                kind=kind,
                number=_parse_number(entity.unique_id),
                entity_id=entity.entity_id,
                name=entity.name or entity.original_name,
                device_class=entity.device_class or entity.original_device_class,
                area_id=_area_of(hass, ent_reg, entity),
                unique_id=entity.unique_id,
            )
        )

    _LOGGER.debug(
        "Read %d existing entities from %s (%d zones, %d outputs, %d partitions)",
        len(config.entities),
        entry.domain,
        len(config.zones),
        len(config.outputs),
        len(config.partitions),
    )
    return config


def _area_of(
    hass: HomeAssistant, ent_reg: er.EntityRegistry, entity: er.RegistryEntry
) -> str | None:
    """Area is on the entity, or inherited from its device."""
    if entity.area_id:
        return entity.area_id
    if entity.device_id:
        dev_reg = dr.async_get(hass)
        if (device := dev_reg.async_get(entity.device_id)) is not None:
            return device.area_id
    return None
