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
