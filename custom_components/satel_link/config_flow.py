"""Satel Link — config and options flow.

Flow shape:

    1. user step         — confirm we found a base integration (satel_integra /
                           ha_satel_integra_ext) and adopt its host/port/code.
    2. discover step     — run the one-off Satel Integra Panel scan. This briefly unloads the
                           base integration (the ETHM allows one client), scans,
                           and reloads it. Result is cached on the entry.
    3. create entry      — the merged model is stored; entities and links are
                           configured afterwards from the options flow.

Modules and links are added from the options flow, which is where the reviewers'
"modular, meet the user where they are" requirement lives: linking a sensor,
adopting a cover, or exposing an output as a switch are independent steps.

Nothing here holds a rendered sentence: user-facing text comes from
translations/<lang>.json via the standard HA config-flow mechanism (step ids and
error keys), and finding/remedy keys from classify/verify.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_CODE, CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DEFAULT_PORT, DOMAIN, HaPlatform, Severity, StatusForwarding
from .i18n import render_findings
from .model import SystemModel
from .registry import ExistingConfig
from .registry_ha import find_base_entry, read_existing
from .runtime import (
    OPT_CONTROLS,
    OPT_LINKS,
    OPT_MASTER,
    OPT_MODEL,
    OPT_SETTINGS,
    Control,
    Link,
    MasterPanel,
    Settings,
    dump_model,
    load_controls,
    load_links,
    load_master,
    load_model,
    load_settings,
)

_LOGGER = logging.getLogger(__name__)

CONF_BASE_ENTRY = "base_entry_id"

# Field names used in the link/control subflow forms.
CONF_OUTPUT = "output"
CONF_ZONE = "zone"
CONF_SOURCE = "source"
CONF_FORWARDING = "forwarding"
CONF_INVERT = "invert"
CONF_ENTRY_DELAY = "entry_delay_s"
CONF_MIN_ON = "min_on_s"
CONF_LOOKBACK = "breach_lookback_s"
CONF_MASTER_NAME = "name"
CONF_MASTER_PARTITIONS = "partitions"
CONF_CONFIRM = "confirm"


class SatelLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Satel Link on top of an existing Satel base integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._existing: ExistingConfig | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Adopt a base integration, or fall back to manual connection details."""
        base_entry = find_base_entry(self.hass)

        if base_entry is None:
            return await self.async_step_manual()

        self._existing = read_existing(self.hass, base_entry)

        if user_input is not None:
            await self.async_set_unique_id(f"{DOMAIN}_{base_entry.entry_id}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Satel Link",
                data={
                    CONF_BASE_ENTRY: base_entry.entry_id,
                    CONF_HOST: self._existing.host,
                    CONF_PORT: self._existing.port,
                    CONF_CODE: self._existing.code,
                },
            )

        # Show what we will adopt; text is in translations under step "user".
        return self.async_show_form(
            step_id="user",
            description_placeholders={
                "base_domain": base_entry.domain,
                "host": self._existing.host or "?",
                "zones": str(len(self._existing.zones)),
                "outputs": str(len(self._existing.outputs)),
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No base integration found: ask for connection details directly."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{DOMAIN}_{user_input[CONF_HOST]}_{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Satel Link",
                data={
                    CONF_HOST: user_input[CONF_HOST],
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_CODE: user_input.get(CONF_CODE),
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Optional(CONF_CODE): str,
            }
        )
        return self.async_show_form(
            step_id="manual", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return SatelLinkOptionsFlow(entry)


class SatelLinkOptionsFlow(OptionsFlow):
    """Modular configuration: discovery, links, controls — each independent."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        # Selection state, carried across the steps of one subflow.
        self._sel_output: int | None = None
        self._sel_zone: int | None = None
        self._sel_source: str | None = None
        self._sel_forwarding: StatusForwarding | None = None
        self._sel_invert: bool = False
        self._sel_entry_delay: int = 0
        self._sel_min_on: float = 0.0

    @property
    def _model(self) -> SystemModel | None:
        """The discovered model: freshest from runtime_data, else from options.

        Persisted in options by the discover step, so links and controls can be
        configured after a restart without re-scanning the Satel Integra Panel.
        """
        runtime = getattr(self._entry, "runtime_data", None)
        if runtime is not None and getattr(runtime, "model", None) is not None:
            return runtime.model
        stored = self._entry.options.get(OPT_MODEL)
        return load_model(stored) if stored else None

    @property
    def _language(self) -> str:
        return self.hass.config.language

    def _options_with(self, **changes: Any) -> dict[str, Any]:
        """Current options plus changes, so we never drop links/controls/model."""
        return {**self._entry.options, **changes}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu of independent modules (translations key: 'options.step.init')."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "discover",   # module A — connect & discover
                "link",       # module B — link an HA sensor to a Satel zone
                "control",    # module B2 — expose an output as switch/cover
                "master",     # module C — master alarm panel over partitions
                "settings",   # module D — breach lookback window
            ],
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Module A — run the one-off scan.

        The scan needs the integration socket, which the base integration holds
        (the ETHM allows one client). So: unload base -> scan -> reload base.
        Confirmed by the user before we interrupt their alarm connection.
        """
        if user_input is None:
            return self.async_show_form(step_id="discover")

        from .discovery_runner import run_discovery  # local import: pulls HA + lib

        try:
            model = await run_discovery(self.hass, self._entry)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Discovery failed")
            return self.async_show_form(
                step_id="discover", errors={"base": "discovery_failed"}
            )

        # Persist the full model so links/controls survive a restart without a
        # re-scan. Existing links/controls are kept intact.
        if (runtime := getattr(self._entry, "runtime_data", None)) is not None:
            runtime.model = model
        return self.async_create_entry(
            title="", data=self._options_with(**{OPT_MODEL: dump_model(model)})
        )

    # -- Module B: link an HA sensor to a Satel zone -----------------------

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1 — pick a switchable output to link through."""
        model = self._model
        if model is None:
            return self.async_abort(reason="run_discovery_first")

        outputs = model.linkable_outputs()
        if not outputs:
            return self.async_abort(reason="no_linkable_outputs")

        if user_input is not None:
            self._sel_output = int(user_input[CONF_OUTPUT])
            return await self.async_step_link_target()

        schema = vol.Schema(
            {
                vol.Required(CONF_OUTPUT): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=str(o.number),
                                label=f"{o.number} — {o.display_name} "
                                f"({o.function_name})",
                            )
                            for o in outputs
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="link", data_schema=schema)

    async def async_step_link_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2 — pick the HA source sensor and the zone that follows."""
        model = self._model
        assert model is not None  # guarded in async_step_link

        if user_input is not None:
            self._sel_source = user_input[CONF_SOURCE]
            self._sel_zone = int(user_input[CONF_ZONE])
            return await self.async_step_link_verify()

        # Prefill the zone that already shares the output's number, if any.
        suggested = model.zone_for_output(self._sel_output)
        zone_field = (
            vol.Required(CONF_ZONE, default=str(suggested.number))
            if suggested
            else vol.Required(CONF_ZONE)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE): _binary_sensor_selector(),
                zone_field: selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=str(z.number),
                                label=f"{z.number} — {z.display_name} "
                                f"({z.function_name})",
                            )
                            for z in model.zones
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="link_target",
            data_schema=schema,
            description_placeholders={"output": str(self._sel_output)},
        )

    async def async_step_link_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3 — show findings; block on errors; else choose forwarding & save."""
        model = self._model
        assert model is not None

        zone = next(z for z in model.zones if z.number == self._sel_zone)
        output = next(o for o in model.outputs if o.number == self._sel_output)
        findings = model.check_link(zone=zone, output=output)
        blocking = [f for f in findings if f.severity is Severity.ERROR]
        rendered = render_findings(findings, self._language)

        # Hard errors: show them and send the user back to fix the selection.
        if blocking:
            if user_input is not None:
                return await self.async_step_link()
            return self.async_show_form(
                step_id="link_verify",
                data_schema=vol.Schema({}),
                errors={"base": "link_has_errors"},
                description_placeholders={"findings": rendered},
            )

        # No blockers: confirm and pick the status-forwarding policy.
        if user_input is not None:
            self._sel_forwarding = StatusForwarding(user_input[CONF_FORWARDING])
            self._sel_invert = user_input.get(CONF_INVERT, False)
            self._sel_entry_delay = int(user_input.get(CONF_ENTRY_DELAY, 0))
            self._sel_min_on = float(user_input.get(CONF_MIN_ON, 0))
            return self._save_link()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FORWARDING, default=zone.default_forwarding.value
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=f.value, label=f.value)
                            for f in StatusForwarding
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="forwarding",
                    )
                ),
                vol.Optional(CONF_ENTRY_DELAY, default=0): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=255, step=1, unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_MIN_ON, default=0): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=60, step=0.5, unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_INVERT, default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="link_verify",
            data_schema=schema,
            description_placeholders={"findings": rendered or "—"},
        )

    def _save_link(self) -> ConfigFlowResult:
        links = load_links(self._entry.options)
        # Replace any existing link on the same output.
        links = [l for l in links if l.output_number != self._sel_output]
        links.append(
            Link(
                source_entity_id=self._sel_source,
                output_number=self._sel_output,
                zone_number=self._sel_zone,
                forwarding=self._sel_forwarding,
                invert=self._sel_invert,
                entry_delay_s=self._sel_entry_delay,
                min_on_s=self._sel_min_on,
            )
        )
        return self.async_create_entry(
            title="",
            data=self._options_with(**{OPT_LINKS: [l.to_dict() for l in links]}),
        )

    # -- Module B2: control a Satel output from HA -------------------------

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Expose a Satel output as an HA switch, or a roller-shutter pair as a
        cover. No zone follows, so there is no link test here."""
        model = self._model
        if model is None:
            return self.async_abort(reason="run_discovery_first")

        # A cover option per shutter pair, plus each switch-like output.
        options: list[selector.SelectOptionDict] = [
            selector.SelectOptionDict(
                value=f"cover:{c.number}",
                label=f"{c.name} (cover {c.up.number}/{c.down.number})",
            )
            for c in model.covers
        ] + [
            selector.SelectOptionDict(
                value=f"switch:{o.number}",
                label=f"{o.number} — {o.display_name} ({o.function_name})",
            )
            for o in model.controllable_outputs()
        ]
        # Read-only outputs the base exposes can be observed as binary_sensors.
        base_outputs = self._entry.runtime_data.base.by_number("output") if (
            self._entry.runtime_data and self._entry.runtime_data.base
        ) else {}
        options += [
            selector.SelectOptionDict(
                value=f"sensor:{o.number}",
                label=f"{o.number} — {o.display_name} ({o.function_name}) · read-only",
            )
            for o in model.outputs
            if o.platform is HaPlatform.BINARY_SENSOR and o.number in base_outputs
        ]
        if not options:
            return self.async_abort(reason="no_controllable_outputs")

        if user_input is not None:
            kind, _, number = user_input[CONF_OUTPUT].partition(":")
            return self._save_control(kind, int(number), model)

        schema = vol.Schema(
            {
                vol.Required(CONF_OUTPUT): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="control", data_schema=schema)

    # -- Module C: master alarm panel --------------------------------------

    async def async_step_master(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a master panel: which partitions, in which arm order.

        Partitions are entered in arm order (interior before perimeter, say).
        HomeKit "home" maps to the base arm_home service (the Satel mode is the
        user's arm_home_mode); "away" and "night" map to full arming.
        """
        model = self._model
        if model is None:
            return self.async_abort(reason="run_discovery_first")

        known = sorted({z.partition for z in model.zones if z.partition})
        if not known:
            return self.async_abort(reason="no_partitions")

        existing = load_master(self._entry.options)
        errors: dict[str, str] = {}

        if user_input is not None:
            raw = user_input[CONF_MASTER_PARTITIONS]
            try:
                order = [int(p.strip()) for p in raw.split(",") if p.strip()]
            except ValueError:
                order = []
            if not order or any(p not in known for p in order):
                errors["base"] = "invalid_partitions"
            else:
                master = MasterPanel(
                    partitions=order,
                    name=user_input.get(CONF_MASTER_NAME) or "Alarm master",
                )
                return self.async_create_entry(
                    title="",
                    data=self._options_with(**{OPT_MASTER: master.to_dict()}),
                )

        default_order = (
            ",".join(str(p) for p in existing.partitions)
            if existing
            else ",".join(str(p) for p in known)
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MASTER_NAME,
                    default=existing.name if existing else "Alarm master",
                ): str,
                vol.Required(
                    CONF_MASTER_PARTITIONS, default=default_order
                ): str,
            }
        )
        return self.async_show_form(
            step_id="master",
            data_schema=schema,
            errors=errors,
            description_placeholders={"partitions": ", ".join(str(p) for p in known)},
        )

    # -- Module D: breach lookback settings --------------------------------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the breach lookback window: system-wide, with a per-partition
        override. The window is how many seconds back the breach snapshot
        looks when a partition is triggered."""
        settings = load_settings(self._entry.options)
        model = self._model
        partitions = (
            sorted({z.partition for z in model.zones if z.partition})
            if model
            else []
        )

        if user_input is not None:
            overrides: dict[int, float] = {}
            for number in partitions:
                value = user_input.get(f"lookback_p{number}")
                if value is not None:
                    overrides[number] = float(value)
            new_settings = Settings(
                breach_lookback_s=float(user_input[CONF_LOOKBACK]),
                partition_lookback=overrides,
            )
            return self.async_create_entry(
                title="",
                data=self._options_with(**{OPT_SETTINGS: new_settings.to_dict()}),
            )

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_LOOKBACK, default=settings.breach_lookback_s
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=120, step=0.5, unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        }
        # One optional override field per partition; blank = use the default.
        for number in partitions:
            key = f"lookback_p{number}"
            override = settings.partition_lookback.get(number)
            field = (
                vol.Optional(key, default=override)
                if override is not None
                else vol.Optional(key)
            )
            fields[field] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=120, step=0.5, unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(fields)
        )

    def _save_control(
        self, kind: str, number: int, model: SystemModel
    ) -> ConfigFlowResult:
        controls = load_controls(self._entry.options)
        controls = [c for c in controls if c.output_number != number]
        if kind == "cover":
            cover = next(c for c in model.covers if c.number == number)
            controls.append(
                Control(
                    output_number=cover.up.number,
                    platform=HaPlatform.COVER.value,
                    down_number=cover.down.number,
                )
            )
        elif kind == "sensor":
            controls.append(
                Control(
                    output_number=number, platform=HaPlatform.BINARY_SENSOR.value
                )
            )
        else:
            controls.append(
                Control(output_number=number, platform=HaPlatform.SWITCH.value)
            )
        return self.async_create_entry(
            title="",
            data=self._options_with(
                **{OPT_CONTROLS: [c.to_dict() for c in controls]}
            ),
        )


def _binary_sensor_selector() -> selector.EntitySelector:
    """The HA source sensor for a link is a binary_sensor."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="binary_sensor")
    )
