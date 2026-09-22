"""Tests for the hand-written mDNS record codec.

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

Byte-exact assertions, because this is hand-rolled DNS wire format with no
library to lean on and no visible failure mode: a wrong cache-flush bit or a
mis-sized rdata field does not raise, it just quietly fails to resolve on the
phone, or worse, evicts another host's records from every listener on the LAN.

No socket is opened here.
"""

from __future__ import annotations

import socket
import struct

import pytest

from py_printer_server import mdns

INFO = mdns.ServiceInfo(
    instance="OFFICE-PC",
    hostname="OFFICE-PC.local.",
    ip="192.168.1.24",
    port=8114,
    txt={"path": "/"},
)


def build_query(name: str, qtype: int, qclass: int = mdns.CLASS_IN, ident: int = 0) -> bytes:
    """A minimal query packet, shaped like the one NsdManager sends."""
    header = struct.pack(">HHHHHH", ident, 0x0000, 1, 0, 0, 0)
    return header + mdns.encode_name(name) + struct.pack(">HH", qtype, qclass)


class TestNameCoding:
    def test_encode_exact_bytes(self):
        assert (
            mdns.encode_name("_pyprint._tcp.local.")
            == b"\x08_pyprint\x04_tcp\x05local\x00"
        )

    def test_encode_ignores_trailing_dot(self):
        assert mdns.encode_name("a.b") == mdns.encode_name("a.b.")

    def test_round_trip(self):
        raw = mdns.encode_name("OFFICE-PC._pyprint._tcp.local.")
        name, offset = mdns.decode_name(raw, 0)
        assert name == "OFFICE-PC._pyprint._tcp.local."
        assert offset == len(raw)

    def test_follows_a_compression_pointer(self):
        # "local." at offset 0, then a name whose tail points back at it.
        base = mdns.encode_name("local.")
        packet = base + b"\x04_tcp" + struct.pack(">H", 0xC000)
        name, offset = mdns.decode_name(packet, len(base))
        assert name == "_tcp.local."
        assert offset == len(packet)

    def test_self_referential_pointer_terminates(self):
        """A packet can point a label at itself. Without the hop guard this
        spins forever on the responder thread."""
        packet = struct.pack(">H", 0xC000)
        with pytest.raises(mdns.MdnsFormatError):
            mdns.decode_name(packet, 0)

    def test_forward_pointer_is_rejected(self):
        packet = struct.pack(">H", 0xC004) + b"\x00\x00" + mdns.encode_name("x.")
        with pytest.raises(mdns.MdnsFormatError):
            mdns.decode_name(packet, 0)

    def test_truncated_name_is_rejected(self):
        with pytest.raises(mdns.MdnsFormatError):
            mdns.decode_name(b"\x08_pyprint", 0)

    def test_oversized_label_is_rejected(self):
        with pytest.raises(mdns.MdnsFormatError):
            mdns.encode_name("x" * 64)

    def test_empty_label_is_rejected(self):
        with pytest.raises(mdns.MdnsFormatError):
            mdns.encode_name("a..b")


class TestRecords:
    def test_ptr_has_no_cache_flush_bit(self):
        """PTR is a shared record. Setting cache-flush on it would evict every
        other host's PTR for this service type from every listener's cache."""
        rec = mdns.ptr_record(INFO)
        name = mdns.encode_name(mdns.SERVICE_TYPE)
        rtype, rclass = struct.unpack(">HH", rec[len(name) : len(name) + 4])
        assert rtype == mdns.TYPE_PTR
        assert rclass == mdns.CLASS_IN
        assert not rclass & mdns.CACHE_FLUSH

    @pytest.mark.parametrize(
        "builder,name_attr,rtype",
        [
            (mdns.srv_record, "fqdn", mdns.TYPE_SRV),
            (mdns.txt_record, "fqdn", mdns.TYPE_TXT),
            (mdns.a_record, "hostname", mdns.TYPE_A),
        ],
    )
    def test_unique_records_set_cache_flush(self, builder, name_attr, rtype):
        rec = builder(INFO)
        name = mdns.encode_name(getattr(INFO, name_attr))
        got_type, rclass = struct.unpack(">HH", rec[len(name) : len(name) + 4])
        assert got_type == rtype
        assert rclass == mdns.CLASS_IN | mdns.CACHE_FLUSH

    def test_srv_rdata(self):
        rec = mdns.srv_record(INFO)
        name = mdns.encode_name(INFO.fqdn)
        rdlen = struct.unpack(">H", rec[len(name) + 8 : len(name) + 10])[0]
        rdata = rec[len(name) + 10 :]
        assert len(rdata) == rdlen
        priority, weight, port = struct.unpack(">HHH", rdata[:6])
        assert (priority, weight, port) == (0, 0, 8114)
        target, _ = mdns.decode_name(rdata, 6)
        assert target == "OFFICE-PC.local."

    def test_txt_rdata(self):
        rec = mdns.txt_record(INFO)
        assert rec.endswith(b"\x06path=/")

    def test_empty_txt_is_a_single_zero_byte(self):
        info = mdns.ServiceInfo(
            instance="X", hostname="X.local.", ip="1.2.3.4", port=1, txt={}
        )
        rec = mdns.txt_record(info)
        assert rec.endswith(b"\x00")
        name = mdns.encode_name(info.fqdn)
        rdlen = struct.unpack(">H", rec[len(name) + 8 : len(name) + 10])[0]
        assert rdlen == 1

    def test_a_rdata(self):
        rec = mdns.a_record(INFO)
        assert rec.endswith(socket.inet_aton("192.168.1.24"))

    def test_rdlen_matches_every_record(self):
        for rec, name in [
            (mdns.ptr_record(INFO), mdns.SERVICE_TYPE),
            (mdns.srv_record(INFO), INFO.fqdn),
            (mdns.txt_record(INFO), INFO.fqdn),
            (mdns.a_record(INFO), INFO.hostname),
        ]:
            head = len(mdns.encode_name(name))
            rdlen = struct.unpack(">H", rec[head + 8 : head + 10])[0]
            assert len(rec) - head - 10 == rdlen


class TestAnnouncement:
    def test_header(self):
        packet = mdns.build_announcement(INFO)
        ident, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", packet[:12])
        assert ident == 0
        assert flags == mdns.FLAG_RESPONSE      # QR=1, AA=1
        assert (qd, an, ns, ar) == (0, 1, 0, 3)

    def test_carries_all_four_records(self):
        packet = mdns.build_announcement(INFO)
        for rec in (
            mdns.ptr_record(INFO),
            mdns.srv_record(INFO),
            mdns.txt_record(INFO),
            mdns.a_record(INFO),
        ):
            assert rec in packet

    def test_goodbye_zeroes_every_ttl(self):
        packet = mdns.build_goodbye(INFO)
        for rec, name in [
            (mdns.ptr_record(INFO, 0), mdns.SERVICE_TYPE),
            (mdns.srv_record(INFO, 0), INFO.fqdn),
            (mdns.txt_record(INFO, 0), INFO.fqdn),
            (mdns.a_record(INFO, 0), INFO.hostname),
        ]:
            head = len(mdns.encode_name(name))
            assert struct.unpack(">I", rec[head + 4 : head + 8])[0] == 0
            assert rec in packet


class TestQueryParsing:
    def test_parses_an_nsd_shaped_ptr_query(self):
        questions = mdns.parse_query(build_query(mdns.SERVICE_TYPE, mdns.TYPE_PTR))
        assert questions is not None
        assert len(questions) == 1
        assert questions[0].name == mdns.SERVICE_TYPE
        assert questions[0].qtype == mdns.TYPE_PTR
        assert questions[0].unicast_response is False

    def test_unicast_response_bit(self):
        questions = mdns.parse_query(
            build_query(mdns.SERVICE_TYPE, mdns.TYPE_PTR, mdns.CLASS_IN | mdns.UNICAST_RESPONSE)
        )
        assert questions is not None
        assert questions[0].unicast_response is True

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\x00" * 11,
            struct.pack(">HHHHHH", 0, 0x8400, 1, 0, 0, 0),   # a response
            struct.pack(">HHHHHH", 0, 0, 0, 0, 0, 0),        # no questions
            struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + b"\x08trunc",
        ],
    )
    def test_malformed_query_returns_none(self, data):
        assert mdns.parse_query(data) is None


class TestResponse:
    def test_ptr_query_gets_srv_txt_a_as_additional(self):
        """resolveService must complete without a second round trip."""
        questions = mdns.parse_query(build_query(mdns.SERVICE_TYPE, mdns.TYPE_PTR))
        packet = mdns.build_response(INFO, questions)
        assert packet is not None
        _, _, qd, an, _, ar = struct.unpack(">HHHHHH", packet[:12])
        assert (qd, an, ar) == (0, 1, 3)
        assert mdns.srv_record(INFO) in packet
        assert mdns.txt_record(INFO) in packet
        assert mdns.a_record(INFO) in packet

    def test_a_query_answers_only_the_a_record(self):
        questions = mdns.parse_query(build_query(INFO.hostname, mdns.TYPE_A))
        packet = mdns.build_response(INFO, questions)
        assert packet is not None
        _, _, _, an, _, ar = struct.unpack(">HHHHHH", packet[:12])
        assert (an, ar) == (1, 0)
        assert mdns.a_record(INFO) in packet

    def test_srv_query_answers_srv_and_offers_a(self):
        questions = mdns.parse_query(build_query(INFO.fqdn, mdns.TYPE_SRV))
        packet = mdns.build_response(INFO, questions)
        assert packet is not None
        _, _, _, an, _, ar = struct.unpack(">HHHHHH", packet[:12])
        assert (an, ar) == (1, 1)

    def test_another_service_gets_no_answer(self):
        questions = mdns.parse_query(build_query("_airplay._tcp.local.", mdns.TYPE_PTR))
        assert mdns.build_response(INFO, questions) is None

    def test_meta_query_is_ignored(self):
        """Answering the DNS-SD meta-query would let a generic browser
        enumerate this host, which defeats the point of an opt-in ad."""
        questions = mdns.parse_query(
            build_query("_services._dns-sd._udp.local.", mdns.TYPE_PTR)
        )
        assert mdns.build_response(INFO, questions) is None

    def test_legacy_unicast_echoes_the_question_and_id(self):
        query = build_query(mdns.SERVICE_TYPE, mdns.TYPE_PTR, ident=0xBEEF)
        questions = mdns.parse_query(query)
        packet = mdns.build_response(INFO, questions, legacy=True, query=query)
        assert packet is not None
        ident, _, qd, an, _, _ = struct.unpack(">HHHHHH", packet[:12])
        assert ident == 0xBEEF
        assert qd == 1
        assert an == 1
        assert packet[12 : 12 + len(query) - 12] == query[12:]

    def test_legacy_response_uses_a_short_ttl(self):
        query = build_query(mdns.SERVICE_TYPE, mdns.TYPE_PTR)
        questions = mdns.parse_query(query)
        packet = mdns.build_response(INFO, questions, legacy=True, query=query)
        assert packet is not None
        assert mdns.ptr_record(INFO, mdns.LEGACY_TTL) in packet

    def test_matching_questions_filters_by_ownership(self):
        questions = [
            mdns.Question(mdns.SERVICE_TYPE, mdns.TYPE_PTR, mdns.CLASS_IN),
            mdns.Question("_airplay._tcp.local.", mdns.TYPE_PTR, mdns.CLASS_IN),
        ]
        matched = mdns.matching_questions(questions, INFO.fqdn, INFO.hostname)
        assert [q.name for q in matched] == [mdns.SERVICE_TYPE]
