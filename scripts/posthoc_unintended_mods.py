#!/usr/bin/env python3
"""Audit real final scripts for unintended-modification risk.

Canonical episodes do not retain the initial workbook or answer-position
metadata needed for an exact workbook diff. This audit therefore computes a
conservative static risk flag from each sealed final script: structural sheet
operations and broad table/workbook replacement calls are flagged, while
ordinary cell writes are counted as evidence but are not presumed unintended.
No hidden, raw, initial, or golden workbook is read.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from delegation_study.analysis.analyze import (  # noqa: E402
    AnalysisInputError,
    load_real_records,
)

_ALL_CONDITIONS = ("A0", "A1", "P0", "P1", "SOLO")
_METHOD = "static-python-mutation-risk-audit-v1"
_STRUCTURAL_CALLS = frozenset({
    "create_sheet",
    "remove",
    "remove_sheet",
    "delete_rows",
    "delete_cols",
    "insert_rows",
    "insert_cols",
    "move_range",
    "merge_cells",
    "unmerge_cells",
})
_BROAD_REPLACEMENT_CALLS = frozenset({
    "append",
    "to_excel",
    "from_dataframe",
})
_STYLE_ATTRIBUTES = frozenset({
    "alignment",
    "border",
    "fill",
    "font",
    "number_format",
    "protection",
    "style",
})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _attribute_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _workbook_receiver(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    receiver = call.func.value
    if isinstance(receiver, ast.Name):
        lowered = receiver.id.lower()
        return (
            lowered in {"wb", "ws", "sheet", "worksheet", "workbook"}
            or lowered.startswith(("ws_", "sheet_"))
        )
    if isinstance(receiver, ast.Attribute):
        return receiver.attr.lower() in {"active", "worksheets", "sheets"}
    if isinstance(receiver, ast.Subscript):
        return True
    return False


def audit_script(script: bytes) -> dict[str, Any]:
    """Count mutation sites without retaining cell addresses or values."""
    try:
        tree = ast.parse(script.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return {
            "status": "parse_error",
            "flagged": None,
            "reason": f"script_parse_error:{type(exc).__name__}",
            "cell_write_sites": 0,
            "style_write_sites": 0,
            "structural_mutation_sites": 0,
            "broad_replacement_sites": 0,
        }

    cell_writes = 0
    style_writes = 0
    structural = 0
    broad = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    if target.attr == "value":
                        cell_writes += 1
                    elif target.attr in _STYLE_ATTRIBUTES:
                        style_writes += 1
                elif isinstance(target, ast.Subscript):
                    cell_writes += 1
        elif isinstance(node, ast.Call):
            name = _attribute_name(node)
            if name in _STRUCTURAL_CALLS and _workbook_receiver(node):
                structural += 1
            elif name in {"to_excel", "from_dataframe"}:
                broad += 1
            elif name == "append" and _workbook_receiver(node):
                broad += 1
            elif name == "cell" and any(
                keyword.arg == "value" for keyword in node.keywords
            ):
                cell_writes += 1

    flagged = structural > 0 or broad > 0
    if structural:
        reason = "structural_workbook_mutation_detected"
    elif broad:
        reason = "broad_workbook_replacement_detected"
    elif cell_writes or style_writes:
        reason = "localized_mutations_only"
    else:
        reason = "no_static_workbook_mutation_detected"
    return {
        "status": "evaluated",
        "flagged": flagged,
        "reason": reason,
        "cell_write_sites": cell_writes,
        "style_write_sites": style_writes,
        "structural_mutation_sites": structural,
        "broad_replacement_sites": broad,
    }


def _empty_counts() -> dict[str, int]:
    return {"flagged": 0, "not_flagged": 0, "unavailable": 0}


def run_audit(
    runs_dir: Path,
    task_set_scope: str,
    sidecars_dir: Path | None,
) -> dict[str, Any]:
    """Audit every selected record and (optionally) write safe per-run sidecars.

    When *sidecars_dir* is None the per-run sidecar JSON files are not written;
    the aggregate count summary is still returned (and written via ``--out``).
    This is the preferred mode when the caller only needs aggregate counts and
    wants to avoid creating any new files in an external directory.
    """
    records = load_real_records(runs_dir, task_set_scope)
    runs_resolved = runs_dir.resolve()
    if sidecars_dir is not None:
        sidecars_resolved = sidecars_dir.resolve()
        if sidecars_resolved == runs_resolved or runs_resolved in sidecars_resolved.parents:
            raise AnalysisInputError("sidecars directory must be outside the runs directory")
        sidecars_dir.mkdir(parents=True, exist_ok=True)

    by_task_set: dict[str, dict[str, dict[str, int]]] = {}
    unreadable_runs: list[str] = []
    for record in records:
        run_id = str(record["run_id"])
        task_set = str(record["task_set"])
        condition = str(record["condition"])
        script_path = runs_dir / run_id / "final" / "solution.py"
        if script_path.is_file():
            script = script_path.read_bytes()
            audit = audit_script(script)
            script_sha256: str | None = _sha256(script)
        else:
            audit = {
                "status": "unavailable",
                "flagged": None,
                "reason": "sealed_final_script_missing",
                "cell_write_sites": 0,
                "style_write_sites": 0,
                "structural_mutation_sites": 0,
                "broad_replacement_sites": 0,
            }
            script_sha256 = None

        evidence = {
            "script_sha256": script_sha256,
            "episode_sha256": record["_source_episode_sha256"],
            "cell_write_sites": audit["cell_write_sites"],
            "style_write_sites": audit["style_write_sites"],
            "structural_mutation_sites": audit["structural_mutation_sites"],
            "broad_replacement_sites": audit["broad_replacement_sites"],
            "reason": audit["reason"],
        }
        evidence_sha256 = _sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        )
        sidecar = {
            "analysis": "unintended_modifications",
            "method": _METHOD,
            "run_id": run_id,
            "task_id": str(record["task_id"]),
            "task_set": task_set,
            "condition": condition,
            "status": audit["status"],
            "flagged": audit["flagged"],
            "reason": audit["reason"],
            "evidence": evidence,
            "evidence_sha256": evidence_sha256,
            "golden_values_accessed": False,
            "limitation": (
                "Static sealed-script risk audit; exact outside-answer workbook "
                "diffs cannot be reconstructed because canonical run artifacts "
                "do not retain the initial workbook and answer-position pair."
            ),
        }
        if audit["status"] == "unavailable":
            unreadable_runs.append(run_id)

        if sidecars_dir is not None:
            sidecar_path = sidecars_dir / f"{run_id}.json"
            sidecar_path.write_text(
                json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        bucket = by_task_set.setdefault(
            task_set, {cond: _empty_counts() for cond in _ALL_CONDITIONS}
        )[condition]
        if audit["flagged"] is True:
            bucket["flagged"] += 1
        elif audit["flagged"] is False:
            bucket["not_flagged"] += 1
        else:
            bucket["unavailable"] += 1

    totals = _empty_counts()
    for conditions in by_task_set.values():
        for counts in conditions.values():
            for key in totals:
                totals[key] += counts[key]
    n_sidecars = sum(
        1
        for ts_counts in by_task_set.values()
        for cond_counts in ts_counts.values()
        for _ in [None]  # count episodes, not buckets
    ) if sidecars_dir is None else len(records)
    return {
        "analysis": "unintended_modifications",
        "method": _METHOD,
        "task_set_scope": task_set_scope,
        "n_episodes": len(records),
        "totals": totals,
        "by_task_set": by_task_set,
        "sidecars_dir": str(sidecars_dir) if sidecars_dir is not None else None,
        "n_sidecars": len(records) if sidecars_dir is not None else 0,
        "unreadable_runs": unreadable_runs,
        "n_unreadable": len(unreadable_runs),
        "golden_values_accessed": False,
    }


def count_unintended_mod_flags(
    records: list[dict[str, Any]],
    threshold: int = 1,
) -> dict[str, Any]:
    """Compatibility counter for already-materialized evaluator fields."""
    by_condition: dict[str, dict[str, int]] = {
        cond: {"flagged": 0, "clean": 0, "no_flag_field": 0}
        for cond in _ALL_CONDITIONS
    }
    for record in records:
        bucket = by_condition.setdefault(
            str(record.get("condition")),
            {"flagged": 0, "clean": 0, "no_flag_field": 0},
        )
        outcome = record.get("final_outcome") or {}
        if "n_unintended_modifications" not in outcome:
            bucket["no_flag_field"] += 1
        elif int(outcome["n_unintended_modifications"]) >= threshold:
            bucket["flagged"] += 1
        else:
            bucket["clean"] += 1
    by_condition = {
        condition: counts
        for condition, counts in by_condition.items()
        if sum(counts.values())
    }
    return {
        "threshold": threshold,
        "n_episodes": len(records),
        "n_flagged": sum(v["flagged"] for v in by_condition.values()),
        "n_clean": sum(v["clean"] for v in by_condition.values()),
        "n_no_flag_field": sum(v["no_flag_field"] for v in by_condition.values()),
        "by_condition": by_condition,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument(
        "--task-set",
        choices=("main", "all", "main_v04", "solo_v04"),
        required=True,
        dest="task_set_scope",
        help=(
            "Task-set scope to audit. 'main'/'all' target v0.3 records; "
            "'main_v04'/'solo_v04' target v0.4.1 records. "
            "All choices read runs read-only from --runs-dir."
        ),
    )
    parser.add_argument(
        "--sidecars-dir",
        type=Path,
        default=None,
        required=False,
        help=(
            "Directory to write per-run sidecar JSON files. Must be outside "
            "--runs-dir. If omitted, no per-run sidecars are written; only the "
            "aggregate summary (--out) is produced. This is the recommended "
            "read-only report mode."
        ),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_audit(
            args.runs_dir, args.task_set_scope, args.sidecars_dir
        )
    except (AnalysisInputError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
