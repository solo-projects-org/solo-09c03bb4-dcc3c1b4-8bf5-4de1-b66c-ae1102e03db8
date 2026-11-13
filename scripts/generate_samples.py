"""Seed the mock local mailbox with sample daily water-level mails."""

from __future__ import annotations

import base64
from pathlib import Path


EOLS = "\r\n"


def _csv(rows: list[list[str]]) -> bytes:
    return ("\n".join(",".join(row) for row in rows) + "\n").encode("utf-8")


def _build_mail(
    *,
    message_id: str,
    subject: str,
    sender: str = "logger@station.example",
    filename: str = "daily.csv",
    body: bytes | None = None,
    extra_parts: list[tuple[str, str, bytes]] | None = None,
    in_reply_to: str | None = None,
    body_text: str | None = None,
) -> bytes:
    boundary = "mock-boundary-42"
    lines: list[str] = [
        f"From: {sender}",
        f"To: intake@hydro.example",
        f"Subject: {subject}",
        f"Message-ID: {message_id}",
        "MIME-Version: 1.0",
        'Content-Type: multipart/mixed; boundary="mock-boundary-42"',
        "Date: Mon, 02 Nov 2026 00:05:00 +0000",
    ]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    parts = ["--mock-boundary-42"]
    parts.append("Content-Type: text/plain; charset=utf-8")
    parts.append("")
    parts.append(body_text or "Attached: daily water-level CSV.")
    parts.append("--mock-boundary-42")
    parts.append(f'Content-Type: text/csv; name="{filename}"')
    parts.append(f'Content-Disposition: attachment; filename="{filename}"')
    parts.append("Content-Transfer-Encoding: base64")
    parts.append("")
    encoded = base64.b64encode(body if body is not None else b"").decode("ascii")
    for i in range(0, len(encoded), 76):
        parts.append(encoded[i : i + 76])
    for name, ctype, content in extra_parts or []:
        parts.append("--mock-boundary-42")
        parts.append(f'Content-Type: {ctype}; name="{name}"')
        parts.append(f'Content-Disposition: attachment; filename="{name}"')
        parts.append("Content-Transfer-Encoding: base64")
        parts.append("")
        enc = base64.b64encode(content).decode("ascii")
        for i in range(0, len(enc), 76):
            parts.append(enc[i : i + 76])
    parts.append("--mock-boundary-42--")
    parts.append("")
    return (EOLS.join(lines) + EOLS + EOLS + EOLS.join(parts)).encode("utf-8")


# Report numbers and stations are self-describing so the demo output is easy
# to check by eye.  1793491200 .. 1793498400 are 2026-11-01 00:00..02:00 UTC.
R1_ROWS = [
    ["HY2026110101", "S001", "1793491200", "1010"],
    ["HY2026110101", "S001", "1793494800", "1011"],
    ["HY2026110101", "S001", "1793498400", "1012"],
]


def build_seed_files() -> dict[str, bytes]:
    good = _csv([["日报号", "站号", "UTC秒", "水位"], *R1_ROWS])
    duplicate_same_bytes = _build_mail(
        message_id="<dup-1@station.example>",
        subject="Daily water level HY2026110101 S001",
        body=good,
    )
    same_number_diff_content = _csv(
        [["日报号", "站号", "UTC秒", "水位"], ["HY2026110101", "S001", "1793491200", "9999"]]
    )
    second_good = _csv(
        [
            ["日报号", "站号", "UTC秒", "水位"],
            ["HY2026110102", "S002", "1793577600", "2050"],
            ["HY2026110102", "S002", "1793581200", "2051"],
        ]
    )
    invalid_multi_day = _csv(
        [
            ["日报号", "站号", "UTC秒", "水位"],
            ["HY2026110103", "S003", "1793491200", "3000"],
            ["HY2026110103", "S003", "1793581200", "3001"],
        ]
    )
    return {
        # 1. Valid, unread: should be ingested and marked read.
        "01_good_20261101.eml": _build_mail(
            message_id="<good-1@station.example>",
            subject="Daily water level HY2026110101 S001",
            body=good,
        ),
        # 2. Duplicate report number, byte-identical attachment (also a
        #    repeated Message-ID): accepted as duplicate, marked read.
        "02_duplicate_same_bytes.eml": _build_mail(
            message_id="<good-1@station.example>",
            subject="Daily water level HY2026110101 S001 (resend)",
            body=good,
        ),
        # 3. Same report number, different bytes: rejected, must stay unread.
        "03_same_report_different_content.eml": _build_mail(
            message_id="<conflict-1@station.example>",
            subject="Daily water level HY2026110101 S001 (corrected)",
            body=same_number_diff_content,
        ),
        # 4. Valid second report, also has a PDF attachment alongside the CSV.
        "04_good_with_pdf.eml": _build_mail(
            message_id="<good-2@station.example>",
            subject="Daily water level HY2026110102 S002",
            body=second_good,
            extra_parts=[("notes.pdf", "application/pdf", b"%PDF-1.4\n%mock\n")],
        ),
        # 5. Invalid: two CSV attachments; rejected and left unread.
        "05_two_csv_attachments.eml": _build_mail(
            message_id="<two-csv@station.example>",
            subject="Daily water level (two attachments)",
            body=second_good,
            extra_parts=[("other.csv", "text/csv", second_good)],
        ),
        # 6. Invalid: rows span two UTC days; rejected and left unread.
        "06_multi_day.eml": _build_mail(
            message_id="<multi-day@station.example>",
            subject="Daily water level (spans days)",
            body=invalid_multi_day,
        ),
        # 7. Already seen before the service starts: never scanned.
        "07_read_already.eml": _build_mail(
            message_id="<already-seen@station.example>",
            subject="Old water level",
            body=_csv(
                [["日报号", "站号", "UTC秒", "水位"], ["HY2026103101", "S009", "1793404800", "40"]]
            ),
        ),
        # 8. Invalid CSV content (short row); rejected and left unread.
        "08_bad_csv.eml": _build_mail(
            message_id="<bad-csv@station.example>",
            subject="Daily water level (bad row)",
            body=b"report,station,ts,level\nHY2026110104,S004,not-a-number,10\n",
        ),
    }


def main(dest: str) -> None:
    target = Path(dest)
    target.mkdir(parents=True, exist_ok=True)
    # Keep the seeded "already read" file name recognisable to the mock server.
    for name, data in build_seed_files().items():
        out_name = name
        if name.startswith("07_"):
            out_name = "read_" + name.split("_", 1)[1]
        (target / out_name).write_bytes(data)
        print(f"wrote {target / out_name} ({len(data)} bytes)")


if __name__ == "__main__":
    import sys

    main(sys.argv[1] if len(sys.argv) > 1 else "mock_imap/mails")
