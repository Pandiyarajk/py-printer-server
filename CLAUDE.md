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

- **Two dependencies, both required: Pillow and pypdfium2.** Added in 0.5.0
  because per-job colour control is impossible without rendering pages
  ourselves: handing a file to Windows' own handler cannot carry a devmode.
  **Everything else stays stdlib-only** -- HTTP, LAN discovery, mDNS, QR and
  all Win32 access. `mdns.py` and `qrcode_ascii.py` exist *because* of that
  rule and stay hand-written; do not replace them with a library now that the
  door is ajar. `pywin32` in particular stays rejected: `winspool.py` already
  does its job, and it would duplicate a tested layer with a fragile install.
  The companion file-share project remains zero-dependency; this repo no longer
  matches it, so do not cite it as precedent here.
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
- **A DEVMODE that is built is not a DEVMODE that is applied.** Until 0.5.0
  `PrinterSession` built a perfectly correct devmode and then handed it to
  nothing: `open_printer` passed `pDevMode=None` and `session.devmode` was read
  nowhere, so every job printed at the printer's standing defaults and
  unchecking Colour did nothing. When a setting appears not to work, check in
  this order: (1) did the devmode reach `OpenPrinterW`'s `PRINTER_DEFAULTS` or
  `CreateDCW`'s `lpInitData`, (2) is the field's `dmFields` bit set, (3) did
  `DocumentPropertiesW` clamp it (`devmode.unapplied_settings` reports this).
  The old note sent readers straight to (2), the one part already correct.
- **The settings logic lives in `devmode.py`, which imports no ctypes**, so it
  is testable off Windows. `winspool.py` re-exports its constants, so
  `winspool.DM_COLOR` still works. `pagelayout.py` is the same idea for the
  fit-to-page geometry.
- **Do not let GDI shrink a bitmap.** Its default stretch mode is
  `BLACKONWHITE`, which discards pixels rather than averaging them, and
  Pillow's `Dib.draw` never calls `SetStretchBltMode`. `render._resample_for`
  downscales with LANCZOS first so GDI only ever enlarges. A photo left for GDI
  to shrink prints as noise, and nothing errors.
- **Normalise every image before `ImageWin.Dib`**: P-mode raises outright, and
  `RGBA.convert("RGB")` drops alpha without compositing, so a transparent
  background prints as a solid black rectangle. `render.to_printable` is the
  one funnel that handles both, plus the greyscale conversion.
- **Mono is enforced twice on purpose**: `dmColor = DMCOLOR_MONOCHROME` *and* a
  greyscale raster. The devmode is the correct mechanism, but the bug that
  prompted this was a setting being ignored downstream, and a grey raster
  cannot come out coloured whatever the driver does. It also keeps colour ink
  off the page on an ink-tank device that would otherwise mix composite black.
- **`DC_COLORDEVICE` is 32, not 6.** It was 6 (which is `DC_BINS`), so
  `supports_color` was reading the paper-bin count, and a comment in
  `printers.py` rationalised that as a driver quirk.
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
- Office files still have no per-job duplex/colour control, because they are
  handed to Word/Excel/PowerPoint. That gap is now reported per file on the job
  record (`JobFile.settings`) rather than claimed away in a blanket sentence.
  The original design rationale (why SumatraPDF and pywin32 were rejected) is
  in the plan file this repo was built from:
  `C:\Users\kpandiyaraj\.claude\plans\i-have-files-on-merry-dolphin.md`.

## Testing

`python -m pytest` from the repo root. Tests write to `tmp_path` only.
`test_winspool.py` and `test_printers.py` make live (read-only) Win32 calls
and require Windows; they do not require a specific printer to be attached,
only that Windows has at least one printer driver installed (even a virtual
one satisfies this).
