# Satel Link

A companion for the Satel Integra Panel. Satel Link links Home Assistant sensors
into the panel as real, armed Satel zones, and exposes Satel outputs (switches,
roller-shutter covers, and read-only statuses) in Home Assistant.

It builds on an existing Satel base integration (`satel_integra` or
`ha_satel_integra_ext`) — no second connection to the panel at runtime.

**Features**
- Turn any HA sensor into an armed Satel zone (with status forwarding and entry delay)
- Roller shutters as covers, remote/MONO/BI outputs as switches
- Read-only outputs (fire, siren, trouble) as binary sensors with the right device class
- Active pre-arm blocker check and breach snapshots as events

See the [README](https://github.com/barloew/satel_link) for requirements and setup.
