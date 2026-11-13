"""SQLite persistence for the water-level intake API.

All writes that belong to one incoming mail are performed in a single
``BEGIN IMMEDIATE`` transaction: the original attachment, the parsed readings,
the daily-report registry and the "mark this UID as read" retry record are
committed together.  Only after that commit does the IMAP worker ask the
server to set ``\\Seen``.

``emails`` is a lightweight processing ledger (metadata only).  Readings and
attachment bytes are written *only* for reports that passed validation and
were accepted (or were byte-identical duplicates of an accepted report).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .csv_parser import ParsedCsv, sha256_hex

SCHEMA_VERSION = 1

OUTCOME_RECEIVED = "received"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_REJECTED = "rejected"

MARK_PENDING = "pending"
MARK_READ = "read"
MARK_FAILED = "failed"
MARK_STALE = "stale"
MARK_REJECTED = "rejected"
MARK_MISSING = "missing"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class MailIdentity:
    mailbox: str
    uid_validity: int
    uid: int


@dataclass(frozen=True)
class IncomingMail:
    """Everything read from one IMAP message before persistence."""

    mailbox: str
    uid_validity: int
    uid: int
    message_id: str | None
    subject: str | None
    from_addr: str | None
    filename: str | None
    content_type: str | None
    attachment: bytes
    parsed: ParsedCsv

    @property
    def attachment_sha256(self) -> str:
        return sha256_hex(self.attachment)


@dataclass(frozen=True)
class IngestResult:
    email_id: int
    outcome: str
    reason: str | None = None
    reading_count: int = 0


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    # ------------------------------------------------------------------ setup
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """Read-only helpers: a short-lived connection, closed deterministically."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _initialise(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    sha256       TEXT NOT NULL UNIQUE,
                    size_bytes   INTEGER NOT NULL,
                    content      BLOB NOT NULL,
                    filename     TEXT,
                    content_type TEXT,
                    report_no    TEXT NOT NULL,
                    station_no   TEXT NOT NULL,
                    utc_day      TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_reports (
                    report_no      TEXT PRIMARY KEY,
                    sha256         TEXT NOT NULL,
                    station_no     TEXT NOT NULL,
                    utc_day        TEXT NOT NULL,
                    attachment_id  INTEGER NOT NULL REFERENCES attachments(id),
                    reading_count  INTEGER NOT NULL,
                    received_at    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS emails (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    mailbox          TEXT NOT NULL,
                    uid_validity     INTEGER NOT NULL,
                    uid              INTEGER NOT NULL,
                    message_id       TEXT,
                    subject          TEXT,
                    from_addr        TEXT,
                    report_no        TEXT,
                    station_no       TEXT,
                    utc_day          TEXT,
                    attachment_sha256 TEXT,
                    outcome          TEXT NOT NULL,
                    mark_state       TEXT NOT NULL,
                    mark_attempts    INTEGER NOT NULL DEFAULT 0,
                    mark_last_error  TEXT,
                    error            TEXT,
                    received_at      TEXT NOT NULL,
                    marked_at        TEXT,
                    UNIQUE (mailbox, uid_validity, uid)
                );

                CREATE TABLE IF NOT EXISTS readings (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_no     TEXT NOT NULL,
                    station_no    TEXT NOT NULL,
                    utc_seconds   INTEGER NOT NULL,
                    utc_day       TEXT NOT NULL,
                    level_mm      INTEGER NOT NULL,
                    attachment_id INTEGER NOT NULL REFERENCES attachments(id),
                    email_id      INTEGER NOT NULL REFERENCES emails(id),
                    created_at    TEXT NOT NULL,
                    UNIQUE (report_no, station_no, utc_seconds)
                );

                CREATE INDEX IF NOT EXISTS idx_readings_day
                    ON readings (utc_day, station_no, utc_seconds);
                CREATE INDEX IF NOT EXISTS idx_readings_email ON readings (email_id);
                CREATE INDEX IF NOT EXISTS idx_emails_report ON emails (report_no);
                CREATE INDEX IF NOT EXISTS idx_emails_mark ON emails (mark_state);

                CREATE TABLE IF NOT EXISTS pending_marks (
                    email_id      INTEGER PRIMARY KEY REFERENCES emails(id) ON DELETE CASCADE,
                    mailbox       TEXT NOT NULL,
                    uid_validity  INTEGER NOT NULL,
                    uid           INTEGER NOT NULL,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_mailbox
                    ON pending_marks (mailbox, uid_validity);

                CREATE TABLE IF NOT EXISTS mailbox_state (
                    mailbox                   TEXT PRIMARY KEY,
                    uid_validity              INTEGER NOT NULL,
                    first_seen_at             TEXT NOT NULL,
                    last_connected_at         TEXT NOT NULL,
                    last_uidvalidity_change_at TEXT
                );

                CREATE TABLE IF NOT EXISTS scan_errors (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    mailbox     TEXT,
                    uid_validity INTEGER,
                    uid         INTEGER,
                    error       TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
            row = conn.execute("PRAGMA user_version").fetchone()
            if row[0] < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            conn.close()

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # -------------------------------------------------------------- ingest
    def email_recorded(self, identity: MailIdentity) -> bool:
        with self._session() as conn:
            row = conn.execute(
                "SELECT 1 FROM emails WHERE mailbox = ? AND uid_validity = ? AND uid = ?",
                (identity.mailbox, identity.uid_validity, identity.uid),
            ).fetchone()
            return row is not None

    def ingest(self, mail: IncomingMail) -> IngestResult:
        """Atomically persist an accepted report, or classify it as duplicate.

        For a report number already stored with different bytes a rejected
        ledger entry is written instead; no readings and no attachment are
        stored, and the caller must not mark the mail as read.
        """
        sha = mail.attachment_sha256
        parsed = mail.parsed
        now = utc_now()

        with self._immediate() as conn:
            existing_report = conn.execute(
                "SELECT * FROM daily_reports WHERE report_no = ?",
                (parsed.report_no,),
            ).fetchone()

            if existing_report is not None and existing_report["sha256"] != sha:
                reason = (
                    f"report {parsed.report_no!r} already stored from a different "
                    f"attachment (existing sha256 {existing_report['sha256']}, "
                    f"incoming sha256 {sha})"
                )
                email_id = self._insert_email_ledger(
                    conn,
                    mail,
                    sha,
                    outcome=OUTCOME_REJECTED,
                    mark_state=MARK_REJECTED,
                    error=reason,
                    now=now,
                )
                return IngestResult(email_id=email_id, outcome=OUTCOME_REJECTED, reason=reason)

            duplicate = existing_report is not None
            if duplicate:
                attachment_id = int(existing_report["attachment_id"])
                reading_count = int(existing_report["reading_count"])
                outcome = OUTCOME_DUPLICATE
            else:
                attachment_id = self._store_attachment(conn, mail, sha, now)
                outcome = OUTCOME_RECEIVED
                reading_count = len(parsed.readings)

            email_id = self._insert_email_ledger(
                conn,
                mail,
                sha,
                outcome=outcome,
                mark_state=MARK_PENDING,
                error=None,
                now=now,
            )

            if not duplicate:
                conn.executemany(
                    """
                    INSERT INTO readings
                        (report_no, station_no, utc_seconds, utc_day, level_mm,
                         attachment_id, email_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            reading.report_no,
                            reading.station_no,
                            reading.utc_seconds,
                            parsed.utc_day,
                            reading.level_mm,
                            attachment_id,
                            email_id,
                            now,
                        )
                        for reading in parsed.readings
                    ],
                )
                conn.execute(
                    """
                    INSERT INTO daily_reports
                        (report_no, sha256, station_no, utc_day, attachment_id,
                         reading_count, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parsed.report_no,
                        sha,
                        parsed.station_no,
                        parsed.utc_day,
                        attachment_id,
                        reading_count,
                        now,
                    ),
                )

            conn.execute(
                """
                INSERT INTO pending_marks
                    (email_id, mailbox, uid_validity, uid, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (email_id, mail.mailbox, mail.uid_validity, mail.uid, now, now),
            )
            return IngestResult(
                email_id=email_id,
                outcome=outcome,
                reading_count=reading_count,
            )

    def record_rejected(
        self,
        *,
        mailbox: str,
        uid_validity: int,
        uid: int,
        message_id: str | None,
        subject: str | None,
        from_addr: str | None,
        report_no: str | None,
        station_no: str | None,
        utc_day: str | None,
        attachment_sha256: str | None,
        reason: str,
    ) -> int:
        """Ledger a rejected mail (metadata only; never readings/attachment)."""
        now = utc_now()
        with self._immediate() as conn:
            cur = conn.execute(
                """
                INSERT INTO emails
                    (mailbox, uid_validity, uid, message_id, subject, from_addr,
                     report_no, station_no, utc_day, attachment_sha256, outcome,
                     mark_state, error, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mailbox,
                    uid_validity,
                    uid,
                    message_id,
                    subject,
                    from_addr,
                    report_no,
                    station_no,
                    utc_day,
                    attachment_sha256,
                    OUTCOME_REJECTED,
                    MARK_REJECTED,
                    reason,
                    now,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _insert_email_ledger(
        conn: sqlite3.Connection,
        mail: IncomingMail,
        sha: str | None,
        *,
        outcome: str,
        mark_state: str,
        error: str | None,
        now: str,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO emails
                (mailbox, uid_validity, uid, message_id, subject, from_addr,
                 report_no, station_no, utc_day, attachment_sha256, outcome,
                 mark_state, error, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mail.mailbox,
                mail.uid_validity,
                mail.uid,
                mail.message_id,
                mail.subject,
                mail.from_addr,
                mail.parsed.report_no,
                mail.parsed.station_no,
                mail.parsed.utc_day,
                sha,
                outcome,
                mark_state,
                error,
                now,
            ),
        )
        return int(cur.lastrowid)

    def _store_attachment(
        self,
        conn: sqlite3.Connection,
        mail: IncomingMail,
        sha: str,
        now: str,
    ) -> int:
        existing = conn.execute(
            "SELECT id FROM attachments WHERE sha256 = ?", (sha,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO attachments
                (sha256, size_bytes, content, filename, content_type,
                 report_no, station_no, utc_day, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha,
                len(mail.attachment),
                mail.attachment,
                mail.filename,
                mail.content_type,
                mail.parsed.report_no,
                mail.parsed.station_no,
                mail.parsed.utc_day,
                now,
            ),
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------- marking bookkeeping
    def complete_mark(self, email_id: int, *, success: bool, error: str | None) -> None:
        now = utc_now()
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT mark_attempts FROM emails WHERE id = ?", (email_id,)
            ).fetchone()
            if row is None:
                return
            attempts = int(row["mark_attempts"]) + 1
            if success:
                conn.execute(
                    """
                    UPDATE emails
                       SET mark_state = ?, mark_attempts = ?, mark_last_error = NULL,
                           marked_at = ?
                     WHERE id = ?
                    """,
                    (MARK_READ, attempts, now, email_id),
                )
                conn.execute("DELETE FROM pending_marks WHERE email_id = ?", (email_id,))
            else:
                conn.execute(
                    """
                    UPDATE emails
                       SET mark_state = ?, mark_attempts = ?, mark_last_error = ?
                     WHERE id = ?
                    """,
                    (MARK_FAILED, attempts, error or "unknown error", email_id),
                )
                conn.execute(
                    "UPDATE pending_marks SET updated_at = ? WHERE email_id = ?",
                    (now, email_id),
                )

    def mark_pending_stale(self, mailbox: str, current_uid_validity: int) -> int:
        """Drop retry records that belong to an obsolete UIDVALIDITY.

        Their old UIDs must never be used to flag messages in the rebuilt
        mailbox; the unread rescan will create fresh records with new UIDs.
        """
        with self._immediate() as conn:
            rows = conn.execute(
                """
                SELECT email_id FROM pending_marks
                 WHERE mailbox = ? AND uid_validity != ?
                """,
                (mailbox, current_uid_validity),
            ).fetchall()
            if not rows:
                return 0
            ids = [int(r["email_id"]) for r in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE emails SET mark_state = ? WHERE id IN ({placeholders})",
                (MARK_STALE, *ids),
            )
            conn.execute(
                f"DELETE FROM pending_marks WHERE email_id IN ({placeholders})",
                tuple(ids),
            )
            return len(ids)

    def mark_missing(self, email_id: int, reason: str) -> None:
        with self._immediate() as conn:
            conn.execute(
                "UPDATE emails SET mark_state = ?, mark_last_error = ? WHERE id = ?",
                (MARK_MISSING, reason, email_id),
            )
            conn.execute("DELETE FROM pending_marks WHERE email_id = ?", (email_id,))

    def clear_seen_pending_after_crash(self, email_id: int) -> None:
        """Close a pending mark when the server says the UID is already ``\\Seen``."""
        with self._immediate() as conn:
            conn.execute(
                "UPDATE emails SET mark_state = ?, marked_at = ?, mark_last_error = NULL WHERE id = ?",
                (MARK_READ, utc_now(), email_id),
            )
            conn.execute("DELETE FROM pending_marks WHERE email_id = ?", (email_id,))

    def pending_marks(self, mailbox: str) -> list[sqlite3.Row]:
        with self._session() as conn:
            return conn.execute(
                """
                SELECT p.email_id, p.mailbox, p.uid_validity, p.uid,
                       e.mark_attempts, e.mark_state
                  FROM pending_marks p
                  JOIN emails e ON e.id = p.email_id
                 WHERE p.mailbox = ?
                 ORDER BY p.email_id
                """,
                (mailbox,),
            ).fetchall()

    # ------------------------------------------------------- UIDVALIDITY state
    def sync_uid_validity(self, mailbox: str, uid_validity: int) -> tuple[bool, int | None]:
        """Return (changed, previous_uid_validity) and persist the observation."""
        now = utc_now()
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT * FROM mailbox_state WHERE mailbox = ?", (mailbox,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO mailbox_state
                        (mailbox, uid_validity, first_seen_at, last_connected_at,
                         last_uidvalidity_change_at)
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (mailbox, uid_validity, now, now),
                )
                return False, None
            previous = int(row["uid_validity"])
            if previous == uid_validity:
                conn.execute(
                    "UPDATE mailbox_state SET last_connected_at = ? WHERE mailbox = ?",
                    (now, mailbox),
                )
                return False, previous
            conn.execute(
                """
                UPDATE mailbox_state
                   SET uid_validity = ?, last_connected_at = ?,
                       last_uidvalidity_change_at = ?
                 WHERE mailbox = ?
                """,
                (uid_validity, now, now, mailbox),
            )
            return True, previous

    def record_scan_error(
        self, mailbox: str | None, uid_validity: int | None, uid: int | None, error: str
    ) -> None:
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO scan_errors (mailbox, uid_validity, uid, error, occurred_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (mailbox, uid_validity, uid, error, utc_now()),
            )

    def mailbox_status(self, mailbox: str) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM mailbox_state WHERE mailbox = ?", (mailbox,)
            ).fetchone()
            return dict(row) if row is not None else None

    # --------------------------------------------------------------- queries
    def list_readings(
        self,
        *,
        report_no: str | None = None,
        station_no: str | None = None,
        utc_day: str | None = None,
        email_id: int | None = None,
        mark_state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if report_no is not None:
            where.append("r.report_no = ?")
            params.append(report_no)
        if station_no is not None:
            where.append("r.station_no = ?")
            params.append(station_no)
        if utc_day is not None:
            where.append("r.utc_day = ?")
            params.append(utc_day)
        if email_id is not None:
            where.append("r.email_id = ?")
            params.append(email_id)
        if mark_state is not None:
            where.append("e.mark_state = ?")
            params.append(mark_state)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._session() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM readings r JOIN emails e ON e.id = r.email_id" + clause,
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT r.id, r.report_no, r.station_no, r.utc_seconds, r.utc_day,
                       r.level_mm, r.attachment_id, r.email_id, r.created_at,
                       e.outcome, e.mark_state, e.mark_attempts, e.mark_last_error,
                       e.marked_at, e.uid_validity, e.uid, e.mailbox,
                       a.sha256 AS attachment_sha256, a.filename AS attachment_filename
                  FROM readings r
                  JOIN emails e ON e.id = r.email_id
                  JOIN attachments a ON a.id = r.attachment_id
                {clause}
                 ORDER BY r.utc_seconds, r.id
                 LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows], total

    def get_reading(self, reading_id: int) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute(
                """
                SELECT r.id, r.report_no, r.station_no, r.utc_seconds, r.utc_day,
                       r.level_mm, r.attachment_id, r.email_id, r.created_at,
                       e.outcome, e.mark_state, e.mark_attempts, e.mark_last_error,
                       e.marked_at, e.uid_validity, e.uid, e.mailbox,
                       a.sha256 AS attachment_sha256, a.filename AS attachment_filename
                  FROM readings r
                  JOIN emails e ON e.id = r.email_id
                  JOIN attachments a ON a.id = r.attachment_id
                 WHERE r.id = ?
                """,
                (reading_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_attachments(self, *, report_no: str | None = None, limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        where = " WHERE report_no = ?" if report_no else ""
        params: list[Any] = [report_no] if report_no else []
        with self._session() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM attachments" + where, params).fetchone()[0])
            rows = conn.execute(
                """
                SELECT id, sha256, size_bytes, filename, content_type,
                       report_no, station_no, utc_day, created_at
                  FROM attachments
                """
                + where
                + " ORDER BY id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows], total

    def get_attachment(self, attachment_id: int) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def list_emails(
        self,
        *,
        report_no: str | None = None,
        outcome: str | None = None,
        mark_state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        for column, value in (("report_no", report_no), ("outcome", outcome), ("mark_state", mark_state)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._session() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM emails" + clause, params).fetchone()[0])
            rows = conn.execute(
                """
                SELECT id, mailbox, uid_validity, uid, message_id, subject, from_addr,
                       report_no, station_no, utc_day, attachment_sha256, outcome,
                       mark_state, mark_attempts, mark_last_error, error, received_at,
                       marked_at
                  FROM emails
                """
                + clause
                + " ORDER BY id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows], total

    def get_email(self, email_id: int) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
            return dict(row) if row is not None else None

    def report_status(self, report_no: str) -> dict[str, Any] | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT * FROM daily_reports WHERE report_no = ?", (report_no,)
            ).fetchone()
            return dict(row) if row is not None else None

    def counts(self) -> dict[str, int]:
        with self._session() as conn:
            def scalar(sql: str, params: Sequence[Any] = ()) -> int:
                return int(conn.execute(sql, params).fetchone()[0])

            return {
                "reports": scalar("SELECT COUNT(*) FROM daily_reports"),
                "readings": scalar("SELECT COUNT(*) FROM readings"),
                "attachments": scalar("SELECT COUNT(*) FROM attachments"),
                "emails": scalar("SELECT COUNT(*) FROM emails"),
                "pending_marks": scalar("SELECT COUNT(*) FROM pending_marks"),
            }

