"""FastAPI application exposing the intake/QC read API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from .db import Database
from .settings import Settings  # noqa: F401  (re-exported for tests)

logger = logging.getLogger("qc.api")

MARK_STATES = {"pending", "read", "failed", "stale", "rejected", "missing"}
OUTCOMES = {"received", "duplicate", "rejected"}


def _iso_day(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} must be a UTC date YYYY-MM-DD"
        ) from exc
    return value


def _reading_payload(row: dict[str, Any]) -> dict[str, Any]:
    moment = datetime.fromtimestamp(int(row["utc_seconds"]), tz=timezone.utc)
    return {
        "id": row["id"],
        "report_no": row["report_no"],
        "station_no": row["station_no"],
        "utc_seconds": row["utc_seconds"],
        "utc_time": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "utc_day": row["utc_day"],
        "level_mm": row["level_mm"],
        "email_id": row["email_id"],
        "attachment_id": row["attachment_id"],
        "attachment_sha256": row["attachment_sha256"],
        "received_at": row["created_at"],
        "mark_state": row["mark_state"],
        "outcome": row["outcome"],
        "mail": {
            "mailbox": row["mailbox"],
            "uid_validity": row["uid_validity"],
            "uid": row["uid"],
        },
    }


def _attachment_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "filename": row.get("filename"),
        "content_type": row.get("content_type"),
        "report_no": row.get("report_no"),
        "station_no": row.get("station_no"),
        "utc_day": row.get("utc_day"),
        "created_at": row.get("created_at"),
        "download_url": f"/attachments/{row['id']}/download",
    }


def _email_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "mailbox": row["mailbox"],
        "uid_validity": row["uid_validity"],
        "uid": row["uid"],
        "message_id": row["message_id"],
        "subject": row["subject"],
        "from": row["from_addr"],
        "report_no": row["report_no"],
        "station_no": row["station_no"],
        "utc_day": row["utc_day"],
        "attachment_sha256": row["attachment_sha256"],
        "outcome": row["outcome"],
        "mark_state": row["mark_state"],
        "mark_attempts": row["mark_attempts"],
        "mark_last_error": row["mark_last_error"],
        "error": row["error"],
        "received_at": row["received_at"],
        "marked_at": row["marked_at"],
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=getattr(logging, cfg.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        db = Database(cfg.db_path)
        app.state.settings = cfg
        app.state.db = db
        stop_event = None
        worker_thread = None
        if cfg.worker_enabled:
            stop_event, worker_thread = _start_worker(app, cfg)
        try:
            yield
        finally:
            if stop_event is not None:
                stop_event.set()
            if worker_thread is not None:
                worker_thread.join(timeout=10)

    app = FastAPI(
        title="水位日值质控收录 API",
        description=(
            "Collects daily water-level CSV attachments from unread mail in a "
            "configured IMAP inbox. A mail is marked read only after its parsed "
            "readings, original attachment bytes and a retryable 'mark as read' "
            "record have been committed together. Invalid mail stays unread."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------- helpers
    def db(request: Request) -> Database:
        return request.app.state.db

    def cfg_of(request: Request) -> Settings:
        return request.app.state.settings

    # ---------------------------------------------------------------- meta
    @app.get("/health", tags=["system"], summary="Liveness and configuration summary")
    def health(request: Request) -> dict[str, Any]:
        settings: Settings = cfg_of(request)
        return {
            "status": "ok",
            "worker_enabled": settings.worker_enabled,
            "imap_host": settings.imap_host,
            "imap_mailbox": settings.imap_mailbox,
            "db_path": settings.db_path,
            "counts": db(request).counts(),
            "mailbox_state": db(request).mailbox_status(settings.imap_mailbox),
        }

    @app.get("/stats", tags=["system"], summary="Row counts")
    def stats(request: Request) -> dict[str, Any]:
        return db(request).counts()

    @app.get(
        "/pipeline/runs",
        tags=["system"],
        summary="Recent intake pass results",
    )
    def pipeline_runs(
        request: Request,
        limit: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        worker = getattr(request.app.state, "worker", None)
        runs = worker.recent_runs(limit) if worker is not None else []
        return {
            "worker_enabled": cfg_of(request).worker_enabled,
            "last_run": worker.last_stats.as_dict() if worker is not None and worker.last_stats else None,
            "runs": runs,
        }

    # ------------------------------------------------------------ readings
    @app.get("/readings", tags=["readings"], summary="Query collected readings")
    def list_readings(
        request: Request,
        report_no: str | None = Query(None, description="日报号"),
        station_no: str | None = Query(None, description="站号"),
        utc_day: str | None = Query(None, description="UTC day, YYYY-MM-DD"),
        email_id: int | None = Query(None),
        mark_state: str | None = Query(None, description="pending/read/failed/stale/missing"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        utc_day = _iso_day(utc_day, "utc_day")
        if mark_state is not None and mark_state not in MARK_STATES:
            raise HTTPException(422, f"mark_state must be one of {sorted(MARK_STATES)}")
        rows, total = db(request).list_readings(
            report_no=report_no,
            station_no=station_no,
            utc_day=utc_day,
            email_id=email_id,
            mark_state=mark_state,
            limit=limit,
            offset=offset,
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [_reading_payload(row) for row in rows],
        }

    @app.get("/readings/{reading_id}", tags=["readings"], summary="One reading")
    def get_reading(request: Request, reading_id: int) -> dict[str, Any]:
        row = db(request).get_reading(reading_id)
        if row is None:
            raise HTTPException(404, "reading not found")
        return _reading_payload(row)

    # ---------------------------------------------------------- attachments
    @app.get("/attachments", tags=["attachments"], summary="List stored original attachments")
    def list_attachments(
        request: Request,
        report_no: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        rows, total = db(request).list_attachments(
            report_no=report_no, limit=limit, offset=offset
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [_attachment_payload(row) for row in rows],
        }

    @app.get(
        "/attachments/{attachment_id}",
        tags=["attachments"],
        summary="Attachment metadata",
    )
    def get_attachment(request: Request, attachment_id: int) -> dict[str, Any]:
        row = db(request).get_attachment(attachment_id)
        if row is None:
            raise HTTPException(404, "attachment not found")
        return _attachment_payload(row)

    @app.get(
        "/attachments/{attachment_id}/download",
        tags=["attachments"],
        summary="Download the original attachment bytes",
        response_class=Response,
        responses={200: {"content": {"text/csv": {}}}},
    )
    def download_attachment(request: Request, attachment_id: int) -> Response:
        row = db(request).get_attachment(attachment_id)
        if row is None:
            raise HTTPException(404, "attachment not found")
        content: bytes = row["content"]
        filename = row.get("filename") or f"attachment-{attachment_id}.csv"
        return Response(
            content=content,
            media_type=row.get("content_type") or "text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Attachment-SHA256": row["sha256"],
            },
        )

    # --------------------------------------------------------------- emails
    @app.get("/emails", tags=["mail status"], summary="Mail processing and mark status")
    def list_emails(
        request: Request,
        report_no: str | None = Query(None),
        outcome: str | None = Query(None, description="received/duplicate/rejected"),
        mark_state: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        if outcome is not None and outcome not in OUTCOMES:
            raise HTTPException(422, f"outcome must be one of {sorted(OUTCOMES)}")
        if mark_state is not None and mark_state not in MARK_STATES:
            raise HTTPException(422, f"mark_state must be one of {sorted(MARK_STATES)}")
        rows, total = db(request).list_emails(
            report_no=report_no, outcome=outcome, mark_state=mark_state,
            limit=limit, offset=offset,
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [_email_payload(row) for row in rows],
        }

    @app.get("/emails/{email_id}", tags=["mail status"], summary="One mail record")
    def get_email(request: Request, email_id: int) -> dict[str, Any]:
        row = db(request).get_email(email_id)
        if row is None:
            raise HTTPException(404, "mail record not found")
        return _email_payload(row)

    # -------------------------------------------------------------- reports
    @app.get("/reports/{report_no}", tags=["readings"], summary="Report status")
    def get_report(request: Request, report_no: str) -> dict[str, Any]:
        row = db(request).report_status(report_no)
        if row is None:
            raise HTTPException(404, "report not found")
        attachment = db(request).get_attachment(int(row["attachment_id"]))
        emails, _ = db(request).list_emails(report_no=report_no, limit=1000)
        return {
            "report_no": row["report_no"],
            "station_no": row["station_no"],
            "utc_day": row["utc_day"],
            "sha256": row["sha256"],
            "reading_count": row["reading_count"],
            "received_at": row["received_at"],
            "attachment": _attachment_payload(attachment) if attachment else None,
            "mails": [_email_payload(email) for email in emails],
        }

    @app.get(
        "/mailbox/state",
        tags=["mail status"],
        summary="Observed UIDVALIDITY and pending mark retries",
    )
    def mailbox_state(request: Request) -> dict[str, Any]:
        settings: Settings = cfg_of(request)
        state = db(request).mailbox_status(settings.imap_mailbox)
        pending = [dict(row) for row in db(request).pending_marks(settings.imap_mailbox)]
        return {
            "mailbox": settings.imap_mailbox,
            "state": state,
            "pending_marks": pending,
        }

    return app


def _start_worker(app: FastAPI, settings: Settings):
    import threading

    from .imap_client import ImapClient
    from .worker import IntakeWorker

    def factory() -> ImapClient:
        return ImapClient(
            host=settings.imap_host,
            port=settings.imap_port,
            user=settings.imap_user,
            password=settings.imap_password,
            use_ssl=settings.imap_use_ssl,
            ssl_verify=settings.imap_ssl_verify,
            timeout=settings.imap_timeout_seconds,
        )

    worker = IntakeWorker(
        app.state.db,
        factory,
        mailbox=settings.imap_mailbox,
        max_attachment_bytes=settings.max_attachment_bytes,
        max_readings_per_report=settings.max_readings_per_report,
    )
    app.state.worker = worker
    stop_event = threading.Event()

    def loop() -> None:
        worker.run_forever(stop_event.is_set, settings.poll_interval_seconds)

    thread = threading.Thread(target=loop, name="imap-intake", daemon=True)
    thread.start()
    return stop_event, thread


app = create_app()
