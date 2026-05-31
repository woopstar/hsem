"""Tests for the HSEM config flow single-entry guard (issue #368).

Scope
-----
- Unique id is set to the stable domain string early in ``async_step_user``.
- A first config flow proceeds normally to the form.
- A second config flow aborts immediately with ``already_configured`` before
  showing any form.
- The guard fires before the user fills in any fields (no partial data needed).
- Options flow behavior is unchanged.

Approach
--------
The tests mock the HA ``ConfigFlow`` base-class methods so that the
``HSEMConfigFlow`` can be exercised without a real Home Assistant instance or
the ``pytest-homeassistant-custom-component`` plugin.

Key mocked collaborators:
- ``async_set_unique_id``  — records the id passed to it and optionally marks
  the flow as already-configured.
- ``_abort_if_unique_id_configured`` — raises ``data_entry_flow.AbortFlow``
  when a duplicate is detected (mirrors the real HA implementation).
- ``async_show_form`` / ``async_create_entry`` — return lightweight dicts so
  that results can be inspected without HA internals.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.data_entry_flow import AbortFlow

from custom_components.hsem.config_flow import HSEMConfigFlow
from custom_components.hsem.const import DOMAIN

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_flow(*, already_configured: bool = False) -> HSEMConfigFlow:
    """Return a minimally wired ``HSEMConfigFlow`` instance.

    Args:
        already_configured: When ``True`` the ``_abort_if_unique_id_configured``
            mock raises :class:`AbortFlow` to simulate a second config entry.

    Returns:
        An ``HSEMConfigFlow`` with all HA collaborators stubbed out.
    """
    flow = HSEMConfigFlow.__new__(HSEMConfigFlow)
    flow._user_input = {}

    # Minimal fake hass — no active entries needed for these tests because the
    # guard operates through the unique-id mechanism, not entry enumeration.
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    flow.hass = hass

    # async_set_unique_id: record the id but do nothing else.
    flow.async_set_unique_id = AsyncMock(return_value=None)  # type: ignore[method-assign]  # test monkey-patch

    if already_configured:
        # Simulate the real HA behaviour: raises AbortFlow("already_configured").
        flow._abort_if_unique_id_configured = MagicMock(  # type: ignore[method-assign]  # test monkey-patch
            side_effect=AbortFlow("already_configured")
        )
    else:
        # No duplicate: the guard is a no-op.
        flow._abort_if_unique_id_configured = MagicMock(return_value=None)  # type: ignore[method-assign]  # test monkey-patch

    # async_show_form: return a dict representing the form result.
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]  # test monkey-patch
        side_effect=lambda **kwargs: {
            "type": "form",
            "step_id": kwargs.get("step_id"),
            "errors": kwargs.get("errors", {}),
        }
    )

    # async_create_entry: return a dict representing the created entry.
    flow.async_create_entry = MagicMock(  # type: ignore[method-assign]  # test monkey-patch
        side_effect=lambda **kwargs: {
            "type": "create_entry",
            "title": kwargs.get("title"),
            "data": kwargs.get("data"),
        }
    )

    return flow


# ---------------------------------------------------------------------------
# Tests: unique id is set early and is stable
# ---------------------------------------------------------------------------


class TestUniqueIdIsSetEarly:
    """The unique id must be set at the very beginning of async_step_user."""

    @pytest.mark.asyncio
    async def test_unique_id_is_domain_string(self) -> None:
        """async_set_unique_id is called with the DOMAIN constant."""
        flow = _make_flow()
        with patch.object(
            flow,
            "async_show_form",
            return_value={"type": "form", "step_id": "user", "errors": {}},
        ):
            await flow.async_step_user(user_input=None)

        flow.async_set_unique_id.assert_awaited_once_with(DOMAIN)  # type: ignore[attr-defined]  # mock attribute set in test

    @pytest.mark.asyncio
    async def test_unique_id_is_called_before_abort_guard(self) -> None:
        """async_set_unique_id must be called before _abort_if_unique_id_configured."""
        call_order: list[str] = []

        def _set_unique_id_side_effect(uid: str) -> None:
            call_order.append("set_unique_id")

        def _abort_guard_side_effect() -> None:
            call_order.append("abort_guard")

        flow = _make_flow()
        flow.async_set_unique_id = AsyncMock(  # type: ignore[method-assign]  # test monkey-patch
            side_effect=_set_unique_id_side_effect
        )
        flow._abort_if_unique_id_configured = MagicMock(  # type: ignore[method-assign]  # test monkey-patch
            side_effect=_abort_guard_side_effect
        )
        flow.async_show_form = MagicMock(  # type: ignore[method-assign]  # test monkey-patch
            return_value={"type": "form", "step_id": "user", "errors": {}}
        )

        await flow.async_step_user(user_input=None)

        assert call_order == [
            "set_unique_id",
            "abort_guard",
        ], f"Unexpected call order: {call_order}"

    @pytest.mark.asyncio
    async def test_unique_id_is_domain_constant(self) -> None:
        """The unique id value must equal the DOMAIN constant, not a random uuid."""
        recorded_uid: list[Any] = []

        def _record_uid_side_effect(uid: str) -> None:
            recorded_uid.append(uid)

        flow = _make_flow()
        flow.async_set_unique_id = AsyncMock(  # type: ignore[method-assign]  # test monkey-patch
            side_effect=_record_uid_side_effect
        )
        flow.async_show_form = MagicMock(  # type: ignore[method-assign]  # test monkey-patch
            return_value={"type": "form", "step_id": "user", "errors": {}}
        )

        await flow.async_step_user(user_input=None)

        assert len(recorded_uid) == 1
        assert recorded_uid[0] == DOMAIN, (
            f"Expected unique_id == {DOMAIN!r}, got {recorded_uid[0]!r}"
        )


# ---------------------------------------------------------------------------
# Tests: first setup proceeds normally
# ---------------------------------------------------------------------------


class TestFirstSetupProceeds:
    """When no HSEM entry exists the config flow must show the user form."""

    @pytest.mark.asyncio
    async def test_first_flow_shows_user_form(self) -> None:
        """The first config flow shows the 'user' form step."""
        flow = _make_flow(already_configured=False)
        result = await flow.async_step_user(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert result["errors"] == {}

    @pytest.mark.asyncio
    async def test_first_flow_abort_guard_does_not_raise(self) -> None:
        """_abort_if_unique_id_configured must NOT raise on the first setup."""
        flow = _make_flow(already_configured=False)

        # Should complete without exception.
        result = await flow.async_step_user(user_input=None)
        assert result["type"] == "form"

    @pytest.mark.asyncio
    async def test_first_flow_no_base_errors(self) -> None:
        """The first config flow must not pre-populate any form errors."""
        flow = _make_flow(already_configured=False)
        result = await flow.async_step_user(user_input=None)

        errors: dict[str, str] = result["errors"]  # type: ignore[assignment]  # test fixture override
        assert "base" not in errors


# ---------------------------------------------------------------------------
# Tests: duplicate config flow is aborted early
# ---------------------------------------------------------------------------


class TestDuplicateFlowAbortsEarly:
    """When HSEM is already configured a second flow must abort immediately."""

    @pytest.mark.asyncio
    async def test_duplicate_flow_raises_abort_flow(self) -> None:
        """A second config flow raises AbortFlow before touching any form."""
        flow = _make_flow(already_configured=True)

        with pytest.raises(AbortFlow) as exc_info:
            await flow.async_step_user(user_input=None)

        assert exc_info.value.reason == "already_configured"

    @pytest.mark.asyncio
    async def test_duplicate_flow_does_not_show_form(self) -> None:
        """async_show_form must never be called when the guard fires."""
        flow = _make_flow(already_configured=True)

        with pytest.raises(AbortFlow):
            await flow.async_step_user(user_input=None)

        flow.async_show_form.assert_not_called()  # type: ignore[attr-defined]  # mock attribute set in test

    @pytest.mark.asyncio
    async def test_duplicate_flow_aborts_before_user_input_is_processed(
        self,
    ) -> None:
        """Abort must fire even if the caller passes user_input (late submission)."""
        flow = _make_flow(already_configured=True)
        fake_input = {"device_name": "HSEM duplicate", "some_field": "value"}

        with pytest.raises(AbortFlow) as exc_info:
            await flow.async_step_user(user_input=fake_input)

        assert exc_info.value.reason == "already_configured"

    @pytest.mark.asyncio
    async def test_duplicate_flow_unique_id_still_set_before_abort(self) -> None:
        """async_set_unique_id must be called even on the duplicate path."""
        flow = _make_flow(already_configured=True)

        with pytest.raises(AbortFlow):
            await flow.async_step_user(user_input=None)

        flow.async_set_unique_id.assert_awaited_once_with(DOMAIN)  # type: ignore[attr-defined]  # mock attribute set in test


# ---------------------------------------------------------------------------
# Tests: guard uses the unique id mechanism, not entry enumeration
# ---------------------------------------------------------------------------


class TestGuardMechanism:
    """The guard must rely on unique-id matching, not manual entry enumeration."""

    @pytest.mark.asyncio
    async def test_no_manual_entry_enumeration(self) -> None:
        """hass.config_entries.async_entries must not be called by async_step_user.

        The old implementation called async_entries() to check for duplicates.
        The new implementation delegates entirely to _abort_if_unique_id_configured
        after setting the unique id, so async_entries should not be consulted.
        """
        flow = _make_flow(already_configured=False)
        await flow.async_step_user(user_input=None)

        flow.hass.config_entries.async_entries.assert_not_called()  # type: ignore[attr-defined]  # mock attribute set in test

    @pytest.mark.asyncio
    async def test_abort_guard_called_exactly_once_per_step(self) -> None:
        """_abort_if_unique_id_configured must be called exactly once per step invocation."""
        flow = _make_flow(already_configured=False)
        await flow.async_step_user(user_input=None)

        flow._abort_if_unique_id_configured.assert_called_once()  # type: ignore[attr-defined]  # mock attribute set in test
