"""Satel Link — discovery over the Satel integration protocol.

Only a small subset of the protocol is used: enough to *identify* what the Satel Integra Panel
has. `satel_integra2` already provides framing, checksums, encryption and the
Satel character table; this module adds the semantics on top.

The one command that matters:

    0xEE  read device name
      query: [device_type] [device_id]
      reply: [0]=type  [1]=id  [2]=type_function  [3:19]=name(16)  [19]=partition

`partition` is present for ZONES ONLY (device type 0x05). Outputs carry no
partition in the protocol — so a partition shown against an output is
meaningless. A link derives its partition from the zone that follows the
output, never from the output itself.

Connection note: the ETHM accepts only ONE client at a time on the integration
port. If the base integration (satel_integra / ha_satel_integra_ext) is
connected, it owns the socket. Discovery therefore runs once, with the base
integration briefly unloaded, and the result is cached.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .classify import (
    capabilities,
    classify_output,
    default_forwarding,
    ha_platform,
    is_always_monitored,
    output_name,
    zone_name,
)
from .const import (
    DEFAULT_PORT,
    DISCOVERY_TIMEOUT,
    MAX_OUTPUTS,
    MAX_PARTITIONS,
    MAX_ZONES,
    QUERY_INTERVAL,
    DeviceType,
    HaPlatform,
    OutputCapability,
    OutputKind,
    StatusForwarding,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Partition:
    number: int
    name: str


@dataclass(slots=True)
class Zone:
    number: int
    name: str
    function: int
    partition: int | None
    always_monitored: bool
    default_forwarding: StatusForwarding

    @property
    def function_name(self) -> str:
        return zone_name(self.function, "en")


@dataclass(slots=True)
class Output:
    number: int
    name: str
    function: int
    kind: OutputKind
    capabilities: set[OutputCapability] = field(default_factory=set)
    platform: HaPlatform = HaPlatform.BINARY_SENSOR

    @property
    def function_name(self) -> str:
        return output_name(self.function, "en")

    @property
    def switchable(self) -> bool:
        return self.kind is OutputKind.SWITCHABLE

    @property
    def linkable(self) -> bool:
        return OutputCapability.LINKABLE in self.capabilities

    @property
    def controllable(self) -> bool:
        return OutputCapability.CONTROLLABLE in self.capabilities


@dataclass(slots=True)
class Cover:
    """A roller-shutter pair (105 up + 106 down) bundled into one HA cover.
    Satel always programs the two as consecutive outputs; the 'up' output names
    the cover."""

    up: Output
    down: Output

    @property
    def number(self) -> int:
        return self.up.number

    @property
    def name(self) -> str:
        return self.up.name


@dataclass(slots=True)
class DiscoveryResult:
    partitions: list[Partition] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    outputs: list[Output] = field(default_factory=list)
    covers: list[Cover] = field(default_factory=list)

    @property
    def linkable_outputs(self) -> list[Output]:
        """Outputs usable as a pass-through for a link (sensor -> output -> zone)."""
        return [o for o in self.outputs if o.linkable]

    @property
    def controllable_outputs(self) -> list[Output]:
        """Switch-like outputs worth driving from HA (excludes cover halves)."""
        return [
            o
            for o in self.outputs
            if o.controllable and o.platform is not HaPlatform.COVER
        ]

    @property
    def zones_without_partition(self) -> list[Zone]:
        """Zones with no partition are invisible to the pre-arm open-zone check:
        such a zone can sit open while the system arms around it."""
        return [z for z in self.zones if z.partition is None]

    def summary(self) -> dict[str, int]:
        return {
            "partitions": len(self.partitions),
            "zones": len(self.zones),
            "outputs": len(self.outputs),
            "linkable_outputs": len(self.linkable_outputs),
            "controllable_outputs": len(self.controllable_outputs),
            "covers": len(self.covers),
            "zones_without_partition": len(self.zones_without_partition),
        }

    def pair_covers(self) -> None:
        """Bundle roller-shutter up/down outputs into covers.

        Satel programs 105 (up) and 106 (down) as a consecutive pair, so a
        COVER_UP output is paired with the next COVER_DOWN output by number.
        """
        by_number = {o.number: o for o in self.outputs}
        ups = [o for o in self.outputs if OutputCapability.COVER_UP in o.capabilities]
        for up in sorted(ups, key=lambda o: o.number):
            down = by_number.get(up.number + 1)
            if down and OutputCapability.COVER_DOWN in down.capabilities:
                self.covers.append(Cover(up=up, down=down))


class SatelDiscovery:
    """Runs a one-off discovery scan against the Satel Integra Panel.

    `client` is a connected satel_integra2 client. Discovery must run after
    connect() and before monitor_status() — it owns the socket while scanning.
    """

    def __init__(self, client) -> None:
        self._client = client

    async def scan(
        self,
        *,
        max_partitions: int = MAX_PARTITIONS,
        max_zones: int = MAX_ZONES,
        max_outputs: int = MAX_OUTPUTS,
    ) -> DiscoveryResult:
        """Discover partitions, zones and outputs via the client\'s 0xEE scan.

        The vendored client exposes a high-level ``discover_devices`` that returns
        dicts keyed by object id. We classify each object here (switchable?
        continuously monitored? cover?) and pair roller shutters.
        """
        result = DiscoveryResult()
        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                raw = await self._client.discover_devices(
                    max_zones=max_zones,
                    max_partitions=max_partitions,
                    max_outputs=max_outputs,
                )
        except TimeoutError:
            _LOGGER.warning("Discovery timed out after %ss", DISCOVERY_TIMEOUT)
            return result
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Discovery failed: %s", err)
            return result

        for number, info in sorted((raw.get("partitions") or {}).items()):
            name = (info.get("name") or "").strip()
            if name:
                result.partitions.append(Partition(number=int(number), name=name))

        for number, info in sorted((raw.get("zones") or {}).items()):
            name = (info.get("name") or "").strip()
            if not name:
                continue
            function = info.get("type_function", 0)
            partition = info.get("partition_id") or None
            result.zones.append(
                Zone(
                    number=int(number),
                    name=name,
                    function=function,
                    partition=partition,
                    always_monitored=is_always_monitored(function),
                    default_forwarding=default_forwarding(function),
                )
            )

        for number, info in sorted((raw.get("outputs") or {}).items()):
            name = (info.get("name") or "").strip()
            if not name:
                continue
            function = info.get("type_function", 0)
            result.outputs.append(
                Output(
                    number=int(number),
                    name=name,
                    function=function,
                    kind=classify_output(function),
                    capabilities=capabilities(function),
                    platform=ha_platform(function),
                )
            )

        result.pair_covers()
        _LOGGER.info("Discovery complete: %s", result.summary())
        return result



async def discover(
    host: str,
    port: int = DEFAULT_PORT,
    *,
    integration_key: str = "",
    client_factory=None,
) -> DiscoveryResult:
    """Connect, scan, disconnect.

    The caller is responsible for making sure no other client holds the
    integration port — in practice: unload the base integration first, reload it
    afterwards.
    """
    if client_factory is None:  # pragma: no cover - real runtime path
        from .vendor.satel_integra2.satel_integra import AsyncSatel

        client_factory = AsyncSatel

    import asyncio as _asyncio

    client = client_factory(host, port, _asyncio.get_event_loop())
    await client.connect()
    try:
        return await SatelDiscovery(client).scan()
    finally:
        client.close()
