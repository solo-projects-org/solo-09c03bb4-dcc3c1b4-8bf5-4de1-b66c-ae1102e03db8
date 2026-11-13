"""End-to-end worker tests against the in-process mock IMAP server."""

from __future__ import annotations

from pathlib import Path

from app.db import Database
from conftest import make_worker


SEED = Path(__file__).resolve().parents[1] / "mock_imap" / "mails"


def _emails(db: Database) -> list[dict]:
    rows, _ = db.list_emails(limit=1000)
    return rows


def test_first_pass_classifies_and_marks(imap_server, db):
    stats = make_worker(db, imap_server).run_once()

    assert stats.uid_validity == 1000
    assert stats.unread_seen == 7  # one seeded message is already \Seen
    assert stats.accepted == 2
    assert stats.duplicates == 1
    assert stats.rejected == 4
    assert stats.marked == 3

    # Readings: 3 from report 1, 2 from report 2.
    rows, total = db.list_readings(limit=100)
    assert total == 5
    assert all(row["mark_state"] == "read" for row in rows)

    messages = {m["uid"]: m for m in imap_server.snapshot()["messages"]}
    # Accepted and duplicate mails are read.
    assert "\\Seen" in messages[1]["flags"]
    assert "\\Seen" in messages[2]["flags"]
    assert "\\Seen" in messages[4]["flags"]
    # Rejected mails stay unread.
    for uid in (3, 5, 6, 7):
        assert "\\Seen" not in messages[uid]["flags"]
    # Pre-read mail was never touched.
    assert "\\Seen" in messages[8]["flags"]

    ledger = {row["uid"]: row for row in _emails(db)}
    assert ledger[1]["outcome"] == "received"
    assert ledger[2]["outcome"] == "duplicate"
    assert ledger[2]["message_id"] == ledger[1]["message_id"]  # Message-ID may repeat
    assert ledger[3]["outcome"] == "rejected"
    assert ledger[3]["mark_state"] == "rejected"
    assert "different attachment" in ledger[3]["error"]
    assert ledger[6]["outcome"] == "rejected"
    assert "multiple UTC days" in ledger[6]["error"]


def test_second_pass_does_not_reprocess(imap_server, db):
    make_worker(db, imap_server).run_once()
    stats = make_worker(db, imap_server).run_once()
    # Remaining unread mails are already ledgered as rejected; nothing new.
    assert stats.accepted == 0
    assert stats.duplicates == 0
    assert stats.rejected == 0
    assert db.counts()["readings"] == 5


def test_mark_failure_leaves_pending_and_retries(imap_server, db):
    imap_server.fail_seen(2)
    stats = make_worker(db, imap_server).run_once()
    assert stats.mark_failed == 2
    # Readings are stored despite the failed mark.
    assert db.counts()["readings"] == 5
    pending = db.pending_marks("INBOX")
    assert len(pending) == 2

    stats = make_worker(db, imap_server).run_once()
    assert stats.mark_failed == 0
    assert stats.marked == 2
    assert db.pending_marks("INBOX") == []
    messages = {m["uid"]: m for m in imap_server.snapshot()["messages"]}
    assert all("\\Seen" in messages[uid]["flags"] for uid in (1, 2))


def test_mark_failure_survives_restart(imap_server, tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    imap_server.fail_seen(99)

    first = Database(db_path)
    stats = make_worker(first, imap_server).run_once()
    assert stats.mark_failed == 3
    assert len(first.pending_marks("INBOX")) == 3

    # "Restart": new Database instance on the same file, server still failing.
    second = Database(db_path)
    stats = make_worker(second, imap_server).run_once()
    assert stats.mark_failed == 3
    assert len(second.pending_marks("INBOX")) == 3

    imap_server.clear_fail_seen()
    stats = make_worker(second, imap_server).run_once()
    assert stats.marked == 3
    assert second.pending_marks("INBOX") == []
    assert second.counts()["readings"] == 5


def test_uidvalidity_change_drops_stale_pending(imap_server, db, tmp_path):
    """A rebuilt mailbox reuses UIDs; old pending markers must never mark them."""
    import shutil

    imap_server.fail_seen(1)  # UID 1 = first sample mail, stays pending
    first = make_worker(db, imap_server).run_once()
    # UID 1 might not be the one that failed depending on ordering; force the
    # failure to hit a specific mail by using the report that got accepted.
    pending_before = db.pending_marks("INBOX")
    assert len(pending_before) >= 1
    pending_uid = pending_before[0]["uid"]
    pending_validity = pending_before[0]["uid_validity"]
    assert pending_validity == 1000

    # Rebuild with a different message occupying the same low UIDs.
    new_seed = tmp_path / "rebuilt"
    new_seed.mkdir()
    shutil.copy(SEED / "06_multi_day.eml", new_seed / "01_new.eml")
    shutil.copy(SEED / "01_good_20261101.eml", new_seed / "02_good.eml")
    imap_server.seed_dir = new_seed
    imap_server.rebuild(uid_validity=7777)

    stats = make_worker(db, imap_server).run_once()
    assert stats.uid_validity == 7777
    assert stats.uid_validity_changed is True
    assert stats.stale_cleared == 1

    # The old pending marker is now stale, never used against the new mailbox.
    assert db.pending_marks("INBOX") == []
    ledger = {(row["uid_validity"], row["uid"]): row for row in _emails(db)}
    assert ledger[(pending_validity, pending_uid)]["mark_state"] == "stale"

    # UID 1 in the new epoch is the multi-day (invalid) mail and must stay unread.
    messages = {m["uid"]: m for m in imap_server.snapshot()["messages"]}
    assert "\\Seen" not in messages[1]["flags"]
    # UID 2 is a byte-identical duplicate of the accepted report: read.
    assert "\\Seen" in messages[2]["flags"]


def test_rebuild_rescan_ingests_new_unread_mail(imap_server, db):
    make_worker(db, imap_server).run_once()
    # A brand new mail lands after a rebuild (UIDs are reused from 1).
    raw = (SEED / "01_good_20261101.eml").read_bytes()
    imap_server.seed_dir = None
    imap_server.rebuild(uid_validity=4242, reseed=False)
    imap_server.append_raw(raw)
    stats = make_worker(db, imap_server).run_once()
    assert stats.uid_validity_changed is True
    assert stats.uid_validity == 4242
    assert stats.duplicates == 1  # same bytes as the already-stored report
    assert db.counts()["readings"] == 5
    messages = imap_server.snapshot()["messages"]
    assert "\\Seen" in messages[0]["flags"]


def test_run_history_is_bounded(imap_server, db):
    worker = make_worker(db, imap_server)
    worker.run_once()
    worker.run_once()
    runs = worker.recent_runs()
    assert len(runs) == 2
    assert runs[-1]["unread_seen"] <= runs[0]["unread_seen"]
    assert worker.last_stats is not None


def test_connection_failure_is_reported_not_fatal(db):
    from app.imap_client import ImapClient
    from app.worker import IntakeWorker

    worker = IntakeWorker(
        db,
        lambda: ImapClient(host="127.0.0.1", port=1, user="x", password="y", timeout=2),
    )
    stats = worker.run_once()
    assert stats.errors
    assert db.counts()["reports"] == 0
