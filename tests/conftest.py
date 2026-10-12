"""Test configuration and fixtures for the HSEM test suite.

On Windows, the pytest-socket plugin (brought in by pytest-homeassistant-custom-component)
blocks all socket creation including the ``socket.socketpair()`` call that both
``ProactorEventLoop`` and ``SelectorEventLoop`` need during initialisation.  The
HA plugin allows UNIX sockets (``allow_unix_socket=True``) but on Windows the
loop uses a TCP loopback pair, not a UNIX socket.

We override the ``event_loop`` fixture here to temporarily re-enable sockets
while the event loop is being created so that the internal pipe is set up
without pytest-socket interfering.
"""

import asyncio
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_socket


@pytest.fixture
def event_loop() -> Generator[asyncio.AbstractEventLoop]:
    """Yield an asyncio event loop, temporarily enabling sockets during loop creation.

    The ProactorEventLoop / SelectorEventLoop on Windows needs to call
    ``socket.socketpair()`` as part of its internal pipe setup.  pytest-socket
    blocks this.  We re-enable sockets only for the duration of loop creation,
    then restore the blocked state immediately.
    """
    # Briefly allow socket creation so the event loop can set up its self-pipe.
    pytest_socket.enable_socket()
    try:
        # Python 3.14+ has deprecated public event loop policies.
        loop = asyncio.new_event_loop()
    finally:
        # Re-block sockets immediately after loop creation.
        pytest_socket.disable_socket(allow_unix_socket=True)

    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _performance_tracking_off_in_cycle_tests() -> Generator[AsyncMock]:
    """Keep full-cycle tests from recording into the performance tracker.

    The update cycle ends by handing a sample to the performance tracker
    (issue #1182), which persists to ``hass.config.config_dir`` and starts a
    background task.  Most cycle tests run against a ``MagicMock`` Home
    Assistant, where that would write history files under a directory named
    after the mock and leave a coroutine un-awaited.  The cycle's call is
    therefore replaced here; ``tests/test_coordinator_performance.py`` tests
    the real function directly and puts it back for its own cycle test.
    """
    with patch(
        "custom_components.hsem.coordinator_cycle.async_track_performance",
        new_callable=AsyncMock,
    ) as tracked:
        yield tracked
