import itertools
import time

import pytest

# Below the ephemeral range (32768-60999), so a port is never one a fake charger
# was handed a moment ago in another test.
_ports = itertools.count(21000)


@pytest.fixture
def listen_port() -> int:
    """A port of this test's own, so tests never share a socket with each other."""
    return next(_ports)


@pytest.fixture
def system_zone(monkeypatch):
    """Switch the process's local zone, as TZ does, for the rest of the test."""

    def set_zone(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield set_zone
    monkeypatch.undo()
    time.tzset()
