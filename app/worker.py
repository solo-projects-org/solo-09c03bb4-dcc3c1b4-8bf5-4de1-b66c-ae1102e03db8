"""Background IMAP intake worker.

Life cycle
----------
``run_once`` is the testable unit: connect, verify UIDVALIDITY, retry any
pending mark operations, scan unread mail and ingest each message.  A full
``run_forever`` loop surrounds it with reconnect/back-off logic.

Ordering is deliberate:

1. Pending markers (from a previous crash or a failed STORE) are retried
   *before* the unread scan.  If a mail was already persisted but never
   flagged, it now shows as ``UNSEEN`` and would otherwise be scanned again;
   marking it first avoids a duplicate pass.  Re-ingesting is harmless anyway
   (the report is a byte-identical duplicate), but retrying first keeps
   attempts low.
2. The unread scan processes only UIDs not already in the ledger for the
   current UIDVALIDITY.
3. Each accepted mail is persisted transactionally, then marked.  A failed
   mark is left in ``pending_marks`` for a later attempt.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .csv_parser import CsvValidationError, parse_daily_csv, sha256_hex
from .db import OUTCOME_REJECTED, Database, IncomingMail, MailIdentity
from .imap_client import (
    AttachmentError,
    FetchedMessage,
    ImapClient,
    ImapError,
    UidValidityError,
)

logger = logging.getLogger("qc.worker")


@dataclass
class RunStats:
    uid_validity: int | None = None
    uid_validity_changed: bool = False
    unread_seen: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    marked: int = 0
    mark_failed: int = 0
    stale_cleared: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "uid_validity": self.uid_validity,
            "uid_validity_changed": self.uid_validity_changed,
            "unread_seen": self.unread_seen,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "marked": self.marked,
            "mark_failed": self.mark_failed,
            "stale_cleared": self.stale_cleared,
            "errors": list(self.errors),
        }


class IntakeWorker:
    def __init__(
        self,
        db: Database,
        client_factory: Callable[[], ImapClient],
        *,
        mailbox: str = "INBOX",
        max_attachment_bytes: int = 5_000_000,
        max_readings_per_report: int = 100_000,
    ):
        self.db = db
        self._client_factory = client_factory
        self.mailbox = mailbox
        self.max_attachment_bytes = max_attachment_bytes
        self.max_readings_per_report = max_readings_per_report
        self.client: ImapClient | None = None
        self.last_stats: RunStats | None = None
        self._recent_runs: list[dict] = []

    def recent_runs(self, limit: int = 50) -> list[dict]:
        return self._recent_runs[-limit:]

    # ------------------------------------------------------------------ run
    def run_once(self) -> RunStats:
        self.last_stats = self._run_once()
        self._recent_runs.append(
            {
                "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **self.last_stats.as_dict(),
            }
        )
        del self._recent_runs[:-100]
        return self.last_stats

    def _run_once(self) -> RunStats:
        stats = RunStats()
        client = self._client_factory()
        self.client = client
        try:
            try:
                client.connect()
            except (OSError, ImapError, Exception) as exc:  # noqa: BLE001 - reported
                stats.errors.append(f"connect/login failed: {exc}")
                return stats
            try:
                uid_validity = client.select(self.mailbox)
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"select mailbox failed: {exc}")
                return stats
            stats.uid_validity = uid_validity

            changed, previous = self.db.sync_uid_validity(self.mailbox, uid_validity)
            stats.uid_validity_changed = changed
            if changed:
                logger.warning(
                    "UIDVALIDITY for %s changed from %s to %s; discarding old-epoch "
                    "pending markers and rescanning unread mail",
                    self.mailbox,
                    previous,
                    uid_validity,
                )
            stale = self.db.mark_pending_stale(self.mailbox, uid_validity)
            stats.stale_cleared = stale

            self._retry_pending_marks(client, uid_validity, stats)
            self._scan_unread(client, uid_validity, stats)
            return stats
        finally:
            try:
                client.close()
            finally:
                self.client = None

    # ------------------------------------------------------ pending markers
    def _retry_pending_marks(
        self, client: ImapClient, uid_validity: int, stats: RunStats
    ) -> None:
        for row in self.db.pending_marks(self.mailbox):
            if int(row["uid_validity"]) != uid_validity:
                continue
            uid = int(row["uid"])
            email_id = int(row["email_id"])
            try:
                if self._already_seen(client, uid):
                    # The server already has \Seen on this exact UID under the
                    # same UIDVALIDITY (e.g. the store succeeded and the client
                    # crashed before recording it).  Nothing to retry.
                    self.db.clear_seen_pending_after_crash(email_id)
                    stats.marked += 1
                    continue
                self._mark(client, uid, uid_validity)
            except MessageVanished:
                self.db.mark_missing(
                    email_id, "UID no longer exists in the mailbox under this UIDVALIDITY"
                )
                stats.errors.append(f"pending mark: UID {uid} vanished")
            except Exception as exc:  # noqa: BLE001
                self.db.complete_mark(email_id, success=False, error=str(exc))
                stats.mark_failed += 1
                stats.errors.append(f"pending mark UID {uid} failed: {exc}")
                logger.warning("retrying \\Seen for UID %s failed: %s", uid, exc)
            else:
                self.db.complete_mark(email_id, success=True, error=None)
                stats.marked += 1

    def _already_seen(self, client: ImapClient, uid: int) -> bool:
        flags = client.fetch_flags(uid)
        if not flags:
            raise MessageVanished(uid)
        return b"\\seen" in flags.lower()

    def _mark(self, client: ImapClient, uid: int, uid_validity: int) -> None:
        """Set \\Seen; raise MessageVanished if the UID is gone."""
        try:
            client.mark_seen(uid, uid_validity)
        except ImapError as exc:
            if "no message" in str(exc).lower() or "not found" in str(exc).lower():
                raise MessageVanished(uid) from exc
            raise

    # ----------------------------------------------------------- unread scan
    def _scan_unread(
        self, client: ImapClient, uid_validity: int, stats: RunStats
    ) -> None:
        uids = client.search_unread()
        stats.unread_seen = len(uids)
        for uid in uids:
            identity = MailIdentity(self.mailbox, uid_validity, uid)
            if self.db.email_recorded(identity):
                # Already handled under this UIDVALIDITY.  The only reason it
                # is still unread is a failed/pending marker; that was retried
                # above, so do not ingest it again on this pass.
                continue
            self._process(client, uid, uid_validity, stats)

    def _process(
        self, client: ImapClient, uid: int, uid_validity: int, stats: RunStats
    ) -> None:
        try:
            fetched = client.fetch_message(uid)
        except UidValidityError as exc:
            # The mailbox changed under us.  Abort the pass and let the next
            # connection re-read UIDVALIDITY before touching any UID.
            stats.errors.append(str(exc))
            logger.warning("UIDVALIDITY changed during fetch of UID %s: %s", uid, exc)
            raise
        except (AttachmentError, ImapError) as exc:
            self._reject(uid, uid_validity, None, f"cannot read message: {exc}", stats)
            return
        except Exception as exc:  # noqa: BLE001
            self.db.record_scan_error(self.mailbox, uid_validity, uid, f"fetch failed: {exc}")
            stats.errors.append(f"fetch UID {uid} failed: {exc}")
            return

        if len(fetched.attachment) > self.max_attachment_bytes:
            self._reject(
                uid,
                uid_validity,
                fetched,
                f"attachment is {len(fetched.attachment)} bytes, limit is "
                f"{self.max_attachment_bytes}",
                stats,
                attachment=fetched.attachment,
            )
            return

        try:
            parsed = parse_daily_csv(
                fetched.attachment,
                max_readings=self.max_readings_per_report,
            )
        except CsvValidationError as exc:
            self._reject(
                uid,
                uid_validity,
                fetched,
                f"invalid CSV: {exc}",
                stats,
                attachment=fetched.attachment,
            )
            return

        mail = IncomingMail(
            mailbox=self.mailbox,
            uid_validity=uid_validity,
            uid=uid,
            message_id=fetched.message_id,
            subject=fetched.subject,
            from_addr=fetched.from_addr,
            filename=fetched.filename,
            content_type=fetched.content_type,
            attachment=fetched.attachment,
            parsed=parsed,
        )
        try:
            result = self.db.ingest(mail)
        except Exception as exc:  # noqa: BLE001
            self.db.record_scan_error(self.mailbox, uid_validity, uid, f"ingest failed: {exc}")
            stats.errors.append(f"ingest UID {uid} failed: {exc}")
            logger.exception("ingest failed for UID %s", uid)
            return

        if result.outcome == OUTCOME_REJECTED:
            stats.rejected += 1
            logger.warning(
                "rejected UID %s (%s); leaving it unread", uid, result.reason
            )
            return
        if result.outcome == "duplicate":
            stats.duplicates += 1
        else:
            stats.accepted += 1

        try:
            self._mark(client, uid, uid_validity)
        except MessageVanished:
            self.db.mark_missing(result.email_id, "UID vanished before it could be marked")
            stats.errors.append(f"UID {uid} vanished before \\Seen")
            return
        except Exception as exc:  # noqa: BLE001
            self.db.complete_mark(result.email_id, success=False, error=str(exc))
            stats.mark_failed += 1
            stats.errors.append(f"mark UID {uid} failed: {exc}")
            logger.warning("could not mark UID %s seen; will retry: %s", uid, exc)
            return
        self.db.complete_mark(result.email_id, success=True, error=None)
        stats.marked += 1

    def _reject(
        self,
        uid: int,
        uid_validity: int,
        fetched: FetchedMessage | None,
        reason: str,
        stats: RunStats,
        *,
        attachment: bytes | None = None,
    ) -> None:
        """Record a rejected mail without persisting anything and without marking."""
        stats.rejected += 1
        self.db.record_scan_error(self.mailbox, uid_validity, uid, reason)
        try:
            self.db.record_rejected(
                mailbox=self.mailbox,
                uid_validity=uid_validity,
                uid=uid,
                message_id=fetched.message_id if fetched else None,
                subject=fetched.subject if fetched else None,
                from_addr=fetched.from_addr if fetched else None,
                report_no=None,
                station_no=None,
                utc_day=None,
                attachment_sha256=sha256_hex(attachment) if attachment is not None else None,
                reason=reason,
            )
        except Exception:  # noqa: BLE001
            # Ledgering a rejection must never break the pass.
            logger.exception("could not ledger rejection for UID %s", uid)
        logger.warning("rejected UID %s: %s; leaving it unread", uid, reason)

    # -------------------------------------------------------------- polling
    def run_forever(self, stop: Callable[[], bool], interval: float) -> None:
        while not stop():
            try:
                stats = self.run_once()
                if stats.errors:
                    logger.info("intake pass finished with issues: %s", stats.as_dict())
                else:
                    logger.debug("intake pass finished: %s", stats.as_dict())
            except Exception:  # noqa: BLE001
                logger.exception("intake pass crashed; will retry next interval")
            slept = 0.0
            while slept < interval and not stop():
                time.sleep(min(0.25, interval - slept))
                slept += 0.25


class MessageVanished(ImapError):
    def __init__(self, uid: int):
        self.uid = uid
        super().__init__(f"UID {uid} no longer exists")
