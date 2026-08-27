"""
timetable_parser.py

Extracts exam timetable records from college-generated PDF timetables.

The source layout may vary: exam cards can appear in different positions,
there may be different numbers of cards, and subject names can wrap across
multiple lines. The parser therefore identifies exam-card tables by their
content rather than by fixed row/column positions.

This module is pure extraction + light structural normalization. It does not
access Django models or the database. Missing subject codes and other
row-level problems are reported in ``TimetableParseResult.issues`` instead of
being silently discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
import re
from typing import Any

import pymupdf


class PDFExtractionError(Exception):
    """Raised when the PDF cannot be recognized as an exam timetable."""


@dataclass
class ParsedExam:
    """One normalized examination entry from the timetable."""

    slot: str
    subject_name: str
    subject_code: str | None
    exam_date: date
    session: str
    duration: int


@dataclass
class ParseIssue:
    """A non-fatal problem found while parsing one timetable entry."""

    page_number: int
    table_index: int | None
    issue_type: str
    detail: str


@dataclass
class TimetableParseResult:
    """Complete result returned by ``parse_timetable_pdf``."""

    source_file: str
    metadata: dict[str, Any] = field(default_factory=dict)
    exams: list[ParsedExam] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)


DATE_RE = re.compile(
    r"\b(?:Date\s*:\s*)?(\d{1,2})\s*-\s*(\d{1,2})\s*-{1,2}\s*(\d{4})\b",
    re.IGNORECASE,
)

TIME_RE = re.compile(
    r"Time\s*:\s*(\d{1,2})\s*:\s*(\d{2})\s*([AP]M|Noon)\s*"
    r"(?:to|[-–—])\s*"
    r"(\d{1,2})\s*:\s*(\d{2})\s*([AP]M|Noon)",
    re.IGNORECASE,
)

SLOT_RE = re.compile(r"^\s*([A-Za-z0-9]+)\s*\|\s*(.+?)\s*$", re.DOTALL)

# Subject codes in the supplied timetables look like 23ARS402 / 22ARS803.
# Keep this deliberately broader so future departments/courses can use codes
# containing letters, digits, dots, slashes or hyphens.
SUBJECT_CODE_RE = re.compile(
    r"\(\s*([A-Za-z0-9][A-Za-z0-9._/-]{2,})\s*\)\s*[*†‡]?\s*$"
)

TITLE_RE = re.compile(
    r"\b(?P<course>.+?)\s*-\s*S(?P<semester>\d+)\s*-\s*"
    r"(?P<exam_series>.+?)\s*-\s*(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})"
    r"\s*-\s*TIME\s*TABLE\b",
    re.IGNORECASE,
)


def parse_timetable_pdf(file_path: str) -> TimetableParseResult:
    """Parse an exam timetable PDF into normalized ``ParsedExam`` records."""
    path = Path(file_path)
    if not path.exists():
        raise PDFExtractionError(f"File not found: {file_path}")
    if not path.is_file():
        raise PDFExtractionError(f"Not a file: {file_path}")

    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        raise PDFExtractionError(f"Could not open PDF {path.name}: {exc}") from exc

    try:
        if len(doc) == 0:
            raise PDFExtractionError(f"{path.name}: PDF contains no pages.")

        metadata = _extract_metadata(doc)
        exams: list[ParsedExam] = []
        issues: list[ParseIssue] = []
        card_count = 0

        for page_number, page in enumerate(doc, start=1):
            cards = _find_exam_cards(page)

            for table_index, rows in cards:
                card_count += 1
                exam, card_issues = _parse_card(
                    rows, page_number=page_number, table_index=table_index
                )
                issues.extend(card_issues)
                if exam is not None:
                    exams.append(exam)

        if card_count == 0:
            raise PDFExtractionError(
                f"{path.name}: no exam cards were found. Expected timetable cards "
                "containing Date, Time, and Slot/Subject information."
            )

        # Return records in chronological exam order, with FN before AN.
        exams.sort(
            key=lambda exam: (
                exam.exam_date,
                0 if exam.session == "FN" else 1,
                exam.slot,
            )
        )

        _validate_exams(exams, issues)

        return TimetableParseResult(
            source_file=path.name,
            metadata=metadata,
            exams=exams,
            issues=issues,
        )
    finally:
        doc.close()


def _extract_metadata(doc: "pymupdf.Document") -> dict[str, Any]:
    """Extract useful timetable metadata from the page text when available."""
    text = "\n".join(page.get_text("text") for page in doc)
    metadata: dict[str, Any] = {}

    # Search line-by-line so the parser does not accidentally start the
    # title match at a Date/Time line when PDF text extraction reorders text.
    for line in text.splitlines():
        normalized = " ".join(line.split())
        match = TITLE_RE.search(normalized)
        if match:
            metadata["course"] = match.group("course").strip()
            metadata["semester"] = int(match.group("semester"))
            metadata["exam_series"] = match.group("exam_series").strip()
            metadata["month"] = match.group("month").strip()
            metadata["year"] = int(match.group("year"))
            metadata["title"] = match.group(0).strip()
            break

    if "title" not in metadata:
        # Still expose the most useful raw title-like line if the exact format
        # changes in a future timetable.
        for line in text.splitlines():
            normalized = " ".join(line.split())
            if "TIME TABLE" in normalized.upper() or "TIMETABLE" in normalized.upper():
                metadata["title"] = normalized
                break

    return metadata


def _find_exam_cards(page: "pymupdf.Page") -> list[tuple[int, list[list[str]]]]:
    """Find exam-card tables without relying on their page position.

    Current TKM timetables expose each exam card as a 4x2 table:
      Date | slot number
      Time | FN/AN
      Slot, Subject & Code | ...
      subject text | ...

    Some PDFs also expose one large outer table around all cards. That table
    is intentionally ignored because its cells combine multiple cards.
    """
    try:
        tables = page.find_tables().tables
    except Exception as exc:
        raise PDFExtractionError(
            f"Page {page.number + 1}: could not inspect PDF table structure: {exc}"
        ) from exc

    cards: list[tuple[int, list[list[str]]]] = []
    for table_index, table in enumerate(tables):
        rows = table.extract()
        if not rows:
            continue

        # Normal exam-card table: 4 rows x 2 columns.
        if table.row_count == 4 and table.col_count == 2:
            if _looks_like_exam_card(rows, 0):
                cards.append((table_index, rows))
            continue

        # Some PDF producers expose a larger outer table around a row of two
        # exam cards. For example, a 5x4 outer table can contain the final two
        # cards while the other cards are exposed as separate 4x2 tables.
        # Extract each pair of columns as an independent card, ignoring the
        # outer title/header row when it does not contain Date/Time data.
        if table.col_count == 4 and table.row_count >= 4:
            for pair_start in (0, 2):
                pair_rows = [
                    [
                        row[pair_start] if pair_start < len(row) else None,
                        row[pair_start + 1] if pair_start + 1 < len(row) else None,
                    ]
                    for row in rows
                ]
                # The card starts at the row containing Date.
                for start in range(max(0, len(pair_rows) - 3)):
                    candidate = pair_rows[start : start + 4]
                    if len(candidate) == 4 and _looks_like_exam_card(candidate, 0):
                        cards.append((table_index, candidate))
                        break

    # find_tables normally returns cards in reading order. Sorting by their
    # bounding box gives deterministic ordering even if a PDF producer changes
    # the internal table order.
    if cards:
        indexed_bboxes = {i: tables[i].bbox for i, _ in cards}
        cards.sort(key=lambda item: (indexed_bboxes[item[0]][1], indexed_bboxes[item[0]][0]))

    return cards


def _looks_like_exam_card(rows: list[list[str]], start: int = 0) -> bool:
    """Return True when four rows have the shape of an exam card."""
    if len(rows) < start + 4:
        return False
    first = _cell_text(rows[start][0] if rows[start] else "")
    second = _cell_text(rows[start + 1][0] if rows[start + 1] else "")
    third = _cell_text(rows[start + 2][0] if rows[start + 2] else "")
    return (
        bool(DATE_RE.search(first))
        and bool(TIME_RE.search(second))
        and "slot" in third.lower()
        and "subject" in third.lower()
    )


def _parse_card(
    rows: list[list[str]], *, page_number: int, table_index: int
) -> tuple[ParsedExam | None, list[ParseIssue]]:
    issues: list[ParseIssue] = []

    date_text = _cell_text(rows[0][0]) if len(rows) > 0 else ""
    time_text = _cell_text(rows[1][0]) if len(rows) > 1 else ""
    session_cell = _cell_text(rows[1][1]) if len(rows) > 1 and len(rows[1]) > 1 else ""
    subject_text = _cell_text(rows[3][0]) if len(rows) > 3 else ""

    exam_date = _parse_date(date_text)
    if exam_date is None:
        issues.append(
            ParseIssue(page_number, table_index, "invalid_date", f"Could not parse date: {date_text!r}.")
        )
        return None, issues

    time_match = TIME_RE.search(time_text)
    if not time_match:
        issues.append(
            ParseIssue(page_number, table_index, "invalid_time", f"Could not parse time: {time_text!r}.")
        )
        return None, issues

    start_hour = int(time_match.group(1))
    start_minute = int(time_match.group(2))
    start_ampm = _normalize_ampm(time_match.group(3))
    end_hour = int(time_match.group(4))
    end_minute = int(time_match.group(5))
    end_ampm = _normalize_ampm(time_match.group(6))

    duration = _calculate_duration(
        start_hour, start_minute, start_ampm, end_hour, end_minute, end_ampm
    )
    if duration <= 0:
        issues.append(
            ParseIssue(page_number, table_index, "invalid_duration", f"Calculated duration {duration} minutes from {time_text!r}.")
        )
        return None, issues

    session = _normalize_session(session_cell)
    inferred_session = _infer_session(start_hour, start_ampm)

    if session is None:
        session = inferred_session
        issues.append(
            ParseIssue(
                page_number,
                table_index,
                "session_inferred",
                f"FN/AN was not explicitly present; inferred {session} from the start time.",
            )
        )
    elif session != inferred_session:
        issues.append(
            ParseIssue(
                page_number,
                table_index,
                "session_time_mismatch",
                f"Timetable says {session}, but the start time {start_hour}:{start_minute:02d} {start_ampm} suggests {inferred_session}.",
            )
        )

    slot, subject_name, subject_code = _parse_subject(subject_text)
    if slot is None:
        issues.append(
            ParseIssue(page_number, table_index, "invalid_subject", f"Could not parse slot/subject: {subject_text!r}.")
        )
        return None, issues

    if not subject_code:
        issues.append(
            ParseIssue(
                page_number,
                table_index,
                "missing_subject_code",
                f"Slot {slot!r} / subject {subject_name!r} has no subject code.",
            )
        )

    return (
        ParsedExam(
            slot=slot,
            subject_name=subject_name,
            subject_code=subject_code,
            exam_date=exam_date,
            session=session,
            duration=duration,
        ),
        issues,
    )


def _parse_date(text: str) -> date | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None


def _normalize_ampm(value: str) -> str:
    """Normalize timetable time labels such as ``Noon`` to ``PM``."""
    return "PM" if value.strip().upper() == "NOON" else value.strip().upper()


def _calculate_duration(
    start_hour: int,
    start_minute: int,
    start_ampm: str,
    end_hour: int,
    end_minute: int,
    end_ampm: str,
) -> int:
    start = datetime.strptime(
        f"{start_hour}:{start_minute:02d} {start_ampm}", "%I:%M %p"
    )
    end = datetime.strptime(
        f"{end_hour}:{end_minute:02d} {end_ampm}", "%I:%M %p"
    )
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 0:
        minutes += 24 * 60
    return minutes


def _normalize_session(text: str) -> str | None:
    normalized = re.sub(r"[^A-Za-z]", "", text).upper()
    if normalized == "FN":
        return "FN"
    if normalized == "AN":
        return "AN"
    return None


def _infer_session(hour: int, ampm: str) -> str:
    if ampm.upper() == "AM":
        return "FN"
    # A 12:00 PM or later start is an afternoon/evening session.
    return "AN"


def _parse_subject(text: str) -> tuple[str | None, str, str | None]:
    normalized = " ".join(text.replace("\u00a0", " ").split())
    normalized = normalized.rstrip("*†‡ ")

    slot_match = SLOT_RE.match(normalized)
    if not slot_match:
        return None, "", None

    slot = slot_match.group(1).strip().upper()
    subject_part = slot_match.group(2).strip()

    code_match = SUBJECT_CODE_RE.search(subject_part)
    if code_match:
        subject_code = code_match.group(1).strip()
        subject_name = subject_part[: code_match.start()].strip()
    else:
        subject_code = None
        subject_name = subject_part.rstrip("*†‡ ").strip()

    # Clean whitespace before punctuation introduced by PDF line wrapping.
    subject_name = re.sub(r"\s+([,.;:)])", r"\1", subject_name)
    subject_name = re.sub(r"([(/])\s+", r"\1", subject_name)
    subject_name = re.sub(r"\s{2,}", " ", subject_name).strip()

    return slot, subject_name, subject_code


def _validate_exams(exams: list[ParsedExam], issues: list[ParseIssue]) -> None:
    seen: dict[tuple[date, str, str], int] = {}
    for index, exam in enumerate(exams, start=1):
        key = (exam.exam_date, exam.session, exam.slot)
        if key in seen:
            issues.append(
                ParseIssue(
                    page_number=0,
                    table_index=None,
                    issue_type="duplicate_exam",
                    detail=(
                        f"Duplicate exam for date {exam.exam_date.isoformat()}, "
                        f"session {exam.session}, slot {exam.slot!r}; "
                        f"first seen at parsed record {seen[key]}."
                    ),
                )
            )
        else:
            seen[key] = index


def _cell_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


# Backwards-friendly alias: callers can use either the explicit PDF name or
# the shorter extractor name used by student_parser.py.
extract_timetable = parse_timetable_pdf
