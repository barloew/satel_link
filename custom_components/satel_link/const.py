"""Constants for Satel Link.

Satel Link is a companion for the Satel Integra Panel. Its purpose is to
*link* Home Assistant sensors into the Satel Integra Panel as real, armed Satel zones.

Object model — three types, distinguished by direction:

    Zone              Satel -> HA   detection; belongs to a partition
    Output            Satel -> HA   read-only. HA can react to it, never drive it.
    Switchable Output HA -> Satel   output *and* input:
                                      - as an output it drives something (may be virtual)
                                      - as an input it accepts a switch command from HA
                                    That input is what makes linking possible.

The link chain:

    HA sensor -> Switchable Output -> Zone (wiring type 8: follow output) -> Partition

Two parameters carry the whole link but are NOT exposed over the integration
protocol, so they can only be *verified*, never read:

    * wiring type (8 = follow output) — without it the zone does not follow at all
    * polarity (DLOADX option POL.+) — decides whether "active" is right or inverted.
      Applies to virtual outputs too: POL.+ is a property of the output *object*,
      not of a physical terminal.
"""

from __future__ import annotations

from enum import Enum, IntEnum

DOMAIN = "satel_link"

# Events fired for automations (module D). Payloads are documented in
# services.yaml / the docs; both carry a partition and a list of zones.
EVENT_ARM_BLOCKED = "satel_link_arm_blocked"
EVENT_BREACH = "satel_link_breach"
EVENT_ARM_FAILED = "satel_link_arm_failed"

# Service: the active pre-arm blocker check. Returns the blocking zones.
SERVICE_CHECK_ARM = "check_arm"
ATTR_PARTITION = "partition"

# --- Integration protocol (ETHM, port 7094) ------------------------------------
# Ports 7090 (DLOADX) and 7091 (GuardX) speak a different, closed protocol.
# The ETHM accepts only ONE client at a time on the integration port.
DEFAULT_PORT = 7094

CMD_ZONES_VIOLATED = 0x00
CMD_ZONES_BYPASSED = 0x06
CMD_OUTPUTS_STATE = 0x17
CMD_DEVICE_INFO = 0xEE  # read device name + function + partition

CMD_OUTPUT_ON = 0x88
CMD_OUTPUT_OFF = 0x89
CMD_BYPASS_ON = 0x86
CMD_BYPASS_OFF = 0x87


class DeviceType(IntEnum):
    """Device type byte for CMD_DEVICE_INFO (0xEE)."""

    PARTITION = 0x00
    OUTPUT = 0x04
    ZONE = 0x05  # zone *with* partition assignment (byte 19 of the reply)


# Discovery pacing. The ETHM command buffer holds roughly 160 slots, so partitions
# +zones and outputs are queried in two separate passes; otherwise the output
# queries are dropped silently.
QUERY_INTERVAL = 0.01
IDLE_TIMEOUT = 15.0
DISCOVERY_TIMEOUT = 90.0

MAX_PARTITIONS = 32
MAX_ZONES = 128
MAX_OUTPUTS = 128

# Zone wiring type that makes a zone follow an output. Not readable over the
# protocol — see verify.py.
WIRING_FOLLOW_OUTPUT = 8


class OutputKind(str, Enum):
    """Whether Home Assistant can drive an output at all."""

    SWITCHABLE = "switchable"  # HA -> Satel: controllable and/or linkable
    READ_ONLY = "read_only"    # Satel -> HA: read-only


class OutputCapability(str, Enum):
    """What a switchable output is *useful* for. An output can be both linkable
    and controllable (MONO/BI); a roller shutter is controllable only."""

    LINKABLE = "linkable"            # pass-through for a link: sensor -> output -> zone
    CONTROLLABLE = "controllable"    # drive it from HA for a physical effect
    COVER_UP = "cover_up"            # roller shutter up   (pairs with COVER_DOWN)
    COVER_DOWN = "cover_down"        # roller shutter down


class HaPlatform(str, Enum):
    """Home Assistant entity type an output maps to."""

    SWITCH = "switch"        # MONO/BI/remote switch
    COVER = "cover"          # roller shutter (up+down bundled)
    BINARY_SENSOR = "binary_sensor"  # read-only output


class StatusForwarding(str, Enum):
    """When does Satel Link forward a sensor's state to the Satel Integra Panel?"""

    ALWAYS = "always"            # continuously monitored (24H): smoke/CO/gas/water
    ARMED_ONLY = "armed_only"    # only while the partition is armed
    ENTRY_DELAY = "entry_delay"  # with a Satel Link controlled entry delay


class LinkStatus(str, Enum):
    """Outcome of verifying a link."""

    OK = "ok"
    POLARITY_INVERTED = "polarity_inverted"
    NOT_FOLLOWING = "not_following"
    NOT_BYPASSABLE = "not_bypassable"
    INCONCLUSIVE = "inconclusive"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
