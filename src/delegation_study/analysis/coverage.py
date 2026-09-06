"""Error-coverage analyses (C1–C5) over committed records.

Read-only on ``data/runs``. Does not import ``analyze.py`` pipelines, does
not write under ``data/runs/``, and does not print gold, hints, patches, or
FAIL_TO_PASS lists. Gold is used host-side only to produce a class label.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import openpyxl

from delegation_study.analysis.process import (
    EMPTY_SHA256,
    ENV_SPREADSHEET,
    ENV_SWE,
    SS_EXEC_TOOLS,
    SS_INSPECT_TOOLS,
    SS_REVISE_TOOLS,
    SWE_REVISE_TOOLS,
    count_parent_verification,
    disposition_from_hashes,
)
from delegation_study.evaluator import (
    _parse_answer_positions,
    evaluate_task,
)
from delegation_study.recalc import recalc_file
from delegation_study.spreadsheetbench.execute import run_in_sandbox


# ── Labels ───────────────────────────────────────────────────────────────────

SS_CLASSES = ("crash", "error_cells", "empty", "silent")
SS_VISIBLE = frozenset({"crash", "error_cells", "empty"})
SWE_CLASSES = ("no_patch", "apply_fail", "tests_fail_loud", "tests_fail_silent")
SWE_VISIBLE = frozenset({"no_patch", "apply_fail", "tests_fail_loud"})
REPLAY_FAILED = "replay_failed"
REPLAY_DISAGREES = "replay_disagrees_correct"

EXCEL_ERROR_TOKENS = (
    "#NAME?",
    "#REF!",
    "#VALUE!",
    "#DIV/0!",
    "#N/A",
    "#NULL!",
    "#NUM!",
    "#GETTING_DATA",
    "#SPILL!",
    "#CALC!",
)
_EXCEL_ERROR_RE = re.compile(r"^#[A-Z0-9/_!.?]+$")
TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
EMPTY_BLANK_THRESHOLD = 0.80
PUBLIC_META_KEYS = (
    "id",
    "task_id",
    "answer_position",
    "answer_sheet",
    "init_files",
    "spreadsheet_path",
)

SONNET_ROOT = Path("<REPO>")
LUNA_ROOT = Path("<REPO>")
SONNET_SS_TASK_SET = "main_v04"
LUNA_SS_TASK_SET = "main_v04_m2"
FA_TASK_SET = "fixed_artifact"
SONNET_SWE_V035 = "main_v035"
SONNET_SWE_V3 = "main"
LUNA_SWE_TASK_SET = "swe_main_m2"


# ── Pure classifiers (locked by tests) ───────────────────────────────────────

def is_excel_error(value: Any) -> bool:
    """True iff *value* is an Excel error token a parent could see."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text in EXCEL_ERROR_TOKENS:
        return True
    return bool(_EXCEL_ERROR_RE.match(text))


def is_blank_cell(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def blank_fraction(values: list[Any]) -> float:
    if not values:
        return 1.0
    return sum(1 for value in values if is_blank_cell(value)) / float(len(values))


def is_plausible_populated(values: list[Any]) -> bool:
    """Populated with numbers or non-error strings of plausible type/shape."""
    if not values:
        return False
    if blank_fraction(values) >= EMPTY_BLANK_THRESHOLD:
        return False
    if any(is_excel_error(value) for value in values):
        return False
    nonblank = [value for value in values if not is_blank_cell(value)]
    if not nonblank:
        return False
    return all(
        isinstance(value, (int, float, bool))
        or (isinstance(value, str) and not is_excel_error(value))
        for value in nonblank
    )


def classify_spreadsheet(
    *,
    exit_code: int | None,
    timed_out: bool,
    graded_values: list[Any] | None,
    gold_match: bool | None,
    exception: bool = False,
    blank_threshold: float = EMPTY_BLANK_THRESHOLD,
) -> str:
    """Classify one wrong-candidate replay by what a parent could see.

    Priority: crash > error_cells > empty > silent.
    ``gold_match`` is a host-side boolean only (never gold contents).
    """
    crashed = bool(timed_out) or bool(exception)
    if exit_code is not None and exit_code != 0:
        crashed = True
    if crashed:
        return "crash"
    if graded_values is None:
        return "empty" if exit_code == 0 else "crash"
    if any(is_excel_error(value) for value in graded_values):
        return "error_cells"
    if blank_fraction(graded_values) >= blank_threshold:
        return "empty"
    if gold_match is True:
        return REPLAY_DISAGREES
    return "silent"


def classify_swe_wrong(
    *,
    has_patch: bool,
    applies_cleanly: bool | None,
    stored_log_has_traceback: bool,
) -> str:
    """Classify one wrong SWE candidate from stored episode fields and logs."""
    if not has_patch:
        return "no_patch"
    if applies_cleanly is False:
        return "apply_fail"
    if stored_log_has_traceback:
        return "tests_fail_loud"
    return "tests_fail_silent"


def stored_log_has_traceback(text: str) -> bool:
    return bool(TRACEBACK_RE.search(text or ""))


def is_visible_ss(label: str) -> bool:
    return label in SS_VISIBLE


def is_visible_swe(label: str) -> bool:
    return label in SWE_VISIBLE


class CoverageModelMixError(ValueError):
    """Raised when a C4 aggregate would mix model strings."""


def assert_single_model(rows: Iterable[dict[str, Any]]) -> str | None:
    """Refuse to mix model strings in one C4 aggregate."""
    models = sorted({
        str(row.get("model"))
        for row in rows
        if row.get("model") not in (None, "")
    })
    if len(models) > 1:
        raise CoverageModelMixError(
            "refusing to mix model strings in one table: " + ", ".join(models)
        )
    return models[0] if models else None


# ── C4 aggregation (fixture-safe) ────────────────────────────────────────────

def c4_rates(
    rows: list[dict[str, Any]],
    *,
    class_key: str = "error_class",
    visible_fn=is_visible_ss,
) -> dict[str, Any]:
    """Marginal coverage row from per-episode fixture dicts.

    Each row needs ``candidate_success`` and ``final_success`` in {0, 1}.
    Wrong-candidate rows may carry ``error_class``. Missing outcomes omitted
    (SWE null stays missing). Rows that carry ``model`` must share one model.
    """
    assert_single_model(rows)
    observed = [
        row
        for row in rows
        if row.get("candidate_success") in (0, 0.0, 1, 1.0)
        and row.get("final_success") in (0, 0.0, 1, 1.0)
    ]
    n = len(observed)
    cand_ok = sum(1 for row in observed if float(row["candidate_success"]) == 1.0)
    final_ok = sum(1 for row in observed if float(row["final_success"]) == 1.0)
    wrong = [row for row in observed if float(row["candidate_success"]) == 0.0]
    correct = [row for row in observed if float(row["candidate_success"]) == 1.0]
    n_wrong = len(wrong)
    n_correct = len(correct)
    repair_n = sum(1 for row in wrong if float(row["final_success"]) == 1.0)
    damage_n = sum(1 for row in correct if float(row["final_success"]) == 0.0)
    classified = [
        row for row in wrong
        if row.get(class_key) in SS_CLASSES + SWE_CLASSES
    ]
    n_visible = sum(1 for row in classified if visible_fn(str(row.get(class_key))))
    n_classified = len(classified)
    cand_rate = (cand_ok / n) if n else None
    final_rate = (final_ok / n) if n else None
    return {
        "n_observed": n,
        "n_wrong": n_wrong,
        "n_correct": n_correct,
        "candidate_correct_n": cand_ok,
        "candidate_correct_rate": cand_rate,
        "parent_repair_n": repair_n,
        "parent_repair_rate_among_wrong": (repair_n / n_wrong) if n_wrong else None,
        "parent_damage_n": damage_n,
        "parent_damage_rate": (damage_n / n_correct) if n_correct else None,
        "final_success_n": final_ok,
        "final_success_rate": final_rate,
        "net_parent_contribution": (
            None if cand_rate is None or final_rate is None else final_rate - cand_rate
        ),
        "n_visible_wrong": n_visible,
        "n_classified_wrong": n_classified,
        "visible_wrong_share": (
            n_visible / n_classified if n_classified else None
        ),
    }


# ── IO helpers ───────────────────────────────────────────────────────────────

def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if isinstance(event, dict):
            events.append(event)
    return events


def _parse_summary(summary: Any) -> dict[str, Any]:
    if isinstance(summary, dict):
        return summary
    if not isinstance(summary, str) or not summary.strip():
        return {}
    text = summary[:-1] if summary.endswith("…") else summary
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nonempty_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _as_binary(value: Any) -> float | None:
    if value is True or value == 1 or value == 1.0 or value == "1" or value == "1.0":
        return 1.0
    if value is False or value == 0 or value == 0.0 or value == "0" or value == "0.0":
        return 0.0
    return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not fieldnames:
        path.write_text("", encoding="utf-8")
        return
    names = fieldnames or list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in names})


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join([line, sep, *body])


def _fmt_rate(numer: int | None, denom: int | None) -> str:
    if numer is None or denom is None or denom == 0:
        return "n/a"
    return f"{numer}/{denom} ({numer / denom:.4f})"


# ── Public metadata (no gold) ────────────────────────────────────────────────

def load_public_task_meta(dataset_path: Path) -> dict[str, dict[str, Any]]:
    raw = _load_json(dataset_path)
    items = raw if isinstance(raw, list) else list(raw.values())
    mapping: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        task_id = item.get("id", item.get("task_id"))
        if task_id is None:
            continue
        mapping[str(task_id)] = {key: item.get(key) for key in PUBLIC_META_KEYS}
    return mapping


def public_init_path(public_root: Path, task_id: str, meta: dict[str, Any]) -> Path | None:
    init_files = meta.get("init_files") or []
    for name in init_files:
        candidate = public_root / str(task_id) / str(name)
        if candidate.is_file():
            return candidate
    folder = public_root / str(task_id)
    if folder.is_dir():
        matches = sorted(folder.glob("*_init.xls*"))
        if matches:
            return matches[0]
    return None


def host_golden_path(hidden_root: Path, task_id: str) -> Path | None:
    """Host-side gold path. Caller must not print file contents."""
    folder = hidden_root / str(task_id)
    if not folder.is_dir():
        return None
    matches = sorted(folder.glob("*_golden.xls*"))
    return matches[0] if matches else None


# ── Graded-range inspection (output workbook only) ───────────────────────────

def inspect_graded_values(
    output_path: Path,
    answer_position: str,
    answer_sheet: str | None,
) -> list[Any] | None:
    """Read graded-range values from an output workbook. No gold involved."""
    if not output_path.is_file():
        return None
    try:
        workbook = openpyxl.load_workbook(filename=str(output_path), data_only=True)
    except Exception:
        return None
    try:
        ranges = _parse_answer_positions(answer_position, answer_sheet, workbook)
    except Exception:
        workbook.close()
        return None
    values: list[Any] = []
    for sheet_name, cells in ranges:
        if sheet_name not in workbook.sheetnames:
            continue
        worksheet = workbook[sheet_name]
        for addr in cells:
            values.append(worksheet[addr].value)
    workbook.close()
    return values


def host_gold_match(
    golden_path: Path,
    output_path: Path,
    answer_position: str,
    answer_sheet: str | None,
) -> bool | None:
    """Host-side gold compare. Returns only a boolean; never gold values."""
    result = evaluate_task(
        golden_path=golden_path,
        output_path=output_path,
        answer_position=answer_position,
        answer_sheet=answer_sheet,
    )
    if result.get("evaluator_status") != "ok":
        return None
    success = result.get("full_task_success")
    if success in (1, 1.0, True):
        return True
    if success in (0, 0.0, False):
        return False
    return None


def summarize_values(values: list[Any] | None) -> dict[str, Any]:
    """Type/error summary of an observation. Never gold."""
    if values is None:
        return {
            "n_cells": 0,
            "n_blank": 0,
            "n_error": 0,
            "n_numeric": 0,
            "n_text": 0,
            "error_tokens": [],
            "type_mix": {},
            "blank_fraction": 1.0,
            "looks_plausible": False,
        }
    mix: Counter[str] = Counter()
    error_tokens: list[str] = []
    n_blank = n_error = n_numeric = n_text = 0
    for value in values:
        if is_blank_cell(value):
            n_blank += 1
            mix["blank"] += 1
            continue
        if is_excel_error(value):
            n_error += 1
            token = str(value).strip()
            error_tokens.append(token)
            mix["excel_error"] += 1
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            n_numeric += 1
            mix["numeric"] += 1
            continue
        if isinstance(value, bool):
            mix["bool"] += 1
            continue
        n_text += 1
        mix["text"] += 1
    return {
        "n_cells": len(values),
        "n_blank": n_blank,
        "n_error": n_error,
        "n_numeric": n_numeric,
        "n_text": n_text,
        "error_tokens": sorted(set(error_tokens)),
        "type_mix": dict(mix),
        "blank_fraction": blank_fraction(values),
        "looks_plausible": is_plausible_populated(values),
    }


# ── Parent behavior ──────────────────────────────────────────────────────────

def parent_ran_candidate(events: list[dict[str, Any]], environment: str) -> bool:
    """True iff the parent executed the handed-off candidate before revising."""
    saw_revise = False
    for event in events:
        if event.get("role") != "parent" or event.get("event_type") != "tool_result":
            continue
        payload = event.get("payload") or {}
        tool = payload.get("tool_name")
        if environment == ENV_SPREADSHEET:
            if tool in SS_REVISE_TOOLS:
                saw_revise = True
            if tool == "run_solution" and not saw_revise:
                return True
        elif environment == ENV_SWE:
            if tool in SWE_REVISE_TOOLS:
                saw_revise = True
            if tool == "exec_shell" and not saw_revise:
                summary = _parse_summary(payload.get("summary"))
                executed = summary.get("executed")
                if executed is None:
                    executed = float(summary.get("wall_seconds") or 0.0) > 0.001
                blob = " ".join(
                    str(summary.get(key) or "")
                    for key in ("command", "cmd", "stderr", "output", "stdout")
                )
                if executed and re.search(r"^(python3?|pytest)\b", blob.strip(), re.I):
                    return True
                if executed and summary.get("feedback_command") is True:
                    return True
        else:
            raise ValueError(f"unknown environment {environment!r}")
    return False


def count_explicit_read_output_range(events: list[dict[str, Any]]) -> int:
    n = 0
    for event in events:
        if event.get("role") != "parent" or event.get("event_type") != "tool_result":
            continue
        payload = event.get("payload") or {}
        if payload.get("tool_name") in SS_INSPECT_TOOLS:
            n += 1
    return n


def ast_fingerprint(source: str) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    dumped = ast.dump(tree, include_attributes=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def revision_hash_and_ast(
    candidate_path: Path | None,
    final_path: Path | None,
    candidate_hash: str | None,
    final_hash: str | None,
) -> dict[str, Any]:
    hash_revised = bool(
        candidate_hash and (not final_hash or candidate_hash != final_hash)
    )
    cand_ast = ast_fingerprint(candidate_path.read_text(encoding="utf-8")) if (
        candidate_path is not None and candidate_path.is_file()
    ) else None
    final_ast = ast_fingerprint(final_path.read_text(encoding="utf-8")) if (
        final_path is not None and final_path.is_file()
    ) else None
    ast_revised = bool(cand_ast and final_ast and cand_ast != final_ast)
    if cand_ast is None or final_ast is None:
        ast_comparable = False
    else:
        ast_comparable = True
    return {
        "hash_revised": hash_revised,
        "ast_revised": ast_revised if ast_comparable else None,
        "ast_comparable": ast_comparable,
        "disposition": disposition_from_hashes(candidate_hash, final_hash),
    }


def extract_parent_terminal_message(events: list[dict[str, Any]], run_dir: Path) -> dict[str, Any]:
    """Last parent assistant text from events/traces. Not gold.

    Spreadsheet v0.4.1 public events omit assistant text. That is recorded
    as MISSING rather than invented.
    """
    last_text: str | None = None
    source = None
    for event in events:
        if event.get("role") != "parent" or event.get("event_type") != "model_call":
            continue
        payload = event.get("payload") or {}
        text = payload.get("assistant_text")
        if isinstance(text, str):
            last_text = text
            source = "events.model_call.assistant_text"
        raw_path = payload.get("raw_payload_path")
        if isinstance(raw_path, str) and raw_path:
            candidate = Path(raw_path)
            if not candidate.is_file():
                candidate = run_dir / raw_path
            if candidate.is_file():
                try:
                    raw = _load_json(candidate)
                except (OSError, json.JSONDecodeError):
                    raw = None
                if isinstance(raw, dict) and isinstance(raw.get("assistant_text"), str):
                    last_text = raw["assistant_text"]
                    source = "raw_payload_path"
    parent_txt = run_dir / "handoff" / "parent_terminal_text.txt"
    if last_text is None and parent_txt.is_file():
        last_text = parent_txt.read_text(encoding="utf-8")
        source = "handoff/parent_terminal_text.txt"
    if last_text is None:
        return {
            "present": False,
            "status": "MISSING",
            "reason": "parent assistant text not persisted in events/traces",
            "n_chars": 0,
            "mentions_observation": None,
        }
    lowered = last_text.lower()
    mentions = any(
        token in lowered
        for token in (
            "output",
            "value",
            "range",
            "error",
            "#name",
            "#ref",
            "#value",
            "blank",
            "empty",
            "ran",
            "run_solution",
        )
    )
    return {
        "present": True,
        "status": "present",
        "reason": source,
        "n_chars": len(last_text),
        "mentions_observation": mentions,
    }


def parent_auto_observations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("role") != "parent" or event.get("event_type") != "tool_result":
            continue
        payload = event.get("payload") or {}
        observation = payload.get("auto_observation")
        if not isinstance(observation, dict):
            continue
        summary = _parse_summary(payload.get("summary"))
        values = observation.get("values")
        flat: list[Any] | None
        if isinstance(values, list):
            flat = []
            for row in values:
                if isinstance(row, list):
                    flat.extend(row)
                else:
                    flat.append(row)
        else:
            flat = None
        errors = observation.get("errors") or []
        error_tokens = []
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], str):
                    error_tokens.append(item[1])
                elif isinstance(item, str):
                    error_tokens.append(item)
        rows.append({
            "tool_name": payload.get("tool_name"),
            "exit_code": summary.get("exit_code"),
            "succeeded": summary.get("succeeded"),
            "timed_out": summary.get("timed_out"),
            "observation_error": observation.get("error"),
            "sheet": observation.get("sheet"),
            "range": observation.get("range"),
            "n_listed_errors": len(errors) if isinstance(errors, list) else 0,
            "error_tokens": sorted(set(error_tokens)),
            "value_summary": summarize_values(flat),
        })
    return rows


# ── Episode loading ──────────────────────────────────────────────────────────

@dataclass
class CoverageEpisode:
    model: str
    environment: str
    task_set: str
    run_id: str
    task_id: str
    condition: str
    run_dir: str
    candidate_success: float | None
    final_success: float | None
    candidate_hash: str | None
    final_hash: str | None
    parent_finalized: bool
    worker_submitted: bool
    executions: int
    inspections: int
    inspections_explicit: int
    inspections_auto: int
    revisions: int
    ran_candidate: bool
    hash_revised: bool
    ast_revised: bool | None
    disposition: str
    arm: str | None = None
    source_run_id: str | None = None
    source_condition: str | None = None
    candidate_correct_flag: bool | None = None
    applies_cleanly: bool | None = None
    error_class: str | None = None
    class_source: str | None = None
    replay_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _script_paths(run_dir: Path, environment: str) -> tuple[Path | None, Path | None]:
    if environment == ENV_SPREADSHEET:
        cand = run_dir / "handoff" / "solution.py"
        final = run_dir / "final" / "solution.py"
        return (cand if cand.is_file() else None, final if final.is_file() else None)
    cand = run_dir / "handoff" / "candidate.patch"
    final = run_dir / "handoff" / "final.patch"
    if not final.is_file():
        alt = run_dir / "final" / "final.patch"
        final = alt if alt.is_file() else None
    return (cand if cand.is_file() else None, final if final is not None and final.is_file() else None)


def _spreadsheet_outcomes(record: dict[str, Any]) -> tuple[float | None, float | None]:
    worker = (record.get("worker_outcome") or {}).get("full_task_success")
    final = (record.get("final_outcome") or {}).get("full_task_success")
    worker_bin = _as_binary(worker)
    final_bin = 0.0 if final is None else _as_binary(final)
    if final is not None and final_bin is None:
        final_bin = 0.0
    return worker_bin, final_bin


def _swe_outcomes(record: dict[str, Any]) -> tuple[float | None, float | None]:
    metrics = (record.get("env_metrics") or {}).get("swebench") or {}
    return _as_binary(metrics.get("candidate_resolved")), _as_binary(metrics.get("final_resolved"))


def episode_from_record(
    record: dict[str, Any],
    run_dir: Path,
    *,
    model: str,
    environment: str,
) -> CoverageEpisode:
    events = _load_jsonl(run_dir / "events.jsonl")
    handoff = record.get("handoff") or {}
    fa = record.get("fixed_artifact") if isinstance(record.get("fixed_artifact"), dict) else {}
    if environment == ENV_SPREADSHEET:
        cand, final = _spreadsheet_outcomes(record)
        applies = None
    else:
        cand, final = _swe_outcomes(record)
        applies = ((record.get("env_metrics") or {}).get("swebench") or {}).get("applies_cleanly")
        if applies is not None:
            applies = bool(applies)
    cand_hash = _nonempty_hash(handoff.get("candidate_script_sha256"))
    final_hash = _nonempty_hash(record.get("final_script_sha256"))
    cand_path, final_path = _script_paths(run_dir, environment)
    rev = revision_hash_and_ast(cand_path, final_path, cand_hash, final_hash)
    counts = count_parent_verification(events, environment)
    compliance = record.get("compliance") or {}
    return CoverageEpisode(
        model=model,
        environment=environment,
        task_set=str(record.get("task_set") or ""),
        run_id=str(record["run_id"]),
        task_id=str(record["task_id"]),
        condition=str(record["condition"]),
        run_dir=str(run_dir),
        candidate_success=cand,
        final_success=final,
        candidate_hash=cand_hash,
        final_hash=final_hash,
        parent_finalized=bool(compliance.get("parent_finalized")),
        worker_submitted=bool(handoff.get("worker_submitted_candidate")),
        executions=counts["executions"],
        inspections=counts["inspections"],
        inspections_explicit=counts["inspections_explicit"],
        inspections_auto=counts["inspections_auto"],
        revisions=counts["revisions"],
        ran_candidate=parent_ran_candidate(events, environment),
        hash_revised=bool(rev["hash_revised"]),
        ast_revised=rev["ast_revised"],
        disposition=str(rev["disposition"]),
        arm=fa.get("arm"),
        source_run_id=fa.get("source_run_id"),
        source_condition=fa.get("source_condition"),
        candidate_correct_flag=(
            None if fa.get("candidate_correct") is None else bool(fa.get("candidate_correct"))
        ),
        applies_cleanly=applies,
    )


def _iter_episode_dirs(runs_dir: Path) -> Iterable[Path]:
    for path in sorted(runs_dir.iterdir(), key=lambda item: item.name):
        if path.is_dir() and (path / "episode.json").is_file():
            yield path


def load_task_set_episodes(
    runs_dir: Path,
    *,
    model: str,
    environment: str,
    task_set: str,
    run_ids: list[str] | None = None,
) -> list[CoverageEpisode]:
    episodes: list[CoverageEpisode] = []
    if run_ids is not None:
        dirs = [runs_dir / run_id for run_id in run_ids]
    else:
        dirs = list(_iter_episode_dirs(runs_dir))
    for run_dir in dirs:
        episode_path = run_dir / "episode.json"
        if not episode_path.is_file():
            continue
        record = _load_json(episode_path)
        if not isinstance(record, dict):
            continue
        if record.get("task_set") != task_set:
            continue
        if environment == ENV_SWE and record.get("benchmark") != "swebench":
            continue
        if environment == ENV_SPREADSHEET and record.get("benchmark") == "swebench":
            continue
        episodes.append(
            episode_from_record(record, run_dir, model=model, environment=environment)
        )
    return episodes


def parse_status_run_ids(status_path: Path) -> list[str]:
    uuid_re = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.I,
    )
    ids: list[str] = []
    seen: set[str] = set()
    for line in status_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        match = uuid_re.search(line)
        if match is None:
            continue
        run_id = match.group(0).lower()
        if run_id in seen:
            continue
        seen.add(run_id)
        ids.append(run_id)
    return ids


# ── Replay ───────────────────────────────────────────────────────────────────

def replay_spreadsheet_candidate(
    *,
    script_path: Path,
    input_path: Path,
    answer_position: str,
    answer_sheet: str | None,
    golden_path: Path | None,
    run_id: str,
) -> dict[str, Any]:
    """Run one candidate on a clean public workbook. Gold host-side only."""
    if not script_path.is_file():
        return {
            "error_class": REPLAY_FAILED,
            "reason": "missing artifact: handoff/solution.py",
            "gold_used": False,
        }
    if not input_path.is_file():
        return {
            "error_class": REPLAY_FAILED,
            "reason": "missing artifact: public init workbook",
            "gold_used": False,
        }
    scratch = Path(tempfile.mkdtemp(prefix="coverage_replay_"))
    try:
        output_dir = scratch / "output"
        output_dir.mkdir()
        result = run_in_sandbox(
            script_path=script_path,
            input_path=input_path,
            output_dir=output_dir,
            run_id=f"coverage-{run_id[:12]}",
            role="coverage_replay",
        )
        graded: list[Any] | None = None
        gold_match: bool | None = None
        gold_used = False
        if result.output_path is not None and result.output_path.is_file():
            recalc_file(result.output_path)
            graded = inspect_graded_values(
                result.output_path, answer_position, answer_sheet
            )
            if golden_path is not None and golden_path.is_file():
                gold_match = host_gold_match(
                    golden_path, result.output_path, answer_position, answer_sheet
                )
                gold_used = True
        label = classify_spreadsheet(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            graded_values=graded,
            gold_match=gold_match,
        )
        return {
            "error_class": label,
            "reason": None,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "had_output": result.output_path is not None,
            "gold_used": gold_used,
            "gold_match": gold_match,
            "value_summary": summarize_values(graded),
        }
    except Exception as exc:
        return {
            "error_class": REPLAY_FAILED,
            "reason": f"replay exception: {type(exc).__name__}",
            "gold_used": False,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def classify_from_stored_ss(run_dir: Path, answer_position: str, answer_sheet: str | None) -> dict[str, Any]:
    """Secondary class from stored worker events / worker_eval (not a new evaluator)."""
    events = _load_jsonl(run_dir / "events.jsonl")
    exit_code = None
    timed_out = False
    for event in events:
        if event.get("role") != "worker" or event.get("event_type") != "tool_result":
            continue
        payload = event.get("payload") or {}
        if payload.get("tool_name") != "run_solution":
            continue
        summary = _parse_summary(payload.get("summary"))
        if "exit_code" in summary:
            exit_code = summary.get("exit_code")
        if summary.get("timed_out"):
            timed_out = True
    output = run_dir / "worker_eval" / "output.xlsx"
    graded = inspect_graded_values(output, answer_position, answer_sheet) if output.is_file() else None
    # Stored evaluator already compared to gold; we only use the public success bit.
    weval_path = run_dir / "worker_eval" / "evaluator.json"
    gold_match = None
    if weval_path.is_file():
        weval = _load_json(weval_path)
        gold_match = True if weval.get("full_task_success") in (1, 1.0) else False
        if weval.get("evaluator_status") != "ok":
            gold_match = None
    label = classify_spreadsheet(
        exit_code=exit_code,
        timed_out=timed_out,
        graded_values=graded,
        gold_match=gold_match,
    )
    return {
        "error_class": label,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "had_output": output.is_file(),
        "value_summary": summarize_values(graded),
        "source": "stored_worker_eval",
    }


def scan_swe_traceback(run_dir: Path) -> bool:
    """True if stored harness/test logs contain a Python traceback.

    Does not read hidden FAIL_TO_PASS lists. Scans reports, runner logs, and
    stored tool-output files only.
    """
    roots = [
        run_dir / "reports",
        run_dir / "logs",
        run_dir / "logs" / "runner.stderr.log",
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        files.append(events_path)
    for path in files:
        if path.suffix not in {".log", ".txt", ".json", ".jsonl"} and path.name != "runner.stderr.log":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if stored_log_has_traceback(text):
            return True
    return False


def classify_swe_episode(episode: CoverageEpisode) -> dict[str, Any]:
    run_dir = Path(episode.run_dir)
    cand_path, _final = _script_paths(run_dir, ENV_SWE)
    has_patch = bool(
        cand_path is not None
        and cand_path.is_file()
        and cand_path.stat().st_size > 0
        and episode.candidate_hash
        and episode.candidate_hash != EMPTY_SHA256
    )
    if not episode.worker_submitted and not has_patch:
        has_patch = False
    traceback_present = scan_swe_traceback(run_dir) if has_patch else False
    label = classify_swe_wrong(
        has_patch=has_patch,
        applies_cleanly=episode.applies_cleanly,
        stored_log_has_traceback=traceback_present,
    )
    return {
        "error_class": label,
        "has_patch": has_patch,
        "applies_cleanly": episode.applies_cleanly,
        "stored_log_has_traceback": traceback_present,
        "source": "stored_harness_and_logs",
    }


# ── Report assembly ──────────────────────────────────────────────────────────

def assign_ss_classes(
    episodes: list[CoverageEpisode],
    *,
    public_root: Path,
    dataset_path: Path,
    hidden_root: Path | None,
    replay: bool,
) -> None:
    meta = load_public_task_meta(dataset_path)
    cache: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        if episode.candidate_success != 0.0:
            continue
        task_meta = meta.get(episode.task_id) or {}
        answer_position = task_meta.get("answer_position")
        answer_sheet = task_meta.get("answer_sheet")
        run_dir = Path(episode.run_dir)
        stored = None
        if isinstance(answer_position, str) and answer_position:
            stored = classify_from_stored_ss(run_dir, answer_position, answer_sheet)
        script = run_dir / "handoff" / "solution.py"
        replay_result = None
        if replay and isinstance(answer_position, str) and answer_position:
            cache_key = episode.candidate_hash or episode.run_id
            if cache_key in cache:
                replay_result = dict(cache[cache_key])
            else:
                init_path = public_init_path(public_root, episode.task_id, task_meta)
                golden = host_golden_path(hidden_root, episode.task_id) if hidden_root else None
                if init_path is None:
                    replay_result = {
                        "error_class": REPLAY_FAILED,
                        "reason": "missing artifact: public init workbook",
                        "gold_used": False,
                    }
                else:
                    replay_result = replay_spreadsheet_candidate(
                        script_path=script,
                        input_path=init_path,
                        answer_position=answer_position,
                        answer_sheet=answer_sheet if isinstance(answer_sheet, str) else None,
                        golden_path=golden,
                        run_id=episode.run_id,
                    )
                cache[cache_key] = dict(replay_result)
        replay_ok = (
            replay_result is not None
            and replay_result.get("error_class") not in {None, REPLAY_FAILED}
        )
        if replay_ok:
            episode.error_class = str(replay_result["error_class"])
            episode.class_source = "host_replay"
            episode.replay_reason = replay_result.get("reason")
            episode.extras["replay"] = {
                key: replay_result[key]
                for key in replay_result
                if key != "values"
            }
        elif stored is not None:
            episode.error_class = str(stored["error_class"])
            episode.class_source = "stored_worker_eval"
            episode.extras["stored"] = stored
            if replay_result is not None:
                episode.extras["replay_unused"] = {
                    key: replay_result[key]
                    for key in replay_result
                    if key != "values"
                }
        else:
            episode.error_class = REPLAY_FAILED
            episode.class_source = "missing"
            episode.replay_reason = "missing artifact: answer_position or worker output"
        if stored is not None:
            episode.extras.setdefault("stored", stored)


def assign_swe_classes(episodes: list[CoverageEpisode]) -> None:
    for episode in episodes:
        if episode.candidate_success not in (0.0, None):
            continue
        if episode.candidate_success is None and episode.worker_submitted:
            # Missing resolved with a submitted patch: still classify.
            pass
        elif episode.candidate_success is None and not episode.worker_submitted:
            episode.error_class = "no_patch"
            episode.class_source = "stored_harness_and_logs"
            episode.extras["swe"] = {"has_patch": False, "reason": "no submitted candidate"}
            continue
        if episode.candidate_success == 1.0:
            continue
        result = classify_swe_episode(episode)
        episode.error_class = str(result["error_class"])
        episode.class_source = str(result["source"])
        episode.extras["swe"] = result


def cell_class_counts(episodes: list[CoverageEpisode]) -> list[dict[str, Any]]:
    """Count classes for a pre-filtered wrong-candidate list."""
    bags: dict[str, Counter[str]] = defaultdict(Counter)
    n_by_cell: Counter[str] = Counter()
    for episode in episodes:
        label = episode.error_class or REPLAY_FAILED
        bags[episode.condition][label] += 1
        n_by_cell[episode.condition] += 1
    rows: list[dict[str, Any]] = []
    for condition in ("P0", "P1"):
        bag = bags.get(condition, Counter())
        row = {
            "cell": condition,
            "n_wrong": int(n_by_cell.get(condition, 0)),
        }
        for label in SS_CLASSES + (REPLAY_FAILED, REPLAY_DISAGREES) + SWE_CLASSES:
            row[label] = int(bag.get(label, 0))
        rows.append(row)
    return rows


def c4_from_episodes(
    episodes: list[CoverageEpisode],
    *,
    model: str,
    environment: str,
    cell: str,
    visible_fn=is_visible_ss,
) -> dict[str, Any]:
    rows = [
        {
            "candidate_success": episode.candidate_success,
            "final_success": episode.final_success,
            "error_class": episode.error_class,
        }
        for episode in episodes
        if episode.condition == cell
    ]
    rates = c4_rates(rows, visible_fn=visible_fn)
    rates.update({
        "model": model,
        "environment": environment,
        "cell": cell,
    })
    return rates


def c2_rows(episodes: list[CoverageEpisode]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for episode in episodes:
        if episode.condition not in ("P0", "P1"):
            continue
        rows.append({
            "model": episode.model,
            "environment": episode.environment,
            "task_set": episode.task_set,
            "cell": episode.condition,
            "task_id": episode.task_id,
            "run_id": episode.run_id,
            "arm": episode.arm or "",
            "source_run_id": episode.source_run_id or "",
            "candidate_success": episode.candidate_success,
            "final_success": episode.final_success,
            "error_class": episode.error_class or "",
            "class_source": episode.class_source or "",
            "executions": episode.executions,
            "read_output_range_explicit": episode.inspections_explicit,
            "inspections_auto": episode.inspections_auto,
            "ran_candidate": episode.ran_candidate,
            "hash_revised": episode.hash_revised,
            "ast_revised": episode.ast_revised,
            "disposition": episode.disposition,
            "parent_finalized": episode.parent_finalized,
            "repaired": (
                episode.candidate_success == 0.0 and episode.final_success == 1.0
            ),
            "damaged": (
                episode.candidate_success == 1.0 and episode.final_success == 0.0
            ),
        })
    return rows


def write_c3_page(
    path: Path,
    *,
    title: str,
    episode: CoverageEpisode,
    observations: list[dict[str, Any]],
    terminal: dict[str, Any],
) -> None:
    visible = episode.error_class in SS_VISIBLE if episode.error_class else None
    lines = [
        f"# {title}",
        "",
        f"- model: `{episode.model}`",
        f"- environment: `{episode.environment}`",
        f"- task_set: `{episode.task_set}`",
        f"- task_id: `{episode.task_id}`",
        f"- cell: `{episode.condition}`",
        f"- run_id: `{episode.run_id}`",
        f"- arm: `{episode.arm or 'n/a'}`",
        f"- candidate_success: `{episode.candidate_success}`",
        f"- final_success: `{episode.final_success}`",
        f"- C1 class: `{episode.error_class}` (source `{episode.class_source}`)",
        f"- parent ran candidate before revise: `{episode.ran_candidate}`",
        f"- parent executions: `{episode.executions}`",
        f"- explicit read_output_range: `{episode.inspections_explicit}`",
        f"- auto inspections: `{episode.inspections_auto}`",
        f"- hash revised: `{episode.hash_revised}`; AST revised: `{episode.ast_revised}`",
        f"- disposition: `{episode.disposition}`",
        f"- parent finalized: `{episode.parent_finalized}`",
        "",
        "## Did the parent run?",
        "",
        (
            "Yes — at least one parent `run_solution` occurred before any "
            "`write_solution`."
            if episode.ran_candidate
            else "No — the parent did not execute the handed-off candidate before revising "
            "(or never executed it)."
        ),
        "",
        "## Auto-observation after parent runs",
        "",
    ]
    if not observations:
        lines.append("MISSING — no parent `auto_observation` payload in events.")
    else:
        for index, obs in enumerate(observations, 1):
            summary = obs.get("value_summary") or {}
            lines.extend([
                f"### Observation {index} (`{obs.get('tool_name')}`)",
                "",
                f"- exit_code: `{obs.get('exit_code')}`; succeeded: `{obs.get('succeeded')}`; timed_out: `{obs.get('timed_out')}`",
                f"- observation error: `{obs.get('observation_error')}`",
                f"- sheet/range: `{obs.get('sheet')}` / `{obs.get('range')}`",
                f"- n_cells: `{summary.get('n_cells')}`; numeric: `{summary.get('n_numeric')}`; blank: `{summary.get('n_blank')}`; errors: `{summary.get('n_error')}`",
                f"- error tokens: `{summary.get('error_tokens')}`",
                f"- type mix: `{summary.get('type_mix')}`",
                f"- looks plausible (populated, non-error, not mostly blank): `{summary.get('looks_plausible')}`",
                "",
            ])
    last = observations[-1]["value_summary"] if observations else {}
    lines.extend([
        "## Did the values look plausible?",
        "",
        (
            "Yes — last observation was populated with non-error values of plausible type."
            if last.get("looks_plausible")
            else (
                "No — last observation was blank, erroneous, or missing."
                if observations
                else "Unknown — parent left no observation payload."
            )
        ),
        "",
        "## Parent terminal message",
        "",
    ])
    if terminal.get("present"):
        lines.extend([
            f"Present (`{terminal.get('reason')}`), {terminal.get('n_chars')} chars.",
            f"Comments on observed values/errors/run: `{terminal.get('mentions_observation')}`.",
            "",
            "Message text is not copied here (length-only + mention flag).",
        ])
    else:
        lines.extend([
            f"MISSING — {terminal.get('reason')}.",
            "",
            "v0.4.1 spreadsheet events store model_call token counts only, not assistant text.",
        ])
    if visible is True:
        lines.extend(["", "Visible-wrong class (crash|error_cells|empty)."])
    elif episode.error_class == "silent":
        lines.extend(["", "Silent-wrong class: clean run, populated graded range, wrong vs gold (host-side)."])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _c4_table_rows(bundles: list[dict[str, Any]]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in bundles:
        rows.append([
            item.get("model"),
            item.get("environment"),
            item.get("cell"),
            item.get("n_observed"),
            _fmt_rate(item.get("candidate_correct_n"), item.get("n_observed")),
            _fmt_rate(item.get("parent_repair_n"), item.get("n_wrong")),
            _fmt_rate(item.get("parent_damage_n"), item.get("n_correct")),
            item.get("net_parent_contribution"),
            _fmt_rate(item.get("n_visible_wrong"), item.get("n_classified_wrong")),
        ])
    return rows


def write_digest(
    path: Path,
    *,
    model_label: str,
    c4_rows: list[dict[str, Any]],
    c1_blocks: list[tuple[str, list[dict[str, Any]]]],
    c2_notes: list[str],
    c3_index: list[str],
    c5_rows: list[dict[str, Any]],
    extras: list[str],
) -> None:
    headers = [
        "model",
        "environment",
        "P cell",
        "n",
        "candidate correct",
        "repair among wrong",
        "damage among correct",
        "net (final − candidate)",
        "visible wrong / n_wrong",
    ]
    lines = [
        f"# Coverage digest — {model_label}",
        "",
        "Sonnet and Luna are **different models; not pooled.**",
        "C4 is the paper marginal-coverage table. Classes come from host replay",
        "(spreadsheet) or stored harness/logs (SWE). Gold is used host-side only",
        "and is never written into these files.",
        "",
        "## C4 — Marginal coverage",
        "",
        _md_table(headers, _c4_table_rows(c4_rows)),
        "",
        "Visible-wrong (spreadsheet) = `crash` | `error_cells` | `empty`.",
        "Visible-wrong (SWE analogue) = `no_patch` | `apply_fail` | `tests_fail_loud`.",
        "",
        "## C1 — Wrong-candidate error class",
        "",
        "Prediction (overlap thesis): P0 wrongs mostly crash/error/empty; P1 wrongs mostly silent.",
        "",
    ]
    for title, rows in c1_blocks:
        lines.append(f"### {title}")
        lines.append("")
        if not rows:
            lines.append("MISSING — no rows.")
            lines.append("")
            continue
        keys = ["cell", "n_wrong", "crash", "error_cells", "empty", "silent", REPLAY_FAILED]
        if any(row.get("no_patch") or row.get("apply_fail") or row.get("tests_fail_loud") or row.get("tests_fail_silent") for row in rows):
            keys = ["cell", "n_wrong", "no_patch", "apply_fail", "tests_fail_loud", "tests_fail_silent"]
        lines.append(_md_table(keys, [[row.get(key, 0) for key in keys] for row in rows]))
        lines.append("")
    lines.extend(["## C2 — Parent behavior × correctness and class", "", *c2_notes, ""])
    lines.extend(["## C3 — What the parent saw", "", *c3_index, ""])
    lines.extend([
        "## C5 — SWE analogue",
        "",
        _md_table(
            ["model", "task_set", "cell", "n_wrong", "no_patch", "apply_fail", "tests_fail_loud", "tests_fail_silent"],
            [
                [
                    row.get("model"),
                    row.get("task_set"),
                    row.get("cell"),
                    row.get("n_wrong"),
                    row.get("no_patch"),
                    row.get("apply_fail"),
                    row.get("tests_fail_loud"),
                    row.get("tests_fail_silent"),
                ]
                for row in c5_rows
            ],
        ) if c5_rows else "MISSING — no SWE wrong-candidate rows on this branch.",
        "",
        *extras,
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _contrast_notes(episodes: list[CoverageEpisode], label: str) -> list[str]:
    notes = [f"### {label}", ""]
    for cell in ("P0", "P1"):
        wrongs = [
            ep for ep in episodes
            if ep.condition == cell and ep.candidate_success == 0.0
        ]
        if not wrongs:
            notes.append(f"- {cell}: no A1 wrong candidates.")
            continue
        visible = [ep for ep in wrongs if ep.error_class in SS_VISIBLE]
        silent = [ep for ep in wrongs if ep.error_class == "silent"]
        notes.append(
            f"- {cell}: n_wrong={len(wrongs)}; visible={len(visible)}; silent={len(silent)}; "
            f"parent-ran {sum(1 for ep in wrongs if ep.ran_candidate)}/{len(wrongs)}; "
            f"repaired {sum(1 for ep in wrongs if ep.final_success == 1.0)}/{len(wrongs)}."
        )
        for ep in wrongs:
            notes.append(
                f"  - `{ep.task_id}` `{ep.error_class}` ran={ep.ran_candidate} "
                f"exec={ep.executions} ror={ep.inspections_explicit} "
                f"rev_hash={ep.hash_revised} ast={ep.ast_revised} "
                f"final={ep.final_success} disp={ep.disposition}"
            )
    p0_visible = [
        ep for ep in episodes
        if ep.condition == "P0" and ep.candidate_success == 0.0 and ep.error_class in SS_VISIBLE
    ]
    p1_silent = [
        ep for ep in episodes
        if ep.condition == "P1" and ep.candidate_success == 0.0 and ep.error_class == "silent"
    ]
    p1_visible = [
        ep for ep in episodes
        if ep.condition == "P1" and ep.candidate_success == 0.0 and ep.error_class in SS_VISIBLE
    ]
    notes.append("")
    notes.append(
        f"- Key contrast P0×visible-wrong: n={len(p0_visible)}; "
        f"ran={sum(1 for ep in p0_visible if ep.ran_candidate)}; "
        f"repaired={sum(1 for ep in p0_visible if ep.final_success == 1.0)}."
    )
    notes.append(
        f"- Key contrast P1×silent-wrong: n={len(p1_silent)}; "
        f"ran={sum(1 for ep in p1_silent if ep.ran_candidate)}; "
        f"repaired={sum(1 for ep in p1_silent if ep.final_success == 1.0)}."
    )
    if p1_visible:
        notes.append(
            f"- P1×visible-wrong present (overlap miss / contraction): n={len(p1_visible)} "
            + ", ".join(f"{ep.task_id}:{ep.error_class}" for ep in p1_visible)
        )
    else:
        notes.append("- P1×visible-wrong: none (overlap miss if the thesis expects P1 silent-only).")
    notes.append("")
    return notes


def generate_sonnet(
    *,
    repo: Path,
    out_dir: Path,
    replay: bool,
    swe_runs: Path | None,
    swe_status: Path | None,
    v3_swe_runs: Path | None,
) -> None:
    runs = repo / "data" / "runs"
    public_root = repo / "data" / "public"
    dataset_path = public_root / "dataset.json"
    hidden_root = repo / "data" / "hidden"
    out_dir.mkdir(parents=True, exist_ok=True)

    ss = load_task_set_episodes(
        runs, model="sonnet", environment=ENV_SPREADSHEET, task_set=SONNET_SS_TASK_SET
    )
    assign_ss_classes(
        ss,
        public_root=public_root,
        dataset_path=dataset_path,
        hidden_root=hidden_root if hidden_root.is_dir() else None,
        replay=replay,
    )
    fa = load_task_set_episodes(
        runs, model="sonnet", environment=ENV_SPREADSHEET, task_set=FA_TASK_SET
    )
    source_class = {
        ep.run_id: ep.error_class
        for ep in ss
        if ep.candidate_success == 0.0
    }
    for episode in fa:
        if episode.source_run_id and episode.source_run_id in source_class:
            episode.error_class = source_class[episode.source_run_id]
            episode.class_source = "fa_source_replay"
        elif episode.candidate_correct_flag is False or episode.candidate_success == 0.0:
            episode.error_class = source_class.get(episode.source_run_id or "", REPLAY_FAILED)
            episode.class_source = "fa_source_replay"

    swe_v035: list[CoverageEpisode] = []
    if swe_runs is not None and swe_runs.is_dir():
        ids = parse_status_run_ids(swe_status) if swe_status and swe_status.is_file() else None
        swe_v035 = load_task_set_episodes(
            swe_runs,
            model="sonnet",
            environment=ENV_SWE,
            task_set=SONNET_SWE_V035,
            run_ids=ids,
        )
        assign_swe_classes(swe_v035)

    swe_v3: list[CoverageEpisode] = []
    if v3_swe_runs is not None and v3_swe_runs.is_dir():
        swe_v3 = load_task_set_episodes(
            v3_swe_runs, model="sonnet", environment=ENV_SWE, task_set=SONNET_SWE_V3
        )
        assign_swe_classes(swe_v3)

    c4: list[dict[str, Any]] = []
    for cell in ("P0", "P1"):
        c4.append(c4_from_episodes(ss, model="sonnet", environment="spreadsheet_main_v04", cell=cell))
    fa_p0 = [ep for ep in fa if ep.source_condition == "P0"]
    fa_p1 = [ep for ep in fa if ep.source_condition == "P1"]
    for arm in ("evidence_withheld", "evidence_supplied"):
        for cell, subset in (("P0-origin", fa_p0), ("P1-origin", fa_p1)):
            armed = [ep for ep in subset if ep.arm == arm]
            # Treat FA outcomes as parent-on-fixed-candidate: candidate_success from flag.
            rows = []
            for ep in armed:
                cand = 1.0 if ep.candidate_correct_flag else 0.0 if ep.candidate_correct_flag is False else ep.candidate_success
                rows.append({
                    "candidate_success": cand,
                    "final_success": ep.final_success,
                    "error_class": ep.error_class,
                })
            rates = c4_rates(rows, visible_fn=is_visible_ss)
            rates.update({
                "model": "sonnet",
                "environment": f"fixed_artifact:{arm}",
                "cell": cell,
            })
            c4.append(rates)
    for cell in ("P0", "P1"):
        if swe_v035:
            c4.append(c4_from_episodes(
                swe_v035, model="sonnet", environment="swe_main_v035", cell=cell, visible_fn=is_visible_swe
            ))
    for cell in ("P0", "P1"):
        if swe_v3:
            c4.append(c4_from_episodes(
                swe_v3, model="sonnet", environment="swe_v3_main", cell=cell, visible_fn=is_visible_swe
            ))

    ss_wrong = [ep for ep in ss if ep.condition in ("P0", "P1") and ep.candidate_success == 0.0]
    fa_wrong_sources = [ep for ep in ss_wrong]
    c1_ss = cell_class_counts(ss_wrong)
    c1_fa = cell_class_counts(fa_wrong_sources)

    _write_csv(out_dir / "c4_marginal_coverage.csv", c4)
    _write_json(out_dir / "c4_marginal_coverage.json", c4)
    _write_csv(out_dir / "c1_sonnet_spreadsheet.csv", c1_ss)
    _write_csv(out_dir / "c1_fa_source.csv", c1_fa)
    _write_csv(
        out_dir / "c1_sonnet_wrong_candidates.csv",
        [
            {
                "task_id": ep.task_id,
                "cell": ep.condition,
                "run_id": ep.run_id,
                "error_class": ep.error_class,
                "class_source": ep.class_source,
                "replay_reason": ep.replay_reason or "",
                "final_success": ep.final_success,
                "ran_candidate": ep.ran_candidate,
            }
            for ep in ss_wrong
        ],
    )
    _write_csv(out_dir / "c2_sonnet_spreadsheet.csv", c2_rows(ss))
    fa_incorrect = [
        ep for ep in fa
        if ep.candidate_correct_flag is False or (ep.source_run_id in source_class)
    ]
    _write_csv(out_dir / "c2_fa.csv", c2_rows(fa_incorrect if fa_incorrect else fa))

    c3_index: list[str] = []
    cases_dir = out_dir / "c3_cases"
    for ep in ss:
        if ep.condition != "P1" or ep.candidate_success != 0.0:
            continue
        events = _load_jsonl(Path(ep.run_dir) / "events.jsonl")
        page = cases_dir / f"sonnet_p1_{ep.task_id}_{ep.run_id[:8]}.md"
        write_c3_page(
            page,
            title=f"Sonnet P1 {ep.task_id}",
            episode=ep,
            observations=parent_auto_observations(events),
            terminal=extract_parent_terminal_message(events, Path(ep.run_dir)),
        )
        c3_index.append(f"- [Sonnet P1 {ep.task_id}]({page.relative_to(out_dir)}) class=`{ep.error_class}` ran=`{ep.ran_candidate}`")
    for ep in fa:
        if ep.candidate_correct_flag is not False and ep.source_run_id not in source_class:
            continue
        events = _load_jsonl(Path(ep.run_dir) / "events.jsonl")
        slug = f"fa_{ep.task_id}_{ep.source_condition}_{ep.arm}_{ep.run_id[:8]}.md"
        page = cases_dir / slug
        write_c3_page(
            page,
            title=f"FA {ep.arm} {ep.task_id} origin {ep.source_condition}",
            episode=ep,
            observations=parent_auto_observations(events),
            terminal=extract_parent_terminal_message(events, Path(ep.run_dir)),
        )
        c3_index.append(
            f"- [FA {ep.task_id} {ep.arm}]({page.relative_to(out_dir)}) "
            f"origin=`{ep.source_condition}` class=`{ep.error_class}` ran=`{ep.ran_candidate}`"
        )

    c2_notes = []
    c2_notes.extend(_contrast_notes(ss, "Sonnet spreadsheet main_v04"))
    c2_notes.extend(_contrast_notes(
        [ep for ep in fa if ep.arm == "evidence_withheld" and (ep.candidate_correct_flag is False or ep.source_run_id in source_class)],
        "FA withheld (incorrect sources)",
    ))
    c2_notes.extend(_contrast_notes(
        [ep for ep in fa if ep.arm == "evidence_supplied" and (ep.candidate_correct_flag is False or ep.source_run_id in source_class)],
        "FA supplied (incorrect sources)",
    ))

    c5_rows: list[dict[str, Any]] = []
    for task_set, bundle in ((SONNET_SWE_V035, swe_v035), (SONNET_SWE_V3, swe_v3)):
        if not bundle:
            continue
        _write_csv(out_dir / f"c5_{task_set}.csv", c2_rows(bundle))
        _write_csv(out_dir / f"c1_swe_{task_set}.csv", cell_class_counts(
            [ep for ep in bundle if ep.condition in ("P0", "P1") and (ep.candidate_success == 0.0 or ep.error_class == "no_patch")]
        ))
        counts = cell_class_counts(
            [ep for ep in bundle if ep.condition in ("P0", "P1") and (ep.candidate_success == 0.0 or (ep.candidate_success is None and ep.error_class == "no_patch"))]
        )
        for row in counts:
            c5_rows.append({
                "model": "sonnet",
                "task_set": task_set,
                **row,
            })

    extras = [
        "## Notes",
        "",
        f"- Spreadsheet A1 verified: P0 wrong=8, P1 wrong=4 (main_v04).",
        f"- FA incorrect sources: {len(source_class)} (expected 12 = 8 P0 + 4 P1).",
        "- FA C1 classes are the source-candidate replays, not a second Sonnet re-analysis.",
        "- Luna lives on the model2 branch and is not pooled here.",
        f"- Replay used: `{replay}`.",
    ]
    write_digest(
        out_dir / "COVERAGE_DIGEST.md",
        model_label="Sonnet (main)",
        c4_rows=c4,
        c1_blocks=[
            ("Sonnet spreadsheet main_v04", c1_ss),
            ("FA source candidates (same 12 incorrect scripts)", c1_fa),
            ("Sonnet SWE main_v035", [
                row for row in c5_rows if row.get("task_set") == SONNET_SWE_V035
            ]),
            ("Sonnet SWE v3 main", [
                row for row in c5_rows if row.get("task_set") == SONNET_SWE_V3
            ]),
        ],
        c2_notes=c2_notes,
        c3_index=c3_index,
        c5_rows=c5_rows,
        extras=extras,
    )
    _write_json(
        out_dir / "coverage_summary.json",
        {
            "model": "sonnet",
            "n_ss": len(ss),
            "n_ss_wrong": len(ss_wrong),
            "n_fa": len(fa),
            "c4": c4,
            "c1_ss": c1_ss,
        },
    )


def generate_luna(
    *,
    repo: Path,
    out_dir: Path,
    replay: bool,
) -> None:
    runs = repo / "data" / "runs"
    public_root = repo / "data" / "public"
    dataset_path = public_root / "dataset.json"
    hidden_root = repo / "data" / "hidden"
    out_dir.mkdir(parents=True, exist_ok=True)

    ss = load_task_set_episodes(
        runs, model="luna", environment=ENV_SPREADSHEET, task_set=LUNA_SS_TASK_SET
    )
    # A1 P1 is hashed n=23; two missing-hash cells are excluded from wrong-set rates.
    hashed_p1 = [
        ep for ep in ss
        if ep.condition == "P1" and ep.candidate_hash
    ]
    missing_hash_p1 = [
        ep for ep in ss
        if ep.condition == "P1" and not ep.candidate_hash
    ]
    assign_ss_classes(
        [ep for ep in ss if ep.candidate_hash or ep.condition == "P0"],
        public_root=public_root,
        dataset_path=dataset_path,
        hidden_root=hidden_root if hidden_root.is_dir() else None,
        replay=replay,
    )
    swe = load_task_set_episodes(
        runs, model="luna", environment=ENV_SWE, task_set=LUNA_SWE_TASK_SET
    )
    assign_swe_classes(swe)

    c4: list[dict[str, Any]] = []
    p0 = [ep for ep in ss if ep.condition == "P0"]
    c4.append(c4_from_episodes(p0, model="luna", environment="spreadsheet_main_v04_m2", cell="P0"))
    c4.append(c4_from_episodes(hashed_p1, model="luna", environment="spreadsheet_main_v04_m2", cell="P1"))
    for cell in ("P0", "P1"):
        c4.append(c4_from_episodes(
            swe, model="luna", environment="swe_main_m2", cell=cell, visible_fn=is_visible_swe
        ))

    ss_wrong = [
        ep for ep in ss
        if ep.condition in ("P0", "P1")
        and ep.candidate_success == 0.0
        and (ep.condition == "P0" or ep.candidate_hash)
    ]
    c1_ss = cell_class_counts(ss_wrong)
    _write_csv(out_dir / "c4_marginal_coverage.csv", c4)
    _write_json(out_dir / "c4_marginal_coverage.json", c4)
    _write_csv(out_dir / "c1_luna_spreadsheet.csv", c1_ss)
    _write_csv(
        out_dir / "c1_luna_wrong_candidates.csv",
        [
            {
                "task_id": ep.task_id,
                "cell": ep.condition,
                "run_id": ep.run_id,
                "error_class": ep.error_class,
                "class_source": ep.class_source,
                "replay_reason": ep.replay_reason or "",
                "final_success": ep.final_success,
                "ran_candidate": ep.ran_candidate,
                "candidate_hash": ep.candidate_hash or "",
            }
            for ep in ss_wrong
        ],
    )
    _write_csv(out_dir / "c2_luna_spreadsheet.csv", c2_rows(ss))
    _write_csv(out_dir / "c2_luna_swe.csv", c2_rows(swe))

    c3_index: list[str] = []
    cases_dir = out_dir / "c3_cases"
    for ep in ss_wrong:
        if ep.condition != "P1":
            continue
        events = _load_jsonl(Path(ep.run_dir) / "events.jsonl")
        page = cases_dir / f"luna_p1_{ep.task_id}_{ep.run_id[:8]}.md"
        write_c3_page(
            page,
            title=f"Luna P1 {ep.task_id}",
            episode=ep,
            observations=parent_auto_observations(events),
            terminal=extract_parent_terminal_message(events, Path(ep.run_dir)),
        )
        c3_index.append(
            f"- [Luna P1 {ep.task_id}]({page.relative_to(out_dir)}) class=`{ep.error_class}` ran=`{ep.ran_candidate}`"
        )
    # Luna 6239 P1 is the missing-hash / no-candidate case co-author A asked about.
    for ep in missing_hash_p1:
        events = _load_jsonl(Path(ep.run_dir) / "events.jsonl")
        page = cases_dir / f"luna_p1_{ep.task_id}_{ep.run_id[:8]}_excluded_missing_hash.md"
        write_c3_page(
            page,
            title=f"Luna P1 {ep.task_id} (excluded from A1: missing hash)",
            episode=ep,
            observations=parent_auto_observations(events),
            terminal=extract_parent_terminal_message(events, Path(ep.run_dir)),
        )
        c3_index.append(
            f"- [Luna P1 {ep.task_id} EXCLUDED missing-hash]({page.relative_to(out_dir)}) "
            f"submitted=`{ep.worker_submitted}` ran=`{ep.ran_candidate}`"
        )

    c5_counts = cell_class_counts(
        [ep for ep in swe if ep.condition in ("P0", "P1") and (ep.candidate_success == 0.0 or ep.error_class == "no_patch")]
    )
    c5_rows = [{"model": "luna", "task_set": LUNA_SWE_TASK_SET, **row} for row in c5_counts]
    _write_csv(out_dir / "c1_swe_swe_main_m2.csv", c5_counts)

    extras = [
        "## Notes",
        "",
        "- A1 verified: P0 wrong=4/25; P1 wrong=2/23 hashed (two missing-hash P1 cells excluded).",
        f"- Missing-hash P1 tasks: {', '.join(ep.task_id for ep in missing_hash_p1) or 'none'}.",
        "- Luna 6239 P1 is the excluded missing-hash case (no submitted candidate), not one of the two A1 P1 wrongs.",
        "- Sonnet lives on main and is not pooled here.",
        f"- Replay used: `{replay}`.",
    ]
    write_digest(
        out_dir / "COVERAGE_DIGEST.md",
        model_label="Luna (model2)",
        c4_rows=c4,
        c1_blocks=[
            ("Luna spreadsheet main_v04_m2", c1_ss),
            ("Luna SWE swe_main_m2", c5_counts),
        ],
        c2_notes=_contrast_notes(ss, "Luna spreadsheet main_v04_m2")
        + _contrast_notes(
            [ep for ep in swe if ep.condition in ("P0", "P1") and (ep.candidate_success == 0.0 or ep.error_class == "no_patch")],
            "Luna SWE swe_main_m2",
        ),
        c3_index=c3_index,
        c5_rows=c5_rows,
        extras=extras,
    )
    _write_json(
        out_dir / "coverage_summary.json",
        {
            "model": "luna",
            "n_ss": len(ss),
            "n_ss_wrong": len(ss_wrong),
            "n_missing_hash_p1": len(missing_hash_p1),
            "c4": c4,
            "c1_ss": c1_ss,
        },
    )


def write_combined_c4_digest(
    *,
    sonnet_dir: Path,
    luna_dir: Path,
    dest: Path,
) -> None:
    """Write C4-first combined digest. Models stay labeled; never pooled."""
    sonnet_c4 = _load_json(sonnet_dir / "c4_marginal_coverage.json")
    luna_c4 = _load_json(luna_dir / "c4_marginal_coverage.json")
    if not isinstance(sonnet_c4, list):
        sonnet_c4 = []
    if not isinstance(luna_c4, list):
        luna_c4 = []
    assert_single_model(sonnet_c4)
    assert_single_model(luna_c4)
    sonnet_page = sonnet_dir / "COVERAGE_DIGEST_sonnet.md"
    if not sonnet_page.is_file():
        sonnet_page = sonnet_dir / "COVERAGE_DIGEST.md"
    luna_page = luna_dir / "COVERAGE_DIGEST_luna.md"
    if not luna_page.is_file():
        luna_page = luna_dir / "COVERAGE_DIGEST.md"
    sonnet_digest = sonnet_page.read_text(encoding="utf-8")
    luna_digest = luna_page.read_text(encoding="utf-8")
    headers = [
        "model",
        "environment",
        "P cell",
        "n",
        "candidate correct",
        "repair among wrong",
        "damage among correct",
        "net (final − candidate)",
        "visible wrong / n_wrong",
    ]
    lines = [
        "# Coverage digest — combined (models labeled, not pooled)",
        "",
        "Sonnet and Luna are **different models; not pooled.** The C4 table is a",
        "juxtaposition of separately computed rows. No cell is a pooled estimate.",
        "",
        "## C4 — Marginal coverage (paper table)",
        "",
        _md_table(headers, _c4_table_rows(list(sonnet_c4) + list(luna_c4))),
        "",
        "Visible-wrong (spreadsheet) = `crash` | `error_cells` | `empty`.",
        "Visible-wrong (SWE analogue) = `no_patch` | `apply_fail` | `tests_fail_loud`.",
        "",
        "---",
        "",
        "## Sonnet + FA + SWE (from main)",
        "",
        sonnet_digest,
        "",
        "---",
        "",
        "## Luna (from model2)",
        "",
        luna_digest,
        "",
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_json(dest.parent / "c4_combined.json", list(sonnet_c4) + list(luna_c4))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("sonnet", "luna", "combine"), required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--replay", action="store_true", help="Replay spreadsheet candidates on a clean workbook")
    parser.add_argument("--swe-runs", type=Path, default=None)
    parser.add_argument("--swe-status", type=Path, default=None)
    parser.add_argument("--v3-swe-runs", type=Path, default=None)
    parser.add_argument("--sonnet-dir", type=Path, default=None)
    parser.add_argument("--luna-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    out_dir = (args.out_dir or (repo / "paper_outputs" / "coverage")).resolve()
    if args.model == "combine":
        sonnet_dir = args.sonnet_dir or (SONNET_ROOT / "paper_outputs" / "coverage")
        luna_dir = args.luna_dir or (LUNA_ROOT / "paper_outputs" / "coverage")
        write_combined_c4_digest(
            sonnet_dir=sonnet_dir,
            luna_dir=luna_dir,
            dest=out_dir / "COVERAGE_DIGEST_combined.md",
        )
        return 0
    if args.model == "sonnet":
        generate_sonnet(
            repo=repo,
            out_dir=out_dir,
            replay=args.replay,
            swe_runs=args.swe_runs,
            swe_status=args.swe_status,
            v3_swe_runs=args.v3_swe_runs,
        )
    else:
        generate_luna(repo=repo, out_dir=out_dir, replay=args.replay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
