"""HTTP server: upload box, flat spool listing, print bar.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026

Session/CSRF/rate-limit/upload machinery ported from file-share's
py_file_server/server.py, trimmed for a single flat spool folder: no
directory tree, breadcrumbs, mkdir/rename/rmdir, or the card/detail
dual-view toggle -- users only upload here, so there is nothing to browse.
"""

from __future__ import annotations

import argparse
import base64
import http.server
import json
import logging
import logging.handlers
import os
import secrets
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
from email.message import Message
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from py_printer_server.config import Config

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = 8114
SPOOL_DIR = os.path.join(os.getcwd(), "spool")
JOBS_DIR = os.path.join(os.getcwd(), "jobs")
CHUNK = 1024 * 1024
LOG_FILE = "print-server.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
# Cap for bodies that are never file uploads (login, print, delete). These
# are small JSON/form posts; anything larger is a mistake or an attempt to
# make the server buffer arbitrary memory.
SMALL_BODY_LIMIT = 1024 * 1024

SESSION_TTL = 8 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300

ADMIN_PASSWORD_ENV = "ADMIN_PASSWORD"

# The server's own files, which live in the spool dir but are not uploads.
_HIDDEN_SPOOL_NAMES = frozenset({"config.json"})

# Reserved DOS device names: opening one of these resolves to a device rather
# than a file in the spool, regardless of the directory.
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

DISCONNECT_ERRORS = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
)

FAVICON_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    b"YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

DISCLAIMER_EPILOG = """Provided AS IS, without warranty of any kind. Use entirely at your own risk.
This server exposes an upload spool over the network and prints files on a
physical printer attached to this machine. Anyone who can reach the port and
log in can upload files and spend this printer's paper and ink. Set a strong
ADMIN_PASSWORD (required -- the server refuses to start without one), verify
the printer and paper tray before printing anything you did not author
yourself, and keep backups of anything uploaded. The author accepts no
liability for data loss, wasted consumables, hardware damage or unauthorised
disclosure. See DISCLAIMER.md; LICENSE is the governing text.
"""

# JobQueue is created in main() once the print worker is wired up; routes
# below read it through this module-level reference so Handler methods do
# not need it threaded through every call.
job_queue = None  # type: ignore[assignment]


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Remote print server for a USB-connected printer",
        epilog=DISCLAIMER_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=PORT, help=f"Port (default: {PORT})")
    parser.add_argument(
        "--spool",
        default=SPOOL_DIR,
        help="Spool folder for uploaded files (default: ./spool)",
    )
    parser.add_argument(
        "--jobs",
        default=JOBS_DIR,
        help="Archive folder for printed jobs (default: ./jobs)",
    )
    parser.add_argument(
        "--list-printers", action="store_true",
        help="List installed printers (hardware vs virtual) and exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be printed instead of sending jobs to a printer",
    )
    parser.add_argument(
        "--generate-password", action="store_true",
        help="Print a strong random ADMIN_PASSWORD suggestion and exit",
    )
    parser.add_argument(
        "--no-qr", action="store_true",
        help="Do not print a QR code for the LAN URL on startup",
    )
    return parser.parse_args()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("printer_server")
logger.setLevel(logging.INFO)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
logger.addHandler(_console_handler)


def _attach_file_logging(directory: str) -> None:
    """Add the rotating file handler, writing inside the spool folder.

    Only attached once the spool directory is known and created -- opening a
    log file at import time would make the package unimportable from any
    directory the user cannot write to.
    """
    path = os.path.join(directory, LOG_FILE)
    try:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("file logging disabled, cannot write %s: %s", path, exc)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)


def human(size: float) -> str:
    """Convert a byte count to a human-readable string (e.g. 1.4 MB)."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def lan_url(port: int) -> str:
    """Best-effort LAN-reachable URL for this machine.

    Uses the UDP-connect-to-a-public-address trick to learn the local
    outbound IP without sending any packet -- connecting a UDP socket never
    transmits, it only asks the OS to pick a route. socket.gethostbyname on
    the local hostname is not used because it returns 127.0.0.1 on a
    surprising number of Windows configurations, which would print a URL a
    phone cannot reach.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return f"http://{ip}:{port}"

# ---------------------------------------------------------------------------
# Session store  (in-memory, thread-safe)
# ---------------------------------------------------------------------------

_sessions_lock = threading.Lock()
_sessions: dict[str, dict] = {}


def _prune_sessions() -> None:
    now = time.time()
    for t in [t for t, v in _sessions.items() if v["expires"] < now]:
        del _sessions[t]


def session_create() -> tuple[str, str]:
    token = secrets.token_hex(32)
    csrf = secrets.token_hex(24)
    with _sessions_lock:
        _prune_sessions()
        _sessions[token] = {"expires": time.time() + SESSION_TTL, "csrf": csrf}
    return token, csrf


def session_get(token: str) -> dict | None:
    with _sessions_lock:
        s = _sessions.get(token)
        if s and s["expires"] >= time.time():
            return s
        if s:
            del _sessions[token]
    return None


def session_delete(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)

# ---------------------------------------------------------------------------
# Login rate limiter  (per IP, in-memory)
# ---------------------------------------------------------------------------

_rl_lock = threading.Lock()
_rl: dict[str, tuple[int, float]] = {}


def rl_check(ip: str) -> bool:
    with _rl_lock:
        entry = _rl.get(ip)
        if not entry:
            return False
        count, start = entry
        if time.time() - start > LOCKOUT_SECONDS:
            del _rl[ip]
            return False
        return count >= MAX_FAILURES


def rl_fail(ip: str) -> None:
    with _rl_lock:
        entry = _rl.get(ip)
        if entry and time.time() - entry[1] <= LOCKOUT_SECONDS:
            _rl[ip] = (entry[0] + 1, entry[1])
        else:
            _rl[ip] = (1, time.time())


def rl_reset(ip: str) -> None:
    with _rl_lock:
        _rl.pop(ip, None)

# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class Handler(http.server.SimpleHTTPRequestHandler):
    """
    Routes:
        GET  /              — upload box + flat spool listing + print bar
        GET  /login         — admin login form
        POST /login         — process login, set session cookie
        GET  /logout        — invalidate session, redirect to /
        GET  /printers      — JSON list of installed printers
        GET  /jobs          — JSON list of recent print jobs (for polling)
        GET  /jobs/<id>     — JSON detail of one job
        POST /upload        — multipart upload into the spool
        POST /print         — submit selected spool files as a print job
        POST /delete        — remove a spool file without printing it
        GET  /settings      — current default print options (admin only)
        POST /settings      — save default print options (admin only)
        GET  /archive       — archived (printed) job folders, with delete UI
        POST /archive/delete — permanently delete one or more archived job folders
    """

    def parse_path(self, raw_path: str) -> tuple[str, dict]:
        parsed = urlparse(raw_path)
        return unquote(parsed.path), parse_qs(parsed.query)

    def translate_name(self, name: str) -> str | None:
        """Resolve a user-supplied filename against the spool dir.

        The spool is a single flat folder, so this is deliberately stricter
        than a general path-translate: any separator or a name that is not
        exactly its own basename is rejected outright, rather than merely
        normalised. Names arrive from phones over the network, so the
        traversal guard matters more here, not less.
        """
        if not name or name in (".", ".."):
            return None
        if "/" in name or "\\" in name or os.path.basename(name) != name:
            return None
        # ':' would address an NTFS alternate data stream ("file.txt:hidden"),
        # which contains no separator and survives basename() unchanged. The
        # wildcards are rejected for the same reason: they are not valid in a
        # real filename here, so their presence means the name did not come
        # from our own listing.
        if any(ch in name for ch in ':*?"<>|') or "\x00" in name:
            return None
        # Reserved DOS device names resolve to devices, not files, whatever
        # directory they appear to be in.
        stem = name.split(".")[0].upper()
        if stem in _RESERVED_DEVICE_NAMES:
            return None
        full = os.path.normpath(os.path.join(SPOOL_DIR, name))
        base = os.path.normpath(SPOOL_DIR)
        if full != base and not full.startswith(base + os.sep):
            return None
        return full

    def translate_job_dir(self, name: str) -> str | None:
        """Resolve a user-supplied archive folder name against JOBS_DIR.

        Same traversal guard as translate_name: reject anything that is not
        exactly its own basename, rather than merely normalising it. These
        names arrive from a network client requesting a permanent delete, so
        there is no room for "probably fine".
        """
        if not name or name in (".", ".."):
            return None
        if "/" in name or "\\" in name or os.path.basename(name) != name:
            return None
        if any(ch in name for ch in ':*?"<>|') or "\x00" in name:
            return None
        full = os.path.normpath(os.path.join(JOBS_DIR, name))
        base = os.path.normpath(JOBS_DIR)
        if full != base and not full.startswith(base + os.sep):
            return None
        return full

    def _read_exactly(self, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, CHUNK))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _session_token(self) -> str | None:
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        m = jar.get("session")
        return m.value if m else None

    def is_admin(self) -> bool:
        token = self._session_token()
        return token is not None and session_get(token) is not None

    def _csrf_token(self) -> str | None:
        token = self._session_token()
        if not token:
            return None
        s = session_get(token)
        return s["csrf"] if s else None

    def _validate_csrf(self) -> bool:
        expected = self._csrf_token()
        if not expected:
            return False
        provided = self.headers.get("X-CSRF-Token", "")
        return secrets.compare_digest(expected, provided)

    def send_html(self, html: str, status: int = 200, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(html.encode())

    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_text(self, status: int, msg: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(msg.encode())

    def redirect(self, location: str, extra_headers: dict | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()

    def _session_cookie_header(self, token: str) -> str:
        return f"session={token}; Path=/; HttpOnly; SameSite=Strict"

    def _expire_session_cookie(self) -> str:
        return "session=; Path=/; HttpOnly; SameSite=Strict; Expires=Thu, 01 Jan 1970 00:00:00 GMT"

    def send_favicon(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(FAVICON_BYTES)))
        self.end_headers()
        self.wfile.write(FAVICON_BYTES)

    def log_event(self, event_type: str, client_ip: str, filename: str, size: int) -> None:
        logger.info("%s ip=%s filename=%s size=%d", event_type, client_ip, filename, size)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except DISCONNECT_ERRORS as exc:
            logger.debug("client %s disconnected: %s", self.client_address[0], exc)
            self.close_connection = True

    def handle_error(self, *args) -> None:
        pass

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.client_address[0], fmt % args)

    # ------------------------------------------------------------------
    # Page renderers
    # ------------------------------------------------------------------

    def login_page(self, message: str = "") -> None:
        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Server Login</title>
<style>
:root {{
    --bg: linear-gradient(135deg,#0f172a,#020617);
    --surface: rgba(15,23,42,0.92); --surface2: #1e293b;
    --text: #e2e8f0; --border: #334155; --accent: #3b82f6; --danger: #fda4af;
}}
[data-theme="light"] {{
    --bg: linear-gradient(135deg,#e0e7ff,#f0f9ff);
    --surface: rgba(255,255,255,0.92); --surface2: #f1f5f9;
    --text: #1e293b; --border: #cbd5e1; --accent: #2563eb; --danger: #dc2626;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; min-height: 100vh; }}
.container {{ max-width: 420px; margin: 80px auto; padding: 28px; background: var(--surface); border-radius: 18px; border: 1px solid var(--border); }}
h2 {{ margin-bottom: 20px; }}
input {{ width: 100%; padding: 12px; margin: 8px 0 16px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface2); color: var(--text); font-size: 15px; }}
button {{ width: 100%; padding: 12px; border: none; background: var(--accent); border-radius: 10px; color: white; cursor: pointer; font-size: 16px; }}
.message {{ margin-bottom: 14px; color: var(--danger); font-size: 14px; }}
</style>
</head>
<body>
<div class="container">
    <h2>🖨️ Print Server Login</h2>
    {f'<div class="message">{message}</div>' if message else ''}
    <form method="POST" action="/login">
        <input type="password" name="password" placeholder="Password" autocomplete="off" required>
        <button type="submit">Login</button>
    </form>
</div>
<script>(function(){{const t=localStorage.getItem("theme")||"dark";document.documentElement.setAttribute("data-theme",t);}})();</script>
</body>
</html>"""
        self.send_html(html)

    def main_page(self) -> None:
        """Render the single page: upload box, flat spool listing, print bar."""
        admin = self.is_admin()
        csrf = self._csrf_token() or ""

        if not admin:
            self.redirect("/login")
            return

        entries = []
        try:
            for name in sorted(os.listdir(SPOOL_DIR), key=str.lower):
                # The spool also holds our own bookkeeping: the config file,
                # the rotating log, and .upload_* files staged mid-request.
                # None of those are things the user uploaded to print, and
                # offering them would be confusing at best.
                if name in _HIDDEN_SPOOL_NAMES or name.startswith(".upload_"):
                    continue
                if name == LOG_FILE or name.startswith(LOG_FILE + "."):
                    continue
                full = os.path.join(SPOOL_DIR, name)
                if not os.path.isfile(full):
                    continue
                stat = os.stat(full)
                entries.append((name, stat.st_size, stat.st_mtime))
        except OSError as exc:
            logger.warning("cannot list spool dir %s: %s", SPOOL_DIR, exc)

        rows = ""
        for name, size, mtime in entries:
            mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            rows += f"""
        <tr class="file-row" data-name="{_html_escape(name)}">
            <td><input type="checkbox" class="file-check" value="{_html_escape(name)}"></td>
            <td>{_html_escape(name)}</td>
            <td>{human(size)}</td>
            <td>{mtime_str}</td>
            <td><button class="btn-small delete-btn" data-name="{_html_escape(name)}">🗑</button></td>
        </tr>"""

        empty_notice = (
            '<tr><td colspan="5" class="empty-notice">Spool is empty. Upload a file to print it.</td></tr>'
            if not entries else ""
        )

        html = _MAIN_PAGE_TEMPLATE.format(
            csrf=csrf,
            rows=rows,
            empty_notice=empty_notice,
        )
        self.send_html(html)

    def archive_page(self) -> None:
        """Render the archived-jobs page: one row per printed-job folder."""
        if not self.is_admin():
            self.redirect("/login")
            return
        csrf = self._csrf_token() or ""

        entries = []
        try:
            for name in sorted(os.listdir(JOBS_DIR), key=str.lower, reverse=True):
                full = os.path.join(JOBS_DIR, name)
                if not os.path.isdir(full):
                    continue
                file_count = 0
                total_size = 0
                for root, _dirs, files in os.walk(full):
                    for fname in files:
                        file_count += 1
                        try:
                            total_size += os.path.getsize(os.path.join(root, fname))
                        except OSError:
                            pass
                mtime = os.stat(full).st_mtime
                entries.append((name, file_count, total_size, mtime))
        except OSError as exc:
            logger.warning("cannot list jobs dir %s: %s", JOBS_DIR, exc)

        rows = ""
        for name, file_count, total_size, mtime in entries:
            mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            rows += f"""
        <tr class="archive-row" data-name="{_html_escape(name)}">
            <td><input type="checkbox" class="archive-check" value="{_html_escape(name)}"></td>
            <td>{_html_escape(name)}</td>
            <td>{file_count}</td>
            <td>{human(total_size)}</td>
            <td>{mtime_str}</td>
            <td><button class="btn-small archive-delete-btn" data-name="{_html_escape(name)}">🗑</button></td>
        </tr>"""

        empty_notice = (
            '<tr><td colspan="6" class="empty-notice">No archived jobs yet.</td></tr>'
            if not entries else ""
        )

        html = _ARCHIVE_PAGE_TEMPLATE.format(
            csrf=csrf,
            rows=rows,
            empty_notice=empty_notice,
        )
        self.send_html(html)

    # ------------------------------------------------------------------
    # GET handler
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        request_path, query = self.parse_path(self.path)

        if request_path == "/favicon.ico":
            self.send_favicon()
            return

        if request_path == "/login":
            self.login_page()
            return

        if request_path == "/logout":
            token = self._session_token()
            if token:
                session_delete(token)
            self.redirect("/", extra_headers={"Set-Cookie": self._expire_session_cookie()})
            return

        if request_path == "/":
            self.main_page()
            return

        if request_path == "/archive":
            self.archive_page()
            return

        if request_path == "/printers":
            if not self.is_admin():
                self.send_error(401)
                return
            from py_printer_server.printers import list_printers
            # The query parameter is the per-request override (the UI's
            # checkbox); the config key is the saved default when absent.
            if "all" in query:
                show_virtual = query["all"][0] == "1"
            else:
                show_virtual = Config.show_virtual_printers
            printers = list_printers(show_virtual=show_virtual)
            self.send_json([
                {
                    "name": p.name, "port": p.port, "driver": p.driver,
                    "is_virtual": p.is_virtual, "is_default": p.is_default,
                    "supports_color": p.supports_color,
                }
                for p in printers
            ])
            return

        if request_path == "/settings":
            if not self.is_admin():
                self.send_error(401)
                return
            self.send_json(_current_settings())
            return

        if request_path == "/jobs":
            if not self.is_admin():
                self.send_error(401)
                return
            jobs = job_queue.list_recent() if job_queue else []
            self.send_json({
                "server_time": time.time(),
                "jobs": [_job_to_dict(j) for j in jobs],
            })
            return

        if request_path.startswith("/jobs/"):
            if not self.is_admin():
                self.send_error(401)
                return
            job_id = request_path[len("/jobs/"):]
            job = job_queue.get(job_id) if job_queue else None
            if job is None:
                self.send_error(404)
                return
            self.send_json(_job_to_dict(job))
            return

        self.send_error(404)

    # ------------------------------------------------------------------
    # POST handler
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        request_path, _ = self.parse_path(self.path)
        client_ip = self.client_address[0]
        content_type = self.headers.get("Content-Type", "")

        # A client-supplied header must never reach int() unguarded: a
        # malformed Content-Length would raise ValueError here, before any
        # auth check, and return a 500 with a traceback in the log.
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self.send_error_text(400, "Invalid Content-Length")
            return
        if length < 0:
            self.send_error_text(400, "Invalid Content-Length")
            return

        if request_path == "/login":
            # Cap the body before reading it. /login is the one route
            # reachable without credentials, so an uncapped read here lets an
            # anonymous client announce a multi-gigabyte body and have the
            # server try to buffer it.
            if length > SMALL_BODY_LIMIT:
                self.send_error_text(413, "Request body too large")
                return
            if rl_check(client_ip):
                self.send_error_text(429, "Too many failed attempts. Try again later.")
                return
            body = self._read_exactly(length)
            post = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            password = post.get("password", [""])[0]
            if secrets.compare_digest(password, _admin_password()):
                rl_reset(client_ip)
                token, _ = session_create()
                self.redirect("/", extra_headers={"Set-Cookie": self._session_cookie_header(token)})
            else:
                rl_fail(client_ip)
                self.login_page("Invalid password.")
            return

        if not self.is_admin():
            self.send_error_text(401, "Admin login required")
            return

        if request_path == "/upload":
            self._handle_upload(length, content_type, client_ip)
            return

        if request_path in ("/print", "/delete", "/settings", "/archive/delete"):
            if length > SMALL_BODY_LIMIT:
                self.send_error_text(413, "Request body too large")
                return
            if request_path == "/print":
                self._handle_print(length, client_ip)
            elif request_path == "/delete":
                self._handle_delete(length, client_ip)
            elif request_path == "/archive/delete":
                self._handle_archive_delete(length, client_ip)
            else:
                self._handle_settings(length, client_ip)
            return

        self.send_error_text(400, "Bad request")

    def _handle_upload(self, length: int, content_type: str, client_ip: str) -> None:
        if not self._validate_csrf():
            self.send_error_text(403, "Invalid or missing CSRF token")
            return
        if not content_type.startswith("multipart/form-data"):
            self.send_error_text(400, "Expected multipart/form-data")
            return

        msg = Message()
        msg["content-type"] = content_type
        boundary = msg.get_param("boundary")
        if not boundary:
            self.send_error_text(400, "Missing multipart boundary")
            return

        max_body = Config.max_body_bytes()
        if max_body and length > max_body:
            logger.info("UPLOAD_REJECTED ip=%s reason=body too large (%d bytes)", client_ip, length)
            self.send_error_text(413, "Upload too large")
            return

        try:
            body = self._read_exactly(length)
        except DISCONNECT_ERRORS:
            logger.debug("upload aborted by client before body was received")
            return

        tmp_files: list[tuple[str, str, int]] = []
        errors: list[str] = []
        # Names claimed by earlier parts of THIS request. _dedupe_name only
        # sees what is already on disk, and nothing is committed until the
        # whole batch is parsed, so without this two parts named the same
        # (two IMG_0001.JPG from different phone folders, say) would both
        # resolve to the same name and the second would overwrite the first.
        claimed: set[str] = set()

        try:
            for filename, file_data in _iter_multipart_files(body, boundary):
                if filename is None:
                    errors.append("Malformed upload part")
                    continue
                if not filename:
                    continue

                err = Config.check_upload(filename, len(file_data))
                if err:
                    errors.append(f"{filename}: {err}")
                    logger.info("UPLOAD_REJECTED ip=%s filename=%s reason=%s",
                                client_ip, filename, err)
                    continue

                final_name = _dedupe_name(SPOOL_DIR, filename, claimed)
                claimed.add(final_name.lower())

                fd, tmp_path = tempfile.mkstemp(dir=SPOOL_DIR, prefix=".upload_")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(file_data)
                except OSError as exc:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    errors.append(f"{filename}: write error ({exc})")
                    logger.warning("upload write failed for %s: %s", filename, exc)
                    continue
                tmp_files.append((tmp_path, final_name, len(file_data)))

            if errors and not tmp_files:
                self.send_error_text(400, "\n".join(errors))
                return

            committed: list[tuple[str, int]] = []
            for tmp_path, filename, size in tmp_files:
                os.replace(tmp_path, os.path.join(SPOOL_DIR, filename))
                committed.append((filename, size))
            tmp_files = []  # all committed; nothing left to clean up
            for filename, size in committed:
                self.log_event("UPLOAD", client_ip, filename, size)
        finally:
            # Any staged file still here failed to commit (a locked
            # destination, an antivirus hold). Without this they linger as
            # .upload_* files in the spool, where the listing shows them as
            # printable.
            for tmp_path, _, _ in tmp_files:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if errors:
            self.send_json({"ok": True, "warnings": errors}, status=207)
        else:
            self.send_json({"ok": True})

    def _handle_print(self, length: int, client_ip: str) -> None:
        if not self._validate_csrf():
            self.send_error_text(403, "Invalid or missing CSRF token")
            return
        body = self._read_exactly(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_error_text(400, "Invalid JSON body")
            return

        names = payload.get("files") or []
        if not isinstance(names, list) or not names:
            self.send_error_text(400, "No files selected")
            return

        resolved = []
        for name in names:
            path = self.translate_name(str(name))
            if path is None or not os.path.isfile(path):
                self.send_error_text(400, f"Invalid or missing file: {name!r}")
                return
            resolved.append(str(name))

        from py_printer_server.printing import PrintOptions

        printer = str(payload.get("printer") or Config.default_printer)
        if not printer:
            self.send_error_text(400, "No printer selected")
            return

        options = PrintOptions(
            printer=printer,
            color=bool(payload.get("color", Config.default_color)),
            paper=str(payload.get("paper") or Config.default_paper),
            copies=max(1, int(payload.get("copies", Config.default_copies) or 1)),
            duplex=bool(payload.get("duplex", Config.default_duplex)),
        )

        if job_queue is None:
            self.send_error_text(503, "Print queue is not running")
            return

        job = job_queue.submit(Path(SPOOL_DIR), resolved, options)
        logger.info("PRINT_SUBMIT ip=%s job=%s files=%s printer=%r",
                    client_ip, job.id, ",".join(resolved), printer)
        self.send_json({"ok": True, "job_id": job.id})

    def _handle_delete(self, length: int, client_ip: str) -> None:
        if not self._validate_csrf():
            self.send_error_text(403, "Invalid or missing CSRF token")
            return
        body = self._read_exactly(length)
        post = parse_qs(body.decode(), keep_blank_values=True)
        name = post.get("name", [""])[0]
        path = self.translate_name(name)
        if path is None or not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        os.remove(path)
        self.log_event("DELETE", client_ip, name, size)
        self.send_json({"ok": True})

    def _handle_archive_delete(self, length: int, client_ip: str) -> None:
        """Permanently remove one or more archived-job folders from JOBS_DIR.

        Unlike /delete (one spool file, still unprinted), this destroys
        already-printed job records with no retry path -- every name is
        validated before anything is removed, so a request naming one bad
        folder deletes nothing rather than deleting everything before it.
        """
        if not self._validate_csrf():
            self.send_error_text(403, "Invalid or missing CSRF token")
            return
        body = self._read_exactly(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_error_text(400, "Invalid JSON body")
            return

        names = payload.get("folders") or []
        if not isinstance(names, list) or not names:
            self.send_error_text(400, "No archive folders selected")
            return

        resolved: list[tuple[str, str]] = []
        for name in names:
            path = self.translate_job_dir(str(name))
            if path is None or not os.path.isdir(path):
                self.send_error_text(400, f"Invalid or missing archive folder: {name!r}")
                return
            resolved.append((str(name), path))

        removed: list[str] = []
        errors: list[str] = []
        for name, path in resolved:
            try:
                shutil.rmtree(path)
                removed.append(name)
                self.log_event("ARCHIVE_DELETE", client_ip, name, 0)
            except OSError as exc:
                errors.append(f"{name}: {exc}")
                logger.warning("could not remove archive folder %s: %s", path, exc)

        if errors:
            self.send_json(
                {"ok": False, "removed": removed, "errors": errors},
                status=207 if removed else 500,
            )
        else:
            self.send_json({"ok": True, "removed": removed})

    def _handle_settings(self, length: int, client_ip: str) -> None:
        """Persist default print options to config.json."""
        if not self._validate_csrf():
            self.send_error_text(403, "Invalid or missing CSRF token")
            return
        body = self._read_exactly(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_error_text(400, "Invalid JSON body")
            return
        if not isinstance(payload, dict):
            self.send_error_text(400, "Expected a JSON object")
            return

        updates: dict = {}
        if "default_printer" in payload:
            updates["default_printer"] = str(payload["default_printer"])
        if "default_color" in payload:
            updates["default_color"] = bool(payload["default_color"])
        if "default_paper" in payload:
            updates["default_paper"] = str(payload["default_paper"])
        if "default_duplex" in payload:
            updates["default_duplex"] = bool(payload["default_duplex"])
        if "default_copies" in payload:
            try:
                copies = int(payload["default_copies"])
            except (TypeError, ValueError):
                self.send_error_text(400, "default_copies must be a number")
                return
            if not 1 <= copies <= 99:
                self.send_error_text(400, "default_copies must be between 1 and 99")
                return
            updates["default_copies"] = copies
        if "show_virtual_printers" in payload:
            updates["show_virtual_printers"] = bool(payload["show_virtual_printers"])

        if not updates:
            self.send_error_text(400, "No recognised settings in request")
            return

        Config.update(**updates)
        logger.info("SETTINGS ip=%s %s", client_ip,
                    " ".join(f"{k}={v}" for k, v in sorted(updates.items())))
        self.send_json({"ok": True, "settings": _current_settings()})


def _current_settings() -> dict:
    return {
        "default_printer": Config.default_printer,
        "default_color": Config.default_color,
        "default_paper": Config.default_paper,
        "default_duplex": Config.default_duplex,
        "default_copies": Config.default_copies,
        "show_virtual_printers": Config.show_virtual_printers,
    }


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _iter_multipart_files(body: bytes, boundary: str):
    """Yield ``(filename, data)`` for each file part in a multipart body.

    Yields ``(None, b"")`` for a part that cannot be parsed, so the caller can
    report it rather than silently dropping it.

    The delimiter is ``CRLF + "--" + boundary``, and that leading CRLF belongs
    to the delimiter, not to the file. Splitting on the full delimiter
    therefore removes it automatically, which is the only way to get this
    right: a file may legitimately end in CRLF itself, so stripping a trailing
    CRLF after the fact cannot tell the two apart and eats a real byte pair.

    Splitting on the *bare* boundary is also wrong -- it splits anywhere those
    bytes occur inside a file's own content, truncating it.
    """
    delimiter = b"\r\n--" + boundary.encode()
    # The opening delimiter has no leading CRLF; normalise by prepending one
    # so every delimiter in the body has the same shape.
    for part in (b"\r\n" + body).split(delimiter):
        if not part or part.startswith(b"--"):
            continue  # preamble, or the closing "--" delimiter
        if b'filename="' not in part:
            continue
        try:
            header_raw, file_data = part.split(b"\r\n\r\n", 1)
            filename = os.path.basename(
                header_raw.split(b'filename="')[1].split(b'"')[0]
                .decode("utf-8", errors="replace")
            )
        except (ValueError, IndexError):
            yield None, b""
            continue
        yield filename, file_data


def _dedupe_name(directory: str, filename: str, claimed: set[str] | None = None) -> str:
    """Return `filename`, or a ` (2)`-suffixed variant if it is already taken.

    `claimed` holds names reserved earlier in the same upload batch but not
    yet written to disk; without it, two parts with the same name in one
    request both pass the on-disk check and the second overwrites the first.
    Comparison is case-insensitive because the filesystem is.
    """
    taken = claimed or set()
    stem, ext = os.path.splitext(filename)
    candidate = filename
    counter = 2
    while os.path.exists(os.path.join(directory, candidate)) or candidate.lower() in taken:
        candidate = f"{stem} ({counter}){ext}"
        counter += 1
    return candidate


def _job_to_dict(job) -> dict:
    from dataclasses import asdict
    return {
        "id": job.id,
        "status": job.status,
        "created": job.created,
        "finished": job.finished,
        "error": job.error,
        "options": job.options.to_dict(),
        "files": [asdict(f) for f in job.files],
    }


def _admin_password() -> str:
    return os.environ.get(ADMIN_PASSWORD_ENV, "")

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, DISCONNECT_ERRORS):
            logger.debug("connection from %s dropped: %s", client_address[0], exc)
            return
        logger.exception("error handling request from %s", client_address[0])


_MAIN_PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Print Server</title>
<style>
:root {{
    --bg: #0f172a; --surface: #1e293b; --surface2: #334155;
    --text: #e2e8f0; --text-muted: #94a3b8; --border: #334155;
    --accent: #3b82f6; --danger: #f87171; --success: #34d399;
}}
[data-theme="light"] {{
    --bg: #f1f5f9; --surface: #ffffff; --surface2: #f8fafc;
    --text: #1e293b; --text-muted: #64748b; --border: #e2e8f0;
    --accent: #2563eb; --danger: #dc2626; --success: #16a34a;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; padding: 16px; }}
.wrap {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 20px; margin: 0 0 16px; }}
.nav-link {{ margin: -8px 0 16px; font-size: 13px; }}
.nav-link a {{ color: var(--accent); text-decoration: none; }}
.nav-link a:hover {{ text-decoration: underline; }}
.panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px; margin-bottom: 16px; }}
.drop {{ border: 2px dashed var(--border); border-radius: 10px; padding: 28px; text-align: center; color: var(--text-muted); margin-bottom: 12px; }}
.drop.drag {{ border-color: var(--accent); color: var(--accent); }}
.upload-btn {{ display: inline-block; padding: 10px 16px; background: var(--accent); color: white; border-radius: 8px; cursor: pointer; font-size: 14px; }}
.upload-btn input {{ display: none; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid var(--border); }}
th {{ color: var(--text-muted); font-weight: 600; font-size: 12px; text-transform: uppercase; }}
.empty-notice {{ text-align: center; color: var(--text-muted); padding: 24px; }}
.btn-small {{ background: none; border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px; cursor: pointer; color: var(--text); }}
.select-bar {{ display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }}
.select-bar button {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; cursor: pointer; color: var(--text); font-size: 13px; }}
.print-bar {{ position: sticky; bottom: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 14px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
.print-bar select, .print-bar input[type=number] {{ padding: 8px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface2); color: var(--text); }}
.print-bar label {{ display: flex; align-items: center; gap: 4px; font-size: 13px; }}
#print-btn, #print-all-btn {{ background: var(--accent); color: white; border: none; border-radius: 8px; padding: 10px 16px; cursor: pointer; font-size: 14px; }}
#print-btn:disabled, #print-all-btn:disabled {{ opacity: 0.6; cursor: not-allowed; }}
.status-line {{ font-size: 12px; color: var(--text-muted); margin-top: 6px; }}
.jobs-panel {{ margin-top: 16px; }}
.job-row {{ font-size: 13px; padding: 6px 0; border-bottom: 1px solid var(--border); }}
.job-done {{ color: var(--success); }}
.job-error {{ color: var(--danger); }}
.job-warn {{ color: #f59e0b; }}
.job-detail {{ font-size: 12px; color: var(--text-muted); margin-left: 8px; }}
</style>
</head>
<body>
<div class="wrap">
    <h1>🖨️ Print Server</h1>
    <div class="nav-link"><a href="/archive">📁 Archived jobs</a></div>

    <div class="panel">
        <div class="drop" id="drop">Drag &amp; drop files here to add them to the print spool</div>
        <label class="upload-btn">📂 Browse Files<input type="file" id="fileInput" multiple></label>
        <div class="status-line" id="upload-status"></div>
    </div>

    <div class="panel">
        <div class="select-bar">
            <button id="select-all">Select all</button>
            <button id="select-none">Select none</button>
            <button id="select-invert">Invert</button>
        </div>
        <table>
            <thead><tr><th></th><th>Name</th><th>Size</th><th>Modified</th><th></th></tr></thead>
            <tbody id="file-table-body">{empty_notice}{rows}</tbody>
        </table>
    </div>

    <div class="print-bar">
        <select id="printer-select"></select>
        <label><input type="checkbox" id="show-virtual"> Show all printers</label>
        <label><input type="checkbox" id="color-toggle"> Colour</label>
        <select id="paper-select"><option value="A4" selected>A4</option><option value="Letter">Letter</option></select>
        <label>Copies <input type="number" id="copies-input" value="1" min="1" max="99" style="width:60px"></label>
        <label><input type="checkbox" id="duplex-toggle"> Duplex</label>
        <button id="print-btn">Print selected</button>
        <button id="print-all-btn">Print all</button>
    </div>
    <div class="status-line">Colour/copies/duplex apply to plain text files only. PDFs, images and Office
        documents are handed to their own program to print and follow that printer's own default settings
        instead -- change those in Windows' Printing Preferences for this printer if they print in colour
        when you don't want them to.</div>
    <div class="status-line" id="print-status">Last updated: never</div>

    <div class="jobs-panel panel">
        <strong>Recent jobs</strong>
        <div id="jobs-list"></div>
    </div>
</div>

<script>
const CSRF = "{csrf}";

function setTheme() {{
    const t = localStorage.getItem("theme") || "dark";
    document.documentElement.setAttribute("data-theme", t);
}}
setTheme();

async function api(path, opts) {{
    opts = opts || {{}};
    opts.headers = Object.assign({{"X-CSRF-Token": CSRF}}, opts.headers || {{}});
    const resp = await fetch(path, opts);
    return resp;
}}

// --- File selection ---
function fileCheckboxes() {{ return Array.from(document.querySelectorAll(".file-check")); }}
document.getElementById("select-all").onclick = () => fileCheckboxes().forEach(c => c.checked = true);
document.getElementById("select-none").onclick = () => fileCheckboxes().forEach(c => c.checked = false);
document.getElementById("select-invert").onclick = () => fileCheckboxes().forEach(c => c.checked = !c.checked);

document.querySelectorAll(".delete-btn").forEach(btn => {{
    btn.onclick = async () => {{
        const name = btn.dataset.name;
        if (!confirm("Remove " + name + " from the spool without printing it?")) return;
        const body = new URLSearchParams({{name: name}});
        const resp = await api("/delete", {{method: "POST", body: body}});
        if (resp.ok) location.reload();
    }};
}});

// --- Upload ---
const drop = document.getElementById("drop");
const fileInput = document.getElementById("fileInput");
const uploadStatus = document.getElementById("upload-status");

function uploadFiles(files) {{
    if (!files.length) return;
    uploadStatus.textContent = "Uploading " + files.length + " file(s)...";
    const form = new FormData();
    for (const f of files) form.append("file", f, f.name);
    api("/upload", {{method: "POST", body: form}}).then(resp => {{
        if (resp.ok) {{
            uploadStatus.textContent = "Uploaded. Refreshing...";
            setTimeout(() => location.reload(), 400);
        }} else {{
            resp.text().then(t => uploadStatus.textContent = "Upload failed: " + t);
        }}
    }}).catch(err => uploadStatus.textContent = "Upload failed: " + err);
}}

fileInput.addEventListener("change", e => uploadFiles(e.target.files));
drop.addEventListener("dragover", e => {{ e.preventDefault(); drop.classList.add("drag"); }});
drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
drop.addEventListener("drop", e => {{
    e.preventDefault();
    drop.classList.remove("drag");
    uploadFiles(e.dataTransfer.files);
}});

// --- Printers ---
const printerSelect = document.getElementById("printer-select");
const showVirtual = document.getElementById("show-virtual");
const colorToggle = document.getElementById("color-toggle");

async function loadPrinters() {{
    const resp = await api("/printers?all=" + (showVirtual.checked ? "1" : "0"));
    if (!resp.ok) return;
    const printers = await resp.json();
    const prevValue = printerSelect.value;
    printerSelect.innerHTML = "";
    for (const p of printers) {{
        const opt = document.createElement("option");
        opt.value = p.name;
        opt.textContent = p.name + (p.is_virtual ? " (virtual)" : "") + (p.is_default ? " — default" : "");
        opt.dataset.color = p.supports_color ? "1" : "0";
        printerSelect.appendChild(opt);
    }}
    if (prevValue && Array.from(printerSelect.options).some(o => o.value === prevValue)) {{
        printerSelect.value = prevValue;
    }} else {{
        const def = printers.find(p => p.is_default);
        if (def) printerSelect.value = def.name;
    }}
    updateColorAvailability();
}}
function updateColorAvailability() {{
    const opt = printerSelect.selectedOptions[0];
    if (opt && opt.dataset.color === "0") {{
        colorToggle.checked = false;
        colorToggle.disabled = true;
    }} else {{
        colorToggle.disabled = false;
    }}
}}
printerSelect.addEventListener("change", updateColorAvailability);
showVirtual.addEventListener("change", loadPrinters);
loadPrinters();

// --- Printing ---
const printBtn = document.getElementById("print-btn");
const printAllBtn = document.getElementById("print-all-btn");
const printStatus = document.getElementById("print-status");

function selectedFiles() {{
    return fileCheckboxes().filter(c => c.checked).map(c => c.value);
}}
function allFiles() {{
    return fileCheckboxes().map(c => c.value);
}}

async function submitPrint(files, btn) {{
    if (!files.length) {{
        printStatus.textContent = "Nothing selected. Last updated: " + new Date().toLocaleTimeString();
        return;
    }}
    // In-flight state painted before the request goes out, in its own
    // frame, so it is visible even when the job is fast -- and cleared by
    // the job-status poll below, not here, so a killed server cannot leave
    // the button stuck disabled forever from the caller's side.
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Printing…";
    printStatus.textContent = "Submitting " + files.length + " file(s)...";

    const payload = {{
        files: files,
        printer: printerSelect.value,
        color: colorToggle.checked,
        paper: document.getElementById("paper-select").value,
        copies: parseInt(document.getElementById("copies-input").value, 10) || 1,
        duplex: document.getElementById("duplex-toggle").checked,
    }};
    try {{
        const resp = await api("/print", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify(payload),
        }});
        if (resp.ok) {{
            printStatus.textContent = "Job submitted. Last updated: " + new Date().toLocaleTimeString();
        }} else {{
            const text = await resp.text();
            printStatus.textContent = "Print failed: " + text;
        }}
    }} catch (err) {{
        printStatus.textContent = "Print failed: " + err;
    }} finally {{
        btn.disabled = false;
        btn.textContent = originalText;
        refreshJobs();
    }}
}}

printBtn.onclick = () => submitPrint(selectedFiles(), printBtn);
printAllBtn.onclick = () => submitPrint(allFiles(), printAllBtn);

// --- Jobs panel ---
const jobsList = document.getElementById("jobs-list");
function esc(t) {{
    return String(t).replace(/[&<>"']/g, c => ({{
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }})[c]);
}}
async function refreshJobs() {{
    const resp = await api("/jobs");
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.jobs.length) {{
        jobsList.innerHTML = '<div class="status-line">No jobs yet.</div>';
    }} else {{
        jobsList.innerHTML = data.jobs.map(j => {{
            let cls = "";
            if (j.status === "done") cls = "job-done";
            else if (j.status === "error") cls = "job-error";
            else if (j.status === "partial" || j.status === "unsupported") cls = "job-warn";
            // Show why, not just that: a failed or skipped file is useless to
            // the user without the reason, and it stays in the spool to retry.
            const parts = j.files.map(f => {{
                if (f.status === "done") return esc(f.name);
                return esc(f.name) + ' <span class="job-detail">[' + esc(f.status) +
                       (f.detail ? ": " + esc(f.detail) : "") + ']</span>';
            }}).join(", ");
            const jobErr = j.error ? ' <span class="job-detail">' + esc(j.error) + '</span>' : "";
            return '<div class="job-row ' + cls + '">' + esc(j.status.toUpperCase()) +
                   ' — ' + parts + jobErr + '</div>';
        }}).join("");
    }}
    // A last-updated timestamp that changes on every poll, so a no-op
    // refresh (nothing printed since the last check) still looks visibly
    // different from a dead poll loop.
    printStatus.textContent = "Last updated: " + new Date(data.server_time * 1000).toLocaleTimeString();
}}
refreshJobs();
setInterval(refreshJobs, 4000);
</script>
</body>
</html>"""


_ARCHIVE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Archived Jobs</title>
<style>
:root {{
    --bg: #0f172a; --surface: #1e293b; --surface2: #334155;
    --text: #e2e8f0; --text-muted: #94a3b8; --border: #334155;
    --accent: #3b82f6; --danger: #f87171; --success: #34d399;
}}
[data-theme="light"] {{
    --bg: #f1f5f9; --surface: #ffffff; --surface2: #f8fafc;
    --text: #1e293b; --text-muted: #64748b; --border: #e2e8f0;
    --accent: #2563eb; --danger: #dc2626; --success: #16a34a;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; padding: 16px; }}
.wrap {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 20px; margin: 0 0 16px; }}
.nav-link {{ margin: -8px 0 16px; font-size: 13px; }}
.nav-link a {{ color: var(--accent); text-decoration: none; }}
.nav-link a:hover {{ text-decoration: underline; }}
.panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px; margin-bottom: 16px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid var(--border); }}
th {{ color: var(--text-muted); font-weight: 600; font-size: 12px; text-transform: uppercase; }}
.empty-notice {{ text-align: center; color: var(--text-muted); padding: 24px; }}
.btn-small {{ background: none; border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px; cursor: pointer; color: var(--text); }}
.select-bar {{ display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }}
.select-bar button {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; cursor: pointer; color: var(--text); font-size: 13px; }}
.action-bar {{ display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; align-items: center; }}
.action-bar button {{ border: none; border-radius: 8px; padding: 10px 16px; cursor: pointer; font-size: 14px; color: white; }}
#delete-selected-btn {{ background: var(--danger); }}
#delete-all-btn {{ background: var(--danger); opacity: 0.85; }}
.status-line {{ font-size: 12px; color: var(--text-muted); margin-top: 6px; }}
</style>
</head>
<body>
<div class="wrap">
    <h1>📁 Archived Jobs</h1>
    <div class="nav-link"><a href="/">← Back to Print Server</a></div>

    <div class="panel">
        <div class="select-bar">
            <button id="select-all">Select all</button>
            <button id="select-none">Select none</button>
            <button id="select-invert">Invert</button>
        </div>
        <table>
            <thead><tr><th></th><th>Folder</th><th>Files</th><th>Size</th><th>Printed</th><th></th></tr></thead>
            <tbody id="archive-table-body">{empty_notice}{rows}</tbody>
        </table>
        <div class="action-bar">
            <button id="delete-selected-btn">🗑 Delete selected</button>
            <button id="delete-all-btn">🗑 Delete all</button>
        </div>
        <div class="status-line" id="archive-status"></div>
    </div>
</div>

<script>
const CSRF = "{csrf}";

function setTheme() {{
    const t = localStorage.getItem("theme") || "dark";
    document.documentElement.setAttribute("data-theme", t);
}}
setTheme();

async function api(path, opts) {{
    opts = opts || {{}};
    opts.headers = Object.assign({{"X-CSRF-Token": CSRF}}, opts.headers || {{}});
    return fetch(path, opts);
}}

function archiveCheckboxes() {{ return Array.from(document.querySelectorAll(".archive-check")); }}
document.getElementById("select-all").onclick = () => archiveCheckboxes().forEach(c => c.checked = true);
document.getElementById("select-none").onclick = () => archiveCheckboxes().forEach(c => c.checked = false);
document.getElementById("select-invert").onclick = () => archiveCheckboxes().forEach(c => c.checked = !c.checked);

function selectedFolders() {{ return archiveCheckboxes().filter(c => c.checked).map(c => c.value); }}
function allFolders() {{ return archiveCheckboxes().map(c => c.value); }}

const status = document.getElementById("archive-status");

async function deleteFolders(folders) {{
    if (!folders.length) {{
        status.textContent = "Nothing selected.";
        return;
    }}
    const label = folders.length === 1 ? folders[0] : folders.length + " archived job folder(s)";
    if (!confirm("Permanently delete " + label + "? This cannot be undone.")) return;
    status.textContent = "Deleting...";
    try {{
        const resp = await api("/archive/delete", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{folders: folders}}),
        }});
        if (resp.ok || resp.status === 207) {{
            location.reload();
        }} else {{
            const text = await resp.text();
            status.textContent = "Delete failed: " + text;
        }}
    }} catch (err) {{
        status.textContent = "Delete failed: " + err;
    }}
}}

document.querySelectorAll(".archive-delete-btn").forEach(btn => {{
    btn.onclick = () => deleteFolders([btn.dataset.name]);
}});
document.getElementById("delete-selected-btn").onclick = () => deleteFolders(selectedFolders());
document.getElementById("delete-all-btn").onclick = () => deleteFolders(allFolders());
</script>
</body>
</html>"""


def _print_qr(url: str) -> None:
    """Print a scannable QR code for `url`.

    Prefers the compact half-block rendering (square modules, half the
    lines); falls back to the plain-ASCII one if this console cannot encode
    those glyphs, which prints larger and taller but needs nothing beyond
    7-bit ASCII.
    """
    from py_printer_server.qrcode_ascii import generate_matrix, render_ascii, render_compact

    try:
        matrix = generate_matrix(url)
    except ValueError as exc:
        logger.warning("could not build QR code: %s", exc)
        return

    try:
        print(render_compact(matrix))
    except UnicodeEncodeError:
        print(render_ascii(matrix))


def main() -> int:
    global PORT, SPOOL_DIR, JOBS_DIR, job_queue

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = _parse_args()

    if args.generate_password:
        print(secrets.token_urlsafe(18))
        return 0

    PORT = args.port
    SPOOL_DIR = os.path.abspath(args.spool)
    JOBS_DIR = os.path.abspath(args.jobs)

    if args.list_printers:
        from py_printer_server.printers import list_printers
        for p in list_printers(show_virtual=True):
            kind = "virtual" if p.is_virtual else "hardware"
            default = " (default)" if p.is_default else ""
            print(f"{p.name}  [{kind}]{default}  port={p.port}  color={p.supports_color}")
        return 0

    if not _admin_password():
        print(
            f"ERROR: {ADMIN_PASSWORD_ENV} is not set. This server binds all "
            "network interfaces and spends real paper and ink, so it refuses "
            "to start with no password.\n"
            f"Set it, e.g.: $env:{ADMIN_PASSWORD_ENV} = 'your-strong-password'\n"
            "Or generate a suggestion with --generate-password."
        )
        return 1

    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        os.makedirs(JOBS_DIR, exist_ok=True)
    except OSError as exc:
        print(f"Cannot create spool/jobs folders: {exc}")
        return 1

    _attach_file_logging(SPOOL_DIR)
    Config.set_config_file(os.path.join(SPOOL_DIR, "config.json"))
    Config.load()

    from py_printer_server.printing import JobQueue
    job_queue = JobQueue(Path(JOBS_DIR), dry_run=args.dry_run)

    try:
        httpd = Server(("0.0.0.0", PORT), Handler)
    except OSError as exc:
        print(f"Cannot bind port {PORT}: {exc}")
        return 1

    with httpd:
        url = lan_url(PORT)
        print(f"Print Server: http://localhost:{PORT}")
        print(f"From another device on this network: {url}")
        if not args.no_qr:
            _print_qr(url)
        if args.dry_run:
            print("DRY RUN: jobs will be logged, not sent to a printer.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")
        finally:
            httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
