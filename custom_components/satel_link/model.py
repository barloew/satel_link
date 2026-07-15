"""Satel Link — the merged view the config flow works from.

Discovery (protocol.py) knows what the *Satel Integra Panel* has: numbers, functions,
partitions, capabilities. The registry (registry.py) knows what *Home Assistant*
already exposes: entity_id, friendly name, device class, area.

This module joins them by Satel object number, so every object carries both:
the Satel Integra Panel's truth (function -> continuously monitored? switchable? cover?) and
HA's context (name/device_class/area) for pre-filling.
"""

from __future__ import annotations

from dataclasses import dataclass

from .classify import validate_link
from .const import HaPlatform, OutputCapability, StatusForwarding
from .protocol import Cover, DiscoveryResult, Output, Zone
from .registry import ExistingConfig, ExistingEntity


@dataclass(slots=True)
class MergedZone:
    number: int
    function: int
    function_name: str
    partition: int | None
    always_monitored: bool
    default_forwarding: StatusForwarding
    # From HA, if the base integration already exposes this zone:
    entity_id: str | None = None
    ha_name: str | None = None
    device_class: str | None = None
    area_id: str | None = None

    @property
    def display_name(self) -> str:
        return self.ha_name or f"Zone {self.number}"

    @property
    def known_in_ha(self) -> bool:
        return self.entity_id is not None


@dataclass(slots=True)
class MergedOutput:
    number: int
    function: int
    function_name: str
    capabilities: set[OutputCapability]
    platform: HaPlatform
    entity_id: str | None = None
    ha_name: str | None = None
    area_id: str | None = None

    @property
    def display_name(self) -> str:
        return self.ha_name or f"Output {self.number}"

    @property
    def linkable(self) -> bool:
        return OutputCapability.LINKABLE in self.capabilities

    @property
    def controllable(self) -> bool:
        return OutputCapability.CONTROLLABLE in self.capabilities

    @property
    def known_in_ha(self) -> bool:
        return self.entity_id is not None


@dataclass(slots=True)
class SystemModel:
    """Everything the config flow needs, Satel Integra Panel truth + HA context merged."""

    zones: list[MergedZone]
    outputs: list[MergedOutput]
    covers: list[Cover]

    def linkable_outputs(self) -> list[MergedOutput]:
        return [o for o in self.outputs if o.linkable]

    def controllable_outputs(self) -> list[MergedOutput]:
        return [
            o
            for o in self.outputs
            if o.controllable and o.platform is not HaPlatform.COVER
        ]

    def zones_without_partition(self) -> list[MergedZone]:
        return [z for z in self.zones if z.partition is None]

    def zone_for_output(self, output_number: int) -> MergedZone | None:
        """A link's partition comes from the zone that follows the output.

        By the recommended numbering convention the zone shares the output's
        number; fall back to None so the UI asks which zone follows it.
        """
        for zone in self.zones:
            if zone.number == output_number:
                return zone
        return None

    def check_link(
        self, *, zone: MergedZone, output: MergedOutput, wiring_type: int | None = None
    ):
        """Validate a proposed link using Satel Integra Panel + HA facts. Delegates to
        classify.validate_link; the flow renders the findings via translations."""
        return validate_link(
            device_class=zone.device_class,
            zone_function=zone.function,
            output_function=output.function,
            wiring_type=wiring_type,
        )


def build_model(discovery: DiscoveryResult, existing: ExistingConfig | None) -> SystemModel:
    """Join Satel Integra Panel discovery with existing HA entities by Satel object number."""
    zone_ctx: dict[int, ExistingEntity] = (
        existing.by_number("zone") if existing else {}
    )
    output_ctx: dict[int, ExistingEntity] = (
        existing.by_number("output") if existing else {}
    )

    zones = [_merge_zone(z, zone_ctx.get(z.number)) for z in discovery.zones]
    outputs = [_merge_output(o, output_ctx.get(o.number)) for o in discovery.outputs]
    return SystemModel(zones=zones, outputs=outputs, covers=discovery.covers)


def _merge_zone(zone: Zone, ha: ExistingEntity | None) -> MergedZone:
    return MergedZone(
        number=zone.number,
        function=zone.function,
        function_name=zone.function_name,
        partition=zone.partition,
        always_monitored=zone.always_monitored,
        default_forwarding=zone.default_forwarding,
        entity_id=ha.entity_id if ha else None,
        ha_name=(ha.name if ha else None) or zone.name or None,
        device_class=ha.device_class if ha else None,
        area_id=ha.area_id if ha else None,
    )


def _merge_output(output: Output, ha: ExistingEntity | None) -> MergedOutput:
    return MergedOutput(
        number=output.number,
        function=output.function,
        function_name=output.function_name,
        capabilities=output.capabilities,
        platform=output.platform,
        entity_id=ha.entity_id if ha else None,
        ha_name=(ha.name if ha else None) or output.name or None,
        area_id=ha.area_id if ha else None,
    )
