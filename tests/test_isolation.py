"""Network isolation is enforced, not claimed.

pytest runs with pytest-socket's --disable-socket (pyproject addopts), so
real socket creation fails every test that tries. DNS resolution does not
go through a socket object, so the site stage's resolver is stubbed by an
autouse conftest fixture to fail loudly instead. Both belts are asserted
here so a config regression cannot silently reopen the network.
"""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError

import coldscreen.site


@pytest.mark.filterwarnings("ignore:A test tried to use socket.socket")
def test_socket_creation_is_blocked_in_tests() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_the_site_resolver_is_stubbed_to_refuse_real_dns() -> None:
    with pytest.raises(AssertionError, match="real DNS resolution attempted"):
        coldscreen.site.system_resolver("any-host.example")
