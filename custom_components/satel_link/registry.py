"""Satel Link — reading what Home Assistant already knows about the Satel Integra Panel.

Most users already run a base Satel integration. Two are supported:

    * `satel_integra`        (Home Assistant core)
    * `ha_satel_integra_ext` (custom, sjauquet)

Both expose the same shapes (partitions as alarm_control_panel, zones as
binary_sensor, outputs as switch) and store host/port/code in a config entry.
This module reads that via the entity/device registry and the config entry —
no socket needed — so discovery can pre-fill instead of asking the user again.

It deliberately does NOT talk the protocol; that is protocol.py. Here we only
harvest what HA has, and normalize it behind one interface so the config flow
never has to care which base integration is underneath.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .const import DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

# Base integrations we can adopt, best first.
BASE_DOMAINS = ("satel_integra", "ha_satel_integra_ext")

# Satel object number is encoded in the unique_id; both integrations use a
# "<something>_<type>_<number>" tail, e.g. "..._zone_12" / "..._output_5".
_UNIQUE_ID_TAIL = re.compile(r"(?:^|[_-])(zone|output|partition|switch)[_-](\d+)$", re.I)

_PLATFORM_TO_KIND = {
    "binary_sensor": "zone",
    "alarm_control_panel": "partition",
    "switch": "output",
}


@dataclass(slots=True)
class ExistingEntity:
    """One object the base integration already exposes in HA."""

    kind: str            # "zone" | "output" | "partition"
    number: int | None   # Satel object number, if we could parse it
    entity_id: str
    name: str | None
    device_class: str | None
    area_id: str | None
    unique_id: str | None


@dataclass(slots=True)
class ExistingConfig:
    """What HA already has for the Satel Integra Panel, normalized across base integrations."""

    base_domain: str
    entry_id: str
    host: str | None
    port: int
    code: str | None
    entities: list[ExistingEntity] = field(default_factory=list)

    def by_number(self, kind: str) -> dict[int, ExistingEntity]:
        """Map Satel object number -> entity, for one kind. Used to pre-fill
        discovery so name/device_class/area come from HA, not just the Satel Integra Panel."""
        return {
            e.number: e
            for e in self.entities
            if e.kind == kind and e.number is not None
        }

    @property
    def zones(self) -> list[ExistingEntity]:
        return [e for e in self.entities if e.kind == "zone"]

    @property
    def outputs(self) -> list[ExistingEntity]:
        return [e for e in self.entities if e.kind == "output"]

    @property
    def partitions(self) -> list[ExistingEntity]:
        return [e for e in self.entities if e.kind == "partition"]


def _parse_number(unique_id: str | None) -> int | None:
    """Extract the Satel object number from a base-integration unique_id."""
    if not unique_id:
        return None
    match = _UNIQUE_ID_TAIL.search(unique_id)
    return int(match.group(2)) if match else None
