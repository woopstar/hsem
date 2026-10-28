"""Tests for the OCPP server's TLS material (issue #1341).

Covers where generated files go, CA generation and reuse, server
certificate rotation, client certificate signing, context building, the
config-flow path checks and the client identity helpers. Real keys and
certificates are generated throughout; nothing about the crypto is mocked.
"""

from __future__ import annotations

import ipaddress
import shutil
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from custom_components.hsem.utils import ocpp_tls
from custom_components.hsem.utils.ocpp_tls import (
    CA_CERT_FILE,
    CA_KEY_FILE,
    CLIENTS_DIR,
    SERVER_CERT_FILE,
    SERVER_KEY_FILE,
    OCPPTLSError,
    OCPPTLSInfo,
    OCPPTLSSettings,
    async_prepare_ocpp_tls,
    async_server_names,
    certificate_fingerprint,
    certificate_sans,
    cpid_matches_common_name,
    ensure_ca,
    ensure_server_certificate,
    issue_client_certificate,
    peer_common_name,
    prepare_ocpp_tls,
    resolve_tls_dir,
    validate_tls_paths,
)

_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
_NAMES = ["localhost", "127.0.0.1"]


def _cert(path: Path) -> x509.Certificate:
    """Load a PEM certificate."""
    return x509.load_pem_x509_certificate(path.read_bytes())


@pytest.fixture(scope="module")
def _seed_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate a CA and server certificate once; RSA key generation is slow."""
    tls_dir = tmp_path_factory.mktemp("seed") / "hsem" / "ocpp"
    ensure_server_certificate(tls_dir, _NAMES, _NOW)
    return tls_dir


@pytest.fixture
def tls_dir(_seed_dir: Path, tmp_path: Path) -> Path:
    """Return a private copy of the seeded TLS folder."""
    target = tmp_path / "hsem" / "ocpp"
    shutil.copytree(_seed_dir, target)
    return target


class TestResolveTlsDir:
    """Generated files go to /ssl/hsem/ocpp, or the config folder without /ssl."""

    def test_uses_the_ssl_folder_when_it_exists(self, tmp_path: Path) -> None:
        """An existing SSL folder gets an hsem/ocpp subfolder."""
        ssl_root = tmp_path / "ssl"
        ssl_root.mkdir()

        assert resolve_tls_dir(tmp_path / "config", ssl_root) == (
            ssl_root / "hsem" / "ocpp"
        )

    def test_falls_back_to_the_config_folder(self, tmp_path: Path) -> None:
        """Without /ssl the files go under <config>/ssl/hsem/ocpp."""
        config_dir = tmp_path / "config"

        assert resolve_tls_dir(config_dir, tmp_path / "missing") == (
            config_dir / "ssl" / "hsem" / "ocpp"
        )

    def test_defaults_to_home_assistants_ssl_folder(self, tmp_path: Path) -> None:
        """With no override the module's HA_SSL_DIR is consulted."""
        ssl_root = tmp_path / "ha_ssl"
        ssl_root.mkdir()

        with patch.object(ocpp_tls, "HA_SSL_DIR", ssl_root):
            assert resolve_tls_dir(tmp_path) == ssl_root / "hsem" / "ocpp"


class TestEnsureCa:
    """The CA is generated once, private, and never overwritten."""

    def test_generates_a_private_ca(self, tmp_path: Path) -> None:
        """A new CA is a CA, and its key and folder are owner-only."""
        tls_dir = tmp_path / "hsem" / "ocpp"

        cert, _key = ensure_ca(tls_dir, _NOW)

        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert constraints.value.ca is True
        assert (tls_dir / CA_KEY_FILE).stat().st_mode & 0o777 == 0o600
        assert (tls_dir / CA_CERT_FILE).stat().st_mode & 0o777 == 0o644
        assert tls_dir.stat().st_mode & 0o777 == 0o700
        assert not list(tls_dir.glob(".*.tmp"))

    def test_reuses_an_existing_ca_byte_for_byte(self, tls_dir: Path) -> None:
        """A second call loads the CA rather than replacing it."""
        before = (tls_dir / CA_CERT_FILE).read_bytes()
        key_before = (tls_dir / CA_KEY_FILE).read_bytes()

        cert, _key = ensure_ca(tls_dir, _NOW + timedelta(days=400))

        assert (tls_dir / CA_CERT_FILE).read_bytes() == before
        assert (tls_dir / CA_KEY_FILE).read_bytes() == key_before
        assert certificate_fingerprint(cert) == certificate_fingerprint(
            _cert(tls_dir / CA_CERT_FILE)
        )

    @pytest.mark.parametrize("left_over", [CA_CERT_FILE, CA_KEY_FILE])
    def test_half_a_ca_is_an_error(self, tls_dir: Path, left_over: str) -> None:
        """One CA file without the other is reported, not silently replaced."""
        other = CA_KEY_FILE if left_over == CA_CERT_FILE else CA_CERT_FILE
        (tls_dir / other).unlink()

        with pytest.raises(OCPPTLSError, match="Incomplete OCPP CA"):
            ensure_ca(tls_dir, _NOW)
        assert (tls_dir / left_over).exists()

    def test_an_expired_ca_is_an_error(self, tls_dir: Path) -> None:
        """An expired CA is reported with how to replace it."""
        with pytest.raises(OCPPTLSError, match="has expired"):
            ensure_ca(tls_dir, _NOW + timedelta(days=3651))


class TestEnsureServerCertificate:
    """The generated server certificate is reused until it is due."""

    def test_names_usage_and_issuer(self, tls_dir: Path) -> None:
        """SANs, server-auth usage and the CA signature are all in place."""
        cert = _cert(tls_dir / SERVER_CERT_FILE)
        ca = _cert(tls_dir / CA_CERT_FILE)

        assert set(certificate_sans(cert)) == {"localhost", "127.0.0.1"}
        usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        assert ExtendedKeyUsageOID.SERVER_AUTH in usage.value
        cert.verify_directly_issued_by(ca)
        assert (tls_dir / SERVER_KEY_FILE).stat().st_mode & 0o777 == 0o600
        assert cert.not_valid_after_utc == _NOW + timedelta(days=365)

    def test_reused_while_current(self, tls_dir: Path) -> None:
        """A valid certificate with the same names is kept."""
        before = _cert(tls_dir / SERVER_CERT_FILE)

        cert = ensure_server_certificate(
            tls_dir, ["127.0.0.1", "localhost"], _NOW + timedelta(days=300)
        )

        assert cert.serial_number == before.serial_number

    def test_rotated_within_thirty_days_of_expiry(self, tls_dir: Path) -> None:
        """Near expiry a new certificate is issued from the same CA."""
        ca_before = (tls_dir / CA_CERT_FILE).read_bytes()
        before = _cert(tls_dir / SERVER_CERT_FILE)
        later = _NOW + timedelta(days=340)

        cert = ensure_server_certificate(tls_dir, _NAMES, later)

        assert cert.serial_number != before.serial_number
        assert cert.not_valid_after_utc == later + timedelta(days=365)
        assert (tls_dir / CA_CERT_FILE).read_bytes() == ca_before
        cert.verify_directly_issued_by(_cert(tls_dir / CA_CERT_FILE))

    def test_rotated_when_the_host_names_change(self, tls_dir: Path) -> None:
        """A new LAN IP gets a certificate that names it."""
        cert = ensure_server_certificate(tls_dir, ["192.168.1.20"], _NOW)

        assert certificate_sans(cert) == ("192.168.1.20",)
        assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
            "192.168.1.20"
        )

    def test_rotated_when_the_ca_was_replaced(self, tls_dir: Path) -> None:
        """A certificate from a deleted CA is reissued from the new one."""
        (tls_dir / CA_CERT_FILE).unlink()
        (tls_dir / CA_KEY_FILE).unlink()

        cert = ensure_server_certificate(tls_dir, _NAMES, _NOW)

        cert.verify_directly_issued_by(_cert(tls_dir / CA_CERT_FILE))

    def test_needs_at_least_one_name(self, tmp_path: Path) -> None:
        """A certificate with no name to verify against is refused."""
        with pytest.raises(OCPPTLSError, match="No host name"):
            ensure_server_certificate(tmp_path, [], _NOW)


class TestIssueClientCertificate:
    """Client certificates carry the CPID as CN and are signed by the CA."""

    def test_signs_a_client_certificate(self, tls_dir: Path) -> None:
        """CN, client-auth usage, CA signature and key permissions."""
        paths = issue_client_certificate(tls_dir, "CP-1", _NOW)

        cert = _cert(paths.certificate)
        assert paths.certificate == tls_dir / CLIENTS_DIR / "CP-1.crt"
        assert paths.ca_certificate == tls_dir / CA_CERT_FILE
        assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
            "CP-1"
        )
        usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        assert ExtendedKeyUsageOID.CLIENT_AUTH in usage.value
        cert.verify_directly_issued_by(_cert(tls_dir / CA_CERT_FILE))
        assert paths.key.stat().st_mode & 0o777 == 0o600

    def test_existing_certificate_is_kept_unless_replaced(self, tls_dir: Path) -> None:
        """A second request fails; replace=True issues a new one."""
        first = issue_client_certificate(tls_dir, "CP1", _NOW)
        serial = _cert(first.certificate).serial_number

        with pytest.raises(FileExistsError):
            issue_client_certificate(tls_dir, "CP1", _NOW)
        assert _cert(first.certificate).serial_number == serial

        second = issue_client_certificate(tls_dir, "CP1", _NOW, replace=True)
        assert _cert(second.certificate).serial_number != serial

    @pytest.mark.parametrize("cpid", ["../evil", "a/b", ".hidden", "", "x" * 65])
    def test_unsafe_identities_are_refused(self, tls_dir: Path, cpid: str) -> None:
        """An identity that is not a plain file name is never used as one."""
        with pytest.raises(OCPPTLSError, match="Invalid charge point identity"):
            issue_client_certificate(tls_dir, cpid, _NOW)
        assert not (tls_dir / CLIENTS_DIR).exists()


class TestPrepareOcppTls:
    """Settings resolve to a server context and the facts diagnostics show."""

    def test_generated_tls(self, tls_dir: Path) -> None:
        """TLS with no paths uses the generated files and needs no client cert."""
        context, info = prepare_ocpp_tls(
            OCPPTLSSettings(mode="tls"), tls_dir, _NAMES, _NOW
        )

        assert context.verify_mode == ssl.CERT_NONE
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
        assert info.source == "generated"
        assert info.ca_fingerprint_sha256 == certificate_fingerprint(
            _cert(tls_dir / CA_CERT_FILE)
        )
        assert set(info.server_cert_sans) == set(_NAMES)

    def test_generated_mtls_requires_client_certificates(self, tls_dir: Path) -> None:
        """Mutual TLS without a CA path trusts HSEM's own CA."""
        context, info = prepare_ocpp_tls(
            OCPPTLSSettings(mode="mtls"), tls_dir, _NAMES, _NOW
        )

        assert context.verify_mode == ssl.CERT_REQUIRED
        assert info.mode == "mtls"
        assert info.ca_fingerprint_sha256 == certificate_fingerprint(
            _cert(tls_dir / CA_CERT_FILE)
        )

    def test_user_files(self, tls_dir: Path, tmp_path: Path) -> None:
        """User-supplied files are used as-is and generate nothing."""
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        shutil.copy(tls_dir / SERVER_CERT_FILE, user_dir / "fullchain.pem")
        shutil.copy(tls_dir / SERVER_KEY_FILE, user_dir / "privkey.pem")
        shutil.copy(tls_dir / CA_CERT_FILE, user_dir / "clients-ca.pem")
        empty_dir = tmp_path / "unused"

        _context, info = prepare_ocpp_tls(
            OCPPTLSSettings(
                mode="tls",
                server_cert_path=str(user_dir / "fullchain.pem"),
                server_key_path=str(user_dir / "privkey.pem"),
            ),
            empty_dir,
            _NAMES,
            _NOW,
        )
        assert info.source == "user"
        assert info.ca_fingerprint_sha256 is None
        assert not empty_dir.exists()

        context, info = prepare_ocpp_tls(
            OCPPTLSSettings(
                mode="mtls",
                server_cert_path=str(user_dir / "fullchain.pem"),
                server_key_path=str(user_dir / "privkey.pem"),
                client_ca_path=str(user_dir / "clients-ca.pem"),
            ),
            empty_dir,
            _NAMES,
            _NOW,
        )
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert info.ca_fingerprint_sha256 == certificate_fingerprint(
            _cert(user_dir / "clients-ca.pem")
        )
        assert not empty_dir.exists()

    def test_user_certificate_near_expiry_is_logged(self, tls_dir: Path) -> None:
        """HSEM does not rotate a user's certificate, so it warns instead."""
        settings = OCPPTLSSettings(
            mode="tls",
            server_cert_path=str(tls_dir / SERVER_CERT_FILE),
            server_key_path=str(tls_dir / SERVER_KEY_FILE),
        )

        with patch.object(ocpp_tls, "_LOGGER") as logger:
            prepare_ocpp_tls(settings, tls_dir, _NAMES, _NOW + timedelta(days=350))

        assert "renew it" in logger.warning.call_args.args[0]

    @pytest.mark.parametrize(
        ("settings", "message"),
        [
            (OCPPTLSSettings(mode="off"), "Unknown OCPP TLS mode"),
            (OCPPTLSSettings(mode="bogus"), "Unknown OCPP TLS mode"),
            (
                OCPPTLSSettings(mode="tls", server_cert_path="/x.pem"),
                "Set both the server certificate",
            ),
            (
                OCPPTLSSettings(
                    mode="tls",
                    server_cert_path="/nonexistent/cert.pem",
                    server_key_path="/nonexistent/key.pem",
                ),
                "Cannot load OCPP TLS files",
            ),
            (
                OCPPTLSSettings(mode="mtls", client_ca_path="/nonexistent/ca.pem"),
                "Cannot load OCPP TLS files",
            ),
        ],
    )
    def test_errors(
        self, tls_dir: Path, settings: OCPPTLSSettings, message: str
    ) -> None:
        """Every failure surfaces as OCPPTLSError, never a half-built context."""
        with pytest.raises(OCPPTLSError, match=message):
            prepare_ocpp_tls(settings, tls_dir, _NAMES, _NOW)

    def test_info_is_json_safe(self) -> None:
        """The diagnostics dict holds plain values and no key material."""
        info = OCPPTLSInfo(
            mode="tls",
            source="generated",
            server_cert_expiry=_NOW,
            server_cert_sans=("localhost",),
            ca_fingerprint_sha256="AA:BB",
        )

        assert info.as_dict() == {
            "mode": "tls",
            "source": "generated",
            "server_cert_expiry": "2026-10-28T12:00:00+00:00",
            "server_cert_sans": ["localhost"],
            "ca_fingerprint_sha256": "AA:BB",
        }


class TestValidateTlsPaths:
    """Config-flow checks of user-supplied files."""

    def test_empty_paths_are_fine(self) -> None:
        """Empty means "use the generated files"."""
        assert validate_tls_paths("", "", "") == {}

    def test_valid_files(self, tls_dir: Path) -> None:
        """A matching certificate and key plus a PEM CA pass."""
        assert (
            validate_tls_paths(
                str(tls_dir / SERVER_CERT_FILE),
                str(tls_dir / SERVER_KEY_FILE),
                str(tls_dir / CA_CERT_FILE),
            )
            == {}
        )

    def test_certificate_and_key_come_in_pairs(self, tls_dir: Path) -> None:
        """The missing half of the pair is the field flagged."""
        cert = str(tls_dir / SERVER_CERT_FILE)
        key = str(tls_dir / SERVER_KEY_FILE)

        assert validate_tls_paths(cert, "", "") == {
            "hsem_ocpp_tls_server_key_path": "tls_cert_key_pair_required"
        }
        assert validate_tls_paths("", key, "") == {
            "hsem_ocpp_tls_server_cert_path": "tls_cert_key_pair_required"
        }

    def test_missing_files(self, tmp_path: Path) -> None:
        """Every path that is not a readable file is flagged."""
        missing = str(tmp_path / "missing.pem")

        assert validate_tls_paths(missing, missing, missing) == {
            "hsem_ocpp_tls_server_cert_path": "tls_file_not_found",
            "hsem_ocpp_tls_server_key_path": "tls_file_not_found",
            "hsem_ocpp_tls_client_ca_path": "tls_file_not_found",
        }

    def test_mismatched_key_and_invalid_ca(self, tls_dir: Path, tmp_path: Path) -> None:
        """A key from another certificate and a non-PEM CA are both caught."""
        other = issue_client_certificate(tls_dir, "other", _NOW)
        garbage = tmp_path / "ca.pem"
        garbage.write_text("not a certificate")

        assert validate_tls_paths(
            str(tls_dir / SERVER_CERT_FILE), str(other.key), str(garbage)
        ) == {
            "hsem_ocpp_tls_server_key_path": "tls_cert_key_mismatch",
            "hsem_ocpp_tls_client_ca_path": "tls_invalid_ca",
        }


class TestClientIdentity:
    """The client certificate's CN must name the connecting charge point."""

    def test_common_name_from_peercert(self) -> None:
        """The CN is read from getpeercert()'s nested subject tuples."""
        peercert = {
            "subject": (
                (("organizationName", "Vendor"),),
                (("commonName", "CP1"),),
            )
        }

        assert peer_common_name(peercert) == "CP1"
        assert peer_common_name({"subject": ()}) is None
        assert peer_common_name({}) is None
        assert peer_common_name(None) is None

    @pytest.mark.parametrize(
        ("cpid", "common_name", "matches"),
        [
            ("CP1", "CP1", True),
            ("ocpp/CP1", "CP1", True),
            ("CP1", "CP2", False),
            ("CP1", "cp1", False),
            ("CP1/extra", "CP1", False),
            ("CP1", None, False),
        ],
    )
    def test_cpid_matching(
        self, cpid: str, common_name: str | None, matches: bool
    ) -> None:
        """Only an exact match with the last path segment is accepted."""
        assert cpid_matches_common_name(cpid, common_name) is matches


class TestAsyncEntryPoints:
    """Host-name discovery and the executor hand-off."""

    @pytest.mark.asyncio
    async def test_server_names(self) -> None:
        """URL host, host name and LAN IPs; loopback and scopes dropped."""
        hass = MagicMock()
        hass.config.internal_url = "http://homeassistant.local:8123"
        ips = [
            ipaddress.ip_address("192.168.1.5"),
            ipaddress.ip_address("127.0.0.1"),
            ipaddress.ip_address("fe80::1%eth0"),
            ipaddress.ip_address("192.168.1.5"),
        ]

        with (
            patch.object(ocpp_tls.socket, "gethostname", return_value="ha-box"),
            patch.object(
                ocpp_tls.network,
                "async_get_enabled_source_ips",
                AsyncMock(return_value=ips),
            ),
        ):
            names = await async_server_names(hass)

        assert names == ["homeassistant.local", "ha-box", "192.168.1.5", "fe80::1"]

    @pytest.mark.asyncio
    async def test_server_names_fall_back_to_localhost(self) -> None:
        """With nothing discoverable the certificate still gets a name."""
        hass = MagicMock()
        hass.config.internal_url = None

        with (
            patch.object(ocpp_tls.socket, "gethostname", side_effect=OSError),
            patch.object(
                ocpp_tls.network,
                "async_get_enabled_source_ips",
                AsyncMock(side_effect=RuntimeError("no network")),
            ),
        ):
            names = await async_server_names(hass)

        assert names == ["localhost"]

    @pytest.mark.asyncio
    async def test_prepare_runs_in_the_executor(
        self, _seed_dir: Path, tmp_path: Path
    ) -> None:
        """Generation and loading go through async_add_executor_job."""
        config_dir = tmp_path / "config"
        shutil.copytree(_seed_dir, config_dir / "ssl" / "hsem" / "ocpp")
        hass = MagicMock()
        hass.config.config_dir = str(config_dir)
        hass.async_add_executor_job = AsyncMock(
            side_effect=lambda func, *args: func(*args)
        )

        with (
            patch.object(ocpp_tls, "HA_SSL_DIR", tmp_path / "no-ssl"),
            patch.object(
                ocpp_tls, "async_server_names", AsyncMock(return_value=_NAMES)
            ),
        ):
            context, info = await async_prepare_ocpp_tls(
                hass, OCPPTLSSettings(mode="mtls")
            )

        hass.async_add_executor_job.assert_awaited_once()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert info.source == "generated"
