"""Sockets for the discovery beacon: the responder thread and the client scan.

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

This is the only module in the discovery path that opens a socket. The wire
format lives in discovery.py, which is pure and fully unit-tested; everything
here is plumbing around it, following the same split printroute.py uses.

The mDNS advertiser lives here too, over the pure record codec in mdns.py.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable

from py_printer_server import discovery
from py_printer_server.discovery import (
    MAX_DATAGRAM,
    ReplayWindow,
    Reply,
    build_probe,
    build_reply,
    derive_key,
    new_nonce,
    parse_probe_verbose,
    parse_reply,
)

logger = logging.getLogger("printer_server")

# Fixed on purpose: it does not follow --port. A client probing 8114 must be
# able to find a server started with --port 9000, otherwise discovery cannot
# bootstrap. The reply carries the real HTTP port.
DISCOVERY_PORT = 8114

# The socket blocks in recvfrom, so this bounds how long a stopped thread
# lingers. Do not close the socket from another thread to unblock it: on Windows
# that raises an unpredictable OSError in the blocked thread.
_POLL_SECONDS = 1.0

# The reply is about 1.6x the probe, so cap it. Forging a probe needs the shared
# secret, which already defeats a spoofed-source attacker; this is a backstop.
_REPLIES_PER_SECOND = 20
_REPLIES_PER_SECOND_PER_IP = 5


def local_ip_for(peer: str = "8.8.8.8") -> str:
    """Best-effort local address that would be used to reach `peer`.

    Connecting a UDP socket transmits nothing: it only asks the OS to run a
    route lookup and pick a source address. Asking per peer, rather than once
    against a public address, is what makes the answer correct on a machine with
    a VPN or a second NIC. Reporting the default-route address to a phone that
    probed over Wi-Fi would hand it a URL it cannot reach, which is the most
    likely field failure for this feature.

    socket.recvmsg with IP_PKTINFO would answer this more directly, but CPython
    does not build recvmsg on Windows.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class _RateLimiter:
    """Sliding one-second window, globally and per source address."""

    def __init__(self, per_second: int, per_ip: int) -> None:
        self.per_second = per_second
        self.per_ip = per_ip
        self._all: deque[float] = deque()
        self._by_ip: dict[str, deque[float]] = {}

    def allow(self, ip: str, now: float) -> bool:
        cutoff = now - 1.0
        while self._all and self._all[0] < cutoff:
            self._all.popleft()

        seen = self._by_ip.get(ip)
        if seen is None:
            seen = self._by_ip[ip] = deque()
        while seen and seen[0] < cutoff:
            seen.popleft()

        # Drop idle peers so a long run against many addresses cannot grow this
        # without bound.
        if len(self._by_ip) > 256:
            for addr in [a for a, q in self._by_ip.items() if not q]:
                del self._by_ip[addr]

        if len(self._all) >= self.per_second or len(seen) >= self.per_ip:
            return False

        self._all.append(now)
        seen.append(now)
        return True


class DiscoveryResponder:
    """Answers signed discovery probes on UDP.

    The socket is bound in __init__, so a port collision surfaces synchronously
    to main() and can be reported in the startup banner instead of being
    swallowed inside a thread. Key derivation (PBKDF2, about 100ms) happens here
    too, once, for the same reason.
    """

    def __init__(
        self,
        *,
        password: str,
        http_port: int,
        version: str,
        ip_for: Callable[[str], str] = local_ip_for,
        bind_port: int = DISCOVERY_PORT,
        name: str | None = None,
    ) -> None:
        self.http_port = http_port
        self.bind_port = bind_port
        self.version = version
        self.name = name or socket.gethostname()
        self._ip_for = ip_for
        self._master = derive_key(password)
        self._seen = ReplayWindow()
        self._limit = _RateLimiter(_REPLIES_PER_SECOND, _REPLIES_PER_SECOND_PER_IP)
        self._stop = threading.Event()

        # No SO_REUSEADDR here. UDP has no TIME_WAIT so it buys nothing after a
        # restart, and on Windows it would let any other local process bind the
        # same port and steal our datagrams. (The mDNS socket does set it, where
        # sharing 5353 with the OS responder is unavoidable.)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 0.0.0.0 is required, not cosmetic: on Windows a socket bound to a
            # specific interface address does not reliably receive datagrams
            # sent to 255.255.255.255.
            self._sock.bind(("0.0.0.0", bind_port))
            self._sock.settimeout(_POLL_SECONDS)
        except OSError:
            self._sock.close()
            raise

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="discovery-beacon"
        )

    @property
    def status(self) -> str:
        return (
            f"UDP beacon on {self.bind_port} "
            f"(key derived from ADMIN_PASSWORD)"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            # Everything is inside the try, including the recvfrom unpack: an
            # exception escaping this loop would silently kill discovery for the
            # life of the process, and the only symptom would be an app that
            # stops finding the server.
            try:
                try:
                    data, peer = self._sock.recvfrom(MAX_DATAGRAM + 1024)
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    raise
                self._handle(data, peer)
            except Exception:
                if self._stop.is_set():
                    return
                logger.exception("discovery responder error")
                time.sleep(0.1)

    def _handle(self, data: bytes, peer: tuple[str, int]) -> None:
        now = time.time()
        probe, reason = parse_probe_verbose(
            data, self._master, now=now, seen=self._seen
        )
        if probe is None:
            # Silence on the wire; the reason only ever reaches the local log.
            # Never log the payload: a bad-mac datagram is attacker-controlled.
            logger.debug("discovery probe rejected from %s: %s", peer[0], reason)
            return

        if not self._limit.allow(peer[0], now):
            logger.debug("discovery reply rate-limited for %s", peer[0])
            return

        ip = self._ip_for(peer[0])
        reply = build_reply(
            self._master,
            nonce=probe.nonce,
            name=self.name,
            url=f"http://{ip}:{self.http_port}",
            ip=ip,
            port=self.http_port,
            ver=self.version,
            now=int(now),
        )
        self._sock.sendto(reply, peer)
        logger.info("discovery reply to %s as %s", peer[0], ip)


def _broadcast_targets() -> list[str]:
    """Addresses worth sending a probe to.

    The limited broadcast address alone is not reliable: some stacks and access
    points drop it, others drop the directed subnet broadcast, so send to both.
    Directed targets are derived from the local addresses because Python has no
    stdlib interface enumeration that reports netmasks on Windows.
    """
    targets = ["255.255.255.255"]
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        addrs = []
    addrs.append(local_ip_for())
    for addr in addrs:
        if addr.startswith("127."):
            continue
        octets = addr.split(".")
        if len(octets) != 4:
            continue
        # Assume a /24, which is what essentially every home and small office
        # LAN uses. A wrong guess costs one wasted datagram.
        directed = ".".join(octets[:3] + ["255"])
        if directed not in targets:
            targets.append(directed)
    return targets


def discover(
    password: str,
    *,
    port: int = DISCOVERY_PORT,
    timeout: float = 2.0,
) -> list[Reply]:
    """Broadcast a signed probe and collect the replies.

    Backs `--discover`, and doubles as the reference implementation the Android
    client is ported from: the retransmit schedule and the verification steps
    here are what the Kotlin side must reproduce.
    """
    master = derive_key(password)
    nonce = new_nonce()
    found: dict[str, Reply] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.25)
        sock.bind(("0.0.0.0", 0))

        targets = _broadcast_targets()
        deadline = time.monotonic() + timeout
        # A single UDP probe is routinely lost on Wi-Fi, so retransmit.
        sends = [0.0, 0.4, 1.0]

        while time.monotonic() < deadline:
            if sends and time.monotonic() >= deadline - timeout + sends[0]:
                sends.pop(0)
                probe = build_probe(master, nonce=nonce, now=int(time.time()))
                for target in targets:
                    try:
                        sock.sendto(probe, (target, port))
                    except OSError as exc:
                        logger.debug("probe to %s failed: %s", target, exc)

            try:
                data, peer = sock.recvfrom(MAX_DATAGRAM + 1024)
            except TimeoutError:
                continue
            except OSError:
                break

            reply = parse_reply(data, master, expected_nonce=nonce)
            if reply is None:
                continue
            # Prefer the datagram's real source over the claimed address. The
            # ip field is inside the MAC so it cannot be altered in flight, but
            # this costs nothing.
            if reply.ip != peer[0]:
                logger.debug("reply from %s claims %s, ignoring", peer[0], reply.ip)
                continue
            found[f"{reply.ip}:{reply.port}"] = reply
    finally:
        sock.close()

    return list(found.values())


class MdnsAdvertiser:
    """Advertises the HTTP service over mDNS, for clients using NsdManager.

    Off by default. Unlike the UDP beacon this carries no secret and cannot be
    gated, so enabling it makes the server visible to everything on the LAN.
    That trade-off is stated in the banner, the README and PROTOCOL.md.
    """

    def __init__(
        self,
        *,
        instance: str,
        http_port: int,
        ip_for: Callable[[], str] = local_ip_for,
    ) -> None:
        from py_printer_server import mdns

        self._mdns = mdns
        self.instance = instance
        self.http_port = http_port
        self._ip_for = ip_for
        self._stop = threading.Event()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Required here, unlike the beacon: Windows ships its own mDNS
        # responder, and Bonjour may also be present, so 5353 has to be shared
        # or the bind fails outright.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", mdns.MDNS_PORT))
        except OSError:
            sock.close()
            raise

        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        self._join_groups(sock)
        sock.settimeout(_POLL_SECONDS)
        self._sock = sock

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mdns-advertiser"
        )

    def _join_groups(self, sock: socket.socket) -> None:
        """Join the mDNS group on the default interface, then best-effort on
        each local address. Joining per interface is the only dependency-free
        way to cover a multi-NIC Windows box."""
        group = socket.inet_aton(self._mdns.MDNS_GROUP)
        interfaces = ["0.0.0.0"]
        try:
            _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
            interfaces.extend(a for a in addrs if not a.startswith("127."))
        except OSError:
            pass
        for iface in interfaces:
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_ADD_MEMBERSHIP,
                    group + socket.inet_aton(iface),
                )
            except OSError as exc:
                logger.debug("mDNS join on %s failed: %s", iface, exc)

    @property
    def status(self) -> str:
        return (
            f'advertising {self._mdns.SERVICE_TYPE.rstrip(".")} as "{self.instance}" '
            "(anyone on this LAN can see it)"
        )

    def _info(self):
        return self._mdns.ServiceInfo(
            instance=self.instance,
            hostname=f"{self.instance}.local.",
            ip=self._ip_for(),
            port=self.http_port,
            txt={"path": "/", "v": "1"},
        )

    def _send(self, packet: bytes, addr: tuple[str, int] | None = None) -> None:
        try:
            self._sock.sendto(
                packet, addr or (self._mdns.MDNS_GROUP, self._mdns.MDNS_PORT)
            )
        except OSError as exc:
            logger.debug("mDNS send failed: %s", exc)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._send(self._mdns.build_goodbye(self._info()))
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _run(self) -> None:
        # Announce, then answer queries. Announcing alone is not enough:
        # NsdManager sends a PTR query when discovery starts, so an app launched
        # after our burst would never see us.
        announced = 0
        next_announce = time.monotonic()

        while not self._stop.is_set():
            try:
                if announced < 2 and time.monotonic() >= next_announce:
                    self._send(self._mdns.build_announcement(self._info()))
                    announced += 1
                    next_announce = time.monotonic() + 1.0

                try:
                    data, peer = self._sock.recvfrom(9000)
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    raise

                questions = self._mdns.parse_query(data)
                if not questions:
                    continue
                info = self._info()
                legacy = peer[1] != self._mdns.MDNS_PORT
                packet = self._mdns.build_response(
                    info, questions, legacy=legacy, query=data if legacy else None
                )
                if packet is None:
                    continue
                unicast = legacy or any(
                    q.unicast_response
                    for q in self._mdns.matching_questions(
                        questions, info.fqdn, info.hostname
                    )
                )
                self._send(packet, peer if unicast else None)
            except Exception:
                if self._stop.is_set():
                    return
                logger.exception("mDNS advertiser error")
                time.sleep(0.1)


__all__ = [
    "DISCOVERY_PORT",
    "DiscoveryResponder",
    "MdnsAdvertiser",
    "discover",
    "local_ip_for",
    "discovery",
]
