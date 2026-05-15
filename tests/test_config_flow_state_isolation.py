"""Regression tests for HSEMConfigFlow instance-level state isolation (issue #367).

Scope
-----
- ``_user_input`` is NOT a mutable class-level attribute.
- Two ``HSEMConfigFlow`` instances cannot share or pollute each other's
  ``_user_input`` dict.
- Writing to one instance's ``_user_input`` does not affect any other instance.
- A fresh instance always starts with an empty ``_user_input``.
- Class-level audit: no other mutable containers (``list``, ``dict``, ``set``)
  are declared as class attributes on ``HSEMConfigFlow``.

Approach
--------
All tests operate directly on the class and its instances.  No real Home
Assistant instance is required for the state-isolation tests; the few tests
that exercise ``async_step_user`` use the same lightweight mock helpers as
``test_config_flow_single_entry_guard.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.config_flow import HSEMConfigFlow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_flow() -> HSEMConfigFlow:
    """Return a minimally wired ``HSEMConfigFlow`` instance.

    Stubs out all HA collaborators so the flow can be constructed and
    exercised without a real Home Assistant runtime.

    Returns:
        An ``HSEMConfigFlow`` with all base-class methods stubbed out.
    """
    flow = HSEMConfigFlow.__new__(HSEMConfigFlow)
    # Call __init__ explicitly to exercise the real initialiser.
    flow.__init__()  # type: ignore[misc]

    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    flow.hass = hass

    flow.async_set_unique_id = AsyncMock(return_value=None)
    flow._abort_if_unique_id_configured = MagicMock(return_value=None)
    flow.async_show_form = MagicMock(
        side_effect=lambda **kwargs: {
            "type": "form",
            "step_id": kwargs.get("step_id"),
            "errors": kwargs.get("errors", {}),
        }
    )
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kwargs: {
            "type": "create_entry",
            "title": kwargs.get("title"),
            "data": kwargs.get("data"),
        }
    )
    return flow


# ---------------------------------------------------------------------------
# Class-level attribute audit
# ---------------------------------------------------------------------------


class TestNoMutableClassAttributes:
    """HSEMConfigFlow must not declare mutable containers as class attributes."""

    def test_user_input_not_a_class_attribute(self) -> None:
        """``_user_input`` must NOT appear as a class-level attribute.

        Before the fix it was declared as ``_user_input = {}`` on the class
        body. After the fix it must only be set inside ``__init__``.
        """
        # Class __dict__ holds only attributes defined on the class body,
        # NOT those set in __init__, so this directly detects the old pattern.
        assert "_user_input" not in HSEMConfigFlow.__dict__, (
            "_user_input is still declared as a class attribute; "
            "it must be moved to __init__."
        )

    def test_no_mutable_dict_class_attributes(self) -> None:
        """No ``dict`` instance should exist as a class-level attribute."""
        mutable_class_dicts = [
            name
            for name, value in HSEMConfigFlow.__dict__.items()
            if isinstance(value, dict) and not name.startswith("__")
        ]
        assert (
            mutable_class_dicts == []
        ), f"Mutable dict class attributes found: {mutable_class_dicts}"

    def test_no_mutable_list_class_attributes(self) -> None:
        """No ``list`` instance should exist as a class-level attribute."""
        mutable_class_lists = [
            name
            for name, value in HSEMConfigFlow.__dict__.items()
            if isinstance(value, list) and not name.startswith("__")
        ]
        assert (
            mutable_class_lists == []
        ), f"Mutable list class attributes found: {mutable_class_lists}"

    def test_no_mutable_set_class_attributes(self) -> None:
        """No ``set`` instance should exist as a class-level attribute."""
        mutable_class_sets = [
            name
            for name, value in HSEMConfigFlow.__dict__.items()
            if isinstance(value, set) and not name.startswith("__")
        ]
        assert (
            mutable_class_sets == []
        ), f"Mutable set class attributes found: {mutable_class_sets}"

    def test_version_is_immutable_int(self) -> None:
        """``VERSION`` is an ``int``, which is immutable — this is safe."""
        assert isinstance(
            HSEMConfigFlow.__dict__.get("VERSION"), int
        ), "VERSION must be an int class attribute."


# ---------------------------------------------------------------------------
# Instance isolation: fresh instances start empty
# ---------------------------------------------------------------------------


class TestFreshInstanceStartsEmpty:
    """Every new ``HSEMConfigFlow`` instance must have its own empty ``_user_input``."""

    def test_new_instance_has_empty_user_input(self) -> None:
        """``_user_input`` is an empty dict immediately after construction."""
        flow = _make_flow()
        assert flow._user_input == {}

    def test_user_input_is_a_dict(self) -> None:
        """``_user_input`` must be a ``dict``."""
        flow = _make_flow()
        assert isinstance(flow._user_input, dict)

    def test_two_new_instances_have_independent_dicts(self) -> None:
        """Two brand-new instances must not share the same ``_user_input`` object."""
        flow_a = _make_flow()
        flow_b = _make_flow()
        assert flow_a._user_input is not flow_b._user_input, (
            "Both instances share the same _user_input dict object — "
            "they are still using the class-level default."
        )


# ---------------------------------------------------------------------------
# Instance isolation: writes do not cross instance boundaries
# ---------------------------------------------------------------------------


class TestWritesDoNotLeak:
    """Mutations to one instance's ``_user_input`` must not affect any other instance."""

    def test_write_to_first_does_not_affect_second(self) -> None:
        """Setting a key on flow_a must not appear in flow_b."""
        flow_a = _make_flow()
        flow_b = _make_flow()

        flow_a._user_input["device_name"] = "Alpha Inverter"

        assert (
            "device_name" not in flow_b._user_input
        ), "Writing to flow_a leaked into flow_b — class-level dict is still shared."

    def test_write_to_second_does_not_affect_first(self) -> None:
        """Setting a key on flow_b must not appear in flow_a."""
        flow_a = _make_flow()
        flow_b = _make_flow()

        flow_b._user_input["device_name"] = "Beta Inverter"

        assert "device_name" not in flow_a._user_input

    def test_both_can_hold_different_values_for_same_key(self) -> None:
        """Two instances can independently store different values under the same key."""
        flow_a = _make_flow()
        flow_b = _make_flow()

        flow_a._user_input["hsem_energy_share_price"] = 0.12
        flow_b._user_input["hsem_energy_share_price"] = 0.47

        assert flow_a._user_input["hsem_energy_share_price"] == pytest.approx(0.12)
        assert flow_b._user_input["hsem_energy_share_price"] == pytest.approx(0.47)

    def test_update_on_one_does_not_pollute_other(self) -> None:
        """``dict.update()`` on one instance must not affect the other."""
        flow_a = _make_flow()
        flow_b = _make_flow()

        flow_a._user_input.update(
            {"hsem_ev_charger_enabled": True, "hsem_solcast_pv_forecast_1": "sensor.pv"}
        )

        assert (
            flow_b._user_input == {}
        ), "flow_b._user_input was modified by an update() on flow_a."

    def test_clear_on_one_does_not_affect_other(self) -> None:
        """Clearing one instance's dict must leave the other intact."""
        flow_a = _make_flow()
        flow_b = _make_flow()

        flow_a._user_input["some_key"] = "some_value"
        flow_b._user_input["some_key"] = "other_value"

        flow_a._user_input.clear()

        assert flow_b._user_input == {
            "some_key": "other_value"
        }, "Clearing flow_a._user_input also cleared flow_b._user_input."


# ---------------------------------------------------------------------------
# Instance isolation: sequential instance reuse
# ---------------------------------------------------------------------------


class TestSequentialInstanceIsolation:
    """A new instance created after a previous one was used must start clean."""

    def test_second_instance_starts_clean_after_first_populated(self) -> None:
        """The second instance must not inherit state from the first."""
        flow_a = _make_flow()
        flow_a._user_input.update(
            {
                "device_name": "Living Room Solar",
                "hsem_huawei_solar_device_id_inverter_1": "inverter_123",
            }
        )
        # Simulate end of first flow; create a second instance.
        del flow_a

        flow_b = _make_flow()
        assert (
            flow_b._user_input == {}
        ), "flow_b inherited non-empty _user_input from the previously used flow_a."

    def test_three_sequential_instances_all_independent(self) -> None:
        """Three instances created in sequence must all have independent state."""
        instances = [_make_flow() for _ in range(3)]

        for i, flow in enumerate(instances):
            flow._user_input[f"key_{i}"] = f"value_{i}"

        # Each instance should only contain its own key.
        for i, flow in enumerate(instances):
            assert list(flow._user_input.keys()) == [
                f"key_{i}"
            ], f"Instance {i} contains unexpected keys: {list(flow._user_input.keys())}"


# ---------------------------------------------------------------------------
# Instance isolation: async_step_user accumulates data per-instance
# ---------------------------------------------------------------------------


class TestAsyncStepUserPerInstanceState:
    """``async_step_user`` stores input into the instance's own ``_user_input``."""

    @pytest.mark.asyncio
    async def test_async_step_user_populates_instance_user_input(self) -> None:
        """After a successful step, ``_user_input`` on *this* instance is populated."""
        flow = _make_flow()

        fake_input = {"device_name": "My HSEM", "hsem_energy_share_price": 0.3}

        with (
            MagicMock() as _,
            # Patch validate to always return no errors.
            # Patch get_init_step_schema so we do not need hass entities.
        ):
            import unittest.mock as mock

            with (
                mock.patch(
                    "custom_components.hsem.config_flow.validate_init_step_input",
                    new=AsyncMock(return_value={}),
                ),
                mock.patch(
                    "custom_components.hsem.config_flow.get_init_step_schema",
                    new=AsyncMock(return_value=None),
                ),
                mock.patch.object(
                    flow,
                    "async_step_energidataservice",
                    new=AsyncMock(
                        return_value={"type": "form", "step_id": "energidataservice"}
                    ),
                ),
            ):
                await flow.async_step_user(user_input=fake_input)

        assert flow._user_input.get("device_name") == "My HSEM"

    @pytest.mark.asyncio
    async def test_two_flows_with_different_inputs_do_not_cross_pollute(
        self,
    ) -> None:
        """Two flows receiving different user inputs each store their own data."""
        import unittest.mock as mock

        flow_a = _make_flow()
        flow_b = _make_flow()

        input_a = {"device_name": "Solar North", "hsem_energy_share_price": 0.1}
        input_b = {"device_name": "Solar South", "hsem_energy_share_price": 0.2}

        with (
            mock.patch(
                "custom_components.hsem.config_flow.validate_init_step_input",
                new=AsyncMock(return_value={}),
            ),
            mock.patch(
                "custom_components.hsem.config_flow.get_init_step_schema",
                new=AsyncMock(return_value=None),
            ),
        ):
            # Patch the next-step on each instance independently.
            flow_a.async_step_energidataservice = AsyncMock(
                return_value={"type": "form", "step_id": "energidataservice"}
            )
            flow_b.async_step_energidataservice = AsyncMock(
                return_value={"type": "form", "step_id": "energidataservice"}
            )

            await flow_a.async_step_user(user_input=input_a)
            await flow_b.async_step_user(user_input=input_b)

        assert (
            flow_a._user_input.get("device_name") == "Solar North"
        ), f"flow_a has wrong device_name: {flow_a._user_input.get('device_name')}"
        assert (
            flow_b._user_input.get("device_name") == "Solar South"
        ), f"flow_b has wrong device_name: {flow_b._user_input.get('device_name')}"
        # Cross-contamination check: flow_a must not contain flow_b's value.
        assert flow_a._user_input.get("device_name") != "Solar South"
        assert flow_b._user_input.get("device_name") != "Solar North"


# ---------------------------------------------------------------------------
# __init__ is defined and callable
# ---------------------------------------------------------------------------


class TestInitIsDefined:
    """``HSEMConfigFlow.__init__`` must be an explicit method, not inherited."""

    def test_init_is_defined_on_class(self) -> None:
        """``__init__`` must be defined directly on ``HSEMConfigFlow``, not only inherited."""
        # inspect.getmembers walks the MRO; __dict__ is class-local only.
        assert "__init__" in HSEMConfigFlow.__dict__, (
            "__init__ is not defined on HSEMConfigFlow — _user_input won't be "
            "set per-instance."
        )

    def test_init_initialises_user_input(self) -> None:
        """Calling ``__init__`` directly must result in ``_user_input`` being set."""
        flow = _make_flow()
        assert hasattr(
            flow, "_user_input"
        ), "flow._user_input does not exist after __init__."
        assert flow._user_input == {}
