"""Tests for the discovery beacon wire format.

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

No socket is opened here. Everything security-relevant in the beacon lives in
the pure module, which is the point of the split: these run on any OS, with a
clock the test controls.

The vectors in TestSharedVectors are the cross-language contract. The Kotlin
client asserts the same values, so a divergence between the two implementations
fails at build time instead of on a phone.
"""

from __future__ import annotations

import json

import pytest

from py_printer_server import discovery as d

PASSWORD = "test-password"
NOW = 1_758_499_200


@pytest.fixture(scope="module")
def key() -> bytes:
    # 200k PBKDF2 iterations is deliberately slow; derive once for the module.
    return d.derive_key(PASSWORD)


@pytest.fixture(scope="module")
def other_key() -> bytes:
    return d.derive_key("a-different-password")


def a_probe(key: bytes, *, nonce: str | None = None, now: int = NOW) -> bytes:
    return d.build_probe(key, nonce=nonce or d.new_nonce(), now=now)


def a_reply(key: bytes, nonce: str, *, now: int = NOW, name: str = "OFFICE-PC") -> bytes:
    return d.build_reply(
        key,
        nonce=nonce,
        name=name,
        url="http://192.168.1.24:8114",
        ip="192.168.1.24",
        port=8114,
        ver="0.2.0",
        now=now,
    )


def retag(key: bytes, direction: bytes, body: bytes) -> bytes:
    """Re-frame `body` with a valid tag, to test payload-level rejections."""
    return d.MAGIC + b" " + d._tag(key, direction, body).encode("ascii") + b" " + body


class TestKeyDerivation:
    def test_is_deterministic(self):
        assert d.derive_key("abc") == d.derive_key("abc")

    def test_different_passwords_give_different_keys(self):
        assert d.derive_key("abc") != d.derive_key("abd")

    def test_empty_password_raises(self):
        with pytest.raises(ValueError):
            d.derive_key("")

    def test_subkeys_differ_by_direction(self, key):
        assert d._subkey(key, d.LABEL_PROBE) != d._subkey(key, d.LABEL_REPLY)


class TestProbeRoundTrip:
    def test_round_trip_returns_the_nonce(self, key):
        nonce = d.new_nonce()
        probe = a_probe(key, nonce=nonce)
        parsed = d.parse_probe(probe, key, now=NOW)
        assert parsed is not None
        assert parsed.nonce == nonce
        assert parsed.ts == NOW

    def test_wrong_password_is_rejected(self, key, other_key):
        assert d.parse_probe(a_probe(key), other_key, now=NOW) is None

    def test_tampered_payload_byte_is_rejected(self, key):
        probe = bytearray(a_probe(key))
        probe[-2] = probe[-2] ^ 0x01          # flip a bit in the timestamp
        assert d.parse_probe(bytes(probe), key, now=NOW) is None

    def test_tampered_tag_byte_is_rejected(self, key):
        probe = bytearray(a_probe(key))
        probe[6] = ord("A") if probe[6] != ord("A") else ord("B")
        assert d.parse_probe(bytes(probe), key, now=NOW) is None


class TestDirectionSeparation:
    """A captured probe tag must be unusable as a reply tag, and vice versa.

    This is what the two labelled subkeys and the direction byte buy, so it gets
    an explicit test rather than being assumed from the construction.
    """

    def test_probe_is_not_accepted_as_a_reply(self, key):
        nonce = d.new_nonce()
        probe = a_probe(key, nonce=nonce)
        assert d.parse_reply(probe, key, expected_nonce=nonce) is None

    def test_reply_is_not_accepted_as_a_probe(self, key):
        nonce = d.new_nonce()
        assert d.parse_probe(a_reply(key, nonce), key, now=NOW) is None

    def test_relabelled_probe_body_is_rejected(self, key):
        """Rewriting t to "reply" and re-tagging under the probe key must still
        fail, because parse_reply verifies under the reply subkey."""
        nonce = d.new_nonce()
        body = json.dumps(
            {"v": 1, "t": "reply", "n": nonce, "ts": NOW}, separators=(",", ":")
        ).encode()
        forged = retag(key, d._DIR_PROBE, body)
        assert d.parse_reply(forged, key, expected_nonce=nonce) is None


class TestReplyVerification:
    def test_round_trip(self, key):
        nonce = d.new_nonce()
        parsed = d.parse_reply(a_reply(key, nonce), key, expected_nonce=nonce)
        assert parsed is not None
        assert parsed.url == "http://192.168.1.24:8114"
        assert parsed.port == 8114
        assert parsed.ip == "192.168.1.24"
        assert parsed.ver == "0.2.0"

    def test_unexpected_nonce_is_rejected(self, key):
        """The client's mutual-auth check: a reply carrying someone else's
        nonce, or a replayed one from an earlier scan, must not be trusted."""
        reply = a_reply(key, d.new_nonce())
        assert d.parse_reply(reply, key, expected_nonce=d.new_nonce()) is None

    def test_wrong_password_is_rejected(self, key, other_key):
        nonce = d.new_nonce()
        assert d.parse_reply(a_reply(key, nonce), other_key, expected_nonce=nonce) is None

    def test_malformed_expected_nonce_is_rejected(self, key):
        nonce = d.new_nonce()
        assert d.parse_reply(a_reply(key, nonce), key, expected_nonce="short") is None

    @pytest.mark.parametrize("port", [0, -1, 65536, True, "8114", None])
    def test_bad_port_is_rejected(self, key, port):
        nonce = d.new_nonce()
        body = json.dumps(
            {
                "v": 1, "t": "reply", "n": nonce, "ts": NOW, "name": "x",
                "url": "http://x", "ip": "1.2.3.4", "port": port, "ver": "0",
            },
            separators=(",", ":"),
        ).encode()
        forged = retag(key, d._DIR_REPLY, body)
        assert d.parse_reply(forged, key, expected_nonce=nonce) is None

    def test_non_string_name_is_rejected(self, key):
        nonce = d.new_nonce()
        body = json.dumps(
            {
                "v": 1, "t": "reply", "n": nonce, "ts": NOW, "name": 42,
                "url": "http://x", "ip": "1.2.3.4", "port": 8114, "ver": "0",
            },
            separators=(",", ":"),
        ).encode()
        forged = retag(key, d._DIR_REPLY, body)
        assert d.parse_reply(forged, key, expected_nonce=nonce) is None


class TestGarbageInput:
    """Every rejection returns None. Nothing here may raise: a malformed
    datagram is ordinary LAN background traffic, and an exception escaping into
    the responder loop would take the beacon down for the life of the process.
    """

    @pytest.mark.parametrize(
        "datagram",
        [
            b"",
            b"PPS1",
            b"PPS1 ",
            b"PPS1 x",
            b"PPS1 tooshort body",
            b"XXXX AAAAAAAAAAAAAAAAAAAAAA {}",
            b"PPS1 AAAAAAAAAAAAAAAAAAAAAA notjson",
            b"\x00\xff\xfe",
            "a string, not bytes",
            None,
            12345,
        ],
    )
    def test_garbage_returns_none(self, key, datagram):
        assert d.parse_probe(datagram, key, now=NOW) is None
        assert d.parse_reply(datagram, key, expected_nonce=d.new_nonce()) is None

    def test_valid_tag_over_a_json_array_is_rejected(self, key):
        forged = retag(key, d._DIR_PROBE, b"[1,2,3]")
        assert d.parse_probe(forged, key, now=NOW) is None

    def test_oversized_datagram_is_rejected_before_hmac(self, key):
        nonce = d.new_nonce()
        body = json.dumps(
            {"v": 1, "t": "probe", "n": nonce, "ts": NOW, "pad": "A" * 2000},
            separators=(",", ":"),
        ).encode()
        forged = retag(key, d._DIR_PROBE, body)
        assert len(forged) > d.MAX_DATAGRAM
        assert d.parse_probe(forged, key, now=NOW) is None

    def test_wrong_protocol_version_is_rejected(self, key):
        nonce = d.new_nonce()
        body = json.dumps(
            {"v": 2, "t": "probe", "n": nonce, "ts": NOW}, separators=(",", ":")
        ).encode()
        forged = retag(key, d._DIR_PROBE, body)
        assert d.parse_probe(forged, key, now=NOW) is None

    @pytest.mark.parametrize("nonce", ["", "short", "A" * 21, "A" * 23, "!" * 22, 42, None])
    def test_bad_nonce_is_rejected(self, key, nonce):
        body = json.dumps(
            {"v": 1, "t": "probe", "n": nonce, "ts": NOW}, separators=(",", ":")
        ).encode()
        forged = retag(key, d._DIR_PROBE, body)
        assert d.parse_probe(forged, key, now=NOW) is None

    @pytest.mark.parametrize("ts", [True, "1758499200", None, 1.5])
    def test_bad_timestamp_is_rejected(self, key, ts):
        nonce = d.new_nonce()
        body = json.dumps(
            {"v": 1, "t": "probe", "n": nonce, "ts": ts}, separators=(",", ":")
        ).encode()
        forged = retag(key, d._DIR_PROBE, body)
        assert d.parse_probe(forged, key, now=NOW) is None


class TestClockWindow:
    @pytest.mark.parametrize("offset", [0, 10, -10, 119, -119, 120, -120])
    def test_inside_the_window_is_accepted(self, key, offset):
        probe = a_probe(key, now=NOW + offset)
        assert d.parse_probe(probe, key, now=NOW) is not None

    @pytest.mark.parametrize("offset", [121, -121, 3600, -3600])
    def test_outside_the_window_is_rejected(self, key, offset):
        probe = a_probe(key, now=NOW + offset)
        assert d.parse_probe(probe, key, now=NOW) is None


class TestReplayWindow:
    def test_same_nonce_twice_is_rejected(self):
        w = d.ReplayWindow()
        assert w.check_and_add("n1", NOW, NOW) is True
        assert w.check_and_add("n1", NOW, NOW) is False

    def test_distinct_nonces_are_accepted(self):
        w = d.ReplayWindow()
        assert w.check_and_add("n1", NOW, NOW) is True
        assert w.check_and_add("n2", NOW, NOW) is True

    def test_entry_is_evicted_once_it_leaves_the_window(self):
        w = d.ReplayWindow()
        assert w.check_and_add("n1", NOW, NOW) is True
        later = NOW + d.CLOCK_SKEW_SECONDS + 1
        assert w.check_and_add("n1", later, later) is True
        assert len(w) == 1

    def test_size_is_bounded(self):
        w = d.ReplayWindow(max_entries=64)
        for i in range(5000):
            w.check_and_add(f"n{i}", NOW, NOW)
        assert len(w) <= 64

    def test_parse_probe_rejects_a_replay(self, key):
        seen = d.ReplayWindow()
        probe = a_probe(key)
        assert d.parse_probe(probe, key, now=NOW, seen=seen) is not None
        assert d.parse_probe(probe, key, now=NOW, seen=seen) is None


class TestDatagramSize:
    def test_reply_with_a_long_name_stays_under_512_bytes(self, key):
        reply = d.build_reply(
            key,
            nonce=d.new_nonce(),
            name="X" * 300,
            url="http://255.255.255.255:65535",
            ip="255.255.255.255",
            port=65535,
            ver="10.10.10",
            now=NOW,
        )
        assert len(reply) < 512

    def test_long_hostname_is_truncated(self, key):
        nonce = d.new_nonce()
        reply = d.build_reply(
            key, nonce=nonce, name="X" * 300, url="http://x", ip="1.2.3.4",
            port=8114, ver="0.2.0", now=NOW,
        )
        parsed = d.parse_reply(reply, key, expected_nonce=nonce)
        assert parsed is not None
        assert len(parsed.name) == d.MAX_NAME_CHARS


class TestSharedVectors:
    """Frozen cross-language vectors.

    The Kotlin client's unit tests assert these same values. If a refactor here
    changes any of them, every already-installed app stops discovering this
    server, so these failing means "you just broke the wire format", not "update
    the expected value".
    """

    PASSWORD = "hunter2"
    NONCE = "AAAAAAAAAAAAAAAAAAAAAA"
    TS = 1_758_499_200

    MASTER = "68ca0d69c993cd44188a21066b7ce34a67fee2c92d2fd25685008029bb63ce09"
    K_PROBE = "09e03c07fcfa7da7a76ed36dad835c1126ff1bfdd1d6f326fbc29793855bd89e"
    K_REPLY = "3c6cc4bfc1140011c3ad03fd146c4d87d8f8bff0a685bae43f135f9685a98e97"

    PROBE = (
        b'PPS1 2bIo5Zv2zr6DSHMRwVXn_w '
        b'{"v":1,"t":"probe","n":"AAAAAAAAAAAAAAAAAAAAAA","ts":1758499200}'
    )
    REPLY = (
        b'PPS1 3ohJECh3lDn8wuSWg9Zh5w '
        b'{"v":1,"t":"reply","n":"AAAAAAAAAAAAAAAAAAAAAA","ts":1758499201,'
        b'"name":"OFFICE-PC","url":"http://192.168.1.24:8114",'
        b'"ip":"192.168.1.24","port":8114,"ver":"0.2.0"}'
    )

    def test_master_key(self):
        assert d.derive_key(self.PASSWORD).hex() == self.MASTER

    def test_subkeys(self):
        master = bytes.fromhex(self.MASTER)
        assert d._subkey(master, d.LABEL_PROBE).hex() == self.K_PROBE
        assert d._subkey(master, d.LABEL_REPLY).hex() == self.K_REPLY

    def test_probe_bytes(self):
        master = bytes.fromhex(self.MASTER)
        assert d.build_probe(master, nonce=self.NONCE, now=self.TS) == self.PROBE

    def test_reply_bytes(self):
        master = bytes.fromhex(self.MASTER)
        built = d.build_reply(
            master,
            nonce=self.NONCE,
            name="OFFICE-PC",
            url="http://192.168.1.24:8114",
            ip="192.168.1.24",
            port=8114,
            ver="0.2.0",
            now=self.TS + 1,
        )
        assert built == self.REPLY

    def test_vectors_verify(self):
        master = bytes.fromhex(self.MASTER)
        assert d.parse_probe(self.PROBE, master, now=self.TS) is not None
        assert d.parse_reply(self.REPLY, master, expected_nonce=self.NONCE) is not None
