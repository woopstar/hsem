"""Tests for how OCPP TLS is wired into HSEM (issue #1341).

Covers the OCPP flow step's TLS fields and validation, reading the settings
into ``SensorConfig``, starting both OCPP servers with one TLS context (and
never without it when TLS setup fails), the client-certificate service,
and the ``ocpp_tls`` block in diagnostics. The certificate code itself is
tested in ``tests/utils/test_ocpp_tls.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID

from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES, DOMAIN
from custom_components.hsem.custom_sensors.config_reader import build_sensor_config
from custom_components.hsem.flows.ocpp import (
    get_ocpp_step_schema,
    validate_ocpp_step_input,
)
from custom_components.hsem.planner import run_planner
from custom_components.hsem.services_ocpp_tls import (
    SCHEMA_OCPP_GENERATE_CLIENT_CERTIFICATE,
    async_handle_ocpp_generate_client_certificate,
)
from custom_components.hsem.utils import ocpp_tls
from custom_components.hsem.utils.diagnostics import build_diagnostics_dump
from custom_components.hsem.utils.ocpp_tls import OCPPTLSError, OCPPTLSInfo
from tests.coordinator_fixtures import make_real_coordinator
from tests.planner.fixtures import make_summer_day_input

_LIFECYCLE = "custom_components.hsem.coordinator_lifecycle"

_OCPP_INPUT: dict[str, Any] = {
    "hsem_ocpp_enabled": True,
    "hsem_ocpp_port": 9000,
    "hsem_ocpp_start_window_s": 60,
    "hsem_ocpp_stop_window_s": 180,
}

_BOTH_SERVERS: dict[str, Any] = {
    **_OCPP_INPUT,
    "hsem_ocpp_second_enabled": True,
    "hsem_ocpp_second_port": 9001,
    "hsem_ev_second_planned_load_enabled": True,
}


def _executor_hass() -> MagicMock:
    """Return a mock hass whose executor jobs run inline."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    return hass


class TestFlowStep:
    """The OCPP step offers the TLS settings and checks the files."""

    @pytest.mark.asyncio
    async def test_schema_offers_tls_fields_with_safe_defaults(self) -> None:
        """TLS defaults to off, and the paths default to empty."""
        schema = await get_ocpp_step_schema(None)

        validated = schema(_OCPP_INPUT | {"hsem_ocpp_charging_rate_unit": "auto"})

        assert validated["hsem_ocpp_tls_mode"] == "off"
        assert validated["hsem_ocpp_tls_server_cert_path"] == ""
        assert validated["hsem_ocpp_tls_server_key_path"] == ""
        assert validated["hsem_ocpp_tls_client_ca_path"] == ""

    @pytest.mark.asyncio
    async def test_paths_are_ignored_while_tls_is_off(self) -> None:
        """A stale path must not block saving the step with TLS off."""
        hass = _executor_hass()
        user_input = _OCPP_INPUT | {
            "hsem_ocpp_tls_mode": "off",
            "hsem_ocpp_tls_server_cert_path": "/nonexistent.pem",
        }

        assert await validate_ocpp_step_input(hass, user_input) == {}
        hass.async_add_executor_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_paths_are_checked_in_the_executor_when_tls_is_on(
        self, tmp_path: Path
    ) -> None:
        """With TLS on, a missing file is reported against its field."""
        hass = _executor_hass()
        user_input = _OCPP_INPUT | {
            "hsem_ocpp_tls_mode": "mtls",
            "hsem_ocpp_tls_client_ca_path": f"  {tmp_path / 'ca.pem'}  ",
        }

        errors = await validate_ocpp_step_input(hass, user_input)

        assert errors == {"hsem_ocpp_tls_client_ca_path": "tls_file_not_found"}
        hass.async_add_executor_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_paths_pass_with_tls_on(self) -> None:
        """Empty paths mean generated certificates, which need no check."""
        errors = await validate_ocpp_step_input(
            _executor_hass(), _OCPP_INPUT | {"hsem_ocpp_tls_mode": "tls"}
        )

        assert errors == {}


class TestConfigReader:
    """TLS settings are read into SensorConfig."""

    def test_defaults(self) -> None:
        """An entry without TLS keys reads as TLS off: today's behaviour."""
        entry = MagicMock()
        entry.options = {}
        entry.data = {}

        cfg = build_sensor_config(entry)

        assert DEFAULT_CONFIG_VALUES["hsem_ocpp_tls_mode"] == "off"
        assert cfg.ocpp_tls_mode == "off"
        assert cfg.ocpp_tls_server_cert_path == ""
        assert cfg.ocpp_tls_server_key_path == ""
        assert cfg.ocpp_tls_client_ca_path == ""

    def test_values_are_read_and_paths_trimmed(self) -> None:
        """Stray whitespace around a path is not part of the path."""
        entry = MagicMock()
        entry.data = {}
        entry.options = {
            "hsem_ocpp_tls_mode": "mtls",
            "hsem_ocpp_tls_server_cert_path": " /ssl/cert.pem ",
            "hsem_ocpp_tls_server_key_path": "/ssl/key.pem",
            "hsem_ocpp_tls_client_ca_path": "/ssl/ca.pem\n",
        }

        cfg = build_sensor_config(entry)

        assert cfg.ocpp_tls_mode == "mtls"
        assert cfg.ocpp_tls_server_cert_path == "/ssl/cert.pem"
        assert cfg.ocpp_tls_server_key_path == "/ssl/key.pem"
        assert cfg.ocpp_tls_client_ca_path == "/ssl/ca.pem"


def _coordinator(options: dict[str, Any]) -> Any:
    """Return a real coordinator with a task-recording mock hass."""
    hass = MagicMock()
    hass.async_create_task = MagicMock(return_value=MagicMock())
    return make_real_coordinator(options, hass=hass)


_INFO = OCPPTLSInfo(
    mode="mtls",
    source="generated",
    server_cert_expiry=datetime(2027, 1, 1, tzinfo=UTC),
    server_cert_sans=("192.168.1.5",),
    ca_fingerprint_sha256="AA:BB",
)


class TestServerStartup:
    """Both servers share one TLS context and never fall back to ws://."""

    @pytest.mark.asyncio
    async def test_tls_off_starts_plain_servers(self) -> None:
        """With TLS off nothing is prepared and no context is passed."""
        coordinator = _coordinator(_BOTH_SERVERS)
        prepare = AsyncMock()

        with (
            patch(
                f"{_LIFECYCLE}.OCPPServer", return_value=MagicMock(start=AsyncMock())
            ) as server_cls,
            patch(f"{_LIFECYCLE}.async_prepare_ocpp_tls", prepare),
            patch(f"{_LIFECYCLE}.async_track_time_change"),
            patch(f"{_LIFECYCLE}.async_track_time_interval"),
        ):
            await coordinator.async_setup()

        prepare.assert_not_called()
        assert [c.kwargs["ssl_context"] for c in server_cls.call_args_list] == [
            None,
            None,
        ]
        assert coordinator._ocpp_tls_status == {"mode": "off"}

    @pytest.mark.asyncio
    async def test_tls_context_is_shared_by_both_servers(self) -> None:
        """One prepared context goes to the primary and second server."""
        coordinator = _coordinator(_BOTH_SERVERS | {"hsem_ocpp_tls_mode": "mtls"})
        context = MagicMock()
        prepare = AsyncMock(return_value=(context, _INFO))

        with (
            patch(
                f"{_LIFECYCLE}.OCPPServer", return_value=MagicMock(start=AsyncMock())
            ) as server_cls,
            patch(f"{_LIFECYCLE}.async_prepare_ocpp_tls", prepare),
            patch(f"{_LIFECYCLE}.async_track_time_change"),
            patch(f"{_LIFECYCLE}.async_track_time_interval"),
        ):
            await coordinator.async_setup()

        prepare.assert_awaited_once()
        assert prepare.call_args.args[1].mode == "mtls"
        assert [c.kwargs["ssl_context"] for c in server_cls.call_args_list] == [
            context,
            context,
        ]
        assert coordinator._ocpp_tls_status == _INFO.as_dict()

    @pytest.mark.asyncio
    async def test_failed_tls_setup_starts_no_server(self) -> None:
        """A TLS failure leaves both servers stopped and records the error."""
        coordinator = _coordinator(_BOTH_SERVERS | {"hsem_ocpp_tls_mode": "tls"})
        log = MagicMock()

        with (
            patch(f"{_LIFECYCLE}.OCPPServer") as server_cls,
            patch(
                f"{_LIFECYCLE}.async_prepare_ocpp_tls",
                AsyncMock(side_effect=OCPPTLSError("bad key")),
            ),
            patch(f"{_LIFECYCLE}.async_track_time_change"),
            patch(f"{_LIFECYCLE}.async_track_time_interval"),
            patch(f"{_LIFECYCLE}.async_log", log),
        ):
            await coordinator.async_setup()

        server_cls.assert_not_called()
        assert coordinator._ocpp_server is None
        assert coordinator._ocpp_second_server is None
        assert coordinator._ocpp_tls_status == {"mode": "tls", "error": "bad key"}
        logged = [call.args[1] for call in log.call_args_list]
        assert "OCPP TLS setup failed, OCPP server(s) not started: %s" in logged

    @pytest.mark.asyncio
    async def test_tls_is_not_prepared_without_an_enabled_server(self) -> None:
        """TLS on but OCPP off: no certificates are generated."""
        coordinator = _coordinator(
            {"hsem_ocpp_enabled": False, "hsem_ocpp_tls_mode": "tls"}
        )
        prepare = AsyncMock()

        with (
            patch(f"{_LIFECYCLE}.async_prepare_ocpp_tls", prepare),
            patch(f"{_LIFECYCLE}.async_track_time_change"),
            patch(f"{_LIFECYCLE}.async_track_time_interval"),
        ):
            await coordinator.async_setup()

        prepare.assert_not_called()


def _service_hass(tmp_path: Path, options: dict[str, Any] | None = None) -> MagicMock:
    """Return a mock hass with a real config folder and inline executor."""
    hass = _executor_hass()
    hass.config.config_dir = str(tmp_path / "config")
    entry = MagicMock()
    entry.data = {}
    entry.options = options or {}
    hass.config_entries.async_entries.return_value = [entry]
    return hass


def _call(hass: MagicMock, data: dict[str, Any]) -> ServiceCall:
    """Return a service call with schema-validated data."""
    return ServiceCall(
        hass=hass,  # type: ignore[arg-type]
        domain=DOMAIN,
        service="ocpp_generate_client_certificate",
        data=SCHEMA_OCPP_GENERATE_CLIENT_CERTIFICATE(data),
    )


@pytest.fixture
def _no_ssl_folder(tmp_path: Path) -> Iterator[None]:
    """Make the tests independent of a real /ssl on the machine."""
    with patch.object(ocpp_tls, "HA_SSL_DIR", tmp_path / "no-ssl"):
        yield


@pytest.mark.usefixtures("_no_ssl_folder")
class TestClientCertificateService:
    """The service signs with HSEM's CA and returns paths only."""

    @pytest.mark.asyncio
    async def test_issues_a_certificate(self, tmp_path: Path) -> None:
        """Files land under <config>/ssl/hsem/ocpp/clients; paths come back."""
        hass = _service_hass(tmp_path)

        result = await async_handle_ocpp_generate_client_certificate(
            _call(hass, {"cpid": "CP1"})
        )

        assert result is not None
        tls_dir = tmp_path / "config" / "ssl" / "hsem" / "ocpp"
        assert result == {
            "certificate_path": str(tls_dir / "clients" / "CP1.crt"),
            "key_path": str(tls_dir / "clients" / "CP1.key"),
            "ca_certificate_path": str(tls_dir / "ca.crt"),
        }
        cert = x509.load_pem_x509_certificate(
            (tls_dir / "clients" / "CP1.crt").read_bytes()
        )
        assert (
            cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "CP1"
        )
        assert "PRIVATE KEY" not in str(result)

    @pytest.mark.asyncio
    async def test_existing_certificate_needs_replace(self, tmp_path: Path) -> None:
        """A second call fails until replace is set."""
        hass = _service_hass(tmp_path)
        await async_handle_ocpp_generate_client_certificate(
            _call(hass, {"cpid": "CP1"})
        )

        with pytest.raises(ServiceValidationError, match="already exists"):
            await async_handle_ocpp_generate_client_certificate(
                _call(hass, {"cpid": "CP1"})
            )
        result = await async_handle_ocpp_generate_client_certificate(
            _call(hass, {"cpid": "CP1", "replace": True})
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_refused_with_a_custom_client_ca(self, tmp_path: Path) -> None:
        """HSEM cannot sign for a CA whose key it does not hold."""
        hass = _service_hass(
            tmp_path, {"hsem_ocpp_tls_client_ca_path": "/ssl/my-ca.pem"}
        )

        with pytest.raises(ServiceValidationError, match="custom client CA"):
            await async_handle_ocpp_generate_client_certificate(
                _call(hass, {"cpid": "CP1"})
            )
        assert not (tmp_path / "config").exists()

    @pytest.mark.asyncio
    async def test_write_failure_is_a_service_error(self, tmp_path: Path) -> None:
        """A folder that cannot be written surfaces as HomeAssistantError."""
        hass = _service_hass(tmp_path)

        with (
            patch(
                "custom_components.hsem.services_ocpp_tls.issue_client_certificate",
                side_effect=PermissionError("read-only"),
            ),
            pytest.raises(HomeAssistantError, match="read-only"),
        ):
            await async_handle_ocpp_generate_client_certificate(
                _call(hass, {"cpid": "CP1"})
            )

    @pytest.mark.parametrize("cpid", ["../evil", "a/b", ".hidden", ""])
    def test_schema_rejects_unsafe_identities(self, cpid: str) -> None:
        """Path-like identities never reach the filesystem."""
        with pytest.raises(Exception, match="match"):
            SCHEMA_OCPP_GENERATE_CLIENT_CERTIFICATE({"cpid": cpid})


class TestDiagnostics:
    """Diagnostics carry the TLS facts when the caller has them."""

    def test_ocpp_tls_block(self) -> None:
        """The block is added as-is, and omitted when not given."""
        inp = make_summer_day_input()
        output = run_planner(inp)

        with_tls = build_diagnostics_dump(inp, output, ocpp_tls=_INFO.as_dict())
        without = build_diagnostics_dump(inp, output)

        assert with_tls["ocpp_tls"] == {
            "mode": "mtls",
            "source": "generated",
            "server_cert_expiry": "2027-01-01T00:00:00+00:00",
            "server_cert_sans": ["192.168.1.5"],
            "ca_fingerprint_sha256": "AA:BB",
        }
        assert "ocpp_tls" not in without
        # A mock coordinator's attribute must not end up in the dump.
        mocked = build_diagnostics_dump(inp, output, ocpp_tls=MagicMock())
        assert "ocpp_tls" not in mocked
