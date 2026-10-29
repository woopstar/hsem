"""TLS material for the embedded OCPP server (issue #1341).

The OCPP 1.6 Security Whitepaper has the central system serve ``wss://``
with a server certificate (security profile 2) and, optionally, require
the charge point to present a client certificate (profile 3, mutual TLS).
This module covers the certificate side of that:

- **User-supplied files:** a server certificate and key, and a CA that
  client certificates must chain to.
- **Generated files:** when no paths are configured, HSEM creates a private
  CA and a server certificate signed by it, and can sign client
  certificates for chargers that need one uploaded.

Generated files live in Home Assistant's SSL folder, in a subfolder HSEM
owns (``/ssl/hsem/ocpp/``). Installs without ``/ssl`` (Container, Core) use
``<config>/ssl/hsem/ocpp/`` instead. Nothing is ever written directly into
``/ssl``. The CA is never overwritten: chargers keep trusting it across
restarts. The server certificate is HSEM's own and is replaced when it
nears expiry or its names no longer match the host.

Every function that touches the filesystem or generates keys is
synchronous and must run in the executor; :func:`async_prepare_ocpp_tls`
is the event-loop entry point. No function returns or logs key material.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from homeassistant.components import network
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.ssl import server_context_modern

from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

TLS_MODE_OFF = "off"
TLS_MODE_TLS = "tls"
TLS_MODE_MTLS = "mtls"
#: Selector options for ``hsem_ocpp_tls_mode``, in display order.
TLS_MODES: tuple[str, ...] = (TLS_MODE_OFF, TLS_MODE_TLS, TLS_MODE_MTLS)

#: Home Assistant's SSL folder on HA OS and Supervised installs.
HA_SSL_DIR = Path("/ssl")
_TLS_SUBDIR = ("hsem", "ocpp")

CA_CERT_FILE = "ca.crt"
CA_KEY_FILE = "ca.key"
SERVER_CERT_FILE = "server.crt"
SERVER_KEY_FILE = "server.key"
CLIENTS_DIR = "clients"

_CA_VALIDITY = timedelta(days=3650)
_SERVER_VALIDITY = timedelta(days=365)
_CLIENT_VALIDITY = timedelta(days=730)
#: The generated server certificate is replaced when it expires sooner.
ROTATE_BEFORE = timedelta(days=30)
# Backdate notBefore so a charger whose clock runs a little behind still
# accepts a freshly generated certificate.
_CLOCK_SKEW = timedelta(minutes=5)

# The OCPP Security Whitepaper asks for RSA keys of at least 2048 bits;
# RSA rather than EC because embedded charger TLS stacks support it widely.
_CA_KEY_BITS = 3072
_LEAF_KEY_BITS = 2048

#: Charge point identities HSEM will sign a client certificate for. The
#: identity becomes a file name, so path separators and leading dots are
#: rejected.
CPID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_CPID_RE = re.compile(CPID_PATTERN)


class OCPPTLSError(Exception):
    """TLS material for the OCPP server cannot be prepared."""


@dataclass(frozen=True)
class OCPPTLSSettings:
    """TLS settings for the OCPP server, as configured by the user.

    Attributes:
        mode: One of :data:`TLS_MODES`.
        server_cert_path: User-supplied server certificate (PEM), or ``""``
            to use the generated one.
        server_key_path: User-supplied server private key (PEM), or ``""``.
        client_ca_path: CA that client certificates must chain to in
            ``mtls`` mode, or ``""`` to use the generated CA.
    """

    mode: str
    server_cert_path: str = ""
    server_key_path: str = ""
    client_ca_path: str = ""


@dataclass(frozen=True)
class OCPPTLSInfo:
    """Public facts about the prepared TLS material, for diagnostics.

    Attributes:
        mode: The TLS mode in effect.
        source: ``"generated"`` or ``"user"`` (server certificate origin).
        server_cert_expiry: The server certificate's ``notAfter`` (UTC).
        server_cert_sans: Subject alternative names on the server
            certificate.
        ca_fingerprint_sha256: SHA-256 fingerprint of the CA chargers must
            trust (generated server certificate) or that client
            certificates must chain to (``mtls``); ``None`` when neither
            applies.
    """

    mode: str
    source: str
    server_cert_expiry: datetime
    server_cert_sans: tuple[str, ...]
    ca_fingerprint_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict for the diagnostics dump."""
        return {
            "mode": self.mode,
            "source": self.source,
            "server_cert_expiry": self.server_cert_expiry.isoformat(),
            "server_cert_sans": list(self.server_cert_sans),
            "ca_fingerprint_sha256": self.ca_fingerprint_sha256,
        }


@dataclass(frozen=True)
class ClientCertificatePaths:
    """Where a signed client certificate was written.

    Attributes:
        certificate: Client certificate (PEM).
        key: Client private key (PEM, mode ``0600``).
        ca_certificate: The CA certificate that signed it.
    """

    certificate: Path
    key: Path
    ca_certificate: Path


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def resolve_tls_dir(config_dir: Path, ssl_root: Path | None = None) -> Path:
    """Return the folder HSEM keeps generated OCPP TLS files in.

    Args:
        config_dir: Home Assistant's configuration directory.
        ssl_root: Home Assistant's SSL folder; defaults to
            :data:`HA_SSL_DIR`.

    Returns:
        ``<ssl_root>/hsem/ocpp`` when *ssl_root* exists, otherwise
        ``<config_dir>/ssl/hsem/ocpp``.
    """
    root = HA_SSL_DIR if ssl_root is None else ssl_root
    base = root if root.is_dir() else config_dir / "ssl"
    return base.joinpath(*_TLS_SUBDIR)


def _ensure_dir(path: Path) -> None:
    """Create *path* (and parents) and restrict it to the owner."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_file(path: Path, data: bytes, mode: int) -> None:
    """Write *data* to *path* atomically with permissions *mode*.

    The temporary file is created with *mode* already applied, so a private
    key is never readable by others, not even briefly.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp.chmod(mode)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    """Write an unencrypted PKCS#8 PEM private key, readable by owner only."""
    data = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _write_file(path, data, 0o600)


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    """Write a PEM certificate."""
    _write_file(path, cert.public_bytes(serialization.Encoding.PEM), 0o644)


def _load_cert(path: Path) -> x509.Certificate:
    """Load a PEM certificate from *path*."""
    return x509.load_pem_x509_certificate(path.read_bytes())


def _load_key(path: Path) -> rsa.RSAPrivateKey:
    """Load an unencrypted PEM RSA private key from *path*."""
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise OCPPTLSError(f"{path} is not an RSA private key")
    return key


# ---------------------------------------------------------------------------
# Certificate generation
# ---------------------------------------------------------------------------


def _new_key(bits: int) -> rsa.RSAPrivateKey:
    """Generate an RSA private key."""
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _san_entries(names: list[str]) -> list[x509.GeneralName]:
    """Turn host names and IP literals into SAN entries."""
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return entries


def certificate_sans(cert: x509.Certificate) -> tuple[str, ...]:
    """Return the DNS and IP subject alternative names of *cert*."""
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return ()
    dns = ext.value.get_values_for_type(x509.DNSName)
    ips = [str(ip) for ip in ext.value.get_values_for_type(x509.IPAddress)]
    return tuple(dns + ips)


def certificate_fingerprint(cert: x509.Certificate) -> str:
    """Return the colon-separated SHA-256 fingerprint of *cert*."""
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def _leaf_builder(
    subject_cn: str,
    issuer: x509.Certificate,
    public_key: rsa.RSAPublicKey,
    now: datetime,
    validity: timedelta,
    usage: x509.ObjectIdentifier,
) -> x509.CertificateBuilder:
    """Return a builder for an end-entity certificate signed by *issuer*."""
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)]))
        .issuer_name(issuer.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + validity)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(x509.ExtendedKeyUsage([usage]), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                issuer.extensions.get_extension_for_class(
                    x509.SubjectKeyIdentifier
                ).value
            ),
            False,
        )
    )


def ensure_ca(
    tls_dir: Path, now: datetime
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Load HSEM's private CA, generating it on first use.

    An existing CA is never overwritten: chargers that trust it must keep
    working. A half-present CA (certificate without key or the reverse) is
    an error rather than something to repair silently.

    Args:
        tls_dir: Folder from :func:`resolve_tls_dir`.
        now: Current time (UTC).

    Returns:
        The CA certificate and its private key.

    Raises:
        OCPPTLSError: When only one CA file exists or the CA has expired.
    """
    cert_path = tls_dir / CA_CERT_FILE
    key_path = tls_dir / CA_KEY_FILE
    if cert_path.exists() and key_path.exists():
        cert = _load_cert(cert_path)
        if cert.not_valid_after_utc <= now:
            raise OCPPTLSError(
                f"The OCPP CA certificate {cert_path} has expired; remove "
                f"{CA_CERT_FILE} and {CA_KEY_FILE} to generate a new CA"
            )
        return cert, _load_key(key_path)
    if cert_path.exists() or key_path.exists():
        raise OCPPTLSError(
            f"Incomplete OCPP CA in {tls_dir}: {CA_CERT_FILE} and {CA_KEY_FILE} "
            "must both exist; remove the remaining one to generate a new CA"
        )

    _ensure_dir(tls_dir)
    key = _new_key(_CA_KEY_BITS)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HSEM"),
            x509.NameAttribute(NameOID.COMMON_NAME, "HSEM OCPP CA"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + _CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
        )
        .sign(key, hashes.SHA256())
    )
    _write_key(key_path, key)
    _write_cert(cert_path, cert)
    _LOGGER.warning(
        "Generated a new OCPP CA certificate at %s (SHA-256 %s); install it in "
        "the charger's trust store",
        cert_path,
        certificate_fingerprint(cert),
    )
    return cert, key


def _server_cert_is_current(
    cert: x509.Certificate,
    ca_cert: x509.Certificate,
    server_names: list[str],
    now: datetime,
) -> bool:
    """Return whether the generated server certificate can be kept."""
    if cert.not_valid_after_utc - now < ROTATE_BEFORE:
        return False
    if set(certificate_sans(cert)) != set(_normalise_names(server_names)):
        return False
    try:
        cert.verify_directly_issued_by(ca_cert)
    except ValueError, TypeError, InvalidSignature:
        # Signed by a CA that has since been replaced.
        return False
    return True


def _normalise_names(names: list[str]) -> list[str]:
    """Return *names* as they read back from a certificate's SANs."""
    normalised: list[str] = []
    for name in names:
        try:
            normalised.append(str(ipaddress.ip_address(name)))
        except ValueError:
            normalised.append(name)
    return normalised


def ensure_server_certificate(
    tls_dir: Path, server_names: list[str], now: datetime
) -> x509.Certificate:
    """Return HSEM's generated server certificate, issuing a new one if due.

    A new key and certificate are issued when either file is missing, the
    certificate expires within :data:`ROTATE_BEFORE`, its SANs differ from
    *server_names*, or it was not signed by the current CA.

    Args:
        tls_dir: Folder from :func:`resolve_tls_dir`.
        server_names: Host names and IP literals the server is reached on.
        now: Current time (UTC).

    Returns:
        The server certificate in effect.
    """
    if not server_names:
        raise OCPPTLSError("No host name or IP address to put on the certificate")
    ca_cert, ca_key = ensure_ca(tls_dir, now)
    cert_path = tls_dir / SERVER_CERT_FILE
    key_path = tls_dir / SERVER_KEY_FILE
    if cert_path.exists() and key_path.exists():
        cert = _load_cert(cert_path)
        if _server_cert_is_current(cert, ca_cert, server_names, now):
            return cert

    key = _new_key(_LEAF_KEY_BITS)
    cert = (
        _leaf_builder(
            server_names[0],
            ca_cert,
            key.public_key(),
            now,
            _SERVER_VALIDITY,
            ExtendedKeyUsageOID.SERVER_AUTH,
        )
        .add_extension(x509.SubjectAlternativeName(_san_entries(server_names)), False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(key_path, key)
    _write_cert(cert_path, cert)
    _LOGGER.info(
        "Issued OCPP server certificate %s for %s, valid until %s",
        cert_path,
        ", ".join(server_names),
        cert.not_valid_after_utc.isoformat(),
    )
    return cert


def issue_client_certificate(
    tls_dir: Path, cpid: str, now: datetime, *, replace: bool = False
) -> ClientCertificatePaths:
    """Sign a client key and certificate (``CN=<cpid>``) with HSEM's CA.

    Args:
        tls_dir: Folder from :func:`resolve_tls_dir`.
        cpid: Charge point identity; must match :data:`CPID_PATTERN`.
        now: Current time (UTC).
        replace: Overwrite an existing certificate for this identity.

    Returns:
        Paths of the written files. Key contents are never returned.

    Raises:
        OCPPTLSError: When *cpid* is not a safe identity.
        FileExistsError: When a certificate for *cpid* exists and *replace*
            is ``False``.
    """
    if not _CPID_RE.fullmatch(cpid):
        raise OCPPTLSError(f"Invalid charge point identity {cpid!r}")
    ca_cert, ca_key = ensure_ca(tls_dir, now)
    clients = tls_dir / CLIENTS_DIR
    cert_path = clients / f"{cpid}.crt"
    key_path = clients / f"{cpid}.key"
    if not replace and (cert_path.exists() or key_path.exists()):
        raise FileExistsError(str(cert_path))

    _ensure_dir(clients)
    key = _new_key(_LEAF_KEY_BITS)
    cert = _leaf_builder(
        cpid,
        ca_cert,
        key.public_key(),
        now,
        _CLIENT_VALIDITY,
        ExtendedKeyUsageOID.CLIENT_AUTH,
    ).sign(ca_key, hashes.SHA256())
    _write_key(key_path, key)
    _write_cert(cert_path, cert)
    return ClientCertificatePaths(cert_path, key_path, tls_dir / CA_CERT_FILE)


# ---------------------------------------------------------------------------
# SSL context
# ---------------------------------------------------------------------------


def build_ssl_context(
    cert_path: Path, key_path: Path, client_ca_path: Path | None
) -> ssl.SSLContext:
    """Return a server ``SSLContext`` for the OCPP server.

    Built on Home Assistant's Mozilla "modern" server profile (TLS 1.2+).

    Args:
        cert_path: Server certificate (PEM; may hold the chain).
        key_path: Server private key (PEM).
        client_ca_path: When set, client certificates are required and must
            chain to this CA (mutual TLS).

    Returns:
        The configured context.
    """
    context = server_context_modern()
    context.load_cert_chain(cert_path, key_path)
    if client_ca_path is not None:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=client_ca_path)
    return context


def prepare_ocpp_tls(
    settings: OCPPTLSSettings,
    tls_dir: Path,
    server_names: list[str],
    now: datetime,
) -> tuple[ssl.SSLContext, OCPPTLSInfo]:
    """Resolve, generate if needed, and load the OCPP server's TLS material.

    Runs in the executor.

    Args:
        settings: The user's TLS settings; ``mode`` must not be ``off``.
        tls_dir: Folder from :func:`resolve_tls_dir`.
        server_names: SANs for a generated server certificate.
        now: Current time (UTC).

    Returns:
        The server ``SSLContext`` and the facts diagnostics may show.

    Raises:
        OCPPTLSError: On an unknown mode, an incomplete certificate/key
            pair, or files that cannot be read or loaded.
    """
    if settings.mode not in (TLS_MODE_TLS, TLS_MODE_MTLS):
        raise OCPPTLSError(f"Unknown OCPP TLS mode {settings.mode!r}")
    if bool(settings.server_cert_path) != bool(settings.server_key_path):
        raise OCPPTLSError(
            "Set both the server certificate and the server key path, or neither"
        )

    try:
        ca_fingerprint: str | None = None
        if settings.server_cert_path:
            source = "user"
            cert_path = Path(settings.server_cert_path)
            key_path = Path(settings.server_key_path)
            server_cert = _load_cert(cert_path)
        else:
            source = "generated"
            server_cert = ensure_server_certificate(tls_dir, server_names, now)
            cert_path = tls_dir / SERVER_CERT_FILE
            key_path = tls_dir / SERVER_KEY_FILE
            ca_fingerprint = certificate_fingerprint(_load_cert(tls_dir / CA_CERT_FILE))

        client_ca_path: Path | None = None
        if settings.mode == TLS_MODE_MTLS:
            if settings.client_ca_path:
                client_ca_path = Path(settings.client_ca_path)
            else:
                ensure_ca(tls_dir, now)
                client_ca_path = tls_dir / CA_CERT_FILE
            ca_fingerprint = certificate_fingerprint(_load_cert(client_ca_path))

        context = build_ssl_context(cert_path, key_path, client_ca_path)
    except OCPPTLSError:
        raise
    except (OSError, ValueError, ssl.SSLError) as err:
        raise OCPPTLSError(f"Cannot load OCPP TLS files: {err}") from err

    if source == "user" and server_cert.not_valid_after_utc - now < ROTATE_BEFORE:
        _LOGGER.warning(
            "OCPP server certificate %s expires on %s; renew it",
            cert_path,
            server_cert.not_valid_after_utc.isoformat(),
        )
    info = OCPPTLSInfo(
        mode=settings.mode,
        source=source,
        server_cert_expiry=server_cert.not_valid_after_utc,
        server_cert_sans=certificate_sans(server_cert),
        ca_fingerprint_sha256=ca_fingerprint,
    )
    return context, info


def validate_tls_paths(
    server_cert_path: str, server_key_path: str, client_ca_path: str
) -> dict[str, str]:
    """Check user-supplied TLS file paths for the config flow.

    Empty paths are allowed (generated material is used instead). Runs in
    the executor.

    Args:
        server_cert_path: Server certificate path, or ``""``.
        server_key_path: Server key path, or ``""``.
        client_ca_path: Client CA path, or ``""``.

    Returns:
        Field name → translation error key; empty when everything loads.
    """
    errors: dict[str, str] = {}
    if bool(server_cert_path) != bool(server_key_path):
        missing = (
            "hsem_ocpp_tls_server_key_path"
            if server_cert_path
            else "hsem_ocpp_tls_server_cert_path"
        )
        errors[missing] = "tls_cert_key_pair_required"
    for field, value in (
        ("hsem_ocpp_tls_server_cert_path", server_cert_path),
        ("hsem_ocpp_tls_server_key_path", server_key_path),
        ("hsem_ocpp_tls_client_ca_path", client_ca_path),
    ):
        if value and not Path(value).is_file():
            errors[field] = "tls_file_not_found"
    if errors:
        return errors

    if server_cert_path:
        try:
            ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(
                server_cert_path, server_key_path
            )
        except OSError, ssl.SSLError, ValueError:
            errors["hsem_ocpp_tls_server_key_path"] = "tls_cert_key_mismatch"
    if client_ca_path:
        try:
            ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_verify_locations(
                cafile=client_ca_path
            )
        except OSError, ssl.SSLError, ValueError:
            errors["hsem_ocpp_tls_client_ca_path"] = "tls_invalid_ca"
    return errors


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------


def peer_common_name(peercert: dict[str, Any] | None) -> str | None:
    """Return the subject CN from an ``SSLSocket.getpeercert()`` dict."""
    if not peercert:
        return None
    for rdn in peercert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return str(value)
    return None


def cpid_matches_common_name(cpid: str, common_name: str | None) -> bool:
    """Return whether a client certificate's CN names this charge point.

    The CPID is taken from the connection path. Chargers configured with a
    base URL such as ``wss://host:9000/ocpp/`` append their identity as the
    last segment, so the CN is compared with that segment.
    """
    return common_name is not None and common_name == cpid.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Event-loop entry point
# ---------------------------------------------------------------------------


async def async_server_names(hass: HomeAssistant) -> list[str]:
    """Return host names and LAN IPs a charger may use to reach HSEM.

    Combines the enabled network adapters' addresses, the host name of
    Home Assistant's internal URL, and the machine's host name.
    """
    names: list[str] = []
    internal_url = getattr(hass.config, "internal_url", None)
    if internal_url and (host := urlparse(internal_url).hostname):
        names.append(host)
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        names.append(hostname)
    try:
        source_ips = await network.async_get_enabled_source_ips(hass)
    except Exception as err:
        _LOGGER.debug("OCPP TLS: cannot list network adapters: %s", err)
        source_ips = []
    for ip in source_ips:
        if not ip.is_loopback:
            # Drop an IPv6 scope ("%eth0"): it cannot go in a certificate.
            names.append(str(ip).split("%", 1)[0])
    return list(dict.fromkeys(_normalise_names(names))) or ["localhost"]


async def async_prepare_ocpp_tls(
    hass: HomeAssistant, settings: OCPPTLSSettings
) -> tuple[ssl.SSLContext, OCPPTLSInfo]:
    """Prepare the OCPP server's TLS context without blocking the loop.

    Args:
        hass: The Home Assistant instance.
        settings: The user's TLS settings; ``mode`` must not be ``off``.

    Returns:
        The server ``SSLContext`` and its diagnostics facts.

    Raises:
        OCPPTLSError: When the material cannot be prepared.
    """
    server_names = await async_server_names(hass)
    config_dir = Path(hass.config.config_dir)
    return await hass.async_add_executor_job(
        _prepare_in_config_dir, settings, config_dir, server_names, dt_util.utcnow()
    )


def _prepare_in_config_dir(
    settings: OCPPTLSSettings,
    config_dir: Path,
    server_names: list[str],
    now: datetime,
) -> tuple[ssl.SSLContext, OCPPTLSInfo]:
    """Resolve the TLS folder and prepare the material, in the executor."""
    return prepare_ocpp_tls(settings, resolve_tls_dir(config_dir), server_names, now)
