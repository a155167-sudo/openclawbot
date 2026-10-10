"""Hermetic boundary installed before test-module collection/import."""
from __future__ import annotations

import ipaddress
import os
import socket

for key in (
    "OPENAI_API_KEY", "GOOGLE_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS",
    "LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "SPREADSHEET_ID",
):
    os.environ[key] = ""
for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ[key] = ""
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_GETADDRINFO = socket.getaddrinfo


def _loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    text = str(host).strip("[]").lower()
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _check(address):
    if isinstance(address, tuple) and address and not _loopback(address[0]):
        raise RuntimeError(f"external network disabled in tests: {address[0]!r}")


def _connect(sock, address):
    _check(address)
    return _REAL_CONNECT(sock, address)


def _connect_ex(sock, address):
    _check(address)
    return _REAL_CONNECT_EX(sock, address)


def _getaddrinfo(host, *args, **kwargs):
    if host is not None and not _loopback(host):
        raise RuntimeError(f"external DNS disabled in tests: {host!r}")
    return _REAL_GETADDRINFO(host, *args, **kwargs)


def pytest_configure(config):
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.getaddrinfo = _getaddrinfo


def pytest_unconfigure(config):
    socket.socket.connect = _REAL_CONNECT
    socket.socket.connect_ex = _REAL_CONNECT_EX
    socket.getaddrinfo = _REAL_GETADDRINFO
