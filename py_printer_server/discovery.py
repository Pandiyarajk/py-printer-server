"""Wire format for the LAN discovery beacon.

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

An Android client broadcasts a signed probe; this server answers with the URL
it is reachable on, so the user never has to read an IP off the console. The
beacon is gated on a shared secret derived from ADMIN_PASSWORD: a datagram that
does not carry a valid tag gets no reply at all, so the server stays invisible
to a LAN scanner.

This module is pure logic. It opens no sockets and never calls time.time(): the
caller passes `now` in. That keeps every security-relevant decision testable on
any OS without a network, matching the split printroute.py uses.

Frame::

    PPS1 <b64url_tag_22> <payload_json_utf8>

The tag covers the literal payload bytes as sent or received, never a
re-serialisation. That is deliberate, and it is the whole reason the Kotlin
client can interoperate: org.json on Android is HashMap-backed and does not
preserve key order, so any "canonical JSON" agreement between the two
implementations would be a standing trap. Each side instead MACs exactly the
bytes it puts on, or takes off, the wire.

See PROTOCOL.md for the specification the Android client is written against.
Changing anything here breaks every shipped client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import TypeGuard

MAGIC = b"PPS1"
PROTOCOL_VERSION = 1

# Key derivation. The salt is fixed and public because there is no channel to
# negotiate one before the first packet, so this is a per-password work factor,
# not rainbow-table protection. PBKDF2 rather than using the password directly
# as an HMAC key: every probe on the wire is a (message, tag) pair under the
# same secret that logs into the web UI, so a single captured datagram is an
# offline oracle. 200k iterations costs about 100ms once at startup and raises
# the cost of grinding that oracle by roughly five orders of magnitude.
KDF_SALT = b"py-printer-server/discovery/v1"
KDF_ITERATIONS = 200_000
KDF_DKLEN = 32

# Direction-separated subkeys, so a captured probe tag is structurally unusable
# as a reply tag.
LABEL_PROBE = b"pps-discovery-v1/probe"
LABEL_REPLY = b"pps-discovery-v1/reply"
_DIR_PROBE = b"probe"
_DIR_REPLY = b"reply"

MAX_DATAGRAM = 1024
NONCE_BYTES = 16
NONCE_CHARS = 22          # base64url of 16 bytes, unpadded
TAG_BYTES = 16
TAG_CHARS = 22

# Wide enough to survive the clock drift of a phone that has not synced, narrow
# enough to bound the replay cache. A rejection here looks exactly like a wrong
# password from outside, which is why the responder logs a reason word.
CLOCK_SKEW_SECONDS = 120
REPLAY_CACHE_SIZE = 4096

MAX_NAME_CHARS = 63

_B64U_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


@dataclass(frozen=True)
class Probe:
    """A verified inbound probe."""

    nonce: str
    ts: int


@dataclass(frozen=True)
class Reply:
    """A verified inbound reply, that is, a discovered server."""

    nonce: str
    ts: int
    name: str
    url: str
    ip: str
    port: int
    ver: str


def derive_key(password: str) -> bytes:
    """Derive the discovery master key from the admin password.

    Raises ValueError on an empty password: the server refuses to start without
    ADMIN_PASSWORD, so an empty key here means a caller has gone wrong.
    """
    if not password:
        raise ValueError("discovery key requires a non-empty password")
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), KDF_SALT, KDF_ITERATIONS, KDF_DKLEN
    )


def _subkey(master: bytes, label: bytes) -> bytes:
    """One HMAC block, that is, HKDF-Expand with a single-block output."""
    return hmac.new(master, label, hashlib.sha256).digest()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _tag(master: bytes, direction: bytes, payload: bytes) -> str:
    label = LABEL_PROBE if direction == _DIR_PROBE else LABEL_REPLY
    key = _subkey(master, label)
    mac = hmac.new(key, direction + b"\x00" + payload, hashlib.sha256).digest()
    return _b64u(mac[:TAG_BYTES])


def _frame(master: bytes, direction: bytes, payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return MAGIC + b" " + _tag(master, direction, body).encode("ascii") + b" " + body


def _unframe(
    datagram: object, master: bytes, direction: bytes
) -> tuple[dict | None, str]:
    """Verify the frame and return (payload, reason).

    `reason` is a short word for the responder's DEBUG log. It matters because a
    clock-skew rejection and a wrong password are indistinguishable to the user,
    so the log is the only place the difference can surface. The reason is never
    sent back on the wire.

    Never raises. A malformed datagram is ordinary background traffic on a LAN,
    not an error condition.
    """
    if not isinstance(datagram, (bytes, bytearray)):
        return None, "bad-frame"
    if len(datagram) > MAX_DATAGRAM:
        return None, "oversized"
    parts = bytes(datagram).split(b" ", 2)
    if len(parts) != 3:
        return None, "bad-frame"
    magic, tag, body = parts
    if magic != MAGIC:
        return None, "bad-magic"
    if len(tag) != TAG_CHARS:
        return None, "bad-frame"

    # Verify before parsing: json.loads on unauthenticated bytes is work done on
    # behalf of anyone who can send a packet.
    expected = _tag(master, direction, body).encode("ascii")
    if not hmac.compare_digest(tag, expected):
        return None, "bad-mac"

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "bad-json"
    if not isinstance(payload, dict):
        return None, "bad-json"
    if payload.get("v") != PROTOCOL_VERSION:
        return None, "bad-version"
    return payload, "ok"


def _valid_nonce(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == NONCE_CHARS
        and not (set(value) - _B64U_ALPHABET)
    )


def _valid_int(value: object) -> int | None:
    # bool is an int subclass, so reject it explicitly: {"ts": true} must not
    # sail through as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def new_nonce() -> str:
    """A fresh probe nonce."""
    return _b64u(secrets.token_bytes(NONCE_BYTES))


def build_probe(master: bytes, *, nonce: str, now: int) -> bytes:
    """Build a signed probe datagram."""
    return _frame(
        master,
        _DIR_PROBE,
        {"v": PROTOCOL_VERSION, "t": "probe", "n": nonce, "ts": int(now)},
    )


def parse_probe_verbose(
    datagram: object,
    master: bytes,
    *,
    now: float,
    seen: ReplayWindow | None = None,
) -> tuple[Probe | None, str]:
    """Verify an inbound probe, returning (probe, reason).

    Returns None for every rejection, and the caller must answer None with
    silence: a scanner, a wrong-password client and a replayer all have to look
    identical from outside. `reason` is for the local log only.
    """
    payload, reason = _unframe(datagram, master, _DIR_PROBE)
    if payload is None:
        return None, reason
    if payload.get("t") != "probe":
        return None, "bad-type"

    nonce = payload.get("n")
    if not _valid_nonce(nonce):
        return None, "bad-nonce"

    ts = _valid_int(payload.get("ts"))
    if ts is None:
        return None, "bad-ts"
    if abs(float(now) - ts) > CLOCK_SKEW_SECONDS:
        return None, "stale-ts"

    if seen is not None and not seen.check_and_add(nonce, ts, now):
        return None, "replay"

    return Probe(nonce=nonce, ts=ts), "ok"


def parse_probe(
    datagram: object,
    master: bytes,
    *,
    now: float,
    seen: ReplayWindow | None = None,
) -> Probe | None:
    """Verify an inbound probe. See parse_probe_verbose for the reason word."""
    return parse_probe_verbose(datagram, master, now=now, seen=seen)[0]


def build_reply(
    master: bytes,
    *,
    nonce: str,
    name: str,
    url: str,
    ip: str,
    port: int,
    ver: str,
    now: int,
) -> bytes:
    """Build a signed reply.

    `name` is truncated so a long hostname cannot push the datagram past the
    fragmentation threshold.
    """
    return _frame(
        master,
        _DIR_REPLY,
        {
            "v": PROTOCOL_VERSION,
            "t": "reply",
            "n": nonce,
            "ts": int(now),
            "name": str(name)[:MAX_NAME_CHARS],
            "url": url,
            "ip": ip,
            "port": int(port),
            "ver": ver,
        },
    )


def parse_reply(
    datagram: object, master: bytes, *, expected_nonce: str
) -> Reply | None:
    """Verify an inbound reply against the nonce this scan sent.

    The nonce check is what makes the reply unforgeable: only a holder of the
    shared secret could MAC a message carrying a value that was random a few
    hundred milliseconds ago. Without it, a replayed reply could point the
    client at a stale or attacker-chosen address.
    """
    payload, _reason = _unframe(datagram, master, _DIR_REPLY)
    if payload is None:
        return None
    if payload.get("t") != "reply":
        return None

    nonce = payload.get("n")
    if not _valid_nonce(nonce) or not _valid_nonce(expected_nonce):
        return None
    if not hmac.compare_digest(nonce, expected_nonce):
        return None

    ts = _valid_int(payload.get("ts"))
    if ts is None:
        return None

    port = _valid_int(payload.get("port"))
    if port is None or not 1 <= port <= 65535:
        return None

    name = payload.get("name")
    url = payload.get("url")
    ip = payload.get("ip")
    ver = payload.get("ver")
    if not (
        isinstance(name, str)
        and isinstance(url, str)
        and isinstance(ip, str)
        and isinstance(ver, str)
    ):
        return None

    return Reply(nonce=nonce, ts=ts, name=name, url=url, ip=ip, port=port, ver=ver)


class ReplayWindow:
    """Nonces seen inside the clock-skew window.

    A captured probe would otherwise let an eavesdropper re-confirm the server
    forever without knowing the password. Bounded, so a flood of distinct valid
    nonces cannot grow memory without limit, though minting those requires the
    shared secret anyway.

    Not thread-safe; the responder drives it from a single thread.
    """

    def __init__(self, max_entries: int = REPLAY_CACHE_SIZE) -> None:
        self.max_entries = max_entries
        self._seen: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._seen)

    def check_and_add(self, nonce: str, ts: int, now: float) -> bool:
        """True if `nonce` is new. False means drop it as a replay."""
        cutoff = float(now) - CLOCK_SKEW_SECONDS
        stale = [n for n, seen_ts in self._seen.items() if seen_ts < cutoff]
        for n in stale:
            del self._seen[n]

        if nonce in self._seen:
            return False

        if len(self._seen) >= self.max_entries:
            # Evict the oldest. Entries go in roughly in time order, so this is
            # close enough without carrying a heap.
            oldest = min(self._seen, key=self._seen.__getitem__)
            del self._seen[oldest]

        self._seen[nonce] = ts
        return True
