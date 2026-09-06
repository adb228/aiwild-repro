"""End-to-end SpreadsheetBench episode runner supporting all four conditions.

Entry point for producing one complete delegation-study episode.  Run as:

    python -m delegation_study.spreadsheetbench.run \\
        --task-id 343-20 --condition A0 --replicate 1 --task-set dev

Hard invariants enforced here
------------------------------
1. Evaluator smoke preflight passes before any agent is constructed.
2. Worker workspace is created from clean public input; never mounts
   data/raw or data/hidden.
3. Parent workspace is created independently from clean public input;
   it has no reference to any worker workspace path, scratch, or output.
4. A conditions: parent receives task description + memo only (no candidate).
   P conditions: parent receives task description + memo + candidate script.
5. Parent never receives worker messages, sandbox output, scores, or traces.
6. Correct phase order: worker → handoff → parent → parent sealed →
   THEN offline candidate evaluation (P only) → final evaluation.
7. episode.json is written atomically via a .tmp → rename sequence.
8. All events are validated against contracts/event_schema.json before write.
9. Run directory creation is exclusive (exist_ok=False): duplicate run_ids
   are detected and refused before any API or container work.

Run directory layout (under data/runs/<run_id>/)
-------------------------------------------------
    episode.json          (written atomically at completion)
    events.jsonl          (public event log, validated per line)
    handoff/
        memo.json         (worker memo)
        solution.py       (worker candidate script — P conditions only)
    worker_eval/          (P conditions only)
        output.xlsx
        evaluator.json
    final/
        solution.py       (parent final script)
        output.xlsx
        evaluator.json
    logs/
        worker_trace.jsonl  (private worker trace; never accessible to parent)
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from ..atomic import atomic_write_json
from ..agent_loop import run_agent_phase
from ..evaluator import evaluate_task
from ..model_client import ModelClient
from ..qc import qc_episode
from ..recalc import recalc_file
from ..records import EventLogger
from ..task_selection import load_spreadsheet_selection
from ..validate import validate_episode
from .execute import StaleContainerError, check_stale_containers
from .preflight import PreflightError, run_preflight
from .tools import ToolDispatcher
from .workspace import TaskWorkspace

# ── Project roots ─────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[3]  # …/Submission/
_CONFIG = _ROOT / "config" / "manifest.yaml"
_TOOL_MANIFESTS = _ROOT / "config" / "tool_manifests.yaml"
_PUBLIC = _ROOT / "data" / "public"
_HIDDEN = _ROOT / "data" / "hidden"
_RUNS = _ROOT / "data" / "runs"

PROTOCOL_VERSION = "0.4.1"

_ALLOWED_CONDITIONS = ("A0", "A1", "P0", "P1", "SOLO")

# Canonical task_set values (also accepted in episode schema).
# The CLI also accepts the alias "dev" → "development".
# "hard" is the exploratory difficulty probe (supplement-only, never pooled with main).
# "main_v04" and "solo_v04" are the v0.4 protocol production task sets.
_ALLOWED_TASK_SETS = ("development", "engineering", "pilot", "main", "hard",
                      "main_v04", "solo_v04")

# Task-sets that require a clean git tree before launch.
_CLEAN_TREE_TASK_SETS = ("pilot", "main", "hard", "main_v04", "solo_v04")


# ── SHA-256 helper ─────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── Task loading ───────────────────────────────────────────────────────────────

def _load_task(task_id: str) -> dict[str, Any]:
    """Load task metadata from data/public/dataset.json."""
    dataset_path = _PUBLIC / "dataset.json"
    if not dataset_path.is_file():
        raise FileNotFoundError(f"dataset.json not found: {dataset_path}")
    dataset = json.loads(dataset_path.read_text())
    if isinstance(dataset, dict):
        task = dataset.get(task_id)
    else:
        task = next(
            (t for t in dataset if str(t.get("task_id", t.get("id", ""))) == task_id),
            None,
        )
    if task is None:
        raise ValueError(f"Task {task_id!r} not found in dataset.json")
    return task


def _find_public_input(task_id: str, init_files: list[str]) -> Path:
    """Find the init xlsx for a task under data/public/<task_id>/."""
    task_pub = _PUBLIC / task_id
    for fname in init_files:
        candidate = task_pub / fname
        if candidate.is_file():
            return candidate
    # Fallback: look for any xlsx in the directory
    for candidate in task_pub.glob("*.xlsx"):
        return candidate
    raise FileNotFoundError(
        f"No init xlsx found for task {task_id!r} in {task_pub}"
    )


def _find_hidden_golden(task_id: str) -> Path:
    """Find the golden xlsx for a task under data/hidden/<task_id>/."""
    task_hid = _HIDDEN / task_id
    for candidate in task_hid.glob("*.xlsx"):
        return candidate
    raise FileNotFoundError(
        f"No golden xlsx found for task {task_id!r} in {task_hid}"
    )


# ── System prompts (mention only actual tools available) ──────────────────────

def _worker_system_prompt(condition: str) -> str:
    """Return a system prompt that mentions ONLY tools the worker actually has."""
    if condition == "A0":
        return (
            "You are a data-processing advisory agent (condition A0).\n\n"
            "Your goal is to inspect the input workbook and produce a detailed memo "
            "describing the task, the data layout, and the approach another agent "
            "should use to solve it.\n\n"
            "Available tools:\n"
            "  list_sheets         — list worksheet names\n"
            "  workbook_metadata   — structural metadata (dimensions, named ranges)\n"
            "  read_range          — read cell values and formulas from a range\n"
            "  search_workbook     — search all cells for a substring\n"
            "  submit_memo         — submit your analysis memo (required to complete)\n\n"
            "These are the ONLY tools available to you.\n\n"
            "When you have finished inspecting:\n"
            "  1. Call submit_memo with your complete analysis memo.\n"
            "     This completes your contribution for this episode."
        )
    elif condition == "A1":
        return (
            "You are a data-processing advisory agent (condition A1).\n\n"
            "Your goal is to inspect the input workbook — including running exploratory "
            "Python code — and produce a detailed memo describing the task, the data "
            "layout, and the approach another agent should use to solve it.\n\n"
            "Available tools:\n"
            "  list_sheets            — list worksheet names\n"
            "  workbook_metadata      — structural metadata (dimensions, named ranges)\n"
            "  read_range             — read the ORIGINAL INPUT workbook. "
            "To inspect what your script produced, use read_output_range.\n"
            "  search_workbook        — search all cells for a substring\n"
            "  run_ephemeral_python   — run exploratory Python code in a sandbox\n"
            "                          (scratch is discarded at handoff)\n"
            "  read_output_range      — read recalculated values from your output workbook\n"
            "  submit_memo            — submit your analysis memo (required to complete)\n\n"
            "IMPORTANT: You MUST call run_ephemeral_python at least once to explore "
            "the data computationally before submitting your memo.\n\n"
            "Each run returns the evaluated contents of the graded range; "
            "read it before deciding your next step.\n\n"
            "When you have finished:\n"
            "  1. Call submit_memo with your complete analysis memo.\n"
            "     This completes your contribution for this episode."
        )
    elif condition == "P0":
        return (
            "You are a data-processing production agent (condition P0).\n\n"
            "Your goal is to inspect the input workbook, write a correct solution "
            "script, and submit both a memo and the solution as the candidate.\n\n"
            "Available tools:\n"
            "  list_sheets        — list worksheet names\n"
            "  workbook_metadata  — structural metadata (dimensions, named ranges)\n"
            "  read_range         — read cell values and formulas from a range\n"
            "  search_workbook    — search all cells for a substring\n"
            "  write_solution     — write or overwrite solution.py (no execution)\n"
            "  submit_memo        — submit your analysis memo\n"
            "  submit_solution    — seal solution.py as the candidate\n\n"
            "These are the ONLY tools available to you. Write a correct solution "
            "without executing it.\n\n"
            "The script MUST accept:\n"
            "  python solution.py --input /input/input.xlsx --output /output/output.xlsx\n\n"
            "When you are satisfied:\n"
            "  1. Call submit_memo with your analysis memo.\n"
            "  2. Call submit_solution to seal your candidate script."
        )
    else:  # P1
        return (
            "You are a data-processing production agent (condition P1).\n\n"
            "Your goal is to inspect the input workbook, write a complete solution "
            "script, test it, and submit both a memo and the sealed solution.\n\n"
            "Available tools:\n"
            "  list_sheets        — list worksheet names\n"
            "  workbook_metadata  — structural metadata (dimensions, named ranges)\n"
            "  read_range         — read the ORIGINAL INPUT workbook. "
            "To inspect what your script produced, use read_output_range.\n"
            "  search_workbook    — search all cells for a substring\n"
            "  write_solution     — write or overwrite solution.py\n"
            "  run_solution       — execute solution.py in the secure sandbox\n"
            "  read_output_range  — read recalculated values from your output workbook\n"
            "  submit_memo        — submit your analysis memo\n"
            "  submit_solution    — seal solution.py as the candidate\n\n"
            "These are the ONLY tools available to you.\n\n"
            "Each run returns the evaluated contents of the graded range; "
            "read it before deciding your next step.\n\n"
            "The script MUST accept:\n"
            "  python solution.py --input /input/input.xlsx --output /output/output.xlsx\n\n"
            "When you are satisfied:\n"
            "  1. Call submit_memo with your analysis memo.\n"
            "  2. Call submit_solution to seal your candidate script."
        )


def _parent_system_prompt() -> str:
    """Return the one condition-independent parent system prompt."""
    return (
        "You are a senior data-processing agent.\n\n"
        "You will receive an original task description and a worker handoff. "
        "The handoff may contain a memo, a candidate solution.py, or both. "
        "Use only the handoff artifacts actually supplied. If a candidate is "
        "supplied, review and revise it as needed; otherwise write solution.py "
        "from scratch. Use any supplied memo to inform and verify your approach.\n\n"
        "Your goal is to:\n"
        "1. Inspect the clean input workbook to verify understanding.\n"
        "2. Produce a complete solution.py.\n"
        "3. Run the solution to confirm it produces correct output.\n"
        "4. Use read_output_range to inspect the recalculated result before sealing.\n"
        "5. Call submit_solution to seal the final script.\n\n"
        "Available tools:\n"
        "  list_sheets        — list worksheet names\n"
        "  workbook_metadata  — structural metadata (dimensions, named ranges)\n"
        "  read_range         — read the ORIGINAL INPUT workbook. "
        "To inspect what your script produced, use read_output_range.\n"
        "  search_workbook    — search all cells for a substring\n"
        "  write_solution     — write or overwrite solution.py\n"
        "  run_solution       — execute solution.py in the secure sandbox\n"
        "  read_output_range  — read recalculated values from your output workbook\n"
        "  submit_solution    — seal the final script\n\n"
        "Important:\n"
        "- You start from a clean copy of the input workbook.\n"
        "- You do NOT have access to the worker's execution history, scratch files,\n"
        "  sandbox output, or any evaluation results.\n"
        "- Each run returns the evaluated contents of the graded range; "
        "read it before deciding your next step.\n"
        "- The script MUST accept:\n"
        "    python solution.py --input /input/input.xlsx --output /output/output.xlsx\n"
        "- Calling submit_solution finalises the episode."
    )


def _solo_system_prompt() -> str:
    """Return the SOLO condition system prompt.

    Identical to the condition-independent parent prompt in structure but with
    no references to worker handoffs or handoff artifacts.  This is the
    canonical prompt used when one agent solves from scratch with the full
    parent tool set and no worker phase.
    """
    return (
        "You are a senior data-processing agent.\n\n"
        "You will receive a spreadsheet manipulation task and must solve it "
        "from scratch with the full tool set.\n\n"
        "Your goal is to:\n"
        "1. Inspect the input workbook to understand the task.\n"
        "2. Produce a complete solution.py.\n"
        "3. Run the solution to confirm it produces correct output.\n"
        "4. Use read_output_range to inspect the recalculated result before sealing.\n"
        "5. Call submit_solution to seal the final script.\n\n"
        "Available tools:\n"
        "  list_sheets        — list worksheet names\n"
        "  workbook_metadata  — structural metadata (dimensions, named ranges)\n"
        "  read_range         — read the ORIGINAL INPUT workbook. "
        "To inspect what your script produced, use read_output_range.\n"
        "  search_workbook    — search all cells for a substring\n"
        "  write_solution     — write or overwrite solution.py\n"
        "  run_solution       — execute solution.py in the secure sandbox\n"
        "  read_output_range  — read recalculated values from your output workbook\n"
        "  submit_solution    — seal the final script\n\n"
        "Important:\n"
        "- You start from a clean copy of the input workbook.\n"
        "- Each run returns the evaluated contents of the graded range; "
        "read it before deciding your next step.\n"
        "- The script MUST accept:\n"
        "    python solution.py --input /input/input.xlsx --output /output/output.xlsx\n"
        "- Calling submit_solution finalises the episode."
    )


def _official_task_text(task: dict[str, Any], libreoffice_version: str = "26.8.0.3") -> str:
    """Format public task metadata using SpreadsheetBench's official structure.

    The LibreOffice sentence is appended to both worker and parent task text
    so it is byte-identical across all cells for a fixed task.
    """
    lo_sentence = (
        f"Formulas you write are evaluated with LibreOffice {libreoffice_version} at grading; "
        "use functions and reference syntax that evaluate there. "
        "Values written directly are graded as-is."
    )
    return (
        "Below is the spreadsheet manipulation question you need to solve:\n"
        "### instruction\n"
        f"{task['instruction']}\n\n"
        "### spreadsheet_path\n"
        "/input/input.xlsx\n\n"
        "### instruction_type\n"
        f"{task['instruction_type']}\n\n"
        "### answer_position\n"
        f"{task['answer_position']}\n\n"
        "### output_path\n"
        f"/output/output.xlsx\n\n"
        f"{lo_sentence}"
    )


def _prompt_sha256(prompt: str) -> str:
    """Return the SHA-256 of the exact UTF-8 system prompt sent to the model."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _worker_user_message(task: dict[str, Any], condition: str,
                          libreoffice_version: str = "26.8.0.3") -> str:
    base = (
        f"{_official_task_text(task, libreoffice_version)}\n\n"
        "The input workbook is available via the inspection tools.\n"
        "(When running code, it is at /input/input.xlsx inside the sandbox.)\n\n"
    )
    if condition in ("A0", "A1"):
        return base + "When you have finished inspecting, call submit_memo with your analysis memo."
    else:
        return base + (
            "When you are satisfied with your solution:\n"
            "1. Call submit_memo with your analysis memo.\n"
            "2. Call submit_solution to seal your candidate script.\n"
        )


def _parent_user_message_advisory(
    task: dict[str, Any],
    memo: dict[str, Any],
    libreoffice_version: str = "26.8.0.3",
) -> str:
    """Parent user message for A conditions — no candidate script."""
    memo_json = json.dumps(memo, indent=2)
    return (
        f"{_official_task_text(task, libreoffice_version)}\n\n"
        "== Worker Advisory Memo ==\n"
        f"{memo_json}\n\n"
        "The input workbook is available via the inspection tools "
        "(or as /input/input.xlsx when running the solution).\n\n"
        "Please write a complete solution.py based on the worker memo, test it, "
        "then call submit_solution to seal the final script."
    )


def _parent_user_message_production(
    task: dict[str, Any],
    memo: dict[str, Any],
    candidate_script: str,
    libreoffice_version: str = "26.8.0.3",
) -> str:
    """Parent user message for P conditions — includes candidate script."""
    memo_json = json.dumps(memo, indent=2)
    return (
        f"{_official_task_text(task, libreoffice_version)}\n\n"
        "== Worker Memo ==\n"
        f"{memo_json}\n\n"
        "== Candidate Solution (solution.py) ==\n"
        f"```python\n{candidate_script}\n```\n\n"
        "The input workbook is available via the inspection tools "
        "(or as /input/input.xlsx when running the solution).\n\n"
        "Please review the solution, test it, revise if needed, then call "
        "submit_solution to seal the final script."
    )


def _solo_user_message(task: dict[str, Any],
                        libreoffice_version: str = "26.8.0.3") -> str:
    """SOLO user message — no handoff material, solve from scratch."""
    return (
        f"{_official_task_text(task, libreoffice_version)}\n\n"
        "The input workbook is available via the inspection tools "
        "(or as /input/input.xlsx when running the solution).\n\n"
        "Please write a complete solution.py, test it, then call "
        "submit_solution to seal the final script."
    )


def _compute_solo_budget(worker_budget: dict[str, Any], parent_budget: dict[str, Any]) -> dict[str, Any]:
    """Compute the SOLO budget by summing worker + parent caps.

    Summed caps: max_turns, max_tool_calls, max_output_tokens_total, max_wall_seconds.
    Unchanged: max_tokens (per-call limit), max_input_tokens_safety (single ceiling).
    """
    return {
        "max_turns": worker_budget["max_turns"] + parent_budget["max_turns"],
        "max_tool_calls": worker_budget["max_tool_calls"] + parent_budget["max_tool_calls"],
        "max_output_tokens_total": (
            worker_budget["max_output_tokens_total"] + parent_budget["max_output_tokens_total"]
        ),
        "max_wall_seconds": worker_budget["max_wall_seconds"] + parent_budget["max_wall_seconds"],
        # Per-call token limit is unchanged — not summed.
        "max_tokens": worker_budget["max_tokens"],
        # Input-safety ceiling stays at the single configured ceiling — not summed.
        "max_input_tokens_safety": worker_budget["max_input_tokens_safety"],
    }


# ── Episode runner ──────────────────────────────────────────────────────────────

class DirtyTreeError(RuntimeError):
    """Raised when pilot/main episode is attempted with a dirty git tree."""


class RunDirectoryCollisionError(RuntimeError):
    """Raised when run_id directory already exists (exclusive creation failed)."""


class InfrastructureFailure(RuntimeError):
    """Failure in the episode harness rather than agent behavior."""


class HandoffLost(InfrastructureFailure):
    """A submitted artifact disappeared before it could be persisted."""


def _successful_submits(events_path: Path, role: str) -> set[str]:
    calls: set[tuple[str, str]] = set()
    submitted: set[str] = set()
    for line in events_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("role") != role:
            continue
        payload = event.get("payload", {})
        name = payload.get("tool_name")
        tool_use_id = payload.get("tool_use_id")
        if name not in {"submit_memo", "submit_solution"} or not tool_use_id:
            continue
        key = (name, tool_use_id)
        if event.get("event_type") == "tool_call":
            calls.add(key)
        elif (
            event.get("event_type") == "tool_result"
            and payload.get("error") is None
            and key in calls
        ):
            submitted.add(name)
    return submitted


def _compute_auto_obs_compliance(
    events_path: Path, condition: str
) -> dict[str, Any]:
    """Derive auto-observation compliance counts from actual events.jsonl (v0.4.1).

    Counts eligible run-tool events (tool_result for run_solution /
    run_ephemeral_python) and checks whether each carries an ``auto_observation``
    payload in the serialised event.  This is derived from real events, not
    model claims.

    Eligible worker roles: A1 and P1 only (A0/P0 have no run tools).
    Eligible parent roles: all conditions including SOLO.
    """
    _RUN_TOOLS = {"run_solution", "run_ephemeral_python"}
    _ELIGIBLE_WORKER_CONDITIONS = {"A1", "P1"}

    eligible_worker = 0
    eligible_parent = 0
    worker_missing = 0
    parent_missing = 0

    if not events_path.exists():
        return {
            "eligible_worker_run_events": 0,
            "eligible_parent_run_events": 0,
            "worker_runs_without_auto_observation": 0,
            "parent_runs_without_auto_observation": 0,
        }

    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            ev = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if ev.get("event_type") != "tool_result":
            continue
        payload = ev.get("payload", {})
        if payload.get("tool_name") not in _RUN_TOOLS:
            continue
        role = ev.get("role", "")
        has_auto = "auto_observation" in payload

        if role == "worker" and condition in _ELIGIBLE_WORKER_CONDITIONS:
            eligible_worker += 1
            if not has_auto:
                worker_missing += 1
        elif role == "parent":
            eligible_parent += 1
            if not has_auto:
                parent_missing += 1

    return {
        "eligible_worker_run_events": eligible_worker,
        "eligible_parent_run_events": eligible_parent,
        "worker_runs_without_auto_observation": worker_missing,
        "parent_runs_without_auto_observation": parent_missing,
    }


def _successful_worker_submits(events_path: Path) -> set[str]:
    return _successful_submits(events_path, "worker")


def persist_handoff(
    condition: str,
    sealed: dict[str, Any],
    handoff_dir: Path,
    *,
    worker_submitted_memo: bool,
    worker_submitted_candidate: bool,
) -> dict[str, Any]:
    """Copy and hash submitted artifacts before the worker workspace is removed."""
    record: dict[str, Any] = {
        "memo_path": None,
        "memo_sha256": None,
        "candidate_script_path": None,
        "candidate_script_sha256": None,
        "worker_submitted_memo": worker_submitted_memo,
        "worker_submitted_candidate": worker_submitted_candidate,
    }
    artifacts = [
        ("memo", worker_submitted_memo, sealed.get("memo_path"), "memo.json"),
    ]
    if condition.startswith("P"):
        artifacts.append((
            "candidate",
            worker_submitted_candidate,
            sealed.get("candidate_script_path"),
            "solution.py",
        ))
    lost: list[str] = []
    for name, submitted, raw_path, filename in artifacts:
        if not submitted:
            continue
        source = Path(raw_path) if raw_path else None
        if source is None or not source.is_file():
            lost.append(name)
            continue
        destination = handoff_dir / filename
        try:
            shutil.copy2(source, destination)
        except FileNotFoundError:
            lost.append(name)
            continue
        path_key = "memo_path" if name == "memo" else "candidate_script_path"
        hash_key = "memo_sha256" if name == "memo" else "candidate_script_sha256"
        record[path_key] = str(destination)
        record[hash_key] = _sha256(destination)
    if lost:
        exc = HandoffLost(
            "handoff_lost: submitted "
            f"{', '.join(lost)} is absent at persistence time"
        )
        exc.handoff_record = dict(record)  # type: ignore[attr-defined]
        raise exc
    return record


def run_worker_phase(
    *,
    condition: str,
    run_id: str,
    task_id: str,
    task: dict[str, Any],
    public_input: Path,
    batch_id: str | None,
    client: ModelClient,
    worker_budget: dict[str, Any],
    event_logger: EventLogger,
    private_logger: EventLogger,
    handoff_dir: Path,
    worker_system_prompt: str,
    soffice: str | None = None,
    libreoffice_version: str = "26.8.0.3",
) -> tuple[Any, int, bool, dict[str, Any], bool, bool, int, bool, int]:
    """Run, seal, and persist the worker handoff; cleanup always runs last.

    Returns a 9-tuple:
      (worker_result, feedback_call_count, scratch_destroyed,
       handoff_record, submitted_memo, submitted_candidate,
       observed_output_calls, revised_after_observation,
       worker_auto_observations)
    """
    worker_ws = TaskWorkspace.create(
        run_id=f"{run_id}_worker",
        task_id=task_id,
        source_input=public_input,
    )
    try:
        dispatcher = ToolDispatcher(
            condition=condition,
            role="worker",
            workspace=worker_ws,
            run_id=run_id,
            batch_id=batch_id,
            on_scratch_destroyed=lambda: event_logger.log(
                "system",
                "scratch_destroyed",
                {"phase": "worker", "condition": condition},
            ),
            soffice=soffice,
            answer_position=task.get("answer_position"),
        )
        result = run_agent_phase(
            role="worker",
            condition=condition,
            client=client,
            dispatcher=dispatcher,
            system_prompt=worker_system_prompt,
            initial_user_message=_worker_user_message(task, condition, libreoffice_version),
            budget=worker_budget,
            event_logger=event_logger,
            private_logger=private_logger,
        )
        event_logger.log("system", "phase_completed", {
            "phase": "worker",
            "terminated_normally": result.terminated_normally,
            "failure_code": result.failure_code,
        })
        submitted = _successful_worker_submits(event_logger._path)
        submitted_memo = "submit_memo" in submitted
        submitted_candidate = "submit_solution" in submitted
        sealed = dispatcher.seal_handoff()
        handoff = persist_handoff(
            condition,
            sealed,
            handoff_dir,
            worker_submitted_memo=submitted_memo,
            worker_submitted_candidate=submitted_candidate,
        )
        return (
            result,
            dispatcher.feedback_call_count,
            worker_ws.scratch_destroyed,
            handoff,
            submitted_memo,
            submitted_candidate,
            dispatcher.observed_output_call_count,
            dispatcher.worker_revised_after_observation,
            dispatcher.auto_observation_count,
        )
    except HandoffLost as exc:
        exc.worker_result = result  # type: ignore[attr-defined]
        exc.worker_feedback_calls = dispatcher.feedback_call_count  # type: ignore[attr-defined]
        exc.scratch_destroyed = worker_ws.scratch_destroyed  # type: ignore[attr-defined]
        exc.worker_submitted_memo = submitted_memo  # type: ignore[attr-defined]
        exc.worker_submitted_candidate = submitted_candidate  # type: ignore[attr-defined]
        exc.worker_observed_output_calls = dispatcher.observed_output_call_count  # type: ignore[attr-defined]
        exc.worker_revised_after_observation = dispatcher.worker_revised_after_observation  # type: ignore[attr-defined]
        exc.worker_auto_observations = dispatcher.auto_observation_count  # type: ignore[attr-defined]
        raise
    finally:
        worker_ws.cleanup()


def _decode_porcelain_path(raw: str) -> str:
    """Decode a git porcelain v1 path field.

    git quotes paths containing special characters (non-ASCII, newlines, etc.)
    using C-style quoting.  This decoder handles the common escape sequences:
    \\, \", \n, \t, and \\NNN octal.
    """
    raw = raw.strip()
    if not (raw.startswith('"') and raw.endswith('"')):
        return raw
    result: list[str] = []
    i = 1          # skip opening quote
    end = len(raw) - 1  # stop before closing quote
    while i < end:
        ch = raw[i]
        if ch == "\\" and i + 1 < end:
            nxt = raw[i + 1]
            if nxt == "\\":
                result.append("\\"); i += 2
            elif nxt == '"':
                result.append('"'); i += 2
            elif nxt == "n":
                result.append("\n"); i += 2
            elif nxt == "t":
                result.append("\t"); i += 2
            elif nxt in "01234567":
                # Octal escape: up to 3 digits
                j = i + 1
                while j < end and j < i + 4 and raw[j] in "01234567":
                    j += 1
                result.append(chr(int(raw[i + 1 : j], 8)))
                i = j
            else:
                result.append(ch); i += 1
        else:
            result.append(ch); i += 1
    return "".join(result)


# Protected directory prefixes for untracked-file dirty detection.
# An untracked file is only considered "dirty" when it falls under one of
# these prefixes; untracked files elsewhere (data/dev/, docs/, …) are allowed
# and do not block pilot/main launches.
_PROTECTED_UNTRACKED_PREFIXES: tuple[str, ...] = (
    "src/",
    "contracts/",
    "config/",
    "scripts/",
    "tests/",
)


def _compute_git_dirty(repo_root: Path) -> bool | None:
    """Return True if the working tree is dirty under the narrowed provenance gate.

    The gate fails (returns True) when any of the following are true:
    - Any tracked file has a staged or worktree change (modified, deleted,
      renamed, type-changed, etc.).
    - Any untracked (non-ignored) path begins with a protected directory
      prefix: src/, contracts/, config/, scripts/, or tests/.

    Untracked paths outside those prefixes (e.g. data/dev/, docs/) do NOT
    mark the tree dirty and must not block pilot/main launches.

    Returns None if git is unavailable or the repository cannot be queried
    (callers treat None as unknown and fail-closed for pilot/main).
    """
    import subprocess

    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repo_root,
            text=True,
        )
    except Exception:
        return None  # type: ignore[return-value]

    for line in out.splitlines():
        if len(line) < 2:
            continue
        xy = line[:2]
        if xy == "??":
            # Untracked file.  Extract path (after the "?? " prefix), decode
            # any C-style quoting, then test against protected prefixes.
            raw_path = line[3:] if len(line) > 3 else ""
            path = _decode_porcelain_path(raw_path)
            if any(path.startswith(prefix) for prefix in _PROTECTED_UNTRACKED_PREFIXES):
                return True
            # Untracked file outside protected dirs — not dirty, continue.
        else:
            # Any non-untracked status code means a tracked file has changed
            # (staged, worktree-modified, deleted, renamed, etc.).
            return True

    return False


def run_episode(
    task_id: str,
    condition: str = "P1",
    replicate: int = 1,
    *,
    task_set: str = "development",
    run_id: str | None = None,
    runs_dir: Path | None = None,
    skip_preflight: bool = False,
    skip_stale_check: bool = False,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Run one complete delegation episode for any condition (A0, A1, P0, P1).

    Parameters
    ----------
    task_id:
        SpreadsheetBench task identifier (e.g. ``"343-20"``).
    condition:
        Experimental condition: ``"A0"``, ``"A1"``, ``"P0"``, or ``"P1"``.
    replicate:
        Replicate index (≥ 1).
    task_set:
        Collection tier: ``"development"``, ``"engineering"``, ``"pilot"``, or ``"main"``.
        Alias ``"dev"`` is accepted and stored as ``"development"``.
        Episodes with ``task_set`` of ``"pilot"`` or ``"main"`` are refused when the
        git working tree is dirty.
    run_id:
        Override the auto-generated UUID run identifier.  Must be unique;
        run directory creation is exclusive (FileExistsError if already present).
    runs_dir:
        Override the default ``data/runs/`` directory.
    skip_preflight:
        Skip the evaluator smoke preflight (test use only).
    skip_stale_check:
        Skip the stale-container check.  Only safe when an orchestrator has
        already performed the check and spawned this process as part of a
        declared concurrent batch.
    batch_id:
        UUID shared by all concurrent conditions launched together (e.g. four
        tmux panes in the same smoke run).  Passed to ``check_stale_containers``
        so containers from sibling conditions are recognized as expected peers
        rather than stale leftovers.  Also applied as a Docker label on every
        container this episode starts, enabling cross-condition identification.

    Returns
    -------
    dict
        The final episode record (same as the written episode.json).

    Raises
    ------
    RunDirectoryCollisionError
        If the run_id directory already exists (duplicate run prevented).
    DirtyTreeError
        If task_set is pilot/main and the git tree is dirty.
    PreflightError
        If evaluator smoke preflight fails (when skip_preflight=False).
    StaleContainerError
        If stale delegation-study containers are detected (when skip_stale_check=False).
    """
    # Validate condition and task_set
    if condition not in _ALLOWED_CONDITIONS:
        raise ValueError(
            f"condition must be one of {_ALLOWED_CONDITIONS}; got {condition!r}"
        )
    if task_set not in _ALLOWED_TASK_SETS:
        raise ValueError(
            f"task_set must be one of {_ALLOWED_TASK_SETS!r}; got {task_set!r}"
        )

    t_episode_start = time.monotonic()
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # ── 0a. Git provenance and clean-tree gate ─────────────────────────────────
    git_dirty_val = _compute_git_dirty(_ROOT)
    if task_set in _CLEAN_TREE_TASK_SETS:
        if git_dirty_val is None:
            raise DirtyTreeError(
                f"Cannot determine git tree state; refusing to start {task_set!r} episode. "
                "Ensure git is available and the repository is accessible."
            )
        if git_dirty_val:
            raise DirtyTreeError(
                f"Git tree is dirty; refusing to start {task_set!r} episode. "
                "Commit or stash all changes before running pilot/main/hard episodes."
            )
    git_dirty: bool = bool(git_dirty_val) if git_dirty_val is not None else False

    # ── 0b. Stale container check ─────────────────────────────────────────────
    # skip_stale_check=True when an orchestrator performed the check before
    # spawning concurrent children — prevents false positives from sibling runs.
    # When batch_id is provided without skip_stale_check, containers from the
    # same batch are excluded from the "stale" set (concurrent peer detection).
    if not skip_stale_check:
        check_stale_containers(batch_id=batch_id)  # raises StaleContainerError if any found

    # ── 0c. Evaluator smoke preflight ─────────────────────────────────────────
    if not skip_preflight:
        run_preflight()  # raises PreflightError on any failure

    # ── 1. Load manifest and task ─────────────────────────────────────────────
    with _CONFIG.open() as fh:
        manifest = yaml.safe_load(fh)

    task = _load_task(task_id)
    answer_position: str = task["answer_position"]
    answer_sheet: str | None = task.get("answer_sheet")
    init_files: list[str] = task.get("init_files", [])

    public_input = _find_public_input(task_id, init_files)
    golden_path = _find_hidden_golden(task_id)

    # ── 2. Create run directory (exclusive — collision prevention) ─────────────
    run_id = run_id or str(uuid.uuid4())
    base_runs = runs_dir if runs_dir is not None else _RUNS
    run_dir = base_runs / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)  # exclusive: raises if exists
    except FileExistsError:
        raise RunDirectoryCollisionError(
            f"Run directory already exists: {run_dir}. "
            f"run_id={run_id!r} is not unique. Use a different run_id."
        )


    # ── Early-failure cleanup guard ─────────────────────────────────────────
    # If an exception escapes after the run directory was created but before
    # the first successful model_call event, remove the stub run directory.
    # Any run that logged at least one model response is preserved intact.
    _episode_client: ModelClient | None = None
    try:
        handoff_dir = run_dir / "handoff"
        worker_eval_dir = run_dir / "worker_eval"
        final_dir = run_dir / "final"
        logs_dir = run_dir / "logs"
        for d in (handoff_dir, final_dir, logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        # worker_eval only for P conditions (created later if needed)

        # ── 3. Set up loggers and model client ─────────────────────────────────────
        event_logger = EventLogger(run_dir / "events.jsonl", run_id)
        private_logger = EventLogger(logs_dir / "worker_trace.jsonl", run_id)

        client = ModelClient()
        _episode_client = client  # register for early-failure cleanup

        # Git commit for reproducibility record
        try:
            import subprocess
            schema_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True
            ).strip()
        except Exception:
            schema_commit = "uncommitted"

        benchmark_commit = manifest["benchmark"]["git_commit"]
        dataset_sha = manifest["benchmark"].get(
            "archive_sha256",
            manifest["benchmark"].get("archive_sha256", "0" * 64),
        )
        docker_digest = manifest["sandbox"]["image_digest"]

        worker_model = manifest["models"]["worker"]
        parent_model = manifest["models"]["parent"]
        temperature = manifest["models"]["temperature"]
        worker_budget = manifest["budgets"]["worker"]
        parent_budget = manifest["budgets"]["parent"]
        libreoffice_version = str(manifest["recalculation"]["libreoffice_version"])
        soffice_bin = manifest["recalculation"]["executable"]
        if condition == "SOLO":
            solo_budget = _compute_solo_budget(worker_budget, parent_budget)
            worker_system_prompt = ""  # not used for SOLO
            parent_system_prompt = _solo_system_prompt()
            worker_prompt_hash = "0" * 64  # no worker; sentinel zero hash
            parent_prompt_hash = _prompt_sha256(parent_system_prompt)
        else:
            solo_budget = None
            worker_system_prompt = _worker_system_prompt(condition)
            parent_system_prompt = _parent_system_prompt()
            worker_prompt_hash = _prompt_sha256(worker_system_prompt)
            parent_prompt_hash = _prompt_sha256(parent_system_prompt)
        selection = load_spreadsheet_selection(_CONFIG)

        # ── 4. Log episode start ──────────────────────────────────────────────────
        if condition == "SOLO":
            # SOLO: skip worker entirely; log a solo phase start.
            event_logger.log("system", "phase_started", {"phase": "solo", "task_id": task_id})

            # Sentinel values for the absent worker phase.
            worker_result = None
            worker_feedback_calls = 0
            scratch_destroyed_at_handoff = True   # no worker scratch ever existed
            worker_submitted_memo = False
            worker_submitted_candidate = False
            worker_observed_output_calls = 0       # no worker phase
            worker_revised_after_observation = False  # no worker phase
            worker_auto_observation_count = 0      # no worker phase

            # No handoff artifacts for SOLO.
            handoff_record: dict[str, Any] = {
                "memo_path": None,
                "memo_sha256": None,
                "candidate_script_path": None,
                "candidate_script_sha256": None,
                "worker_submitted_memo": False,
                "worker_submitted_candidate": False,
                "memo_claims_validation": False,   # no memo for SOLO
            }
        else:
            event_logger.log("system", "phase_started", {"phase": "worker", "task_id": task_id})

            # ── 5. Worker phase ───────────────────────────────────────────────────────
            (
                worker_result,
                worker_feedback_calls,
                scratch_destroyed_at_handoff,
                handoff_record,
                worker_submitted_memo,
                worker_submitted_candidate,
                worker_observed_output_calls,
                worker_revised_after_observation,
                worker_auto_observation_count,
            ) = run_worker_phase(
                condition=condition,
                run_id=run_id,
                task_id=task_id,
                task=task,
                public_input=public_input,
                batch_id=batch_id,
                client=client,
                worker_budget=worker_budget,
                event_logger=event_logger,
                private_logger=private_logger,
                handoff_dir=handoff_dir,
                worker_system_prompt=worker_system_prompt,
                soffice=soffice_bin,
                libreoffice_version=libreoffice_version,
            )

            event_logger.log("system", "handoff_sealed", {
                "memo_present": handoff_record["memo_sha256"] is not None,
                "candidate_present": handoff_record["candidate_script_sha256"] is not None,
            })

        # ── 7. Parent / SOLO phase ────────────────────────────────────────────────
        if condition == "SOLO":
            event_logger.log("system", "phase_started", {"phase": "solo", "task_id": task_id})
        else:
            event_logger.log("system", "phase_started", {"phase": "parent", "task_id": task_id})

        # Load memo and (for P conditions) candidate script for parent context.
        # Parent receives ONLY memo + (P: candidate) — no worker trace, no eval results.
        # SOLO receives no handoff material — it solves from scratch.
        memo_content: dict[str, Any] = {}
        candidate_script_content: str = ""

        if handoff_record["memo_path"]:
            memo_content = json.loads(Path(handoff_record["memo_path"]).read_text())
            # v0.4: memo_claims_validation — True if observed_feedback is non-empty
            handoff_record["memo_claims_validation"] = bool(
                memo_content.get("observed_feedback")
            )
        elif "memo_claims_validation" not in handoff_record:
            handoff_record["memo_claims_validation"] = False

        if handoff_record["candidate_script_path"]:
            candidate_script_content = Path(handoff_record["candidate_script_path"]).read_text()

        # Compose parent/SOLO user message.
        if condition == "SOLO":
            parent_user_msg = _solo_user_message(task, libreoffice_version)
        elif condition.startswith("A"):
            parent_user_msg = _parent_user_message_advisory(task, memo_content, libreoffice_version)
        else:
            parent_user_msg = _parent_user_message_production(
                task, memo_content, candidate_script_content, libreoffice_version
            )

        # Default parent observation count (overridden after parent phase)
        parent_observed_output_calls: int = 0
        # v0.4.1: default parent auto-observation count (overridden after parent phase)
        parent_auto_observation_count: int = 0

        # Fresh parent workspace from clean public input (no worker files)
        parent_ws = TaskWorkspace.create(
            run_id=f"{run_id}_parent",
            task_id=task_id,
            source_input=public_input,  # clean copy; NOT worker output
        )
        try:
            parent_dispatcher = ToolDispatcher(
                condition=condition,
                role="parent",
                workspace=parent_ws,
                run_id=run_id,
                batch_id=batch_id,
                soffice=soffice_bin,
                answer_position=answer_position,
            )

            # Seed parent workspace with candidate script so parent can run/modify it.
            # For A conditions, no candidate exists; parent writes from scratch.
            if candidate_script_content:
                parent_ws.solution_path.write_text(candidate_script_content, encoding="utf-8")

            # SOLO uses the summed solo budget; four-cell conditions use parent budget.
            active_budget = solo_budget if condition == "SOLO" else parent_budget

            parent_result = run_agent_phase(
                role="parent",
                condition=condition,
                client=client,
                dispatcher=parent_dispatcher,
                system_prompt=parent_system_prompt,
                initial_user_message=parent_user_msg,
                budget=active_budget,
                event_logger=event_logger,
                private_logger=None,   # parent has no private trace
            )

            phase_label = "solo" if condition == "SOLO" else "parent"
            event_logger.log("system", "phase_completed", {
                "phase": phase_label,
                "terminated_normally": parent_result.terminated_normally,
                "failure_code": parent_result.failure_code,
            })

            # ── 8. Persist only an event-confirmed parent submission ───────────────
            final_script_path: Path | None = None
            final_script_sha256: str | None = None
            unsealed_script_path: Path | None = None
            parent_submitted_solution = (
                "submit_solution"
                in _successful_submits(event_logger._path, "parent")
            )

            if parent_submitted_solution:
                if not parent_ws.solution_exists():
                    raise InfrastructureFailure(
                        "Parent submit_solution succeeded but solution.py "
                        "was absent at persistence time"
                    )
                dest_final = final_dir / "solution.py"
                shutil.copy2(parent_ws.solution_path, dest_final)
                final_script_path = dest_final
                final_script_sha256 = _sha256(dest_final)

                event_logger.log("system", "artifact_sealed", {
                    "artifact": "final_solution",
                    "sha256": final_script_sha256,
                })
            elif parent_ws.solution_exists():
                diagnostics_dir = run_dir / "diagnostics"
                diagnostics_dir.mkdir(parents=True, exist_ok=True)
                unsealed_script_path = diagnostics_dir / "unsealed_solution.py"
                shutil.copy2(parent_ws.solution_path, unsealed_script_path)

            # v0.4: capture parent observation call count before cleanup
            parent_observed_output_calls = parent_dispatcher.observed_output_call_count
            # v0.4.1: capture parent auto-observation count before cleanup
            parent_auto_observation_count = parent_dispatcher.auto_observation_count

        finally:
            parent_ws.cleanup()

        # ── 9. Offline candidate evaluation (P conditions only) ──────────────────
        # Evaluates the worker's candidate script from a clean input copy.
        # This runs AFTER the parent is sealed — parent never sees this result.
        worker_eval_result: dict[str, Any] | None = None

        if condition.startswith("P") and handoff_record["candidate_script_path"]:
            worker_eval_dir.mkdir(parents=True, exist_ok=True)
            _weval: dict[str, Any] = {
                "evaluator_status": "evaluator_failure",
                "full_task_success": None,
                "target_unit_pass_rate": None,
                "n_units": 0,
                "output_path": None,
                "evaluator_path": str(worker_eval_dir / "evaluator.json"),
            }
            cand_exec_ws = TaskWorkspace.create(
                run_id=f"{run_id}_worker_eval",
                task_id=task_id,
                source_input=public_input,
            )
            try:
                from .execute import run_in_sandbox
                exec_result = run_in_sandbox(
                    script_path=Path(handoff_record["candidate_script_path"]),
                    input_path=cand_exec_ws.input_path,
                    output_dir=cand_exec_ws.output_dir,
                    run_id=run_id,
                    role="worker_eval",
                    batch_id=batch_id,
                )
                if exec_result.output_path and exec_result.output_path.is_file():
                    recalc_file(exec_result.output_path)
                    dest_out = worker_eval_dir / "output.xlsx"
                    shutil.copy2(exec_result.output_path, dest_out)
                    _weval["output_sha256"] = _sha256(dest_out)
                    raw_eval = evaluate_task(
                        golden_path=golden_path,
                        output_path=dest_out,
                        answer_position=answer_position,
                        answer_sheet=answer_sheet,
                    )
                    _weval.update(raw_eval)
                    _weval["output_path"] = str(dest_out)
            finally:
                cand_exec_ws.cleanup()

            worker_eval_json = worker_eval_dir / "evaluator.json"
            worker_eval_json.write_text(
                json.dumps(_weval, indent=2) + "\n", encoding="utf-8"
            )
            _weval["evaluator_path"] = str(worker_eval_json)
            worker_eval_result = _weval

            event_logger.log("evaluator", "evaluation", {
                "phase": "worker",
                "evaluator_status": _weval["evaluator_status"],
                "n_units": _weval["n_units"],
            })

        # ── 10. Optional diagnostic evaluation of an unsealed last script ─────────
        unsealed_last_script_score: dict[str, Any] | None = None
        if unsealed_script_path is not None and unsealed_script_path.is_file():
            diagnostic = {
                "status": "evaluator_failure",
                "score": None,
                "n_units": 0,
            }
            diagnostic_ws = TaskWorkspace.create(
                run_id=f"{run_id}_unsealed_eval",
                task_id=task_id,
                source_input=public_input,
            )
            try:
                from .execute import run_in_sandbox
                exec_result = run_in_sandbox(
                    script_path=unsealed_script_path,
                    input_path=diagnostic_ws.input_path,
                    output_dir=diagnostic_ws.output_dir,
                    run_id=run_id,
                    role="unsealed_parent_eval",
                    batch_id=batch_id,
                )
                if exec_result.output_path and exec_result.output_path.is_file():
                    recalc_file(exec_result.output_path)
                    raw_eval = evaluate_task(
                        golden_path=golden_path,
                        output_path=exec_result.output_path,
                        answer_position=answer_position,
                        answer_sheet=answer_sheet,
                    )
                    diagnostic = {
                        "status": raw_eval["evaluator_status"],
                        "score": raw_eval["target_unit_pass_rate"],
                        "n_units": raw_eval["n_units"],
                    }
            finally:
                diagnostic_ws.cleanup()
            unsealed_last_script_score = diagnostic

        # ── 11. Primary final evaluation (successful submission only) ─────────────
        if parent_submitted_solution:
            final_eval_result: dict[str, Any] = {
                "evaluator_status": "evaluator_failure",
                "full_task_success": None,
                "target_unit_pass_rate": None,
                "n_units": 0,
                "output_path": None,
                "evaluator_path": str(final_dir / "evaluator.json"),
            }
        else:
            final_eval_result = {
                "evaluator_status": "not_finalized",
                "full_task_success": 0,
                "target_unit_pass_rate": 0.0,
                "n_units": 0,
                "output_path": None,
                "evaluator_path": str(final_dir / "evaluator.json"),
            }

        if parent_submitted_solution and final_script_path is not None and final_script_path.is_file():
            final_exec_ws = TaskWorkspace.create(
                run_id=f"{run_id}_final_eval",
                task_id=task_id,
                source_input=public_input,
            )
            try:
                from .execute import run_in_sandbox
                exec_result = run_in_sandbox(
                    script_path=final_script_path,
                    input_path=final_exec_ws.input_path,
                    output_dir=final_exec_ws.output_dir,
                    run_id=run_id,
                    role="final_eval",
                    batch_id=batch_id,
                )
                if exec_result.output_path and exec_result.output_path.is_file():
                    recalc_file(exec_result.output_path)
                    dest_out = final_dir / "output.xlsx"
                    shutil.copy2(exec_result.output_path, dest_out)
                    final_eval_result["output_sha256"] = _sha256(dest_out)
                    raw_eval = evaluate_task(
                        golden_path=golden_path,
                        output_path=dest_out,
                        answer_position=answer_position,
                        answer_sheet=answer_sheet,
                    )
                    final_eval_result.update(raw_eval)
                    final_eval_result["output_path"] = str(dest_out)
            finally:
                final_exec_ws.cleanup()

        final_eval_json = final_dir / "evaluator.json"
        final_eval_json.write_text(
            json.dumps(final_eval_result, indent=2) + "\n", encoding="utf-8"
        )
        final_eval_result["evaluator_path"] = str(final_eval_json)

        event_logger.log("evaluator", "evaluation", {
            "phase": "final",
            "evaluator_status": final_eval_result["evaluator_status"],
            "n_units": final_eval_result["n_units"],
        })

        # ── 12. Compute accepted_unchanged (sealed P finals only) ─────────────────
        accepted_unchanged: bool | None = None
        if (
            parent_submitted_solution
            and
            handoff_record["candidate_script_sha256"] is not None
            and final_script_sha256 is not None
        ):
            accepted_unchanged = (
                handoff_record["candidate_script_sha256"] == final_script_sha256
            )

        # ── 13. Aggregate usage ────────────────────────────────────────────────────
        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        episode_wall = time.monotonic() - t_episode_start

        tokens_by_role = client.tokens_by_role()
        empty_tokens = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

        # SOLO: no worker phase — worker usage is zeroed; solo agent logged as "parent".
        # Four-cell conditions: worker and parent usages populated from their phases.
        _zero_worker_usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "model_calls": 0,
            "tool_calls": 0,
            "validation_cycles": 0,
            "wall_seconds": 0.0,
            "budget_exhausted": False,
            "budget_exhausted_on": None,
            "safety_exhausted": False,
            "safety_exhausted_on": None,
            "observed_output_calls": 0,  # v0.4
            "auto_observations": 0,      # v0.4.1
        }

        if condition == "SOLO":
            worker_tokens = empty_tokens
            parent_tokens = tokens_by_role.get("parent", empty_tokens)
            worker_usage = _zero_worker_usage
            parent_usage: dict[str, Any] = {
                "input_tokens": parent_tokens["input_tokens"],
                "output_tokens": parent_tokens["output_tokens"],
                "cache_read_input_tokens": parent_tokens["cache_read_input_tokens"],
                "cache_creation_input_tokens": parent_tokens["cache_creation_input_tokens"],
                "model_calls": parent_result.model_calls,
                "tool_calls": parent_result.tool_calls,
                "validation_cycles": parent_result.validation_cycles,
                "wall_seconds": round(parent_result.wall_seconds, 3),
                "budget_exhausted": parent_result.budget_exhausted,
                "budget_exhausted_on": parent_result.budget_exhausted_on,
                "safety_exhausted": parent_result.safety_exhausted,
                "safety_exhausted_on": parent_result.safety_exhausted_on,
                # v0.4: observed_output_calls for SOLO parent
                "observed_output_calls": parent_observed_output_calls,
                # v0.4.1: automatic observations delivered with run tools
                "auto_observations": parent_auto_observation_count,
            }
        else:
            worker_tokens = tokens_by_role.get("worker", empty_tokens)
            parent_tokens = tokens_by_role.get("parent", empty_tokens)
            worker_usage = {
                "input_tokens": worker_tokens["input_tokens"],
                "output_tokens": worker_tokens["output_tokens"],
                "cache_read_input_tokens": worker_tokens["cache_read_input_tokens"],
                "cache_creation_input_tokens": worker_tokens["cache_creation_input_tokens"],
                "model_calls": worker_result.model_calls,
                "tool_calls": worker_result.tool_calls,
                "validation_cycles": worker_result.validation_cycles,
                "wall_seconds": round(worker_result.wall_seconds, 3),
                "budget_exhausted": worker_result.budget_exhausted,
                "budget_exhausted_on": worker_result.budget_exhausted_on,
                "safety_exhausted": worker_result.safety_exhausted,
                "safety_exhausted_on": worker_result.safety_exhausted_on,
                # v0.4: worker observed output calls (always 0 for A0/P0 by allowlist)
                "observed_output_calls": worker_observed_output_calls,
                # v0.4.1: automatic observations delivered with run tools
                "auto_observations": worker_auto_observation_count,
            }
            parent_usage = {
                "input_tokens": parent_tokens["input_tokens"],
                "output_tokens": parent_tokens["output_tokens"],
                "cache_read_input_tokens": parent_tokens["cache_read_input_tokens"],
                "cache_creation_input_tokens": parent_tokens["cache_creation_input_tokens"],
                "model_calls": parent_result.model_calls,
                "tool_calls": parent_result.tool_calls,
                "validation_cycles": parent_result.validation_cycles,
                "wall_seconds": round(parent_result.wall_seconds, 3),
                "budget_exhausted": parent_result.budget_exhausted,
                "budget_exhausted_on": parent_result.budget_exhausted_on,
                "safety_exhausted": parent_result.safety_exhausted,
                "safety_exhausted_on": parent_result.safety_exhausted_on,
                # v0.4: observed_output_calls for parent
                "observed_output_calls": parent_observed_output_calls,
                # v0.4.1: automatic observations delivered with run tools
                "auto_observations": parent_auto_observation_count,
            }

        usage_record = {"worker": worker_usage, "parent": parent_usage}

        # ── 14. Compliance ────────────────────────────────────────────────────────
        # worker_feedback_calls: actual count from dispatcher (not just validation_cycles)
        # worker_scratch_destroyed: True for A1 (verified), True for all others (no scratch created)
        # SOLO: no worker ever ran — worker_scratch_destroyed=True, worker_submitted=False,
        #   worker_feedback_calls=0, artifact_crossed_handoff=False.
        # v0.4.1: derive auto-observation compliance from actual events
        auto_obs_compliance = _compute_auto_obs_compliance(
            event_logger._path, condition
        )

        if condition == "SOLO":
            compliance = {
                "artifact_crossed_handoff": False,
                "worker_feedback_calls": 0,
                "parent_finalized": parent_submitted_solution,
                "worker_scratch_destroyed": True,  # no worker scratch ever existed
                "worker_submitted": False,
                # v0.4 observable feedback fields (SOLO has no worker)
                "worker_observed_output_calls": 0,
                "worker_revised_after_observation": False,
                # v0.4.1 auto-observation compliance
                "worker_auto_observations": 0,
                "parent_auto_observations": parent_auto_observation_count,
                **auto_obs_compliance,
            }
        else:
            compliance = {
                "artifact_crossed_handoff": handoff_record["candidate_script_sha256"] is not None,
                "worker_feedback_calls": worker_feedback_calls,
                "parent_finalized": parent_submitted_solution,
                "worker_scratch_destroyed": scratch_destroyed_at_handoff if condition == "A1" else True,
                "worker_submitted": (
                    worker_submitted_memo
                    and (not condition.startswith("P") or worker_submitted_candidate)
                ),
                # v0.4 observable feedback fields
                "worker_observed_output_calls": worker_observed_output_calls,
                "worker_revised_after_observation": worker_revised_after_observation,
                # v0.4.1 auto-observation compliance
                "worker_auto_observations": worker_auto_observation_count,
                "parent_auto_observations": parent_auto_observation_count,
                **auto_obs_compliance,
            }

        # ── 15. Failures ──────────────────────────────────────────────────────────
        failures: list[dict[str, Any]] = []
        # For SOLO, worker_result is None — skip worker failure check.
        if worker_result is not None:
            if not worker_result.terminated_normally and worker_result.failure_code:
                failures.append({
                    "source": "agent",
                    "code": worker_result.failure_code,
                    "message": worker_result.failure_message or "",
                })
        if not parent_result.terminated_normally and parent_result.failure_code:
            failures.append({
                "source": "agent",
                "code": parent_result.failure_code,
                "message": parent_result.failure_message or "",
            })

        # ── 16. Build episode record ──────────────────────────────────────────────
        # analysis_included:
        #   False for development/engineering (smoke only).
        #   True for pilot/main/hard/SOLO (structurally valid; filtered by task_set in analysis).
        #   Hard episodes are supplement-only and never pooled with main estimates.
        if task_set in ("development", "engineering"):
            analysis_included = False
            analysis_reason = (
                "Development-only engineering smoke; excluded from pilot and main analysis."
            )
        elif task_set == "hard":
            analysis_included = True
            analysis_reason = (
                "Hard-probe episode; supplement-only, reported in hard-probe table, "
                "never pooled with main estimates."
            )
        else:
            analysis_included = True
            analysis_reason = (
                f"Structurally valid {task_set} episode; inclusion confirmed post-run."
            )

        episode: dict[str, Any] = {
            "run_id": run_id,
            "task_id": task_id,
            "task_set": task_set,
            "condition": condition,
            "protocol_version": PROTOCOL_VERSION,
            "replicate": replicate,
            "started_at": started_at,
            "completed_at": completed_at,
            "reproducibility": {
                "schema_git_commit": schema_commit,
                "benchmark_git_commit": benchmark_commit,
                "dataset_archive_sha256": dataset_sha,
                "docker_image_digest": docker_digest,
                "worker_model": worker_model,
                "parent_model": parent_model,
                "temperature": temperature,
                "git_dirty": git_dirty,
                "workspace_header_used": client.workspace_header_used,
                "worker_prompt_hash": worker_prompt_hash,
                "parent_prompt_hash": parent_prompt_hash,
                "libreoffice_version": libreoffice_version,
                "task_selection_package": selection.package,
                "selection_sha256": selection.selection_sha256,
            },
            "budgets": {
                # SOLO: both slots hold the summed solo budget so the record is
                # self-consistent; the solo agent ran with solo_budget.
                # Four-cell: worker/parent hold their respective manifest budgets.
                "worker": solo_budget if condition == "SOLO" else worker_budget,
                "parent": solo_budget if condition == "SOLO" else parent_budget,
            },
            "usage": usage_record,
            "handoff": handoff_record,
            # A conditions: worker_outcome is null (no candidate to evaluate offline)
            # P conditions: populated from offline candidate evaluation above
            "worker_outcome": worker_eval_result,
            "final_outcome": final_eval_result,
            "unsealed_last_script_score": unsealed_last_script_score,
            "final_script_path": str(final_script_path) if final_script_path else None,
            "final_script_sha256": final_script_sha256,
            "accepted_unchanged": accepted_unchanged,
            "failures": failures,
            "compliance": compliance,
            "status": "complete",
            "analysis_included": analysis_included,
            "analysis_inclusion_reason": analysis_reason,
        }

        # ── 17. Validate + QC ────────────────────────────────────────────────────
        episode_path = run_dir / "episode.json"
        validate_issues = validate_episode(episode, episode_path)
        qc_flags = qc_episode(episode)

        # ── 18. Write episode.json atomically ────────────────────────────────────
        atomic_write_json(episode_path, episode)

        event_logger.log("system", "artifact_sealed", {
            "artifact": "episode.json",
            "run_id": run_id,
            "validation_issues": len(validate_issues),
            "qc_flags": len(qc_flags),
        })

        # Attach validation/QC results to the returned dict for callers
        episode["_meta"] = {
            "validate_issues": [str(i) for i in validate_issues],
            "qc_flags": [{"code": f.code, "message": f.message} for f in qc_flags],
            "run_dir": str(run_dir),
        }

        return episode
    except HandoffLost as exc:
        event_logger.log("system", "failure", {
            "source": "infrastructure",
            "code": "handoff_lost",
            "message": str(exc),
        })
        result = exc.worker_result  # type: ignore[attr-defined]
        worker_tokens = client.tokens_by_role().get(
            "worker",
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        )
        zero_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "model_calls": 0,
            "tool_calls": 0,
            "validation_cycles": 0,
            "wall_seconds": 0.0,
            "budget_exhausted": False,
            "budget_exhausted_on": None,
            "safety_exhausted": False,
            "safety_exhausted_on": None,
            "observed_output_calls": 0,  # v0.4
            "auto_observations": 0,      # v0.4.1
        }
        # v0.4: ensure memo_claims_validation is present in HandoffLost handoff record
        _exc_handoff = dict(exc.handoff_record)  # type: ignore[attr-defined]
        if "memo_claims_validation" not in _exc_handoff:
            _exc_handoff["memo_claims_validation"] = False
        episode = {
            "run_id": run_id,
            "task_id": task_id,
            "task_set": task_set,
            "condition": condition,
            "protocol_version": PROTOCOL_VERSION,
            "replicate": replicate,
            "started_at": started_at,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "reproducibility": {
                "schema_git_commit": schema_commit,
                "benchmark_git_commit": benchmark_commit,
                "dataset_archive_sha256": dataset_sha,
                "docker_image_digest": docker_digest,
                "worker_model": worker_model,
                "parent_model": parent_model,
                "temperature": temperature,
                "git_dirty": git_dirty,
                "workspace_header_used": client.workspace_header_used,
                "worker_prompt_hash": worker_prompt_hash,
                "parent_prompt_hash": parent_prompt_hash,
                "libreoffice_version": libreoffice_version,
                "task_selection_package": selection.package,
                "selection_sha256": selection.selection_sha256,
            },
            "budgets": {"worker": worker_budget, "parent": parent_budget},
            "usage": {
                "worker": {
                    "input_tokens": worker_tokens["input_tokens"],
                    "output_tokens": worker_tokens["output_tokens"],
                    "cache_read_input_tokens": worker_tokens["cache_read_input_tokens"],
                    "cache_creation_input_tokens": worker_tokens["cache_creation_input_tokens"],
                    "model_calls": result.model_calls,
                    "tool_calls": result.tool_calls,
                    "validation_cycles": result.validation_cycles,
                    "wall_seconds": round(result.wall_seconds, 3),
                    "budget_exhausted": result.budget_exhausted,
                    "budget_exhausted_on": result.budget_exhausted_on,
                    "safety_exhausted": result.safety_exhausted,
                    "safety_exhausted_on": result.safety_exhausted_on,
                    "observed_output_calls": getattr(exc, "worker_observed_output_calls", 0),
                    "auto_observations": getattr(exc, "worker_auto_observations", 0),
                },
                "parent": zero_usage,
            },
            "handoff": _exc_handoff,
            "worker_outcome": None,
            "final_outcome": None,
            "unsealed_last_script_score": None,
            "failures": [{
                "source": "infrastructure",
                "code": "handoff_lost",
                "message": str(exc),
            }],
            "compliance": {
                "artifact_crossed_handoff": (
                    exc.handoff_record["candidate_script_sha256"] is not None  # type: ignore[attr-defined]
                ),
                "worker_feedback_calls": exc.worker_feedback_calls,  # type: ignore[attr-defined]
                "parent_finalized": False,
                "worker_scratch_destroyed": (
                    exc.scratch_destroyed if condition == "A1" else True  # type: ignore[attr-defined]
                ),
                "worker_submitted": (
                    exc.worker_submitted_memo  # type: ignore[attr-defined]
                    and (
                        not condition.startswith("P")
                        or exc.worker_submitted_candidate  # type: ignore[attr-defined]
                    )
                ),
                # v0.4 observable feedback fields
                "worker_observed_output_calls": getattr(exc, "worker_observed_output_calls", 0),
                "worker_revised_after_observation": getattr(exc, "worker_revised_after_observation", False),
                # v0.4.1 auto-observation compliance (partial: handoff failed before parent ran)
                "worker_auto_observations": getattr(exc, "worker_auto_observations", 0),
                "parent_auto_observations": 0,
                "eligible_worker_run_events": 0,
                "eligible_parent_run_events": 0,
                "worker_runs_without_auto_observation": 0,
                "parent_runs_without_auto_observation": 0,
            },
            "status": "infrastructure_failure",
            "analysis_included": False,
            "analysis_inclusion_reason": "Infrastructure failure: handoff_lost.",
        }
        atomic_write_json(run_dir / "episode.json", episode)
        episode["_meta"] = {
            "validate_issues": [],
            "qc_flags": [],
            "run_dir": str(run_dir),
        }
        return episode
    except Exception:
        # Early-failure cleanup: remove the stub run directory only when no
        # model_call event was recorded (i.e. no API call succeeded yet).
        # Once any response is logged the directory is preserved so partial
        # traces remain available for post-hoc diagnosis.
        if _episode_client is None or not _episode_client.events:
            shutil.rmtree(run_dir, ignore_errors=True)
        raise


# ── CLI ────────────────────────────────────────────────────────────────────────

_TASK_SET_ALIASES = {"dev": "development"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a delegation-study episode for any condition (A0/A1/P0/P1/SOLO)."
    )
    parser.add_argument("--task-id", default="343-20", help="Task ID (default: 343-20)")
    parser.add_argument(
        "--condition",
        default="P1",
        choices=list(_ALLOWED_CONDITIONS),
        help="Condition (default: P1)",
    )
    parser.add_argument("--replicate", type=int, default=1, help="Replicate index")
    parser.add_argument("--run-id", default=None, help="Override auto UUID run_id")
    parser.add_argument(
        "--task-set",
        default="development",
        help=(
            "Collection tier (default: development). "
            "Alias 'dev' accepted. pilot/main/hard refuse dirty trees."
        ),
    )
    parser.add_argument(
        "--skip-stale-check",
        action="store_true",
        help=(
            "Skip the stale-container check. ONLY safe when an orchestrator "
            "performed the check before spawning this process as part of a "
            "declared concurrent batch."
        ),
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help=(
            "UUID shared by all concurrent conditions in the same run batch. "
            "Containers from the same batch are excluded from the stale check "
            "so four tmux panes can run simultaneously without blocking each "
            "other.  Generate once: python -c \"import uuid; print(uuid.uuid4())\""
        ),
    )
    args = parser.parse_args(argv)

    # Resolve task_set alias
    task_set = _TASK_SET_ALIASES.get(args.task_set, args.task_set)
    if task_set not in _ALLOWED_TASK_SETS:
        print(
            f"ERROR: --task-set must be one of {list(_ALLOWED_TASK_SETS) + list(_TASK_SET_ALIASES)} "
            f"(got {args.task_set!r})",
            file=sys.stderr,
        )
        return 1

    try:
        episode = run_episode(
            task_id=args.task_id,
            condition=args.condition,
            replicate=args.replicate,
            task_set=task_set,
            run_id=args.run_id,
            skip_stale_check=args.skip_stale_check,
            batch_id=args.batch_id,
        )
    except DirtyTreeError as exc:
        print(f"DIRTY TREE REFUSAL:\n{exc}", file=sys.stderr)
        return 3
    except PreflightError as exc:
        print(f"PREFLIGHT FAILED:\n{exc}", file=sys.stderr)
        return 2
    except RunDirectoryCollisionError as exc:
        print(f"RUN COLLISION:\n{exc}", file=sys.stderr)
        return 4

    meta = episode.pop("_meta", {})
    run_dir = meta.get("run_dir", "unknown")

    print(f"\n{'='*60}")
    print(f"Episode complete: run_id={episode['run_id']}")
    print(f"Condition: {episode['condition']}  Task: {episode['task_id']}")
    print(f"Run directory: {run_dir}")

    if episode.get("worker_outcome"):
        wo = episode["worker_outcome"]
        print(
            f"Worker outcome:  {wo['evaluator_status']} "
            f"full={wo['full_task_success']} "
            f"rate={wo['target_unit_pass_rate']}"
        )
    else:
        print("Worker outcome:  null (advisory condition)")

    fo = episode["final_outcome"]
    print(
        f"Final outcome:   {fo['evaluator_status']} "
        f"full={fo['full_task_success']} "
        f"rate={fo['target_unit_pass_rate']}"
    )
    print(f"Accepted unchanged: {episode['accepted_unchanged']}")

    worker_u = episode["usage"]["worker"]
    parent_u = episode["usage"]["parent"]
    print(
        f"Worker usage: {worker_u['model_calls']} calls, "
        f"{worker_u['input_tokens']+worker_u['output_tokens']} tokens, "
        f"{worker_u['tool_calls']} tools, "
        f"{worker_u['validation_cycles']} validation cycles"
    )
    print(
        f"Parent usage: {parent_u['model_calls']} calls, "
        f"{parent_u['input_tokens']+parent_u['output_tokens']} tokens, "
        f"{parent_u['tool_calls']} tools, "
        f"{parent_u['validation_cycles']} validation cycles"
    )

    v_issues = meta.get("validate_issues", [])
    qc_flags = meta.get("qc_flags", [])
    print(f"Validation issues: {len(v_issues)}")
    for issue in v_issues:
        print(f"  ISSUE: {issue}")
    print(f"QC flags: {len(qc_flags)}")
    for flag in qc_flags:
        print(f"  FLAG [{flag['code']}]: {flag['message']}")
    print("=" * 60)

    return 0 if not v_issues else 1


if __name__ == "__main__":
    sys.exit(main())
