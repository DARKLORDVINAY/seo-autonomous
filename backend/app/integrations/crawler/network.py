"""Public HTTPS transport with DNS validation at connect time and numeric-IP pinning.

TLS SNI and certificate checks retain the original hostname in httpcore. DNS is
resolved exactly once per new socket; only the checked numeric address connects.
No environment proxy, UNIX socket, or URL-supplied credential can bypass this.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Iterable
from urllib.parse import urldefrag, urlsplit, urlunsplit

import httpcore
import httpx


class UnsafeURL(ValueError):
    pass


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address.sixtofour is not None or address.teredo is not None or address not in ipaddress.ip_network("2000::/3"):
            # Block transition/translation ranges that may tunnel to private IPv4.
            return False
    return address.is_global and not address.is_multicast and not address.is_unspecified


def validate_url(url: str, *, origin: str | None = None, fixture: bool = False) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise UnsafeURL("URL contains invalid characters or exceeds length limit")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise UnsafeURL("Invalid URL") from exc
    if parts.scheme != "https" or not host or parts.username is not None or parts.password is not None:
        raise UnsafeURL("Only credential-free HTTPS URLs are permitted")
    if port not in (None, 443) or "%" in parts.netloc or host.endswith("."):
        raise UnsafeURL("Nonstandard ports and ambiguous hostnames are forbidden")
    host = host.lower()
    if fixture and host == "example.test":
        pass
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid", ".example", ".home", ".arpa")):
                raise UnsafeURL("Non-public hostname is forbidden")
        else:
            if not public_ip(str(address)):
                raise UnsafeURL("Non-public IP is forbidden")
    try:
        normal = str(httpx.URL(urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))))
    except (httpx.InvalidURL, ValueError) as exc:
        # Invalid IDNA and unpaired surrogates are attacker-controlled data too.
        # Callers can fail closed on one public exception without logging URLs.
        raise UnsafeURL("URL cannot be safely encoded") from exc
    if origin:
        expected = urlsplit(origin)
        actual = urlsplit(normal)
        if (actual.scheme, actual.hostname, actual.port or 443) != (
            expected.scheme, expected.hostname, expected.port or 443
        ):
            raise UnsafeURL("Cross-origin fetch is forbidden")
    return urldefrag(normal)[0]


class PublicNetworkBackend(httpcore.SyncBackend):
    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None, socket_options: Iterable | None = None):
        if port != 443 or local_address is not None:
            raise httpcore.ConnectError("Only public HTTPS sockets are permitted")
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise httpcore.ConnectError("DNS lookup failed") from exc
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not public_ip(address) for address in addresses):
            raise httpcore.ConnectError("DNS returned a forbidden or ambiguous address")
        # Passing a validated numeric address prevents DNS rebinding at connect time.
        return super().connect_tcp(addresses[0], port, timeout, None, socket_options)

    def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("UNIX sockets are forbidden")


class CoreStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        try:
            yield from self.stream
        except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise httpx.ReadError("Public HTTPS response stream failed") from exc

    def close(self):
        self.stream.close()


class PublicHTTPTransport(httpx.BaseTransport):
    """Uses public httpcore APIs; does not patch global DNS or client internals."""
    def __init__(self):
        self.pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=PublicNetworkBackend(),
            max_connections=5, max_keepalive_connections=2, retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        validate_url(str(request.url))
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        )
        try:
            response = self.pool.handle_request(core_request)
        except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise httpx.ConnectError("Public HTTPS transport failed", request=request) from exc
        return httpx.Response(response.status, headers=response.headers,
                              stream=CoreStream(response.stream), extensions=response.extensions)

    def close(self) -> None:
        self.pool.close()
