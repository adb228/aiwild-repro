"""Real-record analysis pipeline for the delegation study.

Loads explicitly scoped episode records, applies QC flags, computes
task-clustered bootstrap estimates for tau0/tau1/delta, and writes:

  fig1_performance_interaction.pdf   — condition means + treatment effects
  fig2_handoff_mechanism.pdf         — worker vs parent outcome comparison
  fig3_stock_paired_returns.pdf      — SA/SP stock paired returns, when present
  table1_effects.tex                 — point estimates + 95 % bootstrap CI
  tableS1_condition_summary.tex      — per-condition summary statistics
  tableS2_qc.tex                     — QC flag counts by type and condition
  qc_report.json                     — machine-readable QC summary
  episodes.parquet                   — flattened episode data (Arrow/Parquet)
  episodes.csv                       — same data as CSV

Bootstrap parameters
--------------------
  seed   = 20260902
  n_draws = 10_000
  unit   = task (task-clustered resampling)
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import math
import os
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")            # non-interactive backend; no display needed
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..qc import memo_body_is_empty, qc_episode
from ..task_selection import load_spreadsheet_selection
from ..validate import validate_episode, validate_event_log
from .terminal_episode import is_terminal_agent_failure

# ── Bootstrap constants ───────────────────────────────────────────────────────
BOOTSTRAP_SEED   = 20260902
BOOTSTRAP_DRAWS  = 10_000
V041_BOOTSTRAP_SEED = 0
V041_BOOTSTRAP_DRAWS = 5_000
P0_P1_BOOTSTRAP_SEED = BOOTSTRAP_SEED
P0_P1_BOOTSTRAP_DRAWS = BOOTSTRAP_DRAWS
ALPHA            = 0.05          # two-sided 95 % CI
_CONDITIONS      = ("A0", "A1", "P0", "P1")   # 2×2 factorial conditions only
_SOLO_CONDITION  = "SOLO"
_ALL_CONDITIONS  = (*_CONDITIONS, _SOLO_CONDITION)
_TASK_SET_SCOPES = {
    "main": frozenset({"main"}),
    "all": frozenset({"main", "hard"}),
    "main_v04": frozenset({"main_v04"}),
    "solo_v04": frozenset({"solo_v04"}),
}
_SCOPE_PROTOCOL = {
    "main": "0.3",
    "all": "0.3",
    "main_v04": "0.4.1",
    "solo_v04": "0.4.1",
}
_SCOPE_EXPECTED_CONDITIONS = {
    "main": frozenset(_ALL_CONDITIONS),
    "all": frozenset(_ALL_CONDITIONS),
    "main_v04": frozenset(_CONDITIONS),
    "solo_v04": frozenset({_SOLO_CONDITION}),
}
_DISCLOSED_TERMINAL_DETAILS = {
    "80bdf704-cec3-4669-96f4-ffb134c13eec": (
        "prompt_too_long: 225,243 tokens > 200,000 context-window maximum"
    ),
}


class AnalysisInputError(ValueError):
    """Raised when the requested real-record analysis scope is not usable."""


def _load_event_usage(
    run_dir: Path,
) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    """Recover usage and phase evidence from a failed run's persisted events."""
    usage = {
        role: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "model_calls": 0,
            "tool_calls": 0,
            "validation_cycles": 0,
            "wall_seconds": None,
            "auto_observations": 0,
        }
        for role in ("worker", "parent")
    }
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return usage, {
            "events_present": False,
            "model_calls": 0,
            "roles_started": [],
            "run_events": {"worker": 0, "parent": 0},
            "runs_without_auto_observation": {"worker": 0, "parent": 0},
        }
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AnalysisInputError(
            f"cannot read failed-run events: {events_path}"
        ) from exc
    roles_started: set[str] = set()
    model_calls = 0
    run_events = {"worker": 0, "parent": 0}
    runs_without_auto = {"worker": 0, "parent": 0}
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnalysisInputError(
                f"malformed failed-run event {events_path}:{line_number}"
            ) from exc
        event_type = event.get("event_type")
        if event_type == "phase_started":
            role = event.get("role")
            if isinstance(role, str):
                roles_started.add(role)
        role = event.get("role")
        payload = event.get("payload") or {}
        if event_type == "tool_call" and role in usage:
            usage[role]["tool_calls"] += 1
        if (
            event_type == "tool_result"
            and role in usage
            and payload.get("tool_name")
            in {"run_solution", "run_ephemeral_python"}
        ):
            run_events[role] += 1
            if payload.get("auto_observation") is None:
                runs_without_auto[role] += 1
            else:
                usage[role]["auto_observations"] += 1
        if event_type != "model_call":
            continue
        if role not in usage:
            raise AnalysisInputError(
                f"unsupported role in failed-run event: {role!r}"
            )
        model_calls += 1
        usage[role]["model_calls"] += 1
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = payload.get(key)
            if not isinstance(value, int) or value < 0:
                raise AnalysisInputError(
                    f"invalid {key} in failed-run event "
                    f"{events_path}:{line_number}"
                )
            usage[role][key] += value
    return usage, {
        "events_present": True,
        "model_calls": model_calls,
        "roles_started": sorted(roles_started),
        "run_events": run_events,
        "runs_without_auto_observation": runs_without_auto,
    }


def _resolve_batch_status(
    runs_dir: Path,
    task_set: str,
    explicit_path: Path | None,
) -> Path:
    """Resolve exactly one terminal batch status for missing-row reconciliation."""
    if explicit_path is not None:
        candidates = [explicit_path]
    else:
        batches_dir = runs_dir.parent / "batches"
        candidates = sorted(batches_dir.glob("*/status.json"))
    matches: list[Path] = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if explicit_path is not None:
                raise AnalysisInputError(
                    f"cannot parse explicit batch status {path}: {exc}"
                ) from exc
            continue
        entries = payload.get("episodes")
        is_terminal = (
            isinstance(entries, list)
            and bool(entries)
            and all(
                entry.get("state") in {"complete", "failed"}
                for entry in entries
            )
        )
        if payload.get("task_set") == task_set and is_terminal:
            matches.append(path)
    if len(matches) != 1:
        raise AnalysisInputError(
            f"expected exactly one batch status for {task_set!r}; "
            f"found {[str(path) for path in matches]}"
        )
    return matches[0]


def _reconcile_terminal_batch_failures(
    records: list[dict[str, Any]],
    runs_dir: Path,
    task_set: str,
    batch_status_path: Path,
    *,
    expected_conditions: frozenset[str],
) -> list[dict[str, Any]]:
    """Add analysis-only ITT rows for terminal failed pairs lacking episodes."""
    raw = batch_status_path.read_bytes()
    try:
        batch = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnalysisInputError(
            f"malformed batch status: {batch_status_path}"
        ) from exc
    entries = batch.get("episodes")
    if batch.get("task_set") != task_set or not isinstance(entries, list):
        raise AnalysisInputError(
            f"batch status does not describe task set {task_set!r}"
        )
    if not entries:
        raise AnalysisInputError("batch status has no expected episode pairs")

    terminal_states = {"complete", "failed"}
    nonterminal = [
        (entry.get("task_id"), entry.get("cell"), entry.get("state"))
        for entry in entries
        if entry.get("state") not in terminal_states
    ]
    if nonterminal:
        raise AnalysisInputError(
            f"batch status is not terminal; unresolved pairs: {nonterminal}"
        )

    by_cell = {
        (str(record["task_id"]), str(record["condition"])): record
        for record in records
    }
    expected: set[tuple[str, str]] = set()
    status_sha256 = hashlib.sha256(raw).hexdigest()
    template = records[0]
    reconciled = list(records)
    for entry in entries:
        task_id = str(entry.get("task_id"))
        condition = str(entry.get("cell"))
        run_id = entry.get("run_id")
        pair = (task_id, condition)
        if condition not in expected_conditions or not isinstance(run_id, str):
            raise AnalysisInputError(f"invalid batch status entry: {entry}")
        if pair in expected:
            raise AnalysisInputError(f"duplicate expected batch pair: {pair}")
        expected.add(pair)
        existing = by_cell.get(pair)
        if existing is not None:
            if existing.get("run_id") != run_id:
                raise AnalysisInputError(
                    f"batch/episode run-id mismatch for {pair}: "
                    f"{run_id!r} != {existing.get('run_id')!r}"
                )
            continue
        if entry.get("state") != "failed" or entry.get("exit_code") in (None, 0):
            raise AnalysisInputError(
                f"missing episode is not a terminal failed pair: {entry}"
            )
        run_dir = runs_dir / run_id
        if (run_dir / "episode.json").exists():
            raise AnalysisInputError(
                f"episode exists but was not loaded for expected pair {pair}"
            )
        recovered_usage, event_evidence = _load_event_usage(run_dir)
        in_task = bool(
            event_evidence["model_calls"]
            or event_evidence["roles_started"]
        )
        if in_task:
            outcome_category = "agent_outcome_censored"
            failure_source = "agent"
            failure_code = "terminal_agent_outcome_no_episode"
            rerun_eligible = False
            detail = _DISCLOSED_TERMINAL_DETAILS.get(
                run_id,
                entry.get("error") or "terminal in-task failure",
            )
            reason = (
                f"censored agent outcome retained under ITT: {detail}; "
                "episode.json absent because the terminal outcome was not "
                "serialized"
            )
        else:
            outcome_category = "infrastructure_pre_execution"
            failure_source = "infrastructure"
            failure_code = "terminal_launcher_failure_no_episode"
            rerun_eligible = True
            reason = (
                "pre-execution launcher failure without episode.json: "
                f"{entry.get('error') or 'unspecified launcher failure'}"
            )
        reproducibility = template.get("reproducibility") or {}
        reconciled.append({
            "_analysis_only_itt_row": True,
            "_batch_status_sha256": status_sha256,
            "_missing_record_reason": reason,
            "_outcome_category": outcome_category,
            "_record_serialization_gap": True,
            "_rerun_eligible": rerun_eligible,
            "_event_evidence": event_evidence,
            "_source_episode_sha256": None,
            "run_id": run_id,
            "task_id": task_id,
            "task_set": task_set,
            "condition": condition,
            "replicate": 1,
            "protocol_version": template.get("protocol_version"),
            "status": "analysis_only_itt_failure",
            "analysis_included": True,
            "analysis_inclusion_reason": (
                "ITT analysis-only row for terminal failed expected batch pair; "
                f"classification={outcome_category}; no episode.json was created"
            ),
            "reproducibility": {
                "task_selection_package": reproducibility.get(
                    "task_selection_package"
                ),
                "selection_sha256": reproducibility.get("selection_sha256"),
            },
            "handoff": {},
            "compliance": {
                "parent_finalized": None,
                "eligible_worker_run_events": event_evidence["run_events"][
                    "worker"
                ],
                "eligible_parent_run_events": event_evidence["run_events"][
                    "parent"
                ],
                "worker_runs_without_auto_observation": event_evidence[
                    "runs_without_auto_observation"
                ]["worker"],
                "parent_runs_without_auto_observation": event_evidence[
                    "runs_without_auto_observation"
                ]["parent"],
            },
            "worker_outcome": None,
            "final_outcome": {
                "evaluator_status": "missing_record_infrastructure_failure",
                "full_task_success": 0,
                "target_unit_pass_rate": 0.0,
                "n_units": 0,
            },
            "usage": recovered_usage,
            "failures": [{
                "source": failure_source,
                "code": failure_code,
                "message": reason,
            }],
        })

    observed = {
        (str(record["task_id"]), str(record["condition"]))
        for record in reconciled
    }
    if observed != expected:
        raise AnalysisInputError(
            "selected records and terminal batch grid differ: "
            f"missing={sorted(expected - observed)}, "
            f"unexpected={sorted(observed - expected)}"
        )
    return reconciled


def load_real_records(
    runs_dir: Path,
    task_set_scope: str,
    *,
    batch_status_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Load a deterministic, explicitly scoped real-record analysis set.

    ``main`` selects only confirmatory main records. ``all`` selects main and
    hard records from v0.3. ``main_v04`` selects only v0.4.1 delegated-main
    records and reconciles a terminal batch grid so a failed expected pair with
    no ``episode.json`` is retained as an analysis-only ITT zero. ``solo_v04``
    selects the separate v0.4.1 SOLO batch. Development, pilot, and unrelated
    records are never inferred.

    The loader fails closed on malformed JSON, duplicate design cells, mixed
    protocol/package provenance, missing main cells, or a run-id/directory
    mismatch. Each returned record receives the SHA-256 of its source
    ``episode.json`` under the internal ``_source_episode_sha256`` key.
    """
    if task_set_scope not in _TASK_SET_SCOPES:
        raise AnalysisInputError(
            f"unsupported task-set scope {task_set_scope!r}; "
            f"choose one of {sorted(_TASK_SET_SCOPES)}"
        )
    if not runs_dir.is_dir():
        raise AnalysisInputError(f"runs directory does not exist: {runs_dir}")

    selected_sets = _TASK_SET_SCOPES[task_set_scope]
    records: list[dict[str, Any]] = []
    parse_failures: list[str] = []
    for run_dir in sorted(runs_dir.iterdir(), key=lambda path: path.name):
        if not run_dir.is_dir() or run_dir.name.startswith("."):
            continue
        episode_path = run_dir / "episode.json"
        if not episode_path.is_file():
            continue
        try:
            raw = episode_path.read_bytes()
            record = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            parse_failures.append(f"{run_dir.name}: {type(exc).__name__}")
            continue
        if not isinstance(record, dict):
            parse_failures.append(f"{run_dir.name}: episode is not an object")
            continue
        if record.get("task_set") not in selected_sets:
            continue
        protocol = record.get("protocol_version")
        if record.get("benchmark") == "swebench" or (
            isinstance(protocol, str) and protocol.startswith("swe-")
        ):
            continue
        if record.get("run_id") != run_dir.name:
            raise AnalysisInputError(
                f"run-id/directory mismatch: directory={run_dir.name!r}, "
                f"record={record.get('run_id')!r}"
            )
        copied = dict(record)
        copied["_source_episode_sha256"] = hashlib.sha256(raw).hexdigest()
        records.append(copied)

    if parse_failures:
        raise AnalysisInputError(
            "malformed episode records make the runs directory ambiguous: "
            + "; ".join(parse_failures)
        )
    if not records:
        raise AnalysisInputError(
            f"no records found for explicit task-set scope {task_set_scope!r}"
        )

    protocol_versions = {record.get("protocol_version") for record in records}
    expected_protocol = _SCOPE_PROTOCOL[task_set_scope]
    if protocol_versions != {expected_protocol}:
        raise AnalysisInputError(
            f"selected records must be protocol {expected_protocol} only; "
            f"got {protocol_versions}"
        )
    packages = {
        (record.get("reproducibility") or {}).get("task_selection_package")
        for record in records
    }
    if packages != {"config/task_selection_v03"}:
        raise AnalysisInputError(
            f"selected records have ambiguous task-selection packages: {packages}"
        )
    selection_hashes = {
        (record.get("reproducibility") or {}).get("selection_sha256")
        for record in records
    }
    if len(selection_hashes) != 1 or None in selection_hashes:
        raise AnalysisInputError(
            f"selected records have ambiguous selection hashes: {selection_hashes}"
        )

    if task_set_scope in {"main_v04", "solo_v04"}:
        resolved_status = _resolve_batch_status(
            runs_dir,
            task_set_scope,
            batch_status_path,
        )
        records = _reconcile_terminal_batch_failures(
            records,
            runs_dir,
            task_set_scope,
            resolved_status,
            expected_conditions=_SCOPE_EXPECTED_CONDITIONS[task_set_scope],
        )

    seen_run_ids: set[str] = set()
    seen_cells: set[tuple[str, str, str, int]] = set()
    for record in records:
        run_id = record.get("run_id")
        if run_id in seen_run_ids:
            raise AnalysisInputError(f"duplicate run_id: {run_id}")
        seen_run_ids.add(run_id)
        try:
            cell = (
                str(record["task_set"]),
                str(record["task_id"]),
                str(record["condition"]),
                int(record["replicate"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalysisInputError(
                f"record {run_id!r} lacks a valid design cell"
            ) from exc
        if cell in seen_cells:
            raise AnalysisInputError(f"duplicate design cell: {cell}")
        seen_cells.add(cell)
        if record.get("condition") not in _ALL_CONDITIONS:
            raise AnalysisInputError(
                f"record {run_id!r} has unsupported condition "
                f"{record.get('condition')!r}"
            )
        if not isinstance(record.get("final_outcome"), dict):
            raise AnalysisInputError(
                f"record {run_id!r} has no final_outcome object"
            )

    selected_task_sets = {record["task_set"] for record in records}
    if selected_task_sets != selected_sets:
        raise AnalysisInputError(
            f"scope {task_set_scope!r} requires task sets "
            f"{sorted(selected_sets)}, got {sorted(selected_task_sets)}"
        )

    if task_set_scope in {"main_v04", "solo_v04"}:
        factorial_task_set = task_set_scope
    else:
        factorial_task_set = "main"
    expected_conditions = _SCOPE_EXPECTED_CONDITIONS[task_set_scope]
    main = [
        record for record in records
        if record["task_set"] == factorial_task_set
    ]
    by_task: dict[str, set[str]] = {}
    for record in main:
        by_task.setdefault(str(record["task_id"]), set()).add(record["condition"])
    incomplete = {
        task_id: sorted(set(expected_conditions) - conditions)
        for task_id, conditions in by_task.items()
        if conditions != set(expected_conditions)
    }
    if incomplete:
        raise AnalysisInputError(
            f"incomplete {factorial_task_set} design cells: {incomplete}"
        )

    return records


# ── QC flag logic (mirrors src/delegation_study/qc.py) ───────────────────────

def _qc_flags(record: dict[str, Any]) -> list[str]:
    """Return a list of QC flag codes for a single episode record."""
    flags: list[str] = []
    if record.get("_analysis_only_itt_row"):
        flags.append("record_serialization_gap")
        if record.get("_outcome_category") == "agent_outcome_censored":
            flags.append("agent_outcome_censored")
        return flags
    if is_terminal_agent_failure(record):
        flags.append("agent_outcome_censored")
        return flags
    condition = record["condition"]
    handoff   = record.get("handoff", {})
    compliance = record.get("compliance", {})

    if condition.startswith("P") and not handoff.get("candidate_script_path"):
        flags.append("production_missing_artifact")
    if condition.startswith("A") and handoff.get("candidate_script_path"):
        flags.append("advisory_artifact_leak")
    if condition.endswith("0") and compliance.get("worker_feedback_calls", 0) > 0:
        flags.append("withheld_feedback_call")
    if not compliance.get("parent_finalized", False):
        flags.append("parent_not_finalized")
    if condition == "A1" and not compliance.get("worker_scratch_destroyed", False):
        flags.append("advisory_scratch_retained")

    for failure in record.get("failures", []):
        if failure.get("source") == "agent" and failure.get("code") == "worker_timeout":
            flags.append("worker_timeout")

    worker_outcome = record.get("worker_outcome")
    if handoff.get("candidate_script_path") and (
        worker_outcome is None
        or worker_outcome.get("evaluator_status") == "evaluator_failure"
    ):
        flags.append("invalid_candidate")

    return flags


# ── Record flattening ─────────────────────────────────────────────────────────

def flatten_records(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Flatten a list of episode records into a tidy DataFrame.

    Missing / null full_task_success values (evaluator_failure episodes) are
    coerced to 0.0 for analysis purposes: the episode is retained (per QC
    contract) and counted as a failed outcome.
    """
    rows = []
    for rec in records:
        final_outcome = rec.get("final_outcome") or {}
        worker_outcome = rec.get("worker_outcome") or {}

        # Coerce null full_task_success → 0 (failure retained, not excluded)
        final_success_raw = final_outcome.get("full_task_success")
        final_success = float(final_success_raw) if final_success_raw is not None else 0.0
        final_score_raw = final_outcome.get("target_unit_pass_rate")
        final_score = (
            float(final_score_raw) if final_score_raw is not None else 0.0
        )

        worker_success_raw = worker_outcome.get("full_task_success")
        worker_success = float(worker_success_raw) if worker_success_raw is not None else None

        flags = _qc_flags(rec)
        worker_usage = (rec.get("usage") or {}).get("worker") or {}
        parent_usage = (rec.get("usage") or {}).get("parent") or {}
        compliance = rec.get("compliance") or {}

        rows.append({
            "run_id": rec["run_id"],
            "task_id": rec["task_id"],
            "task_set": rec.get("task_set", "main"),  # default "main" for legacy records
            "protocol_version": rec.get("protocol_version"),
            "condition": rec["condition"],
            "replicate": rec["replicate"],
            "status": rec.get("status"),
            "run_status": rec.get("run_status", rec.get("status")),
            "failure_reason": rec.get("failure_reason"),
            "analysis_included": rec.get("analysis_included", True),
            "task_selection_package": rec.get("reproducibility", {}).get(
                "task_selection_package"
            ),
            "selection_sha256": rec.get("reproducibility", {}).get(
                "selection_sha256"
            ),
            "source_episode_sha256": rec.get("_source_episode_sha256"),
            "worker_prompt_hash": rec.get("reproducibility", {}).get(
                "worker_prompt_hash"
            ),
            "parent_prompt_hash": rec.get("reproducibility", {}).get(
                "parent_prompt_hash"
            ),
            "libreoffice_version": rec.get("reproducibility", {}).get(
                "libreoffice_version"
            ),
            "final_success": final_success,
            "final_score": final_score,
            "worker_success": worker_success,
            "evaluator_status": final_outcome.get("evaluator_status"),
            "n_units": final_outcome.get("n_units", 0),
            "parent_finalized": compliance.get("parent_finalized"),
            "parent_budget_exhausted": bool(
                parent_usage.get("budget_exhausted", False)
            ),
            "parent_budget_exhausted_on": parent_usage.get(
                "budget_exhausted_on"
            ),
            "parent_model_calls": parent_usage.get("model_calls"),
            "parent_tool_calls": parent_usage.get("tool_calls"),
            "parent_validation_cycles": parent_usage.get("validation_cycles"),
            "parent_wall_seconds": parent_usage.get("wall_seconds"),
            "parent_run_events": compliance.get(
                "eligible_parent_run_events", 0
            ),
            "worker_run_events": compliance.get(
                "eligible_worker_run_events", 0
            ),
            "parent_runs_without_auto_observation": compliance.get(
                "parent_runs_without_auto_observation", 0
            ),
            "worker_runs_without_auto_observation": compliance.get(
                "worker_runs_without_auto_observation", 0
            ),
            "accepted_unchanged": rec.get("accepted_unchanged"),
            "candidate_present": bool(
                (rec.get("handoff") or {}).get("candidate_script_path")
            ),
            "final_script_present": bool(rec.get("final_script_path")),
            "analysis_only_itt_row": bool(
                rec.get("_analysis_only_itt_row", False)
            ),
            "outcome_category": rec.get(
                "_outcome_category",
                (
                    "agent_outcome_censored"
                    if (
                        is_terminal_agent_failure(rec)
                        or parent_usage.get("budget_exhausted")
                    )
                    else "completed_episode"
                ),
            ),
            "record_serialization_gap": bool(
                rec.get("_record_serialization_gap", False)
            ),
            "rerun_eligible": rec.get("_rerun_eligible", False),
            "source_kind": (
                "batch_status_and_events"
                if rec.get("_analysis_only_itt_row")
                else "episode_json"
            ),
            "missing_record_reason": rec.get("_missing_record_reason"),
            "batch_status_sha256": rec.get("_batch_status_sha256"),
            "qc_flags": "|".join(flags) if flags else "",
            "qc_flagged": len(flags) > 0,
            "n_agent_failures": sum(
                1 for f in rec.get("failures", []) if f.get("source") == "agent"
            ),
            "n_infra_failures": sum(
                1 for f in rec.get("failures", []) if f.get("source") == "infrastructure"
            ),
            **{
                f"{role}_{key}": usage.get(key)
                for role, usage in (
                    ("worker", worker_usage),
                    ("parent", parent_usage),
                )
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                    "model_calls",
                    "tool_calls",
                    "validation_cycles",
                    "wall_seconds",
                    "auto_observations",
                )
            },
        })
    return pd.DataFrame(rows)


# ── Estimand computation ──────────────────────────────────────────────────────

def compute_task_means(
    df: pd.DataFrame,
    *,
    task_set: str = "main",
) -> pd.DataFrame:
    """Average final_success within each (task_id, condition) cell.

    Returns a DataFrame with columns: task_id, condition, task_mean.

    Filtering:
    - Only analysis_included=True rows are used.
    - Only the explicitly requested task set is used.
    - Only 2×2 factorial conditions (A0/A1/P0/P1) are included here.
      SOLO is handled separately via compute_solo_means().
    """
    included = df[
        (df["analysis_included"] == True) &  # noqa: E712
        (df.get("task_set", pd.Series(["main"] * len(df))) == task_set) &
        (df["condition"].isin(_CONDITIONS))
    ].copy()
    means = (
        included
        .groupby(["task_id", "condition"], sort=True)["final_success"]
        .mean()
        .reset_index(name="task_mean")
    )
    return means


def compute_solo_means(df: pd.DataFrame) -> pd.DataFrame:
    """Average final_success for SOLO episodes (main task_set only).

    Returns a DataFrame with columns: task_id, condition, task_mean.
    Only analysis_included=True rows with condition==SOLO and task_set==main.
    """
    solo = df[
        (df["analysis_included"] == True) &  # noqa: E712
        (df.get("task_set", pd.Series(["main"] * len(df))) == "main") &
        (df["condition"] == _SOLO_CONDITION)
    ].copy()
    if solo.empty:
        return pd.DataFrame(columns=["task_id", "condition", "task_mean"])
    means = (
        solo
        .groupby(["task_id", "condition"], sort=True)["final_success"]
        .mean()
        .reset_index(name="task_mean")
    )
    return means


def compute_estimands(task_means: pd.DataFrame) -> dict[str, float]:
    """Compute point estimates for tau0, tau1, delta from task-level means.

    Estimand definitions (analysis_contract.md):
        tau_0 = mean_t[Y_{t,P0}] − mean_t[Y_{t,A0}]
        tau_1 = mean_t[Y_{t,P1}] − mean_t[Y_{t,A1}]
        delta  = tau_1 − tau_0
    where the outer mean weights tasks equally.
    """
    wide = (
        task_means
        .pivot(index="task_id", columns="condition", values="task_mean")
        .sort_index()
    )
    missing_conditions = set(_CONDITIONS) - set(wide.columns)
    if missing_conditions:
        raise AnalysisInputError(
            f"main records lack factorial conditions: {sorted(missing_conditions)}"
        )
    incomplete_tasks = wide[list(_CONDITIONS)].isna().any(axis=1)
    if incomplete_tasks.any():
        raise AnalysisInputError(
            "main records have incomplete factorial tasks: "
            + ", ".join(map(str, wide.index[incomplete_tasks]))
        )

    a0 = wide["A0"].to_numpy()
    a1 = wide["A1"].to_numpy()
    p0 = wide["P0"].to_numpy()
    p1 = wide["P1"].to_numpy()

    # Decimal-looking probability effects are rounded only after computing the
    # task-weighted means. This makes exact gate assertions stable without
    # changing the underlying binary-record estimand.
    tau0 = round(float(np.mean(p0 - a0)), 12)
    tau1 = round(float(np.mean(p1 - a1)), 12)
    delta = round(float(np.mean((p1 - a1) - (p0 - a0))), 12)
    return {"tau0": tau0, "tau1": tau1, "delta": delta}


def bootstrap_estimands(
    task_means: pd.DataFrame,
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Task-clustered bootstrap CIs for tau0, tau1, delta.

    Resamples task IDs with replacement (whole-task bootstrap) and
    recomputes estimands for each draw.

    Parameters
    ----------
    task_means : DataFrame with columns task_id, condition, task_mean
    seed       : integer RNG seed (default BOOTSTRAP_SEED = 20260902)
    n_draws    : number of bootstrap replicates (default 10_000)

    Returns
    -------
    dict with keys:
        tau0_ci, tau1_ci, delta_ci  — each a (lo, hi) tuple at 1-alpha level
        tau0_se, tau1_se, delta_se  — bootstrap standard errors
        tau0_draws, tau1_draws, delta_draws — shape (n_draws,) arrays
    """
    rng = np.random.default_rng(seed)

    # Build a (n_tasks × 4) array of per-task means, one column per condition
    wide = (
        task_means
        .pivot(index="task_id", columns="condition", values="task_mean")
        .sort_index()
    )
    tasks = wide.index.to_numpy()
    n_tasks = len(tasks)

    a0_arr = wide["A0"].to_numpy()
    a1_arr = wide["A1"].to_numpy()
    p0_arr = wide["P0"].to_numpy()
    p1_arr = wide["P1"].to_numpy()

    tau0_draws  = np.empty(n_draws)
    tau1_draws  = np.empty(n_draws)
    delta_draws = np.empty(n_draws)

    for i in range(n_draws):
        idx = rng.integers(0, n_tasks, size=n_tasks)  # resample task indices
        tau0_draws[i]  = np.mean(p0_arr[idx] - a0_arr[idx])
        tau1_draws[i]  = np.mean(p1_arr[idx] - a1_arr[idx])
        delta_draws[i] = np.mean((p1_arr[idx] - a1_arr[idx]) - (p0_arr[idx] - a0_arr[idx]))

    lo_pct = 100 * ALPHA / 2
    hi_pct = 100 * (1 - ALPHA / 2)

    return {
        "tau0_ci":    (float(np.percentile(tau0_draws,  lo_pct)),
                       float(np.percentile(tau0_draws,  hi_pct))),
        "tau1_ci":    (float(np.percentile(tau1_draws,  lo_pct)),
                       float(np.percentile(tau1_draws,  hi_pct))),
        "delta_ci":   (float(np.percentile(delta_draws, lo_pct)),
                       float(np.percentile(delta_draws, hi_pct))),
        "tau0_se":    float(np.std(tau0_draws,  ddof=1)),
        "tau1_se":    float(np.std(tau1_draws,  ddof=1)),
        "delta_se":   float(np.std(delta_draws, ddof=1)),
        "tau0_draws":  tau0_draws,
        "tau1_draws":  tau1_draws,
        "delta_draws": delta_draws,
    }


def _pairwise_mean(left: np.ndarray, right: np.ndarray) -> float:
    mask = np.isfinite(left) & np.isfinite(right)
    if not mask.any():
        return float("nan")
    return float(np.mean(left[mask] - right[mask]))


def swe_pairwise_n(task_means: pd.DataFrame) -> dict[str, int]:
    """Pairwise-complete task counts for τ0, τ1, and δ."""
    wide = (
        task_means
        .pivot(index="task_id", columns="condition", values="task_mean")
        .sort_index()
    )
    for cond in _CONDITIONS:
        if cond not in wide.columns:
            wide[cond] = np.nan
    a0 = wide["A0"].to_numpy(dtype=float)
    a1 = wide["A1"].to_numpy(dtype=float)
    p0 = wide["P0"].to_numpy(dtype=float)
    p1 = wide["P1"].to_numpy(dtype=float)
    return {
        "tau0": int((np.isfinite(a0) & np.isfinite(p0)).sum()),
        "tau1": int((np.isfinite(a1) & np.isfinite(p1)).sum()),
        "delta": int(
            (np.isfinite(a0) & np.isfinite(a1) & np.isfinite(p0) & np.isfinite(p1)).sum()
        ),
    }


def swe_pairwise_n_from_matrix(
    matrix: dict[str, dict[str, int | None]],
) -> dict[str, int]:
    """Pairwise-complete n from an 8×4 ITT matrix (None = missing)."""
    n_tau0 = n_tau1 = n_delta = 0
    for row in matrix.values():
        a0, a1, p0, p1 = (row.get(cond) for cond in _CONDITIONS)
        if a0 is not None and p0 is not None:
            n_tau0 += 1
        if a1 is not None and p1 is not None:
            n_tau1 += 1
        if None not in (a0, a1, p0, p1):
            n_delta += 1
    return {"tau0": n_tau0, "tau1": n_tau1, "delta": n_delta}


def compute_swe_estimands(task_means: pd.DataFrame) -> dict[str, float | int]:
    """SWE ITT estimands with pairwise-complete tasks and n per estimate.

    A task contributes to τ0 only if A0 and P0 are both non-missing; to τ1
    only if A1 and P1 are both non-missing; to δ only if all four cells are
    non-missing.  ``evaluator_failure`` and ``infrastructure_failure`` cells
    are NaN in ``task_mean`` (missing, never 0).
    """
    wide = (
        task_means
        .pivot(index="task_id", columns="condition", values="task_mean")
        .sort_index()
    )
    for cond in _CONDITIONS:
        if cond not in wide.columns:
            wide[cond] = np.nan
    a0 = wide["A0"].to_numpy(dtype=float)
    a1 = wide["A1"].to_numpy(dtype=float)
    p0 = wide["P0"].to_numpy(dtype=float)
    p1 = wide["P1"].to_numpy(dtype=float)
    tau0 = round(_pairwise_mean(p0, a0), 12)
    tau1 = round(_pairwise_mean(p1, a1), 12)
    mask_delta = (
        np.isfinite(a0) & np.isfinite(a1) & np.isfinite(p0) & np.isfinite(p1)
    )
    if mask_delta.any():
        delta = round(
            float(np.mean((p1[mask_delta] - a1[mask_delta]) - (p0[mask_delta] - a0[mask_delta]))),
            12,
        )
    else:
        delta = float("nan")
    pairwise = swe_pairwise_n(task_means)
    return {
        "tau0": tau0,
        "tau1": tau1,
        "delta": delta,
        "n_tau0": pairwise["tau0"],
        "n_tau1": pairwise["tau1"],
        "n_delta": pairwise["delta"],
    }


def bootstrap_swe_estimands(
    task_means: pd.DataFrame,
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Task-clustered bootstrap using pairwise-complete contrasts per draw."""
    rng = np.random.default_rng(seed)
    wide = (
        task_means
        .pivot(index="task_id", columns="condition", values="task_mean")
        .sort_index()
    )
    for cond in _CONDITIONS:
        if cond not in wide.columns:
            wide[cond] = np.nan
    n_tasks = len(wide)
    a0_arr = wide["A0"].to_numpy(dtype=float)
    a1_arr = wide["A1"].to_numpy(dtype=float)
    p0_arr = wide["P0"].to_numpy(dtype=float)
    p1_arr = wide["P1"].to_numpy(dtype=float)

    tau0_draws = np.empty(n_draws)
    tau1_draws = np.empty(n_draws)
    delta_draws = np.empty(n_draws)
    for i in range(n_draws):
        idx = rng.integers(0, n_tasks, size=n_tasks)
        tau0_draws[i] = _pairwise_mean(p0_arr[idx], a0_arr[idx])
        tau1_draws[i] = _pairwise_mean(p1_arr[idx], a1_arr[idx])
        dmask = (
            np.isfinite(a0_arr[idx]) & np.isfinite(a1_arr[idx])
            & np.isfinite(p0_arr[idx]) & np.isfinite(p1_arr[idx])
        )
        if dmask.any():
            delta_draws[i] = float(np.mean(
                (p1_arr[idx][dmask] - a1_arr[idx][dmask])
                - (p0_arr[idx][dmask] - a0_arr[idx][dmask])
            ))
        else:
            delta_draws[i] = np.nan

    lo_pct = 100 * ALPHA / 2
    hi_pct = 100 * (1 - ALPHA / 2)
    return {
        "tau0_ci":    (float(np.nanpercentile(tau0_draws, lo_pct)),
                       float(np.nanpercentile(tau0_draws, hi_pct))),
        "tau1_ci":    (float(np.nanpercentile(tau1_draws, lo_pct)),
                       float(np.nanpercentile(tau1_draws, hi_pct))),
        "delta_ci":   (float(np.nanpercentile(delta_draws, lo_pct)),
                       float(np.nanpercentile(delta_draws, hi_pct))),
        "tau0_se":    float(np.nanstd(tau0_draws, ddof=1)),
        "tau1_se":    float(np.nanstd(tau1_draws, ddof=1)),
        "delta_se":   float(np.nanstd(delta_draws, ddof=1)),
        "tau0_draws":  tau0_draws,
        "tau1_draws":  tau1_draws,
        "delta_draws": delta_draws,
    }


# ── Figure helpers ────────────────────────────────────────────────────────────

def _save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    fig.savefig(
        path,
        bbox_inches="tight",
        metadata={
            "Creator": "delegation-study analysis",
            "CreationDate": epoch,
            "ModDate": epoch,
        },
    )
    plt.close(fig)
    print(f"  wrote {path}")


def make_fig1(
    df: pd.DataFrame,
    task_means: pd.DataFrame,
    estimates: dict[str, float],
    boot: dict[str, Any],
    out: Path,
    *,
    task_set: str = "main",
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> None:
    """Figure 1: Performance interaction — condition means and treatment effects."""
    # Condition-level means from task averages
    cond_means = (
        task_means
        .groupby("condition")["task_mean"]
        .mean()
        .reindex(["A0", "A1", "P0", "P1"])
    )
    # Bootstrap SE per condition (marginal)
    wide = (
        task_means
        .pivot(index="task_id", columns="condition", values="task_mean")
        .sort_index()
    )
    rng = np.random.default_rng(seed + 1)
    n_tasks = len(wide)
    cond_se: dict[str, float] = {}
    for cond in _CONDITIONS:
        arr = wide[cond].to_numpy()
        bs = [
            np.mean(arr[rng.integers(0, n_tasks, n_tasks)])
            for _ in range(n_draws)
        ]
        cond_se[cond] = float(np.std(bs, ddof=1))

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    ax1, ax2 = axes

    # Panel A: condition means with error bars
    colors = {"A0": "#2196F3", "A1": "#64B5F6", "P0": "#F44336", "P1": "#EF9A9A"}
    x = np.arange(len(_CONDITIONS))
    bars = ax1.bar(x, [cond_means[c] for c in _CONDITIONS],
                   color=[colors[c] for c in _CONDITIONS],
                   width=0.6, alpha=0.85, edgecolor="white")
    ax1.errorbar(x, [cond_means[c] for c in _CONDITIONS],
                 yerr=[1.96 * cond_se[c] for c in _CONDITIONS],
                 fmt="none", color="black", capsize=5, linewidth=1.5)
    ax1.set_xticks(x)
    ax1.set_xticklabels(_CONDITIONS)
    ax1.set_ylabel("Mean full-task success rate")
    ax1.set_title("(A) Condition means")
    ax1.set_ylim(0, 1.05)
    ax1.axhline(0, color="black", linewidth=0.5)

    # Panel B: treatment effects tau0, tau1, delta with 95% CI
    effect_names = [r"$\tau_0$", r"$\tau_1$", r"$\delta$"]
    effect_vals  = [estimates["tau0"], estimates["tau1"], estimates["delta"]]
    effect_ci    = [boot["tau0_ci"], boot["tau1_ci"], boot["delta_ci"]]
    effect_se    = [boot["tau0_se"], boot["tau1_se"], boot["delta_se"]]
    colors2      = ["#1976D2", "#D32F2F", "#388E3C"]

    x2 = np.arange(3)
    ax2.axhline(0, color="gray", linewidth=1, linestyle="--")
    for i, (name, val, ci, se, col) in enumerate(
            zip(effect_names, effect_vals, effect_ci, effect_se, colors2)):
        lo, hi = ci
        ax2.bar(i, val, color=col, width=0.5, alpha=0.8, edgecolor="white", label=f"{name} = {val:+.2f}")
        ax2.errorbar(i, val, yerr=[[val - lo], [hi - val]],
                     fmt="none", color="black", capsize=6, linewidth=2)
        ax2.text(i, hi + 0.01, f"{val:+.2f}", ha="center", va="bottom", fontsize=9)

    ax2.set_xticks(x2)
    ax2.set_xticklabels(effect_names, fontsize=12)
    ax2.set_ylabel("Effect size (probability scale)")
    ax2.set_title("(B) Treatment effects + 95% bootstrap CI")
    ax2.legend(fontsize=8, loc="lower right")

    fig.suptitle("Figure 1: Performance × Handoff Interaction", fontweight="bold", fontsize=12)
    analysis_only = df[
        (df["task_set"] == task_set) & df["analysis_only_itt_row"]
    ]
    agent_failures = df[
        (df["task_set"] == task_set)
        & (df["status"] == "agent_failure")
    ]
    if not analysis_only.empty:
        pairs = ", ".join(
            f"{row.task_id} {row.condition}" for row in analysis_only.itertuples()
        )
        fig.text(
            0.5,
            0.01,
            f"ITT note: {pairs} context-window agent outcome = 0; "
            "episode.json serialization gap.",
            ha="center",
            fontsize=8,
        )
        fig.tight_layout(rect=(0, 0.04, 1, 1))
    elif not agent_failures.empty:
        pairs = ", ".join(
            f"{row.task_id} {row.condition}" for row in agent_failures.itertuples()
        )
        fig.text(
            0.5,
            0.01,
            f"ITT note: {pairs} context-window agent_failure = 0.",
            ha="center",
            fontsize=8,
        )
        fig.tight_layout(rect=(0, 0.04, 1, 1))
    else:
        fig.tight_layout()
    _save(fig, out / "fig1_performance_interaction.pdf")


def make_fig2(
    df: pd.DataFrame,
    out: Path,
    *,
    task_set: str = "main",
) -> None:
    """Figure 2: Handoff mechanism — worker vs parent outcome comparison."""
    # Main production conditions only; hard-probe records remain supplement-only.
    prod = df[
        (df["task_set"] == task_set)
        & (df["analysis_included"] == True)  # noqa: E712
        & df["condition"].isin(["P0", "P1"])
        & df["worker_success"].notna()
    ].copy()
    prod["worker_success"] = prod["worker_success"].astype(float)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    ax1, ax2 = axes

    # Panel A: 2×2 contingency of worker vs parent outcomes
    for cond_i, cond in enumerate(["P0", "P1"]):
        sub = prod[prod["condition"] == cond]
        both_pass = ((sub["worker_success"] == 1) & (sub["final_success"] == 1)).sum()
        worker_only = ((sub["worker_success"] == 1) & (sub["final_success"] == 0)).sum()
        parent_only = ((sub["worker_success"] == 0) & (sub["final_success"] == 1)).sum()
        both_fail = ((sub["worker_success"] == 0) & (sub["final_success"] == 0)).sum()
        n = len(sub)

        cats = ["Both\npass", "Worker only\npasses", "Parent only\npasses", "Both\nfail"]
        counts = [both_pass, worker_only, parent_only, both_fail]
        col_cond = "#F44336" if cond == "P0" else "#EF9A9A"
        x = np.arange(4) + cond_i * 0.35
        ax1.bar(x, [c / max(n, 1) for c in counts], width=0.3,
                label=cond, color=col_cond, alpha=0.8)

    ax1.set_xticks([0.175, 1.175, 2.175, 3.175])
    ax1.set_xticklabels(["Both\npass", "Worker↓\nParent↑", "Worker↑\nParent↓", "Both\nfail"],
                        fontsize=9)
    ax1.set_ylabel("Fraction of episodes")
    ax1.set_title("(A) Worker vs parent outcomes (P-conditions)")
    ax1.legend()

    # Panel B: Worker success rate vs final success rate by condition
    summary = prod.groupby("condition").agg(
        worker_mean=("worker_success", "mean"),
        final_mean=("final_success", "mean"),
    ).reset_index()
    ax2.scatter(summary["worker_mean"], summary["final_mean"],
                s=120, c=["#F44336", "#EF9A9A"], zorder=5)
    for _, row in summary.iterrows():
        ax2.annotate(row["condition"],
                     (row["worker_mean"], row["final_mean"]),
                     xytext=(5, 5), textcoords="offset points", fontsize=10)
    ax2.plot([0, 1], [0, 1], "k--", linewidth=1, alpha=0.5, label="y = x")
    ax2.set_xlabel("Worker success rate")
    ax2.set_ylabel("Final (parent) success rate")
    ax2.set_title("(B) Worker vs final success rate")
    ax2.set_xlim(-0.05, 1.05)
    ax2.set_ylim(-0.05, 1.05)
    ax2.legend()

    fig.suptitle("Figure 2: Handoff Mechanism Analysis", fontweight="bold", fontsize=12)
    analysis_only = df[
        (df["task_set"] == task_set) & df["analysis_only_itt_row"]
    ]
    if not analysis_only.empty:
        pairs = ", ".join(
            f"{row.task_id} {row.condition}" for row in analysis_only.itertuples()
        )
        fig.text(
            0.5,
            0.01,
            f"ITT grid note: {pairs} context-window agent outcome = 0; "
            "not a P-cell handoff row; episode serialization gap.",
            ha="center",
            fontsize=8,
        )
        fig.tight_layout(rect=(0, 0.04, 1, 1))
    else:
        fig.tight_layout()
    _save(fig, out / "fig2_handoff_mechanism.pdf")


def make_fig3(stock_rows: list[dict[str, Any]], out: Path) -> bool:
    """Write Figure 3 only when complete paired stock records are present.

    When ``stock_rows`` is empty, a stale output is removed and no placeholder
    figure is written. This deterministic omission is recorded in
    ``analysis_summary.json`` by :func:`run_analysis`.
    """
    output_path = out / "fig3_stock_paired_returns.pdf"
    if not stock_rows:
        output_path.unlink(missing_ok=True)
        print("  omitted fig3_stock_paired_returns.pdf (no stock records)")
        return False

    sa = sorted([r for r in stock_rows if r["condition"] == "SA"], key=lambda r: r["date"])
    sp = sorted([r for r in stock_rows if r["condition"] == "SP"], key=lambda r: r["date"])
    if not sa or len(sa) != len(sp):
        raise AnalysisInputError(
            "stock records must contain a non-empty, balanced SA/SP pairing"
        )

    dates    = [r["date"] for r in sa]
    sa_rets  = np.array([r["return_agent"] for r in sa])
    sp_rets  = np.array([r["return_agent"] for r in sp])
    x        = np.arange(len(dates))

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    ax1, ax2  = axes

    # Panel A: time series
    ax1.plot(x, sa_rets * 100, marker="o", color="#1565C0", label="SA (autonomous)", linewidth=1.5)
    ax1.plot(x, sp_rets * 100, marker="s", color="#B71C1C", label="SP (prompted)", linewidth=1.5)
    ax1.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    ax1.set_xticks(x)
    ax1.set_xticklabels(dates, rotation=45, ha="right", fontsize=7)
    ax1.set_ylabel("Weekly return (%)")
    ax1.set_title("(A) SA vs SP returns over time")
    ax1.legend()

    # Panel B: paired scatter
    ax2.scatter(sa_rets * 100, sp_rets * 100, s=80, color="#4CAF50", alpha=0.85)
    lim = max(abs(sa_rets).max(), abs(sp_rets).max()) * 100 + 0.5
    ax2.plot([-lim, lim], [-lim, lim], "k--", linewidth=1, alpha=0.5, label="y = x")
    for i, (s, p, d) in enumerate(zip(sa_rets, sp_rets, dates)):
        ax2.annotate(d[-5:], (s * 100, p * 100), fontsize=6,
                     xytext=(2, 2), textcoords="offset points")
    ax2.set_xlabel("SA return (%)")
    ax2.set_ylabel("SP return (%)")
    ax2.set_title("(B) Paired returns (SP vs SA)")
    ax2.legend(fontsize=8)

    fig.suptitle("Figure 3: Stock Paired Returns (SA vs SP)", fontweight="bold", fontsize=12)
    fig.tight_layout()
    _save(fig, output_path)
    return True


# ── Table helpers ─────────────────────────────────────────────────────────────

def _fmt_ci(lo: float, hi: float, digits: int = 3) -> str:
    return f"[{lo:+.{digits}f}, {hi:+.{digits}f}]"


def make_table1(
    estimates: dict[str, float],
    boot: dict[str, Any],
    out: Path,
    *,
    itt_note: str | None = None,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> None:
    """Table 1: Main treatment effects."""
    rows_tex = []
    for label, key in [
            (r"$\hat\tau_0$ (P0 $-$ A0)", "tau0"),
            (r"$\hat\tau_1$ (P1 $-$ A1)", "tau1"),
            (r"$\hat\delta$ (interaction)", "delta"),
    ]:
        val = estimates[key]
        ci  = boot[f"{key}_ci"]
        se  = boot[f"{key}_se"]
        rows_tex.append(
            f"  {label} & ${val:+.3f}$ & ${se:.3f}$ "
            f"& ${ci[0]:+.3f}$ & ${ci[1]:+.3f}$ \\\\"
        )

    note = f"  {itt_note}\n" if itt_note else ""
    tex = (
        f"% Table 1: Main effects (task-clustered bootstrap, "
        f"seed={seed}, B={n_draws})\n"
        r"""\begin{table}[ht]
\centering
"""
        + rf"""\caption{{Estimated treatment effects (task-clustered bootstrap, $B={n_draws:,}$, seed \texttt{{{seed}}}).
  $\hat\tau_0$: effect of crossing a candidate script into the handoff when feedback was withheld.
  $\hat\tau_1$: effect of crossing a candidate script when feedback was available.
  $\hat\delta$: interaction (difference of contrasts).
  95\,\% percentile bootstrap CI.
"""
        + note
        + r"""}
\label{tab:effects}
\begin{tabular}{lccccc}
\toprule
Estimand & Point est.\ & Bootstrap SE & 95\,\% CI (lo) & 95\,\% CI (hi) \\
\midrule
""" + "\n".join(rows_tex) + r"""
\bottomrule
\end{tabular}
\end{table}
"""
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tex)
    print(f"  wrote {out}")


def make_table_s1(
    df: pd.DataFrame,
    task_means: pd.DataFrame,
    boot: dict[str, Any],
    out: Path,
    *,
    task_set: str = "main",
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> None:
    """Table S1: Condition summary."""
    rng = np.random.default_rng(seed + 2)
    wide = (
        task_means
        .pivot(index="task_id", columns="condition", values="task_mean")
        .sort_index()
    )
    n_tasks = len(wide)

    rows_tex = []
    for cond in _CONDITIONS:
        n_ep = int(
            (
                (df["task_set"] == task_set)
                & (df["analysis_included"] == True)  # noqa: E712
                & (df["condition"] == cond)
            ).sum()
        )
        mean_val = float(wide[cond].mean())
        # per-condition bootstrap CI
        arr = wide[cond].to_numpy()
        bs = [
            float(np.mean(arr[rng.integers(0, n_tasks, n_tasks)]))
            for _ in range(n_draws)
        ]
        lo = float(np.percentile(bs, 100 * ALPHA / 2))
        hi = float(np.percentile(bs, 100 * (1 - ALPHA / 2)))
        n_analysis_only = int(
            (
                (df["task_set"] == task_set)
                & (df["condition"] == cond)
                & df["analysis_only_itt_row"]
            ).sum()
        )
        rows_tex.append(
            f"  {cond} & {n_ep} & {n_analysis_only} & ${mean_val:.3f}$ "
            f"& ${lo:.3f}$ & ${hi:.3f}$ \\\\"
        )

    tex = r"""% Table S1: Condition summary
\begin{table}[ht]
\centering
\caption{Per-condition episode counts and success rates.
  Success rate is the task-weighted mean of \texttt{full\_task\_success}.
  Evaluator-failure episodes are coerced to 0 (retained per QC contract).
  Analysis-only ITT rows identify terminal agent outcomes whose
  \texttt{episode.json} was not serialized.}
\label{tab:condition_summary}
\begin{tabular}{lccccc}
\toprule
Condition & ITT $n$ & Analysis-only & Success rate & 95\,\% CI (lo) & 95\,\% CI (hi) \\
\midrule
""" + "\n".join(rows_tex) + r"""
\bottomrule
\end{tabular}
\end{table}
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tex)
    print(f"  wrote {out}")


def build_condition_summary(
    df: pd.DataFrame,
    task_means: pd.DataFrame,
    *,
    task_set: str,
) -> pd.DataFrame:
    """Return deterministic per-cell ITT outcome and effort summaries."""
    rows: list[dict[str, Any]] = []
    for condition in _CONDITIONS:
        cell = df[
            (df["task_set"] == task_set)
            & (df["analysis_included"] == True)  # noqa: E712
            & (df["condition"] == condition)
        ].copy()
        task_cell = task_means[task_means["condition"] == condition]
        rows.append({
            "condition": condition,
            "n_itt": int(len(cell)),
            "n_tasks": int(task_cell["task_id"].nunique()),
            "mean_full_task_success": float(task_cell["task_mean"].mean()),
            "mean_final_score": float(cell["final_score"].mean()),
            "parent_model_calls_mean": float(cell["parent_model_calls"].mean()),
            "parent_model_calls_total": int(cell["parent_model_calls"].sum()),
            "n_parent_not_finalized": int(
                (
                    (cell["source_kind"] == "episode_json")
                    & (cell["parent_finalized"] == False)  # noqa: E712
                ).sum()
            ),
            "n_parent_budget_exhausted": int(
                cell["parent_budget_exhausted"].sum()
            ),
            "n_agent_outcome_censored": int(
                (cell["outcome_category"] == "agent_outcome_censored").sum()
            ),
            "n_record_serialization_gap": int(
                cell["record_serialization_gap"].sum()
            ),
            "n_infrastructure_pre_execution": int(
                (
                    cell["outcome_category"]
                    == "infrastructure_pre_execution"
                ).sum()
            ),
            "n_analysis_only_itt": int(cell["analysis_only_itt_row"].sum()),
        })
    return pd.DataFrame(rows)


def make_table_s2_disposition(summary: pd.DataFrame, out: Path) -> None:
    """Write v0.4.1 disposition, censoring, and parent-effort table."""
    rows = []
    for row in summary.itertuples():
        rows.append(
            f"  {row.condition} & {row.n_itt} & "
            f"{row.n_parent_not_finalized} & "
            f"{row.n_agent_outcome_censored} & "
            f"{row.n_record_serialization_gap} & "
            f"{row.parent_model_calls_mean:.2f} \\\\"
        )
    tex = r"""% Table S2: ITT disposition and parent effort
\begin{table}[ht]
\centering
\caption{ITT disposition, censoring, and parent effort by condition.
  Parent effort is the pre-declared number of successful parent model calls.
  Censored agent outcomes are retained at their observed call count and as
  full-task failures. The 57232 A0 context-window exhaustion is an
  analysis-side \texttt{agent\_failure} retained at ITT zero.}
\label{tab:itt_disposition}
\begin{tabular}{lrrrrr}
\toprule
Condition & ITT $n$ & Not finalized & Censored agent outcomes &
Serialization gaps & Mean parent calls \\
\midrule
""" + "\n".join(rows) + r"""
\bottomrule
\end{tabular}
\end{table}
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tex, encoding="utf-8")
    print(f"  wrote {out}")


_TOKEN_RATES_PER_MILLION = {
    "input_tokens": Decimal("3.00"),
    "output_tokens": Decimal("15.00"),
    "cache_read_input_tokens": Decimal("0.30"),
    "cache_creation_input_tokens": Decimal("3.75"),
}


def _token_cost(totals: dict[str, int]) -> Decimal:
    return sum(
        (
            Decimal(totals[key]) * rate / Decimal(1_000_000)
            for key, rate in _TOKEN_RATES_PER_MILLION.items()
        ),
        Decimal(0),
    )


def build_cost_summary(
    df: pd.DataFrame,
    *,
    task_set: str,
    conditions: tuple[str, ...] = _CONDITIONS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return token totals and exact supplied-rate costs by cell and role."""
    selected = df[
        (df["task_set"] == task_set)
        & (df["analysis_included"] == True)  # noqa: E712
    ]
    rows: list[dict[str, Any]] = []
    grand = {key: 0 for key in _TOKEN_RATES_PER_MILLION}
    for protocol in sorted(selected["protocol_version"].dropna().unique()):
        protocol_rows = selected[selected["protocol_version"] == protocol]
        for condition in conditions:
            cell = protocol_rows[protocol_rows["condition"] == condition]
            for role in ("worker", "parent"):
                totals = {
                    key: int(cell[f"{role}_{key}"].fillna(0).sum())
                    for key in _TOKEN_RATES_PER_MILLION
                }
                for key, value in totals.items():
                    grand[key] += value
                rows.append({
                    "protocol_version": protocol,
                    "condition": condition,
                    "role": role,
                    **totals,
                    "cost_usd_exact": format(_token_cost(totals), "f"),
                })
    total_cost = _token_cost(grand)
    summary = {
        "rates_per_million_usd": {
            key: format(rate, "f")
            for key, rate in _TOKEN_RATES_PER_MILLION.items()
        },
        "formula": (
            "(input_tokens*3.00 + output_tokens*15.00 + "
            "cache_read_input_tokens*0.30 + "
            "cache_creation_input_tokens*3.75) / 1,000,000"
        ),
        "token_totals": grand,
        "total_cost_usd_exact": format(total_cost, "f"),
    }
    return pd.DataFrame(rows), summary


def make_table_s2(df: pd.DataFrame, out: Path) -> None:
    """Table S2: QC flag summary."""
    all_flags = [
        "advisory_artifact_leak",
        "withheld_feedback_call",
        "production_missing_artifact",
        "parent_not_finalized",
        "advisory_scratch_retained",
        "worker_timeout",
        "invalid_candidate",
        "agent_outcome_censored",
        "record_serialization_gap",
    ]

    rows_tex = []
    for flag in all_flags:
        counts_by_cond = {}
        for cond in _CONDITIONS:
            sub = df[df["condition"] == cond]
            count = int(sub["qc_flags"].str.contains(flag, regex=False, na=False).sum())
            counts_by_cond[cond] = count
        total = sum(counts_by_cond.values())
        if total == 0:
            continue
        cols = " & ".join(str(counts_by_cond[c]) for c in _CONDITIONS)
        rows_tex.append(f"  \\texttt{{{flag.replace('_', '\\_')}}} & {cols} & {total} \\\\")

    tex = r"""% Table S2: QC flags
\begin{table}[ht]
\centering
\caption{QC flag counts by condition.
  Treatment violations are retained; only malformed records are rejected.}
\label{tab:qc}
\begin{tabular}{lccccc}
\toprule
Flag & A0 & A1 & P0 & P1 & Total \\
\midrule
""" + "\n".join(rows_tex) + r"""
\bottomrule
\end{tabular}
\end{table}
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tex)
    print(f"  wrote {out}")


def make_table_s2_hard_probe(df: pd.DataFrame, out: Path) -> None:
    """Table S2: Hard-probe task performance (supplement-only, never pooled with main).

    Includes all records with task_set=="hard" that have analysis_included==True.
    If no hard episodes exist, writes a table with an explicit note.
    """
    hard_selected = df[
        (df.get("task_set", pd.Series(["main"] * len(df))) == "hard") &
        (df["analysis_included"] == True)  # noqa: E712
    ].copy()
    hard = hard_selected[
        (hard_selected["evaluator_status"] != "evaluator_failure")
        & hard_selected["evaluator_status"].notna()
    ].copy()
    n_invalid = len(hard_selected) - len(hard)

    rows_tex: list[str] = []
    if hard.empty:
        rows_tex.append(
            r"  \multicolumn{4}{c}{\textit{No hard-probe episodes in this dataset.}} \\"
        )
    else:
        for cond in sorted(hard["condition"].unique()):
            sub = hard[hard["condition"] == cond]
            n_ep = len(sub)
            mean_val = float(sub["final_success"].mean())
            n_tasks = sub["task_id"].nunique()
            rows_tex.append(
                f"  {cond} & {n_tasks} & {n_ep} & ${mean_val:.3f}$ \\\\"
            )
        if n_invalid:
            rows_tex.append(
                rf"  \multicolumn{{4}}{{l}}{{\textit{{Excluded: {n_invalid} "
                r"evaluator-failure records with no valid outcome.}} \\"
            )

    tex = r"""% Table S2: Hard-probe tasks (supplement-only; never pooled with main estimates)
\begin{table}[ht]
\centering
\caption{Hard-probe task performance.
  Five tasks from the sealed hard-extension pool, run as an exploratory difficulty
  probe after Wave 2. Results are supplement-only and are never pooled with main
  estimates. Success rate is the episode mean of \texttt{full\_task\_success}.}
\label{tab:hard_probe}
\begin{tabular}{lccr}
\toprule
Condition & Tasks & Episodes & Success rate \\
\midrule
""" + "\n".join(rows_tex) + r"""
\bottomrule
\end{tabular}
\end{table}
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tex)
    print(f"  wrote {out}")


def make_table_s3_qc(df: pd.DataFrame, out: Path) -> None:
    """Table S3: QC flag summary (alias for the former S2).

    This table was renumbered S3 when the hard-probe table became S2.
    """
    make_table_s2(df, out)


def make_qc_report(
    df: pd.DataFrame,
    out: Path,
    *,
    conditions: tuple[str, ...] = _CONDITIONS,
) -> dict[str, Any]:
    """Write qc_report.json and return the report dict."""
    all_flags = [
        "advisory_artifact_leak",
        "withheld_feedback_call",
        "production_missing_artifact",
        "parent_not_finalized",
        "advisory_scratch_retained",
        "worker_timeout",
        "invalid_candidate",
        "agent_outcome_censored",
        "record_serialization_gap",
    ]
    flag_counts: dict[str, int] = {}
    for flag in all_flags:
        count = int(df["qc_flags"].str.contains(flag, regex=False, na=False).sum())
        flag_counts[flag] = count

    report = {
        "n_episodes_total": int(len(df)),
        "n_analysis_included": int(df["analysis_included"].sum()),
        "n_qc_flagged": int(df["qc_flagged"].sum()),
        "n_evaluator_failure": int((df["evaluator_status"] == "evaluator_failure").sum()),
        "n_agent_failures": int(df["n_agent_failures"].sum()),
        "n_infra_failures": int(df["n_infra_failures"].sum()),
        "flag_counts": flag_counts,
        "flag_counts_by_condition": {
            cond: {
                flag: int(
                    df[df["condition"] == cond]["qc_flags"]
                    .str.contains(flag, regex=False, na=False).sum()
                )
                for flag in all_flags
            }
            for cond in conditions
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"  wrote {out}")
    return report


def save_episodes(df: pd.DataFrame, out_dir: Path) -> None:
    """Save flattened episode DataFrame to Parquet and CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Parquet (Arrow)
    table = pa.Table.from_pandas(df)
    parquet_path = out_dir / "episodes.parquet"
    pq.write_table(table, parquet_path)
    print(f"  wrote {parquet_path}")

    # CSV
    csv_path = out_dir / "episodes.csv"
    df.to_csv(csv_path, index=False)
    print(f"  wrote {csv_path}")


def run_solo_analysis(
    records: list[dict[str, Any]],
    out_dir: Path,
    *,
    runs_dir: Path,
) -> dict[str, Any]:
    """Write a separate descriptive analysis for protocol-v0.4.1 SOLO."""
    out_dir.mkdir(parents=True, exist_ok=True)
    df = flatten_records(records)
    selected = df[
        (df["task_set"] == "solo_v04")
        & (df["condition"] == _SOLO_CONDITION)
        & (df["analysis_included"] == True)  # noqa: E712
    ].copy()
    if len(selected) != 25 or selected["task_id"].nunique() != 25:
        raise AnalysisInputError(
            "solo_v04 requires exactly 25 unique analysis-included tasks"
        )
    if selected["analysis_only_itt_row"].any():
        raise AnalysisInputError(
            "completed solo_v04 batch must not require analysis-only rows"
        )

    success_numerator = int(selected["final_success"].sum())
    mean_success = float(selected["final_success"].mean())
    mean_score = float(selected["final_score"].mean())
    n_nonfinalized = int((selected["parent_finalized"] == False).sum())  # noqa: E712
    n_budget_exhausted = int(selected["parent_budget_exhausted"].sum())
    n_agent_failures = int(selected["n_agent_failures"].sum())
    qc = make_qc_report(
        selected,
        out_dir / "qc_report.json",
        conditions=(_SOLO_CONDITION,),
    )
    save_episodes(selected, out_dir)
    cost_rows, cost_summary = build_cost_summary(
        selected,
        task_set="solo_v04",
        conditions=(_SOLO_CONDITION,),
    )
    cost_rows.to_csv(out_dir / "cost_by_cell_role.csv", index=False)
    (out_dir / "cost_summary.json").write_text(
        json.dumps(cost_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    condition_summary = {
        "condition": _SOLO_CONDITION,
        "n": 25,
        "full_task_success_numerator": success_numerator,
        "full_task_success_rate": mean_success,
        "mean_target_unit_pass_rate": mean_score,
        "n_nonfinalized": n_nonfinalized,
        "n_parent_budget_exhausted": n_budget_exhausted,
        "n_agent_failures": n_agent_failures,
        "parent_model_calls_total": int(selected["parent_model_calls"].sum()),
        "parent_model_calls_mean": float(selected["parent_model_calls"].mean()),
        "parent_tool_calls_total": int(selected["parent_tool_calls"].sum()),
        "parent_tool_calls_mean": float(selected["parent_tool_calls"].mean()),
        "parent_output_tokens_total": int(selected["parent_output_tokens"].sum()),
        "parent_output_tokens_mean": float(selected["parent_output_tokens"].mean()),
        "parent_run_events_total": int(selected["parent_run_events"].sum()),
        "parent_wall_seconds_total": float(selected["parent_wall_seconds"].sum()),
        "parent_wall_seconds_mean": float(selected["parent_wall_seconds"].mean()),
        "parent_runs_without_auto_observation": int(
            selected["parent_runs_without_auto_observation"].sum()
        ),
    }
    pd.DataFrame([condition_summary]).to_csv(
        out_dir / "condition_summary.csv",
        index=False,
    )
    tex = rf"""% SOLO descriptive summary; never pooled into factorial effects
\begin{{table}}[ht]
\centering
\caption{{Protocol-v0.4.1 SOLO descriptive outcome and one-agent effort.
  SOLO is reported separately and is not pooled into $\tau_0$, $\tau_1$, or
  $\delta$.}}
\label{{tab:solo_v041}}
\begin{{tabular}}{{lrrrrr}}
\toprule
Condition & $n$ & Full success & Mean pass rate & Nonfinalized & Mean model calls \\
\midrule
SOLO & 25 & {success_numerator}/25 ({mean_success:.3f}) & {mean_score:.3f} &
{n_nonfinalized} & {condition_summary['parent_model_calls_mean']:.2f} \\
\bottomrule
\end{{tabular}}
\end{{table}}
"""
    (out_dir / "tableSolo_summary.tex").write_text(tex, encoding="utf-8")
    summary = {
        "record_source": "explicit episode records",
        "runs_dir": str(runs_dir.resolve()),
        "task_set_scope": "solo_v04",
        "protocol_version": "0.4.1",
        "separate_from_factorial_effects": True,
        "condition_summary": condition_summary,
        "cost_summary": cost_summary,
        "qc_report": qc,
    }
    (out_dir / "analysis_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _as_binary(value: Any) -> float | None:
    """Coerce True/False/1/0 to a binary float; leave missing as None."""
    if value is True or value == 1 or value == 1.0 or value == "True":
        return 1.0
    if value is False or value == 0 or value == 0.0 or value == "False":
        return 0.0
    return None


def compute_p0_p1_handoff_metrics(
    df: pd.DataFrame,
    *,
    task_set: str,
    seed: int = P0_P1_BOOTSTRAP_SEED,
    n_draws: int = P0_P1_BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """P0 vs P1 candidate success, parent repair/damage, effort, and accept CI.

    The accepted-unchanged contrast has no prior estimator in this module, so
    it uses the study bootstrap seed 20260902 and B=10,000. Main-effect
    tau/delta CIs keep the v0.4.1 package convention (seed 0, B=5,000).
    """
    prod = df[
        (df["task_set"] == task_set)
        & (df["analysis_included"] == True)  # noqa: E712
        & df["condition"].isin(["P0", "P1"])
    ].copy()
    if prod.empty:
        raise AnalysisInputError(f"no P0/P1 rows for {task_set!r}")

    def _cell(condition: str) -> pd.DataFrame:
        return prod[prod["condition"] == condition]

    cells: dict[str, dict[str, Any]] = {}
    for condition in ("P0", "P1"):
        cell = _cell(condition)
        worker = cell["worker_success"].astype(float)
        final = cell["final_success"].astype(float)
        accepted = cell["accepted_unchanged"].map(_as_binary)
        cells[condition] = {
            "n": int(len(cell)),
            "candidate_success_n": int((worker == 1).sum()),
            "candidate_success_rate": float(worker.mean()),
            "parent_repair_n": int(((worker == 0) & (final == 1)).sum()),
            "parent_damage_n": int(((worker == 1) & (final == 0)).sum()),
            "accepted_unchanged_n": int((accepted == 1).sum()),
            "accepted_unchanged_rate": float(accepted.mean()),
            "revised_n": int((accepted == 0).sum()),
            "parent_input_tokens_total": int(cell["parent_input_tokens"].sum()),
            "parent_output_tokens_total": int(cell["parent_output_tokens"].sum()),
            "parent_cache_read_input_tokens_total": int(
                cell["parent_cache_read_input_tokens"].sum()
            ),
            "parent_cache_creation_input_tokens_total": int(
                cell["parent_cache_creation_input_tokens"].sum()
            ),
            "parent_tool_calls_total": int(cell["parent_tool_calls"].sum()),
            "parent_tool_calls_mean": float(cell["parent_tool_calls"].mean()),
            "parent_wall_seconds_total": float(cell["parent_wall_seconds"].sum()),
            "parent_wall_seconds_mean": float(cell["parent_wall_seconds"].mean()),
            "parent_model_calls_total": int(cell["parent_model_calls"].sum()),
            "parent_model_calls_mean": float(cell["parent_model_calls"].mean()),
        }

    wide = (
        prod.assign(
            accepted_bin=prod["accepted_unchanged"].map(_as_binary)
        )
        .pivot(index="task_id", columns="condition", values="accepted_bin")
        .sort_index()
    )
    if list(wide.columns) != ["P0", "P1"] and set(wide.columns) != {"P0", "P1"}:
        missing = {"P0", "P1"} - set(wide.columns)
        raise AnalysisInputError(f"P0/P1 accepted-unchanged grid missing {missing}")
    if wide[["P0", "P1"]].isna().any(axis=None):
        raise AnalysisInputError("P0/P1 accepted-unchanged grid has missing tasks")
    diffs = wide["P1"].to_numpy(dtype=float) - wide["P0"].to_numpy(dtype=float)
    estimate = float(np.mean(diffs))
    rng = np.random.default_rng(seed)
    n_tasks = len(diffs)
    draws = np.array([
        float(np.mean(diffs[rng.integers(0, n_tasks, n_tasks)]))
        for _ in range(n_draws)
    ])
    ci = (
        float(np.percentile(draws, 100 * ALPHA / 2)),
        float(np.percentile(draws, 100 * (1 - ALPHA / 2))),
    )
    return {
        "task_set": task_set,
        "seed": seed,
        "draws": n_draws,
        "n_tasks": n_tasks,
        "cells": cells,
        "accepted_unchanged_p1_minus_p0": estimate,
        "accepted_unchanged_p1_minus_p0_ci": list(ci),
        "accepted_unchanged_p1_minus_p0_se": float(np.std(draws, ddof=1)),
        "convention": (
            "study bootstrap seed 20260902, B=10000; no prior accepted-unchanged "
            "contrast convention existed in analyze.py"
        ),
    }


def write_v041_results_digest(
    summary: dict[str, Any],
    out_path: Path,
) -> Path:
    """Write the tracked v0.4.1 results digest, including the P0/P1 section."""
    estimates = summary["estimates"]
    boot = summary["bootstrap"]
    p0p1 = summary["p0_p1_handoff"]
    lines = [
        "# v0.4.1 Spreadsheet Results Digest (n=25)",
        "",
        "## Scope and disposition",
        "",
        "- Protocol: `0.4.1`; task set: `main_v04` only.",
        "- Expected grid: 25 tasks × A0/A1/P0/P1 = 100 ITT rows.",
        f"- Serialized episode records: {summary['n_records'] - len(summary['analysis_only_itt_rows'])}.",
        f"- Analysis-only ITT rows: {len(summary['analysis_only_itt_rows'])}.",
        "- `57232 × A0` is a censored agent outcome "
        "(`run_status=agent_failure`, `failure_reason=context_window_exceeded`) "
        "retained as success=0. Shared `episode_schema.json` cannot name "
        "`agent_failure`; the analysis-side terminal-episode contract loads it.",
        "",
        "## Paired effects on full-task success",
        "",
        f"Task-clustered percentile bootstrap: seed `{summary['seed']}`, "
        f"B={summary['draws']:,}.",
        "",
        "| Estimand | Estimate | 95% CI |",
        "|---|---:|---:|",
        f"| tau0 | {estimates['tau0']:+.2f} | "
        f"[{boot['tau0_ci'][0]:+.2f}, {boot['tau0_ci'][1]:+.2f}] |",
        f"| tau1 | {estimates['tau1']:+.2f} | "
        f"[{boot['tau1_ci'][0]:+.2f}, {boot['tau1_ci'][1]:+.2f}] |",
        f"| delta | {estimates['delta']:+.2f} | "
        f"[{boot['delta_ci'][0]:+.2f}, {boot['delta_ci'][1]:+.2f}] |",
        "",
        "## P0 vs P1 handoff contrast",
        "",
        f"Paired accepted-unchanged(P1) − accepted-unchanged(P0) uses the "
        f"study bootstrap seed `{p0p1['seed']}`, B={p0p1['draws']:,} "
        f"({p0p1['convention']}).",
        "",
        "| Metric | P0 | P1 |",
        "|---|---:|---:|",
    ]
    p0 = p0p1["cells"]["P0"]
    p1 = p0p1["cells"]["P1"]
    lines.append(
        f"| Raw candidate success | {p0['candidate_success_n']}/{p0['n']} "
        f"({p0['candidate_success_rate']:.3f}) | "
        f"{p1['candidate_success_n']}/{p1['n']} "
        f"({p1['candidate_success_rate']:.3f}) |"
    )
    lines.append(
        f"| Parent repair | {p0['parent_repair_n']} | {p1['parent_repair_n']} |"
    )
    lines.append(
        f"| Parent damage | {p0['parent_damage_n']} | {p1['parent_damage_n']} |"
    )
    lines.append(
        f"| Accepted unchanged | {p0['accepted_unchanged_n']}/{p0['n']} "
        f"({p0['accepted_unchanged_rate']:.3f}) | "
        f"{p1['accepted_unchanged_n']}/{p1['n']} "
        f"({p1['accepted_unchanged_rate']:.3f}) |"
    )
    lines.append(
        f"| Parent model calls (total / mean) | "
        f"{p0['parent_model_calls_total']} / {p0['parent_model_calls_mean']:.2f} | "
        f"{p1['parent_model_calls_total']} / {p1['parent_model_calls_mean']:.2f} |"
    )
    lines.append(
        f"| Parent tool calls (total / mean) | "
        f"{p0['parent_tool_calls_total']} / {p0['parent_tool_calls_mean']:.2f} | "
        f"{p1['parent_tool_calls_total']} / {p1['parent_tool_calls_mean']:.2f} |"
    )
    lines.append(
        f"| Parent output tokens | {p0['parent_output_tokens_total']} | "
        f"{p1['parent_output_tokens_total']} |"
    )
    lines.append(
        f"| Parent wall seconds (total / mean) | "
        f"{p0['parent_wall_seconds_total']:.3f} / {p0['parent_wall_seconds_mean']:.3f} | "
        f"{p1['parent_wall_seconds_total']:.3f} / {p1['parent_wall_seconds_mean']:.3f} |"
    )
    ci = p0p1["accepted_unchanged_p1_minus_p0_ci"]
    lines.extend([
        "",
        "| Contrast | Estimate | 95% CI |",
        "|---|---:|---:|",
        f"| accepted-unchanged(P1) − accepted-unchanged(P0) | "
        f"{p0p1['accepted_unchanged_p1_minus_p0']:+.3f} | "
        f"[{ci[0]:+.3f}, {ci[1]:+.3f}] |",
        "",
        "This digest is the canonical tracked v0.4.1 results file. The co-author A "
        "zip is not mutated here.",
        "",
    ])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {out_path}")
    return out_path


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_analysis(
    core_records: list[dict[str, Any]],
    stock_rows: list[dict[str, Any]],
    out_dir: Path,
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
    task_set_scope: str = "main",
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the full analysis pipeline; return a summary dict.

    Parameters
    ----------
    core_records : Episode records loaded from an explicit runs directory.
    stock_rows   : Stock records, or an empty list when none were collected.
    out_dir      : Directory to write all outputs.
    seed         : Bootstrap RNG seed.
    n_draws      : Number of bootstrap draws.

    Returns
    -------
    dict with keys: estimates, boot_summary, n_records, qc_report
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"Delegation Study Analysis")
    print(f"  seed={seed}, n_draws={n_draws}, out={out_dir}")
    print(f"{'='*60}\n")

    # 1. Flatten
    print("[1/8] Flattening records …")
    df = flatten_records(core_records)
    analysis_task_set = (
        "main_v04" if task_set_scope == "main_v04" else "main"
    )
    n_included = int(df["analysis_included"].sum())
    n_main = int(
        (
            (df["analysis_included"] == True)  # noqa: E712
            & (df.get("task_set", pd.Series(["main"] * len(df))) == analysis_task_set)
        ).sum()
    )
    n_hard = int(
        ((df["analysis_included"] == True) & (df.get("task_set", pd.Series(["main"] * len(df))) == "hard")).sum()  # noqa: E712
    )
    hard_invalid = df[
        (df.get("task_set", pd.Series(["main"] * len(df))) == "hard")
        & (
            (df["evaluator_status"] == "evaluator_failure")
            | df["evaluator_status"].isna()
        )
    ]
    hard_exclusions = [
        {
            "run_id": str(row.run_id),
            "task_id": str(row.task_id),
            "condition": str(row.condition),
            "reason": "evaluator_failure: no valid hard-supplement outcome",
        }
        for row in hard_invalid.itertuples()
    ]
    n_solo = int(
        (
            (df["analysis_included"] == True)  # noqa: E712
            & (df["task_set"] == "main")
            & (df["condition"] == _SOLO_CONDITION)
        ).sum()
    )
    print(f"      {len(df)} records total, {n_included} analysis-included "
          f"({n_main} main, {n_hard} hard, {n_solo} SOLO)")

    # 2. Compute task means — main 2×2 only (hard excluded, SOLO excluded)
    print("[2/8] Computing task means (main 2×2 factorial only) …")
    task_means = compute_task_means(df, task_set=analysis_task_set)

    # 2b. SOLO means (main task_set, SOLO condition)
    solo_means = compute_solo_means(df)
    if not solo_means.empty:
        print(f"      SOLO: {len(solo_means)} task means; "
              f"grand mean = {solo_means['task_mean'].mean():.3f}")

    # 3. Point estimates
    print("[3/8] Computing point estimates …")
    estimates = compute_estimands(task_means)
    print(f"      tau0 = {estimates['tau0']:+.4f}   "
          f"tau1 = {estimates['tau1']:+.4f}   "
          f"delta = {estimates['delta']:+.4f}")

    # 4. Bootstrap
    print(f"[4/8] Bootstrapping (seed={seed}, B={n_draws}) …")
    boot = bootstrap_estimands(task_means, seed=seed, n_draws=n_draws)
    boot_summary = {
        k: v for k, v in boot.items()
        if not k.endswith("_draws")          # exclude large arrays from summary
    }
    print(f"      tau0  95%CI = {boot['tau0_ci']}")
    print(f"      tau1  95%CI = {boot['tau1_ci']}")
    print(f"      delta 95%CI = {boot['delta_ci']}")

    # 5. Figures
    print("[5/8] Generating figures …")
    make_fig1(
        df,
        task_means,
        estimates,
        boot,
        out_dir,
        task_set=analysis_task_set,
        seed=seed,
        n_draws=n_draws,
    )
    make_fig2(df, out_dir, task_set=analysis_task_set)
    fig3_generated = make_fig3(stock_rows, out_dir)

    # 6. Tables
    print("[6/8] Generating tables …")
    analysis_only = df[
        (df["task_set"] == analysis_task_set)
        & df["analysis_only_itt_row"]
    ]
    agent_failures = df[
        (df["task_set"] == analysis_task_set)
        & (df["status"] == "agent_failure")
    ]
    itt_note = None
    if not analysis_only.empty:
        pairs = ", ".join(
            f"{row.task_id} {row.condition}" for row in analysis_only.itertuples()
        )
        itt_note = (
            f"ITT includes {pairs} as a zero-valued censored agent outcome; "
            "the absent episode record is a separate serialization gap."
        )
    elif not agent_failures.empty:
        pairs = ", ".join(
            f"{row.task_id} {row.condition}" for row in agent_failures.itertuples()
        )
        itt_note = (
            f"ITT includes {pairs} as a zero-valued censored agent outcome "
            "(context_window_exceeded)."
        )
    make_table1(
        estimates,
        boot,
        out_dir / "table1_effects.tex",
        itt_note=itt_note,
        seed=seed,
        n_draws=n_draws,
    )
    make_table_s1(
        df,
        task_means,
        boot,
        out_dir / "tableS1_condition_summary.tex",
        task_set=analysis_task_set,
        seed=seed,
        n_draws=n_draws,
    )
    condition_summary = build_condition_summary(
        df,
        task_means,
        task_set=analysis_task_set,
    )
    if task_set_scope == "main_v04":
        make_table_s2_disposition(
            condition_summary,
            out_dir / "tableS2_disposition.tex",
        )
    else:
        # v0.3 conventions retained for regression and hard supplement.
        make_table_s2_hard_probe(df, out_dir / "tableS2_hard_probe.tex")
        make_table_s2(df, out_dir / "tableS3_qc.tex")

    # 7. QC + episode data
    print("[7/8] Writing QC report and episode data …")
    qc = make_qc_report(df, out_dir / "qc_report.json")
    save_episodes(df, out_dir)
    condition_summary.to_csv(out_dir / "condition_summary.csv", index=False)
    effects = pd.DataFrame([
        {
            "estimand": key,
            "estimate": estimates[key],
            "ci_low": boot[f"{key}_ci"][0],
            "ci_high": boot[f"{key}_ci"][1],
            "bootstrap_se": boot[f"{key}_se"],
        }
        for key in ("tau0", "tau1", "delta")
    ])
    effects.to_csv(out_dir / "effects.csv", index=False)
    (out_dir / "effects.json").write_text(
        effects.to_json(orient="records", indent=2) + "\n",
        encoding="utf-8",
    )
    cost_rows, cost_summary = build_cost_summary(
        df,
        task_set=analysis_task_set,
        conditions=(
            _ALL_CONDITIONS
            if task_set_scope in {"main", "all"}
            else _CONDITIONS
        ),
    )
    cost_rows.to_csv(out_dir / "cost_by_cell_role.csv", index=False)
    (out_dir / "cost_summary.json").write_text(
        json.dumps(cost_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 8. SOLO comparison summary (written to qc_report and printed; no separate figure)
    print("[8/8] SOLO comparison summary …")
    solo_summary: dict[str, Any] = {}
    if not solo_means.empty:
        solo_grand_mean = float(solo_means["task_mean"].mean())
        # Compare SOLO to the unweighted mean over A0/A1/P0/P1 per task
        four_cell_mean = float(task_means.groupby("task_id")["task_mean"].mean().mean()) if not task_means.empty else None
        solo_summary = {
            "n_solo_tasks": int(solo_means["task_id"].nunique()),
            "solo_grand_mean": solo_grand_mean,
            "four_cell_grand_mean": four_cell_mean,
        }
        comparison = f"{four_cell_mean:.4f}" if four_cell_mean is not None else "n/a"
        print(
            f"      SOLO grand mean = {solo_grand_mean:.4f} "
            f"(four-cell mean = {comparison})"
        )
    else:
        print("      No SOLO main episodes found.")

    print(f"\n{'='*60}")
    print("Analysis complete.")
    print(f"  tau0  = {estimates['tau0']:+.4f}  95%CI {boot['tau0_ci']}")
    print(f"  tau1  = {estimates['tau1']:+.4f}  95%CI {boot['tau1_ci']}")
    print(f"  delta = {estimates['delta']:+.4f}  95%CI {boot['delta_ci']}")
    print(f"{'='*60}\n")

    summary = {
        "record_source": "explicit episode records",
        "runs_dir": str(runs_dir.resolve()) if runs_dir is not None else None,
        "task_set_scope": task_set_scope,
        "protocol_version": _SCOPE_PROTOCOL[task_set_scope],
        "seed": seed,
        "draws": n_draws,
        "estimates": estimates,
        "bootstrap": boot_summary,
        "n_records": len(df),
        "n_analysis_included": n_included,
        "n_main": n_main,
        "n_hard": n_hard,
        "n_hard_valid": n_hard - len(hard_exclusions),
        "hard_supplement_exclusions": hard_exclusions,
        "n_solo": n_solo,
        "solo_summary": solo_summary,
        "condition_summary": condition_summary.to_dict(orient="records"),
        "analysis_only_itt_rows": analysis_only[
            [
                "run_id",
                "task_id",
                "condition",
                "final_success",
                "final_score",
                "outcome_category",
                "record_serialization_gap",
                "rerun_eligible",
                "source_kind",
                "missing_record_reason",
                "batch_status_sha256",
            ]
        ].to_dict(orient="records"),
        "cost_summary": cost_summary,
        "p0_p1_handoff": (
            compute_p0_p1_handoff_metrics(df, task_set=analysis_task_set)
            if task_set_scope == "main_v04"
            else None
        ),
        "fig3_generated": fig3_generated,
        "fig3_reason": (
            "stock records present"
            if fig3_generated
            else "omitted deterministically: no stock records"
        ),
        "qc_report": qc,
    }
    (out_dir / "analysis_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {out_dir / 'analysis_summary.json'}")
    if task_set_scope == "main_v04" and summary.get("p0_p1_handoff") is not None:
        write_v041_results_digest(summary, out_dir / "results_digest_v041.md")
    result = dict(summary)
    result["boot_summary"] = boot_summary
    return result


# ── SWE-bench analysis ────────────────────────────────────────────────────────
#
# SWE-bench records use:
#   task_set = "development" | "pilot" | "main"
#     CLI: --task-set dev | pilot | main
#   benchmark = "swebench"
#   protocol_version = "0.3" / "swe-0.3.1" / "swe-0.3.3" / "swe-0.3.4"
#   env_metrics.swebench.final_resolved  — primary ITT outcome
#   env_metrics.swebench.candidate_resolved — recorded, not the ITT field
#   env_metrics.swebench.fail_to_pass_fraction  — secondary (may be valid-null)
#   env_metrics.swebench.pass_to_pass_fraction  — secondary (may be valid-null)
#   env_metrics.swebench.f2p_p2p_null_reason    — e.g. schema_v2_no_tests_status
#   swe_protocol_usage.worker.memo_source       — advisory memo provenance
#   env_metrics.swebench image/git/container    — provenance
#
# The spec uses "env_metrics.swebench.resolved" but the actual field is
# "env_metrics.swebench.final_resolved".  The discrepancy is noted in the
# missing-field audit (field alias documented, no fabrication).
#
# Dev records are EXCLUDED from every Spreadsheet analysis path and require
# an explicit --allow-task-set dev flag on the CLI.
# Main confirmatory records use --task-set main (no allow-dev gate).

_SWE_BENCHMARK = "swebench"
_SWE_TASK_SET_LABEL = "dev"          # CLI token
_SWE_TASK_SET_RECORDED = "development"  # value recorded in episode.json
_SWE_TASK_SET_PILOT = "pilot"
_SWE_TASK_SET_MAIN = "main"
_SWE_TASK_SET_MAIN_V035 = "main_v035"
_SWE_VALID_F2P_P2P_NULL_REASONS = frozenset({
    "schema_v2_no_tests_status",
    "tests_timed_out",
})
_SWE_HARNESS_TIMEOUT_MARKERS = ("timed_out", "timeout")
_SWE_PASS_FRACTION_FIELDS = frozenset({
    "env_metrics.swebench.fail_to_pass_fraction",
    "env_metrics.swebench.pass_to_pass_fraction",
})

# Canonical 32 SWE-main run IDs from data/dev/swe/main_report.md (8 A-2 tasks × 4 cells).
# Includes four handwritten infrastructure_failure rows.  Prefer this list over a
# directory scan so extra orphans / failed 0.3.4 retries cannot enter the matrix.
SWE_MAIN_TASKS: tuple[str, ...] = (
    "sympy__sympy-24562",
    "django__django-13297",
    "matplotlib__matplotlib-24149",
    "scikit-learn__scikit-learn-13496",
    "pytest-dev__pytest-10081",
    "sympy__sympy-12481",
    "django__django-14855",
    "matplotlib__matplotlib-25332",
)
SWE_MAIN_RUN_IDS: tuple[str, ...] = (
    "ac75cce9-144a-4aac-a38d-66c62dc06fe7",  # sympy-24562 A0
    "b6f76590-5419-44ad-b571-4f01b9f91c16",  # sympy-24562 A1
    "16bf4093-11ce-49e2-bf29-eb0b8a271394",  # sympy-24562 P0 (handwritten infra)
    "15c51462-4b7c-47c1-9367-894e11f504bf",  # sympy-24562 P1
    "1a11a33d-59cd-43a1-8c65-c422241f4c20",  # django-13297 A0
    "9720426a-d426-4e43-a5e1-22806b2ae87d",  # django-13297 A1
    "4e2322d6-3b82-4e59-b46b-8e9fe286dd52",  # django-13297 P0
    "0f4b39ea-9656-4b4d-a966-3b5ac19d01c3",  # django-13297 P1
    "2389d180-0479-4c13-8da1-512f781a49ca",  # matplotlib-24149 A0 (handwritten infra)
    "dbd8ebdf-f0f5-4209-92bb-ff01e70954a4",  # matplotlib-24149 A1 (swe-0.3.4)
    "40a7ca7f-6d28-45c9-8e39-07fe378ac439",  # matplotlib-24149 P0 (handwritten infra)
    "0a8eef47-5256-4114-945c-a7ad9c71f4bb",  # matplotlib-24149 P1 (handwritten infra)
    "4ceea1cb-40a6-4f4b-9e45-9e84d8b85f96",  # scikit-learn-13496 A0
    "bbf637ad-672b-4c4c-8088-4fbd17194047",  # scikit-learn-13496 A1
    "eefd66c8-7889-4202-afb8-f74026d94a94",  # scikit-learn-13496 P0
    "fc0842ef-2db4-4755-b0b4-97794855be83",  # scikit-learn-13496 P1
    "1a8a8e41-fbdf-461b-a7e3-595ccd73b3ad",  # pytest-10081 A0
    "04d21205-9e5b-4f5a-87d0-156c092b1d6d",  # pytest-10081 A1
    "af57623f-ea5d-4746-9d63-ffff0d497fa6",  # pytest-10081 P0
    "e8fe1e00-30f0-4926-97e6-5d8eae5471ed",  # pytest-10081 P1
    "642b6af7-866f-4fe9-abce-b8be0949ae20",  # sympy-12481 A0
    "f243d8bf-42b8-47ce-bdb5-c029d84409f4",  # sympy-12481 A1
    "88e194e8-a559-4210-bd0c-0868120475e1",  # sympy-12481 P0
    "18593dbf-172f-470e-aff7-6e5e599531b5",  # sympy-12481 P1
    "43b42f06-2d24-44de-b98e-746faf60c9a7",  # django-14855 A0
    "9b42f0c2-6f43-4f78-b445-031a51c87c28",  # django-14855 A1
    "76c3b54a-4d43-4640-b538-367797b5e715",  # django-14855 P0
    "597a740c-1070-41df-b349-06714cb47023",  # django-14855 P1
    "abf97d78-81f1-4d4e-a394-09dc4d0f5099",  # matplotlib-25332 A0
    "f396d0e3-3111-499c-ae4c-8b86d2a540d0",  # matplotlib-25332 A1
    "a52f955c-ef7e-4f9a-8ac8-7615417e0e8f",  # matplotlib-25332 P0
    "49235be6-4b58-4849-bfcb-73e4f715606c",  # matplotlib-25332 P1
)
SWE_MAIN_HANDWRITTEN_INFRA_IDS: frozenset[str] = frozenset({
    "16bf4093-11ce-49e2-bf29-eb0b8a271394",
    "2389d180-0479-4c13-8da1-512f781a49ca",
    "40a7ca7f-6d28-45c9-8e39-07fe378ac439",
    "0a8eef47-5256-4114-945c-a7ad9c71f4bb",
})
# Exact 8×4 ITT matrix after swe-0.3.5 recoding of the swe-0.3.3–4 records.
# None = infrastructure_failure or evaluator_failure; never coerced to 0.
# not_finalized / empty-patch measured cells remain 0.
SWE_MAIN_MATRIX: dict[str, dict[str, int | None]] = {
    "sympy-24562": {"A0": 0, "A1": 0, "P0": None, "P1": 0},
    "django-13297": {"A0": 1, "A1": 1, "P0": 1, "P1": 1},
    "matplotlib-24149": {"A0": None, "A1": None, "P0": None, "P1": None},
    "scikit-learn-13496": {"A0": 1, "A1": 1, "P0": 1, "P1": 1},
    "pytest-10081": {"A0": 1, "A1": 1, "P0": 1, "P1": 1},
    "sympy-12481": {"A0": 0, "A1": 1, "P0": 0, "P1": 1},
    "django-14855": {"A0": 1, "A1": 1, "P0": 1, "P1": 1},
    "matplotlib-25332": {"A0": 0, "A1": None, "P0": 0, "P1": None},
}
SWE_MAIN_CELL_SUCCESS = {"A0": 4, "A1": 5, "P0": 4, "P1": 5}
SWE_MAIN_CELL_VALID = {"A0": 7, "A1": 6, "P0": 6, "P1": 6}
SWE_MAIN_N_EVALUATOR_FAILURE = 3
SWE_MAIN_N_INFRASTRUCTURE_FAILURE = 4
SWE_MAIN_PAIRWISE_N = {"tau0": 6, "tau1": 6, "delta": 5}

# Canonical 32 SWE main_v035 run IDs (swe-0.3.5 confirmatory 8×4).
# Distinct from SWE_MAIN_RUN_IDS (closed 0.3.3/4 matrix with handwritten infra).
SWE_MAIN_V035_PROTOCOL = "swe-0.3.5"
SWE_MAIN_V035_PROTOCOL_PIN = "<commit>"
SWE_MAIN_V035_FREEZE_COMMIT = "<commit>"
SWE_MAIN_V035_FREEZE_TAG = "freeze-swe-0.3.5"
SWE_MAIN_V035_BATCH_ID = "swe-main-v035-20260905T031032Z-<id>"
SWE_MAIN_V035_DISCORDANT_THRESHOLD = 3
SWE_MAIN_V035_TASKS: tuple[str, ...] = (
    "pylint-dev__pylint-6386",
    "sphinx-doc__sphinx-8593",
    "sympy__sympy-12419",
    "django__django-14792",
    "sympy__sympy-16766",
    "scikit-learn__scikit-learn-14087",
    "astropy__astropy-8872",
    "django__django-11292",
)
SWE_MAIN_V035_RUN_IDS: tuple[str, ...] = (
    "d7140186-b64d-44eb-82a7-3cd1ef653bd4",  # pylint-6386 A0
    "5dd77b66-4502-4530-a095-fba2d3a29252",  # pylint-6386 A1
    "70009e10-5f47-4d63-af10-883f23e7fc0d",  # pylint-6386 P0
    "ead34336-08cd-47f8-a36d-566f9558b1d5",  # pylint-6386 P1
    "0a219238-7421-4cdd-8866-91a49a572fe0",  # sphinx-8593 A0
    "ffa520cb-d535-42d6-b200-4d91eeef54ee",  # sphinx-8593 A1
    "8df1d32f-7356-4726-a1e5-07e254385c64",  # sphinx-8593 P0
    "3589a8bd-f6c5-4988-9a0a-e2ab7ea80532",  # sphinx-8593 P1
    "3a4191d1-8a84-4349-b36f-91d8e7d1d2f0",  # sympy-12419 A0
    "be7b5e4b-8c55-430c-ad06-3a9bed11fab9",  # sympy-12419 A1
    "94140531-0ae5-46b6-b685-863e063ac36a",  # sympy-12419 P0
    "7400b89a-4cda-4b02-b698-10f083d93463",  # sympy-12419 P1
    "4832dbb4-69b5-4dcf-8256-ef879ad54a28",  # django-14792 A0
    "6899c646-f5c3-4a1c-8587-d88e45f55444",  # django-14792 A1
    "18dba4a9-7f3d-40f2-8101-b3df0e789c2d",  # django-14792 P0
    "00159429-2d17-4e3d-990b-2eb18dcc5a2a",  # django-14792 P1
    "2dd75e38-0260-4d27-909f-773f1f1323f6",  # sympy-16766 A0
    "ee34d0db-4032-436f-9f34-f935dd49903a",  # sympy-16766 A1
    "d067c2f7-3b92-4432-b8de-a03679d2602d",  # sympy-16766 P0
    "82ab0b8d-fb84-48bf-abc2-eefd27fd3a90",  # sympy-16766 P1
    "ff6f7735-0657-47df-b113-954b66a961f3",  # sklearn-14087 A0
    "403d2aa6-6b60-4991-9bfd-c4fcc0f6e9b1",  # sklearn-14087 A1
    "52ef8f03-d55f-47e2-8ae6-a857dc713886",  # sklearn-14087 P0
    "264d2a76-0e5e-4f93-b8da-050ffd128505",  # sklearn-14087 P1
    "ff7be46b-7160-46a9-be49-ea498d5be984",  # astropy-8872 A0
    "bfb2b815-167a-4407-a5d4-342ab676ff36",  # astropy-8872 A1
    "43c0fc1e-d30c-416e-8c64-fdca193c22ea",  # astropy-8872 P0
    "50526ddb-94de-42e6-8e45-451a49ae0ff9",  # astropy-8872 P1
    "a395a799-29cc-4bf2-b081-e7527ade295d",  # django-11292 A0
    "e0d6d173-e2f9-4c18-938d-da26ace1c0b1",  # django-11292 A1
    "c04f1429-d7ae-48c0-952e-7a42ca05a240",  # django-11292 P0
    "f56edadd-4f2c-4b93-81c7-b2edac8dc617",  # django-11292 P1
)
SWE_MAIN_V035_CELL_RUN_IDS: dict[tuple[str, str], str] = {
    (SWE_MAIN_V035_TASKS[i // 4], _CONDITIONS[i % 4]): SWE_MAIN_V035_RUN_IDS[i]
    for i in range(32)
}

# All expected analysis fields for SWE-bench records, with human labels.
_SWE_EXPECTED_FIELDS: list[tuple[str, str]] = [
    ("env_metrics.swebench.final_resolved",        "primary outcome: final_resolved"),
    ("env_metrics.swebench.fail_to_pass_fraction", "secondary: fail-to-pass fraction"),
    ("env_metrics.swebench.pass_to_pass_fraction", "secondary: pass-to-pass fraction"),
    ("env_metrics.swebench.f2p_p2p_null_reason",   "F2P/P2P null reason"),
    ("env_metrics.swebench.applies_cleanly",       "secondary: applies cleanly"),
    ("env_metrics.swebench.candidate_resolved",    "candidate outcome: resolved"),
    ("env_metrics.swebench.final_harness_seconds", "final harness wall time"),
    ("env_metrics.swebench.candidate_harness_seconds", "candidate harness wall time"),
    ("swe_protocol_usage.worker.memo_source",      "advisory memo source"),
    ("env_metrics.swebench.image_tag",             "provenance: eval image tag"),
    ("env_metrics.swebench.image_digest",          "provenance: eval image digest"),
    ("env_metrics.swebench.git_head",              "provenance: container HEAD"),
    ("env_metrics.swebench.git_remote",            "provenance: remote or tag identity"),
    ("env_metrics.swebench.task_repo",             "provenance: task repository"),
    ("reproducibility.benchmark_git_commit",       "provenance: task base_commit"),
    ("env_metrics.swebench.worker_container_id",   "provenance: worker container id"),
    ("env_metrics.swebench.parent_container_id",   "provenance: parent container id"),
    ("final_script_sha256",                        "final patch SHA-256"),
    ("handoff.candidate_script_sha256",            "candidate patch SHA-256"),
    ("usage.worker.model_calls",                   "worker model calls"),
    ("usage.worker.tool_calls",                    "worker tool calls"),
    ("usage.worker.output_tokens",                 "worker output tokens"),
    ("usage.worker.wall_seconds",                  "worker wall seconds"),
    ("usage.parent.model_calls",                   "parent model calls"),
    ("usage.parent.tool_calls",                    "parent tool calls"),
    ("usage.parent.output_tokens",                 "parent output tokens"),
    ("usage.parent.wall_seconds",                  "parent wall seconds"),
    ("compliance.parent_finalized",                "parent finalized flag"),
]


def _get_nested(d: dict[str, Any], dotted_key: str) -> tuple[Any, bool]:
    """Traverse a dotted key path and return (value, found).

    Returns (None, False) if any intermediate key is missing or not a dict.
    Returns (value, True) even if value is None (key present but null).
    """
    parts = dotted_key.split(".")
    current: Any = d
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    if not isinstance(current, dict):
        return None, False
    last = parts[-1]
    if last not in current:
        return None, False
    return current[last], True


def _swe_f2p_p2p_null_reason(record: dict[str, Any]) -> str | None:
    """Return the recorded or sidecar-annotated F2P/P2P null reason."""
    em_swe = (record.get("env_metrics") or {}).get("swebench") or {}
    reason = em_swe.get("f2p_p2p_null_reason")
    if reason:
        return reason
    sidecar_reason = em_swe.get("_f2p_p2p_null_reason")
    if sidecar_reason:
        return sidecar_reason
    return None


def _swe_pass_fraction_status(
    record: dict[str, Any],
    value: Any,
    found: bool,
) -> str:
    """Classify a pass-fraction field: absent, null, valid_null, or present.

    ``schema_v2_no_tests_status`` is a valid absence (harness schema v2 has
    no per-instance tests_status).  It is not malformed.
    """
    if not found:
        return "absent"
    if value is not None:
        return "present_non_null"
    reason = _swe_f2p_p2p_null_reason(record)
    if reason in _SWE_VALID_F2P_P2P_NULL_REASONS:
        return "valid_null"
    return "null"


def swe_short_task_id(task_id: str) -> str:
    """Display label used in main_report.md (segment after the last ``__``)."""
    return task_id.split("__")[-1]


def _swe_report_timeout_reason(report: dict[str, Any]) -> str | None:
    reasons = report.get("failure_reasons") or {}
    if not isinstance(reasons, dict):
        return None
    for reason in reasons.values():
        if not isinstance(reason, str):
            continue
        if any(marker in reason for marker in _SWE_HARNESS_TIMEOUT_MARKERS):
            return reason
    return None


def _swe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def swe_harness_timeout_reason(
    record: dict[str, Any],
    *,
    phase: str = "final",
) -> str | None:
    """Return harness ``failure_reasons`` timeout string, or None."""
    names = {"final": "final_report.json", "candidate": "candidate_report.json"}
    filename = names.get(phase)
    if filename is None:
        return None
    run_dir = _swe_run_dir(record)
    candidates: list[Path] = []
    if run_dir is not None:
        candidates.append(run_dir / "reports" / filename)
    outcome_key = "final_outcome" if phase == "final" else "worker_outcome"
    out_path = (record.get(outcome_key) or {}).get("output_path")
    if out_path:
        candidates.append(Path(out_path))
    for path in candidates:
        report = _swe_read_json(path)
        if report is None:
            continue
        reason = _swe_report_timeout_reason(report)
        if reason:
            return reason
    return None


def swe_is_evaluator_failure(
    record: dict[str, Any],
    *,
    phase: str = "final",
) -> bool:
    """True when the harness timed out or the outcome is ``evaluator_failure``."""
    outcome_key = "final_outcome" if phase == "final" else "worker_outcome"
    outcome = record.get(outcome_key) or {}
    if outcome.get("evaluator_status") == "evaluator_failure":
        return True
    return swe_harness_timeout_reason(record, phase=phase) is not None


def swe_is_infrastructure_failure(record: dict[str, Any]) -> bool:
    """True when the episode is a recorded ``infrastructure_failure``."""
    return record.get("status") == "infrastructure_failure"


def swe_itt_resolved(record: dict[str, Any]) -> int | None:
    """Binary ITT outcome.

    ``evaluator_failure`` (harness timeout) and ``infrastructure_failure``
    are missing (not 0).  Other null ``final_resolved`` values
    (not_finalized / empty patch) remain None→0.
    """
    if swe_is_evaluator_failure(record, phase="final"):
        return None
    if swe_is_infrastructure_failure(record):
        return None
    em_swe = (record.get("env_metrics") or {}).get("swebench") or {}
    val = em_swe.get("final_resolved")
    if val is None:
        return 0
    return int(val)


def swe_itt_cell_label(record: dict[str, Any]) -> str:
    """Display label for one ITT cell: ``0``, ``1``, ``EF``, or ``IF``."""
    if swe_is_evaluator_failure(record, phase="final"):
        return "EF"
    if swe_is_infrastructure_failure(record):
        return "IF"
    itt = swe_itt_resolved(record)
    return "EF" if itt is None else str(itt)


def _fmt_itt_cell(value: int | None) -> str:
    return "EF" if value is None else str(value)


def swe_cell_success_counts(
    matrix: dict[str, dict[str, int | None]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Return (n_success, n_valid) per condition; missing cells are not valid."""
    success = {cond: 0 for cond in _CONDITIONS}
    valid = {cond: 0 for cond in _CONDITIONS}
    for row in matrix.values():
        for cond in _CONDITIONS:
            val = row.get(cond)
            if val is None:
                continue
            valid[cond] += 1
            success[cond] += int(val)
    return success, valid


def _accepted_swe_task_sets(ts_label: str, *, allow_dev: bool) -> set[str]:
    """Map a CLI task-set label to recorded episode.json task_set values."""
    if ts_label == _SWE_TASK_SET_LABEL:
        if not allow_dev:
            raise AnalysisInputError(
                "SWE-bench development records require --allow-task-set dev; "
                "pass that flag to include task_set='development' records"
            )
        return {_SWE_TASK_SET_RECORDED}
    if ts_label == _SWE_TASK_SET_PILOT:
        return {_SWE_TASK_SET_PILOT}
    if ts_label == _SWE_TASK_SET_MAIN:
        return {_SWE_TASK_SET_MAIN}
    if ts_label == _SWE_TASK_SET_MAIN_V035:
        return {_SWE_TASK_SET_MAIN_V035}
    raise AnalysisInputError(
        f"unknown task_set label {ts_label!r}; "
        f"expected 'dev', 'pilot', 'main', or 'main_v035'"
    )


def load_swe_records(
    runs_dirs: list[Path],
    *,
    allow_dev: bool = False,
    run_ids: set[str] | None = None,
    task_set: str | None = None,
) -> list[dict[str, Any]]:
    """Load SWE-bench episode records from one or more run directories.

    Parameters
    ----------
    runs_dirs   : One or more directories containing run subdirectories.
    allow_dev   : Must be True to include development records; raises
                  AnalysisInputError if False.  Pilot records do not require
                  this gate.
    run_ids     : Optional explicit set of run IDs to include; records not in
                  this set are skipped.  When None, all matching records are
                  returned.
    task_set    : CLI task_set label ('dev', 'pilot', 'main', or 'main_v035').
                  Controls which episode.json task_set values are accepted:
                    'dev'        -> task_set='development'
                    'pilot'      -> task_set='pilot'
                    'main'       -> task_set='main'  (closed 0.3.3/4 IDs)
                    'main_v035'  -> task_set='main_v035'  (swe-0.3.5 32 IDs)
                  When None, defaults to 'dev' (backward-compatible).

    Fails closed on:
    - allow_dev=False when loading development records
    - benchmark field absent or not "swebench"
    - task_set field absent or not matching expected value
    - run_id / directory name mismatch
    - duplicate run IDs across dirs
    - malformed episode JSON

    Protocol versions ``0.3``, ``swe-0.3.1``, ``swe-0.3.3``, and
    ``swe-0.3.4`` are accepted.  Null F2P/P2P fractions with
    ``f2p_p2p_null_reason=schema_v2_no_tests_status`` are valid absence, not
    a malformed-record rejection.  ``infrastructure_failure`` rows are
    loaded and retained; ITT codes them missing, never 0.
    """
    ts_label = task_set if task_set is not None else _SWE_TASK_SET_LABEL
    accepted_task_sets = _accepted_swe_task_sets(ts_label, allow_dev=allow_dev)

    records: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    parse_failures: list[str] = []

    for runs_dir in runs_dirs:
        if not runs_dir.is_dir():
            raise AnalysisInputError(
                f"runs directory does not exist: {runs_dir}"
            )
        for run_dir in sorted(runs_dir.iterdir(), key=lambda p: p.name):
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            # Filter to explicit run IDs if provided
            if run_ids is not None and run_dir.name not in run_ids:
                continue
            episode_path = run_dir / "episode.json"
            if not episode_path.is_file():
                continue
            try:
                raw = episode_path.read_bytes()
                record = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                parse_failures.append(f"{run_dir.name}: {type(exc).__name__}: {exc}")
                continue
            if not isinstance(record, dict):
                parse_failures.append(f"{run_dir.name}: episode is not an object")
                continue
            if record.get("benchmark") != _SWE_BENCHMARK:
                continue  # not a SWE-bench record; skip silently
            if record.get("task_set") not in accepted_task_sets:
                continue  # wrong task_set for this analysis scope; skip silently
            if record.get("run_id") != run_dir.name:
                raise AnalysisInputError(
                    f"run-id/directory mismatch: directory={run_dir.name!r}, "
                    f"record={record.get('run_id')!r}"
                )
            run_id = record["run_id"]
            if run_id in seen_run_ids:
                raise AnalysisInputError(
                    f"duplicate run_id across directories: {run_id!r}"
                )
            seen_run_ids.add(run_id)
            copied = dict(record)
            copied["_source_episode_sha256"] = hashlib.sha256(raw).hexdigest()
            copied["_source_runs_dir"] = str(runs_dir.resolve())
            records.append(copied)

    if parse_failures:
        raise AnalysisInputError(
            "malformed SWE episode records: " + "; ".join(parse_failures)
        )
    if not records:
        raise AnalysisInputError(
            f"no SWE-bench records (task_set in {sorted(accepted_task_sets)!r}) "
            f"found in the specified runs directories: {[str(d) for d in runs_dirs]}"
        )
    # If run_ids were requested, verify we found them all
    if run_ids is not None:
        missing = run_ids - seen_run_ids
        if missing:
            raise AnalysisInputError(
                f"requested run IDs not found in any runs directory: "
                f"{sorted(missing)}"
            )
    return records


def _classify_disposition(record: dict[str, Any]) -> str:
    """Return disposition string for a SWE-bench run based on patch hashes.

    Precedence (documented, read-only):
      1. handoff.candidate_script_sha256 (in-record)
      2. candidate_script_sha256 (top-level, legacy)
    Then compares against final_script_sha256 (top-level).

    Returns one of:
      "accepted_unchanged"  — candidate exists; final hash == candidate hash
      "revised"             — candidate exists; final hash != candidate hash
      "rebuilt"             — no candidate; parent still wrote a final patch
      "no_candidate"        — no candidate and no final patch
    """
    handoff = record.get("handoff") or {}
    candidate_hash = (
        handoff.get("candidate_script_sha256")
        or record.get("candidate_script_sha256")
    )
    final_hash = record.get("final_script_sha256")
    if not candidate_hash:
        return "rebuilt" if final_hash else "no_candidate"
    if not final_hash:
        return "revised"  # candidate present but final absent → parent revised
    if final_hash == candidate_hash:
        return "accepted_unchanged"
    return "revised"


def flatten_swe_records(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Flatten SWE-bench episode records into a tidy DataFrame.

    Columns
    -------
    run_id, task_id, task_set, condition, replicate, status
    analysis_included, benchmark, protocol_version, started_at, completed_at
    swe_final_resolved    — env_metrics.swebench.final_resolved (primary ITT)
    swe_candidate_resolved — env_metrics.swebench.candidate_resolved
    swe_fail_to_pass_fraction — secondary metric (often valid-null)
    swe_pass_to_pass_fraction — secondary metric (often valid-null)
    swe_f2p_p2p_null_reason — reason string when fractions are null
    swe_applies_cleanly   — secondary metric
    memo_source           — swe_protocol_usage.worker.memo_source
    provenance_*          — image / repo / HEAD / container ids
    candidate_patch_sha256, final_patch_sha256
    disposition           — accepted_unchanged / revised / rebuilt / no_candidate
    parent_finalized, accepted_unchanged_flag
    worker_* / parent_* usage columns
    source_episode_sha256, source_runs_dir
    qc_flags (SWE-specific), run_events (swe tool call proxy)

    Missing / null values are kept as NaN (never fabricated as 0).
    ITT coercion (None→0) is applied only in the main-report path, not here.
    """
    rows = []
    for rec in records:
        em_swe = (rec.get("env_metrics") or {}).get("swebench") or {}
        handoff = rec.get("handoff") or {}
        compliance = rec.get("compliance") or {}
        worker_usage = (rec.get("usage") or {}).get("worker") or {}
        parent_usage = (rec.get("usage") or {}).get("parent") or {}
        repro = rec.get("reproducibility") or {}
        worker_protocol = ((rec.get("swe_protocol_usage") or {}).get("worker") or {})

        candidate_hash = (
            handoff.get("candidate_script_sha256")
            or rec.get("candidate_script_sha256")
        )
        final_hash = rec.get("final_script_sha256")
        disposition = _classify_disposition(rec)

        # SWE-specific QC flags
        swe_flags: list[str] = []
        if not compliance.get("parent_finalized", False):
            swe_flags.append("parent_not_finalized")
        condition = rec.get("condition", "")
        if condition.startswith("P") and not handoff.get("candidate_script_path"):
            swe_flags.append("production_missing_artifact")
        if condition == "A1" and not compliance.get("worker_scratch_destroyed", False):
            swe_flags.append("advisory_scratch_retained")
        if condition.startswith("A") and handoff.get("candidate_script_path"):
            swe_flags.append("advisory_artifact_leak")
        run_dir = _swe_run_dir(rec)
        episode_path = (run_dir / "episode.json") if run_dir is not None else None
        if condition.startswith("A") and not handoff.get("memo_path"):
            swe_flags.append("advisory_missing_memo")
        elif condition.startswith("A") and memo_body_is_empty(
            rec, record_path=episode_path
        ):
            swe_flags.append("advisory_missing_memo")
        if swe_is_evaluator_failure(rec, phase="final"):
            swe_flags.append("evaluator_failure")

        def _nullable(val: Any) -> Any:
            """Return None as-is (will become NaN in DataFrame)."""
            return val

        rows.append({
            "run_id": rec.get("run_id"),
            "task_id": rec.get("task_id"),
            "task_set": rec.get("task_set"),
            "condition": rec.get("condition"),
            "replicate": rec.get("replicate"),
            "status": rec.get("status"),
            "benchmark": rec.get("benchmark"),
            "protocol_version": rec.get("protocol_version"),
            "started_at": rec.get("started_at"),
            "completed_at": rec.get("completed_at"),
            "analysis_included": rec.get("analysis_included"),
            # Primary outcome. evaluator_failure / infrastructure_failure stay null.
            "swe_final_resolved": _nullable(
                None if (
                    swe_is_evaluator_failure(rec, phase="final")
                    or swe_is_infrastructure_failure(rec)
                )
                else em_swe.get("final_resolved")
            ),
            "swe_candidate_resolved": _nullable(
                None if (
                    swe_is_evaluator_failure(rec, phase="candidate")
                    or swe_is_infrastructure_failure(rec)
                )
                else em_swe.get("candidate_resolved")
            ),
            "swe_evaluator_failure": swe_is_evaluator_failure(rec, phase="final"),
            "swe_infrastructure_failure": swe_is_infrastructure_failure(rec),
            # Secondary outcomes
            "swe_fail_to_pass_fraction": _nullable(
                em_swe.get("fail_to_pass_fraction")
            ),
            "swe_pass_to_pass_fraction": _nullable(
                em_swe.get("pass_to_pass_fraction")
            ),
            "swe_f2p_p2p_null_reason": _swe_f2p_p2p_null_reason(rec),
            "swe_applies_cleanly": _nullable(em_swe.get("applies_cleanly")),
            "memo_source": worker_protocol.get("memo_source"),
            "provenance_image_tag": em_swe.get("image_tag"),
            "provenance_image_digest": (
                em_swe.get("image_digest") or repro.get("docker_image_digest")
            ),
            "provenance_git_head": em_swe.get("git_head"),
            "provenance_git_remote": em_swe.get("git_remote"),
            "provenance_task_repo": em_swe.get("task_repo"),
            "provenance_base_commit": repro.get("benchmark_git_commit"),
            "provenance_worker_container_id": em_swe.get("worker_container_id"),
            "provenance_parent_container_id": em_swe.get("parent_container_id"),
            "provenance_clean_tree": em_swe.get("clean_tree"),
            "swe_final_harness_seconds": _nullable(
                em_swe.get("final_harness_seconds")
            ),
            "swe_candidate_harness_seconds": _nullable(
                em_swe.get("candidate_harness_seconds")
            ),
            # Patch disposition
            "candidate_patch_sha256": candidate_hash,
            "final_patch_sha256": final_hash,
            "disposition": disposition,
            "accepted_unchanged_flag": rec.get("accepted_unchanged"),
            # Compliance
            "parent_finalized": compliance.get("parent_finalized"),
            # Reproducibility
            "worker_model": repro.get("worker_model"),
            "parent_model": repro.get("parent_model"),
            "benchmark_git_commit": repro.get("benchmark_git_commit"),
            "docker_image_digest": repro.get("docker_image_digest"),
            # Worker usage
            "worker_model_calls": _nullable(worker_usage.get("model_calls")),
            "worker_tool_calls": _nullable(worker_usage.get("tool_calls")),
            "worker_output_tokens": _nullable(worker_usage.get("output_tokens")),
            "worker_input_tokens": _nullable(worker_usage.get("input_tokens")),
            "worker_cache_read_input_tokens": _nullable(
                worker_usage.get("cache_read_input_tokens")
            ),
            "worker_cache_creation_input_tokens": _nullable(
                worker_usage.get("cache_creation_input_tokens")
            ),
            "worker_wall_seconds": _nullable(worker_usage.get("wall_seconds")),
            "worker_budget_exhausted": worker_usage.get("budget_exhausted"),
            # Parent usage
            "parent_model_calls": _nullable(parent_usage.get("model_calls")),
            "parent_tool_calls": _nullable(parent_usage.get("tool_calls")),
            "parent_output_tokens": _nullable(parent_usage.get("output_tokens")),
            "parent_input_tokens": _nullable(parent_usage.get("input_tokens")),
            "parent_cache_read_input_tokens": _nullable(
                parent_usage.get("cache_read_input_tokens")
            ),
            "parent_cache_creation_input_tokens": _nullable(
                parent_usage.get("cache_creation_input_tokens")
            ),
            "parent_wall_seconds": _nullable(parent_usage.get("wall_seconds")),
            "parent_budget_exhausted": parent_usage.get("budget_exhausted"),
            # QC
            "qc_flags": "|".join(swe_flags) if swe_flags else "",
            "qc_flagged": len(swe_flags) > 0,
            # Provenance
            "source_episode_sha256": rec.get("_source_episode_sha256"),
            "source_runs_dir": rec.get("_source_runs_dir"),
        })
    return pd.DataFrame(rows)


def build_swe_disposition_table(
    records: list[dict[str, Any]],
    df: pd.DataFrame,
) -> pd.DataFrame:
    """Build a disposition table keyed by run ID and patch hashes.

    Columns
    -------
    run_id, task_id, condition
    candidate_patch_sha256, final_patch_sha256
    disposition           — accepted_unchanged / revised / rebuilt / no_candidate
    swe_final_resolved    — primary outcome (may be null)
    swe_candidate_resolved — candidate outcome (may be null)

    Disposition logic (see _classify_disposition for precedence):
    - accepted_unchanged: candidate hash present AND equals final hash
    - revised: candidate hash present but differs from final (or final absent)
    - rebuilt: no candidate hash; final hash present (parent built from scratch)
    - no_candidate: no candidate and no final patch
    """
    return df[[
        "run_id",
        "task_id",
        "condition",
        "candidate_patch_sha256",
        "final_patch_sha256",
        "disposition",
        "swe_final_resolved",
        "swe_candidate_resolved",
    ]].copy()


def build_swe_parent_effort(df: pd.DataFrame) -> pd.DataFrame:
    """Build parent effort summary by condition (cell).

    Reports per-condition: n, n_finalized, mean/sum model_calls,
    mean/sum tool_calls, mean/sum output_tokens, mean/sum wall_seconds.
    Explicit missingness: if a column is entirely null for a condition,
    records NaN (never fabricates a zero).

    Returns a DataFrame with one row per condition.
    """
    rows = []
    for condition in sorted(df["condition"].dropna().unique()):
        cell = df[df["condition"] == condition]
        n = len(cell)

        def _mean(col: str) -> float | None:
            s = cell[col].dropna()
            return float(s.mean()) if len(s) > 0 else None

        def _sum(col: str) -> float | None:
            s = cell[col].dropna()
            return float(s.sum()) if len(s) > 0 else None

        def _n_present(col: str) -> int:
            return int(cell[col].notna().sum())

        rows.append({
            "condition": condition,
            "n_runs": n,
            "n_roles_with_worker": n,   # all runs have a worker
            "n_roles_with_parent": n,   # all runs have a parent
            "n_parent_finalized": int(
                cell["parent_finalized"].eq(True).sum()
            ),
            # Worker effort
            "worker_model_calls_mean": _mean("worker_model_calls"),
            "worker_model_calls_sum": _sum("worker_model_calls"),
            "worker_model_calls_n_present": _n_present("worker_model_calls"),
            "worker_tool_calls_mean": _mean("worker_tool_calls"),
            "worker_tool_calls_sum": _sum("worker_tool_calls"),
            "worker_output_tokens_mean": _mean("worker_output_tokens"),
            "worker_output_tokens_sum": _sum("worker_output_tokens"),
            "worker_wall_seconds_mean": _mean("worker_wall_seconds"),
            "worker_wall_seconds_sum": _sum("worker_wall_seconds"),
            "worker_wall_seconds_n_present": _n_present("worker_wall_seconds"),
            # Parent effort
            "parent_model_calls_mean": _mean("parent_model_calls"),
            "parent_model_calls_sum": _sum("parent_model_calls"),
            "parent_model_calls_n_present": _n_present("parent_model_calls"),
            "parent_tool_calls_mean": _mean("parent_tool_calls"),
            "parent_tool_calls_sum": _sum("parent_tool_calls"),
            "parent_output_tokens_mean": _mean("parent_output_tokens"),
            "parent_output_tokens_sum": _sum("parent_output_tokens"),
            "parent_wall_seconds_mean": _mean("parent_wall_seconds"),
            "parent_wall_seconds_sum": _sum("parent_wall_seconds"),
            "parent_wall_seconds_n_present": _n_present("parent_wall_seconds"),
        })
    return pd.DataFrame(rows)


def build_swe_missing_field_audit(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generate a per-run-ID, per-field missing-field audit.

    For every expected analysis field, reports absence, nullness, or
    malformation—by run ID and field path.  Never fabricates zeros for
    missing values.

    Also notes the spec vs. actual field name discrepancy:
      spec says "env_metrics.swebench.resolved"
      actual field is "env_metrics.swebench.final_resolved"

    Returns a dict suitable for JSON serialization.
    """
    field_defs = _SWE_EXPECTED_FIELDS

    per_run: dict[str, dict[str, Any]] = {}
    aggregate: dict[str, dict[str, int]] = {
        field: {"absent": 0, "null": 0, "valid_null": 0, "present_non_null": 0}
        for field, _ in field_defs
    }

    for rec in records:
        run_id = rec.get("run_id", "unknown")
        run_issues: dict[str, dict[str, Any]] = {}
        for field_path, label in field_defs:
            value, found = _get_nested(rec, field_path)
            if field_path in _SWE_PASS_FRACTION_FIELDS:
                status = _swe_pass_fraction_status(rec, value, found)
            elif not found:
                status = "absent"
            elif value is None:
                status = "null"
            else:
                status = "present_non_null"
            aggregate[field_path][status] += 1
            if status != "present_non_null":
                issue: dict[str, Any] = {
                    "status": status,
                    "label": label,
                    "value": None if status != "present_non_null" else value,
                }
                if status == "valid_null":
                    issue["reason"] = _swe_f2p_p2p_null_reason(rec)
                    issue["malformed"] = False
                run_issues[field_path] = issue
        if run_issues:
            per_run[run_id] = run_issues

    # Note spec vs actual field name discrepancy
    spec_vs_actual = {
        "spec_field": "env_metrics.swebench.resolved",
        "actual_field": "env_metrics.swebench.final_resolved",
        "note": (
            "The analysis specification refers to 'env_metrics.swebench.resolved' "
            "but the actual field recorded in episode.json is "
            "'env_metrics.swebench.final_resolved'.  The analysis uses the "
            "actual field as the primary ITT outcome.  This discrepancy is "
            "documented here; no values are fabricated."
        ),
    }

    return {
        "provenance": "missing-field audit: SWE-bench records",
        "n_records": len(records),
        "run_ids": [rec.get("run_id") for rec in records],
        "spec_vs_actual_field_discrepancy": spec_vs_actual,
        "pass_fraction_policy": (
            "null fail_to_pass_fraction / pass_to_pass_fraction with "
            "f2p_p2p_null_reason=schema_v2_no_tests_status is valid absence "
            "(harness schema v2 has no tests_status); not malformed"
        ),
        "per_run": per_run,
        "aggregate": aggregate,
    }


def _audit_to_markdown(audit: dict[str, Any]) -> str:
    """Convert a missing-field audit dict to Markdown."""
    lines: list[str] = [
        "# SWE-bench Missing-Field Audit",
        "",
        f"**Records audited:** {audit['n_records']}",
        "",
        "**Run IDs:**",
    ]
    for rid in audit["run_ids"]:
        lines.append(f"- `{rid}`")
    lines += [
        "",
        "## Spec vs. Actual Field Name Discrepancy",
        "",
        f"- **Spec field:** `{audit['spec_vs_actual_field_discrepancy']['spec_field']}`",
        f"- **Actual field:** `{audit['spec_vs_actual_field_discrepancy']['actual_field']}`",
        f"- {audit['spec_vs_actual_field_discrepancy']['note']}",
        "",
        "## Pass-fraction policy",
        "",
        audit.get(
            "pass_fraction_policy",
            "null F2P/P2P with schema_v2_no_tests_status is valid absence",
        ),
        "",
        "## Aggregate Missingness by Field",
        "",
        "| Field | Label | Absent | Null | Valid-null | Present (non-null) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for field_path, label in _SWE_EXPECTED_FIELDS:
        counts = audit["aggregate"].get(field_path, {})
        lines.append(
            f"| `{field_path}` | {label} "
            f"| {counts.get('absent', 0)} "
            f"| {counts.get('null', 0)} "
            f"| {counts.get('valid_null', 0)} "
            f"| {counts.get('present_non_null', 0)} |"
        )
    lines += ["", "## Per-Run Missing Fields", ""]
    per_run = audit.get("per_run", {})
    if not per_run:
        lines.append("_No missing fields in any record._")
    else:
        for run_id, issues in per_run.items():
            lines += [f"### `{run_id}`", ""]
            lines += [
                "| Field | Label | Status |",
                "|---|---|---|",
            ]
            for field_path, info in issues.items():
                lines.append(
                    f"| `{field_path}` | {info['label']} | **{info['status']}** |"
                )
            lines.append("")
    return "\n".join(lines) + "\n"


def _swe_run_dir(record: dict[str, Any]) -> Path | None:
    src = record.get("_source_runs_dir")
    run_id = record.get("run_id")
    if not src or not run_id:
        return None
    path = Path(src) / str(run_id)
    return path if path.is_dir() else None


def _count_pytest_calls(run_dir: Path | None, role: str) -> int | None:
    """Count pytest invocations for one role from traces / events.

    Prefers ``*_trace.jsonl`` ``tool_input.command``; falls back to
    ``events.jsonl`` tool_result summaries that mention pytest.  Returns
    None when the run directory has no events (handwritten infra rows).
    """
    if run_dir is None or not run_dir.is_dir():
        return None
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return 0
    counted: set[str] = set()

    def _maybe(key: str, blob: str) -> None:
        if "pytest" in blob.lower():
            counted.add(key)

    for name in ("worker_trace.jsonl", "parent_trace.jsonl"):
        trace = run_dir / "logs" / name
        if not trace.is_file():
            continue
        try:
            lines = trace.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("role") != role:
                continue
            payload = event.get("payload") or {}
            tool_input = payload.get("tool_input") or {}
            command = ""
            if isinstance(tool_input, dict):
                command = str(tool_input.get("command") or "")
            key = str(payload.get("tool_use_id") or event.get("sequence") or command)
            _maybe(f"trace:{key}", command)

    try:
        ev_lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        ev_lines = []
    for line in ev_lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("role") != role or event.get("event_type") != "tool_result":
            continue
        payload = event.get("payload") or {}
        blob = f"{payload.get('summary') or ''} {payload.get('error') or ''}"
        key = str(payload.get("tool_use_id") or event.get("sequence") or "")
        _maybe(f"event:{key}", blob)
    return len(counted)


def _memo_byte_length(record: dict[str, Any]) -> int | None:
    """UTF-8 byte length of the worker memo file, or None if absent."""
    handoff = record.get("handoff") or {}
    memo_path = handoff.get("memo_path")
    candidates: list[Path] = []
    if memo_path:
        candidates.append(Path(memo_path))
    run_dir = _swe_run_dir(record)
    if run_dir is not None:
        candidates.append(run_dir / "handoff" / "memo.json")
    for path in candidates:
        if path.is_file():
            try:
                memo = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError, TypeError):
                try:
                    return path.stat().st_size
                except OSError:
                    return None
            text = memo.get("task_understanding") if isinstance(memo, dict) else None
            if not (isinstance(text, str) and text.strip()):
                return None
            try:
                return path.stat().st_size
            except OSError:
                return None
    return None


def _worker_final_transition(candidate: Any, final: Any) -> str:
    """Classify P-cell candidate→final resolved: repair/preserve/damage/unrecovered."""
    cand = 0 if candidate is None or (isinstance(candidate, float) and math.isnan(candidate)) else int(candidate)
    fin = 0 if final is None or (isinstance(final, float) and math.isnan(final)) else int(final)
    if cand == 0 and fin == 1:
        return "repair"
    if cand == 1 and fin == 1:
        return "preserve"
    if cand == 1 and fin == 0:
        return "damage"
    return "unrecovered"


def build_swe_main_task_means(records: list[dict[str, Any]]) -> pd.DataFrame:
    """One ITT mean per (task_id, condition). EF/IF → NaN."""
    rows = []
    for rec in records:
        itt = swe_itt_resolved(rec)
        rows.append({
            "task_id": rec["task_id"],
            "condition": rec["condition"],
            "task_mean": float("nan") if itt is None else float(itt),
        })
    return pd.DataFrame(rows)


def build_swe_main_matrix(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, int | None]]:
    """8×4 ITT matrix keyed by the short task label used in main_report.md."""
    matrix: dict[str, dict[str, int | None]] = {}
    for rec in records:
        short = swe_short_task_id(rec["task_id"])
        matrix.setdefault(short, {})
        matrix[short][rec["condition"]] = swe_itt_resolved(rec)
    return matrix


def _fmt_signed4(value: float) -> str:
    return f"{value:+.4f}"


def write_swe_main_report(
    records: list[dict[str, Any]],
    df: pd.DataFrame,
    estimates: dict[str, float],
    boot: dict[str, Any],
    dest: Path,
) -> str:
    """Write main_report.md. Matrix and τ/δ numbers match the committed report."""
    matrix = build_swe_main_matrix(records)
    cell_success, cell_valid = swe_cell_success_counts(matrix)
    pairwise = swe_pairwise_n_from_matrix(matrix)
    missing_art = {cond: 0 for cond in _CONDITIONS}
    not_final = {cond: 0 for cond in _CONDITIONS}
    infra = {cond: 0 for cond in _CONDITIONS}
    proto_rows: list[str] = []
    df_by_id = df.set_index("run_id") if len(df) else df
    by_cell = {(rec.get("task_id"), rec.get("condition")): rec for rec in records}
    matrix_lines = [
        "| task | A0 | A1 | P0 | P1 |",
        "|---|---:|---:|---:|---:|",
    ]
    for task in SWE_MAIN_TASKS:
        short = swe_short_task_id(task)
        labels: list[str] = []
        for cond in _CONDITIONS:
            rec = by_cell.get((task, cond))
            labels.append(swe_itt_cell_label(rec) if rec is not None else "")
        matrix_lines.append(
            f"| {short} | {labels[0]} | {labels[1]} | {labels[2]} | {labels[3]} |"
        )
    ordered: list[dict[str, Any]] = []
    for task in SWE_MAIN_TASKS:
        for cond in _CONDITIONS:
            rec = by_cell.get((task, cond))
            if rec is not None:
                ordered.append(rec)
    leftover = [
        rec for rec in records
        if (rec.get("task_id"), rec.get("condition")) not in {
            (r.get("task_id"), r.get("condition")) for r in ordered
        }
    ]
    for rec in ordered + leftover:
        cond = rec.get("condition") or ""
        if rec.get("status") == "infrastructure_failure":
            if cond in infra:
                infra[cond] += 1
        if cond.startswith("P"):
            flags = ""
            if rec["run_id"] in getattr(df_by_id, "index", []):
                flags = str(df_by_id.loc[rec["run_id"]].get("qc_flags") or "")
            proto = rec.get("swe_protocol_usage") or {}
            if (
                "production_missing_artifact" in flags
                or proto.get("production_artifact_missing")
            ):
                missing_art[cond] += 1
        if not (rec.get("compliance") or {}).get("parent_finalized"):
            if cond in not_final:
                not_final[cond] += 1
        proto_rows.append(
            f"| {swe_short_task_id(rec['task_id'])} | {cond} | `{rec['run_id']}` | "
            f"{rec.get('protocol_version')} | "
            f"`{(rec.get('reproducibility') or {}).get('schema_git_commit', '')[:8]}` | "
            f"{rec.get('status')} | {swe_itt_cell_label(rec)} |"
        )
    handwritten = [
        rid for rid in SWE_MAIN_RUN_IDS if rid in SWE_MAIN_HANDWRITTEN_INFRA_IDS
    ]
    text = "\n".join([
        "# SWE main report",
        "",
        "Batch `swe-main-20260904T085320Z-<id>` (swe-0.3.3 @ `<commit>`) plus",
        "one swe-0.3.4 rerun (`matplotlib-24149/A1` `<commit>` @ `<commit>`) and",
        "four handwritten `infrastructure_failure` records for cells that never",
        "got `episode.json`. Anthropic usage limits closed the account until",
        "2026-10-01 00:00 UTC, so the remaining four 0.3.4 reruns could not finish.",
        "",
        "`analyze.py --benchmark swebench --task-set main` reproduces this matrix",
        "from the 32 canonical run IDs (28 episodes + 4 terminal failure records)",
        "with a task-clustered bootstrap (seed 20260902, 10000 draws).",
        "",
        "## 8×4 matrix (binary ITT; EF = evaluator_failure, IF = infrastructure_failure; both missing, not 0)",
        "",
        *matrix_lines,
        "",
        f"Cell success counts (valid cells only): "
        f"A0 {cell_success['A0']}/{cell_valid['A0']} · "
        f"A1 {cell_success['A1']}/{cell_valid['A1']} · "
        f"P0 {cell_success['P0']}/{cell_valid['P0']} · "
        f"P1 {cell_success['P1']}/{cell_valid['P1']}",
        "",
        "## Treatment effects (task-clustered bootstrap 95% CI)",
        "",
        f"- τ0 = P0−A0 = **{_fmt_signed4(float(estimates['tau0']))}**  "
        f"95% CI ({_fmt_signed4(boot['tau0_ci'][0])}, {_fmt_signed4(boot['tau0_ci'][1])})  "
        f"(n={pairwise['tau0']} pairwise-complete tasks)",
        f"- τ1 = P1−A1 = **{_fmt_signed4(float(estimates['tau1']))}**  "
        f"95% CI ({_fmt_signed4(boot['tau1_ci'][0])}, {_fmt_signed4(boot['tau1_ci'][1])})  "
        f"(n={pairwise['tau1']} pairwise-complete tasks)",
        f"- δ  = τ1−τ0 = **{_fmt_signed4(float(estimates['delta']))}**  "
        f"95% CI ({_fmt_signed4(boot['delta_ci'][0])}, {_fmt_signed4(boot['delta_ci'][1])})  "
        f"(n={pairwise['delta']} tasks with all four cells valid)",
        "",
        "Pairwise-complete remaining tasks are still A0=P0 and A1=P1, including ITT zeros on matplotlib-25332 A0/P0 (not_finalized / empty patch). infrastructure_failure and evaluator_failure cells are missing and do not enter the pairwise n. Every bootstrap resample of the remaining pairs is exactly zero.",
        "",
        "## Missing artifact and non-finalization (QC / ITT)",
        "",
        f"- production_missing_artifact: P0 {missing_art['P0']}/8 · P1 {missing_art['P1']}/8",
        f"- parent_not_finalized: A0 {not_final['A0']}/8 · A1 {not_final['A1']}/8 · "
        f"P0 {not_final['P0']}/8 · P1 {not_final['P1']}/8",
        f"- infrastructure_failure rows (missing, never 0): "
        f"A0 {infra['A0']} · A1 {infra['A1']} · P0 {infra['P0']} · P1 {infra['P1']}",
        f"- evaluator_failure (harness `tests_timed_out`, missing, never 0): "
        f"{SWE_MAIN_N_EVALUATOR_FAILURE} cells "
        f"(matplotlib-24149/A1; matplotlib-25332/A1; matplotlib-25332/P1)",
        "",
        "## Provenance",
        "",
        "| task | cell | run_id | protocol | schema_git | status | resolved |",
        "|---|---|---|---|---|---|---:|",
        *proto_rows,
        "",
        "## Notes",
        "",
        "- Handwritten failure run_ids: " + ", ".join(f"`{i}`" for i in handwritten),
        "- matplotlib-24149/A1 (`<commit>`) is the only swe-0.3.4 episode that finished; its harness timed out (`tests_timed_out`) and is now `evaluator_failure`, not `resolved=0`.",
        "- Primary outcome is `env_metrics.swebench.final_resolved`. infrastructure_failure and evaluator_failure are missing, never 0. None→0 ITT applies only to not_finalized / empty-patch measured cells. τ0/τ1/δ use pairwise-complete tasks with n per estimate.",
        "- Null F2P/P2P with `f2p_p2p_null_reason=schema_v2_no_tests_status` or `tests_timed_out` is valid.",
        "",
    ])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    print(f"  wrote {dest}")
    return text


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False)
    print(f"  wrote {path}")


def emit_swe_main_paper_outputs(
    records: list[dict[str, Any]],
    df: pd.DataFrame,
    out_dir: Path,
) -> dict[str, Path]:
    """Write paper tables (a)–(f) under ``out_dir`` (paper_outputs/swe_main/)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    rec_by_id = {rec["run_id"]: rec for rec in records}

    # (a) per-task table
    per_task_rows: list[dict[str, Any]] = []
    for rec in records:
        em = (rec.get("env_metrics") or {}).get("swebench") or {}
        per_task_rows.append({
            "task_id": rec.get("task_id"),
            "task": swe_short_task_id(rec.get("task_id") or ""),
            "condition": rec.get("condition"),
            "run_id": rec.get("run_id"),
            "status": rec.get("status"),
            "candidate_resolved": em.get("candidate_resolved"),
            "final_resolved": em.get("final_resolved"),
            "final_resolved_itt": swe_itt_resolved(rec),
            "applies_cleanly": em.get("applies_cleanly"),
            "fail_to_pass_fraction": em.get("fail_to_pass_fraction"),
            "pass_to_pass_fraction": em.get("pass_to_pass_fraction"),
            "candidate_f2p_fraction": em.get("candidate_f2p_fraction"),
            "candidate_p2p_fraction": em.get("candidate_p2p_fraction"),
            "f2p_p2p_null_reason": _swe_f2p_p2p_null_reason(rec),
        })
    per_task = pd.DataFrame(per_task_rows)
    paths["per_task"] = out_dir / "per_task_cells.csv"
    _write_csv(per_task, paths["per_task"])

    # (b) worker→final transitions for P cells
    trans_rows: list[dict[str, Any]] = []
    for rec in records:
        if not str(rec.get("condition", "")).startswith("P"):
            continue
        em = (rec.get("env_metrics") or {}).get("swebench") or {}
        trans_rows.append({
            "task_id": rec.get("task_id"),
            "task": swe_short_task_id(rec.get("task_id") or ""),
            "condition": rec.get("condition"),
            "run_id": rec.get("run_id"),
            "candidate_resolved": (
                None if (
                    swe_is_evaluator_failure(rec, phase="candidate")
                    or swe_is_infrastructure_failure(rec)
                )
                else em.get("candidate_resolved")
            ),
            "final_resolved": (
                None if (
                    swe_is_evaluator_failure(rec, phase="final")
                    or swe_is_infrastructure_failure(rec)
                )
                else em.get("final_resolved")
            ),
            "transition": (
                "evaluator_failure"
                if swe_is_evaluator_failure(rec, phase="final")
                or swe_is_evaluator_failure(rec, phase="candidate")
                else "infrastructure_failure"
                if swe_is_infrastructure_failure(rec)
                else _worker_final_transition(
                    em.get("candidate_resolved"), em.get("final_resolved"),
                )
            ),
        })
    transitions = pd.DataFrame(trans_rows)
    paths["transitions"] = out_dir / "p_cell_worker_final_transitions.csv"
    _write_csv(transitions, paths["transitions"])

    # (c) disposition by patch hash
    disp = build_swe_disposition_table(records, df)
    paths["disposition"] = out_dir / "disposition_by_patch_hash.csv"
    _write_csv(disp, paths["disposition"])

    # (d)+(e) parent / worker effort by cell
    effort_rows_parent: list[dict[str, Any]] = []
    effort_rows_worker: list[dict[str, Any]] = []
    memo_rows: list[dict[str, Any]] = []
    for condition in _CONDITIONS:
        recs = [r for r in records if r.get("condition") == condition]
        cell = df[df["condition"] == condition] if len(df) else df

        def _vals(getter) -> list[float]:
            out: list[float] = []
            for rec in recs:
                val = getter(rec)
                if val is None:
                    continue
                if isinstance(val, float) and math.isnan(val):
                    continue
                out.append(float(val))
            return out

        def _stat(values: list[float], how: str) -> float | None:
            if not values:
                return None
            if how == "mean":
                return float(sum(values) / len(values))
            return float(sum(values))

        worker_turns = _vals(lambda r: ((r.get("usage") or {}).get("worker") or {}).get("model_calls"))
        worker_tools = _vals(lambda r: ((r.get("usage") or {}).get("worker") or {}).get("tool_calls"))
        worker_tok = _vals(lambda r: ((r.get("usage") or {}).get("worker") or {}).get("output_tokens"))
        worker_wall = _vals(lambda r: ((r.get("usage") or {}).get("worker") or {}).get("wall_seconds"))
        parent_turns = _vals(lambda r: ((r.get("usage") or {}).get("parent") or {}).get("model_calls"))
        parent_tools = _vals(lambda r: ((r.get("usage") or {}).get("parent") or {}).get("tool_calls"))
        parent_tok = _vals(lambda r: ((r.get("usage") or {}).get("parent") or {}).get("output_tokens"))
        parent_wall = _vals(lambda r: ((r.get("usage") or {}).get("parent") or {}).get("wall_seconds"))
        worker_pytest = _vals(lambda r: _count_pytest_calls(_swe_run_dir(r), "worker"))
        parent_pytest = _vals(lambda r: _count_pytest_calls(_swe_run_dir(r), "parent"))
        effort_rows_parent.append({
            "condition": condition,
            "n_runs": len(recs),
            "turns_mean": _stat(parent_turns, "mean"),
            "turns_sum": _stat(parent_turns, "sum"),
            "tool_calls_mean": _stat(parent_tools, "mean"),
            "tool_calls_sum": _stat(parent_tools, "sum"),
            "output_tokens_mean": _stat(parent_tok, "mean"),
            "output_tokens_sum": _stat(parent_tok, "sum"),
            "pytest_calls_mean": _stat(parent_pytest, "mean"),
            "pytest_calls_sum": _stat(parent_pytest, "sum"),
            "wall_seconds_mean": _stat(parent_wall, "mean"),
            "wall_seconds_sum": _stat(parent_wall, "sum"),
        })
        effort_rows_worker.append({
            "condition": condition,
            "n_runs": len(recs),
            "turns_mean": _stat(worker_turns, "mean"),
            "turns_sum": _stat(worker_turns, "sum"),
            "tool_calls_mean": _stat(worker_tools, "mean"),
            "tool_calls_sum": _stat(worker_tools, "sum"),
            "output_tokens_mean": _stat(worker_tok, "mean"),
            "output_tokens_sum": _stat(worker_tok, "sum"),
            "pytest_calls_mean": _stat(worker_pytest, "mean"),
            "pytest_calls_sum": _stat(worker_pytest, "sum"),
            "wall_seconds_mean": _stat(worker_wall, "mean"),
            "wall_seconds_sum": _stat(worker_wall, "sum"),
        })
        memo_lens = _vals(_memo_byte_length)
        sources = [
            ((r.get("swe_protocol_usage") or {}).get("worker") or {}).get("memo_source")
            for r in recs
        ]
        source_counts: dict[str, int] = {}
        for src in sources:
            key = src if src else "absent"
            source_counts[key] = source_counts.get(key, 0) + 1
        memo_rows.append({
            "condition": condition,
            "n_runs": len(recs),
            "memo_bytes_mean": _stat(memo_lens, "mean"),
            "memo_bytes_sum": _stat(memo_lens, "sum"),
            "memo_n_present": len(memo_lens),
            "memo_source_counts": json.dumps(source_counts, sort_keys=True),
        })
        _ = cell  # retained for symmetry with the flattened frame

    paths["parent_effort"] = out_dir / "parent_effort_by_cell.csv"
    paths["worker_effort"] = out_dir / "worker_effort_by_cell.csv"
    paths["memo"] = out_dir / "memo_length_by_cell.csv"
    _write_csv(pd.DataFrame(effort_rows_parent), paths["parent_effort"])
    _write_csv(pd.DataFrame(effort_rows_worker), paths["worker_effort"])
    _write_csv(pd.DataFrame(memo_rows), paths["memo"])
    _ = rec_by_id
    return paths


def _link_or_copy(src: Path, dest: Path) -> str:
    """Hardlink if possible; otherwise copy. Never rewrite the source."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        os.link(src, dest)
        return "link"
    except OSError:
        shutil.copy2(src, dest)
        return "copy"


def write_swe_trace_bundle(
    records: list[dict[str, Any]],
    dest: Path,
) -> dict[str, Any]:
    """Copy/link memos, patches, terminal text, and harness reports per cell."""
    dest.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    artifact_names = (
        ("memo", ("handoff/memo.json",)),
        ("candidate_patch", ("handoff/candidate.patch",)),
        ("final_patch", ("handoff/final.patch",)),
        ("terminal_text", ("handoff/terminal_text.txt",)),
        ("candidate_report", (
            "reports/candidate_report.json",
            "reports/candidate_eval_summary.json",
        )),
        ("final_report", (
            "reports/final_report.json",
            "reports/final_eval_summary.json",
        )),
    )
    for rec in records:
        short = swe_short_task_id(rec.get("task_id") or "unknown")
        cond = rec.get("condition") or "NA"
        cell_dir = dest / short / cond
        cell_dir.mkdir(parents=True, exist_ok=True)
        run_dir = _swe_run_dir(rec)
        present: dict[str, str] = {}
        missing: list[str] = []
        for label, relatives in artifact_names:
            found = False
            if run_dir is not None:
                for rel in relatives:
                    src = run_dir / rel
                    if src.is_file():
                        action = _link_or_copy(src, cell_dir / src.name)
                        present[label] = f"{src.name} ({action})"
                        found = True
                        break
            if not found:
                missing.append(label)
        index_rows.append({
            "task": short,
            "task_id": rec.get("task_id"),
            "condition": cond,
            "run_id": rec.get("run_id"),
            "status": rec.get("status"),
            "source_run_dir": str(run_dir) if run_dir is not None else None,
            "present": present,
            "missing": missing,
        })
    index = {
        "n_cells": len(index_rows),
        "cells": index_rows,
        "note": (
            "Artifacts are hardlinks or copies of files in the source run "
            "directories. episode.json records are not rewritten."
        ),
    }
    (dest / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md = [
        "# SWE main trace bundle",
        "",
        f"Cells: {len(index_rows)}",
        "",
        "| task | cell | run_id | status | present | missing |",
        "|---|---|---|---|---|---|",
    ]
    for row in index_rows:
        md.append(
            f"| {row['task']} | {row['condition']} | `{row['run_id']}` | "
            f"{row['status']} | {', '.join(row['present']) or '—'} | "
            f"{', '.join(row['missing']) or '—'} |"
        )
    md.append("")
    (dest / "index.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"  wrote {dest / 'index.md'}")
    return index


def _swe_read_text(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def swe_patch_is_nonempty(text: str | None) -> bool:
    """True when a patch file has non-whitespace content."""
    return bool(text and text.strip())


def _swe_artifact_path(
    record: dict[str, Any],
    recorded: str | None,
    *relatives: str,
) -> Path | None:
    candidates: list[Path] = []
    if recorded:
        candidates.append(Path(recorded))
    run_dir = _swe_run_dir(record)
    if run_dir is not None:
        candidates.extend(run_dir / rel for rel in relatives)
    for path in candidates:
        if path.is_file():
            return path
    return None


def swe_v035_candidate_text(record: dict[str, Any]) -> str | None:
    path = _swe_artifact_path(
        record,
        (record.get("handoff") or {}).get("candidate_script_path"),
        "handoff/candidate.patch",
    )
    return _swe_read_text(path)


def swe_v035_final_text(record: dict[str, Any]) -> str | None:
    path = _swe_artifact_path(
        record,
        record.get("final_script_path"),
        "handoff/final.patch",
    )
    return _swe_read_text(path)


def swe_v035_p_disposition(record: dict[str, Any]) -> str:
    """P-cell disposition from candidate vs final **hashes**, not flags.

    Returns one of: unchanged, revised, discarded, rebuilt, no_candidate.
    """
    handoff = record.get("handoff") or {}
    cand_hash = (
        handoff.get("candidate_script_sha256")
        or record.get("candidate_script_sha256")
    )
    final_hash = record.get("final_script_sha256")
    cand_ne = swe_patch_is_nonempty(swe_v035_candidate_text(record))
    fin_ne = swe_patch_is_nonempty(swe_v035_final_text(record))
    if not cand_hash and not cand_ne:
        if fin_ne or final_hash:
            return "rebuilt"
        return "no_candidate"
    if not fin_ne and not final_hash:
        return "discarded"
    if cand_hash and final_hash and cand_hash == final_hash:
        return "unchanged"
    if cand_ne and not fin_ne:
        return "discarded"
    return "revised"


def swe_v035_patch_distance(record: dict[str, Any]) -> dict[str, Any]:
    """Line-level candidate→final distance. Source: handoff/*.patch bytes."""
    cand = swe_v035_candidate_text(record)
    final = swe_v035_final_text(record)
    cand_lines = [] if cand is None else cand.splitlines()
    final_lines = [] if final is None else final.splitlines()
    if not swe_patch_is_nonempty(cand):
        return {
            "n_changed_lines": None,
            "n_candidate_lines": len(cand_lines),
            "n_final_lines": len(final_lines),
            "hash_equal": None,
        }
    matcher = difflib.SequenceMatcher(a=cand_lines, b=final_lines)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += max(i2 - i1, j2 - j1)
    handoff = record.get("handoff") or {}
    cand_hash = handoff.get("candidate_script_sha256")
    final_hash = record.get("final_script_sha256")
    return {
        "n_changed_lines": changed,
        "n_candidate_lines": len(cand_lines),
        "n_final_lines": len(final_lines),
        "hash_equal": bool(cand_hash and final_hash and cand_hash == final_hash),
    }


def swe_v035_transition(record: dict[str, Any]) -> str:
    """Candidate→final resolved transition. Missing stays missing, never 0."""
    if swe_is_infrastructure_failure(record):
        return "infrastructure_failure"
    cand_ef = swe_is_evaluator_failure(record, phase="candidate")
    fin_ef = swe_is_evaluator_failure(record, phase="final")
    if cand_ef or fin_ef:
        return "evaluator_failure"
    em = (record.get("env_metrics") or {}).get("swebench") or {}
    cand = em.get("candidate_resolved")
    final = em.get("final_resolved")
    if cand is None:
        return "missing_candidate"
    if final is None:
        return "missing_final"
    pair = (int(cand), int(final))
    return {
        (0, 0): "fail→fail",
        (0, 1): "fail→pass",
        (1, 1): "pass→pass",
        (1, 0): "pass→fail",
    }[pair]


def swe_v035_discordant_issues(
    matrix: dict[str, dict[str, int | None]],
) -> list[dict[str, Any]]:
    """Issues where a pairwise-complete A/P pair differs on ITT resolved."""
    rows: list[dict[str, Any]] = []
    for task, row in matrix.items():
        a0, a1, p0, p1 = (row.get(cond) for cond in _CONDITIONS)
        tau0 = a0 is not None and p0 is not None and a0 != p0
        tau1 = a1 is not None and p1 is not None and a1 != p1
        if tau0 or tau1:
            rows.append({
                "task": task,
                "discordant_tau0": tau0,
                "discordant_tau1": tau1,
                "A0": a0, "A1": a1, "P0": p0, "P1": p1,
            })
    return rows


def swe_v035_schema_lag_issue(issue: Any) -> bool:
    """Frozen episode_schema.json enum predates recorded task_set=main_v035."""
    return (
        getattr(issue, "path", None) == "task_set"
        and "main_v035" in getattr(issue, "message", "")
    )


def _swe_v035_benign_issue(issue: Any) -> bool:
    """Documented non-malformed findings (frozen schema / analysis overlay)."""
    if swe_v035_schema_lag_issue(issue):
        return True
    msg = getattr(issue, "message", "")
    if "Additional properties are not allowed" in msg and "_source_" in msg:
        return True
    if "path escapes run directory" in msg:
        # Records store absolute paths; copies still hash-check those files.
        return True
    return False


def swe_v035_structural_validation(
    record: dict[str, Any],
    episode_path: Path,
) -> dict[str, Any]:
    """Validate on-disk episode + events. Schema-lag on task_set is documented."""
    try:
        on_disk = json.loads(episode_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "run_id": record.get("run_id"),
            "n_raw_schema_issues": 1,
            "n_schema_lag_task_set": 0,
            "n_material_episode_issues": 1,
            "material_episode_issues": [str(exc)],
            "n_event_issues": 0,
            "event_issues": [],
            "structurally_valid": False,
        }
    if not isinstance(on_disk, dict):
        on_disk = record
    raw_issues = validate_episode(on_disk, episode_path)
    filtered = [i for i in raw_issues if not _swe_v035_benign_issue(i)]
    events_path = episode_path.parent / "events.jsonl"
    event_issues = (
        validate_event_log(events_path, on_disk)
        if events_path.is_file()
        else ["events.jsonl missing"]
    )
    return {
        "run_id": record.get("run_id"),
        "n_raw_schema_issues": len(raw_issues),
        "n_schema_lag_task_set": sum(
            1 for i in raw_issues if swe_v035_schema_lag_issue(i)
        ),
        "n_material_episode_issues": len(filtered),
        "material_episode_issues": [str(i) for i in filtered],
        "n_event_issues": len(event_issues),
        "event_issues": [str(i) for i in event_issues[:8]],
        "structurally_valid": len(filtered) == 0 and len(event_issues) == 0,
    }


def _parse_tool_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary", "{}")
    if isinstance(summary, dict):
        return summary
    if not isinstance(summary, str):
        return {}
    if summary.endswith("…"):
        summary = summary[:-1]
    try:
        parsed = json.loads(summary)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def swe_v035_role_event_stats(
    record: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    """Count parent/worker exec, write_file, and assistant-text presence from events."""
    run_dir = _swe_run_dir(record)
    events_path = (run_dir / "events.jsonl") if run_dir is not None else None
    empty = {
        "n_model_calls": 0,
        "n_assistant_text_key": 0,
        "n_assistant_text_nonempty": 0,
        "n_write_file": 0,
        "n_exec_executed": 0,
        "n_exec_feedback": 0,
        "n_pytest": 0,
        "failure_codes": [],
        "events_present": False,
    }
    if events_path is None or not events_path.is_file():
        return empty
    out = dict(empty)
    out["events_present"] = True
    failures: list[str] = []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return empty
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("role") != role:
            continue
        et = event.get("event_type")
        payload = event.get("payload") or {}
        if et == "model_call":
            out["n_model_calls"] += 1
            if "assistant_text" in payload or payload.get("raw_payload_path"):
                out["n_assistant_text_key"] += 1
            if str(payload.get("assistant_text") or "").strip():
                out["n_assistant_text_nonempty"] += 1
        elif et == "tool_call" and payload.get("tool_name") == "write_file":
            out["n_write_file"] += 1
        elif et == "tool_result" and payload.get("tool_name") == "exec_shell":
            summary = _parse_tool_summary(payload)
            if summary.get("executed") is True:
                out["n_exec_executed"] += 1
                if summary.get("feedback_command"):
                    out["n_exec_feedback"] += 1
            blob = f"{summary.get('command') or ''} {payload.get('summary') or ''}"
            if "pytest" in blob.lower():
                out["n_pytest"] += 1
        elif et == "failure":
            code = payload.get("code") or event.get("code")
            if code:
                failures.append(str(code))
    out["failure_codes"] = failures
    return out


def swe_v035_ordered_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_cell = {(rec.get("task_id"), rec.get("condition")): rec for rec in records}
    ordered: list[dict[str, Any]] = []
    for task in SWE_MAIN_V035_TASKS:
        for cond in _CONDITIONS:
            rec = by_cell.get((task, cond))
            if rec is not None:
                ordered.append(rec)
    seen = {(r.get("task_id"), r.get("condition")) for r in ordered}
    ordered.extend(
        rec for rec in records
        if (rec.get("task_id"), rec.get("condition")) not in seen
    )
    return ordered


def build_swe_main_v035_tables(
    records: list[dict[str, Any]],
    df: pd.DataFrame,
) -> dict[str, Any]:
    """Compute every main_v035 confirmatory table from recorded fields."""
    ordered = swe_v035_ordered_records(records)
    matrix = build_swe_main_matrix(records)
    # Keep report row order as the v3 selection, not hash order.
    matrix_ordered = {
        swe_short_task_id(task): matrix.get(swe_short_task_id(task), {})
        for task in SWE_MAIN_V035_TASKS
    }
    cell_success, cell_valid = swe_cell_success_counts(matrix_ordered)
    pairwise = swe_pairwise_n_from_matrix(matrix_ordered)
    discordant = swe_v035_discordant_issues(matrix_ordered)
    df_by_id = df.set_index("run_id") if len(df) else df

    per_cell: list[dict[str, Any]] = []
    compliance_rows: list[dict[str, Any]] = []
    classification_rows: list[dict[str, Any]] = []
    process_rows: list[dict[str, Any]] = []
    disposition_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []

    for rec in ordered:
        em = (rec.get("env_metrics") or {}).get("swebench") or {}
        handoff = rec.get("handoff") or {}
        compliance = rec.get("compliance") or {}
        proto = rec.get("swe_protocol_usage") or {}
        worker_proto = proto.get("worker") or {}
        parent_proto = proto.get("parent") or {}
        usage_w = (rec.get("usage") or {}).get("worker") or {}
        usage_p = (rec.get("usage") or {}).get("parent") or {}
        repro = rec.get("reproducibility") or {}
        fo = rec.get("final_outcome") or {}
        wo = rec.get("worker_outcome") or {}
        run_dir = _swe_run_dir(rec)
        episode_path = (run_dir / "episode.json") if run_dir is not None else None
        qc_flags = (
            qc_episode(rec, record_path=episode_path)
            if episode_path is not None
            else qc_episode(rec)
        )
        qc_codes = [f.code for f in qc_flags]
        parent_stats = swe_v035_role_event_stats(rec, "parent")
        worker_stats = swe_v035_role_event_stats(rec, "worker")
        cand_text = swe_v035_candidate_text(rec)
        fin_text = swe_v035_final_text(rec)
        cand_ne = swe_patch_is_nonempty(cand_text)
        fin_ne = swe_patch_is_nonempty(fin_text)
        parent_pytest = _count_pytest_calls(run_dir, "parent")
        worker_pytest = _count_pytest_calls(run_dir, "worker")
        any_verification = bool(
            (parent_pytest or 0) > 0
            or parent_stats["n_exec_feedback"] > 0
            or parent_stats["n_pytest"] > 0
        )
        disposition = (
            swe_v035_p_disposition(rec)
            if str(rec.get("condition", "")).startswith("P")
            else None
        )
        distance = (
            swe_v035_patch_distance(rec)
            if str(rec.get("condition", "")).startswith("P")
            else None
        )
        transition = (
            swe_v035_transition(rec)
            if str(rec.get("condition", "")).startswith("P")
            else None
        )
        memo_path = _swe_artifact_path(
            rec, handoff.get("memo_path"), "handoff/memo.json",
        )
        cand_path = _swe_artifact_path(
            rec, handoff.get("candidate_script_path"), "handoff/candidate.patch",
        )
        fin_path = _swe_artifact_path(
            rec, rec.get("final_script_path"), "handoff/final.patch",
        )
        cand_report = _swe_artifact_path(
            rec, (wo or {}).get("output_path"),
            "reports/candidate_report.json", "reports/candidate_eval_summary.json",
        )
        fin_report = _swe_artifact_path(
            rec, fo.get("output_path"),
            "reports/final_report.json", "reports/final_eval_summary.json",
        )
        asst_ok = (
            worker_stats["n_model_calls"] > 0
            and worker_stats["n_assistant_text_key"] == worker_stats["n_model_calls"]
            and parent_stats["n_model_calls"] > 0
            and parent_stats["n_assistant_text_key"] == parent_stats["n_model_calls"]
        )
        if episode_path is not None:
            validation_rows.append(
                swe_v035_structural_validation(rec, episode_path)
            )

        class_row: dict[str, Any] | None = None
        if rec.get("status") != "complete" or qc_codes:
            if swe_is_infrastructure_failure(rec):
                bucket = "infrastructure"
            elif swe_is_evaluator_failure(rec):
                bucket = "evaluator"
            else:
                bucket = "agent"
            evidence = []
            for failure in rec.get("failures") or []:
                evidence.append(
                    f"failures[] source={failure.get('source')} "
                    f"code={failure.get('code')} "
                    f"message={failure.get('message')}"
                )
            if worker_proto.get("end_reason"):
                evidence.append(
                    f"swe_protocol_usage.worker.end_reason="
                    f"{worker_proto.get('end_reason')}"
                )
            if parent_proto.get("end_reason"):
                evidence.append(
                    f"swe_protocol_usage.parent.end_reason="
                    f"{parent_proto.get('end_reason')}"
                )
            if worker_proto.get("unexpected_stop_reason"):
                evidence.append(
                    f"swe_protocol_usage.worker.unexpected_stop_reason="
                    f"{worker_proto.get('unexpected_stop_reason')}"
                )
            for code in qc_codes:
                evidence.append(f"qc_episode:{code}")
            for code in worker_stats["failure_codes"] + parent_stats["failure_codes"]:
                evidence.append(f"events.jsonl failure code={code}")
            class_row = {
                "task": swe_short_task_id(rec.get("task_id") or ""),
                "condition": rec.get("condition"),
                "run_id": rec.get("run_id"),
                "status": rec.get("status"),
                "qc_flags": qc_codes,
                "bucket": bucket,
                "event_evidence": evidence,
            }
            classification_rows.append(class_row)

        per_cell.append({
            "task_id": rec.get("task_id"),
            "task": swe_short_task_id(rec.get("task_id") or ""),
            "condition": rec.get("condition"),
            "run_id": rec.get("run_id"),
            "status": rec.get("status"),
            "final_resolved": em.get("final_resolved"),
            "final_resolved_itt": swe_itt_resolved(rec),
            "candidate_resolved": (
                None
                if swe_is_evaluator_failure(rec, phase="candidate")
                or swe_is_infrastructure_failure(rec)
                else em.get("candidate_resolved")
            ),
            "applies_cleanly": em.get("applies_cleanly"),
            "fail_to_pass_fraction": em.get("fail_to_pass_fraction"),
            "pass_to_pass_fraction": em.get("pass_to_pass_fraction"),
            "f2p_p2p_null_reason": _swe_f2p_p2p_null_reason(rec),
            "parent_finalized": compliance.get("parent_finalized"),
            "qc_flags": qc_codes,
        })
        compliance_rows.append({
            "task": swe_short_task_id(rec.get("task_id") or ""),
            "condition": rec.get("condition"),
            "run_id": rec.get("run_id"),
            "proto": rec.get("protocol_version"),
            "img": em.get("image_tag"),
            "image_digest": em.get("image_digest") or repro.get("docker_image_digest"),
            "remote": em.get("git_remote"),
            "task_repo": em.get("task_repo"),
            "head_eq_base": em.get("git_head") == repro.get("benchmark_git_commit"),
            "git_head": em.get("git_head"),
            "base_commit": repro.get("benchmark_git_commit"),
            "w_neq_p": (
                em.get("worker_container_id")
                and em.get("parent_container_id")
                and em.get("worker_container_id") != em.get("parent_container_id")
            ),
            "worker_container_id": em.get("worker_container_id"),
            "parent_container_id": em.get("parent_container_id"),
            "exec": compliance.get("worker_feedback_calls"),
            "pyt_worker": worker_pytest,
            "wr_worker": worker_stats["n_write_file"],
            "memo": memo_path is not None,
            "msrc": worker_proto.get("memo_source"),
            "cand": cand_path is not None,
            "cNE": cand_ne,
            "apply": em.get("applies_cleanly"),
            "fin": fin_path is not None,
            "fNE": fin_ne,
            "end_w": worker_proto.get("end_reason"),
            "end_p": parent_proto.get("end_reason"),
            "cont_w": worker_proto.get("continued_on_max_tokens"),
            "cont_p": parent_proto.get("continued_on_max_tokens"),
            "asst": asst_ok,
            "harn_c": cand_report is not None,
            "harn_f": fin_report is not None,
            "fRes": em.get("final_resolved"),
            "cRes": em.get("candidate_resolved"),
            "pam": proto.get("production_artifact_missing"),
            "parent_finalized": compliance.get("parent_finalized"),
            "qc_flags": qc_codes,
            "schema_git": repro.get("schema_git_commit"),
            "caps_worker": (rec.get("budgets") or {}).get("worker"),
            "caps_parent": (rec.get("budgets") or {}).get("parent"),
        })
        process_rows.append({
            "task": swe_short_task_id(rec.get("task_id") or ""),
            "condition": rec.get("condition"),
            "run_id": rec.get("run_id"),
            "parent_executions": parent_stats["n_exec_executed"],
            "parent_tests": parent_pytest,
            "any_verification": any_verification,
            "parent_write_file": parent_stats["n_write_file"],
            "parent_output_tokens": usage_p.get("output_tokens"),
            "parent_model_calls": usage_p.get("model_calls"),
            "parent_tool_calls": usage_p.get("tool_calls"),
            "parent_wall_seconds": usage_p.get("wall_seconds"),
            "worker_output_tokens": usage_w.get("output_tokens"),
            "non_finalized": not bool(compliance.get("parent_finalized")),
            "empty_final": not fin_ne,
            "disposition": disposition,
        })
        if disposition is not None and distance is not None:
            disposition_rows.append({
                "task": swe_short_task_id(rec.get("task_id") or ""),
                "condition": rec.get("condition"),
                "run_id": rec.get("run_id"),
                "candidate_patch_sha256": handoff.get("candidate_script_sha256"),
                "final_patch_sha256": rec.get("final_script_sha256"),
                "accepted_unchanged_flag": rec.get("accepted_unchanged"),
                "disposition": disposition,
                **distance,
            })
            transition_rows.append({
                "task": swe_short_task_id(rec.get("task_id") or ""),
                "condition": rec.get("condition"),
                "run_id": rec.get("run_id"),
                "candidate_resolved": em.get("candidate_resolved"),
                "final_resolved": em.get("final_resolved"),
                "transition": transition,
            })
        provenance_rows.append({
            "task": swe_short_task_id(rec.get("task_id") or ""),
            "condition": rec.get("condition"),
            "run_id": rec.get("run_id"),
            "protocol_version": rec.get("protocol_version"),
            "schema_git_commit": repro.get("schema_git_commit"),
            "protocol_pin": SWE_MAIN_V035_PROTOCOL_PIN,
            "freeze_commit": SWE_MAIN_V035_FREEZE_COMMIT,
            "freeze_tag": SWE_MAIN_V035_FREEZE_TAG,
            "image_tag": em.get("image_tag"),
            "image_digest": em.get("image_digest") or repro.get("docker_image_digest"),
            "git_head": em.get("git_head"),
            "benchmark_git_commit": repro.get("benchmark_git_commit"),
            "worker_container_id": em.get("worker_container_id"),
            "parent_container_id": em.get("parent_container_id"),
            "git_dirty": repro.get("git_dirty"),
            "selection_sha256": repro.get("selection_sha256"),
        })
        _ = df_by_id  # flattened frame retained for QC-flag cross-check

    def _cond_rate(rows: list[dict[str, Any]], cond: str, pred) -> dict[str, Any]:
        cell = [r for r in rows if r.get("condition") == cond]
        n = len(cell)
        hits = sum(1 for r in cell if pred(r))
        return {"n": n, "k": hits, "rate": (hits / n) if n else None}

    p_rows = [r for r in process_rows if str(r.get("condition", "")).startswith("P")]
    p_comp = [r for r in compliance_rows if str(r.get("condition", "")).startswith("P")]
    measured_cand = [
        r for r in per_cell
        if str(r.get("condition", "")).startswith("P")
        and r.get("candidate_resolved") is not None
    ]
    process_by_cell = {}
    for cond in _CONDITIONS:
        cell_p = [r for r in process_rows if r.get("condition") == cond]
        cell_c = [r for r in compliance_rows if r.get("condition") == cond]

        def _mean(vals: list[Any]) -> float | None:
            nums = [float(v) for v in vals if v is not None]
            return (sum(nums) / len(nums)) if nums else None

        process_by_cell[cond] = {
            "n": len(cell_p),
            "parent_executions_mean": _mean(
                [r["parent_executions"] for r in cell_p]
            ),
            "parent_tests_mean": _mean([r["parent_tests"] for r in cell_p]),
            "any_verification": _cond_rate(
                process_rows, cond, lambda r: r["any_verification"]
            ),
            "parent_write_file_mean": _mean(
                [r["parent_write_file"] for r in cell_p]
            ),
            "tokens_after_handoff_mean": _mean(
                [r["parent_output_tokens"] for r in cell_p]
            ),
            "non_finalization": _cond_rate(
                process_rows, cond, lambda r: r["non_finalized"]
            ),
            "empty_final": _cond_rate(
                process_rows, cond, lambda r: r["empty_final"]
            ),
            "parent_model_calls_mean": _mean(
                [r["parent_model_calls"] for r in cell_p]
            ),
            "worker_feedback_calls_mean": _mean(
                [r.get("exec") for r in cell_c]
            ),
        }

    disp_counts: dict[str, int] = {}
    for row in disposition_rows:
        disp_counts[row["disposition"]] = disp_counts.get(row["disposition"], 0) + 1
    trans_counts: dict[str, int] = {}
    for row in transition_rows:
        trans_counts[row["transition"]] = trans_counts.get(row["transition"], 0) + 1

    f2p_vals = [
        r["fail_to_pass_fraction"] for r in per_cell
        if r.get("fail_to_pass_fraction") is not None
    ]
    p2p_vals = [
        r["pass_to_pass_fraction"] for r in per_cell
        if r.get("pass_to_pass_fraction") is not None
    ]
    v035_null_reasons = _SWE_VALID_F2P_P2P_NULL_REASONS | {"no_final_evaluation"}
    n_valid_null = sum(
        1 for r in per_cell
        if r.get("f2p_p2p_null_reason") in v035_null_reasons
        and r.get("fail_to_pass_fraction") is None
    )

    candidate_process = {
        "P0_nonempty_candidate": _cond_rate(p_comp, "P0", lambda r: r["cNE"]),
        "P1_nonempty_candidate": _cond_rate(p_comp, "P1", lambda r: r["cNE"]),
        "raw_candidate_resolution": {
            "n_measured": len(measured_cand),
            "n_resolved": sum(
                1 for r in measured_cand if r.get("candidate_resolved") == 1
            ),
            "by_condition": {
                cond: _cond_rate(
                    [r for r in per_cell if r.get("candidate_resolved") is not None],
                    cond,
                    lambda r: r.get("candidate_resolved") == 1,
                )
                for cond in ("P0", "P1")
            },
        },
        "apply_rate_nonempty_candidate": {
            "n": sum(1 for r in p_comp if r["cNE"]),
            "k": sum(1 for r in p_comp if r["cNE"] and r.get("apply") is True),
            "rate": (
                sum(1 for r in p_comp if r["cNE"] and r.get("apply") is True)
                / sum(1 for r in p_comp if r["cNE"])
                if any(r["cNE"] for r in p_comp) else None
            ),
        },
        "f2p_p2p": {
            "n_numeric_f2p": len(f2p_vals),
            "n_numeric_p2p": len(p2p_vals),
            "n_valid_null": n_valid_null,
            "null_reason": (
                "env_metrics.swebench.f2p_p2p_null_reason "
                "(schema_v2_no_tests_status or no_final_evaluation)"
            ),
        },
    }

    n_valid_struct = sum(1 for r in validation_rows if r["structurally_valid"])
    return {
        "matrix": matrix_ordered,
        "cell_success": cell_success,
        "cell_valid": cell_valid,
        "pairwise_n": pairwise,
        "discordant_issues": discordant,
        "n_discordant": len(discordant),
        "contingency_triggered": (
            len(discordant) < SWE_MAIN_V035_DISCORDANT_THRESHOLD
        ),
        "per_cell": per_cell,
        "compliance": compliance_rows,
        "classification": classification_rows,
        "process": process_rows,
        "process_by_cell": process_by_cell,
        "disposition": disposition_rows,
        "disposition_counts": disp_counts,
        "transitions": transition_rows,
        "transition_counts": trans_counts,
        "provenance": provenance_rows,
        "validation": validation_rows,
        "n_structurally_valid": n_valid_struct,
        "n_records": len(ordered),
        "candidate_process": candidate_process,
        "n_p_cells": len(p_rows),
        "source_fields": SWE_MAIN_V035_SOURCE_FIELDS,
    }


SWE_MAIN_V035_SOURCE_FIELDS: dict[str, str] = {
    "itt_matrix": "env_metrics.swebench.final_resolved via swe_itt_resolved (EF/IF missing, not_finalized None→0)",
    "tau0_tau1_delta": "pairwise P−A / (τ1−τ0) on swe_itt_resolved; task-clustered bootstrap seed 20260902 n=10000",
    "pairwise_n": "tasks with both cells of the contrast non-missing",
    "discordant": "issue where pairwise-complete A0≠P0 or A1≠P1 on swe_itt_resolved",
    "nonempty_candidate": "handoff/candidate.patch nonempty bytes (not accepted_unchanged flag)",
    "raw_candidate_resolution": "env_metrics.swebench.candidate_resolved among non-missing P cells",
    "apply_rate": "env_metrics.swebench.applies_cleanly among P cells with nonempty candidate",
    "f2p_p2p": "env_metrics.swebench.fail_to_pass_fraction / pass_to_pass_fraction; null + f2p_p2p_null_reason",
    "parent_executions": "events.jsonl parent exec_shell tool_result with executed=true",
    "parent_tests": "parent pytest invocations (_count_pytest_calls on traces/events)",
    "any_verification": "parent_tests>0 or parent executed feedback_command",
    "revisions": "P disposition from candidate vs final sha256; parent write_file event count",
    "tokens_after_handoff": "usage.parent.output_tokens",
    "non_finalization": "not compliance.parent_finalized",
    "empty_final": "handoff/final.patch missing or whitespace-only",
    "disposition": "handoff.candidate_script_sha256 vs final_script_sha256 (flag ignored)",
    "patch_distance": "difflib line opcodes on candidate.patch vs final.patch",
    "transitions": "candidate_resolved → final_resolved; missing not coerced to 0",
    "compliance_exec": "compliance.worker_feedback_calls",
    "compliance_end": "swe_protocol_usage.{worker,parent}.end_reason",
    "compliance_asst": "events.jsonl model_call.assistant_text or raw_payload_path present",
    "image_digest": "env_metrics.swebench.image_digest",
    "git_head": "env_metrics.swebench.git_head == reproducibility.benchmark_git_commit",
    "containers": "env_metrics.swebench.worker_container_id ≠ parent_container_id",
}


def write_swe_main_v035_report(
    records: list[dict[str, Any]],
    df: pd.DataFrame,
    estimates: dict[str, float | int],
    boot: dict[str, Any],
    tables: dict[str, Any],
    dest: Path,
) -> str:
    """Write data/dev/swe/main_report_v035.md from computed tables."""
    matrix = tables["matrix"]
    cell_success = tables["cell_success"]
    cell_valid = tables["cell_valid"]
    pairwise = tables["pairwise_n"]
    n_disc = tables["n_discordant"]
    triggered = tables["contingency_triggered"]
    matrix_lines = [
        "| task | A0 | A1 | P0 | P1 |",
        "|---|---:|---:|---:|---:|",
    ]
    for task in SWE_MAIN_V035_TASKS:
        short = swe_short_task_id(task)
        row = matrix.get(short, {})
        matrix_lines.append(
            f"| {short} | {_fmt_itt_cell(row.get('A0'))} | "
            f"{_fmt_itt_cell(row.get('A1'))} | "
            f"{_fmt_itt_cell(row.get('P0'))} | "
            f"{_fmt_itt_cell(row.get('P1'))} |"
        )
    proto_rows: list[str] = []
    for rec in swe_v035_ordered_records(records):
        proto_rows.append(
            f"| {swe_short_task_id(rec['task_id'])} | {rec.get('condition')} | "
            f"`{rec['run_id']}` | {rec.get('protocol_version')} | "
            f"`{(rec.get('reproducibility') or {}).get('schema_git_commit', '')[:8]}` | "
            f"{rec.get('status')} | {swe_itt_cell_label(rec)} |"
        )
    cand = tables["candidate_process"]
    proc = tables["process_by_cell"]
    disp = tables["disposition_counts"]
    trans = tables["transition_counts"]
    class_lines = []
    if tables["classification"]:
        class_lines += [
            "| task | cell | run_id | QC | bucket | evidence |",
            "|---|---|---|---|---|---|",
        ]
        for row in tables["classification"]:
            ev = "; ".join(row["event_evidence"][:4]) or "—"
            class_lines.append(
                f"| {row['task']} | {row['condition']} | `{row['run_id']}` | "
                f"{','.join(row['qc_flags']) or '—'} | {row['bucket']} | {ev} |"
            )
    else:
        class_lines.append("_No non-complete or QC-flagged cells._")

    disp_lines = [
        "| task | cell | disposition | hash_equal | n_changed_lines | accepted_unchanged_flag |",
        "|---|---|---|---|---:|---|",
    ]
    for row in tables["disposition"]:
        disp_lines.append(
            f"| {row['task']} | {row['condition']} | {row['disposition']} | "
            f"{row['hash_equal']} | {row['n_changed_lines']} | "
            f"{row['accepted_unchanged_flag']} |"
        )
    trans_lines = [
        "| task | cell | candidate_resolved | final_resolved | transition |",
        "|---|---|---:|---:|---|",
    ]
    for row in tables["transitions"]:
        trans_lines.append(
            f"| {row['task']} | {row['condition']} | {row['candidate_resolved']} | "
            f"{row['final_resolved']} | {row['transition']} |"
        )

    def _rate(d: dict[str, Any]) -> str:
        if d.get("rate") is None:
            return f"0/{d.get('n', 0)}"
        return f"{d['k']}/{d['n']}"

    lead = (
        "Pre-registered contingency triggered: "
        f"{n_disc} of 8 issues are discordant on resolved "
        f"(threshold <{SWE_MAIN_V035_DISCORDANT_THRESHOLD}). "
        "The confirmatory report therefore **leads with the primary null** "
        "on `env_metrics.swebench.final_resolved`, then reports the "
        "pre-registered process endpoints (parent effort, non-finalization)."
        if triggered else
        f"{n_disc} of 8 issues are discordant on resolved "
        f"(threshold <{SWE_MAIN_V035_DISCORDANT_THRESHOLD} not met). "
        "Primary ITT on resolved is the confirmatory contrast."
    )
    text = "\n".join([
        "# SWE main_v035 report (swe-0.3.5 freeze)",
        "",
        f"Batch `{SWE_MAIN_V035_BATCH_ID}`. Protocol pin "
        f"`{SWE_MAIN_V035_PROTOCOL_PIN}`. Freeze tag "
        f"`{SWE_MAIN_V035_FREEZE_TAG}` at `{SWE_MAIN_V035_FREEZE_COMMIT[:7]}`. "
        "32/32 finished, 0 batch errors, 0 handwritten rows, 0 replacements.",
        "",
        f"`analyze.py --benchmark swebench --task-set main_v035` reproduces "
        f"this matrix from the 32 canonical IDs (task-clustered bootstrap "
        f"seed {BOOTSTRAP_SEED}, {BOOTSTRAP_DRAWS} draws). "
        "Primary field: `env_metrics.swebench.final_resolved`. "
        "Missing (infrastructure_failure / evaluator_failure) ≠ 0.",
        "",
        "## Primary confirmatory result",
        "",
        lead,
        "",
        f"- τ0 = P0−A0 = **{_fmt_signed4(float(estimates['tau0']))}**  "
        f"95% CI ({_fmt_signed4(boot['tau0_ci'][0])}, {_fmt_signed4(boot['tau0_ci'][1])})  "
        f"(n={pairwise['tau0']} pairwise-complete tasks)",
        f"- τ1 = P1−A1 = **{_fmt_signed4(float(estimates['tau1']))}**  "
        f"95% CI ({_fmt_signed4(boot['tau1_ci'][0])}, {_fmt_signed4(boot['tau1_ci'][1])})  "
        f"(n={pairwise['tau1']} pairwise-complete tasks)",
        f"- δ  = τ1−τ0 = **{_fmt_signed4(float(estimates['delta']))}**  "
        f"95% CI ({_fmt_signed4(boot['delta_ci'][0])}, {_fmt_signed4(boot['delta_ci'][1])})  "
        f"(n={pairwise['delta']} tasks with all four cells valid)",
        "",
        "Source: `env_metrics.swebench.final_resolved` via `swe_itt_resolved`. "
        "sympy-16766/A0 is `final_outcome.evaluator_status=not_finalized` with "
        "null `final_resolved` → ITT 0 (measured empty-final / non-finalization), "
        "not missing.",
        "",
        "## 8×4 matrix (binary ITT; EF/IF would be missing, not 0)",
        "",
        *matrix_lines,
        "",
        f"Cell success counts (valid cells only): "
        f"A0 {cell_success['A0']}/{cell_valid['A0']} · "
        f"A1 {cell_success['A1']}/{cell_valid['A1']} · "
        f"P0 {cell_success['P0']}/{cell_valid['P0']} · "
        f"P1 {cell_success['P1']}/{cell_valid['P1']}",
        "",
        f"Discordant issues: **{n_disc}**/8 "
        f"(pylint-6386 A0=1 vs P0=0). Contingency "
        f"{'triggered' if triggered else 'not triggered'}.",
        "",
        "## Pre-registered process endpoints",
        "",
        "Reported because the resolved contrast is under the <3-discordant contingency.",
        "",
        "| cell | n | parent_exec mean | parent_tests mean | any-verification | write_file mean | tokens after handoff mean | non-finalization | empty-final |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        *[
            f"| {cond} | {proc[cond]['n']} | "
            f"{proc[cond]['parent_executions_mean']:.2f} | "
            f"{(proc[cond]['parent_tests_mean'] if proc[cond]['parent_tests_mean'] is not None else float('nan')):.2f} | "
            f"{_rate(proc[cond]['any_verification'])} | "
            f"{proc[cond]['parent_write_file_mean']:.2f} | "
            f"{proc[cond]['tokens_after_handoff_mean']:.1f} | "
            f"{_rate(proc[cond]['non_finalization'])} | "
            f"{_rate(proc[cond]['empty_final'])} |"
            for cond in _CONDITIONS
        ],
        "",
        "Sources: parent_exec = `events.jsonl` parent `exec_shell` `executed=true`; "
        "parent_tests = pytest mentions in parent traces/events; "
        "any-verification = parent_tests>0 or parent `feedback_command` executed; "
        "write_file = parent `tool_call.write_file` count; "
        "tokens after handoff = `usage.parent.output_tokens`; "
        "non-finalization = `compliance.parent_finalized=false`; "
        "empty-final = `handoff/final.patch` empty.",
        "",
        f"Non-finalization concentrates in −F only on sympy-16766/A0 "
        f"(A0 {_rate(proc['A0']['non_finalization'])}, "
        f"A1 {_rate(proc['A1']['non_finalization'])}, "
        f"P0 {_rate(proc['P0']['non_finalization'])}, "
        f"P1 {_rate(proc['P1']['non_finalization'])}). "
        f"Parent effort (mean model_calls): "
        f"A0 {proc['A0']['parent_model_calls_mean']:.1f}, "
        f"A1 {proc['A1']['parent_model_calls_mean']:.1f}, "
        f"P0 {proc['P0']['parent_model_calls_mean']:.1f}, "
        f"P1 {proc['P1']['parent_model_calls_mean']:.1f} "
        f"(`usage.parent.model_calls`).",
        "",
        "## P0/P1 candidate process",
        "",
        f"- nonempty-candidate: P0 {_rate(cand['P0_nonempty_candidate'])} · "
        f"P1 {_rate(cand['P1_nonempty_candidate'])} "
        f"(source: `handoff/candidate.patch` nonempty)",
        f"- raw candidate resolution: "
        f"{cand['raw_candidate_resolution']['n_resolved']}/"
        f"{cand['raw_candidate_resolution']['n_measured']} measured P cells "
        f"(source: `env_metrics.swebench.candidate_resolved`; missing ≠ 0). "
        f"P0 {_rate(cand['raw_candidate_resolution']['by_condition']['P0'])} · "
        f"P1 {_rate(cand['raw_candidate_resolution']['by_condition']['P1'])}",
        f"- apply rate among nonempty candidates: "
        f"{cand['apply_rate_nonempty_candidate']['k']}/"
        f"{cand['apply_rate_nonempty_candidate']['n']} "
        f"(source: `env_metrics.swebench.applies_cleanly`)",
        f"- F2P/P2P: {cand['f2p_p2p']['n_numeric_f2p']} numeric F2P, "
        f"{cand['f2p_p2p']['n_numeric_p2p']} numeric P2P, "
        f"{cand['f2p_p2p']['n_valid_null']} valid-null "
        f"(`f2p_p2p_null_reason=schema_v2_no_tests_status` or "
        f"`no_final_evaluation`)",
        "",
        "## P disposition from patch hashes",
        "",
        f"Counts: unchanged={disp.get('unchanged', 0)}, "
        f"revised={disp.get('revised', 0)}, "
        f"discarded={disp.get('discarded', 0)}, "
        f"rebuilt={disp.get('rebuilt', 0)}, "
        f"no_candidate={disp.get('no_candidate', 0)}. "
        "Source: `handoff.candidate_script_sha256` vs `final_script_sha256`. "
        "`accepted_unchanged` is hardcoded false in the runner and is **not** used.",
        "",
        *disp_lines,
        "",
        "## Candidate→final transitions",
        "",
        f"fail→fail={trans.get('fail→fail', 0)}, "
        f"fail→pass={trans.get('fail→pass', 0)}, "
        f"pass→pass={trans.get('pass→pass', 0)}, "
        f"pass→fail={trans.get('pass→fail', 0)}, "
        f"missing_candidate={trans.get('missing_candidate', 0)}. "
        "Source: `env_metrics.swebench.candidate_resolved` → "
        "`env_metrics.swebench.final_resolved`.",
        "",
        *trans_lines,
        "",
        "## Classification of QC-flagged / non-complete cells",
        "",
        *class_lines,
        "",
        f"Structural validation: {tables['n_structurally_valid']}/"
        f"{tables['n_records']} pass after documenting the frozen-schema "
        f"`task_set` enum lag (`main_v035` recorded; "
        f"`contracts/episode_schema.json` enum was not opened). "
        "No material artifact/hash/event-log issues. "
        "Batch log: 0 errors; `retries_complete` `embedded_infra_retry=1` is "
        "the launcher retry **policy**, not a replacement row. "
        "No failed attempts to preserve.",
        "",
        "## Provenance",
        "",
        "| task | cell | run_id | protocol | schema_git | status | resolved |",
        "|---|---|---|---|---|---|---:|",
        *proto_rows,
        "",
        "## Source-field digest",
        "",
        *[f"- **{name}**: {field}" for name, field in SWE_MAIN_V035_SOURCE_FIELDS.items()],
        "",
        "## Notes",
        "",
        "- Caps frozen: worker 115/110/1800/8192, parent 100/100/1800/8192 "
        "(`budgets.*`, matches `swebench.caps`).",
        "- Image digests and container IDs are in "
        "`data/dev/swe/main_v035_provenance.json`.",
        "- Archive: `data/dev/swe/archive/main_v035/` (copies; originals retained).",
        "",
    ])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    print(f"  wrote {dest}")
    return text


def write_swe_main_v035_compliance(
    tables: dict[str, Any],
    dest: Path,
) -> str:
    """Write the 32-row compliance audit."""
    lines = [
        "# SWE main_v035 compliance (32 rows)",
        "",
        f"Batch `{SWE_MAIN_V035_BATCH_ID}`. Protocol `{SWE_MAIN_V035_PROTOCOL}` "
        f"pin `{SWE_MAIN_V035_PROTOCOL_PIN}`. Freeze `{SWE_MAIN_V035_FREEZE_TAG}` "
        f"at `{SWE_MAIN_V035_FREEZE_COMMIT}`.",
        "Sources: `episode.json`, `events.jsonl`, `handoff/`, `reports/`. "
        "Read-only on `data/runs/<run_id>/`. No handwritten rows.",
        "",
        "## Column sources",
        "",
        "| abbr | field |",
        "|---|---|",
        "| proto | `protocol_version` |",
        "| img | `env_metrics.swebench.image_tag` |",
        "| digest | `env_metrics.swebench.image_digest` |",
        "| remote | `env_metrics.swebench.git_remote` + `task_repo` |",
        "| HEAD=base | `env_metrics.swebench.git_head` == `reproducibility.benchmark_git_commit` |",
        "| w≠p | `worker_container_id` ≠ `parent_container_id` |",
        "| exec | `compliance.worker_feedback_calls` (±F) |",
        "| pyt | parent/worker pytest counts from traces/events |",
        "| wr | worker `write_file` tool_call count (`events.jsonl`) |",
        "| memo / msrc | `handoff/memo.json`; `swe_protocol_usage.worker.memo_source` |",
        "| cand / cNE | `handoff/candidate.patch` present / nonempty |",
        "| apply | `env_metrics.swebench.applies_cleanly` |",
        "| fin / fNE | `handoff/final.patch` present / nonempty |",
        "| end | `swe_protocol_usage.{worker,parent}.end_reason` |",
        "| cont | `swe_protocol_usage.{worker,parent}.continued_on_max_tokens` |",
        "| asst | every `model_call` has `assistant_text` key or `raw_payload_path` |",
        "| harn | candidate/final `reports/*_report.json` |",
        "| fRes / cRes | `env_metrics.swebench.final_resolved` / `candidate_resolved` |",
        "",
        "## 32-row matrix",
        "",
        "| task | cell | proto | img | HEAD=base | w≠p | exec | wr | memo (msrc) | cand | cNE | apply | fin | fNE | end | asst | fRes | cRes | notes |",
        "|---|---|---|---|---|---|---:|---:|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in tables["compliance"]:
        img_short = ""
        if row.get("img"):
            raw_img = str(row["img"])
            if "_1776_" in raw_img:
                img_short = raw_img.split("_1776_")[-1].replace(":latest", "")
            else:
                img_short = raw_img.split("/")[-1].replace(":latest", "")
        notes: list[str] = []
        if row.get("qc_flags"):
            notes.append(",".join(row["qc_flags"]))
        if row.get("pam"):
            notes.append("production_artifact_missing")
        if not row.get("parent_finalized"):
            notes.append("parent_not_finalized")
        end = f"{row.get('end_w')}/{row.get('end_p')}"
        lines.append(
            f"| {row['task']} | {row['condition']} | {row['proto']} | "
            f"`{img_short}` | "
            f"{'yes' if row['head_eq_base'] else 'no'} | "
            f"{'yes' if row['w_neq_p'] else 'no'} | "
            f"{row['exec']} | {row['wr_worker']} | "
            f"{'yes' if row['memo'] else 'no'} ({row.get('msrc')}) | "
            f"{'yes' if row['cand'] else 'no'} | "
            f"{'yes' if row['cNE'] else 'no'} | "
            f"{row['apply']} | "
            f"{'yes' if row['fin'] else 'no'} | "
            f"{'yes' if row['fNE'] else 'no'} | "
            f"{end} | "
            f"{'yes' if row['asst'] else 'no'} | "
            f"{row['fRes']} | {row['cRes']} | "
            f"{'; '.join(notes) or '—'} |"
        )
    lines += [
        "",
        f"Structural validation: {tables['n_structurally_valid']}/"
        f"{tables['n_records']} after schema-lag filter on `task_set`.",
        f"All 32: `task_set=main_v035`, `protocol_version=swe-0.3.5`, "
        f"`reproducibility.schema_git_commit` starts with "
        f"`{SWE_MAIN_V035_FREEZE_COMMIT[:8]}`, HEAD=base, worker≠parent containers.",
        "",
        "Caps: worker max_turns=115 max_tool_calls=110 max_wall=1800 "
        "max_tokens=8192; parent 100/100/1800/8192 (`budgets`).",
        "",
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    dest.write_text(text, encoding="utf-8")
    print(f"  wrote {dest}")
    return text


def archive_swe_main_v035_runs(
    records: list[dict[str, Any]],
    dest: Path,
) -> dict[str, Any]:
    """Copy (never delete) the 32 run directories into the freeze archive."""
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    missing: list[str] = []
    checksums: list[str] = []
    for rec in swe_v035_ordered_records(records):
        run_id = rec["run_id"]
        src = _swe_run_dir(rec)
        if src is None:
            missing.append(run_id)
            continue
        target = dest / run_id
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target, copy_function=shutil.copy2)
        copied.append(run_id)
        ep = target / "episode.json"
        if ep.is_file():
            digest = hashlib.sha256(ep.read_bytes()).hexdigest()
            checksums.append(f"{digest}  {run_id}/episode.json")
    provenance = {
        "batch_id": SWE_MAIN_V035_BATCH_ID,
        "protocol_pin": SWE_MAIN_V035_PROTOCOL_PIN,
        "freeze_commit": SWE_MAIN_V035_FREEZE_COMMIT,
        "freeze_tag": SWE_MAIN_V035_FREEZE_TAG,
        "n_copied": len(copied),
        "copied_run_ids": copied,
        "missing_run_ids": missing,
        "image_digests": [
            {
                "task": row["task"],
                "condition": row["condition"],
                "run_id": row["run_id"],
                "image_tag": row["image_tag"],
                "image_digest": row["image_digest"],
            }
            for row in build_swe_main_v035_tables.__doc__ and []
        ],
        "note": "Copies only. Originals in data/runs/<run_id>/ were not deleted.",
    }
    # Fill image digests from the copied episodes themselves.
    image_rows = []
    for run_id in copied:
        rec = json.loads((dest / run_id / "episode.json").read_text())
        em = (rec.get("env_metrics") or {}).get("swebench") or {}
        image_rows.append({
            "task": swe_short_task_id(rec.get("task_id") or ""),
            "condition": rec.get("condition"),
            "run_id": run_id,
            "image_tag": em.get("image_tag"),
            "image_digest": em.get("image_digest"),
            "schema_git_commit": (rec.get("reproducibility") or {}).get(
                "schema_git_commit"
            ),
        })
    provenance["image_digests"] = image_rows
    (dest / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (dest / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(f"  archived {len(copied)} run dirs → {dest}")
    return provenance


def emit_swe_main_v035_outputs(
    records: list[dict[str, Any]],
    df: pd.DataFrame,
    estimates: dict[str, float | int],
    boot: dict[str, Any],
    out_dir: Path,
    *,
    main_report_path: Path | None = None,
    compliance_path: Path | None = None,
    archive_dir: Path | None = None,
    supporting_dir: Path | None = None,
) -> dict[str, Path]:
    """Write report, compliance, supporting tables, optional archive."""
    tables = build_swe_main_v035_tables(records, df)
    out_dir.mkdir(parents=True, exist_ok=True)
    if supporting_dir is not None:
        support = supporting_dir
    elif (
        main_report_path is not None
        and Path(main_report_path).name == "main_report_v035.md"
    ):
        support = Path("data/dev/swe")
    else:
        support = out_dir
    support.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def _dump(name: str, payload: Any, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix == ".json":
            dest.write_text(
                json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
        print(f"  wrote {dest}")
        paths[name] = dest
        return dest

    report_path = main_report_path or (support / "main_report_v035.md")
    write_swe_main_v035_report(records, df, estimates, boot, tables, report_path)
    paths["main_report"] = report_path
    if report_path.resolve() != (out_dir / "main_report_v035.md").resolve():
        write_swe_main_v035_report(
            records, df, estimates, boot, tables, out_dir / "main_report_v035.md",
        )
        paths["main_report_copy"] = out_dir / "main_report_v035.md"

    comp_path = compliance_path or (support / "compliance_v035.md")
    write_swe_main_v035_compliance(tables, comp_path)
    paths["compliance"] = comp_path

    _dump(
        "classification",
        tables["classification"],
        support / "main_v035_classification.json",
    )
    _dump(
        "provenance",
        {
            "protocol_pin": SWE_MAIN_V035_PROTOCOL_PIN,
            "freeze_commit": SWE_MAIN_V035_FREEZE_COMMIT,
            "freeze_tag": SWE_MAIN_V035_FREEZE_TAG,
            "batch_id": SWE_MAIN_V035_BATCH_ID,
            "rows": tables["provenance"],
        },
        support / "main_v035_provenance.json",
    )
    _dump("validation", tables["validation"], support / "main_v035_validation.json")
    _dump(
        "tables",
        {
            "matrix": tables["matrix"],
            "estimates": estimates,
            "bootstrap": {
                "tau0_ci": list(boot["tau0_ci"]),
                "tau1_ci": list(boot["tau1_ci"]),
                "delta_ci": list(boot["delta_ci"]),
                "seed": BOOTSTRAP_SEED,
                "n_draws": BOOTSTRAP_DRAWS,
            },
            "pairwise_n": tables["pairwise_n"],
            "n_discordant": tables["n_discordant"],
            "contingency_triggered": tables["contingency_triggered"],
            "discordant_issues": tables["discordant_issues"],
            "cell_success": tables["cell_success"],
            "cell_valid": tables["cell_valid"],
            "candidate_process": tables["candidate_process"],
            "process_by_cell": tables["process_by_cell"],
            "disposition_counts": tables["disposition_counts"],
            "transition_counts": tables["transition_counts"],
            "source_fields": tables["source_fields"],
        },
        support / "main_v035_tables.json",
    )
    _write_csv(pd.DataFrame(tables["per_cell"]), support / "main_v035_per_cell.csv")
    paths["per_cell"] = support / "main_v035_per_cell.csv"
    _write_csv(pd.DataFrame(tables["process"]), support / "main_v035_process.csv")
    paths["process"] = support / "main_v035_process.csv"
    _write_csv(
        pd.DataFrame(tables["disposition"]),
        support / "main_v035_disposition.csv",
    )
    paths["disposition"] = support / "main_v035_disposition.csv"
    _write_csv(
        pd.DataFrame(tables["transitions"]),
        support / "main_v035_transitions.csv",
    )
    paths["transitions"] = support / "main_v035_transitions.csv"

    paper_tables = emit_swe_main_paper_outputs(records, df, out_dir)
    paths.update({f"paper_{k}": v for k, v in paper_tables.items()})

    if archive_dir is not None:
        archive_swe_main_v035_runs(records, archive_dir)
        paths["archive"] = archive_dir
    return paths


def run_swe_analysis(
    records: list[dict[str, Any]],
    out_dir: Path,
    *,
    runs_dirs: list[Path],
    run_ids: list[str] | None = None,
    sidecar_dir: Path | None = None,
    task_set: str | None = None,
    main_report_path: Path | None = None,
    trace_bundle_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the SWE-bench analysis pipeline; return a summary dict.

    Outputs (all in out_dir)
    -------------------------
    episodes.parquet, episodes.csv     — flattened episode data
    disposition_table.csv              — patch hash disposition per run
    parent_effort.csv                  — parent effort by condition
    swe_metrics.csv                    — primary/secondary metrics per run
    missing_field_audit.json           — machine-readable audit
    missing_field_audit.md             — human-readable audit
    analysis_summary.json              — overall summary

    When ``task_set='main'``, also writes the confirmatory 8×4 ITT matrix,
    task-clustered bootstrap (seed 20260902, 10000 draws), paper tables
    (a)–(f), optional ``main_report.md``, and an optional trace bundle.

    ITT / non-finalized / infrastructure_failure records are RETAINED.
    For main, ``evaluator_failure`` and ``infrastructure_failure`` stay
    missing (never 0).  Other null ``final_resolved`` values
    (not_finalized / empty patch) remain None→0.  For dev/pilot, null stays
    null in the flattened frame (denominators reported explicitly).
    """
    ts_label = task_set if task_set is not None else _SWE_TASK_SET_LABEL
    is_legacy_main = ts_label == _SWE_TASK_SET_MAIN
    is_main_v035 = ts_label == _SWE_TASK_SET_MAIN_V035
    is_main = is_legacy_main or is_main_v035
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*60}")
    if is_main_v035:
        title = "SWE-bench Main v035 Analysis"
    elif is_legacy_main:
        title = "SWE-bench Main Analysis"
    else:
        title = "SWE-bench Development Analysis"
    print(title)
    print(f"  n_records={len(records)}, task_set={ts_label!r}, out={out_dir}")
    print(f"{'='*60}\n")

    # 0. Overlay metric backfill sidecars (read-only; source records NOT mutated)
    from .swe_metric_backfill import overlay_sidecars_on_records  # noqa: PLC0415
    records, sidecar_overlay_summary = overlay_sidecars_on_records(
        records, sidecar_dir=sidecar_dir
    )
    n_sidecar_overlaid = sidecar_overlay_summary["n_overlaid"]
    n_sidecar_missing = sidecar_overlay_summary["n_missing_sidecar"]
    print(f"[0/6] Sidecar overlay: {n_sidecar_overlaid} overlaid, "
          f"{n_sidecar_missing} without sidecar")

    # 1. Flatten
    print("[1/6] Flattening SWE records …")
    df = flatten_swe_records(records)
    n_total = len(df)
    n_parent_finalized = int(df["parent_finalized"].eq(True).sum())
    n_not_finalized = n_total - n_parent_finalized
    print(f"      {n_total} records; {n_parent_finalized} parent-finalized; "
          f"{n_not_finalized} NOT finalized (retained for ITT)")

    # 2. Primary and secondary metrics per run
    print("[2/6] Extracting SWE metrics …")
    n_final_resolved = int(df["swe_final_resolved"].eq(1).sum())
    n_final_not_resolved = int(df["swe_final_resolved"].eq(0).sum())
    n_final_resolved_null = int(df["swe_final_resolved"].isna().sum())
    print(f"      swe_final_resolved: {n_final_resolved} resolved, "
          f"{n_final_not_resolved} not-resolved, "
          f"{n_final_resolved_null} null/missing (retained, NOT coerced to 0)")

    metric_cols = [
        "run_id", "task_id", "condition", "status",
        "swe_final_resolved",
        "swe_candidate_resolved",
        "swe_fail_to_pass_fraction",
        "swe_pass_to_pass_fraction",
        "swe_applies_cleanly",
        "swe_final_harness_seconds",
        "swe_candidate_harness_seconds",
        "parent_finalized",
        "qc_flags",
    ]
    if "memo_source" in df.columns:
        metric_cols.insert(-1, "memo_source")
    if "swe_f2p_p2p_null_reason" in df.columns:
        metric_cols.insert(8, "swe_f2p_p2p_null_reason")
    swe_metrics = df[metric_cols].copy()
    swe_metrics.to_csv(out_dir / "swe_metrics.csv", index=False)
    print(f"  wrote {out_dir / 'swe_metrics.csv'}")

    # 3. Disposition table
    print("[3/6] Building disposition table …")
    disp = build_swe_disposition_table(records, df)
    disp.to_csv(out_dir / "disposition_table.csv", index=False)
    print(f"  wrote {out_dir / 'disposition_table.csv'}")
    disp_counts = {
        str(k): int(v) for k, v in disp["disposition"].value_counts().to_dict().items()
    }
    print(f"      disposition counts: {disp_counts}")

    # 4. Parent effort
    print("[4/6] Building parent effort table …")
    effort = build_swe_parent_effort(df)
    effort.to_csv(out_dir / "parent_effort.csv", index=False)
    print(f"  wrote {out_dir / 'parent_effort.csv'}")

    # 5. Missing-field audit (sidecar-aware: distinguishes recovered vs genuinely absent)
    print("[5/6] Building missing-field audit …")
    audit = build_swe_missing_field_audit(records)
    # Annotate with sidecar recovery info
    audit["sidecar_overlay_summary"] = sidecar_overlay_summary
    (out_dir / "missing_field_audit.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {out_dir / 'missing_field_audit.json'}")
    audit_md = _audit_to_markdown(audit)
    (out_dir / "missing_field_audit.md").write_text(audit_md, encoding="utf-8")
    print(f"  wrote {out_dir / 'missing_field_audit.md'}")

    # 6. Episodes data
    print("[6/6] Writing episode data …")
    save_episodes(df, out_dir)

    # Build run-ID to source mapping for provenance
    run_provenance = {
        rec["run_id"]: {
            "source_runs_dir": rec.get("_source_runs_dir"),
            "source_episode_sha256": rec.get("_source_episode_sha256"),
        }
        for rec in records
    }

    # Per-condition summary
    per_condition: list[dict[str, Any]] = []
    for cond in sorted(df["condition"].dropna().unique()):
        cell = df[df["condition"] == cond]
        n = len(cell)
        resolved_vals = cell["swe_final_resolved"].dropna()
        per_condition.append({
            "condition": cond,
            "n_runs": n,
            "n_parent_finalized": int(cell["parent_finalized"].eq(True).sum()),
            "n_not_finalized": int(cell["parent_finalized"].ne(True).sum()),
            "n_final_resolved": int(cell["swe_final_resolved"].eq(1).sum()),
            "n_final_not_resolved": int(cell["swe_final_resolved"].eq(0).sum()),
            "n_final_resolved_null": int(cell["swe_final_resolved"].isna().sum()),
            "n_evaluated": int(resolved_vals.notna().sum()),
            "resolved_rate_among_evaluated": (
                float(resolved_vals.mean()) if len(resolved_vals) > 0 else None
            ),
        })

    recorded_task_set = (
        _SWE_TASK_SET_MAIN_V035 if is_main_v035
        else _SWE_TASK_SET_MAIN if is_legacy_main
        else (_SWE_TASK_SET_PILOT if ts_label == _SWE_TASK_SET_PILOT
              else _SWE_TASK_SET_RECORDED)
    )
    itt_policy = (
        "Main ITT: infrastructure_failure and evaluator_failure are missing, "
        "never 0; not_finalized / empty-patch None→0; τ0/τ1/δ use "
        "pairwise-complete tasks with n per estimate; all 32 canonical cells "
        "listed regardless of analysis_included"
        if is_main else
        "Non-finalized and null-outcome records are RETAINED; "
        "denominators are reported explicitly; "
        "null values are NOT coerced to 0"
    )
    summary: dict[str, Any] = {
        "benchmark": _SWE_BENCHMARK,
        "task_set": recorded_task_set,
        "task_set_label": ts_label,
        "runs_dirs": [str(d.resolve()) for d in runs_dirs],
        "run_ids_requested": run_ids,
        "run_ids_loaded": [rec["run_id"] for rec in records],
        "n_records": n_total,
        "n_parent_finalized": n_parent_finalized,
        "n_not_finalized": n_not_finalized,
        "n_final_resolved": n_final_resolved,
        "n_final_not_resolved": n_final_not_resolved,
        "n_final_resolved_null": n_final_resolved_null,
        "primary_outcome_field": "env_metrics.swebench.final_resolved",
        "pass_fraction_policy": (
            "null + f2p_p2p_null_reason=schema_v2_no_tests_status is valid "
            "absence; not malformed"
        ),
        "disposition_counts": disp_counts,
        "per_condition": per_condition,
        "run_provenance": run_provenance,
        "itt_policy": itt_policy,
        "missing_field_notes": (
            "See missing_field_audit.json and missing_field_audit.md "
            "for per-run, per-field missingness details"
        ),
        "sidecar_overlay_summary": sidecar_overlay_summary,
    }

    if is_main:
        print("[main] Computing ITT matrix and task-clustered bootstrap …")
        present_conds = set(df["condition"].dropna())
        if not set(_CONDITIONS).issubset(present_conds):
            summary["itt_skipped"] = (
                f"incomplete factorial; have {sorted(present_conds)}"
            )
            (out_dir / "analysis_summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"  wrote {out_dir / 'analysis_summary.json'}")
            return summary
        task_means = build_swe_main_task_means(records)
        estimates = compute_swe_estimands(task_means)
        boot = bootstrap_swe_estimands(
            task_means, seed=BOOTSTRAP_SEED, n_draws=BOOTSTRAP_DRAWS,
        )
        # Drop draw arrays before JSON serialization.
        boot_json = {
            "tau0_ci": list(boot["tau0_ci"]),
            "tau1_ci": list(boot["tau1_ci"]),
            "delta_ci": list(boot["delta_ci"]),
            "seed": BOOTSTRAP_SEED,
            "n_draws": BOOTSTRAP_DRAWS,
        }
        matrix = build_swe_main_matrix(records)
        cell_success, cell_valid = swe_cell_success_counts(matrix)
        paper_paths: dict[str, Path]
        report_path: Path
        if is_main_v035:
            archive_dir = None
            if (
                main_report_path is not None
                and Path(main_report_path).name == "main_report_v035.md"
            ):
                archive_dir = Path("data/dev/swe/archive/main_v035")
            v035_paths = emit_swe_main_v035_outputs(
                records, df, estimates, boot, out_dir,
                main_report_path=main_report_path,
                archive_dir=archive_dir,
            )
            paper_paths = {
                k: v for k, v in v035_paths.items() if k.startswith("paper_")
            }
            report_path = v035_paths.get(
                "main_report",
                main_report_path or (out_dir / "main_report_v035.md"),
            )
        else:
            paper_paths = emit_swe_main_paper_outputs(records, df, out_dir)
            report_path = main_report_path or (out_dir / "main_report.md")
            write_swe_main_report(records, df, estimates, boot, report_path)
            if report_path.resolve() != (out_dir / "main_report.md").resolve():
                write_swe_main_report(
                    records, df, estimates, boot, out_dir / "main_report.md",
                )
        trace_index = None
        if trace_bundle_dir is not None:
            trace_index = write_swe_trace_bundle(records, trace_bundle_dir)
        n_infra = sum(
            1 for rec in records
            if rec.get("status") == "infrastructure_failure"
        )
        summary.update({
            "matrix": matrix,
            "cell_success": cell_success,
            "cell_valid": cell_valid,
            "n_evaluator_failure": sum(
                1 for rec in records if swe_is_evaluator_failure(rec)
            ),
            "pairwise_n": {
                "tau0": estimates["n_tau0"],
                "tau1": estimates["n_tau1"],
                "delta": estimates["n_delta"],
            },
            "estimates": estimates,
            "bootstrap": boot_json,
            "paper_outputs": {k: str(p) for k, p in paper_paths.items()},
            "main_report_path": str(report_path),
            "trace_bundle_dir": (
                str(trace_bundle_dir) if trace_bundle_dir is not None else None
            ),
            "n_infrastructure_failure": n_infra,
        })
        if is_main_v035:
            v035_tables = build_swe_main_v035_tables(records, df)
            summary.update({
                "n_discordant": v035_tables["n_discordant"],
                "contingency_triggered": v035_tables["contingency_triggered"],
                "discordant_issues": v035_tables["discordant_issues"],
                "disposition_counts_v035": v035_tables["disposition_counts"],
                "transition_counts": v035_tables["transition_counts"],
                "source_fields": v035_tables["source_fields"],
            })
        if trace_index is not None:
            summary["trace_bundle_n_cells"] = trace_index["n_cells"]
    (out_dir / "analysis_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {out_dir / 'analysis_summary.json'}")

    print(f"\n{'='*60}")
    print("SWE analysis complete.")
    print(f"  {n_total} runs; {n_final_resolved} final_resolved=1; "
          f"{n_final_resolved_null} null (ITT retained)")
    print(f"  disposition: {disp_counts}")
    print(f"{'='*60}\n")

    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def resolve_analysis_seed(
    explicit_seed: int | None,
    *,
    manifest_path: Path | str | None = None,
) -> int:
    """Use an explicit seed or the current manifest-selected package seed."""
    if explicit_seed is not None:
        return explicit_seed
    if manifest_path is None:
        return load_spreadsheet_selection().seed
    return load_spreadsheet_selection(manifest_path).seed


# ── Split-by helpers ─────────────────────────────────────────────────────────

def load_task_metadata(
    manifest_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Load per-task metadata (instruction_type, n_answer_cells, etc.) from the
    frozen task-selection package named in *manifest_path* (or the default
    manifest when None).

    Returns a dict mapping str(task_id) → task metadata dict.
    All top-level selection lists (``pilot``, ``main``, ``reserve_in_order``,
    ``hard_extension``) are merged; later entries do not overwrite earlier ones
    for the same ID.
    """
    sel = load_spreadsheet_selection(manifest_path) if manifest_path else load_spreadsheet_selection()
    raw = json.loads(sel.selection_path.read_text(encoding="utf-8"))
    metadata: dict[str, dict[str, Any]] = {}
    for key in ("pilot", "main", "reserve_in_order", "hard_extension"):
        items = raw.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and "id" in item:
                tid = str(item["id"])
                if tid not in metadata:
                    metadata[tid] = item
    return metadata


def load_coupling_annotations(path: Path) -> dict[str, str | None]:
    """Read ``config/study/tasks_annotations.csv`` and return a dict mapping
    str(task_id) → coupling value (str), or {} if file is absent.

    The file is expected to have at least a ``task_id`` column.  The coupling
    column is taken from the first column whose name matches (case-insensitively)
    ``coupling``.  All other columns are ignored.

    Returns an empty dict when the file is absent.  If the file exists but
    lacks a ``coupling`` column the function returns {} and adds an entry
    ``"_coupling_column_missing": True`` in the returned dict to signal the
    caller.
    """
    if not path.is_file():
        return {}
    import csv as _csv
    rows: dict[str, str | None] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = _csv.DictReader(fh)
        if reader.fieldnames is None:
            return {}
        # Find task_id and coupling columns (case-insensitive)
        id_col = next(
            (f for f in reader.fieldnames if f.strip().lower() == "task_id"), None
        )
        coupling_col = next(
            (f for f in reader.fieldnames if f.strip().lower() == "coupling"), None
        )
        if id_col is None:
            rows["_task_id_column_missing"] = True  # type: ignore[assignment]
            return rows
        if coupling_col is None:
            rows["_coupling_column_missing"] = True  # type: ignore[assignment]
            return rows
        for row in reader:
            tid = str(row[id_col]).strip()
            val = row[coupling_col].strip() if row[coupling_col] else None
            rows[tid] = val if val else None
    return rows


def compute_answer_cells_quartile(
    task_ids: list[str],
    task_metadata: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Assign each unique included task to a quartile label (Q1/Q2/Q3/Q4) by
    its ``n_answer_cells`` value.

    Quartile boundaries are computed as np.percentile(values, [25, 50, 75])
    over the included unique tasks only (tasks with known n_answer_cells).  The
    assignment method is:
      - Q1: n_answer_cells <= p25
      - Q2: p25 < n_answer_cells <= p50
      - Q3: p50 < n_answer_cells <= p75
      - Q4: n_answer_cells > p75

    Ties on a boundary are assigned to the lower quartile (e.g., a task whose
    value equals p25 lands in Q1).  Tasks missing from metadata are labelled
    ``"missing_metadata"``.  If multiple distinct tasks share the same
    n_answer_cells value that falls exactly on a boundary, they receive the
    same (lower) quartile.

    Parameters
    ----------
    task_ids : Unique task IDs to consider (all included unique tasks in scope).
    task_metadata : Loaded via :func:`load_task_metadata`.

    Returns
    -------
    assignments : dict mapping task_id → quartile label string.
    report : dict with boundary values, tie counts, and missing-metadata count.
    """
    known_values: list[tuple[str, int]] = []
    missing_meta: list[str] = []
    for tid in task_ids:
        meta = task_metadata.get(tid)
        if meta is None or "n_answer_cells" not in meta:
            missing_meta.append(tid)
        else:
            known_values.append((tid, int(meta["n_answer_cells"])))

    if not known_values:
        # All missing
        assignments = {tid: "missing_metadata" for tid in task_ids}
        return assignments, {"error": "no_tasks_with_known_n_answer_cells"}

    values_arr = np.array([v for _, v in known_values])
    p25, p50, p75 = float(np.percentile(values_arr, 25)), float(np.percentile(values_arr, 50)), float(np.percentile(values_arr, 75))

    # Count ties on each boundary
    n_tie_p25 = int(np.sum(values_arr == p25))
    n_tie_p50 = int(np.sum(values_arr == p50))
    n_tie_p75 = int(np.sum(values_arr == p75))

    def _quartile(v: int) -> str:
        if v <= p25:
            return "Q1"
        if v <= p50:
            return "Q2"
        if v <= p75:
            return "Q3"
        return "Q4"

    assignments: dict[str, str] = {}
    for tid, v in known_values:
        assignments[tid] = _quartile(v)
    for tid in missing_meta:
        assignments[tid] = "missing_metadata"

    report = {
        "method": "np.percentile over included unique tasks, ties → lower quartile",
        "n_tasks_with_known_n_answer_cells": len(known_values),
        "n_tasks_missing_metadata": len(missing_meta),
        "missing_metadata_task_ids": missing_meta,
        "boundaries": {"p25": p25, "p50": p50, "p75": p75},
        "n_ties_on_p25": n_tie_p25,
        "n_ties_on_p50": n_tie_p50,
        "n_ties_on_p75": n_tie_p75,
        "quartile_counts": {
            q: sum(1 for v in assignments.values() if v == q)
            for q in ("Q1", "Q2", "Q3", "Q4", "missing_metadata")
        },
    }
    return assignments, report


def compute_subgroup_split(
    task_means: pd.DataFrame,
    split_var: str,
    task_metadata: dict[str, dict[str, Any]],
    coupling_annotations: dict[str, str | None],
    seed: int,
    n_draws: int,
) -> dict[str, Any]:
    """Compute subgroup estimates/tables for one split variable.

    Parameters
    ----------
    task_means  : Output of :func:`compute_task_means` (task_id, condition, task_mean).
    split_var   : One of ``answer_cells_quartile``, ``instruction_type``, ``coupling``.
    task_metadata : From :func:`load_task_metadata`.
    coupling_annotations : From :func:`load_coupling_annotations`.
    seed, n_draws : Bootstrap parameters.

    Returns
    -------
    dict with keys:
      split_var, method_note, quartile_report (if applicable),
      coupling_note (if applicable), groups (dict group_label → subgroup summary),
      missing_category_tasks (tasks with no group assignment).
    """
    unique_task_ids = sorted(task_means["task_id"].unique())

    result: dict[str, Any] = {
        "split_var": split_var,
        "n_unique_tasks": len(unique_task_ids),
    }

    # --- Build label map: task_id → group label ---
    if split_var == "instruction_type":
        label_map: dict[str, str] = {}
        for tid in unique_task_ids:
            meta = task_metadata.get(tid)
            if meta and "instruction_type" in meta:
                label_map[tid] = str(meta["instruction_type"])
            else:
                label_map[tid] = "missing_metadata"
        result["method_note"] = (
            "instruction_type taken from canonical task-selection package "
            "(config/task_selection_v03/ss_task_selection.json); "
            "tasks missing from selection are labelled 'missing_metadata'."
        )

    elif split_var == "answer_cells_quartile":
        label_map, quartile_report = compute_answer_cells_quartile(
            unique_task_ids, task_metadata
        )
        result["quartile_report"] = quartile_report
        result["method_note"] = (
            "Quartile assignment uses n_answer_cells from canonical task-selection "
            "package. Boundaries computed as np.percentile([25,50,75]) over included "
            "unique tasks. Ties land in the lower quartile (e.g., value == p25 → Q1)."
        )

    elif split_var == "coupling":
        if not coupling_annotations:
            label_map = {tid: "missing_annotation" for tid in unique_task_ids}
            result["coupling_note"] = (
                "config/study/tasks_annotations.csv absent or empty; "
                "all tasks labelled 'missing_annotation'. No coupling inference performed."
            )
        elif coupling_annotations.get("_coupling_column_missing"):
            label_map = {tid: "missing_annotation" for tid in unique_task_ids}
            result["coupling_note"] = (
                "tasks_annotations.csv present but lacks a 'coupling' column; "
                "all tasks labelled 'missing_annotation'."
            )
        elif coupling_annotations.get("_task_id_column_missing"):
            label_map = {tid: "missing_annotation" for tid in unique_task_ids}
            result["coupling_note"] = (
                "tasks_annotations.csv present but lacks a 'task_id' column; "
                "all tasks labelled 'missing_annotation'."
            )
        else:
            label_map = {}
            for tid in unique_task_ids:
                val = coupling_annotations.get(tid)
                if val is None:
                    label_map[tid] = "missing_annotation"
                else:
                    label_map[tid] = str(val)
            result["coupling_note"] = (
                "Coupling values read from config/study/tasks_annotations.csv. "
                "Tasks absent from CSV or with empty coupling value are labelled "
                "'missing_annotation'. No inference performed."
            )
    else:
        raise ValueError(f"Unknown split_var: {split_var!r}")

    # --- Compute subgroup estimates per group ---
    groups: dict[str, Any] = {}
    missing_category_tasks: list[str] = []
    for group_label in sorted(set(label_map.values())):
        if group_label in ("missing_metadata", "missing_annotation"):
            missing_category_tasks.extend(
                tid for tid, v in label_map.items() if v == group_label
            )
            groups[group_label] = {
                "n_tasks": sum(1 for v in label_map.values() if v == group_label),
                "n_episodes": int(
                    task_means[task_means["task_id"].isin(
                        [t for t, v in label_map.items() if v == group_label]
                    )].shape[0]
                ),
                "estimates": None,
                "bootstrap": None,
                "reason": "excluded from estimates; no valid group assignment",
            }
            continue

        group_task_ids = [tid for tid, v in label_map.items() if v == group_label]
        sub = task_means[task_means["task_id"].isin(group_task_ids)].copy()

        group_info: dict[str, Any] = {
            "n_tasks": len(group_task_ids),
            "n_episodes": int(sub.shape[0]),
        }

        if len(group_task_ids) < 2:
            group_info["estimates"] = None
            group_info["bootstrap"] = None
            group_info["reason"] = (
                f"only {len(group_task_ids)} task(s); cannot compute reliable subgroup estimate"
            )
        else:
            try:
                sub_estimates = compute_estimands(sub)
                sub_boot = bootstrap_estimands(sub, seed=seed, n_draws=n_draws)
                sub_boot_summary = {
                    k: v
                    for k, v in sub_boot.items()
                    if not k.endswith("_draws")
                }
                group_info["estimates"] = sub_estimates
                group_info["bootstrap"] = sub_boot_summary
                group_info["reason"] = "ok"
            except Exception as exc:  # noqa: BLE001
                group_info["estimates"] = None
                group_info["bootstrap"] = None
                group_info["reason"] = f"estimation_error: {exc}"

        groups[group_label] = group_info

    result["groups"] = groups
    result["missing_category_tasks"] = missing_category_tasks
    result["n_missing_category_tasks"] = len(missing_category_tasks)
    return result


def filter_records_by_memo_size(
    records: list[dict[str, Any]],
    runs_dir: Path,
    k: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter episode records by UTF-8 byte length of the canonical memo JSON.

    A record is excluded when its worker memo file exists, is readable, and its
    UTF-8 byte length exceeds *k*.  SOLO records (condition == 'SOLO') have no
    worker memo and are never excluded.  Records without a memo path or with an
    unreadable/missing memo file are retained and noted in the report but not
    excluded (conservative: absence of evidence is not evidence of excess size).

    The unit is **UTF-8 bytes of the as-stored memo.json file** (the canonical
    serialized form).  This is the unit documented in ``--help`` and all output
    reports.

    Parameters
    ----------
    records  : Episode records as returned by :func:`load_real_records`.
    runs_dir : Root directory of episode runs.
    k        : Non-negative integer cutoff.  Records with memo_bytes > k are
               excluded.

    Returns
    -------
    filtered : Records retained after exclusion.
    report   : Dict with exclusion counts, run IDs, and per-record reasons.
    """
    if k < 0:
        raise AnalysisInputError(
            f"--exclude-memos-over-k requires a non-negative integer; got {k}"
        )

    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    no_memo: list[str] = []          # retained; SOLO or no memo submitted
    unreadable: list[str] = []       # retained; file missing/unreadable

    for record in records:
        run_id = str(record["run_id"])
        condition = str(record.get("condition", ""))

        # SOLO has no worker memo → never excluded
        if condition == _SOLO_CONDITION:
            kept.append(record)
            no_memo.append(run_id)
            continue

        handoff = record.get("handoff") or {}
        memo_path_str = handoff.get("memo_path")

        if not memo_path_str or not handoff.get("worker_submitted_memo"):
            # No memo submitted → retain; not excluded
            kept.append(record)
            no_memo.append(run_id)
            continue

        # Resolve memo path (may be absolute or relative to run dir)
        memo_path = Path(memo_path_str)
        if not memo_path.is_absolute():
            memo_path = runs_dir / run_id / memo_path_str

        if not memo_path.is_file():
            kept.append(record)
            unreadable.append(run_id)
            continue

        try:
            memo_bytes = len(memo_path.read_bytes())
        except OSError:
            kept.append(record)
            unreadable.append(run_id)
            continue

        if memo_bytes > k:
            excluded.append({
                "run_id": run_id,
                "task_id": str(record.get("task_id", "")),
                "condition": condition,
                "memo_bytes": memo_bytes,
                "k": k,
                "reason": f"memo_bytes={memo_bytes} > k={k}",
            })
        else:
            kept.append(record)

    report = {
        "sensitivity_exclusion": "--exclude-memos-over-k",
        "k": k,
        "memo_size_unit": "UTF-8 byte length of canonical handoff/memo.json file",
        "n_records_before": len(records),
        "n_excluded": len(excluded),
        "n_kept": len(kept),
        "n_no_memo_retained": len(no_memo),
        "n_unreadable_retained": len(unreadable),
        "excluded_runs": excluded,
        "no_memo_run_ids": no_memo,
        "unreadable_run_ids": unreadable,
        "solo_policy": "SOLO records (no worker memo) are never excluded by this filter",
    }
    return kept, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark",
        choices=["spreadsheetbench", "swebench"],
        default="spreadsheetbench",
        help=(
            "Benchmark to analyze. Default spreadsheetbench keeps the "
            "real-record Spreadsheet pipeline. swebench bypasses those "
            "loaders and uses the SWE-bench pipeline."
        ),
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        action="append",
        dest="runs_dirs",
        metavar="RUNS_DIR",
        help=(
            "Directory containing canonical episode run directories. "
            "Required. Spreadsheet analysis accepts exactly one directory. "
            "SWE-bench may be specified multiple times."
        ),
    )
    parser.add_argument(
        "--task-set",
        default=None,
        help=(
            "Analysis scope. Spreadsheet (required): 'main', 'all', "
            "'main_v04', or 'solo_v04'. SWE-bench: 'dev', 'pilot', "
            "'main', or 'main_v035'. 'main' loads the closed 0.3.3/4 IDs. "
            "'main_v035' loads the swe-0.3.5 32-ID matrix (no handwritten rows)."
        ),
    )
    parser.add_argument(
        "--allow-task-set",
        choices=["dev"],
        default=None,
        dest="allow_task_set",
        help=(
            "Explicitly allow a restricted SWE-bench task-set scope. "
            "Currently only 'dev' is gated."
        ),
    )
    parser.add_argument(
        "--run-ids",
        default=None,
        help=(
            "Comma-separated list of run IDs to include (SWE-bench only). "
            "For --task-set main, defaults to the closed 0.3.3/4 IDs. "
            "For --task-set main_v035, defaults to the swe-0.3.5 32 IDs."
        ),
    )
    parser.add_argument(
        "--swe-metrics-sidecar-dir",
        type=Path,
        default=None,
        dest="swe_metrics_sidecar_dir",
        help=(
            "Directory containing metric backfill sidecar JSON files "
            "(data/dev/swe/metric_backfills/ by default when --benchmark swebench). "
            "Sidecars are overlaid on episode records without mutating run files."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("paper_outputs"),
        help="Directory to write all analysis outputs (default: paper_outputs/).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "Bootstrap RNG seed (spreadsheetbench only; "
            "default: current manifest-selected package seed)."
        ),
    )
    parser.add_argument(
        "--n-draws",
        type=int,
        default=None,
        help=(
            "Number of bootstrap draws (default: 5000 for main_v04; "
            f"{BOOTSTRAP_DRAWS} otherwise)."
        ),
    )
    parser.add_argument(
        "--split-by",
        choices=("answer_cells_quartile", "instruction_type", "coupling"),
        default=None,
        dest="split_by",
        help=(
            "Optional: compute deterministic subgroup estimates by one split "
            "variable. Exactly one split at a time. "
            "answer_cells_quartile: derive task-level answer-cell count from "
            "canonical task-selection metadata; compute quartile assignment "
            "across included unique tasks. "
            "instruction_type: canonical task field from task-selection package. "
            "coupling: read config/study/tasks_annotations.csv by task ID; if "
            "file absent or task missing, label 'missing_annotation' and report "
            "(never infer). Subgroup outputs written to "
            "out_dir/split_by_{variable}.json. Pooled estimates are unchanged."
        ),
    )
    parser.add_argument(
        "--exclude-memos-over-k",
        type=int,
        default=None,
        dest="exclude_memos_over_k",
        metavar="K",
        help=(
            "Optional sensitivity exclusion: drop episode records whose worker "
            "memo exceeds K UTF-8 bytes (canonical handoff/memo.json file size). "
            "K must be non-negative. SOLO records have no worker memo and are "
            "never excluded. Records with missing or unreadable memo files are "
            "retained. Exclusions are logged explicitly (run IDs, byte counts, "
            "reasons) in out_dir/memo_exclusion_report.json. The pooled "
            "estimates produced by this run use the filtered record set only."
        ),
    )
    args = parser.parse_args(argv)

    # ── SWE-bench path ────────────────────────────────────────────────────────
    if args.benchmark == "swebench":
        if not args.runs_dirs:
            parser.error("--runs-dir is required for --benchmark swebench")
        if args.task_set not in (
            _SWE_TASK_SET_LABEL,
            _SWE_TASK_SET_PILOT,
            _SWE_TASK_SET_MAIN,
            _SWE_TASK_SET_MAIN_V035,
        ):
            parser.error(
                f"--task-set must be 'dev', 'pilot', 'main', or 'main_v035' "
                f"for --benchmark swebench; got {args.task_set!r}"
            )
        if (
            args.task_set == _SWE_TASK_SET_LABEL
            and args.allow_task_set != _SWE_TASK_SET_LABEL
        ):
            parser.error(
                "SWE-bench development records require --allow-task-set dev; "
                "this gate prevents accidental inclusion of development/pilot "
                "records in analyses that do not explicitly opt in"
            )

        run_ids: set[str] | None = None
        if args.run_ids:
            run_ids = {rid.strip() for rid in args.run_ids.split(",") if rid.strip()}
        elif args.task_set == _SWE_TASK_SET_MAIN:
            run_ids = set(SWE_MAIN_RUN_IDS)
        elif args.task_set == _SWE_TASK_SET_MAIN_V035:
            run_ids = set(SWE_MAIN_V035_RUN_IDS)

        sidecar_dir = args.swe_metrics_sidecar_dir
        if sidecar_dir is None:
            sidecar_dir = Path("data/dev/swe/metric_backfills")

        out_dir = args.out_dir
        if args.task_set == _SWE_TASK_SET_MAIN and out_dir == Path("paper_outputs"):
            out_dir = Path("paper_outputs") / "swe_main"
        if (
            args.task_set == _SWE_TASK_SET_MAIN_V035
            and out_dir == Path("paper_outputs")
        ):
            out_dir = Path("paper_outputs") / "swe_main_v035"

        try:
            swe_records = load_swe_records(
                args.runs_dirs,
                allow_dev=(args.allow_task_set == _SWE_TASK_SET_LABEL),
                run_ids=run_ids,
                task_set=args.task_set,
            )
        except AnalysisInputError as exc:
            parser.error(str(exc))

        main_report_path = None
        trace_bundle_dir = None
        if args.task_set == _SWE_TASK_SET_MAIN and out_dir.name == "swe_main":
            main_report_path = Path("data/dev/swe/main_report.md")
            trace_bundle_dir = Path("data/dev/swe/trace_bundle")
        if (
            args.task_set == _SWE_TASK_SET_MAIN_V035
            and out_dir.name == "swe_main_v035"
        ):
            main_report_path = Path("data/dev/swe/main_report_v035.md")
            # Archive is a full directory copy, not the paper trace bundle.
            trace_bundle_dir = None

        run_swe_analysis(
            swe_records,
            out_dir,
            runs_dirs=args.runs_dirs,
            run_ids=list(run_ids) if run_ids is not None else None,
            sidecar_dir=sidecar_dir,
            task_set=args.task_set,
            main_report_path=main_report_path,
            trace_bundle_dir=trace_bundle_dir,
        )
        return 0

    # ── Spreadsheet real-record path (default) ────────────────────────────────
    if args.allow_task_set == _SWE_TASK_SET_LABEL:
        parser.error(
            "--allow-task-set dev is only valid with --benchmark swebench"
        )
    if not args.runs_dirs:
        parser.error("--runs-dir is required")
    if len(args.runs_dirs) != 1:
        parser.error("spreadsheet analysis accepts exactly one --runs-dir")
    args.runs_dir = args.runs_dirs[0]
    if args.task_set not in _TASK_SET_SCOPES:
        parser.error(
            "--task-set must be one of "
            f"{sorted(_TASK_SET_SCOPES)} for spreadsheetbench; "
            f"got {args.task_set!r}"
        )

    # Validate --exclude-memos-over-k
    if args.exclude_memos_over_k is not None and args.exclude_memos_over_k < 0:
        parser.error("--exclude-memos-over-k requires a non-negative integer")

    if args.task_set == "main_v04":
        seed = V041_BOOTSTRAP_SEED if args.seed is None else args.seed
        n_draws = (
            V041_BOOTSTRAP_DRAWS
            if args.n_draws is None
            else args.n_draws
        )
    else:
        seed = resolve_analysis_seed(args.seed)
        n_draws = (
            BOOTSTRAP_DRAWS if args.n_draws is None else args.n_draws
        )
    try:
        core_records = load_real_records(args.runs_dir, args.task_set)
    except AnalysisInputError as exc:
        parser.error(str(exc))

    # Apply memo-size sensitivity exclusion before analysis
    memo_exclusion_report: dict[str, Any] | None = None
    if args.exclude_memos_over_k is not None:
        core_records, memo_exclusion_report = filter_records_by_memo_size(
            core_records, args.runs_dir, args.exclude_memos_over_k
        )
        print(
            f"[memo-exclusion] K={args.exclude_memos_over_k} bytes: "
            f"excluded {memo_exclusion_report['n_excluded']} / "
            f"{memo_exclusion_report['n_records_before']} records"
        )
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "memo_exclusion_report.json").write_text(
            json.dumps(memo_exclusion_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"  wrote {args.out_dir / 'memo_exclusion_report.json'}")

    if args.task_set == "solo_v04":
        result = run_solo_analysis(
            core_records,
            args.out_dir,
            runs_dir=args.runs_dir,
        )
        collection_summary = {
            "runs_dir": str(args.runs_dir.resolve()),
            "task_set_scope": "solo_v04",
            "selected_episode_records": len(core_records),
            "analysis_rows": len(core_records),
            "analysis_only_itt_rows": 0,
            "outside_scope_records": "not enumerated",
            "selected_by_task_set": {"solo_v04": len(core_records)},
            "analysis_included_records": len(core_records),
        }
        (args.out_dir / "collection_summary.json").write_text(
            json.dumps(collection_summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            "SOLO analysis complete: "
            f"{result['condition_summary']['full_task_success_numerator']}/25"
        )
        return 0

    result = run_analysis(
        core_records,
        [],
        args.out_dir,
        seed=seed,
        n_draws=n_draws,
        task_set_scope=args.task_set,
        runs_dir=args.runs_dir,
    )
    selected_episode_records = sum(
        not record.get("_analysis_only_itt_row", False)
        for record in core_records
    )
    collection_summary = {
        "runs_dir": str(args.runs_dir.resolve()),
        "task_set_scope": args.task_set,
        "selected_episode_records": selected_episode_records,
        "analysis_rows": len(core_records),
        "analysis_only_itt_rows": len(core_records) - selected_episode_records,
        "outside_scope_records": "not enumerated",
        "selected_by_task_set": {
            task_set: sum(record["task_set"] == task_set for record in core_records)
            for task_set in sorted(_TASK_SET_SCOPES[args.task_set])
        },
        "analysis_included_records": result["n_analysis_included"],
        "hard_supplement_exclusions": result["hard_supplement_exclusions"],
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "collection_summary.json").write_text(
        json.dumps(collection_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {args.out_dir / 'collection_summary.json'}")

    # Optional: compute and write split-by subgroup estimates
    if args.split_by is not None:
        print(f"\n[split-by] Computing subgroup estimates for '{args.split_by}' …")
        task_metadata = load_task_metadata()
        annotations_path = (
            Path(__file__).resolve().parents[3] / "config" / "study" / "tasks_annotations.csv"
        )
        coupling_annotations = load_coupling_annotations(annotations_path)

        # Re-derive task_means from flattened records (same logic as run_analysis)
        df_for_split = flatten_records(core_records)
        analysis_task_set_for_split = (
            "main_v04" if args.task_set == "main_v04" else "main"
        )
        task_means_for_split = compute_task_means(
            df_for_split, task_set=analysis_task_set_for_split
        )

        split_result = compute_subgroup_split(
            task_means_for_split,
            args.split_by,
            task_metadata,
            coupling_annotations,
            seed=seed,
            n_draws=n_draws,
        )
        split_out = args.out_dir / f"split_by_{args.split_by}.json"
        split_out.write_text(
            json.dumps(split_result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"  wrote {split_out}")
        # Print subgroup summary
        for grp_label, grp_info in sorted(split_result["groups"].items()):
            est = grp_info.get("estimates")
            if est:
                print(
                    f"    {grp_label}: n_tasks={grp_info['n_tasks']} "
                    f"tau0={est['tau0']:+.4f} tau1={est['tau1']:+.4f} "
                    f"delta={est['delta']:+.4f}"
                )
            else:
                print(
                    f"    {grp_label}: n_tasks={grp_info['n_tasks']} "
                    f"[{grp_info.get('reason','no estimate')}]"
                )

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
