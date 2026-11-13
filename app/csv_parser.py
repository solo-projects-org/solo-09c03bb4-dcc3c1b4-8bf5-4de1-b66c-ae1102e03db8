"""Strict parsing/validation of the daily-water-level CSV attachment.

Business rules enforced here:
  * UTF-8 (optionally with BOM), CSV delimited by commas, header optional.
  * Exactly four columns: report number, station number, UTC epoch second,
    water level in integer millimetres.
  * One daily report contains exactly one report number, one station and
    readings from a single UTC day. Report number + station must agree with
    the mail envelope used by the caller.
  * UTC second must be a whole second that physically exists and falls in
    the declared UTC day.
  * Water level is an integer (millimetres); one reading per UTC second.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

HEADER_ALIASES = {
    "report": {"日报号", "report_no", "report", "report_number", "report_id", "daily_report_no"},
    "station": {"站号", "station", "station_no", "station_id"},
    "ts": {
        "utc秒",
        "utc秒数",
        "utc_seconds",
        "utc_second",
        "utc_epoch_seconds",
        "timestamp",
        "ts",
        "time",
        "utc_time",
    },
    "level": {"水位", "level", "water_level", "stage", "water_level_mm", "level_mm"},
}

REQUIRED_FIELDS = ("report", "station", "ts", "level")


class CsvValidationError(ValueError):
    """The attachment is not a valid daily water-level CSV."""

    def __init__(self, message: str, *, row: int | None = None):
        self.row = row
        super().__init__(message if row is None else f"row {row}: {message}")


@dataclass(frozen=True)
class Reading:
    report_no: str
    station_no: str
    utc_seconds: int
    level_mm: int
    row_number: int


@dataclass(frozen=True)
class ParsedCsv:
    report_no: str
    station_no: str
    utc_day: str  # YYYY-MM-DD
    readings: tuple[Reading, ...]


def _normalise_name(value: str) -> str:
    return value.strip().lstrip("﻿").strip().lower()


def _identify_header(cells: Sequence[str]) -> list[str] | None:
    """Return the canonical field order if `cells` is a recognised header row."""
    canonical = [_normalise_name(c) for c in cells]
    matched: list[str] = []
    for cell in canonical:
        for field, aliases in HEADER_ALIASES.items():
            if cell in aliases:
                matched.append(field)
                break
        else:
            return None
    if len(matched) == 4 and set(matched) == set(REQUIRED_FIELDS):
        return matched
    return None


def _decode(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvValidationError(f"attachment is not valid UTF-8: {exc}") from exc
    if "\x00" in text:
        raise CsvValidationError("attachment contains NUL bytes; expected a text CSV")
    return text


def _clean_cell(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def _parse_int(field: str, value: str, *, row: int) -> int:
    if value == "":
        raise CsvValidationError(f"{field} is empty", row=row)
    try:
        return int(value, 10)
    except ValueError as exc:
        raise CsvValidationError(
            f"{field} must be an integer (got {value!r})", row=row
        ) from exc


def parse_daily_csv(
    raw: bytes,
    *,
    expected_report_no: str | None = None,
    expected_station_no: str | None = None,
    expected_utc_day: str | None = None,
    max_readings: int = 100_000,
) -> ParsedCsv:
    """Parse and fully validate an attachment.

    The whole file is validated before anything is returned, so a caller can
    persist only when this function succeeds.
    """

    if not raw or not raw.strip():
        raise CsvValidationError("attachment is empty")

    text = _decode(raw)
    reader = csv.reader(io.StringIO(text, newline=""))

    report_no: str | None = None
    station_no: str | None = None
    utc_day: str | None = None
    readings: list[Reading] = []
    seen_seconds: dict[int, int] = {}
    data_rows = 0
    header_checked = False
    columns = list(REQUIRED_FIELDS)

    for line_no, cells in enumerate(reader, start=1):
        # csv.reader yields logical rows; `line_no` is a monotonic row number
        # suitable for diagnostics. Blank rows are skipped.
        if not cells or all(_clean_cell(c) == "" for c in cells):
            continue
        if not header_checked:
            header_checked = True
            header_order = _identify_header(cells)
            if header_order is not None:
                columns = header_order
                continue
        data_rows += 1
        if data_rows > max_readings:
            raise CsvValidationError(
                f"attachment exceeds the {max_readings} reading limit"
            )

        if len(cells) != 4:
            raise CsvValidationError(
                f"expected exactly 4 columns, got {len(cells)}",
                row=line_no,
            )

        row = dict(zip(columns, (_clean_cell(c) for c in cells)))
        row_report = row["report"]
        row_station = row["station"]

        if row_report == "":
            raise CsvValidationError("日报号 is empty", row=line_no)
        if row_station == "":
            raise CsvValidationError("站号 is empty", row=line_no)

        if report_no is None:
            report_no = row_report
        elif row_report != report_no:
            raise CsvValidationError(
                f"multiple 日报号 values in one attachment ({report_no!r} vs {row_report!r})",
                row=line_no,
            )
        if station_no is None:
            station_no = row_station
        elif row_station != station_no:
            raise CsvValidationError(
                f"multiple 站号 values in one attachment ({station_no!r} vs {row_station!r})",
                row=line_no,
            )

        ts = _parse_int("UTC秒", row["ts"], row=line_no)
        if ts < 0:
            raise CsvValidationError("UTC秒 must not be negative", row=line_no)
        try:
            moment = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise CsvValidationError(
                f"UTC秒 {ts} is outside the supported range", row=line_no
            ) from exc
        row_day = moment.strftime("%Y-%m-%d")
        if utc_day is None:
            utc_day = row_day
        elif row_day != utc_day:
            raise CsvValidationError(
                f"readings span multiple UTC days ({utc_day} vs {row_day})",
                row=line_no,
            )

        level = _parse_int("水位", row["level"], row=line_no)
        if ts in seen_seconds:
            raise CsvValidationError(
                f"duplicate UTC秒 {ts} (already on row {seen_seconds[ts]})",
                row=line_no,
            )
        seen_seconds[ts] = line_no
        readings.append(
            Reading(
                report_no=row_report,
                station_no=row_station,
                utc_seconds=ts,
                level_mm=level,
                row_number=line_no,
            )
        )

    if not readings or report_no is None or station_no is None or utc_day is None:
        raise CsvValidationError("attachment contains no readings")

    if expected_report_no is not None and report_no != expected_report_no:
        raise CsvValidationError(
            f"attachment 日报号 {report_no!r} does not match mail metadata {expected_report_no!r}"
        )
    if expected_station_no is not None and station_no != expected_station_no:
        raise CsvValidationError(
            f"attachment 站号 {station_no!r} does not match mail metadata {expected_station_no!r}"
        )
    if expected_utc_day is not None and utc_day != expected_utc_day:
        raise CsvValidationError(
            f"attachment UTC day {utc_day} does not match mail metadata {expected_utc_day}"
        )

    return ParsedCsv(
        report_no=report_no,
        station_no=station_no,
        utc_day=utc_day,
        readings=tuple(readings),
    )


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
