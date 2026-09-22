# py-printer-server

Remote print server for a USB-connected printer. This repo follows the
**engineering-hub** `pypi-package` profile.

- Canonical standards live at `D:\GitHub\engineering-hub` (see `hub.json` ->
  `profiles.pypi-package` for the exact rule/skill list). Read
  `D:\GitHub\engineering-hub\rules\core\*.mdc` before making non-trivial
  changes here.
- **No `.github/workflows`** in this repo -- per the global CLAUDE.md rule,
  CI is added only when explicitly asked.
- Release with the `pypi-release` skill.

## Project-specific notes

- **Zero third-party dependencies**, deliberately, matching the companion
  project `D:\GitHub\file-share` (PyPI `py-file-server`). Win32 printer access
  goes through hand-written `ctypes` bindings in `py_printer_server/winspool.py`,
  not `pywin32`. Do not add `pywin32`, Pillow, ReportLab, or `qrcode` without
  discussing it first -- that constraint was a deliberate user decision, not
  an oversight.
- **Windows-only.** `winspool.py` raises at import time on any other platform.
  Keep that import out of module scope in `server.py` so the rest of the test
  suite can still run (or at least collect) on a non-Windows CI runner if one
  is ever added.
- **PyPI name already reserved**: `py-printer-server` 0.0.0 was published as a
  placeholder on Sep-14-2026. That version number is permanently burned --
  the first real release must be `0.1.0` or higher.
- **`DEVMODEW` layout is the highest-risk part of this codebase.** If a print
  setting (colour, paper size, copies, duplex) appears to have no effect,
  check `dmFields` in `py_printer_server/winspool.py` first -- a devmode field
  written without its corresponding `dmFields` bit set is silently ignored by
  the driver, not by a bug in this code.
- **Per-job settings go through `DocumentPropertiesW`, never
  `SetPrinterW`.** `winspool.build_job_devmode` builds a DEVMODE scoped to one
  document. Using `GetPrinterW`/`SetPrinterW(level=2)` instead -- which an
  early version did -- is wrong twice: it needs the "Manage this printer"
  right an ordinary user lacks, and it rewrites the printer's *global*
  defaults, changing what every other app on the machine prints.
- **Never size a DEVMODE buffer with `sizeof(DEVMODEW)`.** Drivers append
  private data after the public struct (measured: 15356 bytes vs a 224-byte
  public struct on an HP inkjet). Always take the size from
  `DocumentPropertiesW`'s size query.
- **Plain text prints with datatype `TEXT`, not `RAW`, encoded cp1252.** RAW
  means "already in the printer's language"; sending plain text as RAW to a
  PCL/host-based printer produces nothing or garbage.
- **A job reported `done` must mean paper came out.** `JobQueue._overall_status`
  deliberately treats `queued`, `unsupported` and mixed outcomes as not-done
  (`error`/`partial`/`unsupported`), and only files with status `done` are
  archived out of the spool -- failures stay so they can be retried.
- **The page template is rendered with `str.format`**, so every literal brace
  in its embedded CSS/JS must be doubled. A stray single brace raises at
  request time, not import time, so the UI 500s while unit tests still pass.
  `tests/test_server.py` renders both templates to catch this.
- **The discovery wire format is frozen.** `py_printer_server/discovery.py` and
  `PROTOCOL.md` are the contract with the Android client in
  `D:/GitHub/android-printer-client`. The shared test vectors in
  `tests/test_discovery.py` are asserted on both sides, so if they fail you have
  changed the wire format and every installed app has stopped finding this
  server. Bump `v` rather than editing a field.
- **The beacon signs with PBKDF2, not the raw password as an HMAC key.** Every
  probe on the wire is a (message, tag) pair under the same secret that logs
  into the web UI, so one captured datagram is an offline cracking oracle.
  200k iterations makes that expensive; it looks like over-engineering only
  until you notice what the key is.
- **The reply's IP comes from a per-peer route lookup**
  (`discovery_net.local_ip_for(peer)`), not from `lan_url()`. On a machine with
  a VPN or a second NIC, the default-route address is not the one a phone on
  Wi-Fi can reach, and reporting it hands the app a URL that cannot load.
- **The discovery port does not follow `--port`.** It is fixed at 8114 so a
  client can find a server whatever HTTP port it was started on; the reply
  carries the real port. Making it track `--port` would make discovery
  impossible to bootstrap.
- **A rejected probe gets silence, never an error.** A scanner, a client with
  the wrong password, and a replayer must all be unable to tell their cases
  apart. Rejection reasons go to the DEBUG log only.
- **mDNS (`--mdns`) is off by default and cannot be gated**, unlike the beacon.
  It is an unauthenticated advertisement by design, which is why it is opt-in
  and why the banner says so every time it is on.
- **A year-long session is only defensible because it is revocable.** Every
  session records a fingerprint of the password that issued it, so changing
  `ADMIN_PASSWORD` invalidates all of them without reaching any device. If you
  ever drop that binding, drop `SESSION_TTL` back to hours in the same commit.
- **`sessions.json` must stay in `_HIDDEN_SPOOL_NAMES`.** It lives in the spool
  folder and holds live tokens; listing it would also make it downloadable.
- The full design rationale, trade-offs (no PDF rendering, no per-job
  duplex/colour control for Office files, why SumatraPDF and pywin32 were
  rejected) is in the plan file this repo was built from:
  `C:\Users\kpandiyaraj\.claude\plans\i-have-files-on-merry-dolphin.md`.

## Testing

`python -m pytest` from the repo root. Tests write to `tmp_path` only.
`test_winspool.py` and `test_printers.py` make live (read-only) Win32 calls
and require Windows; they do not require a specific printer to be attached,
only that Windows has at least one printer driver installed (even a virtual
one satisfies this).
