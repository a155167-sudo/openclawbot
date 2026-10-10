import socket

import pytest


def test_external_network_is_blocked_before_connect():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="external network is disabled"):
            sock.connect(("192.0.2.1", 443))
    finally:
        sock.close()


def test_external_dns_is_blocked_before_resolution():
    with pytest.raises(RuntimeError, match="external network is disabled"):
        socket.getaddrinfo("example.com", 443)


def test_loopback_connections_are_not_rejected_by_network_guard():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    accepted = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        accepted, _address = listener.accept()
    finally:
        if accepted is not None:
            accepted.close()
        client.close()
        listener.close()
