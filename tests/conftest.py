import itertools

import pytest

# Below the ephemeral range (32768-60999), so a port is never one a fake charger
# was handed a moment ago in another test.
_ports = itertools.count(21000)


@pytest.fixture
def listen_port() -> int:
    """A port of this test's own, so tests never share a socket with each other."""
    return next(_ports)
