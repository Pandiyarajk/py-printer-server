# py-printer-server

[![PyPI](https://img.shields.io/pypi/v/py-printer-server)](https://pypi.org/project/py-printer-server/)
[![Python versions](https://img.shields.io/pypi/pyversions/py-printer-server)](https://pypi.org/project/py-printer-server/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)

Remote print server for a USB-connected printer. Python 3.11+, no third-party
dependencies. Runs on the machine your printer is plugged into; phones and
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
| Plain text / code (`.txt .log .csv .md .py .json ...`) | Printed directly by this tool -- full control over colour, copies and duplex. |
| PDF and images (`.pdf .png .jpg .tif .bmp .gif`) | Handed to Windows' registered handler for that file type via the shell's print verb. |
| Office documents (`.docx .xlsx .pptx .doc .xls .ppt`) | Same shell handoff, via Word/Excel/PowerPoint. |

**Colour, copies and duplex apply fully only to the text path.** PDFs, images
and Office documents are printed by whichever application Windows has
associated with them, and that application builds its own print settings, so
those files follow the printer's standing defaults instead. This is a real
limitation of printing without a bundled renderer, not a bug.

## No third-party dependencies

Everything, including Win32 printer access, uses only the Python standard
library (`ctypes` bindings to `winspool.drv`/`shell32.dll`, `http.server`,
`socketserver`). This is a deliberate choice, matching the companion project
[py-file-server](https://pypi.org/project/py-file-server/): fewer things that
can go missing or need a security update on a machine you may not touch again
for a year.

## Security

- The server refuses to start unless `ADMIN_PASSWORD` is set.
- Login attempts are rate-limited (5 failures locks an IP out for 5 minutes).
- Sessions use `HttpOnly`, `SameSite=Strict` cookies with CSRF tokens on every
  state-changing request.
- Windows-only: printer access is unavailable on any other OS.

## License

MIT. See [LICENSE](LICENSE).
