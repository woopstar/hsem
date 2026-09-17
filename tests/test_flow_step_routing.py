"""Step-by-step routing tests for the HSEM config flow and options flow.

Every wizard step follows the same contract:

- no input → show the step's own form (built from the real schema getter);
- input with validation errors → re-show the same form carrying the errors,
  without advancing;
- valid input → merge it into the accumulated ``_user_input`` and delegate to
  the next step.

The tests drive each step through the real ``async_show_form`` /
``async_create_entry`` of Home Assistant's ``FlowHandler`` and only patch the
validators (to choose the branch) and the next step (to observe routing).
Branch-specific behaviour — the second-EV detour, the winter/summer month
split, quick setup, connection testing, and option preservation — is covered
explicitly below.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hsem.config_flow import HSEMConfigFlow
from custom_components.hsem.const import DEFAULT_CONFIG_VALUES, NAME
from custom_components.hsem.flows.quick_setup import (
    _DETECTION_TO_CONFIG,
    CRITICAL_DETECTION_KEYS,
)
from custom_components.hsem.options_flow import HSEMOptionsFlow

_CONFIG_FLOW_MODULE = "custom_components.hsem.config_flow"
_OPTIONS_FLOW_MODULE = "custom_components.hsem.options_flow"

# (step, validator patched in the flow module, next step on success).
# ``ev`` and ``ev_planned_load`` are listed with their single-EV successor;
# the second-EV detour is tested separately.
_SHARED_STEPS: list[tuple[str, str, str]] = [
    ("prices", "validate_prices_input", "months"),
    ("months", "validate_months_input", "solcast"),
    ("solcast", "validate_solcast_step_input", "huawei_solar"),
    ("huawei_solar", "validate_huawei_solar_input", "battery_economics"),
    ("battery_economics", "validate_battery_economics_input", "power"),
    ("power", "validate_power_step_input", "ev"),
    ("ev", "validate_ev_step_input", "ev_planned_load"),
    ("ev_second", "validate_ev_second_step_input", "ev_planned_load"),
    ("ev_planned_load", "validate_ev_planned_load_input", "ocpp"),
    (
        "ev_second_planned_load",
        "validate_ev_second_planned_load_input",
        "ocpp",
    ),
    ("ocpp", "validate_ocpp_step_input", "batteries_wait_mode"),
    (
        "batteries_wait_mode",
        "validate_batteries_wait_mode_input",
        "batteries_excess_export",
    ),
    (
        "batteries_excess_export",
        "validate_batteries_excess_export_input",
        "weighted_values",
    ),
    ("weighted_values", "validate_weighted_values_input", "energy_and_ml"),
]

_CONFIG_FLOW_STEPS = [
    ("user", "validate_init_step_input", "quick_setup"),
    *_SHARED_STEPS,
]
_OPTIONS_FLOW_STEPS = [("init", "validate_init_step_input", "prices"), *_SHARED_STEPS]

_ALL_CONFIG_FORM_STEPS = [step for step, _, _ in _CONFIG_FLOW_STEPS] + ["energy_and_ml"]
_ALL_OPTIONS_FORM_STEPS = [step for step, _, _ in _OPTIONS_FLOW_STEPS] + [
    "energy_and_ml"
]

_NEXT_STEP_SENTINEL: dict[str, Any] = {"type": "sentinel"}


def _make_config_flow() -> HSEMConfigFlow:
    """Return a config flow wired with a mock ``hass`` and flow identity."""
    flow = HSEMConfigFlow()
    flow.hass = MagicMock()
    flow.flow_id = "test_flow"
    flow.handler = "hsem"
    flow.async_set_unique_id = AsyncMock(return_value=None)  # type: ignore[method-assign]  # test monkey-patch
    flow._abort_if_unique_id_configured = MagicMock(return_value=None)  # type: ignore[method-assign]  # test monkey-patch
    return flow


def _make_entry(
    options: dict[str, Any] | None = None, data: dict[str, Any] | None = None
) -> MagicMock:
    """Return a mock config entry exposing plain ``options`` and ``data`` dicts."""
    entry = MagicMock()
    entry.options = dict(options or {})
    entry.data = dict(data or {})
    return entry


def _make_options_flow(entry: MagicMock | None = None) -> HSEMOptionsFlow:
    """Return an options flow wired with a mock ``hass`` and flow identity."""
    flow = HSEMOptionsFlow(entry if entry is not None else _make_entry())
    flow.hass = MagicMock()
    flow.flow_id = "test_flow"
    flow.handler = "test_entry"
    return flow


def _mock_hass(flow: HSEMConfigFlow | HSEMOptionsFlow) -> MagicMock:
    """Return the mock ``hass`` installed on *flow* by the factories above."""
    return cast(MagicMock, flow.hass)


def _schema(result: Any) -> vol.Schema:
    """Return the form schema of a flow *result*, asserting one is present."""
    schema = result["data_schema"]
    assert isinstance(schema, vol.Schema)
    return schema


def _field_names(result: Any) -> set[str]:
    """Return the plain field names of a form result's schema."""
    return {str(marker) for marker in _schema(result).schema}


def _field_defaults(result: Any) -> dict[str, Any]:
    """Return a form result's field defaults keyed by field name."""
    return {str(marker): marker.default() for marker in _schema(result).schema}


async def _run_step(
    flow: HSEMConfigFlow | HSEMOptionsFlow,
    step: str,
    user_input: dict[str, Any] | None,
) -> Any:
    """Invoke ``async_step_<step>`` on *flow* with *user_input*."""
    return await getattr(flow, f"async_step_{step}")(user_input)


# ---------------------------------------------------------------------------
# Generic step contract — config flow
# ---------------------------------------------------------------------------


class TestConfigFlowStepContract:
    """Every config flow step shows, re-shows, and advances consistently."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step", _ALL_CONFIG_FORM_STEPS)
    async def test_step_without_input_shows_its_form(self, step: str) -> None:
        """Opening a step renders its own schema with no errors."""
        flow = _make_config_flow()

        result = await _run_step(flow, step, None)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == step
        assert result["errors"] == {}
        _schema(result)
        assert result["last_step"] is (step == "energy_and_ml")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("step", "validator", "next_step"), _CONFIG_FLOW_STEPS)
    async def test_invalid_input_reshows_form_with_errors(
        self, step: str, validator: str, next_step: str
    ) -> None:
        """Validation errors keep the user on the same step."""
        flow = _make_config_flow()
        errors = {"base": "required"}
        next_step_mock = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(f"{_CONFIG_FLOW_MODULE}.{validator}", AsyncMock(return_value=errors)),
            patch.object(flow, f"async_step_{next_step}", next_step_mock),
        ):
            result = await _run_step(flow, step, {"some_field": "value"})

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == step
        assert result["errors"] == errors
        next_step_mock.assert_not_awaited()
        assert "some_field" not in flow._user_input

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("step", "validator", "next_step"), _CONFIG_FLOW_STEPS)
    async def test_valid_input_advances_to_next_step(
        self, step: str, validator: str, next_step: str
    ) -> None:
        """Valid input is accumulated and the flow moves to the next step."""
        flow = _make_config_flow()
        next_step_mock = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(f"{_CONFIG_FLOW_MODULE}.{validator}", AsyncMock(return_value={})),
            patch.object(flow, f"async_step_{next_step}", next_step_mock),
        ):
            result = await _run_step(flow, step, {"some_field": "value"})

        assert result is _NEXT_STEP_SENTINEL
        next_step_mock.assert_awaited_once_with()
        assert flow._user_input["some_field"] == "value"


# ---------------------------------------------------------------------------
# Generic step contract — options flow
# ---------------------------------------------------------------------------


class TestOptionsFlowStepContract:
    """Every options flow step shows, re-shows, and advances consistently."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step", _ALL_OPTIONS_FORM_STEPS)
    async def test_step_without_input_shows_its_form(self, step: str) -> None:
        """Opening a step renders its own schema with no errors."""
        flow = _make_options_flow()

        result = await _run_step(flow, step, None)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == step
        assert result["errors"] == {}
        _schema(result)
        assert result["last_step"] is (step == "energy_and_ml")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("step", "validator", "next_step"), _OPTIONS_FLOW_STEPS)
    async def test_invalid_input_reshows_form_with_errors(
        self, step: str, validator: str, next_step: str
    ) -> None:
        """Validation errors keep the user on the same step."""
        flow = _make_options_flow()
        errors = {"base": "required"}
        next_step_mock = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(
                f"{_OPTIONS_FLOW_MODULE}.{validator}", AsyncMock(return_value=errors)
            ),
            patch.object(flow, f"async_step_{next_step}", next_step_mock),
        ):
            result = await _run_step(flow, step, {"some_field": "value"})

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == step
        assert result["errors"] == errors
        next_step_mock.assert_not_awaited()
        assert "some_field" not in flow._user_input

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("step", "validator", "next_step"), _OPTIONS_FLOW_STEPS)
    async def test_valid_input_advances_to_next_step(
        self, step: str, validator: str, next_step: str
    ) -> None:
        """Valid input is accumulated and the flow moves to the next step."""
        flow = _make_options_flow()
        next_step_mock = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(f"{_OPTIONS_FLOW_MODULE}.{validator}", AsyncMock(return_value={})),
            patch.object(flow, f"async_step_{next_step}", next_step_mock),
        ):
            result = await _run_step(flow, step, {"some_field": "value"})

        assert result is _NEXT_STEP_SENTINEL
        next_step_mock.assert_awaited_once_with()
        assert flow._user_input["some_field"] == "value"


# ---------------------------------------------------------------------------
# Branches shared by both flows
# ---------------------------------------------------------------------------


_FLOW_FACTORIES = [
    pytest.param(_make_config_flow, _CONFIG_FLOW_MODULE, id="config_flow"),
    pytest.param(_make_options_flow, _OPTIONS_FLOW_MODULE, id="options_flow"),
]


class TestSecondEvDetour:
    """Enabling the second EV inserts its dedicated steps into the wizard."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("factory", "module"), _FLOW_FACTORIES)
    async def test_ev_step_routes_to_second_ev_when_enabled(
        self, factory: Any, module: str
    ) -> None:
        """``hsem_ev_second_enabled`` sends the EV step to ``ev_second``."""
        flow = factory()
        ev_second = AsyncMock(return_value=_NEXT_STEP_SENTINEL)
        planned_load = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(f"{module}.validate_ev_step_input", AsyncMock(return_value={})),
            patch.object(flow, "async_step_ev_second", ev_second),
            patch.object(flow, "async_step_ev_planned_load", planned_load),
        ):
            await flow.async_step_ev({"hsem_ev_second_enabled": True})

        ev_second.assert_awaited_once_with()
        planned_load.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("factory", "module"), _FLOW_FACTORIES)
    async def test_planned_load_routes_to_second_planned_load_when_enabled(
        self, factory: Any, module: str
    ) -> None:
        """A previously enabled second EV adds its planned-load step."""
        flow = factory()
        flow._user_input["hsem_ev_second_enabled"] = True
        second_planned_load = AsyncMock(return_value=_NEXT_STEP_SENTINEL)
        ocpp = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(
                f"{module}.validate_ev_planned_load_input",
                AsyncMock(return_value={}),
            ),
            patch.object(
                flow, "async_step_ev_second_planned_load", second_planned_load
            ),
            patch.object(flow, "async_step_ocpp", ocpp),
        ):
            await flow.async_step_ev_planned_load(
                {"hsem_ev_planned_load_enabled": True}
            )

        second_planned_load.assert_awaited_once_with()
        ocpp.assert_not_awaited()


class TestMonthsSplit:
    """The months step stores winter as ints and derives summer as the rest."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("factory", "module"), _FLOW_FACTORIES)
    async def test_winter_strings_become_ints_and_summer_is_complement(
        self, factory: Any, module: str
    ) -> None:
        """String months from the selector become ints; summer fills the gap."""
        flow = factory()
        solcast = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(f"{module}.validate_months_input", AsyncMock(return_value={})),
            patch.object(flow, "async_step_solcast", solcast),
        ):
            await flow.async_step_months({"hsem_months_winter": ["11", "12", "1", "2"]})

        assert flow._user_input["hsem_months_winter"] == [11, 12, 1, 2]
        assert flow._user_input["hsem_months_summer"] == [3, 4, 5, 6, 7, 8, 9, 10]
        solcast.assert_awaited_once_with()


class TestOcppSecondServerFields:
    """The OCPP form only offers the second server when the second EV exists."""

    @pytest.mark.asyncio
    async def test_config_flow_uses_accumulated_second_ev_flag(self) -> None:
        """The config flow reads the flag from earlier steps' input."""
        flow = _make_config_flow()
        without = await flow.async_step_ocpp(None)
        flow._user_input["hsem_ev_second_enabled"] = True
        with_second = await flow.async_step_ocpp(None)

        assert "hsem_ocpp_second_port" not in _field_names(without)
        assert "hsem_ocpp_second_port" in _field_names(with_second)

    @pytest.mark.asyncio
    async def test_options_flow_uses_saved_second_ev_flag(self) -> None:
        """The options flow honours a second EV already saved on the entry."""
        flow = _make_options_flow(_make_entry(options={"hsem_ev_second_enabled": True}))

        result = await flow.async_step_ocpp(None)

        assert "hsem_ocpp_second_port" in _field_names(result)

    @pytest.mark.asyncio
    async def test_options_flow_uses_second_ev_flag_from_this_session(self) -> None:
        """Enabling the second EV earlier in the same options session counts."""
        flow = _make_options_flow()
        flow._user_input["hsem_ev_second_enabled"] = True

        result = await flow.async_step_ocpp(None)

        assert "hsem_ocpp_second_port" in _field_names(result)


# ---------------------------------------------------------------------------
# Config flow specifics
# ---------------------------------------------------------------------------


class TestConfigFlowHuaweiOptionalDefaults:
    """The Huawei step normalises optional device and EV fields."""

    @pytest.mark.asyncio
    async def test_missing_optional_fields_get_safe_defaults(self) -> None:
        """Absent second inverter/battery ids become ``""``; EV sensors ``None``."""
        flow = _make_config_flow()
        battery_economics = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(
                f"{_CONFIG_FLOW_MODULE}.validate_huawei_solar_input",
                AsyncMock(return_value={}),
            ),
            patch.object(flow, "async_step_battery_economics", battery_economics),
        ):
            await flow.async_step_huawei_solar(
                {"hsem_huawei_solar_device_id_inverter_1": "inverter_1"}
            )

        assert flow._user_input["hsem_huawei_solar_device_id_inverter_2"] == ""
        assert flow._user_input["hsem_huawei_solar_device_id_batteries_2"] == ""
        assert flow._user_input["hsem_ev_charger_status"] is None
        assert flow._user_input["hsem_ev_charger_power"] is None
        battery_economics.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_provided_optional_fields_are_kept(self) -> None:
        """Values the user did provide are not overwritten by the defaults."""
        flow = _make_config_flow()

        with (
            patch(
                f"{_CONFIG_FLOW_MODULE}.validate_huawei_solar_input",
                AsyncMock(return_value={}),
            ),
            patch.object(
                flow,
                "async_step_battery_economics",
                AsyncMock(return_value=_NEXT_STEP_SENTINEL),
            ),
        ):
            await flow.async_step_huawei_solar(
                {
                    "hsem_huawei_solar_device_id_inverter_2": "inverter_2",
                    "hsem_huawei_solar_device_id_batteries_2": "battery_2",
                    "hsem_ev_charger_status": "sensor.ev_status",
                    "hsem_ev_charger_power": "sensor.ev_power",
                }
            )

        assert flow._user_input["hsem_huawei_solar_device_id_inverter_2"] == (
            "inverter_2"
        )
        assert flow._user_input["hsem_huawei_solar_device_id_batteries_2"] == (
            "battery_2"
        )
        assert flow._user_input["hsem_ev_charger_status"] == "sensor.ev_status"
        assert flow._user_input["hsem_ev_charger_power"] == "sensor.ev_power"


def _detected(**overrides: str | None) -> dict[str, str | None]:
    """Return a detection map with every key found, then apply *overrides*."""
    detected: dict[str, str | None] = {
        key: f"sensor.detected_{key}" for key in _DETECTION_TO_CONFIG
    }
    detected.update(overrides)
    return detected


class TestConfigFlowQuickSetup:
    """Quick setup pre-fills detected entities or falls back to the wizard."""

    @pytest.mark.asyncio
    async def test_form_warns_about_missing_critical_entities(self) -> None:
        """Undetected critical entities are listed in the form placeholder."""
        flow = _make_config_flow()
        detected = _detected(battery_soc=None, solcast_today=None)

        with patch(
            f"{_CONFIG_FLOW_MODULE}.auto_detect_entities",
            AsyncMock(return_value=detected),
        ):
            result = await flow.async_step_quick_setup(None)

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "quick_setup"
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        warning = placeholders["warning"]
        assert "battery_soc" in warning
        assert "solcast_today" in warning
        assert "working_mode" not in warning

        defaults = _field_defaults(result)
        assert defaults["battery_soc"] == ""
        assert defaults["working_mode"] == "sensor.detected_working_mode"
        assert defaults["use_quick_setup"] is True

    @pytest.mark.asyncio
    async def test_form_has_no_warning_when_all_critical_entities_found(
        self,
    ) -> None:
        """A complete detection shows the form without a warning."""
        flow = _make_config_flow()

        with patch(
            f"{_CONFIG_FLOW_MODULE}.auto_detect_entities",
            AsyncMock(return_value=_detected()),
        ):
            result = await flow.async_step_quick_setup(None)

        assert result["description_placeholders"] == {}
        assert set(CRITICAL_DETECTION_KEYS) <= _field_names(result)

    @pytest.mark.asyncio
    async def test_confirming_quick_setup_fills_config_and_skips_entity_steps(
        self,
    ) -> None:
        """Quick setup maps detections, fills defaults, and jumps ahead."""
        flow = _make_config_flow()
        flow._user_input["device_name"] = "My HSEM"
        battery_economics = AsyncMock(return_value=_NEXT_STEP_SENTINEL)
        prices = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(
                f"{_CONFIG_FLOW_MODULE}.auto_detect_entities",
                AsyncMock(return_value=_detected(house_power=None)),
            ),
            patch.object(flow, "async_step_battery_economics", battery_economics),
            patch.object(flow, "async_step_prices", prices),
        ):
            result = await flow.async_step_quick_setup(
                {
                    "use_quick_setup": True,
                    # A user override wins over the detected entity.
                    "battery_soc": "sensor.user_battery_soc",
                    # Empty form value falls back to the detection.
                    "working_mode": "",
                }
            )

        assert result is _NEXT_STEP_SENTINEL
        battery_economics.assert_awaited_once_with()
        prices.assert_not_awaited()

        user_input = flow._user_input
        assert user_input["hsem_huawei_solar_batteries_state_of_capacity"] == (
            "sensor.user_battery_soc"
        )
        assert user_input["hsem_huawei_solar_batteries_working_mode"] == (
            "sensor.detected_working_mode"
        )
        # Previously entered values are not overwritten by defaults.
        assert user_input["device_name"] == "My HSEM"
        # Undetected entities fall back to the configured default.
        assert (
            user_input["hsem_house_consumption_power"]
            == (DEFAULT_CONFIG_VALUES["hsem_house_consumption_power"])
        )
        # Defaults are filled, but never with the non-serialisable UNDEFINED.
        assert vol.UNDEFINED not in user_input.values()
        winter = user_input["hsem_months_winter"]
        assert sorted(winter + user_input["hsem_months_summer"]) == list(range(1, 13))
        for key in (
            "hsem_huawei_solar_device_id_inverter_1",
            "hsem_huawei_solar_device_id_inverter_2",
            "hsem_huawei_solar_device_id_batteries",
            "hsem_huawei_solar_device_id_batteries_2",
        ):
            assert key in user_input
        assert "use_quick_setup" not in user_input

    @pytest.mark.asyncio
    async def test_non_list_winter_months_are_converted(self) -> None:
        """A non-list winter months value is converted to integer months."""
        flow = _make_config_flow()
        flow._user_input["hsem_months_winter"] = ""

        with (
            patch(
                f"{_CONFIG_FLOW_MODULE}.auto_detect_entities",
                AsyncMock(return_value=_detected()),
            ),
            patch.object(
                flow,
                "async_step_battery_economics",
                AsyncMock(return_value=_NEXT_STEP_SENTINEL),
            ),
        ):
            await flow.async_step_quick_setup({"use_quick_setup": True})

        assert flow._user_input["hsem_months_winter"] == []
        assert flow._user_input["hsem_months_summer"] == list(range(1, 13))

    @pytest.mark.asyncio
    async def test_declining_quick_setup_runs_the_full_wizard(self) -> None:
        """Choosing advanced setup continues with the prices step."""
        flow = _make_config_flow()
        prices = AsyncMock(return_value=_NEXT_STEP_SENTINEL)

        with (
            patch(
                f"{_CONFIG_FLOW_MODULE}.auto_detect_entities",
                AsyncMock(return_value=_detected()),
            ),
            patch.object(flow, "async_step_prices", prices),
        ):
            result = await flow.async_step_quick_setup({"use_quick_setup": False})

        assert result is _NEXT_STEP_SENTINEL
        prices.assert_awaited_once_with()
        assert "hsem_huawei_solar_batteries_state_of_capacity" not in flow._user_input


def _state(value: str) -> MagicMock:
    """Return a mock HA state object with the given state string."""
    state = MagicMock()
    state.state = value
    return state


class TestConfigFlowFinalStep:
    """The final step tests critical entities before creating the entry."""

    @pytest.mark.asyncio
    async def test_creates_entry_when_connections_are_healthy(self) -> None:
        """Healthy critical entities produce a config entry with all input."""
        flow = _make_config_flow()
        flow._user_input = {
            "device_name": "My HSEM",
            "hsem_import_electricity_price_sensor": "sensor.import",
        }
        _mock_hass(flow).states.get.return_value = _state("1.23")

        with patch(
            f"{_CONFIG_FLOW_MODULE}.validate_energy_and_ml_input",
            AsyncMock(return_value={}),
        ):
            result = await flow.async_step_energy_and_ml({"hsem_ml_enabled": True})

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "My HSEM"
        assert result["data"]["hsem_ml_enabled"] is True
        assert result["data"]["hsem_import_electricity_price_sensor"] == (
            "sensor.import"
        )

    @pytest.mark.asyncio
    async def test_entry_title_falls_back_to_integration_name(self) -> None:
        """Without a device name the entry is titled with the integration name."""
        flow = _make_config_flow()

        with patch(
            f"{_CONFIG_FLOW_MODULE}.validate_energy_and_ml_input",
            AsyncMock(return_value={}),
        ):
            result = await flow.async_step_energy_and_ml({})

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == NAME

    @pytest.mark.asyncio
    async def test_connection_errors_reshow_the_final_form(self) -> None:
        """A failing connection test keeps the user on the final step."""
        flow = _make_config_flow()
        flow._user_input = {"hsem_import_electricity_price_sensor": "sensor.missing"}
        _mock_hass(flow).states.get.return_value = None

        with patch(
            f"{_CONFIG_FLOW_MODULE}.validate_energy_and_ml_input",
            AsyncMock(return_value={}),
        ):
            result = await flow.async_step_energy_and_ml({"hsem_ml_enabled": True})

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "energy_and_ml"
        assert result["last_step"] is True
        assert result["errors"] == {
            "hsem_import_electricity_price_sensor": "entity_not_found"
        }
        assert "hsem_ml_enabled" not in flow._user_input

    @pytest.mark.asyncio
    async def test_validation_errors_reshow_the_final_form(self) -> None:
        """Validator errors are shown without running the connection test."""
        flow = _make_config_flow()
        errors = {"base": "invalid"}

        with (
            patch(
                f"{_CONFIG_FLOW_MODULE}.validate_energy_and_ml_input",
                AsyncMock(return_value=errors),
            ),
            patch.object(flow, "_async_test_connections", AsyncMock()) as test_conn,
        ):
            result = await flow.async_step_energy_and_ml({"hsem_ml_enabled": True})

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == errors
        test_conn.assert_not_awaited()


class TestConfigFlowConnectionTest:
    """``_async_test_connections`` classifies each critical entity."""

    @pytest.mark.asyncio
    async def test_classifies_missing_unavailable_unknown_and_healthy(self) -> None:
        """Missing → not found; unknown/unavailable → unavailable; else OK."""
        flow = _make_config_flow()
        flow._user_input = {
            "hsem_import_electricity_price_sensor": "sensor.missing",
            "hsem_export_electricity_price_sensor": "sensor.unavailable",
            "hsem_huawei_solar_batteries_state_of_capacity": "sensor.unknown",
        }
        states = {
            "sensor.unavailable": _state(STATE_UNAVAILABLE),
            "sensor.unknown": _state(STATE_UNKNOWN),
        }
        _mock_hass(flow).states.get.side_effect = states.get

        errors = await flow._async_test_connections()

        assert errors == {
            "hsem_import_electricity_price_sensor": "entity_not_found",
            "hsem_export_electricity_price_sensor": "entity_unavailable",
            "hsem_huawei_solar_batteries_state_of_capacity": "entity_unavailable",
        }

    @pytest.mark.asyncio
    async def test_unconfigured_entities_are_skipped(self) -> None:
        """Empty entity ids are not looked up and produce no error."""
        flow = _make_config_flow()
        flow._user_input = {"hsem_import_electricity_price_sensor": ""}

        errors = await flow._async_test_connections()

        assert errors == {}
        _mock_hass(flow).states.get.assert_not_called()


class TestConfigFlowMigrationAndOptionsHook:
    """Entry-level hooks on the config flow class."""

    @pytest.mark.asyncio
    async def test_future_version_is_refused(self) -> None:
        """An entry newer than the integration cannot be migrated."""
        flow = _make_config_flow()
        entry = MagicMock()
        entry.entry_id = "future_entry"
        entry.version = HSEMConfigFlow.VERSION + 1
        hass = MagicMock()

        assert await flow.async_migrate_entry(hass, entry) is False
        hass.config_entries.async_update_entry.assert_not_called()

    def test_options_flow_factory_returns_hsem_options_flow(self) -> None:
        """The options flow hook binds the given entry."""
        entry = _make_entry()

        options_flow = HSEMConfigFlow.async_get_options_flow(entry)

        assert isinstance(options_flow, HSEMOptionsFlow)
        assert options_flow._config_entry is entry


# ---------------------------------------------------------------------------
# Options flow specifics
# ---------------------------------------------------------------------------


class TestOptionsFlowFinalStep:
    """Saving the options flow merges input and syncs the entry data."""

    @pytest.mark.asyncio
    async def test_saving_merges_options_and_updates_entry_data(self) -> None:
        """Entity-managed options survive, and ``data`` is synced from options."""
        entry = _make_entry(
            options={"hsem_force_charge_now": True, "device_name": "Old"},
            data={"hsem_update_interval": 5},
        )
        flow = _make_options_flow(entry)
        flow._user_input = {"device_name": "New"}

        with patch(
            f"{_OPTIONS_FLOW_MODULE}.validate_energy_and_ml_input",
            AsyncMock(return_value={}),
        ):
            result = await flow.async_step_energy_and_ml({"hsem_ml_enabled": False})

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "New"
        assert result["data"] == {
            "hsem_force_charge_now": True,
            "device_name": "New",
            "hsem_ml_enabled": False,
        }
        _mock_hass(flow).config_entries.async_update_entry.assert_called_once_with(
            entry,
            data={
                "hsem_update_interval": 5,
                "hsem_force_charge_now": True,
                "device_name": "Old",
            },
        )

    @pytest.mark.asyncio
    async def test_title_falls_back_to_integration_name(self) -> None:
        """Without any device name the entry keeps the integration name."""
        flow = _make_options_flow()

        with patch(
            f"{_OPTIONS_FLOW_MODULE}.validate_energy_and_ml_input",
            AsyncMock(return_value={}),
        ):
            result = await flow.async_step_energy_and_ml({})

        assert result["title"] == NAME

    @pytest.mark.asyncio
    async def test_validation_errors_reshow_the_final_form(self) -> None:
        """Errors keep the user on the last step without saving."""
        flow = _make_options_flow()
        errors = {"base": "invalid"}

        with patch(
            f"{_OPTIONS_FLOW_MODULE}.validate_energy_and_ml_input",
            AsyncMock(return_value=errors),
        ):
            result = await flow.async_step_energy_and_ml({"hsem_ml_enabled": True})

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "energy_and_ml"
        assert result["errors"] == errors
        _mock_hass(flow).config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_form_defaults_come_from_the_existing_entry(self) -> None:
        """Re-opening a step pre-fills it with the saved option value."""
        flow = _make_options_flow(_make_entry(options={"device_name": "Saved Name"}))

        result = await flow.async_step_init(None)

        assert _field_defaults(result)["device_name"] == "Saved Name"
