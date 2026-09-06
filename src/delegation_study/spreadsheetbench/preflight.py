"""Executable evaluator smoke preflight gate.

Runs three evaluator checks against fixed data/dev fixtures BEFORE any
experiment agent is constructed or any API call is made.  If any check
fails, raises ``PreflightError`` with a descriptive message.

Control semantics required to pass
-----------------------------------
positive
    evaluator_status == "ok"
    full_task_success == 1
    target_unit_pass_rate == 1.0
    n_units > 0
    stored JSON must match live run exactly on evaluator_status / n_units / scores

negative
    evaluator_status == "ok"
    full_task_success == 0
    n_units == positive.n_units              (same workbook; no unit drift)
    target_unit_pass_rate == (n_units - 1) / n_units  (exactly one cell wrong)
    stored JSON must match live run on evaluator_status / n_units / scores
    provenance fields in stored JSON:
        source_answer_sha256  — matches SHA-256 of the golden fixture
        mutated_coordinate    — non-empty string naming the changed cell
        n_failed_units == 1   — exactly one unit failed

empty_range
    evaluator_status == "evaluator_failure"
    full_task_success is None
    target_unit_pass_rate is None
    n_units == 0

All three controls are re-executed live (not just reading cached JSON) so
the evaluator, fixtures, and stored results stay provably consistent.

Usage
-----
    from delegation_study.spreadsheetbench.preflight import run_preflight
    run_preflight()   # raises PreflightError on any failure
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..evaluator import evaluate_task

# ---------------------------------------------------------------------------
# Default fixture paths (relative to project root)
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[3]  # …/Submission/
_DEV_DEFAULT = _ROOT / "data" / "dev"

# Task-79-7 answer position (positive + negative share this)
_ANSWER_POSITION = "'Sheet1'!A1:A4"
_ANSWER_SHEET = "Sheet1"

# Reversed range triggers evaluator_failure
_EMPTY_ANSWER_POSITION = "'Sheet1'!A4:A1"


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class PreflightError(RuntimeError):
    """Raised when the evaluator smoke preflight does not pass."""


# ---------------------------------------------------------------------------
# SHA-256 helper
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Individual gate functions (all take explicit paths, no module-level globals)
# ---------------------------------------------------------------------------

def _check_positive(
    golden: Path,
    output_positive: Path,
    stored: dict[str, Any],
) -> tuple[list[str], int]:
    """Run positive control; return (failures, live_n_units)."""
    failures: list[str] = []

    live = evaluate_task(
        golden_path=golden,
        output_path=output_positive,
        answer_position=_ANSWER_POSITION,
        answer_sheet=_ANSWER_SHEET,
    )

    # Required live semantics
    if live["evaluator_status"] != "ok":
        failures.append(
            f"positive: live evaluator_status={live['evaluator_status']!r}, want 'ok'"
        )
    if live.get("full_task_success") != 1:
        failures.append(
            f"positive: live full_task_success={live.get('full_task_success')!r}, want 1"
        )
    if live.get("target_unit_pass_rate") != 1.0:
        failures.append(
            f"positive: live target_unit_pass_rate="
            f"{live.get('target_unit_pass_rate')!r}, want 1.0"
        )
    n_units = live.get("n_units", 0)
    if n_units <= 0:
        failures.append(f"positive: live n_units={n_units}, want >0")

    # Stored must match live (fail closed if someone edits stored without re-running)
    for field in ("evaluator_status", "full_task_success", "target_unit_pass_rate", "n_units"):
        if stored.get(field) != live.get(field):
            failures.append(
                f"positive: stored {field}={stored.get(field)!r} "
                f"!= live {live.get(field)!r} (stored result is stale)"
            )

    return failures, n_units


def _check_negative(
    golden: Path,
    output_negative: Path,
    stored: dict[str, Any],
    positive_n_units: int,
) -> list[str]:
    """Run negative control; return failures list."""
    failures: list[str] = []

    live = evaluate_task(
        golden_path=golden,
        output_path=output_negative,
        answer_position=_ANSWER_POSITION,
        answer_sheet=_ANSWER_SHEET,
    )

    # Required live semantics
    if live["evaluator_status"] != "ok":
        failures.append(
            f"negative: live evaluator_status={live['evaluator_status']!r}, want 'ok'"
        )
    if live.get("full_task_success") != 0:
        failures.append(
            f"negative: live full_task_success={live.get('full_task_success')!r}, want 0"
        )

    n_units = live.get("n_units", 0)

    # n_units must agree with positive (no workbook drift between controls)
    if n_units != positive_n_units:
        failures.append(
            f"negative: live n_units={n_units} != positive n_units={positive_n_units} "
            "(unit count drifted — negative control must use a copy of the answer workbook)"
        )
    elif n_units > 0:
        # Rate must be exactly (n_units - 1) / n_units — one cell, no more, no less
        expected_rate = (n_units - 1) / n_units
        actual_rate = live.get("target_unit_pass_rate")
        if actual_rate != expected_rate:
            failures.append(
                f"negative: live target_unit_pass_rate={actual_rate!r}, "
                f"want exactly (n_units-1)/n_units = {expected_rate!r} "
                "(exactly one cell must differ between negative output and golden)"
            )

    # Stored must match live scores
    for field in ("evaluator_status", "full_task_success", "target_unit_pass_rate", "n_units"):
        if stored.get(field) != live.get(field):
            failures.append(
                f"negative: stored {field}={stored.get(field)!r} "
                f"!= live {live.get(field)!r} (stored result is stale)"
            )

    # ── Provenance checks ──────────────────────────────────────────────────

    # source_answer_sha256 must be present and match the golden fixture byte-for-byte
    src_sha = stored.get("source_answer_sha256")
    if not src_sha:
        failures.append(
            "negative: stored result missing 'source_answer_sha256' provenance field "
            "(negative output must be a copy of the answer workbook before mutation)"
        )
    else:
        actual_sha = _sha256(golden)
        if src_sha != actual_sha:
            failures.append(
                f"negative: source_answer_sha256 in stored result "
                f"({src_sha[:16]}…) does not match SHA-256 of golden fixture "
                f"({actual_sha[:16]}…) — re-generate the control from the current golden"
            )

    # mutated_coordinate must be a non-empty string
    coord = stored.get("mutated_coordinate")
    if not coord or not isinstance(coord, str) or not coord.strip():
        failures.append(
            "negative: stored result missing or empty 'mutated_coordinate' provenance field "
            "(must name exactly which cell was changed, e.g. 'Sheet1!A1')"
        )

    # n_failed_units must be exactly 1
    n_failed = stored.get("n_failed_units")
    if n_failed is None:
        failures.append(
            "negative: stored result missing 'n_failed_units' provenance field"
        )
    elif n_failed != 1:
        failures.append(
            f"negative: stored n_failed_units={n_failed}, want exactly 1 "
            "(negative control must mutate exactly one cell)"
        )

    return failures


def _check_empty_range(
    golden: Path,
    output_positive: Path,
    stored: dict[str, Any],
) -> list[str]:
    """Run empty-range control; return failures list."""
    failures: list[str] = []

    live = evaluate_task(
        golden_path=golden,
        output_path=output_positive,  # output content is irrelevant; range is malformed
        answer_position=_EMPTY_ANSWER_POSITION,
        answer_sheet=_ANSWER_SHEET,
    )

    # Required live semantics
    if live["evaluator_status"] != "evaluator_failure":
        failures.append(
            f"empty_range: live evaluator_status={live['evaluator_status']!r}, "
            "want 'evaluator_failure'"
        )
    if live.get("full_task_success") is not None:
        failures.append(
            f"empty_range: live full_task_success={live.get('full_task_success')!r}, "
            "want null/None"
        )
    if live.get("target_unit_pass_rate") is not None:
        failures.append(
            f"empty_range: live target_unit_pass_rate="
            f"{live.get('target_unit_pass_rate')!r}, want null/None"
        )
    if live.get("n_units") != 0:
        failures.append(
            f"empty_range: live n_units={live.get('n_units')}, want 0"
        )

    # Stored must agree on failure status
    if stored.get("evaluator_status") != "evaluator_failure":
        failures.append(
            f"empty_range: stored evaluator_status={stored.get('evaluator_status')!r}, "
            "want 'evaluator_failure'"
        )
    if stored.get("n_units") != 0:
        failures.append(
            f"empty_range: stored n_units={stored.get('n_units')}, want 0"
        )

    return failures


# ---------------------------------------------------------------------------
# Public gate function
# ---------------------------------------------------------------------------

def run_preflight(*, dev_dir: Path | None = None) -> None:
    """Execute all three evaluator smoke controls.

    Parameters
    ----------
    dev_dir:
        Override the data/dev directory.  When None, uses the project-standard
        ``data/dev/`` path derived from this file's location.

    Raises
    ------
    PreflightError
        If any fixture is missing, any live result doesn't match the required
        semantics, or any provenance field is absent or incorrect.
    """
    base = dev_dir if dev_dir is not None else _DEV_DEFAULT

    golden = base / "smoke_79-7_golden_recalc.xlsx"
    output_positive = base / "smoke_79-7_output_positive.xlsx"
    output_negative = base / "smoke_79-7_output_negative.xlsx"
    stored_positive_path = base / "evaluator_positive.json"
    stored_negative_path = base / "evaluator_negative.json"
    stored_empty_path = base / "evaluator_empty_range.json"

    # Verify all fixture files exist before running anything
    missing = [
        str(p) for p in [
            golden, output_positive, output_negative,
            stored_positive_path, stored_negative_path, stored_empty_path,
        ]
        if not p.is_file()
    ]
    if missing:
        raise PreflightError(
            f"Preflight fixtures missing: {missing}\n"
            "Ensure data/dev/ smoke files are present before running."
        )

    try:
        stored_pos = json.loads(stored_positive_path.read_text())
        stored_neg = json.loads(stored_negative_path.read_text())
        stored_empty = json.loads(stored_empty_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(
            f"Cannot read stored evaluator control JSON: {exc}"
        ) from exc

    all_failures: list[str] = []

    # 1. Positive control
    pos_failures, positive_n_units = _check_positive(golden, output_positive, stored_pos)
    all_failures.extend(pos_failures)

    # 2. Negative control + provenance
    neg_failures = _check_negative(golden, output_negative, stored_neg, positive_n_units)
    all_failures.extend(neg_failures)

    # 3. Empty-range control
    empty_failures = _check_empty_range(golden, output_positive, stored_empty)
    all_failures.extend(empty_failures)

    if all_failures:
        lines = "\n  ".join(all_failures)
        raise PreflightError(
            f"Evaluator smoke preflight FAILED ({len(all_failures)} issue(s)):\n  {lines}"
        )
