"""Satel Link — verifying a link without setting off the alarm.

Two parameters carry the whole link and are NOT readable over the protocol:

    * wiring type (8 = follow output)
    * polarity (DLOADX option POL.+) — applies to virtual outputs too, because
      POL.+ belongs to the output *object*, not to a physical terminal.

They can only be verified, and that must happen alarm-free: a linked zone is
usually continuously monitored (24H), so it alarms even while the system is
disarmed. "Only test with the partition disarmed" is NOT a valid guard.

    Check 1 — passive coherence check: no switching at all, catches polarity
    Check 2 — link test via bypass:    does switch, but cannot alarm
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .const import LinkStatus

_LOGGER = logging.getLogger(__name__)

SETTLE_SECONDS = 2.0


@dataclass(slots=True)
class LinkCheck:
    """Outcome of a check. `key` and `remedy_key` map to translations/<lang>.json."""

    status: LinkStatus
    key: str
    remedy_key: str | None = None
    placeholders: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Check 1 — passive coherence check (always, zero risk)
# ---------------------------------------------------------------------------

def coherence_check(*, output_active: bool, zone_violated: bool) -> LinkCheck:
    """Compare output state (0x17) with zone state (0x00) AT REST.

    Zero switching, zero alarm. Runs at discovery and after every link.

    With inverted polarity the zone reads *violated while at rest* — on a 24H
    zone that means a permanent alarm. Polarity faults are therefore loud, and
    detectable passively, before the user arms anything.
    """
    if output_active:
        return LinkCheck(LinkStatus.INCONCLUSIVE, "coherence_not_at_rest")

    if zone_violated:
        return LinkCheck(
            LinkStatus.POLARITY_INVERTED,
            "polarity_inverted",
            remedy_key="fix_polarity",
        )

    return LinkCheck(LinkStatus.OK, "coherence_ok")


# ---------------------------------------------------------------------------
# Check 2 — active link test via bypass (optional, also alarm-free)
# ---------------------------------------------------------------------------

async def link_test(
    client,
    *,
    zone: int,
    output: int,
    code: str,
    settle: float = SETTLE_SECONDS,
) -> LinkCheck:
    """Verify the whole chain without a siren.

    What check 1 does NOT catch: the zone does not follow the output at all —
    the wiring type is not 8, or it points at a different output. Then both read
    "clear" at rest and everything looks fine. That is the silent failure: the
    link appears to work and does nothing.

    Bypassing makes switching safe: a bypassed zone still reports its violation
    (violated, 0x00) but does not alarm — violated and bypass (0x06) are
    separate statuses.

    Requires the zone NOT to have the "not bypassable" option set.

    NOTE: that a bypassed zone keeps reporting `violated` follows from the
    protocol design but is not yet confirmed on real hardware. Treat an
    ambiguous result as INCONCLUSIVE rather than as a hard failure.
    """
    await client.set_bypass(code, zone, True)
    try:
        if zone not in getattr(client, "bypass_zones", []):
            return LinkCheck(
                LinkStatus.NOT_BYPASSABLE,
                "not_bypassable",
                remedy_key="allow_bypass",
                placeholders={"zone": str(zone)},
            )

        await client.set_output(code, output, True)
        await asyncio.sleep(settle)
        violated_on = zone in getattr(client, "violated_zones", [])

        await client.set_output(code, output, False)
        await asyncio.sleep(settle)
        violated_off = zone in getattr(client, "violated_zones", [])
    finally:
        # Always lift the bypass, including on failure — never leave a zone
        # bypassed behind.
        await client.set_bypass(code, zone, False)

    if violated_on and not violated_off:
        return LinkCheck(LinkStatus.OK, "link_ok")

    if violated_off and not violated_on:
        return LinkCheck(
            LinkStatus.POLARITY_INVERTED,
            "polarity_inverted",
            remedy_key="fix_polarity",
        )

    if violated_on == violated_off:
        return LinkCheck(
            LinkStatus.NOT_FOLLOWING,
            "not_following",
            remedy_key="set_wiring_follow_output",
            placeholders={"zone": str(zone), "output": str(output)},
        )

    return LinkCheck(LinkStatus.INCONCLUSIVE, "inconclusive")
