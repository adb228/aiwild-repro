"""Parent-only fixed-artifact replay for SpreadsheetBench.

Standalone CLI (no edit to run.py / tools.py / agent_loop.py):

    python -m delegation_study.spreadsheetbench.parent_replay \\
        --source-run-id <main_v04 P cell> --arm evidence_withheld

Parent system prompt, tools, and budget are imported from the live
``main_v04`` runner so they stay byte-identical.  The parent never sees
hidden data, gold, scores, or source traces.

See ``config/study/decisions.md`` entry 2026-09-05T02:00:00-04:00.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from ..agent_loop import run_agent_phase
from ..atomic import atomic_write_json
from ..evaluator import evaluate_task
from ..model_client import ModelClient
from ..qc import qc_episode
from ..recalc import recalc_file
from ..records import EventLogger
from ..task_selection import load_spreadsheet_selection
from ..validate import (
    ValidationIssue,
    _analysis_inclusion_checks,
    _artifact_checks,
    _limit_exhaustion_checks,
    _load_schema,
    _outcome_checks,
)
from .execute import check_stale_containers, run_in_sandbox
from .preflight import PreflightError, run_preflight
from .run import (
    PROTOCOL_VERSION,
    DirtyTreeError,
    InfrastructureFailure,
    RunDirectoryCollisionError,
    _CONFIG,
    _ROOT,
    _RUNS,
    _compute_auto_obs_compliance,
    _compute_git_dirty,
    _find_hidden_golden,
    _find_public_input,
    _load_task,
    _parent_system_prompt,
    _parent_user_message_production,
    _prompt_sha256,
    _sha256,
    _successful_submits,
)
from .tools import ToolDispatcher
from .workspace import TaskWorkspace

# ── Design constants (locked in decisions.md 2026-09-05T02:00:00-04:00) ────

TASK_SET = "fixed_artifact"
ARM_WITHHELD = "evidence_withheld"
ARM_SUPPLIED = "evidence_supplied"
ARMS = (ARM_WITHHELD, ARM_SUPPLIED)
DESIGN_SEED = 20260905
BOOTSTRAP_SEED = 20260902
BOOTSTRAP_DRAWS = 10_000
ALPHA = 0.05
HARD_GATE = datetime.datetime(2026, 9, 5, 14, 0, 0, tzinfo=ZoneInfo("America/New_York"))
MAIN_V04_PARENT_PROMPT_HASH = (
    "0193f4a73fd2c1c837363dd75e890b03e1fba21f221e043f33b5d968c76ef209"
)
REPLICATE_FOR_ARM = {ARM_WITHHELD: 1, ARM_SUPPLIED: 2}
VERIFY_TOOLS = frozenset({"run_solution", "read_output_range"})
DROPPED_MEMO_KEYS = frozenset({"observed_feedback", "validation_status"})
CANONICAL_MEMO_KEYS = (
    "task_understanding",
    "target_locations",
    "proposed_approach",
    "assumptions",
    "known_risks",
    "validation_plan",
)
# Prefix stems plus whole-word ran/running. ``range`` is not a match.
_VERIFY_SENTENCE_RE = re.compile(
    r"(?i)(?:\b(?:test|verif|check|confirm|observ)\w*|\bran\b|\brunning\b)"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
EVIDENCE_HEADER = (
    "== Execution output of the candidate ==\n"
    "The following is the execution output of the candidate for the graded "
    "answer_position. It is not a correctness judgment.\n"
)
_FORBIDDEN_PROMPT_PATHS = (
    "/data/hidden/",
    "/data/raw/",
    "/.env",
    "data/hidden",
    "data/raw",
)
_JUDGMENT_RE = re.compile(
    r"(?i)\b(?:correct(?:ness)?|incorrect|pass(?:ed)?|fail(?:ed)?|"
    r"gold(?:en)?|score|success(?:ful)?)\b"
)

_BATCHES = _ROOT / "data" / "batches"
_DEV = _ROOT / "data" / "dev"


# ── Small helpers ─────────────────────────────────────────────────────────────

def _now_et() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo("America/New_York"))


def gate_has_passed(now: datetime.datetime | None = None) -> bool:
    current = now if now is not None else _now_et()
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("America/New_York"))
    return current >= HARD_GATE


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _zero_usage() -> dict[str, Any]:
    return {
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
        "observed_output_calls": 0,
        "auto_observations": 0,
    }


# ── Sanitizer (deterministic, correctness-blind) ──────────────────────────────

def _sanitize_sentence_text(text: str) -> str:
    """Drop sentences whose text matches the locked verification stems."""
    if not text:
        return text
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    kept = [part.strip() for part in parts if part.strip() and not _VERIFY_SENTENCE_RE.search(part)]
    return " ".join(kept)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_sentence_text(value)
    if isinstance(value, list):
        cleaned: list[Any] = []
        for item in value:
            sanitized_item = _sanitize_value(item)
            if isinstance(sanitized_item, str) and not sanitized_item.strip():
                continue
            cleaned.append(sanitized_item)
        return cleaned
    if isinstance(value, dict):
        return sanitize_memo(value)
    return value


def sanitize_memo(memo: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized memo dict. Blind to correctness; no score inputs.

    Drops ``observed_feedback`` and ``validation_status``.  Remaining string
    fields lose any sentence matching the locked stems.  Key order is
    canonical so ``json.dumps(..., indent=2)`` is byte-stable across arms.
    """
    if not isinstance(memo, dict):
        raise TypeError("memo must be a dict")
    out: dict[str, Any] = {}
    for key in CANONICAL_MEMO_KEYS:
        if key in DROPPED_MEMO_KEYS:
            continue
        if key in memo:
            out[key] = _sanitize_value(memo[key])
    extra_keys = sorted(
        k for k in memo.keys()
        if k not in CANONICAL_MEMO_KEYS and k not in DROPPED_MEMO_KEYS
    )
    for key in extra_keys:
        out[key] = _sanitize_value(memo[key])
    return out


def memo_canonical_bytes(memo: dict[str, Any]) -> bytes:
    """Canonical on-disk memo bytes (sanitizer output persistence)."""
    return (json.dumps(memo, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# ── Evidence block (host-only; never a correctness judgment) ──────────────────

def format_evidence_block(
    *,
    answer_position: str,
    script_exit_code: int | None,
    timed_out: bool,
    output_workbook_present: bool,
    range_payload: dict[str, Any] | None,
) -> str:
    """Format the one standardized evidence block appended to the supplied arm."""
    payload = range_payload if isinstance(range_payload, dict) else {}
    range_error = payload.get("error")
    body = {
        "answer_position": answer_position,
        "script_exit_code": script_exit_code,
        "timed_out": bool(timed_out),
        "output_workbook_present": bool(output_workbook_present),
        "sheet": payload.get("sheet"),
        "range": payload.get("range"),
        "recalculated_values": payload.get("values"),
        "formulas": payload.get("formulas"),
        "error_cells": payload.get("errors"),
        "recalc_engine": payload.get("recalc_engine"),
        "range_read_note": range_error,
    }
    rendered = json.dumps(body, indent=2, ensure_ascii=False)
    if _JUDGMENT_RE.search(rendered):
        # Host-side guard: never ship judgment vocabulary even if a cell value
        # happens to contain one of those words — replace the matching token
        # in *labels only* by omitting free-text notes that tripped the guard.
        body["range_read_note"] = None
        rendered = json.dumps(body, indent=2, ensure_ascii=False)
    return EVIDENCE_HEADER + "\n" + rendered + "\n"


def generate_evidence_block(
    *,
    candidate_script: str,
    public_input: Path,
    answer_position: str,
    task_id: str,
    run_id: str,
    soffice: str,
    batch_id: str | None = None,
) -> str:
    """Run the candidate on a clean workbook and read ``answer_position``.

    Uses the host ``ToolDispatcher.read_output_range`` implementation.
    Temporary workspace is destroyed before return.  Parent never sees it.
    """
    ws = TaskWorkspace.create(
        run_id=f"{run_id}_fa_evidence",
        task_id=task_id,
        source_input=public_input,
    )
    try:
        dispatcher = ToolDispatcher(
            condition="P1",
            role="parent",
            workspace=ws,
            run_id=run_id,
            batch_id=batch_id,
            soffice=soffice,
            answer_position=answer_position,
        )
        dispatcher.call("write_solution", code=candidate_script)
        run_payload = dispatcher.call("run_solution")
        present = bool(run_payload.get("output_path")) and ws.output_exists()
        range_payload: dict[str, Any]
        if present:
            range_payload = dispatcher.call("read_output_range", range_ref=answer_position)
        else:
            range_payload = {"error": "no output workbook yet; run your solution first"}
        return format_evidence_block(
            answer_position=answer_position,
            script_exit_code=run_payload.get("exit_code"),
            timed_out=bool(run_payload.get("timed_out")),
            output_workbook_present=present,
            range_payload=range_payload,
        )
    finally:
        ws.cleanup()


# ── Prompt assembly ───────────────────────────────────────────────────────────

def compose_parent_user_message(
    task: dict[str, Any],
    sanitized_memo: dict[str, Any],
    candidate_script: str,
    *,
    arm: str,
    evidence_block: str | None,
    libreoffice_version: str,
) -> tuple[str, str]:
    """Return ``(full_message, core_message)``.

    ``core_message`` is byte-identical across arms.  The supplied arm appends
    exactly one evidence block; the withheld arm is the core message.
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    core = _parent_user_message_production(
        task, sanitized_memo, candidate_script, libreoffice_version
    )
    if arm == ARM_SUPPLIED:
        if not evidence_block:
            raise ValueError("evidence_supplied arm requires an evidence block")
        return core + "\n\n" + evidence_block, core
    return core, core


def assert_prompt_has_no_hidden(text: str) -> None:
    lowered = text.lower()
    for needle in _FORBIDDEN_PROMPT_PATHS:
        if needle.lower() in lowered:
            raise RuntimeError(f"refusing to send parent prompt containing {needle!r}")


# ── FA schema (local extension; frozen contracts/ are not edited) ─────────────

_FA_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "arm",
        "source_run_id",
        "source_task_id",
        "source_condition",
        "source_task_set",
        "design_seed",
        "sanitized_memo_sha256",
        "core_user_message_sha256",
        "full_user_message_sha256",
        "parent_system_prompt_sha256",
        "correctness_enriched",
        "pair_id",
    ],
    "properties": {
        "arm": {"enum": [ARM_WITHHELD, ARM_SUPPLIED]},
        "source_run_id": {"type": "string", "minLength": 1},
        "source_task_id": {"type": "string", "minLength": 1},
        "source_condition": {"enum": ["P0", "P1"]},
        "source_task_set": {"const": "main_v04"},
        "candidate_correct": {"type": ["boolean", "null"]},
        "design_seed": {"type": "integer"},
        "arm_order": {
            "type": "array",
            "items": {"enum": [ARM_WITHHELD, ARM_SUPPLIED]},
            "minItems": 2,
            "maxItems": 2,
        },
        "sanitized_memo_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "evidence_block_sha256": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"},
        "core_user_message_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "full_user_message_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "parent_system_prompt_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "correctness_enriched": {"type": "boolean"},
        "pair_id": {"type": "string", "minLength": 1},
        "parent_verify_actions": {"type": "integer", "minimum": 0},
    },
}


def fa_episode_schema() -> dict[str, Any]:
    schema = copy.deepcopy(_load_schema("episode_schema.json"))
    enum = list(schema["properties"]["task_set"]["enum"])
    if TASK_SET not in enum:
        enum.append(TASK_SET)
    schema["properties"]["task_set"]["enum"] = enum
    schema["properties"]["fixed_artifact"] = _FA_OBJECT_SCHEMA
    required = list(schema.get("required", []))
    if "fixed_artifact" not in required:
        required.append("fixed_artifact")
    schema["required"] = required
    return schema


def validate_fa_episode(record: dict[str, Any], record_path: Path) -> list[ValidationIssue]:
    validator = Draft202012Validator(fa_episode_schema(), format_checker=FormatChecker())
    issues = [
        ValidationIssue(
            ".".join(str(part) for part in error.absolute_path) or "$",
            error.message,
        )
        for error in validator.iter_errors(record)
    ]
    issues.extend(_artifact_checks(record, record_path))
    issues.extend(_outcome_checks(record))
    issues.extend(_limit_exhaustion_checks(record))
    issues.extend(_analysis_inclusion_checks(record))
    fa = record.get("fixed_artifact") or {}
    if record.get("task_set") != TASK_SET:
        issues.append(ValidationIssue("task_set", f"must be {TASK_SET!r}"))
    if fa.get("arm") not in ARMS:
        issues.append(ValidationIssue("fixed_artifact.arm", "missing or unknown arm"))
    if not fa.get("source_run_id"):
        issues.append(ValidationIssue("fixed_artifact.source_run_id", "missing"))
    expected_rep = REPLICATE_FOR_ARM.get(fa.get("arm", ""), None)
    if expected_rep is not None and record.get("replicate") != expected_rep:
        issues.append(ValidationIssue(
            "replicate",
            f"must be {expected_rep} for arm {fa.get('arm')}",
        ))
    return sorted(issues, key=lambda issue: (issue.path, issue.message))


# ── Candidate discovery (read-only over main_v04 P cells) ─────────────────────

@dataclass(frozen=True)
class Candidate:
    source_run_id: str
    task_id: str
    source_condition: str
    source_dir: Path
    memo_path: Path
    solution_path: Path
    candidate_correct: bool | None
    worker_eval_dir: Path | None


def discover_main_v04_p_candidates(runs_dir: Path | None = None) -> list[Candidate]:
    """Read-only scan of completed main_v04 P0/P1 cells."""
    base = runs_dir if runs_dir is not None else _RUNS
    found: list[Candidate] = []
    if not base.is_dir():
        return found
    for run_dir in sorted(base.iterdir()):
        ep_path = run_dir / "episode.json"
        if not ep_path.is_file():
            continue
        try:
            rec = json.loads(ep_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("task_set") != "main_v04":
            continue
        if rec.get("condition") not in {"P0", "P1"}:
            continue
        if rec.get("status") != "complete":
            continue
        if rec.get("protocol_version") != PROTOCOL_VERSION:
            continue
        memo_path = run_dir / "handoff" / "memo.json"
        sol_path = run_dir / "handoff" / "solution.py"
        if not memo_path.is_file() or not sol_path.is_file():
            continue
        wo = rec.get("worker_outcome") or {}
        raw_ok = wo.get("full_task_success")
        candidate_correct: bool | None
        if raw_ok == 1:
            candidate_correct = True
        elif raw_ok == 0:
            candidate_correct = False
        else:
            candidate_correct = None
        weval = run_dir / "worker_eval"
        found.append(Candidate(
            source_run_id=str(rec["run_id"]),
            task_id=str(rec["task_id"]),
            source_condition=str(rec["condition"]),
            source_dir=run_dir,
            memo_path=memo_path,
            solution_path=sol_path,
            candidate_correct=candidate_correct,
            worker_eval_dir=weval if weval.is_dir() else None,
        ))
    found.sort(key=lambda c: (c.task_id, c.source_condition, c.source_run_id))
    return found


def assign_arm_order(source_run_id: str, seed: int = DESIGN_SEED) -> list[str]:
    """Deterministic per-candidate permutation of the two arms."""
    import random

    rng = random.Random(f"{seed}:{source_run_id}")
    order = list(ARMS)
    rng.shuffle(order)
    return order


def select_smoke_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """First incorrect and first correct in sorted candidate order."""
    first_correct = next((c for c in candidates if c.candidate_correct is True), None)
    first_incorrect = next((c for c in candidates if c.candidate_correct is False), None)
    chosen = [c for c in (first_correct, first_incorrect) if c is not None]
    if len(chosen) < 2:
        chosen = candidates[:2]
    return chosen


def select_enriched_candidates(candidates: list[Candidate], seed: int = DESIGN_SEED) -> list[Candidate]:
    """All incorrect candidates plus an equal seeded sample of correct ones."""
    import random

    incorrect = [c for c in candidates if c.candidate_correct is False]
    correct = [c for c in candidates if c.candidate_correct is True]
    rng = random.Random(seed)
    n = len(incorrect)
    sampled_correct = sorted(rng.sample(correct, k=min(n, len(correct))),
                             key=lambda c: (c.task_id, c.source_condition))
    return sorted(incorrect + sampled_correct, key=lambda c: (c.task_id, c.source_condition))


# ── Event-derived primary outcome ─────────────────────────────────────────────

def count_parent_verify_actions(events_path: Path) -> int:
    """Primary: parent run_solution + explicit read_output_range tool_results."""
    if not events_path.is_file():
        return 0
    count = 0
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("role") != "parent" or ev.get("event_type") != "tool_result":
            continue
        if ev.get("payload", {}).get("tool_name") in VERIFY_TOOLS:
            count += 1
    return count


# ── Episode runner ────────────────────────────────────────────────────────────

def _copy_source_worker_eval(
    source_dir: Path,
    dest_dir: Path,
) -> dict[str, Any] | None:
    """Copy existing candidate-eval artifacts; rewrite paths into dest_dir."""
    src_eval = source_dir / "worker_eval" / "evaluator.json"
    src_out = source_dir / "worker_eval" / "output.xlsx"
    if not src_eval.is_file():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_eval = dest_dir / "evaluator.json"
    raw = json.loads(src_eval.read_text(encoding="utf-8"))
    dest_out: Path | None = None
    if src_out.is_file():
        dest_out = dest_dir / "output.xlsx"
        shutil.copy2(src_out, dest_out)
        raw["output_path"] = str(dest_out)
        raw["output_sha256"] = _sha256(dest_out)
    else:
        raw["output_path"] = None
    raw["evaluator_path"] = str(dest_eval)
    dest_eval.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return raw


def _evaluate_script(
    *,
    script_path: Path,
    public_input: Path,
    golden_path: Path,
    answer_position: str,
    answer_sheet: str | None,
    dest_dir: Path,
    run_id: str,
    role: str,
    batch_id: str | None,
) -> dict[str, Any]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "evaluator_status": "evaluator_failure",
        "full_task_success": None,
        "target_unit_pass_rate": None,
        "n_units": 0,
        "output_path": None,
        "evaluator_path": str(dest_dir / "evaluator.json"),
    }
    eval_ws = TaskWorkspace.create(
        run_id=f"{run_id}_{role}",
        task_id="fa_eval",
        source_input=public_input,
    )
    try:
        exec_result = run_in_sandbox(
            script_path=script_path,
            input_path=eval_ws.input_path,
            output_dir=eval_ws.output_dir,
            run_id=run_id,
            role=role,
            batch_id=batch_id,
        )
        if exec_result.output_path and exec_result.output_path.is_file():
            recalc_file(exec_result.output_path)
            dest_out = dest_dir / "output.xlsx"
            shutil.copy2(exec_result.output_path, dest_out)
            result["output_sha256"] = _sha256(dest_out)
            raw_eval = evaluate_task(
                golden_path=golden_path,
                output_path=dest_out,
                answer_position=answer_position,
                answer_sheet=answer_sheet,
            )
            result.update(raw_eval)
            result["output_path"] = str(dest_out)
    finally:
        eval_ws.cleanup()
    eval_json = dest_dir / "evaluator.json"
    eval_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    result["evaluator_path"] = str(eval_json)
    return result


def run_fa_episode(
    *,
    source_run_id: str,
    arm: str,
    run_id: str | None = None,
    batch_id: str | None = None,
    runs_dir: Path | None = None,
    skip_preflight: bool = False,
    skip_stale_check: bool = False,
    skip_clean_tree: bool = False,
    correctness_enriched: bool = False,
    evidence_block_override: str | None = None,
    client: ModelClient | None = None,
) -> dict[str, Any]:
    """Run one parent-only FA episode for a frozen main_v04 P-cell candidate."""
    if arm not in ARMS:
        raise ValueError(f"arm must be one of {ARMS}; got {arm!r}")

    candidates = {c.source_run_id: c for c in discover_main_v04_p_candidates(runs_dir)}
    candidate = candidates.get(source_run_id)
    if candidate is None:
        raise ValueError(
            f"source_run_id {source_run_id!r} is not a completed main_v04 P cell"
        )

    t_episode_start = time.monotonic()
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    git_dirty_val = _compute_git_dirty(_ROOT)
    if not skip_clean_tree:
        if git_dirty_val is None:
            raise DirtyTreeError(
                "Cannot determine git tree state; refusing fixed_artifact episode."
            )
        if git_dirty_val:
            raise DirtyTreeError(
                "Git tree is dirty; refusing fixed_artifact episode. "
                "Commit FA module/tests/decisions before launch."
            )
    git_dirty = bool(git_dirty_val) if git_dirty_val is not None else False

    if not skip_stale_check:
        check_stale_containers(batch_id=batch_id)
    if not skip_preflight:
        run_preflight()

    with _CONFIG.open() as fh:
        manifest = yaml.safe_load(fh)

    task = _load_task(candidate.task_id)
    answer_position: str = task["answer_position"]
    answer_sheet: str | None = task.get("answer_sheet")
    init_files: list[str] = task.get("init_files", [])
    public_input = _find_public_input(candidate.task_id, init_files)
    # Golden is loaded only for post-parent final evaluation. Never prompted.
    golden_path = _find_hidden_golden(candidate.task_id)

    run_id = run_id or str(uuid.uuid4())
    base_runs = runs_dir if runs_dir is not None else _RUNS
    run_dir = base_runs / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise RunDirectoryCollisionError(
            f"Run directory already exists: {run_dir}. run_id={run_id!r}"
        )

    _episode_client: ModelClient | None = None
    try:
        handoff_dir = run_dir / "handoff"
        worker_eval_dir = run_dir / "worker_eval"
        final_dir = run_dir / "final"
        logs_dir = run_dir / "logs"
        for d in (handoff_dir, final_dir, logs_dir):
            d.mkdir(parents=True, exist_ok=True)

        event_logger = EventLogger(run_dir / "events.jsonl", run_id)
        client = client or ModelClient()
        _episode_client = client

        try:
            import subprocess
            schema_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True
            ).strip()
        except Exception:
            schema_commit = "uncommitted"

        parent_model = manifest["models"]["parent"]
        worker_model = manifest["models"]["worker"]
        temperature = manifest["models"]["temperature"]
        worker_budget = manifest["budgets"]["worker"]
        parent_budget = manifest["budgets"]["parent"]
        libreoffice_version = str(manifest["recalculation"]["libreoffice_version"])
        soffice_bin = manifest["recalculation"]["executable"]
        parent_system_prompt = _parent_system_prompt()
        parent_prompt_hash = _prompt_sha256(parent_system_prompt)
        if parent_prompt_hash != MAIN_V04_PARENT_PROMPT_HASH:
            raise RuntimeError(
                "parent system prompt hash drifted from main_v04 "
                f"({parent_prompt_hash} != {MAIN_V04_PARENT_PROMPT_HASH})"
            )
        selection = load_spreadsheet_selection(_CONFIG)

        raw_memo = json.loads(candidate.memo_path.read_text(encoding="utf-8"))
        sanitized_memo = sanitize_memo(raw_memo)
        sanitized_again = sanitize_memo(sanitized_memo)
        if memo_canonical_bytes(sanitized_memo) != memo_canonical_bytes(sanitized_again):
            raise RuntimeError("sanitizer is not idempotent")
        candidate_script = candidate.solution_path.read_text(encoding="utf-8")

        dest_memo = handoff_dir / "memo.json"
        dest_sol = handoff_dir / "solution.py"
        dest_memo.write_bytes(memo_canonical_bytes(sanitized_memo))
        shutil.copy2(candidate.solution_path, dest_sol)

        handoff_record: dict[str, Any] = {
            "memo_path": str(dest_memo),
            "memo_sha256": _sha256(dest_memo),
            "candidate_script_path": str(dest_sol),
            "candidate_script_sha256": _sha256(dest_sol),
            "worker_submitted_memo": True,
            "worker_submitted_candidate": True,
            "memo_claims_validation": False,
        }

        evidence_block: str | None = None
        if arm == ARM_SUPPLIED:
            evidence_block = evidence_block_override or generate_evidence_block(
                candidate_script=candidate_script,
                public_input=public_input,
                answer_position=answer_position,
                task_id=candidate.task_id,
                run_id=run_id,
                soffice=soffice_bin,
                batch_id=batch_id,
            )
            if _JUDGMENT_RE.search(evidence_block.split("\n", 2)[0]):
                raise RuntimeError("evidence header must not contain a correctness judgment")

        full_msg, core_msg = compose_parent_user_message(
            task,
            sanitized_memo,
            candidate_script,
            arm=arm,
            evidence_block=evidence_block,
            libreoffice_version=libreoffice_version,
        )
        assert_prompt_has_no_hidden(parent_system_prompt)
        assert_prompt_has_no_hidden(full_msg)
        if ARM_WITHHELD and core_msg != compose_parent_user_message(
            task, sanitized_memo, candidate_script, arm=ARM_WITHHELD,
            evidence_block=None, libreoffice_version=libreoffice_version,
        )[1]:
            raise RuntimeError("core user message is not arm-stable")

        fa_meta: dict[str, Any] = {
            "arm": arm,
            "source_run_id": candidate.source_run_id,
            "source_task_id": candidate.task_id,
            "source_condition": candidate.source_condition,
            "source_task_set": "main_v04",
            "candidate_correct": candidate.candidate_correct,
            "design_seed": DESIGN_SEED,
            "arm_order": assign_arm_order(candidate.source_run_id, DESIGN_SEED),
            "sanitized_memo_sha256": _sha256(dest_memo),
            "evidence_block_sha256": _sha256_text(evidence_block) if evidence_block else None,
            "core_user_message_sha256": _sha256_text(core_msg),
            "full_user_message_sha256": _sha256_text(full_msg),
            "parent_system_prompt_sha256": parent_prompt_hash,
            "correctness_enriched": bool(correctness_enriched),
            "pair_id": candidate.source_run_id,
        }

        event_logger.log("system", "phase_started", {
            "phase": "parent",
            "task_id": candidate.task_id,
            "task_set": TASK_SET,
            "fa_arm": arm,
            "source_run_id": candidate.source_run_id,
        })

        parent_observed_output_calls = 0
        parent_auto_observation_count = 0
        parent_ws = TaskWorkspace.create(
            run_id=f"{run_id}_parent",
            task_id=candidate.task_id,
            source_input=public_input,
        )
        try:
            parent_dispatcher = ToolDispatcher(
                condition=candidate.source_condition,
                role="parent",
                workspace=parent_ws,
                run_id=run_id,
                batch_id=batch_id,
                soffice=soffice_bin,
                answer_position=answer_position,
            )
            parent_ws.solution_path.write_text(candidate_script, encoding="utf-8")
            parent_result = run_agent_phase(
                role="parent",
                condition=candidate.source_condition,
                client=client,
                dispatcher=parent_dispatcher,
                system_prompt=parent_system_prompt,
                initial_user_message=full_msg,
                budget=parent_budget,
                event_logger=event_logger,
                private_logger=None,
            )
            event_logger.log("system", "phase_completed", {
                "phase": "parent",
                "terminated_normally": parent_result.terminated_normally,
                "failure_code": parent_result.failure_code,
            })

            final_script_path: Path | None = None
            final_script_sha256: str | None = None
            unsealed_script_path: Path | None = None
            parent_submitted_solution = (
                "submit_solution" in _successful_submits(event_logger._path, "parent")
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

            parent_observed_output_calls = parent_dispatcher.observed_output_call_count
            parent_auto_observation_count = parent_dispatcher.auto_observation_count
        finally:
            parent_ws.cleanup()

        # Offline candidate evaluation AFTER the parent is sealed.
        worker_eval_result = _copy_source_worker_eval(candidate.source_dir, worker_eval_dir)
        if worker_eval_result is None:
            worker_eval_result = _evaluate_script(
                script_path=dest_sol,
                public_input=public_input,
                golden_path=golden_path,
                answer_position=answer_position,
                answer_sheet=answer_sheet,
                dest_dir=worker_eval_dir,
                run_id=run_id,
                role="worker_eval",
                batch_id=batch_id,
            )
        event_logger.log("evaluator", "evaluation", {
            "phase": "worker",
            "evaluator_status": worker_eval_result["evaluator_status"],
            "n_units": worker_eval_result["n_units"],
        })

        unsealed_last_script_score: dict[str, Any] | None = None
        if unsealed_script_path is not None and unsealed_script_path.is_file():
            diagnostic = {"status": "evaluator_failure", "score": None, "n_units": 0}
            diagnostic_ws = TaskWorkspace.create(
                run_id=f"{run_id}_unsealed_eval",
                task_id=candidate.task_id,
                source_input=public_input,
            )
            try:
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
            final_eval_result = _evaluate_script(
                script_path=final_script_path,
                public_input=public_input,
                golden_path=golden_path,
                answer_position=answer_position,
                answer_sheet=answer_sheet,
                dest_dir=final_dir,
                run_id=run_id,
                role="final_eval",
                batch_id=batch_id,
            )
        else:
            # Mirror run.py: persist the not_finalized / unevaluated sidecar
            # so artifact checks have a file even when the parent never sealed.
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

        accepted_unchanged: bool | None = None
        if (
            parent_submitted_solution
            and handoff_record["candidate_script_sha256"] is not None
            and final_script_sha256 is not None
        ):
            accepted_unchanged = (
                handoff_record["candidate_script_sha256"] == final_script_sha256
            )

        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        tokens_by_role = client.tokens_by_role()
        empty_tokens = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        parent_tokens = tokens_by_role.get("parent", empty_tokens)
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
            "observed_output_calls": parent_observed_output_calls,
            "auto_observations": parent_auto_observation_count,
        }
        auto_obs_compliance = _compute_auto_obs_compliance(event_logger._path, candidate.source_condition)
        verify_actions = count_parent_verify_actions(event_logger._path)
        fa_meta["parent_verify_actions"] = verify_actions

        failures: list[dict[str, Any]] = []
        if not parent_result.terminated_normally and parent_result.failure_code:
            failures.append({
                "source": "agent",
                "code": parent_result.failure_code,
                "message": parent_result.failure_message or "",
            })

        episode: dict[str, Any] = {
            "run_id": run_id,
            "task_id": candidate.task_id,
            "task_set": TASK_SET,
            "condition": candidate.source_condition,
            "protocol_version": PROTOCOL_VERSION,
            "replicate": REPLICATE_FOR_ARM[arm],
            "started_at": started_at,
            "completed_at": completed_at,
            "reproducibility": {
                "schema_git_commit": schema_commit,
                "benchmark_git_commit": manifest["benchmark"]["git_commit"],
                "dataset_archive_sha256": manifest["benchmark"]["archive_sha256"],
                "docker_image_digest": manifest["sandbox"]["image_digest"],
                "worker_model": worker_model,
                "parent_model": parent_model,
                "temperature": temperature,
                "git_dirty": git_dirty,
                "workspace_header_used": client.workspace_header_used,
                "worker_prompt_hash": "0" * 64,
                "parent_prompt_hash": parent_prompt_hash,
                "libreoffice_version": libreoffice_version,
                "task_selection_package": selection.package,
                "selection_sha256": selection.selection_sha256,
            },
            "budgets": {"worker": worker_budget, "parent": parent_budget},
            "usage": {"worker": _zero_usage(), "parent": parent_usage},
            "handoff": handoff_record,
            "worker_outcome": worker_eval_result,
            "final_outcome": final_eval_result,
            "unsealed_last_script_score": unsealed_last_script_score,
            "final_script_path": str(final_script_path) if final_script_path else None,
            "final_script_sha256": final_script_sha256,
            "accepted_unchanged": accepted_unchanged,
            "failures": failures,
            "compliance": {
                "artifact_crossed_handoff": True,
                "worker_feedback_calls": 0,
                "parent_finalized": parent_submitted_solution,
                "worker_scratch_destroyed": True,
                "worker_submitted": True,
                "worker_observed_output_calls": 0,
                "worker_revised_after_observation": False,
                "worker_auto_observations": 0,
                "parent_auto_observations": parent_auto_observation_count,
                **auto_obs_compliance,
            },
            "status": "complete",
            "analysis_included": True,
            "analysis_inclusion_reason": (
                "Fixed-artifact parent-replay episode; never pooled with main estimates."
            ),
            "fixed_artifact": fa_meta,
        }

        episode_path = run_dir / "episode.json"
        validate_issues = validate_fa_episode(episode, episode_path)
        qc_flags = qc_episode(episode)
        atomic_write_json(episode_path, episode)
        atomic_write_json(run_dir / "fixed_artifact.json", fa_meta)

        event_logger.log("system", "artifact_sealed", {
            "artifact": "episode.json",
            "run_id": run_id,
            "validation_issues": len(validate_issues),
            "qc_flags": len(qc_flags),
            "wall_seconds": round(time.monotonic() - t_episode_start, 3),
        })

        episode["_meta"] = {
            "validate_issues": [str(i) for i in validate_issues],
            "qc_flags": [{"code": f.code, "message": f.message} for f in qc_flags],
            "run_dir": str(run_dir),
        }
        return episode
    except Exception:
        if _episode_client is None or not _episode_client.events:
            shutil.rmtree(run_dir, ignore_errors=True)
        raise


# ── Pair discovery ────────────────────────────────────────────────────────────

def load_fa_records(runs_dir: Path | None = None) -> list[dict[str, Any]]:
    base = runs_dir if runs_dir is not None else _RUNS
    records: list[dict[str, Any]] = []
    if not base.is_dir():
        return records
    for run_dir in base.iterdir():
        ep_path = run_dir / "episode.json"
        if not ep_path.is_file():
            continue
        try:
            rec = json.loads(ep_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("task_set") != TASK_SET:
            continue
        rec["_record_path"] = str(ep_path)
        records.append(rec)
    return records


def is_fa_valid_record(record: dict[str, Any], record_path: Path | None = None) -> bool:
    path = record_path or Path(record.get("_record_path") or "")
    if not path or not path.is_file():
        return False
    if record.get("task_set") != TASK_SET:
        return False
    if record.get("status") != "complete":
        return False
    fa = record.get("fixed_artifact") or {}
    if fa.get("arm") not in ARMS or not fa.get("source_run_id"):
        return False
    cleaned = {k: v for k, v in record.items() if not str(k).startswith("_")}
    issues = validate_fa_episode(cleaned, path)
    return len(issues) == 0


def valid_pairs(runs_dir: Path | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """Map source_run_id → {arm → record} for schema-valid complete pairs."""
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for rec in load_fa_records(runs_dir):
        path = Path(rec["_record_path"])
        if not is_fa_valid_record(rec, path):
            continue
        fa = rec["fixed_artifact"]
        grouped.setdefault(fa["source_run_id"], {})[fa["arm"]] = rec
    return {
        sid: arms for sid, arms in grouped.items()
        if ARM_WITHHELD in arms and ARM_SUPPLIED in arms
    }


def write_deferred(reason: str, dest: Path | None = None) -> Path:
    path = dest if dest is not None else _DEV / "fixed_artifact_deferred.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "# Fixed-artifact experiment deferred\n\n"
        f"- Timestamp (ET): {_now_et().isoformat()}\n"
        f"- Hard gate: {HARD_GATE.isoformat()}\n"
        f"- Reason: {reason}\n"
        "- Action: no 100-episode launch.\n"
        "- Main_v04 records were not modified.\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


# ── Analysis (task-clustered bootstrap; never pooled with main) ───────────────

def _outcome_row(rec: dict[str, Any]) -> dict[str, Any]:
    fa = rec["fixed_artifact"]
    parent = rec["usage"]["parent"]
    events_path = Path(rec["_record_path"]).parent / "events.jsonl"
    verify = fa.get("parent_verify_actions")
    if verify is None:
        verify = count_parent_verify_actions(events_path)
    finalized = bool(rec["compliance"]["parent_finalized"])
    success = rec.get("final_outcome", {}).get("full_task_success")
    if success is None:
        success = 0
    accepted = rec.get("accepted_unchanged")
    correct = fa.get("candidate_correct")
    return {
        "source_run_id": fa["source_run_id"],
        "task_id": rec["task_id"],
        "source_condition": fa["source_condition"],
        "arm": fa["arm"],
        "candidate_correct": correct,
        "verify_actions": int(verify),
        "any_verification": int(
            int(verify) > 0 or int(parent.get("auto_observations") or 0) > 0
        ),
        "accepted_unchanged": int(bool(accepted)) if accepted is not None else 0,
        "revision": int(finalized and accepted is False),
        "final_success": int(success),
        "repair": int(correct is False and int(success) == 1),
        "damage": int(correct is True and int(success) == 0),
        "parent_output_tokens": int(parent.get("output_tokens") or 0),
        "parent_total_tokens": int(
            (parent.get("input_tokens") or 0) + (parent.get("output_tokens") or 0)
        ),
        "non_finalization": int(not finalized),
        "correctness_enriched": bool(fa.get("correctness_enriched")),
    }


def _paired_deltas(pairs: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    metrics = (
        "verify_actions",
        "any_verification",
        "accepted_unchanged",
        "revision",
        "final_success",
        "repair",
        "damage",
        "parent_output_tokens",
        "parent_total_tokens",
        "non_finalization",
    )
    for source_id, arms in sorted(pairs.items()):
        w = _outcome_row(arms[ARM_WITHHELD])
        s = _outcome_row(arms[ARM_SUPPLIED])
        delta = {m: s[m] - w[m] for m in metrics}
        delta.update({
            "source_run_id": source_id,
            "task_id": w["task_id"],
            "source_condition": w["source_condition"],
            "candidate_correct": w["candidate_correct"],
            "correctness_enriched": w["correctness_enriched"] or s["correctness_enriched"],
            "withheld": w,
            "supplied": s,
        })
        rows.append(delta)
    return rows


def _bootstrap_mean(
    values_by_task: dict[str, list[float]],
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    import numpy as np

    tasks = sorted(values_by_task)
    if not tasks:
        return {
            "n_tasks": 0,
            "n_values": 0,
            "mean": None,
            "ci": (None, None),
            "se": None,
        }
    rng = np.random.default_rng(seed)
    n_tasks = len(tasks)
    draws = np.empty(n_draws)
    for i in range(n_draws):
        idx = rng.integers(0, n_tasks, size=n_tasks)
        collected: list[float] = []
        for j in idx:
            collected.extend(values_by_task[tasks[j]])
        draws[i] = float(np.mean(collected)) if collected else float("nan")
    point_vals = [v for vals in values_by_task.values() for v in vals]
    lo = float(np.percentile(draws, 100 * ALPHA / 2))
    hi = float(np.percentile(draws, 100 * (1 - ALPHA / 2)))
    return {
        "n_tasks": n_tasks,
        "n_values": len(point_vals),
        "mean": float(sum(point_vals) / len(point_vals)) if point_vals else None,
        "ci": (lo, hi),
        "se": float(np.std(draws, ddof=1)),
    }


def analyze_fixed_artifact(
    runs_dir: Path | None = None,
    dest: Path | None = None,
) -> dict[str, Any]:
    pairs = valid_pairs(runs_dir)
    deltas = _paired_deltas(pairs)
    metrics = (
        "verify_actions",
        "any_verification",
        "accepted_unchanged",
        "revision",
        "final_success",
        "repair",
        "damage",
        "parent_output_tokens",
        "parent_total_tokens",
        "non_finalization",
    )

    def _by_task(rows: list[dict[str, Any]], metric: str) -> dict[str, list[float]]:
        grouped: dict[str, list[float]] = {}
        for row in rows:
            grouped.setdefault(row["task_id"], []).append(float(row[metric]))
        return grouped

    def _stratum(rows: list[dict[str, Any]], correct: bool | None) -> list[dict[str, Any]]:
        if correct is None:
            return rows
        return [r for r in rows if r["candidate_correct"] is correct]

    report_blocks: dict[str, Any] = {}
    for label, subset in (
        ("all", deltas),
        ("incorrect_candidates", _stratum(deltas, False)),
        ("correct_candidates", _stratum(deltas, True)),
    ):
        report_blocks[label] = {
            metric: _bootstrap_mean(_by_task(subset, metric))
            for metric in metrics
        }
        report_blocks[label]["n_pairs"] = len(subset)

    dest = dest if dest is not None else _DEV / "fixed_artifact_report.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Fixed-artifact parent-replay report",
        "",
        f"- Written: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        f"- Design seed (arm order): {DESIGN_SEED}",
        f"- Bootstrap: task-clustered percentile, B={BOOTSTRAP_DRAWS}, "
        f"seed {BOOTSTRAP_SEED}, α={ALPHA}",
        f"- Valid pairs: {len(deltas)} "
        f"(incorrect={report_blocks['incorrect_candidates']['n_pairs']}, "
        f"correct={report_blocks['correct_candidates']['n_pairs']})",
        "- Estimand: mean(supplied − withheld) within candidate.",
        "- Primary: parent `run_solution` + explicit `read_output_range` counts.",
        "- Never pooled with main / main_v04.",
        "",
        "## Hypotheses",
        "",
        "- H_FA1 evidence reduces parent verification (primary `verify_actions`).",
        "- H_FA2 evidence increases unchanged adoption.",
        "- H_FA3 among incorrect candidates, evidence reduces repair.",
        "- H_FA4 among correct candidates, evidence reduces checking without "
        "reducing final success.",
        "",
    ]

    def _fmt(block: dict[str, Any], metric: str) -> str:
        est = block[metric]
        if est["mean"] is None:
            return f"{metric}: n=0"
        lo, hi = est["ci"]
        return (
            f"{metric}: {est['mean']:+.4f} "
            f"95% CI [{lo:+.4f}, {hi:+.4f}] "
            f"(n_pairs={block['n_pairs']}, n_tasks={est['n_tasks']})"
        )

    for title, key in (
        ("All candidates", "all"),
        ("Incorrect-candidate stratum (H_FA3)", "incorrect_candidates"),
        ("Correct-candidate stratum (H_FA4)", "correct_candidates"),
    ):
        lines.append(f"## {title}")
        lines.append("")
        block = report_blocks[key]
        for metric in metrics:
            lines.append(f"- {_fmt(block, metric)}")
        lines.append("")

    lines.append("## Pair rows")
    lines.append("")
    lines.append(
        "| source_run_id | task | cell | correct | "
        "Δ verify | Δ adopt | Δ success | Δ repair | Δ damage |"
    )
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|")
    for row in deltas:
        lines.append(
            f"| {row['source_run_id']} | {row['task_id']} | "
            f"{row['source_condition']} | {row['candidate_correct']} | "
            f"{row['verify_actions']:+d} | {row['accepted_unchanged']:+d} | "
            f"{row['final_success']:+d} | {row['repair']:+d} | {row['damage']:+d} |"
        )
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "n_pairs": len(deltas),
        "blocks": report_blocks,
        "report_path": str(dest),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parent-only fixed-artifact replay (SpreadsheetBench)."
    )
    parser.add_argument("--source-run-id", default=None,
                        help="main_v04 P-cell run_id to replay")
    parser.add_argument("--arm", choices=list(ARMS), default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--task-set", default=TASK_SET,
                        help="Must be fixed_artifact (default).")
    parser.add_argument("--list-candidates", action="store_true")
    parser.add_argument("--plan", action="store_true",
                        help="Print arm assignments for the selected sample.")
    parser.add_argument("--smoke", action="store_true",
                        help="Restrict --plan to the 2-candidate smoke sample.")
    parser.add_argument("--enriched", action="store_true",
                        help="Correctness-enriched fallback sample.")
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--check-pairs", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-stale-check", action="store_true")
    parser.add_argument("--skip-clean-tree", action="store_true")
    parser.add_argument("--runs-dir", default=None)
    args = parser.parse_args(argv)

    if args.task_set != TASK_SET:
        print(
            f"ERROR: --task-set must be {TASK_SET!r} (got {args.task_set!r}). "
            "FA is never pooled with main.",
            file=sys.stderr,
        )
        return 1

    runs_dir = Path(args.runs_dir) if args.runs_dir else None

    if args.list_candidates or args.plan:
        candidates = discover_main_v04_p_candidates(runs_dir)
        if args.smoke:
            candidates = select_smoke_candidates(candidates)
        elif args.enriched:
            candidates = select_enriched_candidates(candidates)
        print(f"n_candidates={len(candidates)} seed={DESIGN_SEED}")
        for cand in candidates:
            order = assign_arm_order(cand.source_run_id)
            print(
                f"{cand.task_id}\t{cand.source_condition}\t"
                f"ok={cand.candidate_correct}\t{cand.source_run_id}\t"
                f"order={','.join(order)}"
            )
        return 0

    if args.check_pairs:
        pairs = valid_pairs(runs_dir)
        print(f"valid_pairs={len(pairs)}")
        for sid, arms in sorted(pairs.items()):
            w = arms[ARM_WITHHELD]["run_id"]
            s = arms[ARM_SUPPLIED]["run_id"]
            print(f"{sid}\twithheld={w}\tsupplied={s}")
        return 0 if pairs else 2

    if args.analyze:
        summary = analyze_fixed_artifact(runs_dir)
        print(f"pairs={summary['n_pairs']} report={summary['report_path']}")
        return 0

    if not args.source_run_id or not args.arm:
        parser.error("--source-run-id and --arm are required unless listing/analyzing")

    try:
        episode = run_fa_episode(
            source_run_id=args.source_run_id,
            arm=args.arm,
            run_id=args.run_id,
            batch_id=args.batch_id,
            runs_dir=runs_dir,
            skip_preflight=args.skip_preflight,
            skip_stale_check=args.skip_stale_check,
            skip_clean_tree=args.skip_clean_tree,
            correctness_enriched=args.enriched,
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
    print(f"FA episode complete: run_id={episode['run_id']}")
    print(f"arm={episode['fixed_artifact']['arm']} "
          f"source={episode['fixed_artifact']['source_run_id']} "
          f"task={episode['task_id']}")
    print(f"run_dir={meta.get('run_dir')}")
    fo = episode["final_outcome"]
    print(
        f"final={fo['evaluator_status']} full={fo['full_task_success']} "
        f"accepted_unchanged={episode['accepted_unchanged']}"
    )
    issues = meta.get("validate_issues", [])
    print(f"FA validation issues: {len(issues)}")
    for issue in issues:
        print(f"  ISSUE: {issue}")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
