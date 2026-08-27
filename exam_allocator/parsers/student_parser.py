"""
PDF student-list extractor.

Extracts class metadata and the student roster from a department-generated
student-list PDF (TKM College of Engineering format), using PyMuPDF's
table detection (page.find_tables()).

This module is pure extraction + light structural normalization. It does
not touch the database and does not silently discard or "fix" bad data --
row-level problems (missing values, duplicate roll numbers, malformed rows)
are collected in `ExtractionResult.issues` for the caller (validation/
service layer) to act on. Only a structural failure -- the PDF not matching
the expected table layout at all -- raises PDFExtractionError, since that
means the file isn't parseable by this format handler, not that a value is
merely missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf


class PDFExtractionError(Exception):
    """Raised when the PDF's structure doesn't match the expected student-list format."""


# Roster header cells, in order, exactly as they appear in the source PDF.
ROSTER_HEADER = ["Sl.No.", "Admission No", "Roll No", "Uni Reg No", "Name", "Gender"]
ROSTER_COLUMN_COUNT = len(ROSTER_HEADER)

# Normalized PDF metadata label -> internal key.
METADATA_KEY_MAP = {
    "department name": "department_name",
    "class name": "class_name",
    "course duration": "course_duration",
    "semester duration": "semester_duration",
    "current semester": "current_semester",
    "students count": "students_count",
    "male count": "male_count",
    "female count": "female_count",
    "faculty advisor": "faculty_advisor",
}


@dataclass
class StudentRecord:
    sl_no: str
    admission_no: str
    roll_number: str
    uni_reg_no: str
    name: str


@dataclass
class ExtractionResult:
    source_file: str
    metadata: dict
    students: list[StudentRecord]
    issues: list[str] = field(default_factory=list)


def extract_student_list(pdf_path: str) -> ExtractionResult:
    """Extract class metadata and student roster from a single PDF file."""
    path = Path(pdf_path)
    if not path.exists():
        raise PDFExtractionError(f"File not found: {pdf_path}")

    doc = pymupdf.open(str(path))
    try:
        metadata = _extract_metadata(doc, path)
        students, issues = _extract_roster(doc, path)
    finally:
        doc.close()

    return ExtractionResult(
        source_file=path.name,
        metadata=metadata,
        students=students,
        issues=issues,
    )


def _extract_metadata(doc: "pymupdf.Document", path: Path) -> dict:
    first_page = doc[0]
    tables = first_page.find_tables()

    for table in tables.tables:
        if table.col_count == 2:
            return _normalize_metadata(table.extract())

    raise PDFExtractionError(
        f"{path.name}: could not locate the metadata table "
        "(expected a 2-column key/value table on page 1)."
    )


def _normalize_metadata(rows: list[list[str]]) -> dict:
    metadata = {}
    for row in rows:
        if len(row) != 2:
            continue
        label, value = row
        key = METADATA_KEY_MAP.get((label or "").strip().lower())
        if key is None:
            continue
        metadata[key] = (value or "").replace("\n", " ").strip()
    return metadata


def _extract_roster(
    doc: "pymupdf.Document", path: Path
) -> tuple[list[StudentRecord], list[str]]:
    issues: list[str] = []
    raw_rows: list[list[str]] = []
    header_seen = False

    for page in doc:
        tables = page.find_tables()
        for table in tables.tables:
            if table.col_count != ROSTER_COLUMN_COUNT:
                continue

            rows = table.extract()
            if not rows:
                continue

            if _looks_like_header(rows[0]):
                header_seen = True
                raw_rows.extend(rows[1:])
            else:
                # Continuation table from a page break -- no header repeated.
                raw_rows.extend(rows)

    if not header_seen:
        raise PDFExtractionError(
            f"{path.name}: could not locate the student roster header "
            f"(expected columns: {', '.join(ROSTER_HEADER)}). "
            "This PDF may use an unsupported format."
        )

    students, row_issues = _normalize_roster_rows(raw_rows)
    issues.extend(row_issues)
    return students, issues


def _looks_like_header(row: list[str]) -> bool:
    normalized = [(cell or "").strip() for cell in row]
    return normalized == ROSTER_HEADER


def _normalize_roster_rows(
    rows: list[list[str]],
) -> tuple[list[StudentRecord], list[str]]:
    students: list[StudentRecord] = []
    issues: list[str] = []
    seen_roll_numbers: dict[str, int] = {}

    for row_index, row in enumerate(rows, start=1):
        if len(row) != ROSTER_COLUMN_COUNT:
            issues.append(
                f"Row {row_index}: expected {ROSTER_COLUMN_COUNT} columns, got {len(row)}. Skipped."
            )
            continue

        sl_no, admission_no, roll_no, uni_reg_no, name, _gender = (
            (cell or "").strip() for cell in row
        )

        if not roll_no:
            issues.append(
                f"Row {row_index} ({name or 'unnamed'}): missing Roll No. Record kept, needs review."
            )
        if not name:
            issues.append(
                f"Row {row_index} (Roll No {roll_no or 'unknown'}): missing Name. Record kept, needs review."
            )

        if roll_no:
            if roll_no in seen_roll_numbers:
                issues.append(
                    f"Row {row_index}: duplicate Roll No '{roll_no}' "
                    f"(first seen at row {seen_roll_numbers[roll_no]})."
                )
            else:
                seen_roll_numbers[roll_no] = row_index

        students.append(
            StudentRecord(
                sl_no=sl_no,
                admission_no=admission_no,
                roll_number=roll_no,
                uni_reg_no=uni_reg_no,
                name=name,
            )
        )

    return students, issues
