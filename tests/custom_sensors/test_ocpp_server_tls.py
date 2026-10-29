"""Real ``wss://`` connections against the embedded OCPP server (issue #1341).

Each test starts an actual :class:`OCPPServer` on a loopback port with a TLS
context built from generated certificates, then connects with an aiohttp
client the way a charger would. Covers server-only TLS, mutual TLS with a
valid client certificate, a missing certificate, a certificate from another
CA, and a certificate whose CN names a different charge point.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
import pytest_asyncio

from custom_components.hsem.custom_sensors.ocpp_server import OCPPServer
from custom_components.hsem.utils.ocpp_tls import (
    CA_CERT_FILE,
    SERVER_CERT_FILE,
    SERVER_KEY_FILE,
    ClientCertificatePaths,
    build_ssl_context,
    ensure_server_certificate,
    issue_client_certificate,
)

_NOW = datetime.now(UTC)
_SUBPROTOCOL = "ocpp1.6"


class _Material:
    """Generated certificates shared by every test in the module."""

    def __init__(self, root: Path) -> None:
        """Generate HSEM's CA, a server cert, and good and bad client certs."""
        self.tls_dir = root / "hsem"
        ensure_server_certificate(self.tls_dir, ["localhost", "127.0.0.1"], _NOW)
        self.client: ClientCertificatePaths = issue_client_certificate(
            self.tls_dir, "CP1", _NOW
        )
        # A valid certificate for a different charge point.
        self.other: ClientCertificatePaths = issue_client_certificate(
            self.tls_dir, "CP2", _NOW
        )
        # Same CN, but signed by a CA the server does not trust.
        self.foreign: ClientCertificatePaths = issue_client_certificate(
            root / "foreign", "CP1", _NOW
        )

    def server_context(self, *, mutual: bool) -> ssl.SSLContext:
        """Return the server context, requiring client certs when *mutual*."""
        return build_ssl_context(
            self.tls_dir / SERVER_CERT_FILE,
            self.tls_dir / SERVER_KEY_FILE,
            self.tls_dir / CA_CERT_FILE if mutual else None,
        )

    def client_context(
        self, client: ClientCertificatePaths | None = None
    ) -> ssl.SSLContext:
        """Return a charger-side context trusting HSEM's CA."""
        context = ssl.create_default_context(cafile=self.tls_dir / CA_CERT_FILE)
        if client is not None:
            context.load_cert_chain(client.certificate, client.key)
        return context


@pytest.fixture(scope="module")
def material(tmp_path_factory: pytest.TempPathFactory) -> _Material:
    """Generate the certificates once for the module."""
    return _Material(tmp_path_factory.mktemp("ocpp_tls"))


def _free_port() -> int:
    """Return a loopback port that is free right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _start(
    context: ssl.SSLContext | None, *, allow_cn_mismatch: bool = False
) -> OCPPServer:
    """Start an OCPP server on a free loopback port."""
    server = OCPPServer(
        hass=MagicMock(),
        host="127.0.0.1",
        port=_free_port(),
        ssl_context=context,
        allow_cn_mismatch=allow_cn_mismatch,
    )
    await server.start()
    return server


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    """Yield a client session, closed after the test."""
    async with aiohttp.ClientSession() as session:
        yield session


async def _wait_for_charger(server: OCPPServer, cpid: str) -> bool:
    """Return whether *cpid* shows up as a connected charger within 2 s."""
    for _ in range(200):
        if cpid in server.charger_sessions:
            return True
        await asyncio.sleep(0.01)
    return False


# A charger that fails the TLS handshake sees one of these, depending on
# whether the server's alert arrives during the handshake (TLS 1.2) or on
# the first read after it (TLS 1.3).
_HANDSHAKE_FAILURES = (aiohttp.ClientError, ssl.SSLError, ConnectionError)


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
class TestOcppServerTls:
    """End-to-end TLS behaviour of the OCPP WebSocket server."""

    async def test_server_tls_accepts_a_charger_without_a_client_cert(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """Server-only TLS: a charger trusting the CA connects over wss://."""
        server = await _start(material.server_context(mutual=False))
        try:
            async with http.ws_connect(
                f"wss://127.0.0.1:{server.port}/CP1",
                protocols=(_SUBPROTOCOL,),
                ssl=material.client_context(),
            ) as ws:
                assert ws.protocol == _SUBPROTOCOL
                assert await _wait_for_charger(server, "CP1")
        finally:
            await server.stop()

    async def test_server_tls_refuses_plain_ws(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """A TLS server does not speak plain ws:// at all."""
        server = await _start(material.server_context(mutual=False))
        try:
            with pytest.raises(_HANDSHAKE_FAILURES):
                async with http.ws_connect(
                    f"ws://127.0.0.1:{server.port}/CP1", protocols=(_SUBPROTOCOL,)
                ):
                    pass
            assert server.charger_sessions == {}
        finally:
            await server.stop()

    async def test_mtls_accepts_a_valid_client_certificate(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """A certificate from HSEM's CA whose CN is the CPID is accepted."""
        server = await _start(material.server_context(mutual=True))
        try:
            async with http.ws_connect(
                f"wss://127.0.0.1:{server.port}/CP1",
                protocols=(_SUBPROTOCOL,),
                ssl=material.client_context(material.client),
            ) as ws:
                assert ws.protocol == _SUBPROTOCOL
                assert await _wait_for_charger(server, "CP1")
        finally:
            await server.stop()

    async def test_mtls_matches_the_last_path_segment(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """A charger using an /ocpp/<id> base URL is matched on <id>."""
        server = await _start(material.server_context(mutual=True))
        try:
            async with http.ws_connect(
                f"wss://127.0.0.1:{server.port}/ocpp/CP1",
                protocols=(_SUBPROTOCOL,),
                ssl=material.client_context(material.client),
            ):
                assert await _wait_for_charger(server, "ocpp/CP1")
        finally:
            await server.stop()

    async def test_mtls_refuses_a_missing_client_certificate(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """No client certificate fails the TLS handshake."""
        server = await _start(material.server_context(mutual=True))
        try:
            with pytest.raises(_HANDSHAKE_FAILURES):
                async with http.ws_connect(
                    f"wss://127.0.0.1:{server.port}/CP1",
                    protocols=(_SUBPROTOCOL,),
                    ssl=material.client_context(),
                ):
                    pass
            assert server.charger_sessions == {}
        finally:
            await server.stop()

    async def test_mtls_refuses_a_certificate_from_another_ca(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """The right CN signed by an untrusted CA fails the handshake."""
        server = await _start(material.server_context(mutual=True))
        try:
            with pytest.raises(_HANDSHAKE_FAILURES):
                async with http.ws_connect(
                    f"wss://127.0.0.1:{server.port}/CP1",
                    protocols=(_SUBPROTOCOL,),
                    ssl=material.client_context(material.foreign),
                ):
                    pass
            assert server.charger_sessions == {}
        finally:
            await server.stop()

    async def test_mtls_refuses_a_cn_that_names_another_charger(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """CP1's valid certificate cannot connect as CP2: HTTP 403."""
        server = await _start(material.server_context(mutual=True))
        try:
            with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
                async with http.ws_connect(
                    f"wss://127.0.0.1:{server.port}/CP2",
                    protocols=(_SUBPROTOCOL,),
                    ssl=material.client_context(material.client),
                ):
                    pass
            assert excinfo.value.status == 403
            assert server.charger_sessions == {}
        finally:
            await server.stop()

    async def test_mtls_mismatch_does_not_displace_a_connected_charger(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """A refused CN mismatch leaves the genuine charger's session alone."""
        server = await _start(material.server_context(mutual=True))
        try:
            async with http.ws_connect(
                f"wss://127.0.0.1:{server.port}/CP1",
                protocols=(_SUBPROTOCOL,),
                ssl=material.client_context(material.client),
            ) as genuine:
                assert await _wait_for_charger(server, "CP1")
                # CP2's own, valid certificate, presented on CP1's path.
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    async with http.ws_connect(
                        f"wss://127.0.0.1:{server.port}/CP1",
                        protocols=(_SUBPROTOCOL,),
                        ssl=material.client_context(material.other),
                    ):
                        pass
                assert not genuine.closed
                assert server.charger_sessions["CP1"].websocket is not None
        finally:
            await server.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("socket_enabled")
class TestOcppServerTlsCnMismatchAllowed:
    """With the mismatch setting on, only the CN check is dropped."""

    async def test_a_cn_naming_another_charger_is_accepted(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """CP1's certificate connects as CP2, with a warning logged."""
        server = await _start(
            material.server_context(mutual=True), allow_cn_mismatch=True
        )
        try:
            with patch(f"{OCPPServer.__module__}._LOGGER") as logger:
                async with http.ws_connect(
                    f"wss://127.0.0.1:{server.port}/CP2",
                    protocols=(_SUBPROTOCOL,),
                    ssl=material.client_context(material.client),
                ) as ws:
                    assert ws.protocol == _SUBPROTOCOL
                    assert await _wait_for_charger(server, "CP2")
            warnings = [call.args[0] for call in logger.warning.call_args_list]
            assert any(
                "accepted because CN mismatches are allowed" in w for w in warnings
            )
        finally:
            await server.stop()

    async def test_the_client_ca_is_still_enforced(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """A certificate from another CA still fails the handshake."""
        server = await _start(
            material.server_context(mutual=True), allow_cn_mismatch=True
        )
        try:
            with pytest.raises(_HANDSHAKE_FAILURES):
                async with http.ws_connect(
                    f"wss://127.0.0.1:{server.port}/CP2",
                    protocols=(_SUBPROTOCOL,),
                    ssl=material.client_context(material.foreign),
                ):
                    pass
            assert server.charger_sessions == {}
        finally:
            await server.stop()

    async def test_a_missing_client_certificate_is_still_refused(
        self, material: _Material, http: aiohttp.ClientSession
    ) -> None:
        """The setting does not make client certificates optional."""
        server = await _start(
            material.server_context(mutual=True), allow_cn_mismatch=True
        )
        try:
            with pytest.raises(_HANDSHAKE_FAILURES):
                async with http.ws_connect(
                    f"wss://127.0.0.1:{server.port}/CP1",
                    protocols=(_SUBPROTOCOL,),
                    ssl=material.client_context(),
                ):
                    pass
            assert server.charger_sessions == {}
        finally:
            await server.stop()
