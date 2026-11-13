"""Unit tests for CSV validation."""

from __future__ import annotations

import pytest

from app.csv_parser import CsvValidationError, parse_daily_csv, sha256_hex

HEADER = "日报号,站号,UTC秒,水位\n"


def test_accepts_header_and_three_readings():
    raw = (
        HEADER
        + "HY1,S001,1793491200,1010\n"
        + "HY1,S001,1793494800,1011\n"
        + "HY1,S001,1793498400,1012\n"
    ).encode()
    parsed = parse_daily_csv(raw)
    assert parsed.report_no == "HY1"
    assert parsed.station_no == "S001"
    assert parsed.utc_day == "2026-11-01"
    assert [r.level_mm for r in parsed.readings] == [1010, 1011, 1012]


def test_accepts_headerless_file_in_canonical_order():
    raw = b"HY2,S002,1793577600,2050\nHY2,S002,1793581200,2051\n"
    parsed = parse_daily_csv(raw)
    assert parsed.report_no == "HY2"
    assert len(parsed.readings) == 2


def test_accepts_english_header_aliases_reordered():
    raw = b"level,ts,station,report\n10,1793491200,S1,R1\n11,1793494800,S1,R1\n"
    parsed = parse_daily_csv(raw)
    assert parsed.report_no == "R1"
    assert parsed.readings[0].level_mm == 10


def test_accepts_crlf_and_bom():
    raw = (
        "﻿"
        + HEADER.replace("\n", "\r\n")
        + "HY1,S001,1793491200,5\r\n"
    ).encode("utf-8")
    parsed = parse_daily_csv(raw)
    assert parsed.report_no == "HY1"


def test_rejects_multiple_report_numbers():
    raw = HEADER + "HY1,S001,1793491200,1\nHY2,S001,1793494800,2\n"
    with pytest.raises(CsvValidationError, match="multiple 日报号"):
        parse_daily_csv(raw.encode())


def test_rejects_multiple_stations():
    raw = HEADER + "HY1,S001,1793491200,1\nHY1,S002,1793494800,2\n"
    with pytest.raises(CsvValidationError, match="multiple 站号"):
        parse_daily_csv(raw.encode())


def test_rejects_multiple_utc_days():
    raw = HEADER + "HY1,S001,1793491200,1\nHY1,S001,1793581200,2\n"
    with pytest.raises(CsvValidationError, match="multiple UTC days"):
        parse_daily_csv(raw.encode())


def test_rejects_non_integer_level():
    raw = HEADER + "HY1,S001,1793491200,1.5\n"
    with pytest.raises(CsvValidationError, match="水位 must be an integer"):
        parse_daily_csv(raw.encode())


def test_rejects_non_integer_timestamp():
    raw = HEADER + "HY1,S001,2026-11-01T00:00:00Z,10\n"
    with pytest.raises(CsvValidationError, match="UTC秒 must be an integer"):
        parse_daily_csv(raw.encode())


def test_rejects_duplicate_seconds():
    raw = HEADER + "HY1,S001,1793491200,1\nHY1,S001,1793491200,2\n"
    with pytest.raises(CsvValidationError, match="duplicate UTC秒"):
        parse_daily_csv(raw.encode())


def test_rejects_wrong_column_count():
    raw = HEADER + "HY1,S001,1793491200\n"
    with pytest.raises(CsvValidationError, match="exactly 4 columns"):
        parse_daily_csv(raw.encode())


def test_rejects_empty_attachment():
    with pytest.raises(CsvValidationError, match="empty"):
        parse_daily_csv(b"   \n")


def test_rejects_non_utf8():
    with pytest.raises(CsvValidationError, match="UTF-8"):
        parse_daily_csv(b"R,S,1,2\n\xff\xfe\x00\n")


def test_rejects_header_only_file():
    with pytest.raises(CsvValidationError, match="no readings"):
        parse_daily_csv(HEADER.encode())


def test_enforces_expected_metadata():
    raw = (HEADER + "HY1,S001,1793491200,1\n").encode()
    with pytest.raises(CsvValidationError, match="日报号"):
        parse_daily_csv(raw, expected_report_no="HY9")
    with pytest.raises(CsvValidationError, match="站号"):
        parse_daily_csv(raw, expected_station_no="S9")
    with pytest.raises(CsvValidationError, match="UTC day"):
        parse_daily_csv(raw, expected_utc_day="2026-11-02")


def test_enforces_reading_limit():
    rows = HEADER + "".join(
        f"HY1,S001,{1793491200 + i},1\n" for i in range(5)
    )
    with pytest.raises(CsvValidationError, match="reading limit"):
        parse_daily_csv(rows.encode(), max_readings=4)


def test_sha256_is_stable():
    assert sha256_hex(b"abc") == sha256_hex(b"abc")
