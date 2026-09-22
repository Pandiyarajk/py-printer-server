"""Tests for server.py's pure helper functions.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026
"""

from __future__ import annotations

from pathlib import Path

from py_printer_server.server import _dedupe_name, human


def test_human_formats_bytes() -> None:
    assert human(500) == "500.0 B"
    assert human(1536) == "1.5 KB"
    assert human(1024 * 1024 * 2) == "2.0 MB"


def test_dedupe_name_returns_original_when_free(tmp_path: Path) -> None:
    assert _dedupe_name(str(tmp_path), "report.pdf") == "report.pdf"


def test_dedupe_name_suffixes_on_collision(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
    assert _dedupe_name(str(tmp_path), "report.pdf") == "report (2).pdf"


def test_dedupe_name_increments_past_existing_suffixes(tmp_path: Path) -> None:
    (tmp_path / "report.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "report (2).pdf").write_text("x", encoding="utf-8")
    assert _dedupe_name(str(tmp_path), "report.pdf") == "report (3).pdf"


class TestTranslateName:
    """translate_name is the guard between a network-supplied string and the
    filesystem, so each rejection case gets a named test."""

    @staticmethod
    def _translate(name: str):
        from py_printer_server import server as srv
        return srv.Handler.translate_name(object.__new__(srv.Handler), name)

    def test_plain_name_resolves(self, tmp_path, monkeypatch):
        from py_printer_server import server as srv
        monkeypatch.setattr(srv, "SPOOL_DIR", str(tmp_path))
        assert self._translate("report.pdf") is not None

    def test_rejects_traversal(self, tmp_path, monkeypatch):
        from py_printer_server import server as srv
        monkeypatch.setattr(srv, "SPOOL_DIR", str(tmp_path))
        for bad in ("../secret", "..\\secret", "sub/file.txt", "sub\\file.txt", ".."):
            assert self._translate(bad) is None, bad

    def test_rejects_alternate_data_stream(self, tmp_path, monkeypatch):
        """'file.txt:hidden' contains no separator and survives basename(),
        but addresses an NTFS stream rather than the file."""
        from py_printer_server import server as srv
        monkeypatch.setattr(srv, "SPOOL_DIR", str(tmp_path))
        assert self._translate("file.txt:hidden") is None

    def test_rejects_reserved_device_names(self, tmp_path, monkeypatch):
        from py_printer_server import server as srv
        monkeypatch.setattr(srv, "SPOOL_DIR", str(tmp_path))
        for bad in ("CON", "con", "NUL", "LPT1", "COM1", "PRN.txt"):
            assert self._translate(bad) is None, bad

    def test_rejects_empty_and_null(self, tmp_path, monkeypatch):
        from py_printer_server import server as srv
        monkeypatch.setattr(srv, "SPOOL_DIR", str(tmp_path))
        assert self._translate("") is None
        assert self._translate("a\x00b") is None


def test_main_page_template_renders():
    """The page template goes through str.format, so every literal brace in
    its embedded CSS and JavaScript must be doubled. A single stray brace
    raises KeyError/IndexError at request time, not at import -- so the whole
    UI 500s while every unit test still passes. This renders it to catch that.
    """
    from py_printer_server.server import _MAIN_PAGE_TEMPLATE
    html = _MAIN_PAGE_TEMPLATE.format(csrf="tok", rows="", empty_notice="")
    assert "<title>Print Server</title>" in html
    assert 'const CSRF = "tok"' in html
    # Doubled braces must have collapsed to single ones in the output.
    assert "{{" not in html and "}}" not in html


def test_login_page_template_renders():
    from py_printer_server import server as srv
    handler = object.__new__(srv.Handler)
    captured = {}
    handler.send_html = lambda html, **kw: captured.update(html=html)
    srv.Handler.login_page(handler, "Invalid password.")
    assert "Invalid password." in captured["html"]
    assert "{{" not in captured["html"]


def test_archive_page_template_renders():
    """Same str.format brace-escaping trap as the main page template."""
    from py_printer_server.server import _ARCHIVE_PAGE_TEMPLATE
    html = _ARCHIVE_PAGE_TEMPLATE.format(csrf="tok", rows="", empty_notice="")
    assert "<title>Archived Jobs</title>" in html
    assert 'const CSRF = "tok"' in html
    assert "{{" not in html and "}}" not in html


class TestTranslateJobDir:
    def _translate(self, name):
        from py_printer_server import server as srv
        handler = object.__new__(srv.Handler)
        return srv.Handler.translate_job_dir(handler, name)

    def test_accepts_plain_folder_name(self, tmp_path, monkeypatch):
        from py_printer_server import server as srv
        monkeypatch.setattr(srv, "JOBS_DIR", str(tmp_path))
        result = self._translate("print-job-20260101-000000-abc123")
        assert result == str(tmp_path / "print-job-20260101-000000-abc123")

    def test_rejects_traversal(self, tmp_path, monkeypatch):
        from py_printer_server import server as srv
        monkeypatch.setattr(srv, "JOBS_DIR", str(tmp_path))
        assert self._translate("../outside") is None
        assert self._translate("..\\outside") is None
        assert self._translate("sub/dir") is None

    def test_rejects_empty_and_dotted(self, tmp_path, monkeypatch):
        from py_printer_server import server as srv
        monkeypatch.setattr(srv, "JOBS_DIR", str(tmp_path))
        assert self._translate("") is None
        assert self._translate(".") is None
        assert self._translate("..") is None


class TestSessionPersistence:
    """Sessions survive a restart, and a password change ends them.

    A 365-day cookie is only defensible because it is revocable: without the
    password binding below, changing ADMIN_PASSWORD would leave every phone that
    ever logged in still holding a working token for a year.
    """

    def _fresh(self, monkeypatch, tmp_path, password="pw-one"):
        from py_printer_server import server as srv
        monkeypatch.setenv(srv.ADMIN_PASSWORD_ENV, password)
        monkeypatch.setattr(srv, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
        srv._sessions.clear()
        return srv.session_create()

    def test_session_round_trips(self, monkeypatch, tmp_path):
        from py_printer_server import server as srv
        token, csrf = self._fresh(monkeypatch, tmp_path)
        got = srv.session_get(token)
        assert got is not None
        assert got["csrf"] == csrf

    def test_ttl_is_a_year(self):
        from py_printer_server import server as srv
        assert srv.SESSION_TTL == 365 * 24 * 3600

    def test_cookie_carries_max_age(self, monkeypatch, tmp_path):
        from py_printer_server import server as srv
        token, _ = self._fresh(monkeypatch, tmp_path)
        header = srv.Handler._session_cookie_header(
            object.__new__(srv.Handler), token
        )
        assert f"Max-Age={srv.SESSION_TTL}" in header
        assert "HttpOnly" in header
        assert "SameSite=Strict" in header

    def test_survives_a_restart(self, monkeypatch, tmp_path):
        from py_printer_server import server as srv
        token, _ = self._fresh(monkeypatch, tmp_path)
        # Simulate the process going away and coming back.
        srv._sessions.clear()
        srv.load_sessions()
        assert srv.session_get(token) is not None

    def test_password_change_invalidates_every_session(self, monkeypatch, tmp_path):
        from py_printer_server import server as srv
        token, _ = self._fresh(monkeypatch, tmp_path, password="pw-one")
        assert srv.session_get(token) is not None
        monkeypatch.setenv(srv.ADMIN_PASSWORD_ENV, "pw-two")
        assert srv.session_get(token) is None

    def test_password_change_invalidates_across_a_restart(self, monkeypatch, tmp_path):
        from py_printer_server import server as srv
        token, _ = self._fresh(monkeypatch, tmp_path, password="pw-one")
        monkeypatch.setenv(srv.ADMIN_PASSWORD_ENV, "pw-two")
        srv._sessions.clear()
        srv.load_sessions()
        assert srv.session_get(token) is None

    def test_logout_deletes_the_session_from_disk(self, monkeypatch, tmp_path):
        from py_printer_server import server as srv
        token, _ = self._fresh(monkeypatch, tmp_path)
        srv.session_delete(token)
        srv._sessions.clear()
        srv.load_sessions()
        assert srv.session_get(token) is None

    def test_expired_session_is_refused(self, monkeypatch, tmp_path):
        import time

        from py_printer_server import server as srv
        token, _ = self._fresh(monkeypatch, tmp_path)
        srv._sessions[token]["expires"] = time.time() - 1
        assert srv.session_get(token) is None

    def test_session_file_is_hidden_from_the_spool_listing(self):
        from py_printer_server import server as srv
        # It holds live tokens. Listing it would also make it downloadable.
        assert "sessions.json" in srv._HIDDEN_SPOOL_NAMES

    def test_corrupt_session_file_does_not_break_startup(self, monkeypatch, tmp_path):
        from py_printer_server import server as srv
        monkeypatch.setenv(srv.ADMIN_PASSWORD_ENV, "pw-one")
        path = tmp_path / "sessions.json"
        path.write_text("{not json at all", encoding="utf-8")
        monkeypatch.setattr(srv, "SESSIONS_FILE", str(path))
        srv._sessions.clear()
        srv.load_sessions()          # must not raise
        assert srv._sessions == {}

    def test_logout_link_is_on_both_pages(self):
        from py_printer_server import server as srv
        for template in (srv._MAIN_PAGE_TEMPLATE, srv._ARCHIVE_PAGE_TEMPLATE):
            assert 'href="/logout"' in template
