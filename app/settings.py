"""Runtime configuration, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


class SettingsError(ValueError):
    """Raised when the environment cannot produce a usable configuration."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be a boolean (true/false), got {raw!r}")


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    db_path: str = "/data/qc.sqlite3"
    imap_host: str = ""
    imap_port: int = 143
    imap_use_ssl: bool = False
    imap_ssl_verify: bool = True
    imap_user: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 15.0
    worker_enabled: bool = True
    max_attachment_bytes: int = 5_000_000
    max_readings_per_report: int = 100_000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        host = os.getenv("IMAP_HOST", "").strip()
        user = os.getenv("IMAP_USER", "").strip()
        password = os.getenv("IMAP_PASSWORD", "")
        # Default: run the worker whenever an IMAP mailbox is configured.
        # An explicit QC_WORKER_ENABLED=false (or 0) disables it.
        worker_enabled = _env_bool("QC_WORKER_ENABLED", bool(host and user))
        if worker_enabled and not host:
            raise SettingsError("IMAP_HOST is required when the background worker is enabled")
        if worker_enabled and not user:
            raise SettingsError("IMAP_USER is required when the background worker is enabled")

        settings = cls(
            db_path=os.getenv("QC_DB_PATH", "/data/qc.sqlite3").strip(),
            imap_host=host,
            imap_port=_env_int("IMAP_PORT", 143, minimum=1),
            imap_use_ssl=_env_bool("IMAP_USE_SSL", False),
            imap_ssl_verify=_env_bool("IMAP_SSL_VERIFY", True),
            imap_user=user,
            imap_password=password,
            imap_mailbox=(os.getenv("IMAP_MAILBOX", "INBOX").strip() or "INBOX"),
            imap_timeout_seconds=_env_float("IMAP_TIMEOUT_SECONDS", 30.0, minimum=1.0),
            poll_interval_seconds=_env_float("IMAP_POLL_INTERVAL_SECONDS", 15.0, minimum=0.5),
            worker_enabled=worker_enabled,
            max_attachment_bytes=_env_int("QC_MAX_ATTACHMENT_BYTES", 5_000_000, minimum=1),
            max_readings_per_report=_env_int(
                "QC_MAX_READINGS_PER_REPORT", 100_000, minimum=1
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip() or "INFO",
        )
        if not settings.db_path:
            raise SettingsError("QC_DB_PATH must not be empty")
        return settings

    def __post_init__(self) -> None:
        level = (self.log_level or "INFO").strip().upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise SettingsError(f"LOG_LEVEL must be a Python log level, got {self.log_level!r}")
