"""Suite-wide safety boundaries for tests.

Tests must never inherit production credentials or contact remote services.  Loopback is
kept available for local HTTP clients/servers; every non-loopback socket connection is
rejected before the OS performs it.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import tempfile


# Apply before test modules import ``server``.  In particular, GOOGLE_CREDENTIALS is
# deliberately present-but-invalid so server import cannot fall back to google_key.json.
os.environ.update(
    {
        "APP_ENV": "legacy",
        "OPENAI_API_KEY": "sk-test-no-network",
        "LINE_CHANNEL_ACCESS_TOKEN": "test-line-token-no-network",
        "LINE_CHANNEL_SECRET": "test-line-secret-no-network",
        "GOOGLE_CREDENTIALS": "{}",
        "SPREADSHEET_ID": "test-spreadsheet-no-network",
        "GOOGLE_APPLICATION_CREDENTIALS": "",
        "DATA_DIR": tempfile.mkdtemp(prefix="defer-cutoff-tests-"),
        "ENABLE_SCHEDULER": "false",
        # Do not permit an inherited proxy to tunnel a nominally local connection.
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "all_proxy": "",
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }
)


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    text = str(host).strip("[]").lower()
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_GETADDRINFO = socket.getaddrinfo


def _checked_address(address):
    # AF_UNIX addresses are local filesystem paths, not host/port tuples.
    if isinstance(address, tuple) and address and not _is_loopback_host(address[0]):
        raise RuntimeError(
            f"external network is disabled during tests (host={address[0]!r})"
        )


def _guarded_connect(sock, address):
    _checked_address(address)
    return _REAL_CONNECT(sock, address)


def _guarded_connect_ex(sock, address):
    _checked_address(address)
    return _REAL_CONNECT_EX(sock, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    # Reject before libc can perform an external DNS lookup.
    if host is not None and not _is_loopback_host(host):
        raise RuntimeError(
            f"external network is disabled during tests (host={host!r})"
        )
    return _REAL_GETADDRINFO(host, *args, **kwargs)


def pytest_configure(config):
    """Install the guard before test-module collection/import begins."""
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo


def pytest_unconfigure(config):
    socket.socket.connect = _REAL_CONNECT
    socket.socket.connect_ex = _REAL_CONNECT_EX
    socket.getaddrinfo = _REAL_GETADDRINFO
