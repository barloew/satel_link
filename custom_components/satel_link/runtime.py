"""Satel Link — runtime data shared across the flow and the platforms.

The config flow discovers a SystemModel but must not stuff it into the entry's
persisted data (it is large and derived). Instead the live model and the client
live in runtime_data on the entry; only the *decisions* (which links, which
controls) are persisted as options.

A Link is a decision: "HA sensor X drives switchable output N, whose zone M
follows it, with this status-forwarding policy." That is the minimal durable
record; everything else is re-derived from discovery.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .const import HaPlatform, OutputCapability, StatusForwarding


@dataclass(slots=True)
class Link:
    """A persisted link decision (module B)."""

    source_entity_id: str          # the HA sensor
    output_number: int             # switchable output it drives
    zone_number: int               # zone that follows the output
    forwarding: StatusForwarding    # when to forward the state
    invert: bool = False           # software polarity correction
    entry_delay_s: int = 0         # ENTRY_DELAY: hold off forwarding a violation
    min_on_s: float = 0.0          # keep the output on at least this long (pulses)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["forwarding"] = self.forwarding.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Link":
        return cls(
            source_entity_id=data["source_entity_id"],
            output_number=int(data["output_number"]),
            zone_number=int(data["zone_number"]),
            forwarding=StatusForwarding(data["forwarding"]),
            invert=bool(data.get("invert", False)),
            entry_delay_s=int(data.get("entry_delay_s", 0)),
            min_on_s=float(data.get("min_on_s", 0.0)),
        )


@dataclass(slots=True)
class Control:
    """A persisted control decision (module B2): expose an output in HA."""

    output_number: int
    platform: str                  # "switch" | "cover"
    down_number: int | None = None  # roller-shutter down half, for covers

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Control":
        return cls(
            output_number=int(data["output_number"]),
            platform=data["platform"],
            down_number=(
                int(data["down_number"]) if data.get("down_number") is not None else None
            ),
        )


@dataclass(slots=True)
class MasterPanel:
    """A master alarm_control_panel that drives several partitions as one tile
    (module C).

    Home Assistant's armed states map onto the base integration's arm services:

        armed_home  -> alarm_arm_home  (Satel's preconfigured arm_home_mode)
        armed_away  -> alarm_arm_away  (mode 0, full arming)
        armed_night -> alarm_arm_away  (mode 0)

    Only the service is chosen here; the Satel mode for "home" is set by the user
    in the base integration (arm_home_mode), so we never send a mode number.
    This is where a future per-state, per-partition mapping (module B) would slot
    in without a storage change.

    Partitions arm in `order`, each verified before the next (the module D
    blocker check runs first); a partition that does not confirm triggers a
    rollback of the whole master.
    """

    partitions: list[int] = field(default_factory=list)  # arm order
    name: str = "Alarm master"

    def to_dict(self) -> dict[str, Any]:
        return {"partitions": self.partitions, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MasterPanel":
        return cls(
            partitions=[int(p) for p in data.get("partitions", [])],
            name=data.get("name", "Alarm master"),
        )

    @staticmethod
    def service_for(ha_state: str) -> str:
        """The base alarm_control_panel service for a Home Assistant armed state."""
        if ha_state == "armed_home":
            return "alarm_arm_home"
        # armed_away and armed_night both map to full arming (mode 0).
        return "alarm_arm_away"


@dataclass(slots=True)
class RuntimeData:
    """Lives on entry.runtime_data. Not persisted.

    Satel Link holds no runtime socket of its own: the ETHM allows one client
    and the base integration owns it. So at runtime Satel Link works *through*
    the base integration — driving its output switches, reading arm state from
    its alarm_control_panel entities. `base` holds that resolved context; the
    only direct connection is the one-off discovery scan.
    """

    model: Any = None                  # last SystemModel from discovery
    links: list[Link] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)
    settings: "Settings" = field(default_factory=lambda: Settings())
    master: "MasterPanel | None" = None
    base: Any = None                   # ExistingConfig: base-integration entities
    engine: Any = None                 # LinkEngine: the forwarding listeners
    events: Any = None                 # EventEngine: blockers + breach snapshots


OPT_MODEL = "model"
OPT_LINKS = "links"
OPT_CONTROLS = "controls"
OPT_SETTINGS = "settings"
OPT_MASTER = "master"


@dataclass(slots=True)
class Settings:
    """Module D settings. The breach lookback is system-wide, with an optional
    per-partition override (partition number -> seconds)."""

    breach_lookback_s: float = 5.0
    partition_lookback: dict[int, float] = field(default_factory=dict)

    def window_for(self, partition: int | None) -> float:
        """The lookback window for a partition: its override, else the default."""
        if partition is None:
            return self.breach_lookback_s
        return self.partition_lookback.get(partition, self.breach_lookback_s)

    def to_dict(self) -> dict[str, Any]:
        return {
            "breach_lookback_s": self.breach_lookback_s,
            # JSON object keys are strings; keep them stringy on the way out.
            "partition_lookback": {
                str(k): v for k, v in self.partition_lookback.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        return cls(
            breach_lookback_s=float(data.get("breach_lookback_s", 5.0)),
            partition_lookback={
                int(k): float(v)
                for k, v in data.get("partition_lookback", {}).items()
            },
        )


def load_settings(options: dict[str, Any]) -> Settings:
    return Settings.from_dict(options.get(OPT_SETTINGS, {}))


def load_master(options: dict[str, Any]) -> "MasterPanel | None":
    data = options.get(OPT_MASTER)
    return MasterPanel.from_dict(data) if data else None


# ---------------------------------------------------------------------------
# Model (de)serialization
#
# Discovery is expensive (it briefly unloads the base integration), so the
# discovered SystemModel is cached in options. The options flow reads it back
# from runtime_data if present, else rebuilds it from here — so restarts never
# force a re-scan, and only an explicit "discover" does.
# ---------------------------------------------------------------------------

def dump_model(model: Any) -> dict[str, Any]:
    return {
        "zones": [_zone_to_dict(z) for z in model.zones],
        "outputs": [_output_to_dict(o) for o in model.outputs],
        "covers": [_cover_to_dict(c) for c in model.covers],
    }


def load_model(data: dict[str, Any]) -> Any:
    from .model import SystemModel

    return SystemModel(
        zones=[_zone_from_dict(z) for z in data.get("zones", [])],
        outputs=[_output_from_dict(o) for o in data.get("outputs", [])],
        covers=[_cover_from_dict(c) for c in data.get("covers", [])],
    )


def _zone_to_dict(z: Any) -> dict[str, Any]:
    return {
        "number": z.number,
        "function": z.function,
        "function_name": z.function_name,
        "partition": z.partition,
        "always_monitored": z.always_monitored,
        "default_forwarding": z.default_forwarding.value,
        "entity_id": z.entity_id,
        "ha_name": z.ha_name,
        "device_class": z.device_class,
        "area_id": z.area_id,
    }


def _zone_from_dict(d: dict[str, Any]) -> Any:
    from .model import MergedZone

    return MergedZone(
        number=d["number"],
        function=d["function"],
        function_name=d["function_name"],
        partition=d["partition"],
        always_monitored=d["always_monitored"],
        default_forwarding=StatusForwarding(d["default_forwarding"]),
        entity_id=d.get("entity_id"),
        ha_name=d.get("ha_name"),
        device_class=d.get("device_class"),
        area_id=d.get("area_id"),
    )


def _output_to_dict(o: Any) -> dict[str, Any]:
    return {
        "number": o.number,
        "function": o.function,
        "function_name": o.function_name,
        "capabilities": sorted(c.value for c in o.capabilities),
        "platform": o.platform.value,
        "entity_id": o.entity_id,
        "ha_name": o.ha_name,
        "area_id": o.area_id,
    }


def _output_from_dict(d: dict[str, Any]) -> Any:
    from .model import MergedOutput

    return MergedOutput(
        number=d["number"],
        function=d["function"],
        function_name=d["function_name"],
        capabilities={OutputCapability(c) for c in d.get("capabilities", [])},
        platform=HaPlatform(d["platform"]),
        entity_id=d.get("entity_id"),
        ha_name=d.get("ha_name"),
        area_id=d.get("area_id"),
    )


def _cover_to_dict(c: Any) -> dict[str, Any]:
    return {
        "up": {"number": c.up.number, "name": c.up.name, "function": c.up.function},
        "down": {
            "number": c.down.number,
            "name": c.down.name,
            "function": c.down.function,
        },
    }


def _cover_from_dict(d: dict[str, Any]) -> Any:
    from .protocol import Cover

    return Cover(up=_output_stub(d["up"]), down=_output_stub(d["down"]))


def _output_stub(d: dict[str, Any]) -> Any:
    """Rebuild a protocol.Output from minimal stored fields; capabilities and
    platform are re-derived from the function so nothing drifts."""
    from .classify import capabilities, classify_output, ha_platform
    from .protocol import Output

    function = d["function"]
    return Output(
        number=d["number"],
        name=d.get("name", ""),
        function=function,
        kind=classify_output(function),
        capabilities=capabilities(function),
        platform=ha_platform(function),
    )


def load_links(options: dict[str, Any]) -> list[Link]:
    return [Link.from_dict(d) for d in options.get(OPT_LINKS, [])]


def load_controls(options: dict[str, Any]) -> list[Control]:
    return [Control.from_dict(d) for d in options.get(OPT_CONTROLS, [])]


def dump_links(links: list[Link]) -> list[dict[str, Any]]:
    return [link.to_dict() for link in links]


def dump_controls(controls: list[Control]) -> list[dict[str, Any]]:
    return [control.to_dict() for control in controls]
