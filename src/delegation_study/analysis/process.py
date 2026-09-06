"""S1 process analyses for Spreadsheet main_v04 and SWE main_v035.

Identical functions both environments. Environment adapters exist only at
record load / field extraction. This module does not import or call
``analyze.py`` pipelines and does not write under ``data/runs/``.

Analyses
--------
A1  Repair/damage on P cells: q, r, d, counts; reconstruction identity.
A2  Calibration 2×2 from **patch hashes**, never ``accepted_unchanged``.
A3  Parent verification V (executions, inspections, revisions, tokens)
    on all four cells; θ_V with task-clustered bootstrap.
A4  Attenuation gap Δ_W − Δ_F; descriptive, not mediation.
A5  Non-finalization, turns-to-finalize, context-overflow, empty-final.

Spreadsheet primary keeps pre-registered ITT 0 (including 57232 A0
context overflow). Every spreadsheet table also reports the harmonized
sensitivity that codes that cell missing. SWE leaves null
``env_metrics.swebench.*_resolved`` missing (never 0).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .terminal_episode import (
    CONTEXT_WINDOW_REASON,
    is_terminal_agent_failure,
)

# ── Constants ────────────────────────────────────────────────────────────────

BOOTSTRAP_SEED = 20260902
BOOTSTRAP_DRAWS = 10_000
ALPHA = 0.05
CONDITIONS = ("A0", "A1", "P0", "P1")
P_CONDITIONS = ("P0", "P1")

ENV_SPREADSHEET = "spreadsheet"
ENV_SWE = "swe"

SS_TASK_SET = "main_v04"
SS_PROTOCOL = "0.4.1"
SS_OVERFLOW_TASK = "57232"
SS_OVERFLOW_CONDITION = "A0"

SWE_TASK_SET = "main_v035"
SWE_BENCHMARK = "swebench"

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

SS_EXEC_TOOLS = frozenset({"run_solution", "run_ephemeral_python"})
SS_INSPECT_TOOLS = frozenset({"read_output_range"})
SS_REVISE_TOOLS = frozenset({"write_solution"})
SWE_FILE_VIEW_TOOLS = frozenset({"read_file"})
SWE_REVISE_TOOLS = frozenset({"write_file"})

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
_CMD_IN_STDERR = re.compile(r"in command:\s*'(.+)'")
_DIFF_CMD = re.compile(r"\bgit\s+(diff|show|log)\b", re.I)
_TEST_CMD = re.compile(r"^(python3?|pytest)\b", re.I)

PRIMARY_SS_RUNS = Path("<REPO>")

SOURCE_FIELDS: dict[str, dict[str, Any]] = {
    "a1_repair_damage": {
        "table": "a1_repair_damage",
        "spreadsheet": {
            "candidate_success": "worker_outcome.full_task_success",
            "final_success": "final_outcome.full_task_success",
            "itt_null_final": "null final_outcome.full_task_success → 0 (pre-registered ITT)",
        },
        "swe": {
            "candidate_success": "env_metrics.swebench.candidate_resolved",
            "final_success": "env_metrics.swebench.final_resolved",
            "missing": "null stays missing; never coerced to 0",
        },
        "identity": "q*(1-d) + (1-q)*r = P(final succeeds) on episodes with both outcomes observed",
    },
    "a2_calibration": {
        "table": "a2_calibration",
        "disposition": (
            "accepted_unchanged iff candidate hash present and equals final hash; "
            "revised iff candidate hash present and differs or final hash absent; "
            "never episode.accepted_unchanged"
        ),
        "hashes": {
            "candidate": "handoff.candidate_script_sha256",
            "final": "final_script_sha256",
        },
        "outcomes": "same candidate/final fields as A1",
        "rates": [
            "P(revise|wrong)",
            "P(revise|correct)",
            "P(repair|wrong)",
            "P(damage|correct)",
        ],
    },
    "a3_verification": {
        "table": "a3_verification_by_cell / a3_theta_v",
        "role": "parent only (after handoff)",
        "spreadsheet_executions": (
            "events.jsonl parent tool_result.payload.tool_name "
            "in {run_solution, run_ephemeral_python}"
        ),
        "spreadsheet_inspections": (
            "events.jsonl parent tool_result.payload.tool_name == read_output_range "
            "OR payload.auto_observation present on a run tool (v0.4.1 auto read_output_range)"
        ),
        "swe_executions": (
            "events.jsonl parent exec_shell tool_result.payload.summary "
            "with executed==true and (feedback_command==true or command is python/pytest)"
        ),
        "swe_inspections": (
            "parent read_file tool_result plus parent exec_shell whose recovered "
            "command matches git diff|show|log (command recovered from summary.stderr "
            "'in command: …' or summary output containing 'diff --git')"
        ),
        "revisions": {
            "spreadsheet": "parent tool_result.payload.tool_name == write_solution",
            "swe": "parent tool_result.payload.tool_name == write_file",
        },
        "tokens_after_handoff": "usage.parent.output_tokens",
        "tokens_aux": [
            "usage.parent.input_tokens",
            "usage.parent.cache_read_input_tokens",
            "usage.parent.cache_creation_input_tokens",
        ],
        "theta_v": "θ_V = [V(P1)-V(P0)] - [V(A1)-V(A0)]; task-clustered percentile bootstrap",
        "bootstrap": {"seed": BOOTSTRAP_SEED, "draws": BOOTSTRAP_DRAWS, "alpha": ALPHA},
    },
    "a4_attenuation": {
        "table": "a4_attenuation",
        "label": "descriptive, not mediation",
        "delta_w": "Q_worker(P1) - Q_worker(P0) using A1 candidate field",
        "delta_f": "Q_final(P1) - Q_final(P0) using A1 final field",
        "gap": "Δ_W - Δ_F; task-clustered CI on tasks with both P outcomes observed",
        "bootstrap": {"seed": BOOTSTRAP_SEED, "draws": BOOTSTRAP_DRAWS, "alpha": ALPHA},
    },
    "a5_process_flags": {
        "table": "a5_process_flags",
        "non_finalization": "compliance.parent_finalized is not true",
        "turns_to_finalize": (
            "parent model_call count at first successful submit_solution "
            "(tool_result.error is null); if finalized without submit, "
            "usage.parent.model_calls; missing if not finalized"
        ),
        "context_overflow": {
            "spreadsheet": (
                "run_status/status == agent_failure or "
                "failure_reason == context_window_exceeded or "
                "failures[] source=agent code=context_window_exceeded"
            ),
            "swe": (
                "failure_reason/failures code containing context_window or "
                "prompt_too_long"
            ),
        },
        "empty_final": (
            "missing final_script_path, zero-byte final artifact, "
            "final_script_sha256 == empty-blob SHA-256, or failures code "
            "empty_final_diff"
        ),
    },
}


class ProcessInputError(ValueError):
    """Raised when process-analysis inputs are unusable."""


@dataclass(frozen=True)
class ProcessEpisode:
    """One extracted episode used by every A1–A5 function."""

    environment: str
    run_id: str
    task_id: str
    condition: str
    run_dir: str
    candidate_success: float | None
    final_success: float | None
    candidate_hash: str | None
    final_hash: str | None
    disposition: str
    accepted_unchanged_flag: bool | None
    parent_finalized: bool
    context_overflow: bool
    empty_final: bool
    parent_output_tokens: float | None
    parent_input_tokens: float | None
    parent_cache_read_input_tokens: float | None
    parent_cache_creation_input_tokens: float | None
    parent_model_calls: float | None
    executions: int
    inspections: int
    inspections_explicit: int
    inspections_auto: int
    revisions: int
    turns_to_finalize: float | None
    any_verification: bool
    missing_cell: bool = False


# ── Small helpers ────────────────────────────────────────────────────────────

def _as_binary(value: Any) -> float | None:
    """Coerce True/False/1/0 to a binary float; leave missing as None."""
    if value is True or value == 1 or value == 1.0 or value == "True":
        return 1.0
    if value is False or value == 0 or value == 0.0 or value == "False":
        return 0.0
    return None


def _finite(value: float | None) -> bool:
    return value is not None and not (
        isinstance(value, float) and (math.isnan(value) or math.isinf(value))
    )


def _nonempty_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def disposition_from_hashes(
    candidate_hash: str | None,
    final_hash: str | None,
) -> str:
    """Hash-only disposition. Never reads the record flag."""
    if not candidate_hash:
        return "rebuilt" if final_hash else "no_candidate"
    if not final_hash:
        return "revised"
    if final_hash == candidate_hash:
        return "accepted_unchanged"
    return "revised"


def reconstruct_final_success(
    q: float | None,
    r: float | None,
    d: float | None,
) -> float | None:
    """q(1−d) + (1−q)r; missing r or d contributes 0 when its weight is 0."""
    if q is None:
        return None
    preserve = 0.0 if q == 0.0 else q * (1.0 - (0.0 if d is None else d))
    repair = 0.0 if q == 1.0 else (1.0 - q) * (0.0 if r is None else r)
    return preserve + repair


def _mean(values: Iterable[float]) -> float | None:
    seq = [float(v) for v in values]
    if not seq:
        return None
    return float(sum(seq) / len(seq))


def _median(values: Iterable[float]) -> float | None:
    seq = [float(v) for v in values]
    if not seq:
        return None
    return float(statistics.median(seq))


def _rate(numer: int, denom: int) -> float | None:
    if denom <= 0:
        return None
    return float(numer) / float(denom)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


# ── Event / verification extraction ──────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProcessInputError(f"malformed event {path}:{line_number}") from exc
        if isinstance(event, dict):
            events.append(event)
    return events


def _parse_tool_summary(summary: Any) -> dict[str, Any]:
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


def _command_from_exec_summary(summary: dict[str, Any]) -> str:
    stderr = str(summary.get("stderr") or "")
    match = _CMD_IN_STDERR.search(stderr)
    if match:
        return match.group(1)
    for key in ("command", "cmd"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _is_test_python_command(command: str, summary: dict[str, Any]) -> bool:
    if summary.get("feedback_command") is True:
        return True
    blob = command or ""
    if _TEST_CMD.search(blob.strip()):
        return True
    return False


def _is_diff_view(command: str, summary: dict[str, Any]) -> bool:
    blob = " ".join(
        [
            command,
            str(summary.get("stderr") or ""),
            str(summary.get("output") or ""),
            str(summary.get("stdout") or ""),
        ]
    )
    if _DIFF_CMD.search(command) or _DIFF_CMD.search(blob):
        return True
    if "diff --git" in blob:
        return True
    return False


def _parent_tool_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "tool_result":
            continue
        if event.get("role") != "parent":
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        rows.append(payload)
    return rows


def count_parent_verification(
    events: list[dict[str, Any]],
    environment: str,
) -> dict[str, int]:
    """Count parent executions / inspections / revisions from public events."""
    executions = 0
    inspections_explicit = 0
    inspections_auto = 0
    revisions = 0
    for payload in _parent_tool_results(events):
        tool = payload.get("tool_name")
        if environment == ENV_SPREADSHEET:
            if tool in SS_EXEC_TOOLS:
                executions += 1
            if tool in SS_INSPECT_TOOLS:
                inspections_explicit += 1
            if payload.get("auto_observation") is not None:
                inspections_auto += 1
            if tool in SS_REVISE_TOOLS:
                revisions += 1
        elif environment == ENV_SWE:
            if tool == "exec_shell":
                summary = _parse_tool_summary(payload.get("summary"))
                command = _command_from_exec_summary(summary)
                executed = summary.get("executed")
                if executed is None:
                    executed = float(summary.get("wall_seconds") or 0.0) > 0.001
                if executed and _is_test_python_command(command, summary):
                    executions += 1
                if _is_diff_view(command, summary):
                    inspections_explicit += 1
            if tool in SWE_FILE_VIEW_TOOLS:
                inspections_explicit += 1
            if tool in SWE_REVISE_TOOLS:
                revisions += 1
        else:
            raise ProcessInputError(f"unknown environment {environment!r}")
    inspections = inspections_explicit + inspections_auto
    return {
        "executions": executions,
        "inspections": inspections,
        "inspections_explicit": inspections_explicit,
        "inspections_auto": inspections_auto,
        "revisions": revisions,
    }


def turns_to_finalize(
    events: list[dict[str, Any]],
    *,
    parent_finalized: bool,
    parent_model_calls: float | None,
) -> float | None:
    """Parent model calls until the first successful submit_solution."""
    parent_calls = 0
    for event in events:
        if event.get("role") != "parent":
            continue
        if event.get("event_type") == "model_call":
            parent_calls += 1
            continue
        if event.get("event_type") != "tool_result":
            continue
        payload = event.get("payload") or {}
        if payload.get("tool_name") != "submit_solution":
            continue
        if payload.get("error") in (None, "", False):
            return float(parent_calls)
    if parent_finalized and _finite(parent_model_calls):
        return float(parent_model_calls)
    return None


# ── Record extraction ────────────────────────────────────────────────────────

def _is_swe_context_overflow(record: dict[str, Any]) -> bool:
    reason = str(record.get("failure_reason") or "").lower()
    if "context_window" in reason or "prompt_too_long" in reason:
        return True
    for item in record.get("failures") or []:
        if not isinstance(item, dict):
            continue
        blob = " ".join(
            str(item.get(key) or "") for key in ("code", "message", "reason")
        ).lower()
        if "context_window" in blob or "prompt_too_long" in blob:
            return True
    return False


def _is_empty_final(record: dict[str, Any], run_dir: Path) -> bool:
    for item in record.get("failures") or []:
        if isinstance(item, dict) and item.get("code") == "empty_final_diff":
            return True
    final_hash = _nonempty_hash(record.get("final_script_sha256"))
    if final_hash == EMPTY_SHA256:
        return True
    candidates: list[Path] = []
    recorded = record.get("final_script_path")
    if recorded:
        candidates.append(Path(recorded))
    candidates.append(run_dir / "final" / "solution.py")
    candidates.append(run_dir / "handoff" / "final.patch")
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return final_hash is None
    return any(path.stat().st_size == 0 for path in existing)


def _spreadsheet_outcomes(
    record: dict[str, Any],
) -> tuple[float | None, float | None]:
    worker = (record.get("worker_outcome") or {}).get("full_task_success")
    final = (record.get("final_outcome") or {}).get("full_task_success")
    worker_bin = _as_binary(worker)
    # Pre-registered ITT: null final success is 0, not missing.
    final_bin = 0.0 if final is None else _as_binary(final)
    if final is not None and final_bin is None:
        final_bin = 0.0
    return worker_bin, final_bin


def _swe_outcomes(record: dict[str, Any]) -> tuple[float | None, float | None]:
    em = (record.get("env_metrics") or {}).get("swebench") or {}
    # Missing stays missing. Do not treat evaluator/infra/not_finalized as 0.
    return _as_binary(em.get("candidate_resolved")), _as_binary(em.get("final_resolved"))


def extract_episode(
    record: dict[str, Any],
    run_dir: Path,
    environment: str,
) -> ProcessEpisode:
    """Map one episode.json plus events.jsonl onto the shared process row."""
    events = _load_jsonl(run_dir / "events.jsonl")
    handoff = record.get("handoff") or {}
    usage_parent = (record.get("usage") or {}).get("parent") or {}
    compliance = record.get("compliance") or {}
    candidate_hash = _nonempty_hash(handoff.get("candidate_script_sha256"))
    final_hash = _nonempty_hash(record.get("final_script_sha256"))
    if environment == ENV_SPREADSHEET:
        candidate, final = _spreadsheet_outcomes(record)
        overflow = is_terminal_agent_failure(record)
    elif environment == ENV_SWE:
        candidate, final = _swe_outcomes(record)
        overflow = _is_swe_context_overflow(record)
    else:
        raise ProcessInputError(f"unknown environment {environment!r}")
    counts = count_parent_verification(events, environment)
    parent_finalized = bool(compliance.get("parent_finalized"))
    parent_model_calls = usage_parent.get("model_calls")
    parent_model_calls_f = (
        float(parent_model_calls) if isinstance(parent_model_calls, (int, float)) else None
    )
    tokens = usage_parent.get("output_tokens")
    tokens_f = float(tokens) if isinstance(tokens, (int, float)) else None
    return ProcessEpisode(
        environment=environment,
        run_id=str(record["run_id"]),
        task_id=str(record["task_id"]),
        condition=str(record["condition"]),
        run_dir=str(run_dir),
        candidate_success=candidate,
        final_success=final,
        candidate_hash=candidate_hash,
        final_hash=final_hash,
        disposition=disposition_from_hashes(candidate_hash, final_hash),
        accepted_unchanged_flag=(
            None if "accepted_unchanged" not in record
            else bool(record.get("accepted_unchanged"))
        ),
        parent_finalized=parent_finalized,
        context_overflow=overflow,
        empty_final=_is_empty_final(record, run_dir),
        parent_output_tokens=tokens_f,
        parent_input_tokens=(
            float(usage_parent["input_tokens"])
            if isinstance(usage_parent.get("input_tokens"), (int, float))
            else None
        ),
        parent_cache_read_input_tokens=(
            float(usage_parent["cache_read_input_tokens"])
            if isinstance(usage_parent.get("cache_read_input_tokens"), (int, float))
            else None
        ),
        parent_cache_creation_input_tokens=(
            float(usage_parent["cache_creation_input_tokens"])
            if isinstance(usage_parent.get("cache_creation_input_tokens"), (int, float))
            else None
        ),
        parent_model_calls=parent_model_calls_f,
        executions=counts["executions"],
        inspections=counts["inspections"],
        inspections_explicit=counts["inspections_explicit"],
        inspections_auto=counts["inspections_auto"],
        revisions=counts["revisions"],
        turns_to_finalize=turns_to_finalize(
            events,
            parent_finalized=parent_finalized,
            parent_model_calls=parent_model_calls_f,
        ),
        any_verification=(counts["executions"] + counts["inspections"]) > 0,
        missing_cell=False,
    )


def mark_overflow_missing(episodes: list[ProcessEpisode]) -> list[ProcessEpisode]:
    """Harmonized sensitivity: 57232 A0 is a missing cell, not ITT 0."""
    out: list[ProcessEpisode] = []
    for episode in episodes:
        if (
            episode.environment == ENV_SPREADSHEET
            and episode.task_id == SS_OVERFLOW_TASK
            and episode.condition == SS_OVERFLOW_CONDITION
        ):
            out.append(
                replace(
                    episode,
                    candidate_success=None,
                    final_success=None,
                    missing_cell=True,
                )
            )
        else:
            out.append(episode)
    return out


def _included(episodes: Iterable[ProcessEpisode]) -> list[ProcessEpisode]:
    return [ep for ep in episodes if not ep.missing_cell]


# ── Loaders ──────────────────────────────────────────────────────────────────

def load_spreadsheet_main_v04(runs_dir: Path) -> list[ProcessEpisode]:
    """Load protocol 0.4.1 / task_set main_v04 records (read-only)."""
    if not runs_dir.is_dir():
        raise ProcessInputError(f"spreadsheet runs directory missing: {runs_dir}")
    records: list[ProcessEpisode] = []
    seen_cells: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    for run_dir in sorted(runs_dir.iterdir(), key=lambda path: path.name):
        if not run_dir.is_dir() or run_dir.name.startswith("."):
            continue
        episode_path = run_dir / "episode.json"
        if not episode_path.is_file():
            continue
        try:
            record = json.loads(episode_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcessInputError(f"cannot read {episode_path}") from exc
        if not isinstance(record, dict):
            raise ProcessInputError(f"{episode_path} is not an object")
        if record.get("benchmark") == SWE_BENCHMARK:
            continue
        if record.get("task_set") != SS_TASK_SET:
            continue
        if record.get("protocol_version") != SS_PROTOCOL:
            raise ProcessInputError(
                f"{record.get('run_id')}: expected protocol {SS_PROTOCOL}, "
                f"got {record.get('protocol_version')!r}"
            )
        if record.get("run_id") != run_dir.name:
            raise ProcessInputError(
                f"run-id/directory mismatch: {run_dir.name} vs {record.get('run_id')}"
            )
        run_id = str(record["run_id"])
        cell = (str(record["task_id"]), str(record["condition"]))
        if run_id in seen_ids:
            raise ProcessInputError(f"duplicate run_id {run_id}")
        if cell in seen_cells:
            raise ProcessInputError(f"duplicate design cell {cell}")
        seen_ids.add(run_id)
        seen_cells.add(cell)
        records.append(extract_episode(record, run_dir, ENV_SPREADSHEET))
    if len(records) != 100:
        raise ProcessInputError(
            f"main_v04 must be 25×4=100 records; found {len(records)}"
        )
    by_cond = {cond: 0 for cond in CONDITIONS}
    tasks: set[str] = set()
    for episode in records:
        if episode.condition not in by_cond:
            raise ProcessInputError(f"unsupported condition {episode.condition}")
        by_cond[episode.condition] += 1
        tasks.add(episode.task_id)
    if by_cond != {cond: 25 for cond in CONDITIONS} or len(tasks) != 25:
        raise ProcessInputError(f"main_v04 grid is incomplete: {by_cond} tasks={len(tasks)}")
    return records


def parse_main_v035_run_ids(status_path: Path) -> list[str]:
    """Parse the 32 finished run IDs from the main_v035 batch status table."""
    if not status_path.is_file():
        raise ProcessInputError(f"missing SWE batch status: {status_path}")
    ids: list[str] = []
    seen: set[str] = set()
    for line in status_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        match = _UUID_RE.search(line)
        if match is None:
            continue
        run_id = match.group(0).lower()
        if run_id in seen:
            raise ProcessInputError(f"duplicate run_id in status table: {run_id}")
        seen.add(run_id)
        ids.append(run_id)
    if len(ids) != 32:
        raise ProcessInputError(
            f"{status_path} must list 32 run IDs; found {len(ids)}"
        )
    return ids


def load_swe_main_v035(
    runs_dir: Path,
    status_path: Path,
) -> list[ProcessEpisode]:
    """Load the 32 main_v035 IDs from the batch status file (read-only)."""
    if not runs_dir.is_dir():
        raise ProcessInputError(f"SWE runs directory missing: {runs_dir}")
    run_ids = parse_main_v035_run_ids(status_path)
    episodes: list[ProcessEpisode] = []
    seen_cells: set[tuple[str, str]] = set()
    for run_id in run_ids:
        run_dir = runs_dir / run_id
        episode_path = run_dir / "episode.json"
        if not episode_path.is_file():
            raise ProcessInputError(f"missing episode for listed run_id {run_id}")
        try:
            record = json.loads(episode_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcessInputError(f"cannot read {episode_path}") from exc
        if record.get("run_id") != run_id:
            raise ProcessInputError(
                f"run-id/directory mismatch: {run_id} vs {record.get('run_id')}"
            )
        if record.get("benchmark") != SWE_BENCHMARK:
            raise ProcessInputError(f"{run_id} is not benchmark=swebench")
        if record.get("task_set") != SWE_TASK_SET:
            raise ProcessInputError(
                f"{run_id} task_set={record.get('task_set')!r}, expected {SWE_TASK_SET}"
            )
        cell = (str(record["task_id"]), str(record["condition"]))
        if cell in seen_cells:
            raise ProcessInputError(f"duplicate SWE design cell {cell}")
        seen_cells.add(cell)
        episodes.append(extract_episode(record, run_dir, ENV_SWE))
    by_cond = {cond: 0 for cond in CONDITIONS}
    tasks: set[str] = set()
    for episode in episodes:
        by_cond[episode.condition] += 1
        tasks.add(episode.task_id)
    if by_cond != {cond: 8 for cond in CONDITIONS} or len(tasks) != 8:
        raise ProcessInputError(f"main_v035 grid is incomplete: {by_cond} tasks={len(tasks)}")
    return episodes


# ── A1 repair / damage ───────────────────────────────────────────────────────

def repair_damage_by_cell(episodes: list[ProcessEpisode]) -> dict[str, Any]:
    """q, r, d and counts per P cell; reconstruction must match final rate."""
    cells: dict[str, Any] = {}
    for condition in P_CONDITIONS:
        rows = [
            ep for ep in _included(episodes)
            if ep.condition == condition
            and _finite(ep.candidate_success)
            and _finite(ep.final_success)
        ]
        n = len(rows)
        cand_ok = sum(1 for ep in rows if ep.candidate_success == 1.0)
        cand_fail = n - cand_ok
        final_ok = sum(1 for ep in rows if ep.final_success == 1.0)
        repair_n = sum(
            1 for ep in rows
            if ep.candidate_success == 0.0 and ep.final_success == 1.0
        )
        damage_n = sum(
            1 for ep in rows
            if ep.candidate_success == 1.0 and ep.final_success == 0.0
        )
        preserve_n = sum(
            1 for ep in rows
            if ep.candidate_success == 1.0 and ep.final_success == 1.0
        )
        unrecovered_n = sum(
            1 for ep in rows
            if ep.candidate_success == 0.0 and ep.final_success == 0.0
        )
        q = _rate(cand_ok, n)
        r = _rate(repair_n, cand_fail)
        d = _rate(damage_n, cand_ok)
        final_rate = _rate(final_ok, n)
        reconstructed = reconstruct_final_success(q, r, d)
        if (
            reconstructed is not None
            and final_rate is not None
            and abs(reconstructed - final_rate) > 1e-12
        ):
            raise ProcessInputError(
                f"{condition} reconstruction {reconstructed} != final rate {final_rate}"
            )
        cells[condition] = {
            "condition": condition,
            "n_observed": n,
            "n_excluded_missing": sum(
                1 for ep in _included(episodes)
                if ep.condition == condition
                and not (_finite(ep.candidate_success) and _finite(ep.final_success))
            ),
            "candidate_success_n": cand_ok,
            "candidate_fail_n": cand_fail,
            "final_success_n": final_ok,
            "repair_n": repair_n,
            "damage_n": damage_n,
            "preserve_n": preserve_n,
            "unrecovered_n": unrecovered_n,
            "q": q,
            "r": r,
            "d": d,
            "final_success_rate": final_rate,
            "reconstructed_final_success": reconstructed,
            "identity_holds": (
                reconstructed is None
                or final_rate is None
                or abs(reconstructed - final_rate) <= 1e-12
            ),
        }
    return {
        "analysis": "A1",
        "scope": "P cells with both candidate and final observed",
        "cells": cells,
    }


# ── A2 calibration ───────────────────────────────────────────────────────────

def calibration_by_cell(episodes: list[ProcessEpisode]) -> dict[str, Any]:
    """Candidate correct/incorrect × hash accept/revise, per P cell."""
    cells: dict[str, Any] = {}
    for condition in P_CONDITIONS:
        rows = [
            ep for ep in _included(episodes)
            if ep.condition == condition
            and ep.disposition in {"accepted_unchanged", "revised"}
            and _finite(ep.candidate_success)
        ]
        correct = [ep for ep in rows if ep.candidate_success == 1.0]
        wrong = [ep for ep in rows if ep.candidate_success == 0.0]
        correct_rev = [ep for ep in correct if ep.disposition == "revised"]
        wrong_rev = [ep for ep in wrong if ep.disposition == "revised"]
        both = [ep for ep in rows if _finite(ep.final_success)]
        repair_wrong = [
            ep for ep in both
            if ep.candidate_success == 0.0 and ep.final_success == 1.0
        ]
        damage_correct = [
            ep for ep in both
            if ep.candidate_success == 1.0 and ep.final_success == 0.0
        ]
        cells[condition] = {
            "condition": condition,
            "n_with_candidate_hash": len(rows),
            "correct_n": len(correct),
            "wrong_n": len(wrong),
            "correct_accepted_n": len(correct) - len(correct_rev),
            "correct_revised_n": len(correct_rev),
            "wrong_accepted_n": len(wrong) - len(wrong_rev),
            "wrong_revised_n": len(wrong_rev),
            "p_revise_given_wrong": _rate(len(wrong_rev), len(wrong)),
            "p_revise_given_correct": _rate(len(correct_rev), len(correct)),
            "p_repair_given_wrong": _rate(
                len(repair_wrong),
                sum(1 for ep in both if ep.candidate_success == 0.0),
            ),
            "p_damage_given_correct": _rate(
                len(damage_correct),
                sum(1 for ep in both if ep.candidate_success == 1.0),
            ),
            "disposition_source": "handoff.candidate_script_sha256 vs final_script_sha256",
            "flag_ignored": "accepted_unchanged",
        }
    return {
        "analysis": "A2",
        "scope": "P cells with a candidate hash and observed candidate outcome",
        "cells": cells,
    }


# ── A3 verification / θ_V ────────────────────────────────────────────────────

V_METRICS = (
    "executions",
    "inspections",
    "revisions",
    "tokens_after_handoff",
    "any_verification",
)


def _v_value(episode: ProcessEpisode, metric: str) -> float | None:
    if episode.missing_cell:
        return None
    if metric == "executions":
        return float(episode.executions)
    if metric == "inspections":
        return float(episode.inspections)
    if metric == "revisions":
        return float(episode.revisions)
    if metric == "tokens_after_handoff":
        return episode.parent_output_tokens
    if metric == "any_verification":
        return 1.0 if episode.any_verification else 0.0
    raise ProcessInputError(f"unknown V metric {metric}")


def verification_by_cell(episodes: list[ProcessEpisode]) -> dict[str, Any]:
    """Mean / median / any-verification of parent V, all four cells."""
    cells: dict[str, Any] = {}
    for condition in CONDITIONS:
        rows = [ep for ep in _included(episodes) if ep.condition == condition]
        execs = [float(ep.executions) for ep in rows]
        inspects = [float(ep.inspections) for ep in rows]
        revs = [float(ep.revisions) for ep in rows]
        tokens = [ep.parent_output_tokens for ep in rows if _finite(ep.parent_output_tokens)]
        any_v = [1.0 if ep.any_verification else 0.0 for ep in rows]
        cells[condition] = {
            "condition": condition,
            "n": len(rows),
            "executions_mean": _mean(execs),
            "executions_median": _median(execs),
            "inspections_mean": _mean(inspects),
            "inspections_median": _median(inspects),
            "revisions_mean": _mean(revs),
            "revisions_median": _median(revs),
            "tokens_after_handoff_mean": _mean(tokens),
            "tokens_after_handoff_median": _median(tokens),
            "any_verification": _mean(any_v),
            "any_verification_n": int(sum(any_v)),
        }
    return {
        "analysis": "A3",
        "scope": "parent verification V on included cells",
        "cells": cells,
    }


def _task_metric_grid(
    episodes: list[ProcessEpisode],
    metric: str,
) -> dict[str, dict[str, float]]:
    grid: dict[str, dict[str, float]] = {}
    for episode in episodes:
        value = _v_value(episode, metric)
        if not _finite(value):
            continue
        grid.setdefault(episode.task_id, {})[episode.condition] = float(value)
    return grid


def _pairwise_interaction(grid: dict[str, dict[str, float]]) -> list[float]:
    values: list[float] = []
    for task_values in grid.values():
        if any(cond not in task_values for cond in CONDITIONS):
            continue
        values.append(
            (task_values["P1"] - task_values["P0"])
            - (task_values["A1"] - task_values["A0"])
        )
    return values


def task_clustered_ci(
    task_values: list[float],
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Percentile CI from whole-task resampling (one contrast per task)."""
    n = len(task_values)
    if n == 0:
        return {
            "estimate": None,
            "ci": [None, None],
            "se": None,
            "n_tasks": 0,
            "seed": seed,
            "draws": n_draws,
        }
    arr = np.asarray(task_values, dtype=float)
    estimate = float(arr.mean())
    rng = np.random.default_rng(seed)
    draws = np.empty(n_draws, dtype=float)
    for i in range(n_draws):
        draws[i] = float(arr[rng.integers(0, n, size=n)].mean())
    lo, hi = np.percentile(draws, [100 * ALPHA / 2, 100 * (1 - ALPHA / 2)])
    return {
        "estimate": estimate,
        "ci": [float(lo), float(hi)],
        "se": float(np.std(draws, ddof=1)),
        "n_tasks": n,
        "seed": seed,
        "draws": n_draws,
        "unit": "task",
    }


def theta_v(
    episodes: list[ProcessEpisode],
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """θ_V = [V(P1)−V(P0)] − [V(A1)−V(A0)] for each V metric."""
    metrics: dict[str, Any] = {}
    for metric in V_METRICS:
        contrasts = _pairwise_interaction(_task_metric_grid(episodes, metric))
        metrics[metric] = {
            "metric": metric,
            "formula": "[V(P1)-V(P0)] - [V(A1)-V(A0)]",
            **task_clustered_ci(contrasts, seed=seed, n_draws=n_draws),
        }
    return {
        "analysis": "A3_theta_v",
        "bootstrap": "task-clustered percentile; a few long tasks cannot dominate as i.i.d. episodes",
        "metrics": metrics,
    }


# ── A4 attenuation ───────────────────────────────────────────────────────────

def _paired_p_contrast(
    episodes: list[ProcessEpisode],
    field: str,
) -> list[float]:
    by_task: dict[str, dict[str, float]] = {}
    for episode in _included(episodes):
        if episode.condition not in P_CONDITIONS:
            continue
        value = getattr(episode, field)
        if not _finite(value):
            continue
        by_task.setdefault(episode.task_id, {})[episode.condition] = float(value)
    return [
        values["P1"] - values["P0"]
        for values in by_task.values()
        if "P0" in values and "P1" in values
    ]


def _paired_gap(episodes: list[ProcessEpisode]) -> list[float]:
    by_task: dict[str, dict[str, float]] = {}
    for episode in _included(episodes):
        if episode.condition not in P_CONDITIONS:
            continue
        if not (_finite(episode.candidate_success) and _finite(episode.final_success)):
            continue
        by_task.setdefault(episode.task_id, {})[episode.condition] = (
            float(episode.candidate_success),
            float(episode.final_success),
        )
    gaps: list[float] = []
    for values in by_task.values():
        if "P0" not in values or "P1" not in values:
            continue
        d_w = values["P1"][0] - values["P0"][0]
        d_f = values["P1"][1] - values["P0"][1]
        gaps.append(d_w - d_f)
    return gaps


def attenuation_gap(
    episodes: list[ProcessEpisode],
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Δ_W, Δ_F, and gap = Δ_W − Δ_F. Descriptive, not mediation."""
    d_w = _paired_p_contrast(episodes, "candidate_success")
    d_f = _paired_p_contrast(episodes, "final_success")
    gaps = _paired_gap(episodes)
    return {
        "analysis": "A4",
        "label": "descriptive, not mediation",
        "delta_w": {
            "formula": "Q_worker(P1) - Q_worker(P0)",
            **task_clustered_ci(d_w, seed=seed, n_draws=n_draws),
        },
        "delta_f": {
            "formula": "Q_final(P1) - Q_final(P0)",
            **task_clustered_ci(d_f, seed=seed, n_draws=n_draws),
        },
        "gap": {
            "formula": "Δ_W - Δ_F",
            **task_clustered_ci(gaps, seed=seed, n_draws=n_draws),
        },
    }


# ── A5 process flags ─────────────────────────────────────────────────────────

def process_flags_by_cell(episodes: list[ProcessEpisode]) -> dict[str, Any]:
    """Non-finalization, turns-to-finalize, overflow, empty-final by cell."""
    cells: dict[str, Any] = {}
    for condition in CONDITIONS:
        rows = [ep for ep in _included(episodes) if ep.condition == condition]
        turns = [ep.turns_to_finalize for ep in rows if _finite(ep.turns_to_finalize)]
        cells[condition] = {
            "condition": condition,
            "n": len(rows),
            "non_finalized_n": sum(1 for ep in rows if not ep.parent_finalized),
            "non_finalized_rate": _rate(
                sum(1 for ep in rows if not ep.parent_finalized),
                len(rows),
            ),
            "turns_to_finalize_mean": _mean(turns),
            "turns_to_finalize_median": _median(turns),
            "turns_to_finalize_n": len(turns),
            "context_overflow_n": sum(1 for ep in rows if ep.context_overflow),
            "empty_final_n": sum(1 for ep in rows if ep.empty_final),
        }
    return {
        "analysis": "A5",
        "scope": "included cells",
        "cells": cells,
    }


# ── Table assembly / writing ─────────────────────────────────────────────────

def _cell_rows(payload: dict[str, Any], extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    extra = extra or {}
    for condition in payload.get("cells", {}):
        row = dict(payload["cells"][condition])
        row.update(extra)
        rows.append(row)
    return rows


def _merge_sensitivity(
    primary: dict[str, Any],
    sensitivity: dict[str, Any],
    *,
    key_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Wide rows with primary_* and sensitivity_overflow_missing_* columns."""
    prim_rows = {tuple(row[k] for k in key_fields): row for row in _cell_rows(primary)}
    sens_rows = {tuple(row[k] for k in key_fields): row for row in _cell_rows(sensitivity)}
    keys = list(dict.fromkeys(list(prim_rows) + list(sens_rows)))
    merged: list[dict[str, Any]] = []
    for key in keys:
        p = prim_rows.get(key, {})
        s = sens_rows.get(key, {})
        row: dict[str, Any] = {field: key[i] for i, field in enumerate(key_fields)}
        skip = set(key_fields)
        for field, value in p.items():
            if field in skip:
                continue
            row[f"primary_{field}"] = value
        for field, value in s.items():
            if field in skip:
                continue
            row[f"sensitivity_overflow_missing_{field}"] = value
        merged.append(row)
    return merged


def _merge_contrast_sensitivity(
    primary: dict[str, Any],
    sensitivity: dict[str, Any],
    *,
    branches: tuple[str, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in branches:
        p = dict(primary[name])
        s = dict(sensitivity[name])
        row = {"contrast": name, "formula": p.get("formula") or s.get("formula")}
        for field, value in p.items():
            if field == "formula":
                continue
            row[f"primary_{field}"] = value
        for field, value in s.items():
            if field == "formula":
                continue
            row[f"sensitivity_overflow_missing_{field}"] = value
        rows.append(row)
    return rows


def _theta_rows(payload: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for metric, body in payload["metrics"].items():
        row = {"metric": metric}
        for field, value in body.items():
            if field == "metric":
                continue
            row[f"{prefix}{field}" if prefix else field] = value
        rows.append(row)
    return rows


def compute_process_bundle(
    episodes: list[ProcessEpisode],
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Run A1–A5 on one episode list (primary or sensitivity)."""
    return {
        "a1": repair_damage_by_cell(episodes),
        "a2": calibration_by_cell(episodes),
        "a3": verification_by_cell(episodes),
        "a3_theta": theta_v(episodes, seed=seed, n_draws=n_draws),
        "a4": attenuation_gap(episodes, seed=seed, n_draws=n_draws),
        "a5": process_flags_by_cell(episodes),
    }


def _episodes_table(episodes: list[ProcessEpisode]) -> list[dict[str, Any]]:
    rows = []
    for episode in episodes:
        row = asdict(episode)
        row["hash_accepted_unchanged"] = episode.disposition == "accepted_unchanged"
        rows.append(row)
    return rows


def write_environment_outputs(
    *,
    environment: str,
    episodes: list[ProcessEpisode],
    out_dir: Path,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
    with_sensitivity: bool = False,
) -> dict[str, Any]:
    """Write every process table plus a source-field digest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    primary = compute_process_bundle(episodes, seed=seed, n_draws=n_draws)
    sensitivity = (
        compute_process_bundle(
            mark_overflow_missing(episodes),
            seed=seed,
            n_draws=n_draws,
        )
        if with_sensitivity
        else None
    )

    _write_csv(out_dir / "episodes.csv", _episodes_table(episodes))
    _write_json(out_dir / "source_fields.json", SOURCE_FIELDS)

    if with_sensitivity and sensitivity is not None:
        _write_csv(
            out_dir / "a1_repair_damage.csv",
            _merge_sensitivity(primary["a1"], sensitivity["a1"], key_fields=("condition",)),
        )
        _write_csv(
            out_dir / "a2_calibration.csv",
            _merge_sensitivity(primary["a2"], sensitivity["a2"], key_fields=("condition",)),
        )
        _write_csv(
            out_dir / "a3_verification_by_cell.csv",
            _merge_sensitivity(primary["a3"], sensitivity["a3"], key_fields=("condition",)),
        )
        theta_primary = _theta_rows(primary["a3_theta"], "")
        theta_sens = {
            row["metric"]: row
            for row in _theta_rows(sensitivity["a3_theta"], "")
        }
        theta_merged = []
        for row in theta_primary:
            merged = {"metric": row["metric"]}
            for field, value in row.items():
                if field == "metric":
                    continue
                merged[f"primary_{field}"] = value
            sens = theta_sens.get(row["metric"], {})
            for field, value in sens.items():
                if field == "metric":
                    continue
                merged[f"sensitivity_overflow_missing_{field}"] = value
            theta_merged.append(merged)
        _write_csv(out_dir / "a3_theta_v.csv", theta_merged)
        _write_csv(
            out_dir / "a4_attenuation.csv",
            _merge_contrast_sensitivity(
                primary["a4"],
                sensitivity["a4"],
                branches=("delta_w", "delta_f", "gap"),
            ),
        )
        _write_csv(
            out_dir / "a5_process_flags.csv",
            _merge_sensitivity(primary["a5"], sensitivity["a5"], key_fields=("condition",)),
        )
    else:
        _write_csv(out_dir / "a1_repair_damage.csv", _cell_rows(primary["a1"]))
        _write_csv(out_dir / "a2_calibration.csv", _cell_rows(primary["a2"]))
        _write_csv(out_dir / "a3_verification_by_cell.csv", _cell_rows(primary["a3"]))
        _write_csv(out_dir / "a3_theta_v.csv", _theta_rows(primary["a3_theta"], ""))
        _write_csv(
            out_dir / "a4_attenuation.csv",
            [
                {"contrast": name, **primary["a4"][name]}
                for name in ("delta_w", "delta_f", "gap")
            ],
        )
        _write_csv(out_dir / "a5_process_flags.csv", _cell_rows(primary["a5"]))

    _write_json(out_dir / "a1_repair_damage.json", {
        "primary": primary["a1"],
        "sensitivity_overflow_missing": None if sensitivity is None else sensitivity["a1"],
    })
    _write_json(out_dir / "a2_calibration.json", {
        "primary": primary["a2"],
        "sensitivity_overflow_missing": None if sensitivity is None else sensitivity["a2"],
    })
    _write_json(out_dir / "a3_theta_v.json", {
        "primary": primary["a3_theta"],
        "sensitivity_overflow_missing": None if sensitivity is None else sensitivity["a3_theta"],
    })
    _write_json(out_dir / "a4_attenuation.json", {
        "primary": primary["a4"],
        "sensitivity_overflow_missing": None if sensitivity is None else sensitivity["a4"],
        "label": "descriptive, not mediation",
    })
    _write_json(out_dir / "a5_process_flags.json", {
        "primary": primary["a5"],
        "sensitivity_overflow_missing": None if sensitivity is None else sensitivity["a5"],
    })

    digest = render_digest(
        environment=environment,
        episodes=episodes,
        primary=primary,
        sensitivity=sensitivity,
        out_dir=out_dir,
    )
    (out_dir / "digest.md").write_text(digest, encoding="utf-8")

    summary = {
        "environment": environment,
        "n_episodes": len(episodes),
        "n_tasks": len({ep.task_id for ep in episodes}),
        "with_sensitivity": with_sensitivity,
        "seed": seed,
        "draws": n_draws,
        "out_dir": str(out_dir.resolve()),
        "primary": primary,
        "sensitivity_overflow_missing": sensitivity,
    }
    _write_json(out_dir / "analysis_summary.json", summary)
    return summary


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):.4f}"


def _fmt_ci(body: dict[str, Any], prefix: str = "") -> str:
    estimate = body.get(f"{prefix}estimate", body.get("estimate"))
    ci = body.get(f"{prefix}ci", body.get("ci"))
    n = body.get(f"{prefix}n_tasks", body.get("n_tasks"))
    if estimate is None or not ci:
        return "— (n=0)"
    return f"{float(estimate):+.4f}  [{float(ci[0]):+.4f}, {float(ci[1]):+.4f}]  (n_tasks={n})"


def render_digest(
    *,
    environment: str,
    episodes: list[ProcessEpisode],
    primary: dict[str, Any],
    sensitivity: dict[str, Any] | None,
    out_dir: Path,
) -> str:
    """One digest listing every table and its exact source fields."""
    title = (
        "Spreadsheet main_v04 process digest"
        if environment == ENV_SPREADSHEET
        else "SWE main_v035 process digest"
    )
    lines = [
        f"# {title}",
        "",
        f"- Environment: `{environment}`",
        f"- Episodes: {len(episodes)}",
        f"- Tasks: {len({ep.task_id for ep in episodes})}",
        f"- Output directory: `{out_dir}`",
        "- Code path: `src/delegation_study/analysis/process.py` (same functions both environments).",
        f"- Bootstrap: task-clustered, seed `{BOOTSTRAP_SEED}`, B={BOOTSTRAP_DRAWS}.",
        "",
    ]
    if environment == ENV_SPREADSHEET:
        lines += [
            "Primary coding keeps pre-registered ITT 0, including `57232 × A0` "
            "(`run_status=agent_failure`, `failure_reason=context_window_exceeded`).",
            "Every table below also has **sensitivity_overflow_missing** columns "
            "that treat `57232 A0` as a missing cell.",
            "",
        ]
    else:
        lines += [
            "Primary outcomes are `env_metrics.swebench.final_resolved` / "
            "`candidate_resolved`. Null is missing, never 0.",
            "",
        ]
    lines += [
        "## Tables and source fields",
        "",
        "### `episodes.csv`",
        "",
        "One row per episode after extraction. Source: `episode.json` plus "
        "`events.jsonl` in the run directory. Hash disposition is computed here; "
        "the record flag `accepted_unchanged` is stored only for audit and is "
        "never used in A2.",
        "",
        "### `a1_repair_damage.csv` / `.json`",
        "",
        f"- Spreadsheet candidate: `{SOURCE_FIELDS['a1_repair_damage']['spreadsheet']['candidate_success']}`",
        f"- Spreadsheet final: `{SOURCE_FIELDS['a1_repair_damage']['spreadsheet']['final_success']}`",
        f"- SWE candidate: `{SOURCE_FIELDS['a1_repair_damage']['swe']['candidate_success']}`",
        f"- SWE final: `{SOURCE_FIELDS['a1_repair_damage']['swe']['final_success']}`",
        f"- Identity: {SOURCE_FIELDS['a1_repair_damage']['identity']}",
        "",
        "| cell | q | r | d | final | reconstructed |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cond in P_CONDITIONS:
        cell = primary["a1"]["cells"][cond]
        lines.append(
            f"| {cond} | {_fmt_rate(cell['q'])} | {_fmt_rate(cell['r'])} | "
            f"{_fmt_rate(cell['d'])} | {_fmt_rate(cell['final_success_rate'])} | "
            f"{_fmt_rate(cell['reconstructed_final_success'])} |"
        )
    lines += [
        "",
        "### `a2_calibration.csv` / `.json`",
        "",
        f"- Disposition: {SOURCE_FIELDS['a2_calibration']['disposition']}",
        f"- Candidate hash: `{SOURCE_FIELDS['a2_calibration']['hashes']['candidate']}`",
        f"- Final hash: `{SOURCE_FIELDS['a2_calibration']['hashes']['final']}`",
        f"- Outcomes: {SOURCE_FIELDS['a2_calibration']['outcomes']}",
        "",
        "| cell | P(revise\\|wrong) | P(revise\\|correct) | P(repair\\|wrong) | P(damage\\|correct) |",
        "|---|---:|---:|---:|---:|",
    ]
    for cond in P_CONDITIONS:
        cell = primary["a2"]["cells"][cond]
        lines.append(
            f"| {cond} | {_fmt_rate(cell['p_revise_given_wrong'])} | "
            f"{_fmt_rate(cell['p_revise_given_correct'])} | "
            f"{_fmt_rate(cell['p_repair_given_wrong'])} | "
            f"{_fmt_rate(cell['p_damage_given_correct'])} |"
        )
    a3src = SOURCE_FIELDS["a3_verification"]
    lines += [
        "",
        "### `a3_verification_by_cell.csv`",
        "",
        f"- Role: {a3src['role']}",
        f"- Spreadsheet executions: {a3src['spreadsheet_executions']}",
        f"- Spreadsheet inspections: {a3src['spreadsheet_inspections']}",
        f"- SWE executions: {a3src['swe_executions']}",
        f"- SWE inspections: {a3src['swe_inspections']}",
        f"- Revisions (spreadsheet): {a3src['revisions']['spreadsheet']}",
        f"- Revisions (SWE): {a3src['revisions']['swe']}",
        f"- Tokens after handoff: `{a3src['tokens_after_handoff']}`",
        "",
        "| cell | n | exec mean/median | inspect mean/median | revise mean/median | tokens mean | any-V |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cond in CONDITIONS:
        cell = primary["a3"]["cells"][cond]
        lines.append(
            f"| {cond} | {cell['n']} | "
            f"{_fmt_rate(cell['executions_mean'])}/{_fmt_rate(cell['executions_median'])} | "
            f"{_fmt_rate(cell['inspections_mean'])}/{_fmt_rate(cell['inspections_median'])} | "
            f"{_fmt_rate(cell['revisions_mean'])}/{_fmt_rate(cell['revisions_median'])} | "
            f"{_fmt_rate(cell['tokens_after_handoff_mean'])} | "
            f"{_fmt_rate(cell['any_verification'])} |"
        )
    lines += [
        "",
        "### `a3_theta_v.csv` / `.json`",
        "",
        f"- Estimand: {a3src['theta_v']}",
        f"- Bootstrap: seed {BOOTSTRAP_SEED}, B={BOOTSTRAP_DRAWS}, unit=task.",
        "",
        "| V | θ_V |",
        "|---|---|",
    ]
    for metric, body in primary["a3_theta"]["metrics"].items():
        lines.append(f"| {metric} | {_fmt_ci(body)} |")
    lines += [
        "",
        "### `a4_attenuation.csv` / `.json`",
        "",
        "- **Label: descriptive, not mediation.**",
        f"- Δ_W source: {SOURCE_FIELDS['a4_attenuation']['delta_w']}",
        f"- Δ_F source: {SOURCE_FIELDS['a4_attenuation']['delta_f']}",
        f"- Gap: {SOURCE_FIELDS['a4_attenuation']['gap']}",
        "",
        f"- Δ_W = {_fmt_ci(primary['a4']['delta_w'])}",
        f"- Δ_F = {_fmt_ci(primary['a4']['delta_f'])}",
        f"- gap = {_fmt_ci(primary['a4']['gap'])}",
        "",
        "### `a5_process_flags.csv` / `.json`",
        "",
        f"- Non-finalization: `{SOURCE_FIELDS['a5_process_flags']['non_finalization']}`",
        f"- Turns-to-finalize: {SOURCE_FIELDS['a5_process_flags']['turns_to_finalize']}",
        f"- Context overflow (spreadsheet): {SOURCE_FIELDS['a5_process_flags']['context_overflow']['spreadsheet']}",
        f"- Context overflow (SWE): {SOURCE_FIELDS['a5_process_flags']['context_overflow']['swe']}",
        f"- Empty-final: {SOURCE_FIELDS['a5_process_flags']['empty_final']}",
        "",
        "| cell | n | not finalized | turns mean | overflow | empty-final |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cond in CONDITIONS:
        cell = primary["a5"]["cells"][cond]
        lines.append(
            f"| {cond} | {cell['n']} | {cell['non_finalized_n']} | "
            f"{_fmt_rate(cell['turns_to_finalize_mean'])} | "
            f"{cell['context_overflow_n']} | {cell['empty_final_n']} |"
        )
    lines += [
        "",
        "### `source_fields.json` / `analysis_summary.json`",
        "",
        "Machine-readable copies of this field map and the computed bundles.",
        "",
    ]
    if sensitivity is not None:
        lines += [
            "## Sensitivity note",
            "",
            "Harmonized columns `sensitivity_overflow_missing_*` recompute every "
            "table after coding `57232 A0` as missing. P-cell tables (A1, A2, A4) "
            "are unchanged; A3/A5 A0 means and θ_V pairwise n may change.",
            "",
        ]
    return "\n".join(lines)


def run_process_analysis(
    *,
    spreadsheet_runs: Path,
    swe_runs: Path,
    swe_status: Path,
    spreadsheet_out: Path,
    swe_out: Path,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Load both environments and write both output directories."""
    ss = load_spreadsheet_main_v04(spreadsheet_runs)
    swe = load_swe_main_v035(swe_runs, swe_status)
    ss_summary = write_environment_outputs(
        environment=ENV_SPREADSHEET,
        episodes=ss,
        out_dir=spreadsheet_out,
        seed=seed,
        n_draws=n_draws,
        with_sensitivity=True,
    )
    swe_summary = write_environment_outputs(
        environment=ENV_SWE,
        episodes=swe,
        out_dir=swe_out,
        seed=seed,
        n_draws=n_draws,
        with_sensitivity=False,
    )
    return {"spreadsheet": ss_summary, "swe": swe_summary}


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    repo = _default_repo_root()
    parser = argparse.ArgumentParser(description="S1 process analyses (spreadsheet + SWE)")
    parser.add_argument(
        "--spreadsheet-runs",
        type=Path,
        default=PRIMARY_SS_RUNS,
        help="Read-only Spreadsheet data/runs (main_v04)",
    )
    parser.add_argument(
        "--swe-runs",
        type=Path,
        default=repo / "data" / "runs",
        help="Read-only SWE data/runs (main_v035 IDs)",
    )
    parser.add_argument(
        "--swe-status",
        type=Path,
        default=repo / "data" / "dev" / "swe" / "main_v035_batch_status.md",
        help="main_v035 batch status listing the 32 run IDs",
    )
    parser.add_argument(
        "--spreadsheet-out",
        type=Path,
        default=repo / "paper_outputs" / "process" / "spreadsheet",
    )
    parser.add_argument(
        "--swe-out",
        type=Path,
        default=repo / "paper_outputs" / "process" / "swe",
    )
    parser.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    parser.add_argument("--draws", type=int, default=BOOTSTRAP_DRAWS)
    args = parser.parse_args(argv)
    run_process_analysis(
        spreadsheet_runs=args.spreadsheet_runs,
        swe_runs=args.swe_runs,
        swe_status=args.swe_status,
        spreadsheet_out=args.spreadsheet_out,
        swe_out=args.swe_out,
        seed=args.seed,
        n_draws=args.draws,
    )
    print(f"wrote {args.spreadsheet_out}")
    print(f"wrote {args.swe_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
