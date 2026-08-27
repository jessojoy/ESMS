"""
classroom_parser.py

Parses the college's classroom-list Excel file into normalized room
records for the ESMS Room model.

Source layout (confirmed by inspecting Classroom_List.xlsx):
  - The sheet contains two independent "column-groups", each a repeating
    cycle of [Block title row] -> [Sl No | Hall No | cctv | Remarks header]
    -> [data rows], for as many blocks as fit in that group:
        Group A: columns A-D  (Main, Chemical, Workshop, Architecture Studio's)
        Group B: columns F-I  (Mechanical)
  - A third table (columns K-M, "Room -> Class" mapping) exists but is
    intentionally ignored: it reflects regular-semester class occupancy,
    not exam capacity, and is out of scope for this system.
  - The source file contains NO capacity/bench data. Every room gets the
    confirmed institutional defaults below; per-room overrides (if ever
    needed) are expected to be entered manually via Django admin.
"""

from dataclasses import dataclass, field
from typing import Any

from openpyxl import load_workbook

# Confirmed defaults (no per-room capacity data exists in the source file)
DEFAULT_CAPACITY = 45
DEFAULT_BENCHES = 15

# (start_column_index) for each independent column-group, 0-based.
# Layout within each group: Sl No, Hall No, cctv, Remarks
COLUMN_GROUPS = [0, 5]


@dataclass
class ParsedRoom:
    room_number: str
    building: str
    capacity: int = DEFAULT_CAPACITY
    benches: int = DEFAULT_BENCHES


@dataclass
class ParseIssue:
    excel_row: int
    column_group: int
    issue_type: str
    detail: str


@dataclass
class ClassroomParseResult:
    rooms: list[ParsedRoom] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)


def _is_header_row(sl_no_cell: Any) -> bool:
    return isinstance(sl_no_cell, str) and sl_no_cell.strip().lower() == "sl no"


def parse_classroom_excel(file_path: str) -> ClassroomParseResult:
    """
    Parse a classroom-list Excel file into normalized Room records.

    Walks each column-group independently, tracking the current block
    name as it descends through block-title rows, header rows, and data
    rows. Table 3 (Room -> Class mapping) is not read at all.
    """
    wb = load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active

    result = ClassroomParseResult()
    seen_room_numbers: dict[str, int] = {}  # room_number -> first Excel row seen
    current_block: dict[int, str | None] = {col: None for col in COLUMN_GROUPS}

    for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        for col in COLUMN_GROUPS:
            sl_no_cell = row[col] if col < len(row) else None
            hall_no_cell = row[col + 1] if col + 1 < len(row) else None
            cctv_cell = row[col + 2] if col + 2 < len(row) else None
            remarks_cell = row[col + 3] if col + 3 < len(row) else None

            row_has_no_data = (
                hall_no_cell is None and cctv_cell is None and remarks_cell is None
            )

            if row_has_no_data:
                if sl_no_cell is None:
                    continue  # genuinely blank row for this group
                if _is_header_row(sl_no_cell):
                    continue
                if isinstance(sl_no_cell, str):
                    # Block title row, e.g. "Main Block", "Chemical Block"
                    current_block[col] = sl_no_cell.strip()
                    continue
                # Numeric Sl No present but Hall No missing entirely
                result.issues.append(
                    ParseIssue(
                        excel_row=row_idx,
                        column_group=col,
                        issue_type="missing_hall_no",
                        detail=f"Sl No {sl_no_cell!r} has no Hall No value.",
                    )
                )
                continue

            if _is_header_row(sl_no_cell):
                continue  # repeated column header row

            # Data row
            if hall_no_cell is None:
                result.issues.append(
                    ParseIssue(
                        excel_row=row_idx,
                        column_group=col,
                        issue_type="missing_hall_no",
                        detail="Row has cctv/remarks data but no Hall No.",
                    )
                )
                continue

            room_number = str(hall_no_cell).strip()
            building = current_block[col] or "UNKNOWN"

            if building == "UNKNOWN":
                result.issues.append(
                    ParseIssue(
                        excel_row=row_idx,
                        column_group=col,
                        issue_type="missing_block",
                        detail=f"Hall No {room_number!r} found before any block title row.",
                    )
                )

            if room_number in seen_room_numbers:
                result.issues.append(
                    ParseIssue(
                        excel_row=row_idx,
                        column_group=col,
                        issue_type="duplicate_room",
                        detail=(
                            f"Room {room_number!r} already seen at row "
                            f"{seen_room_numbers[room_number]}; skipped."
                        ),
                    )
                )
                continue

            seen_room_numbers[room_number] = row_idx
            result.rooms.append(
                ParsedRoom(room_number=room_number, building=building)
            )

    return result