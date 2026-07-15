"""Satel Link — classification of zones and outputs.

`satel_integra2` returns each object's `type_function` as a raw int and never
interprets it. This module adds the semantics:

    * Can Home Assistant drive this output?      -> is_switchable()
    * Is this zone continuously monitored (24H)? -> is_always_monitored()
    * Does the zone function fit the device class? -> validate_link()

Findings carry a translation key and placeholders, never a rendered sentence:
the UI is translated via translations/*.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .const import (
    WIRING_FOLLOW_OUTPUT,
    HaPlatform,
    OutputCapability,
    OutputKind,
    Severity,
    StatusForwarding,
)

_TABLES = json.loads((Path(__file__).parent / "satel_functions.json").read_text("utf-8"))

OUTPUT_FUNCTIONS: dict = _TABLES["output_functions"]
ZONE_FUNCTIONS: dict = _TABLES["zone_functions"]
WIRING_TYPES: dict = _TABLES["wiring_types"]
DEVICE_CLASS_POLICY: dict = _TABLES["device_class_policy"]

_SWITCHABLE: set[int] = set(_TABLES["output_switchable_whitelist"])
_SWITCHABLE_RANGES: list[tuple[int, int]] = [
    tuple(r) for r in _TABLES["output_switchable_ranges"]
]
_ALWAYS_MONITORED: set[int] = set(_TABLES["zone_always_monitored"]["single"])

_CAPS = _TABLES["output_capabilities"]
_LINKABLE: set[int] = set(_CAPS["linkable"])
_LINKABLE_RANGES: list[tuple[int, int]] = [tuple(r) for r in _CAPS["linkable_ranges"]]
_CONTROLLABLE: set[int] = set(_CAPS["controllable_switch"])
_CONTROLLABLE_RANGES: list[tuple[int, int]] = [
    tuple(r) for r in _CAPS["controllable_switch_ranges"]
]
_COVER_UP: set[int] = set(_CAPS["cover_up"])
_COVER_DOWN: set[int] = set(_CAPS["cover_down"])


def _in(function: int, members: set[int], ranges: list[tuple[int, int]]) -> bool:
    return function in members or any(lo <= function <= hi for lo, hi in ranges)


@dataclass(slots=True)
class Finding:
    """A validation result. `key` maps to translations/<lang>.json."""

    severity: Severity
    key: str
    placeholders: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=512)
def _lookup(table_name: str, number: int) -> dict | None:
    """Look up a function; also resolves range keys such as '64-79'."""
    table = OUTPUT_FUNCTIONS if table_name == "output" else ZONE_FUNCTIONS
    if (hit := table.get(str(number))) is not None:
        return hit
    for key, entry in table.items():
        if "-" in key:
            low, high = (int(x) for x in key.split("-"))
            if low <= number <= high:
                return entry
    return None


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def is_switchable(function: int) -> bool:
    """Can Home Assistant drive this output?

    WHITELIST, not a blacklist. An unknown function code is treated as
    read-only. That fails safe: we never try to drive something the Satel Integra Panel
    drives itself — such an output ignores the command WITHOUT reporting an
    error, which is exactly how a link silently does nothing.
    """
    if function in _SWITCHABLE:
        return True
    return any(low <= function <= high for low, high in _SWITCHABLE_RANGES)


def classify_output(function: int) -> OutputKind:
    return OutputKind.SWITCHABLE if is_switchable(function) else OutputKind.READ_ONLY


def is_linkable(function: int) -> bool:
    """Suitable as a pass-through for a link (sensor -> output -> zone).

    Only MONO/BI make sense here: a link just needs on/off. Roller shutter and
    remote-switch outputs are drivable but linking a sensor to a "shutter up"
    output is meaningless, so they are excluded from linking (not from control).
    """
    return _in(function, _LINKABLE, _LINKABLE_RANGES)


def is_controllable(function: int) -> bool:
    """Worth driving from HA for a physical effect (switch-like)."""
    return _in(function, _CONTROLLABLE, _CONTROLLABLE_RANGES)


def is_cover_up(function: int) -> bool:
    return function in _COVER_UP


def is_cover_down(function: int) -> bool:
    return function in _COVER_DOWN


def capabilities(function: int) -> set[OutputCapability]:
    """All the things this output is useful for. May be empty (read-only)."""
    caps: set[OutputCapability] = set()
    if is_linkable(function):
        caps.add(OutputCapability.LINKABLE)
    if is_controllable(function):
        caps.add(OutputCapability.CONTROLLABLE)
    if is_cover_up(function):
        caps.add(OutputCapability.COVER_UP)
    if is_cover_down(function):
        caps.add(OutputCapability.COVER_DOWN)
    return caps


def ha_platform(function: int) -> HaPlatform:
    """The HA entity type this output maps to.

    A roller-shutter pair (105/106) becomes a single cover; both halves report
    COVER so the pairing step in discovery can bundle them.
    """
    if is_cover_up(function) or is_cover_down(function):
        return HaPlatform.COVER
    if is_controllable(function) or is_linkable(function):
        return HaPlatform.SWITCH
    return HaPlatform.BINARY_SENSOR


def output_name(function: int, lang: str = "en") -> str:
    entry = _lookup("output", function)
    return entry[lang] if entry else f"Unknown ({function})"


def output_device_class(function: int) -> str | None:
    """A binary_sensor device class for a read-only output, from its function.

    Read-only outputs are panel-driven statuses (fire, siren, trouble, tamper).
    Mapping them to a device class makes them behave correctly in Home Assistant
    dashboards and automations. Returns None when nothing fits.
    """
    entry = _lookup("output", function)
    if entry is None:
        return None
    name = entry["en"].lower()
    group = entry.get("group")
    if "fire" in name:
        return "smoke"
    if "siren" in name:
        return "sound"
    if "tamper" in name:
        return "tamper"
    if group in ("trouble", "technical"):
        return "problem"
    if group == "alarm":
        return "safety"
    return None


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------

def is_always_monitored(function: int) -> bool:
    """Continuously monitored (24H): alarms even while the partition is disarmed."""
    return function in _ALWAYS_MONITORED


def zone_name(function: int, lang: str = "en") -> str:
    entry = _lookup("zone", function)
    return entry[lang] if entry else f"Unknown ({function})"


def default_forwarding(function: int) -> StatusForwarding:
    """Default status forwarding, derived from the zone function."""
    if is_always_monitored(function):
        return StatusForwarding.ALWAYS
    entry = _lookup("zone", function)
    if entry and entry.get("group") == "delayed":
        return StatusForwarding.ENTRY_DELAY
    return StatusForwarding.ARMED_ONLY


def blocks_arming(function: int) -> bool:
    """True when a zone of this function must be clear to arm.

    Plain burglary zones (perimeter, instant, day/night, exterior) block a
    normal arm when violated — Satel's "denial of arming". Entry/exit delayed
    zones do not (you walk through them on the way out), and 24-hour zones do
    not (they are always active, not a pre-arm condition).
    """
    entry = _lookup("zone", function)
    return bool(entry) and entry.get("group") == "burglary"


# ---------------------------------------------------------------------------
# Link validation
# ---------------------------------------------------------------------------

def validate_link(
    *,
    device_class: str | None,
    zone_function: int,
    output_function: int,
    wiring_type: int | None = None,
    lang: str = "en",
) -> list[Finding]:
    """Validate a proposed link: HA sensor -> switchable output -> zone.

    `wiring_type` is optional because it is not readable over the protocol; pass
    it when the user has supplied it. Whatever cannot be classified is verified
    instead (see verify.py).

    `lang` localizes the function names that go into the placeholders; the
    sentences themselves live in translations/<lang>.json.
    """
    findings: list[Finding] = []

    # 1. Is the output drivable at all?
    if not is_switchable(output_function):
        findings.append(
            Finding(
                Severity.ERROR,
                "output_not_switchable",
                {"function": output_name(output_function, lang)},
            )
        )

    # 2. Does the zone follow an output?
    if wiring_type is not None and wiring_type != WIRING_FOLLOW_OUTPUT:
        findings.append(
            Finding(
                Severity.ERROR,
                "wiring_not_follow_output",
                {"wiring": WIRING_TYPES[str(wiring_type)][lang]},
            )
        )

    # 3. Zone function vs. Home Assistant device class.
    policy = DEVICE_CLASS_POLICY.get(device_class or "")
    always = is_always_monitored(zone_function)
    if policy:
        if policy["mode"] == "always" and not always:
            expected = policy["function"]
            findings.append(
                Finding(
                    Severity.ERROR if policy.get("strict") else Severity.WARNING,
                    "not_always_monitored",
                    {
                        "device_class": device_class or "",
                        "function": zone_name(zone_function, lang),
                        "expected_number": str(expected),
                        "expected": zone_name(expected, lang),
                    },
                )
            )
        elif policy["mode"] == "armed_only" and always:
            findings.append(
                Finding(
                    Severity.WARNING,
                    "unexpectedly_always_monitored",
                    {
                        "device_class": device_class or "",
                        "function": zone_name(zone_function, lang),
                    },
                )
            )

    # 4. Polarity cannot be read — always say so, then verify it.
    findings.append(Finding(Severity.INFO, "polarity_unverifiable"))
    return findings
