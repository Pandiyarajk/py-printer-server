# py-printer-server

[![PyPI](https://img.shields.io/pypi/v/py-printer-server)](https://pypi.org/project/py-printer-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/py-printer-server)](https://pypi.org/project/py-printer-server/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Remote print server for a USB-connected printer. Python 3.11+. Runs on the machine your printer is plugged into; phones and
laptops on the same network upload files to it and print them.

> **Disclaimer.** Provided **AS IS**, without warranty of any kind, express or
> implied. **Use entirely at your own risk.** This tool binds a network port on
> the machine it runs on and lets an authenticated user upload files and send
> them to a physical printer attached to that machine. The author accepts no
> liability for data loss, wasted paper or ink, hardware damage, unauthorised
> access or disclosure, business interruption, or consequential damages. You are
> responsible for setting a strong `ADMIN_PASSWORD` (the server refuses to start
> without one), limiting the server to a network you trust, verifying the
> printer and paper tray before printing, and being authorised to print the
> files you upload. Not certified for regulated, forensic, safety-critical or
> high-assurance use. See [DISCLAIMER.md](DISCLAIMER.md);
> [LICENSE](LICENSE) is the governing text and prevails where the two differ.

## Install

```bash
pip install py-printer-server
```

## Run

```powershell
$env:ADMIN_PASSWORD = "your-strong-password"
py-printer-server
```

`pps` is installed as a shorter alias for the same command. No `ADMIN_PASSWORD`
set? Get a suggestion with:

```powershell
py-printer-server --generate-password
```

The server prints its LAN URL on startup, e.g. `http://192.168.1.24:8114` --
open that from a phone on the same Wi-Fi, log in, and upload a file.

### Options

```
--port PORT        Port (default: 8114)
--spool DIR         Spool folder for uploaded files (default: ./spool)
--jobs DIR          Archive folder for printed jobs (default: ./jobs)
--list-printers      List installed printers (hardware vs virtual) and exit
--dry-run            Log what would be printed instead of sending jobs to a printer
--generate-password  Print a strong random ADMIN_PASSWORD suggestion and exit
--no-qr              Do not print a QR code for the LAN URL on startup
--no-discovery       Do not answer UDP discovery probes from companion apps
--discovery-port     UDP port for discovery (default: 8114)
--mdns               Also advertise over mDNS (visible to every device on the LAN)
--mdns-name NAME     Instance name to advertise over mDNS (default: this hostname)
--discover           Probe the network for print servers, then exit
```

## How it works

1. Upload one or more files from any device on the network -- they land in a
   single flat spool folder. There is no folder browsing; uploading is the
   whole interaction.
2. Tick the files you want (or "Print all"), pick a printer, colour/mono, A4,
   copies and duplex, and press Print.
3. Printed files are **moved**, not deleted, into
   `jobs\print-job-<timestamp>-<id>\`, alongside a `job.json` recording what
   was printed and how -- so nothing needs re-uploading to print again.
   A file that failed or could not be printed **stays in the spool** so you
   can retry it; only successfully printed files are archived.

A job is reported `done` only when every file actually printed. A mixed result
is `partial`, and the jobs panel shows the reason next to each file that did
not print.

Only real hardware printers are shown by default (a "Show all printers"
checkbox reveals software sinks like Microsoft Print to PDF).

## Supported files

| Type | Path |
|---|---|
| Plain text / code (`.txt .log .csv .md .py .json ...`) | Printed directly by this tool. Full control over colour, copies and duplex. |
| PDF and images (`.pdf .png .jpg .tif .bmp .gif`) | Rendered and printed by this tool. Full control over colour, copies and duplex. |
| Office documents (`.docx .xlsx .pptx .doc .xls .ppt`) | Same shell handoff, via Word/Excel/PowerPoint. |

**Colour, copies and duplex apply to text, images and PDFs**, which this server
renders and prints itself. Office documents are still handed to Word, Excel or
PowerPoint, which build their own print settings, so those follow the printer's
standing Windows defaults. The jobs list says which you got for each file.

Until 0.5.0 these settings applied to *nothing*: the server built the right
settings and then never attached them to the print job, so everything came out
at the printer's own defaults. If you set this printer's Windows default to
mono as a workaround, you can set it back.

## Dependencies

Two, both required: **Pillow** and **pypdfium2**, for decoding images and
rendering PDF pages. They exist for one reason: per-job colour control is
impossible without rendering pages ourselves, because handing a file to
Windows' own handler cannot carry print settings.

Everything else is still standard library, deliberately: Win32 access is
hand-written `ctypes` against `winspool.drv`/`gdi32`/`shell32`, and the HTTP
server, LAN discovery, mDNS and QR code are all stdlib. `pywin32` in particular
stays rejected, since `winspool.py` already does that job.

## Finding the server from an app

Typing `http://192.168.1.24:8114` gets old once DHCP moves the address, so the
server answers a discovery probe on **UDP 8114** and replies with the URL it is
reachable on. A companion Android app broadcasts a probe and opens the answer in
a WebView.

The beacon is gated on a shared secret derived from `ADMIN_PASSWORD`, the same
password that logs into the web UI. **A probe without a valid signature gets no
reply at all**, so the server does not announce itself to a port scanner, and an
app has to be told the password before it can find anything.

Try it without an app, from any machine on the network that has the package
installed and the same `ADMIN_PASSWORD` set:

```powershell
py-printer-server --discover
```

```
Probing UDP 8114 for print servers sharing this ADMIN_PASSWORD...
  OFFICE-PC                http://192.168.1.24:8114  (v0.2.0)
```

The discovery port is deliberately **independent of `--port`**. A client has to
be able to find a server whatever HTTP port it was started on, so the beacon
stays on 8114 and the reply carries the real port.

The wire format is specified in [PROTOCOL.md](PROTOCOL.md), in enough detail to
implement a client without reading the Python.

### Windows Firewall

This is the first thing to check when the web UI works from a phone but an app
finds nothing. The prompt Windows showed on first run covered **TCP**; the UDP
responder usually has no rule at all, and no prompt appears when the server runs
without an interactive desktop session.

In an **elevated** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "py-printer-server discovery" `
  -Direction Inbound -Protocol UDP -LocalPort 8114 -Action Allow -Profile Private
```

That rule only applies on a **Private** network. Windows classifies new networks
as Public, where inbound is blocked wholesale, so check and fix the profile too:

```powershell
Get-NetConnectionProfile
Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
```

Other reasons a probe finds nothing: a different `ADMIN_PASSWORD` (silence is by
design), client isolation on a guest Wi-Fi network (which blocks all traffic
between devices, so the web UI will not load either), or a VPN on the phone.

### mDNS

`--mdns` additionally advertises `_pyprint._tcp` so Android's `NsdManager` and
generic DNS-SD browsers can see the server.

It is **off by default and it is not gated**. An mDNS advertisement is an
unauthenticated broadcast by nature, so turning it on makes this machine visible
to everything on the network. The advertisement is kept minimal (service type,
hostname, port, and a `path=/` TXT record; no printer name, version or user), and
the startup banner says so whenever it is enabled. It needs inbound UDP 5353
through the firewall as well.

## Security

- The server refuses to start unless `ADMIN_PASSWORD` is set.
- Login attempts are rate-limited (5 failures locks an IP out for 5 minutes).
- A login lasts a year and survives a server restart, so a phone does not have
  to log in again every day. It is revocable two ways: **Log out** on any page
  ends that device's session, and **changing `ADMIN_PASSWORD` signs out every
  device everywhere**, because each session records which password issued it.
- Session tokens live in `sessions.json` inside the spool folder. That file is
  hidden from the file listing and never served. Treat it as a credential:
  anyone who can read it can act as you until the password changes.
- Sessions use `HttpOnly`, `SameSite=Strict` cookies with CSRF tokens on every
  state-changing request.
- Windows-only: printer access is unavailable on any other OS.
- Discovery probes must be signed with a key derived from `ADMIN_PASSWORD`
  (PBKDF2-HMAC-SHA256); an unsigned or wrongly signed probe is answered with
  silence, not an error.
- Traffic is plain HTTP. Discovery is authenticated, but the login POST and
  the session cookie still cross the LAN in the clear, so treat this as a
  tool for a network you trust.

## License

MIT. See [LICENSE](LICENSE).
