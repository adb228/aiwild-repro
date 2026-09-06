#!/usr/bin/env python3
"""Audit real final scripts for static hard-coding evidence.

This post-hoc audit never reads initial, hidden, raw, or golden workbooks. It
classifies mutation expressions in each sealed ``final/solution.py`` and emits
one sidecar per selected v0.3 run. The result is an artifact-derived static
risk flag, not the appendix's unavailable perturb-and-rerun metric.
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
_METHOD = "static-python-workbook-write-audit-v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _literal_expr(node: ast.AST, literal_names: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_literal_expr(item, literal_names) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is None or _literal_expr(key, literal_names)
            for key in node.keys
        ) and all(_literal_expr(value, literal_names) for value in node.values)
    if isinstance(node, ast.UnaryOp):
        return _literal_expr(node.operand, literal_names)
    if isinstance(node, ast.Name):
        return node.id in literal_names
    return False


def _formula_expr(node: ast.AST, formula_names: set[str]) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.lstrip().startswith("=")
    if isinstance(node, ast.JoinedStr):
        prefix = "".join(
            item.value
            for item in node.values
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
        return prefix.lstrip().startswith("=")
    return isinstance(node, ast.Name) and node.id in formula_names


def _assigned_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for item in target.elts:
            names.update(_assigned_names(item))
        return names
    return set()


def _is_workbook_target(target: ast.AST) -> bool:
    if isinstance(target, ast.Attribute) and target.attr == "value":
        return True
    if isinstance(target, ast.Subscript):
        base = target.value
        if isinstance(base, ast.Name):
            return base.id.lower() in {"ws", "sheet", "worksheet", "cell"}
        if isinstance(base, ast.Attribute):
            return base.attr.lower() in {"active", "worksheets"}
    return False


def _workbook_receiver(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    receiver = call.func.value
    if isinstance(receiver, ast.Name):
        lowered = receiver.id.lower()
        return (
            lowered in {"ws", "sheet", "worksheet"}
            or lowered.startswith(("ws_", "sheet_"))
        )
    return isinstance(receiver, (ast.Attribute, ast.Subscript))


def _collect_name_kinds(tree: ast.AST) -> tuple[set[str], set[str]]:
    assignments: list[tuple[set[str], ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names: set[str] = set()
            for target in targets:
                names.update(_assigned_names(target))
            if names and node.value is not None:
                assignments.append((names, node.value))

    literal_names: set[str] = set()
    formula_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for names, value in assignments:
            if _literal_expr(value, literal_names):
                before = len(literal_names)
                literal_names.update(names)
                changed |= len(literal_names) != before
            if _formula_expr(value, formula_names):
                before = len(formula_names)
                formula_names.update(names)
                changed |= len(formula_names) != before
    return literal_names, formula_names


def audit_script(script: bytes) -> dict[str, Any]:
    """Return static write evidence without retaining source text or values."""
    try:
        tree = ast.parse(script.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return {
            "status": "parse_error",
            "flagged": None,
            "reason": f"script_parse_error:{type(exc).__name__}",
            "literal_write_sites": 0,
            "formula_write_sites": 0,
            "computed_write_sites": 0,
        }

    literal_names, formula_names = _collect_name_kinds(tree)
    writes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(
                _is_workbook_target(target) for target in targets
            ):
                writes.append(node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "append"
                and node.args
                and _workbook_receiver(node)
            ):
                writes.append(node.args[0])
            if node.func.attr == "cell":
                for keyword in node.keywords:
                    if keyword.arg == "value":
                        writes.append(keyword.value)

    formula = sum(_formula_expr(value, formula_names) for value in writes)
    literal = sum(
        _literal_expr(value, literal_names)
        and not _formula_expr(value, formula_names)
        for value in writes
    )
    computed = len(writes) - literal - formula
    flagged = literal > 0 and formula == 0 and computed == 0
    if flagged:
        reason = "literal_only_workbook_writes"
    elif formula or computed:
        reason = "responsive_or_formula_write_detected"
    else:
        reason = "no_literal_workbook_write_detected"
    return {
        "status": "evaluated",
        "flagged": flagged,
        "reason": reason,
        "literal_write_sites": literal,
        "formula_write_sites": formula,
        "computed_write_sites": computed,
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
    try:
        records = load_real_records(runs_dir, task_set_scope)
    except AnalysisInputError:
        raise
    runs_resolved = runs_dir.resolve()
    if sidecars_dir is not None:
        sidecars_resolved = sidecars_dir.resolve()
        if sidecars_resolved == runs_resolved or runs_resolved in sidecars_resolved.parents:
            raise AnalysisInputError("sidecars directory must be outside the runs directory")
        sidecars_dir.mkdir(parents=True, exist_ok=True)

    by_task_set: dict[str, dict[str, dict[str, int]]] = {}
    sidecar_paths: list[str] = []
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
                "literal_write_sites": 0,
                "formula_write_sites": 0,
                "computed_write_sites": 0,
            }
            script_sha256 = None

        if audit["status"] == "unavailable":
            unreadable_runs.append(run_id)

        evidence = {
            "script_sha256": script_sha256,
            "episode_sha256": record["_source_episode_sha256"],
            "literal_write_sites": audit["literal_write_sites"],
            "formula_write_sites": audit["formula_write_sites"],
            "computed_write_sites": audit["computed_write_sites"],
            "reason": audit["reason"],
        }
        evidence_sha256 = _sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        )
        sidecar = {
            "analysis": "hardcoding",
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
                "Static sealed-script risk audit; the perturb-and-rerun metric "
                "cannot be reconstructed because canonical records do not retain "
                "its required perturbed artifacts."
            ),
        }
        if sidecars_dir is not None:
            sidecar_path = sidecars_dir / f"{run_id}.json"
            sidecar_path.write_text(
                json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            sidecar_paths.append(str(sidecar_path))

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
    return {
        "analysis": "hardcoding",
        "method": _METHOD,
        "task_set_scope": task_set_scope,
        "n_episodes": len(records),
        "totals": totals,
        "by_task_set": by_task_set,
        "sidecars_dir": str(sidecars_dir) if sidecars_dir is not None else None,
        "n_sidecars": len(sidecar_paths),
        "unreadable_runs": unreadable_runs,
        "n_unreadable": len(unreadable_runs),
        "golden_values_accessed": False,
    }


def count_hardcoding_flags(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compatibility counter for already-materialized evaluator fields."""
    by_condition: dict[str, dict[str, int]] = {
        cond: {"hardcoded": 0, "not_hardcoded": 0, "no_flag_field": 0}
        for cond in _ALL_CONDITIONS
    }
    for record in records:
        bucket = by_condition.setdefault(
            str(record.get("condition")),
            {"hardcoded": 0, "not_hardcoded": 0, "no_flag_field": 0},
        )
        outcome = record.get("final_outcome") or {}
        if "hardcoded" not in outcome:
            bucket["no_flag_field"] += 1
        elif outcome["hardcoded"]:
            bucket["hardcoded"] += 1
        else:
            bucket["not_hardcoded"] += 1
    by_condition = {
        condition: counts
        for condition, counts in by_condition.items()
        if sum(counts.values())
    }
    return {
        "n_episodes": len(records),
        "n_hardcoded": sum(v["hardcoded"] for v in by_condition.values()),
        "n_not_hardcoded": sum(v["not_hardcoded"] for v in by_condition.values()),
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
