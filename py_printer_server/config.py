"""Runtime configuration for py-printer-server.

Author: Pandiyaraj Karuppasamy
Date: Sep-14-2026
"""

from __future__ import annotations

import json
import os
import threading

DEFAULT_CONFIG = {
    "max_upload_mb": 100,
    "blocked_extensions": [".exe", ".bat", ".cmd", ".ps1", ".sh"],
    "default_printer": "",
    "default_color": True,
    "default_paper": "A4",
    "default_duplex": False,
    "default_copies": 1,
    "show_virtual_printers": False,
}


class Config:
    """
    Runtime settings editable by the admin via the settings page.

    Settings are loaded from config.json (inside the spool directory) at
    startup and saved back on every admin update, so they survive server
    restarts. Ported from file-share's Config class, with upload keys kept
    and print-default keys added.
    """

    _lock = threading.Lock()
    _config_file: str | None = None

    max_upload_mb: int = DEFAULT_CONFIG["max_upload_mb"]
    blocked_extensions: set = set(DEFAULT_CONFIG["blocked_extensions"])
    default_printer: str = DEFAULT_CONFIG["default_printer"]
    default_color: bool = DEFAULT_CONFIG["default_color"]
    default_paper: str = DEFAULT_CONFIG["default_paper"]
    default_duplex: bool = DEFAULT_CONFIG["default_duplex"]
    default_copies: int = DEFAULT_CONFIG["default_copies"]
    show_virtual_printers: bool = DEFAULT_CONFIG["show_virtual_printers"]

    @classmethod
    def set_config_file(cls, path: str) -> None:
        """Point the config at a specific file (called once at startup)."""
        cls._config_file = path

    @classmethod
    def _apply(cls, data: dict) -> None:
        with cls._lock:
            cls.max_upload_mb = int(data.get("max_upload_mb", DEFAULT_CONFIG["max_upload_mb"]))
            cls.blocked_extensions = {
                e.lower() for e in data.get("blocked_extensions", DEFAULT_CONFIG["blocked_extensions"])
            }
            cls.default_printer = str(data.get("default_printer", DEFAULT_CONFIG["default_printer"]))
            cls.default_color = bool(data.get("default_color", DEFAULT_CONFIG["default_color"]))
            cls.default_paper = str(data.get("default_paper", DEFAULT_CONFIG["default_paper"]))
            cls.default_duplex = bool(data.get("default_duplex", DEFAULT_CONFIG["default_duplex"]))
            cls.default_copies = max(1, int(data.get("default_copies", DEFAULT_CONFIG["default_copies"])))
            cls.show_virtual_printers = bool(
                data.get("show_virtual_printers", DEFAULT_CONFIG["show_virtual_printers"])
            )

    @classmethod
    def ensure_config(cls) -> None:
        """Create config.json with defaults if it does not exist."""
        if cls._config_file is None or os.path.isfile(cls._config_file):
            return
        try:
            with open(cls._config_file, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
                f.write("\n")
        except OSError as exc:
            print(f"Warning: could not create {cls._config_file}: {exc}")

    @classmethod
    def load(cls) -> None:
        """Load settings from the config file, creating it with defaults if missing."""
        cls.ensure_config()
        if cls._config_file is None:
            cls._apply(DEFAULT_CONFIG)
            return
        try:
            with open(cls._config_file, encoding="utf-8") as f:
                cls._apply(json.load(f))
        except (OSError, ValueError) as exc:
            print(f"Warning: could not load {cls._config_file}: {exc}")
            cls._apply(DEFAULT_CONFIG)

    @classmethod
    def save(cls) -> None:
        """Persist current settings to the config file."""
        if cls._config_file is None:
            return
        with cls._lock:
            data = {
                "max_upload_mb": cls.max_upload_mb,
                "blocked_extensions": sorted(cls.blocked_extensions),
                "default_printer": cls.default_printer,
                "default_color": cls.default_color,
                "default_paper": cls.default_paper,
                "default_duplex": cls.default_duplex,
                "default_copies": cls.default_copies,
                "show_virtual_printers": cls.show_virtual_printers,
            }
        try:
            with open(cls._config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
        except OSError as exc:
            print(f"Warning: could not save {cls._config_file}: {exc}")

    @classmethod
    def update(cls, **kwargs) -> None:
        """Apply new settings and persist them immediately."""
        with cls._lock:
            for key, value in kwargs.items():
                if hasattr(cls, key):
                    setattr(cls, key, value)
        cls.save()

    # Multipart bodies carry part headers and boundaries on top of the file
    # itself, so the body cap is the per-file cap plus a fixed allowance.
    BODY_OVERHEAD = 1024 * 1024

    @classmethod
    def max_body_bytes(cls) -> int:
        """Largest request body to accept, or 0 when uploads are uncapped."""
        with cls._lock:
            if not cls.max_upload_mb:
                return 0
            return cls.max_upload_mb * 1024 * 1024 + cls.BODY_OVERHEAD

    @classmethod
    def check_upload(cls, filename: str, size: int) -> str | None:
        """
        Validate a pending upload against current restrictions.
        Returns an error message string if rejected, or None if allowed.
        """
        with cls._lock:
            if cls.max_upload_mb and size > cls.max_upload_mb * 1024 * 1024:
                return f"File too large (max {cls.max_upload_mb} MB)"
            ext = os.path.splitext(filename)[1].lower()
            if ext in cls.blocked_extensions:
                return f"Extension '{ext}' is blocked"
        return None
