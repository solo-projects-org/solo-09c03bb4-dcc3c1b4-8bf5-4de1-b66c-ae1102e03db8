"""API-level tests: query readings/mark status and download attachments."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.settings import Settings
from conftest import make_worker


@pytest.fixture()
def client(imap_server, tmp_path):
    db_path = tmp_path / "api.sqlite3"
    db = Database(db_path)
    make_worker(db, imap_server).run_once()

    app = create_app(
        Settings(
            db_path=str(db_path),
            imap_host="127.0.0.1",
            imap_port=imap_server.server_address[1],
            imap_user="hydro",
            imap_password="hydropass",
            worker_enabled=False,
        )
    )
    with TestClient(app) as test_client:
        yield test_client


def test_health_and_stats(client):
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["worker_enabled"] is False
    assert body["counts"]["reports"] == 2
    assert body["counts"]["readings"] == 5


def test_openapi_is_available(client):
    spec = client.get("/openapi.json").json()
    assert "/readings" in spec["paths"]
    assert "/attachments/{attachment_id}/download" in spec["paths"]
    assert "/emails" in spec["paths"]
    docs = client.get("/docs")
    assert docs.status_code == 200


def test_list_readings_by_report_and_day(client):
    response = client.get("/readings", params={"report_no": "HY2026110101"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["items"][0]["level_mm"] == 1010
    assert payload["items"][0]["utc_time"] == "2026-11-01T00:00:00Z"
    assert payload["items"][0]["mark_state"] == "read"

    response = client.get("/readings", params={"utc_day": "2026-11-02"})
    assert response.json()["total"] == 2

    assert client.get("/readings", params={"utc_day": "11/02/2026"}).status_code == 422
    assert client.get("/readings", params={"mark_state": "bogus"}).status_code == 422


def test_mail_status_lists_marking_result(client):
    response = client.get("/emails")
    assert response.status_code == 200
    items = response.json()["items"]
    by_uid = {item["uid"]: item for item in items}
    assert by_uid[1]["outcome"] == "received"
    assert by_uid[1]["mark_state"] == "read"
    assert by_uid[2]["outcome"] == "duplicate"
    assert by_uid[2]["message_id"] == by_uid[1]["message_id"]
    assert by_uid[3]["outcome"] == "rejected"
    assert by_uid[3]["mark_state"] == "rejected"

    rejected = client.get("/emails", params={"outcome": "rejected"}).json()
    assert rejected["total"] == 4


def test_report_endpoint_shows_mails_and_attachment(client):
    response = client.get("/reports/HY2026110101")
    assert response.status_code == 200
    body = response.json()
    assert body["station_no"] == "S001"
    assert body["utc_day"] == "2026-11-01"
    assert body["reading_count"] == 3
    assert body["attachment"]["sha256"] == body["sha256"]
    assert {mail["outcome"] for mail in body["mails"]} == {"received", "duplicate", "rejected"}
    assert client.get("/reports/NOPE").status_code == 404


def test_download_original_attachment_bytes(client):
    listing = client.get("/attachments").json()
    assert listing["total"] == 2
    attachment = listing["items"][0]
    response = client.get(attachment["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    body = response.content
    assert hashlib.sha256(body).hexdigest() == attachment["sha256"]
    assert body.startswith("日报号,站号,UTC秒,水位".encode())
    assert "HY2026110101,S001,1793491200,1010" in body.decode()
    assert attachment["download_url"] in client.get(
        f"/attachments/{attachment['id']}"
    ).json()["download_url"]


def test_mailbox_state_endpoint(client):
    response = client.get("/mailbox/state")
    body = response.json()
    assert body["mailbox"] == "INBOX"
    assert body["state"]["uid_validity"] == 1000
    assert body["pending_marks"] == []


def test_pipeline_runs_endpoint(client):
    body = client.get("/pipeline/runs").json()
    assert body["worker_enabled"] is False
    assert body["runs"] == []
    assert body["last_run"] is None


def test_reading_not_found(client):
    assert client.get("/readings/9999").status_code == 404
    assert client.get("/attachments/9999/download").status_code == 404


def test_rejected_mail_has_no_readings_or_attachment(client):
    """Rejected mail contributes no reading rows, even when it reuses a report number."""
    rejected = client.get("/emails", params={"outcome": "rejected"}).json()["items"]
    assert rejected
    for item in rejected:
        readings = client.get("/readings", params={"email_id": item["id"]}).json()
        assert readings["total"] == 0


def test_conflicting_report_keeps_first_accepted_readings(client):
    readings = client.get("/readings", params={"report_no": "HY2026110101"}).json()
    assert readings["total"] == 3
    assert [item["level_mm"] for item in readings["items"]] == [1010, 1011, 1012]
