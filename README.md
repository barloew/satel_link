# Satel Link

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=barloew&repository=satel_link&category=integration)

A companion for the Satel Integra Panel. Satel Link links Home Assistant sensors
into the Satel Integra Panel as real, armed Satel zones, and exposes Satel
outputs (switches, roller-shutter covers, and read-only statuses) in Home
Assistant.

It builds on an existing Satel base integration rather than replacing it. The
Satel ETHM module accepts only one client on its integration port, so at runtime
Satel Link holds no connection of its own: it drives the base integration's
output switches and reads arm state from its `alarm_control_panel` entities. The
only direct connection is a one-off discovery scan.

> **Status: early release (0.1.0).** The runtime architecture and the link test
> follow from the Satel integration protocol but should be confirmed against your
> own hardware before you rely on them.

## What it does

Three Satel object types, distinguished by direction:

| Type | Home Assistant | Direction | Meaning |
|---|---|---|---|
| Zone | `binary_sensor` | Satel → HA | detection |
| Output | `binary_sensor` | Satel → HA | read-only status (fire, siren, trouble) |
| Switchable Output | `switch` | HA → Satel | the only thing HA can drive — this is what you link |

A **link** is the chain: an HA sensor drives a switchable output, and a Satel
zone configured to *follow output* mirrors it. That turns any Home Assistant
sensor (Zigbee, Z-Wave, whatever) into a genuine, armed Satel zone.

## Requirements

- An **INTEGRA** or **INTEGRA Plus** panel (not VERSA/PERFECTA).
- An **ETHM-1** or **ETHM-1 Plus** module, with *Integration* enabled in DLOADX.
- A working Satel base integration in Home Assistant — either
  [`satel_integra`](https://www.home-assistant.io/integrations/satel_integra/)
  (core) or `ha_satel_integra_ext`.

The protocol client that performs 0xEE discovery and zone bypass is bundled
(vendored) in `custom_components/satel_link/vendor/`, so no extra library is
required beyond `cryptography` (already a Home Assistant dependency). The
vendored client is MIT-licensed; see its NOTICE and LICENSE for credit to the
upstream `satel_integra2` projects.
- For linking: a switchable output (function **24 MONO** or **25 BI**) and a zone
  with wiring type **8 (follow output)**. A user with rights to that output.

See the wiki/docs for the full DLOADX checklist, including the polarity (POL.+)
and user-rights pitfalls.

## Installation

### HACS (recommended)

1. In HACS, add this repository as a custom repository
   (`https://github.com/barloew/satel_link`, category *Integration*).
2. Install **Satel Link**.
3. Restart Home Assistant.
4. Add the integration under *Settings → Devices & Services → Add Integration →
   Satel Link*.

### Manual

Copy `custom_components/satel_link/` into your Home Assistant `config/custom_components/`
directory and restart.

## Configuration

Setup adopts your Satel base integration (host, port, code) automatically. After
that, everything is in the integration's options, as independent modules:

- **Connect & discover** — a one-off scan of the panel. It briefly unloads the
  base integration (one client at a time), then reloads it. The result is cached.
- **Link a sensor** — pick a switchable output, the HA sensor, and the zone that
  follows it. Satel Link validates the combination (function, device class) and
  lets you choose when the state is forwarded.
- **Control an output** — expose a switchable output as a switch, a roller-shutter
  pair as one cover, or a read-only output as a binary sensor.
- **Master panel** — one `alarm_control_panel` that arms several partitions as a
  unit (see below).
- **Settings** — the breach snapshot lookback window, system-wide with an
  optional per-partition override.

### Status forwarding

Each link decides *when* a sensor's state reaches the panel:

- **Continuously monitored** — always forwarded. For smoke, CO, gas and water,
  which must alarm even when disarmed.
- **Only while armed** — forwarded only while the zone's partition is armed. For
  motion, doors and windows.
- **With entry delay** — a violation is held back for a configurable delay, so
  disarming in time prevents the alarm.

A minimum on-time keeps brief pulses (a flickering PIR) visible to the panel.

### Master panel

HomeKit couples one accessory to one alarm panel, so with several partitions you
can normally control only one from HomeKit. A master panel aggregates several
partitions into one `alarm_control_panel` tile.

- Partitions arm **in the order you set** (interior before perimeter, say), each
  verified before the next. The pre-arm blocker check runs first for every
  partition.
- If a partition does not confirm, the master **rolls back** — it disarms
  whatever it already armed and fires `satel_link_arm_failed` — so the tile never
  claims "armed" while the system is only half-armed.
- Home Assistant's armed states map onto the base integration's services:
  **Home** uses `alarm_arm_home` (so the Satel mode is whatever you set as
  `arm_home_mode` in the base integration), while **Away** and **Night** use
  `alarm_arm_away` (full arming). The meaning of "home" is therefore yours to
  define, at the panel, per partition.

**Code handling.** Arming a partition needs a user code. Satel Link never stores
it — it passes through whatever Home Assistant supplies:

- In the normal HA UI, HA prompts for the code and passes it on. The user whose
  code is entered must have rights on **all** partitions in the master.
- HomeKit cannot prompt for a code, so supply it in the HomeKit bridge config:

  ```yaml
  homekit:
    - name: HA Bridge 01
      # ...
      entity_config:
        alarm_control_panel.alarm_master:
          code: !secret alarm_panel_usercode
  ```

  Use a dedicated Satel user with arm/disarm rights on exactly the master's
  partitions, rather than sharing your own code.

## Events and services

Satel Link fires events you can build automations on:

- `satel_link_arm_blocked` — partition plus the burglary zones that would block
  an arm.
- `satel_link_breach` — partition plus the zones breached in the lookback window
  when the partition was triggered.
- `satel_link_arm_failed` — partition plus the reason a master arm was rolled
  back (blocked, or no confirmation).

And a service for an active pre-arm check:

- `satel_link.check_arm` — returns the zones blocking an arm for a partition (and
  fires `satel_link_arm_blocked`). Use it to warn or to hold off before arming.

## Verifying a link

Two parameters that carry a link cannot be read over the protocol — the wiring
type and the output polarity — so Satel Link verifies them instead:

- A **passive coherence check** compares the output and zone at rest; an inverted
  polarity shows up as a zone violated while idle.
- An **active link test** bypasses the zone (so it cannot alarm), toggles the
  output, and checks the zone follows.

## License

Released under the [MIT License](LICENSE).

Not affiliated with SATEL sp. z o.o. "Satel" and "Integra" are trademarks of
their respective owner.
