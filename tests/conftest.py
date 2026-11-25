"""Test-suite guard: the tests must never touch the network (only loopback, used by the visualizer tests)."""

import socket

import pytest

_real_connect = socket.socket.connect
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _guarded_connect(self, address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and (host in _LOOPBACK or host.startswith("127.")):
        return _real_connect(self, address, *args, **kwargs)
    raise RuntimeError(f"network access is not allowed in tests (tried to connect to {host!r})")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    yield


_real_getaddrinfo = socket.getaddrinfo


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host is None or host in _LOOPBACK or (isinstance(host, str) and host.startswith("127.")):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise RuntimeError(f"DNS lookups are not allowed in tests (tried to resolve {host!r})")
