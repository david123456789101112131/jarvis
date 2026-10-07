"""Keep the web tools on the public internet unless the user says otherwise.

A web page can tell an agent to fetch `http://192.168.1.1/…` (the router), `http://localhost:…`
(a database, a dev server, Docker) or a name that resolves to one of those. So:

* `classify_url` sorts a target into "public" or "local" (this computer, the home or office
  network, link-local and other non-internet addresses, single-label and `.local` names) before
  the call; the web tools ask the user first for a local target.
* `GuardedBackend` checks the address every connection really goes to, after DNS: a name that
  resolved to a public address at planning time and to a local one now (DNS rebinding), or a
  redirect to a local address, is refused unless the user approved local access for this call
  (`local_access`). JARVIS's own control server is never reachable.
* `hop_check` looks at every request and redirect before it is sent. It is what covers requests that
  go through a proxy the system names (HTTPS_PROXY, the Windows proxy settings): the proxy resolves
  those names itself, so for them only literal addresses and local-only names can be checked here.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import Any

import httpcore
import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".home.arpa", ".internal", ".intranet", ".localdomain")
_local_ok: ContextVar[bool] = ContextVar("jarvis_local_network_ok", default=False)


async def default_resolver(host: str, port: int) -> list[str]:
    """Every address `host` resolves to (empty when it does not resolve in time)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), 5.0)
    except (OSError, TimeoutError, UnicodeError):
        return []
    return list(dict.fromkeys(str(info[4][0]).split("%")[0] for info in infos))


def parse_ip(text: str) -> IPAddress | None:
    try:
        ip = ipaddress.ip_address(text.strip("[]").split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_public(ip: IPAddress) -> bool:
    """A real internet address (not this computer, a private network, link-local, multicast or reserved)."""
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def name_is_local(host: str) -> bool:
    """Names that only ever mean this computer or the local network (no DNS needed)."""
    name = host.strip("[]").rstrip(".").lower()
    return name == "localhost" or name.endswith(_LOCAL_SUFFIXES) or ("." not in name and parse_ip(name) is None)


async def classify_host(host: str, port: int, resolver: Resolver | None = None) -> tuple[str, list[str]]:
    """("local" | "public", the addresses looked at). A name that does not resolve counts as public:
    the connection itself is checked again (see GuardedBackend)."""
    ip = parse_ip(host)
    if ip is not None:
        return ("public" if is_public(ip) else "local"), [str(ip)]
    if name_is_local(host):
        return "local", []
    addresses = await (resolver or default_resolver)(host, port)
    parsed = [a for a in (parse_ip(x) for x in addresses) if a is not None]
    if any(not is_public(a) for a in parsed):
        return "local", [str(a) for a in parsed]
    return "public", [str(a) for a in parsed]


@contextlib.contextmanager
def local_access(allowed: bool) -> Iterator[None]:
    """Within this block (this task), connections may go to local addresses: the user approved it."""
    token = _local_ok.set(bool(allowed))
    try:
        yield
    finally:
        _local_ok.reset(token)


class LocalAddressRefused(httpcore.ConnectError):
    pass


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolves each host itself and connects only to addresses the current call may reach."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, *, resolver: Resolver | None = None,
                 own_port: Callable[[], int | None] = lambda: None) -> None:
        self.inner = inner
        self.resolver = resolver
        self.own_port = own_port

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        ip = parse_ip(host)
        addresses = [str(ip)] if ip is not None else await (self.resolver or default_resolver)(host, port)
        if not addresses:
            raise httpcore.ConnectError(f"could not resolve {host}")
        allowed_local = _local_ok.get()
        own = self.own_port()
        last: Exception | None = None
        for text in addresses:
            address = parse_ip(text)
            if address is None:
                continue
            if address.is_loopback and own and port == own:
                raise LocalAddressRefused("agents may not call JARVIS's own control server")
            if not is_public(address) and not allowed_local:
                raise LocalAddressRefused(f"{host} leads to a local network address ({address}); "
                                          "the user did not approve reaching the local network")
            try:
                return await self.inner.connect_tcp(str(address), port, timeout=timeout, local_address=local_address,
                                                    socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last = exc
        raise last if last is not None else httpcore.ConnectError(f"no usable address for {host}")

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise LocalAddressRefused("unix sockets are not reachable from the web tools")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def hop_check(own_port: Callable[[], int | None] = lambda: None) -> Callable[[httpx.Request], Awaitable[None]]:
    """An httpx request hook: refuse a literal local address or a local-only name (every redirect too)
    unless the user approved local access, and JARVIS's own control server always."""

    async def check(request: httpx.Request) -> None:
        host = request.url.host
        ip = parse_ip(host)
        own = own_port()
        refusal: LocalAddressRefused | None = None
        if own and request.url.port == own and (host == "localhost" or (ip is not None and ip.is_loopback)):
            refusal = LocalAddressRefused("agents may not call JARVIS's own control server")
        elif ((ip is not None and not is_public(ip)) or (ip is None and name_is_local(host))) and not _local_ok.get():
            refusal = LocalAddressRefused(f"{host} is on the local network; the user did not approve reaching it")
        if refusal is not None:
            raise httpx.ConnectError(str(refusal), request=request) from refusal

    return check


def guard_transport(transport: Any, backend_factory: Callable[[httpcore.AsyncNetworkBackend], GuardedBackend]) -> bool:
    """Put a GuardedBackend under an httpx transport's connection pool; False when it has none."""
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, httpcore.AsyncConnectionPool):
        return False
    pool._network_backend = backend_factory(pool._network_backend)
    return True


__all__ = ["GuardedBackend", "LocalAddressRefused", "Resolver", "classify_host", "default_resolver",
           "guard_transport", "hop_check", "is_public", "local_access", "name_is_local", "parse_ip"]
