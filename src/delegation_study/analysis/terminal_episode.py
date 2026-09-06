"""Analysis-side contract for a terminal agent-failure episode.

The shared ``contracts/episode_schema.json`` status enum is only
``complete`` | ``infrastructure_failure``. A context-window stop is an
agent outcome, not infrastructure, so this module:

* accepts ``run_status=agent_failure`` / ``failure_reason=context_window_exceeded``
* refuses to relabel that outcome as ``infrastructure_failure``
* builds a record from persisted events and on-disk handoff files only

Shared-schema validation is expected to reject these records. Analysis
loading uses this contract instead.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..atomic import atomic_write_json

ORIGINAL_MAIN_V04_A0_RUN_ID = "80bdf704-cec3-4669-96f4-ffb134c13eec"
ABANDONED_RUN_ID = "2fe91799-cec3-4669-96f4-ffb134c13eec"
AGENT_FAILURE_STATUS = "agent_failure"
CONTEXT_WINDOW_REASON = "context_window_exceeded"
DISCLOSED_CONTEXT_WINDOW_DETAIL = (
    "prompt_too_long: 225,243 tokens > 200,000 context-window maximum"
)
SHARED_STATUS_ENUM = frozenset({"complete", "infrastructure_failure"})
ANALYSIS_STATUS_ENUM = SHARED_STATUS_ENUM | {AGENT_FAILURE_STATUS}

_CONTRACT_DIR = Path(__file__).resolve().parent
TERMINAL_EPISODE_SCHEMA_PATH = _CONTRACT_DIR / "terminal_episode.schema.json"


class TerminalEpisodeError(ValueError):
    """Raised when a terminal agent-failure record is dishonest or incomplete."""


def is_terminal_agent_failure(record: dict[str, Any]) -> bool:
    """Return True when the record names an analysis-side agent failure."""
    run_status = record.get("run_status") or record.get("status")
    reason = record.get("failure_reason")
    if run_status == "infrastructure_failure":
        return False
    if run_status == AGENT_FAILURE_STATUS:
        return True
    if reason == CONTEXT_WINDOW_REASON:
        return True
    failures = record.get("failures") or []
    return any(
        isinstance(item, dict)
        and item.get("source") == "agent"
        and item.get("code") == CONTEXT_WINDOW_REASON
        for item in failures
    )


def validate_terminal_agent_failure(record: dict[str, Any]) -> list[str]:
    """Return analysis-side contract issues; empty means the record is usable."""
    issues: list[str] = []
    if record.get("status") == "infrastructure_failure":
        issues.append("must not mislabel agent_failure as infrastructure_failure")
    if record.get("run_status") == "infrastructure_failure":
        issues.append("run_status must not be infrastructure_failure")
    if record.get("run_status") != AGENT_FAILURE_STATUS:
        issues.append("run_status must be agent_failure")
    if record.get("status") != AGENT_FAILURE_STATUS:
        issues.append("status must be agent_failure on the analysis-side record")
    if record.get("failure_reason") != CONTEXT_WINDOW_REASON:
        issues.append("failure_reason must be context_window_exceeded")
    if record.get("analysis_included") is not True:
        issues.append("analysis_included must be true")
    final_outcome = record.get("final_outcome") or {}
    if final_outcome.get("full_task_success") != 0:
        issues.append("full_task_success must be 0")
    if record.get("run_id") == ABANDONED_RUN_ID:
        issues.append("abandoned run_id 2fe91799 is not permitted")
    failures = record.get("failures") or []
    if not any(
        isinstance(item, dict)
        and item.get("source") == "agent"
        and item.get("code") == CONTEXT_WINDOW_REASON
        for item in failures
    ):
        issues.append("failures must include source=agent code=context_window_exceeded")
    if any(
        isinstance(item, dict) and item.get("source") == "infrastructure"
        for item in failures
    ):
        issues.append("must not attach an infrastructure failure to this record")
    return issues


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        raise TerminalEpisodeError(f"events.jsonl missing: {events_path}")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        events_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TerminalEpisodeError(
                f"malformed event {events_path}:{line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise TerminalEpisodeError(
                f"event {events_path}:{line_number} is not an object"
            )
        events.append(event)
    if not events:
        raise TerminalEpisodeError(f"no events in {events_path}")
    return events


def _batch_constant_reproducibility(runs_dir: Path) -> dict[str, Any]:
    """Copy launch-constant reproducibility from a same-batch A0 sibling."""
    for run_dir in sorted(runs_dir.iterdir(), key=lambda path: path.name):
        episode_path = run_dir / "episode.json"
        if run_dir.name == ORIGINAL_MAIN_V04_A0_RUN_ID:
            continue
        if run_dir.name.startswith(ABANDONED_RUN_ID[:8]):
            continue
        if not episode_path.is_file():
            continue
        try:
            record = json.loads(episode_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            record.get("task_set") == "main_v04"
            and record.get("condition") == "A0"
            and record.get("status") == "complete"
            and isinstance(record.get("reproducibility"), dict)
        ):
            repro = dict(record["reproducibility"])
            budgets = record.get("budgets")
            if not isinstance(budgets, dict):
                raise TerminalEpisodeError(
                    f"sibling {run_dir.name} has no budgets object"
                )
            return {"reproducibility": repro, "budgets": budgets}
    raise TerminalEpisodeError(
        "no same-batch A0 sibling with complete reproducibility"
    )


def _handoff_from_disk(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    sealed = [
        event for event in events if event.get("event_type") == "handoff_sealed"
    ]
    if len(sealed) != 1:
        raise TerminalEpisodeError(
            f"expected one handoff_sealed event, found {len(sealed)}"
        )
    payload = sealed[0].get("payload") or {}
    memo_path = run_dir / "handoff" / "memo.json"
    candidate_path = run_dir / "handoff" / "solution.py"
    if payload.get("memo_present") is not True or not memo_path.is_file():
        raise TerminalEpisodeError("handoff memo missing; refusing to invent one")
    if payload.get("candidate_present") or candidate_path.exists():
        raise TerminalEpisodeError("A0 candidate must stay absent")
    memo = json.loads(memo_path.read_text(encoding="utf-8"))
    return {
        "memo_path": str(memo_path),
        "memo_sha256": _sha256_file(memo_path),
        "candidate_script_path": None,
        "candidate_script_sha256": None,
        "worker_submitted_memo": True,
        "worker_submitted_candidate": False,
        "memo_claims_validation": bool(memo.get("observed_feedback")),
    }


def _usage_from_events(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from .analyze import _load_event_usage

    recovered, evidence = _load_event_usage(run_dir)
    usage: dict[str, Any] = {}
    for role in ("worker", "parent"):
        row = dict(recovered[role])
        # Context-window stop is not a role budget or safety-ceiling exhaustion.
        row["budget_exhausted"] = False
        row["budget_exhausted_on"] = None
        row["safety_exhausted"] = False
        row["safety_exhausted_on"] = None
        row["observed_output_calls"] = 0
        usage[role] = row
    return usage, evidence


def _timestamps(events: list[dict[str, Any]]) -> tuple[str, str]:
    started = events[0].get("timestamp")
    completed = events[-1].get("timestamp")
    if not isinstance(started, str) or not isinstance(completed, str):
        raise TerminalEpisodeError("events lack timestamps")
    return started, completed


def build_terminal_agent_failure_episode(
    run_dir: Path,
    *,
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Build an honest terminal episode from persisted events and handoff files."""
    run_dir = run_dir.resolve()
    if run_dir.name != ORIGINAL_MAIN_V04_A0_RUN_ID:
        raise TerminalEpisodeError(
            f"refusing to build a stub for {run_dir.name}; "
            f"only {ORIGINAL_MAIN_V04_A0_RUN_ID} is permitted"
        )
    if (run_dir / "final" / "output.xlsx").exists() or (
        run_dir / "final" / "evaluator.json"
    ).exists():
        raise TerminalEpisodeError("refusing to ignore unexpected final artifacts")
    events = _load_events(run_dir)
    sibling = _batch_constant_reproducibility(runs_dir or run_dir.parent)
    usage, evidence = _usage_from_events(run_dir)
    started_at, completed_at = _timestamps(events)
    handoff = _handoff_from_disk(run_dir, events)
    if evidence["model_calls"] < 1:
        raise TerminalEpisodeError("no persisted model_call events")
    episode = {
        "run_id": ORIGINAL_MAIN_V04_A0_RUN_ID,
        "task_id": "57232",
        "task_set": "main_v04",
        "condition": "A0",
        "protocol_version": "0.4.1",
        "replicate": 1,
        "started_at": started_at,
        "completed_at": completed_at,
        "reproducibility": sibling["reproducibility"],
        "budgets": sibling["budgets"],
        "usage": usage,
        "handoff": handoff,
        "worker_outcome": None,
        "final_outcome": {
            "evaluator_status": "not_finalized",
            "full_task_success": 0,
            "target_unit_pass_rate": 0.0,
            "n_units": 0,
            "output_path": None,
            "evaluator_path": None,
        },
        "unsealed_last_script_score": None,
        "final_script_path": None,
        "final_script_sha256": None,
        "accepted_unchanged": None,
        "failures": [{
            "source": "agent",
            "code": CONTEXT_WINDOW_REASON,
            "message": DISCLOSED_CONTEXT_WINDOW_DETAIL,
        }],
        "compliance": {
            "artifact_crossed_handoff": False,
            "worker_feedback_calls": 0,
            "parent_finalized": False,
            "worker_scratch_destroyed": True,
            "worker_submitted": True,
            "worker_observed_output_calls": 0,
            "worker_revised_after_observation": False,
            "worker_auto_observations": int(usage["worker"].get("auto_observations") or 0),
            "parent_auto_observations": int(usage["parent"].get("auto_observations") or 0),
            "eligible_worker_run_events": int(evidence["run_events"]["worker"]),
            "eligible_parent_run_events": int(evidence["run_events"]["parent"]),
            "worker_runs_without_auto_observation": int(
                evidence["runs_without_auto_observation"]["worker"]
            ),
            "parent_runs_without_auto_observation": int(
                evidence["runs_without_auto_observation"]["parent"]
            ),
        },
        "status": AGENT_FAILURE_STATUS,
        "run_status": AGENT_FAILURE_STATUS,
        "failure_reason": CONTEXT_WINDOW_REASON,
        "analysis_included": True,
        "analysis_inclusion_reason": (
            "Terminal agent_failure retained under ITT with full_task_success=0. "
            "Parent context-window request exceeded the 200,000-token maximum "
            f"({DISCLOSED_CONTEXT_WINDOW_DETAIL}). Shared episode_schema status "
            "enum cannot name agent_failure; this record is validated by the "
            "analysis-side terminal-episode contract. Usage and handoff hashes "
            "are derived from persisted events and files; no evaluator artifact "
            "was invented."
        ),
    }
    issues = validate_terminal_agent_failure(episode)
    if issues:
        raise TerminalEpisodeError("; ".join(issues))
    return episode


def write_terminal_agent_failure_episode(
    run_dir: Path,
    *,
    runs_dir: Path | None = None,
) -> Path:
    """Write ``episode.json`` once; never overwrite an existing record."""
    episode_path = run_dir / "episode.json"
    if episode_path.exists():
        existing = json.loads(episode_path.read_text(encoding="utf-8"))
        issues = validate_terminal_agent_failure(existing)
        if issues:
            raise TerminalEpisodeError(
                f"{episode_path} exists but is not an honest terminal record: "
                + "; ".join(issues)
            )
        return episode_path
    episode = build_terminal_agent_failure_episode(run_dir, runs_dir=runs_dir)
    atomic_write_json(episode_path, episode)
    return episode_path
