"""Tests that guard against broken imports in flow modules and their consumers.

The error that prompted this suite:
  ImportError: cannot import name '_convert_months_to_int' from
  'custom_components.hsem.flows.months'

This caused config_flow.py (which imports options_flow.py) to fail at load
time, preventing the entire HSEM integration from setting up.  These tests
catch that class of mistake at CI time before it reaches production.
"""

import importlib
import inspect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _public_names(module) -> set[str]:
    """Return the set of public names exported by *module*."""
    if hasattr(module, "__all__"):
        return set(module.__all__)
    return {name for name in dir(module) if not name.startswith("_")}


# ---------------------------------------------------------------------------
# Smoke-import tests — each integration module must be importable on its own
# ---------------------------------------------------------------------------

FLOW_MODULES = [
    "custom_components.hsem.flows.batteries_excess_export",
    "custom_components.hsem.flows.batteries_schedule_1",
    "custom_components.hsem.flows.batteries_schedule_2",
    "custom_components.hsem.flows.batteries_schedule_3",
    "custom_components.hsem.flows.energidataservice",
    "custom_components.hsem.flows.ev",
    "custom_components.hsem.flows.ev_second",
    "custom_components.hsem.flows.huawei_solar",
    "custom_components.hsem.flows.init",
    "custom_components.hsem.flows.months",
    "custom_components.hsem.flows.power",
    "custom_components.hsem.flows.solcast",
    "custom_components.hsem.flows.weighted_values",
]

UTIL_MODULES = [
    "custom_components.hsem.utils.misc",
    "custom_components.hsem.utils.huawei",
    "custom_components.hsem.utils.recommendations",
    "custom_components.hsem.utils.sensornames",
    "custom_components.hsem.utils.workingmodes",
]

TOP_LEVEL_MODULES = [
    "custom_components.hsem.config_flow",
    "custom_components.hsem.options_flow",
    "custom_components.hsem.const",
]


class TestFlowModulesImportCleanly:
    """Each flows/* module must be importable without errors."""

    def _assert_importable(self, module_path: str) -> None:
        try:
            importlib.import_module(module_path)
        except ImportError as exc:
            raise AssertionError(f"Failed to import '{module_path}': {exc}") from exc

    def test_batteries_excess_export_importable(self):
        self._assert_importable("custom_components.hsem.flows.batteries_excess_export")

    def test_batteries_schedule_1_importable(self):
        self._assert_importable("custom_components.hsem.flows.batteries_schedule_1")

    def test_batteries_schedule_2_importable(self):
        self._assert_importable("custom_components.hsem.flows.batteries_schedule_2")

    def test_batteries_schedule_3_importable(self):
        self._assert_importable("custom_components.hsem.flows.batteries_schedule_3")

    def test_energidataservice_importable(self):
        self._assert_importable("custom_components.hsem.flows.energidataservice")

    def test_ev_importable(self):
        self._assert_importable("custom_components.hsem.flows.ev")

    def test_ev_second_importable(self):
        self._assert_importable("custom_components.hsem.flows.ev_second")

    def test_huawei_solar_importable(self):
        self._assert_importable("custom_components.hsem.flows.huawei_solar")

    def test_init_importable(self):
        self._assert_importable("custom_components.hsem.flows.init")

    def test_months_importable(self):
        self._assert_importable("custom_components.hsem.flows.months")

    def test_power_importable(self):
        self._assert_importable("custom_components.hsem.flows.power")

    def test_solcast_importable(self):
        self._assert_importable("custom_components.hsem.flows.solcast")

    def test_weighted_values_importable(self):
        self._assert_importable("custom_components.hsem.flows.weighted_values")


class TestUtilModulesImportCleanly:
    """Each utils/* module must be importable without errors."""

    def _assert_importable(self, module_path: str) -> None:
        try:
            importlib.import_module(module_path)
        except ImportError as exc:
            raise AssertionError(f"Failed to import '{module_path}': {exc}") from exc

    def test_misc_importable(self):
        self._assert_importable("custom_components.hsem.utils.misc")

    def test_huawei_importable(self):
        self._assert_importable("custom_components.hsem.utils.huawei")

    def test_recommendations_importable(self):
        self._assert_importable("custom_components.hsem.utils.recommendations")

    def test_sensornames_importable(self):
        self._assert_importable("custom_components.hsem.utils.sensornames")

    def test_workingmodes_importable(self):
        self._assert_importable("custom_components.hsem.utils.workingmodes")


class TestTopLevelModulesImportCleanly:
    """config_flow and options_flow must be importable — these are loaded by HA at startup."""

    def test_config_flow_importable(self):
        """Regression: options_flow imported _convert_months_to_int from flows.months (gone)."""
        try:
            importlib.import_module("custom_components.hsem.config_flow")
        except ImportError as exc:
            raise AssertionError(
                f"config_flow failed to import — this blocks the entire HSEM integration: {exc}"
            ) from exc

    def test_options_flow_importable(self):
        """options_flow must resolve all its imports at load time."""
        try:
            importlib.import_module("custom_components.hsem.options_flow")
        except ImportError as exc:
            raise AssertionError(f"options_flow failed to import: {exc}") from exc


# ---------------------------------------------------------------------------
# Symbol-level tests — specific named exports that other modules depend on
# ---------------------------------------------------------------------------

MONTHS_FLOW_REQUIRED_PUBLIC_SYMBOLS = [
    "get_months_schema",
    "validate_months_input",
]

MISC_UTILS_REQUIRED_PUBLIC_SYMBOLS = [
    "convert_months_to_int",
]


class TestFlowsMonthsPublicAPI:
    """flows.months must NOT expose private helpers that consumers might import.

    The regression was: options_flow imported ``_convert_months_to_int`` directly
    from ``flows.months``.  That private name was later removed (centralised in
    utils.misc), silently breaking the import.  These tests ensure the *intended*
    public API is stable and that the private helper is no longer exported.
    """

    def setup_method(self):
        self.module = importlib.import_module("custom_components.hsem.flows.months")

    def test_get_months_schema_exported(self):
        """get_months_schema must be importable from flows.months."""
        assert hasattr(self.module, "get_months_schema"), (
            "flows.months is missing 'get_months_schema'"
        )

    def test_validate_months_input_exported(self):
        """validate_months_input must be importable from flows.months."""
        assert hasattr(self.module, "validate_months_input"), (
            "flows.months is missing 'validate_months_input'"
        )

    def test_private_convert_months_not_in_flows_months(self):
        """_convert_months_to_int must NOT live in flows.months (it belongs in utils.misc).

        Regression guard: importing a private helper from a flow module caused
        an ImportError that silently broke HA startup.
        """
        assert not hasattr(self.module, "_convert_months_to_int"), (
            "'_convert_months_to_int' was found in flows.months — "
            "it should only exist in utils.misc as the public 'convert_months_to_int'."
        )


class TestUtilsMiscPublicAPI:
    """utils.misc must export the canonical convert_months_to_int."""

    def setup_method(self):
        self.module = importlib.import_module("custom_components.hsem.utils.misc")

    def test_convert_months_to_int_exported(self):
        """convert_months_to_int must be importable from utils.misc."""
        assert hasattr(self.module, "convert_months_to_int"), (
            "utils.misc is missing 'convert_months_to_int'"
        )

    def test_convert_months_to_int_is_callable(self):
        """convert_months_to_int must be a callable function."""
        fn = getattr(self.module, "convert_months_to_int")
        assert callable(fn), "'convert_months_to_int' in utils.misc is not callable"


class TestOptionsFlowUsesCorrectImport:
    """options_flow must import convert_months_to_int from utils.misc, not flows.months."""

    def test_options_flow_imports_from_utils_misc(self):
        """Regression: options_flow once imported a private symbol from flows.months.

        Verify the correct module is the source of the function used at runtime.
        """
        options_flow = importlib.import_module("custom_components.hsem.options_flow")
        misc = importlib.import_module("custom_components.hsem.utils.misc")

        # The function bound in options_flow's namespace must be the same object
        # as the one in utils.misc — not a re-export from flows.months.
        options_fn = getattr(options_flow, "convert_months_to_int", None)
        assert options_fn is not None, (
            "options_flow does not have 'convert_months_to_int' in its namespace"
        )
        assert options_fn is misc.convert_months_to_int, (
            "'convert_months_to_int' in options_flow is not the function from utils.misc"
        )

    def test_options_flow_does_not_import_from_flows_months_private(self):
        """options_flow's source must not reference _convert_months_to_int."""
        options_flow = importlib.import_module("custom_components.hsem.options_flow")
        source_file = inspect.getfile(options_flow)
        with open(source_file, encoding="utf-8") as fh:
            source = fh.read()
        assert "_convert_months_to_int" not in source, (
            "options_flow still references '_convert_months_to_int' — "
            "use 'convert_months_to_int' from utils.misc instead."
        )
