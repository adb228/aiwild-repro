"""
SpreadsheetBench-compatible evaluator for delegation-study episodes.

Implements the upstream value-comparison semantics from
third_party/SpreadsheetBench/evaluation/evaluation.py with these hardening
additions:

* answer_position parser that correctly handles:
  - quoted sheet names containing commas  (e.g. "'b2b, sez, de'!A5:V10")
  - NBSP and other Unicode whitespace in sheet names
  - missing opening single-quote (e.g. "Sheet1'!A1:F14")
  - column-only ranges (e.g. "A:G") resolved against actual sheet dimensions
  - single-cell references (e.g. "B6")
  - malformed, empty, or reversed ranges → evaluator_failure
* all cells counted as individual units (n_units = total cells in answer range)
* zero units (empty / reversed / unparseable range) → evaluator_failure
* missing or unreadable output file → evaluator_failure

Public API
----------
evaluate_task(golden_path, output_path, answer_position, answer_sheet=None)
    → dict  (matches episode_schema.json outcome block, minus output_path /
               evaluator_path which the caller fills in)
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

import openpyxl


# ---------------------------------------------------------------------------
# Value transformation & comparison — exact upstream semantics
# ---------------------------------------------------------------------------

def _datetime_to_float(dt: datetime.datetime) -> float:
    """Convert a datetime to an Excel serial number (1899-12-30 epoch)."""
    excel_epoch = datetime.datetime(1899, 12, 30)
    delta = dt - excel_epoch
    return delta.days + delta.seconds / 86400.0


def _transform(v: Any) -> Any:
    """Normalise a cell value exactly as the upstream evaluation.py does."""
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    if isinstance(v, datetime.time):
        # "HH:MM" string (drop seconds)
        return str(v)[:-3]
    if isinstance(v, datetime.datetime):
        return round(_datetime_to_float(v), 0)
    if isinstance(v, str):
        try:
            return round(float(v), 2)
        except ValueError:
            pass
    return v


def _compare_values(v1: Any, v2: Any) -> bool:
    """Return True iff two cell values are semantically equal under upstream rules."""
    v1 = _transform(v1)
    v2 = _transform(v2)
    # Treat empty string and None as equivalent
    if (v1 == "" and v2 is None) or (v1 is None and v2 == ""):
        return True
    if (v1 == "" and v2 == "") or (v1 is None and v2 is None):
        return True
    # Mismatched types are always unequal
    if type(v1) != type(v2):
        return False
    return v1 == v2


# ---------------------------------------------------------------------------
# Column / row helpers (mirrors upstream col_num2name / col_name2num)
# ---------------------------------------------------------------------------

def _col_name_to_num(name: str) -> int:
    """'A' → 1, 'Z' → 26, 'AA' → 27, …"""
    num = 0
    for ch in name.upper():
        num = num * 26 + (ord(ch) - ord("A") + 1)
    return num


def _col_num_to_name(n: int) -> str:
    """1 → 'A', 26 → 'Z', 27 → 'AA', …"""
    name = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


# ---------------------------------------------------------------------------
# Hardened cell-range parser
# ---------------------------------------------------------------------------

def _normalize_ws(text: str) -> str:
    """Replace NBSP and other Unicode whitespace with regular spaces."""
    return (
        text
        .replace("\xa0", " ")   # NBSP
        .replace("\u202f", " ") # narrow NBSP
        .replace("\u2009", " ") # thin space
        .replace("\u3000", " ") # ideographic space
        .strip()
    )


def _split_on_unquoted_commas(text: str) -> list[str]:
    """
    Split *text* on commas that are not enclosed in single quotes.
    Handles unbalanced / escaped quotes gracefully by toggling on each `'`.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_q = False
    for ch in text:
        if ch == "'":
            in_q = not in_q
            buf.append(ch)
        elif ch == "," and not in_q:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _parse_one_segment(
    segment: str,
    fallback_sheet: str | None,
) -> tuple[str | None, str]:
    """
    Parse a single range segment (after comma-splitting) into
    (sheet_name | None, cell_range_string).

    Handles:
      - "Sheet1!A1:B2"          → ('Sheet1', 'A1:B2')
      - "'My Sheet'!A1:B2"      → ('My Sheet', 'A1:B2')
      - "Sheet1'!A1:B2"         → ('Sheet1', 'A1:B2')   # missing leading quote
      - "'Sheet1!'A1:B2"        → ('Sheet1!', 'A1:B2')  # ! inside quotes (rare)
      - "A1:B2"                 → (fallback_sheet, 'A1:B2')
      - "B6"                    → (fallback_sheet, 'B6')
    """
    segment = _normalize_ws(segment)

    if "!" in segment:
        # Use the FIRST `!` as the sheet/range separator
        bang = segment.index("!")
        raw_sheet = segment[:bang]
        cell_range = segment[bang + 1:]

        # Strip quotes from sheet name (leading/trailing single quotes)
        sheet = raw_sheet.strip().lstrip("'").rstrip("'").strip()

        # Also strip stray quotes from cell_range
        cell_range = cell_range.strip().lstrip("'").rstrip("'").strip()

        return (sheet or fallback_sheet), cell_range
    else:
        return fallback_sheet, segment.strip().lstrip("'").rstrip("'").strip()


class RangeParseError(ValueError):
    """Raised when an answer_position segment cannot be turned into cell addresses."""


def _expand_range(
    cell_range: str,
    ws: openpyxl.worksheet.worksheet.Worksheet,
) -> list[str]:
    """
    Expand *cell_range* into a list of cell addresses like ['A1', 'A2', 'B1', ...].

    Supports:
      - "A1:B3"    standard row/col range
      - "A:B"      column-only (uses worksheet max_row)
      - "A1"       single cell
      - reversed   (end < start) → raises RangeParseError
      - empty      → raises RangeParseError
    """
    cell_range = cell_range.strip()
    if not cell_range:
        raise RangeParseError("empty cell_range string")

    if ":" not in cell_range:
        # Single cell reference – validate it has at least one letter
        col_part = "".join(c for c in cell_range if c.isalpha())
        row_part = "".join(c for c in cell_range if c.isdigit())
        if not col_part:
            raise RangeParseError(f"cannot parse single-cell reference: {cell_range!r}")
        return [cell_range.upper()]

    left, right = cell_range.split(":", 1)
    left = left.strip()
    right = right.strip()

    if not left or not right:
        raise RangeParseError(f"malformed range (empty side): {cell_range!r}")

    # Parse column + row from each side
    def _split_colrow(ref: str) -> tuple[str, str]:
        col = "".join(c for c in ref if c.isalpha())
        row = "".join(c for c in ref if c.isdigit())
        return col, row

    start_col_name, start_row_str = _split_colrow(left)
    end_col_name, end_row_str = _split_colrow(right)

    if not start_col_name or not end_col_name:
        raise RangeParseError(f"cannot extract column letters from: {cell_range!r}")

    start_col = _col_name_to_num(start_col_name)
    end_col = _col_name_to_num(end_col_name)

    # Column-only range (e.g. "A:G") – resolve against worksheet max_row
    if not start_row_str and not end_row_str:
        max_row = ws.max_row or 1
        start_row = 1
        end_row = max_row
    elif not start_row_str or not end_row_str:
        raise RangeParseError(
            f"mixed column-only / full reference not supported: {cell_range!r}"
        )
    else:
        start_row = int(start_row_str)
        end_row = int(end_row_str)

    # Detect reversed / degenerate ranges
    if start_col > end_col:
        raise RangeParseError(
            f"reversed column order in range {cell_range!r}: "
            f"{start_col_name} > {end_col_name}"
        )
    if start_row > end_row:
        raise RangeParseError(
            f"reversed row order in range {cell_range!r}: "
            f"{start_row} > {end_row}"
        )

    cells = [
        f"{_col_num_to_name(c)}{r}"
        for c in range(start_col, end_col + 1)
        for r in range(start_row, end_row + 1)
    ]
    return cells


def _parse_answer_positions(
    answer_position: str,
    fallback_sheet: str | None,
    workbook_gt: openpyxl.Workbook,
) -> list[tuple[str, list[str]]]:
    """
    Parse the full answer_position string into a list of
    (sheet_name, [cell_address, ...]) tuples.

    Returns an empty list if parsing fails at the top level (caller should
    treat that as evaluator_failure).

    Raises RangeParseError for per-segment problems (also → evaluator_failure).
    """
    raw = _normalize_ws(answer_position)
    segments = _split_on_unquoted_commas(raw)
    if not segments:
        raise RangeParseError(f"answer_position produced no segments: {answer_position!r}")

    results: list[tuple[str, list[str]]] = []
    for seg in segments:
        sheet_name, cell_range = _parse_one_segment(seg, fallback_sheet)

        # Resolve sheet name: use fallback_sheet, then first sheet
        if not sheet_name:
            sheet_name = workbook_gt.sheetnames[0]

        if sheet_name not in workbook_gt.sheetnames:
            # Try case-insensitive match
            lower = sheet_name.lower()
            for name in workbook_gt.sheetnames:
                if name.lower() == lower:
                    sheet_name = name
                    break
            else:
                raise RangeParseError(
                    f"sheet {sheet_name!r} not found in golden workbook "
                    f"(available: {workbook_gt.sheetnames})"
                )

        ws_gt = workbook_gt[sheet_name]
        cells = _expand_range(cell_range, ws_gt)
        results.append((sheet_name, cells))

    return results


# ---------------------------------------------------------------------------
# Core evaluation logic
# ---------------------------------------------------------------------------

def _make_failure(msg: str) -> dict[str, Any]:
    """Return an evaluator_failure outcome dict."""
    return {
        "evaluator_status": "evaluator_failure",
        "full_task_success": None,
        "target_unit_pass_rate": None,
        "n_units": 0,
        "error": msg,
    }


def evaluate_task(
    golden_path: Path,
    output_path: Path | None,
    answer_position: str,
    answer_sheet: str | None = None,
) -> dict[str, Any]:
    """
    Compare *output_path* workbook against *golden_path* over *answer_position*.

    Parameters
    ----------
    golden_path:
        Path to the ground-truth (answer) xlsx file.
    output_path:
        Path to the agent-produced xlsx file.  If None or missing, returns
        evaluator_failure.
    answer_position:
        SpreadsheetBench answer_position string (may be multi-range, sheet-
        qualified, column-only, etc.).
    answer_sheet:
        Optional canonical sheet name from dataset.json; used as fallback
        when answer_position does not include a sheet qualifier.

    Returns
    -------
    dict with keys: evaluator_status, full_task_success, target_unit_pass_rate,
    n_units, [error (only on failure)]
    """
    # ---- Guard: golden workbook must exist ----
    if not golden_path.is_file():
        return _make_failure(f"golden file not found: {golden_path}")

    # ---- Guard: output workbook must exist ----
    if output_path is None or not Path(output_path).is_file():
        return _make_failure(
            f"output file not found: {output_path}"
        )

    # ---- Load workbooks ----
    try:
        wb_gt = openpyxl.load_workbook(filename=str(golden_path), data_only=True)
    except Exception as exc:
        return _make_failure(f"cannot open golden workbook: {exc}")

    try:
        wb_out = openpyxl.load_workbook(filename=str(output_path), data_only=True)
    except Exception as exc:
        return _make_failure(f"cannot open output workbook: {exc}")

    # ---- Parse answer_position ----
    try:
        ranges = _parse_answer_positions(answer_position, answer_sheet, wb_gt)
    except RangeParseError as exc:
        return _make_failure(f"answer_position parse error: {exc}")

    total_cells = sum(len(cells) for _, cells in ranges)

    # Zero units → evaluator_failure (validated by schema: ok must have n_units > 0)
    if total_cells == 0:
        return _make_failure(
            f"answer_position resolved to zero cells: {answer_position!r}"
        )

    # ---- Cell-by-cell comparison ----
    passing = 0
    for sheet_name, cells in ranges:
        # Verify sheet exists in output workbook
        if sheet_name not in wb_out.sheetnames:
            # zero passes for all cells in this range
            continue

        ws_gt = wb_gt[sheet_name]
        ws_out = wb_out[sheet_name]

        for addr in cells:
            v_gt = ws_gt[addr].value
            v_out = ws_out[addr].value
            if _compare_values(v_gt, v_out):
                passing += 1

    pass_rate = passing / total_cells
    full_success = 1 if passing == total_cells else 0

    return {
        "evaluator_status": "ok",
        "full_task_success": full_success,
        "target_unit_pass_rate": pass_rate,
        "n_units": total_cells,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("golden", type=Path, help="Ground-truth xlsx file")
    parser.add_argument("output", type=Path, help="Agent-produced xlsx file")
    parser.add_argument(
        "--answer-position", required=True,
        help="answer_position string from dataset.json"
    )
    parser.add_argument(
        "--answer-sheet", default=None,
        help="Fallback sheet name from dataset.json"
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write JSON result to this file (also prints to stdout)"
    )
    args = parser.parse_args(argv)

    result = evaluate_task(
        golden_path=args.golden,
        output_path=args.output,
        answer_position=args.answer_position,
        answer_sheet=args.answer_sheet,
    )
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
    return 0 if result.get("evaluator_status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
