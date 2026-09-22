# py-printer-server discovery protocol, version 1

Author: Pandiyaraj Karuppasamy
Date: Sep-22-2026

This file is the contract between the Python server in this repo and the
Android client in `android-printer-client`. Keep the two copies identical.

Everything here is frozen. Once a client ships, changing any byte below stops
every installed copy from finding the server. Add a new `v` instead.

## 1. Transport

UDP, IPv4, port **8114**.

The discovery port is **fixed**. It does not follow `--port`. If it moved with
the HTTP port, a client probing 8114 could never find a server started with
`--port 9000`, so discovery would be impossible to bootstrap. The reply carries
the actual HTTP port. `--discovery-port` exists for a genuine UDP collision, at
the cost of the client needing manual entry.

The client broadcasts; the server answers unicast to the datagram's source
address and port, from the listening socket, so the source port of the reply is
also 8114.

Maximum datagram: **1024 bytes** in either direction. Anything larger is dropped
unparsed. Replies are kept under 512 bytes so they never fragment.

## 2. Key derivation

The shared secret is the server's `ADMIN_PASSWORD`, the same password that logs
into the web UI.

```
master  = PBKDF2-HMAC-SHA256(
              password   = ADMIN_PASSWORD, encoded UTF-8,
              salt       = "py-printer-server/discovery/v1", ASCII bytes,
              iterations = 200000,
              dkLen      = 32)

k_probe = HMAC-SHA256(master, "pps-discovery-v1/probe")
k_reply = HMAC-SHA256(master, "pps-discovery-v1/reply")
```

PBKDF2, rather than using the password directly as an HMAC key, because every
probe on the wire is a (message, tag) pair under that password. A single
captured datagram would otherwise be an offline oracle a GPU could grind at
billions of guesses per second. 200000 iterations costs roughly 100ms once at
startup, and 100 to 300ms once on a phone, so both sides must cache the derived
key rather than deriving per packet.

The salt is fixed and public because there is no channel to negotiate one before
the first packet. This is therefore a per-password work factor, not
rainbow-table protection, which is why the README pushes `--generate-password`.

Two direction-separated subkeys, so a captured probe tag is structurally
unusable as a reply tag.

**Passwords are ASCII only.** `--generate-password` emits
`secrets.token_urlsafe`, which is ASCII, so this costs nothing in practice and
avoids a Unicode normalisation mismatch between Python and the JVM. A client
should warn if the user types a non-ASCII password.

### Implementing PBKDF2 on Android

Do **not** use `SecretKeyFactory` with `PBEKeySpec`. `PBEKeySpec` takes a
`char[]` and the provider decides how to encode it; `PBKDF2WithHmacSHA256` is
UTF-8 on Conscrypt, but this is the single most likely place for a silent
cross-language mismatch.

Because `dkLen == hLen == 32`, the derivation is exactly one PBKDF2 block, so it
is about 25 lines of `Mac` loop with an unambiguous
`password.toByteArray(Charsets.UTF_8)`:

```
U_1 = HMAC-SHA256(pw, SALT || 0x00000001)
U_i = HMAC-SHA256(pw, U_{i-1})
T   = U_1 xor U_2 xor ... xor U_200000
```

The single-block simplification is only valid while `dkLen` is 32. Comment and
test it; raising `dkLen` silently breaks it.

## 3. Frame

```
PPS1 <tag> <payload>
```

Three fields separated by single spaces, split on the first two spaces only, so
the payload may itself contain spaces (a printer name, for instance).

| field | bytes | meaning |
|---|---|---|
| magic | 4 | ASCII `PPS1`. A datagram not starting with `PPS1 ` is dropped before any crypto. |
| tag | 22 | base64url, unpadded, of the first 16 bytes of the MAC |
| payload | rest | compact JSON, UTF-8 |

```
tag = base64url( HMAC-SHA256(k_dir, dir_label || 0x00 || payload)[0:16] )
```

`dir_label` is the ASCII bytes `probe` or `reply`. `k_dir` is `k_probe` or
`k_reply` to match.

**The tag covers the literal payload bytes as sent or received, never a
re-serialisation.** This is the single most important rule for interoperability.
`org.json` on Android is HashMap-backed and does not preserve key order, so any
"canonical JSON" agreement would be a standing trap. Instead:

- when sending, serialise once and MAC exactly those bytes;
- when receiving, MAC exactly the bytes that arrived, verify, and only then
  parse.

Verify the tag with a constant-time comparison, before parsing JSON. Never use
`String.equals` on a MAC.

## 4. Probe

Client to `255.255.255.255:8114` and to each interface's directed subnet
broadcast.

```json
{"v":1,"t":"probe","n":"3Qk1s9_TfR2mXbA0pLzQ7g","ts":1758499200}
```

| field | type | meaning |
|---|---|---|
| `v` | int | protocol version, must be `1` |
| `t` | string | must be `"probe"` |
| `n` | string | nonce: 16 random bytes, base64url unpadded, 22 chars |
| `ts` | int | client unix seconds, decimal, no padding |

The nonce must come from a cryptographic RNG (`SecureRandom`,
`secrets.token_bytes`), fresh per scan.

## 5. Reply

Server to the probe's source address, unicast.

```json
{"v":1,"t":"reply","n":"3Qk1s9_TfR2mXbA0pLzQ7g","ts":1758499201,
 "name":"OFFICE-PC","url":"http://192.168.1.24:8114",
 "ip":"192.168.1.24","port":8114,"ver":"0.2.0"}
```

| field | type | meaning |
|---|---|---|
| `v` | int | `1` |
| `t` | string | `"reply"` |
| `n` | string | the probe's nonce, echoed |
| `ts` | int | server unix seconds |
| `name` | string | server hostname, truncated to 63 characters |
| `url` | string | the URL to open, ready to hand to a WebView |
| `ip` | string | same address as in `url`, so the client need not parse it |
| `port` | int | the HTTP port, 1 to 65535 |
| `ver` | string | server version, for display |

`name` is only ever revealed to a peer that has already proved knowledge of the
shared secret, so it is not a disclosure.

## 6. Server acceptance rules

In this order. Any failure produces **no reply at all**:

1. length is 1024 bytes or less
2. starts with `PPS1 `, splits into exactly three fields
3. tag is 22 bytes and verifies under `k_probe`
4. payload parses as a JSON object, `v == 1`, `t == "probe"`, `n` is 22
   characters from the base64url alphabet, `ts` is an integer (not a boolean)
5. `abs(now - ts) <= 120`
6. `n` has not been seen inside the last 120 seconds

Silence is the feature: a port scanner, a client with the wrong password, and a
replayer must all be unable to tell each other's case apart from outside.

Because a clock-skew rejection is indistinguishable from a wrong password to the
user, the server logs rejections at DEBUG with a reason word (`bad-mac`,
`stale-ts`, `replay`, `bad-version`) and accepts at INFO. A `bad-mac` line never
logs the payload.

### Replay window

The server keeps the nonces it has seen within the skew window, bounded at 4096
entries. A captured probe would otherwise let an eavesdropper re-confirm the
server indefinitely without knowing the password.

Known and accepted limitation: replay **inside** the 120 second window still
works. Closing that needs a challenge-response round trip, which is not worth a
second datagram to defend against an attacker who already has on-path presence
on the LAN.

## 7. Client acceptance rules

1. tag verifies under `k_reply`
2. `t == "reply"`
3. `n` equals the nonce **this scan** generated, compared constant-time
4. `port` is an integer in 1 to 65535, the string fields are strings
5. the claimed `ip` equals the datagram's source address

Rule 3 is the mutual authentication: only a holder of the shared secret could
MAC a message carrying a value that was random a few hundred milliseconds ago.
Without it, a replayed reply could point the client at a stale or
attacker-chosen address. No timestamp check is needed on the reply.

Rule 5 is defence in depth. The `ip` field is inside the MAC so it cannot be
altered in flight, but preferring the packet's real source costs nothing.

A reply that fails the MAC means something answered that does not know the
password. That is worth surfacing in a client UI as a distinct state, because it
is genuinely different from finding nothing.

## 8. Amplification

The reply is about 1.6 times the size of the probe. The MAC gate already defeats
a spoofed-source attacker, since they cannot forge a probe, but the server also
caps replies at 20 per second globally and 5 per second per source address.

## 9. What this does and does not protect

It authenticates **discovery**, not the HTTP session that follows. Anyone on the
LAN can still reach `http://ip:8114` and see the login page. The login POST and
the session cookie cross the LAN in cleartext, because the server is HTTP only.
PBKDF2 raises the offline cost of cracking a captured datagram but does not
eliminate it: a weak `ADMIN_PASSWORD` is still crackable.

What it does buy: a passive scanner learns nothing, and a client cannot be lured
to a rogue URL.

## 10. mDNS, the optional second transport

Off by default, enabled with `--mdns`. It carries **no secret and cannot be
gated**, which is exactly why it is opt-in.

- service type `_pyprint._tcp.local.`
- instance name: the hostname
- TXT: `path=/` and `v=1` only. No printer model, OS, version, spool path or
  user name.

A client must treat an mDNS hit as a **hint** and confirm it by sending the
signed probe from section 4 unicast to that address. A confirmed hit then earns
the same trust as a broadcast one, and the unicast path also works on networks
where the access point filters broadcast.

## 11. Test vectors

Both implementations assert these. A divergence fails the build rather than the
phone.

```
password = "hunter2"
nonce    = "AAAAAAAAAAAAAAAAAAAAAA"
ts       = 1758499200

master  = 68ca0d69c993cd44188a21066b7ce34a67fee2c92d2fd25685008029bb63ce09
k_probe = 09e03c07fcfa7da7a76ed36dad835c1126ff1bfdd1d6f326fbc29793855bd89e
k_reply = 3c6cc4bfc1140011c3ad03fd146c4d87d8f8bff0a685bae43f135f9685a98e97

probe = PPS1 2bIo5Zv2zr6DSHMRwVXn_w {"v":1,"t":"probe","n":"AAAAAAAAAAAAAAAAAAAAAA","ts":1758499200}

reply (ts 1758499201, name "OFFICE-PC", url "http://192.168.1.24:8114",
       ip "192.168.1.24", port 8114, ver "0.2.0") =
PPS1 3ohJECh3lDn8wuSWg9Zh5w {"v":1,"t":"reply","n":"AAAAAAAAAAAAAAAAAAAAAA","ts":1758499201,"name":"OFFICE-PC","url":"http://192.168.1.24:8114","ip":"192.168.1.24","port":8114,"ver":"0.2.0"}
```

Reference implementation: `py_printer_server/discovery.py`, with the client side
in `py_printer_server/discovery_net.py` (`discover()`, which backs
`py-printer-server --discover`).
