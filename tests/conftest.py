"""Shared pytest fixtures: an in-process mock IMAP server and worker helpers."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.imap_client import ImapClient  # noqa: E402
from app.worker import IntakeWorker  # noqa: E402
from mock_imap.server import MockImapServer  # noqa: E402


@pytest.fixture()
def imap_server(tmp_path):
    server = MockImapServer(
        ("127.0.0.1", 0),
        username="hydro",
        password="hydropass",
        seed_dir=ROOT / "mock_imap" / "mails",
        initial_uid_validity=1000,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "qc.sqlite3")


def make_worker(db: Database, server: MockImapServer) -> IntakeWorker:
    port = server.server_address[1]

    def factory() -> ImapClient:
        return ImapClient(
            host="127.0.0.1",
            port=port,
            user="hydro",
            password="hydropass",
        )

    return IntakeWorker(db, factory, mailbox="INBOX")
