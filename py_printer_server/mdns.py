"""Hand-written mDNS / DNS-SD record codec.

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

Enough of RFC 6762 and RFC 6763 to advertise one service to Android's
NsdManager, and no more. Pure wire-format logic: this module opens no socket, so
every byte layout below is unit-testable on any OS. The socket and thread live
in discovery_net.MdnsAdvertiser.

Zero third-party dependencies is a hard constraint for this project, so there is
no zeroconf here. The cost is roughly three hundred lines of DNS, which is
exactly why mDNS is opt-in while the UDP beacon is on by default: the beacon
already delivers the feature, and this is a convenience for clients that prefer
DNS-SD.

Deliberately NOT implemented:

- RFC 6762 section 8 probing and conflict resolution. The instance name defaults
  to the hostname, which is already unique on a LAN, and --mdns-name covers the
  rest. This is a decision, not an oversight.
- Name compression on write. It is optional for a responder and the packets here
  are small. Pointer following on read IS implemented, because queries from
  other stacks use it and we have to parse those.
- Any answer to the _services._dns-sd._udp.local. meta-query, so generic service
  browsers cannot enumerate this host.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353

SERVICE_TYPE = "_pyprint._tcp.local."

TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_SRV = 33
TYPE_ANY = 255

CLASS_IN = 1
# Top bit of the rrclass on an answer: "this record is unique, flush anything
# else cached under this name". Top bit of the qclass on a question: "send me a
# unicast answer".
CACHE_FLUSH = 0x8000
UNICAST_RESPONSE = 0x8000
QCLASS_MASK = 0x7FFF

FLAG_RESPONSE = 0x8400  # QR=1, AA=1

# RFC 6762 suggests 75 minutes for PTR/SRV/TXT. This server lives on a desktop
# that gets switched off without sending a goodbye, so a stale entry should
# self-heal in two minutes rather than an hour. The refresh traffic that costs
# is nil on a home LAN.
DEFAULT_TTL = 120
LEGACY_TTL = 10

MAX_LABEL = 63
_MAX_POINTER_HOPS = 16


class MdnsFormatError(ValueError):
    """A name or packet that cannot be encoded or decoded."""


def encode_name(name: str) -> bytes:
    """Encode a dotted DNS name as length-prefixed labels."""
    out = bytearray()
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        if not raw:
            raise MdnsFormatError(f"empty label in {name!r}")
        if len(raw) > MAX_LABEL:
            raise MdnsFormatError(f"label too long in {name!r}")
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a name at `offset`, following compression pointers.

    Returns (name, offset just past the name in the original stream). The hop
    limit matters: a packet can point a label at itself, and without the guard
    this loops forever on the responder thread.
    """
    labels: list[str] = []
    hops = 0
    pos = offset
    end_of_name: int | None = None

    while True:
        if pos >= len(data):
            raise MdnsFormatError("name runs past end of packet")
        length = data[pos]

        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise MdnsFormatError("truncated compression pointer")
            hops += 1
            if hops > _MAX_POINTER_HOPS:
                raise MdnsFormatError("compression pointer loop")
            target = ((length & 0x3F) << 8) | data[pos + 1]
            if end_of_name is None:
                end_of_name = pos + 2
            if target >= pos:
                # A pointer must go backwards. Forward or self references are
                # the loop case.
                raise MdnsFormatError("forward compression pointer")
            pos = target
            continue

        if length == 0:
            pos += 1
            break

        pos += 1
        if pos + length > len(data):
            raise MdnsFormatError("label runs past end of packet")
        labels.append(data[pos : pos + length].decode("utf-8", "replace"))
        pos += length

    name = ".".join(labels)
    if name:
        name += "."
    return name, end_of_name if end_of_name is not None else pos


@dataclass(frozen=True)
class Question:
    name: str
    qtype: int
    qclass: int

    @property
    def unicast_response(self) -> bool:
        return bool(self.qclass & UNICAST_RESPONSE)


@dataclass(frozen=True)
class ServiceInfo:
    instance: str            # "OFFICE-PC"
    hostname: str            # "OFFICE-PC.local."
    ip: str
    port: int
    txt: dict[str, str] = field(default_factory=dict)

    @property
    def fqdn(self) -> str:
        return f"{self.instance}.{SERVICE_TYPE}"


def parse_query(data: bytes) -> list[Question] | None:
    """Parse the question section of an inbound packet.

    Returns None for anything that is not a well-formed query, rather than
    raising: this runs on the responder thread against whatever the LAN sends.
    """
    try:
        if len(data) < 12:
            return None
        _ident, flags, qdcount = struct.unpack(">HHH", data[:6])
        if flags & 0x8000:            # a response, not a query
            return None
        if qdcount == 0:
            return None

        questions: list[Question] = []
        pos = 12
        for _ in range(qdcount):
            name, pos = decode_name(data, pos)
            if pos + 4 > len(data):
                return None
            qtype, qclass = struct.unpack(">HH", data[pos : pos + 4])
            pos += 4
            questions.append(Question(name=name, qtype=qtype, qclass=qclass))
        return questions
    except (MdnsFormatError, struct.error, UnicodeDecodeError):
        return None


def matching_questions(
    questions: list[Question], fqdn: str, hostname: str
) -> list[Question]:
    """The subset of `questions` this host owns an answer to.

    Anything else is ignored, including the _services._dns-sd._udp.local.
    meta-query: answering that would let a generic browser enumerate us, which
    is the opposite of what an opt-in advertisement is for.
    """
    out = []
    for q in questions:
        if q.qclass & QCLASS_MASK not in (CLASS_IN, TYPE_ANY):
            continue
        name = q.name.lower()
        if name == SERVICE_TYPE.lower() and q.qtype in (TYPE_PTR, TYPE_ANY):
            out.append(q)
        elif name == fqdn.lower() and q.qtype in (TYPE_SRV, TYPE_TXT, TYPE_ANY):
            out.append(q)
        elif name == hostname.lower() and q.qtype in (TYPE_A, TYPE_ANY):
            out.append(q)
    return out


def _record(name: str, rtype: int, rclass: int, ttl: int, rdata: bytes) -> bytes:
    return (
        encode_name(name)
        + struct.pack(">HHIH", rtype, rclass, ttl, len(rdata))
        + rdata
    )


def _encode_txt(txt: dict[str, str]) -> bytes:
    if not txt:
        # An empty TXT is a single zero-length string, never zero bytes.
        return b"\x00"
    out = bytearray()
    for key, value in txt.items():
        entry = f"{key}={value}".encode("utf-8")
        if len(entry) > 255:
            raise MdnsFormatError(f"TXT entry too long: {key}")
        out.append(len(entry))
        out.extend(entry)
    return bytes(out)


def ptr_record(info: ServiceInfo, ttl: int = DEFAULT_TTL) -> bytes:
    # Shared record: no cache-flush bit. Setting it here would evict other
    # hosts' PTR records for this service type from every listener's cache.
    return _record(SERVICE_TYPE, TYPE_PTR, CLASS_IN, ttl, encode_name(info.fqdn))


def srv_record(info: ServiceInfo, ttl: int = DEFAULT_TTL) -> bytes:
    rdata = struct.pack(">HHH", 0, 0, info.port) + encode_name(info.hostname)
    return _record(info.fqdn, TYPE_SRV, CLASS_IN | CACHE_FLUSH, ttl, rdata)


def txt_record(info: ServiceInfo, ttl: int = DEFAULT_TTL) -> bytes:
    return _record(
        info.fqdn, TYPE_TXT, CLASS_IN | CACHE_FLUSH, ttl, _encode_txt(info.txt)
    )


def a_record(info: ServiceInfo, ttl: int = DEFAULT_TTL) -> bytes:
    import socket

    return _record(
        info.hostname, TYPE_A, CLASS_IN | CACHE_FLUSH, ttl, socket.inet_aton(info.ip)
    )


def _packet(
    *,
    ident: int = 0,
    answers: list[bytes],
    additional: list[bytes],
    questions: bytes = b"",
    qdcount: int = 0,
) -> bytes:
    header = struct.pack(
        ">HHHHHH",
        ident,
        FLAG_RESPONSE,
        qdcount,
        len(answers),
        0,
        len(additional),
    )
    return header + questions + b"".join(answers) + b"".join(additional)


def build_announcement(info: ServiceInfo, *, ttl: int = DEFAULT_TTL) -> bytes:
    """An unsolicited announcement carrying the whole service."""
    return _packet(
        answers=[ptr_record(info, ttl)],
        additional=[srv_record(info, ttl), txt_record(info, ttl), a_record(info, ttl)],
    )


def build_goodbye(info: ServiceInfo) -> bytes:
    """The same records with TTL 0, so listeners drop us immediately."""
    return build_announcement(info, ttl=0)


def build_response(
    info: ServiceInfo,
    questions: list[Question],
    *,
    ttl: int = DEFAULT_TTL,
    legacy: bool = False,
    query: bytes | None = None,
) -> bytes | None:
    """Answer the questions this host owns, or None if it owns none.

    SRV, TXT and A ride along in the additional section of a PTR answer, so
    NsdManager.resolveService completes without a second round trip.

    `legacy` is a query from a source port other than 5353, which per RFC 6762
    section 6.7 must be answered unicast, echoing the question section with the
    original transaction ID and a short TTL.
    """
    matched = matching_questions(questions, info.fqdn, info.hostname)
    if not matched:
        return None

    if legacy:
        ttl = LEGACY_TTL

    answers: list[bytes] = []
    additional: list[bytes] = []
    wanted = {(q.name.lower(), q.qtype) for q in matched}

    def asked(name: str, rtype: int) -> bool:
        key = name.lower()
        return (key, rtype) in wanted or (key, TYPE_ANY) in wanted

    if asked(SERVICE_TYPE, TYPE_PTR):
        answers.append(ptr_record(info, ttl))
        additional.extend(
            [srv_record(info, ttl), txt_record(info, ttl), a_record(info, ttl)]
        )
    else:
        # A direct question about our own names is answered in the answer
        # section, not the additional one.
        if asked(info.fqdn, TYPE_SRV):
            answers.append(srv_record(info, ttl))
            additional.append(a_record(info, ttl))
        if asked(info.fqdn, TYPE_TXT):
            answers.append(txt_record(info, ttl))
        if asked(info.hostname, TYPE_A):
            answers.append(a_record(info, ttl))

    if not answers:
        return None

    ident = 0
    echoed = b""
    qdcount = 0
    if legacy and query is not None and len(query) >= 12:
        ident = struct.unpack(">H", query[:2])[0]
        try:
            pos = 12
            for _ in range(struct.unpack(">H", query[4:6])[0]):
                _name, pos = decode_name(query, pos)
                pos += 4
            echoed = query[12:pos]
            qdcount = struct.unpack(">H", query[4:6])[0]
        except (MdnsFormatError, struct.error):
            echoed, qdcount = b"", 0

    return _packet(
        ident=ident,
        answers=answers,
        additional=additional,
        questions=echoed,
        qdcount=qdcount,
    )
