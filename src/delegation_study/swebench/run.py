"""SWE-bench P1 episode runner — Gate 1 production adapter.

Runs one complete P1 delegation episode for a SWE-bench task using the
shared agent_loop, model_client, records, validate, and qc infrastructure.

Entry point::

    python -m delegation_study.swebench.run \\
        --task-id django__django-14752 --condition P1 --task-set dev

Hard invariants
---------------
1. Gold patch/test_patch/hints/FAIL_TO_PASS/PASS_TO_PASS never enter any
   prompt, event, log, stdout, or git-tracked file.
2. Worker and parent use distinct, fresh, network-isolated containers.
3. Prediction files are chmod 0600 and deleted immediately after scoring.
4. Episode record written atomically; validated against episode_schema.json.
5. All containers labeled delegation-study=true; cleaned in finally blocks.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from ..atomic import atomic_write_json
from ..agent_loop import AgentPhaseResult
from .seal import SEALED_AFTER_REPROMPT, run_swe_agent_phase

# ── Durable stderr capture ─────────────────────────────────────────────────────

class _TeeStderr:
    """Tee ``sys.stderr`` to a run-local log file.

    Installed at episode start so that any Python traceback printed by the
    launcher's ``except`` block (or by the interpreter on crash) is captured
    in ``logs/runner.stderr.log`` even when the process was launched via
    ``nohup`` with a single stdout/stderr redirect.

    * Line-buffered (``buffering=1``) for durability under abnormal exit.
    * Never writes secrets, API keys, or gold patch content.
    * fileno() delegates to the original stderr so tools that need a real fd
      (e.g. ``subprocess``) still work.
    """

    def __init__(self, orig: Any, log_file: Any) -> None:
        self._orig = orig
        self._log = log_file

    def write(self, data: str) -> int:
        self._orig.write(data)
        try:
            self._log.write(data)
        except Exception:
            pass
        return len(data)

    def flush(self) -> None:
        self._orig.flush()
        try:
            self._log.flush()
        except Exception:
            pass

    def fileno(self) -> int:
        return self._orig.fileno()

    # Delegate everything else to original so we satisfy the TextIO protocol.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._orig, name)
from ..model_client import ModelClient, ProviderContentFilterError, PROVIDER_CONTENT_FILTER
from ..qc import qc_episode
from ..records import EventLogger
from ..validate import validate_episode
from .task_loader import load_task, _load_raw, FORBIDDEN_FIELDS
from .prompt import check_prompt_excludes_forbidden
from .evaluator import (
    score_patch,
    check_no_swe_containers,
    ScoreResult,
    HF_DATASET_NAME,
)
from .workspace import SWEToolDispatcher, SWEWorkspace, _sha256_str
from .container import expected_image, sweep_orphaned_swe_containers
from .conversation import (
    ConversationGuard,
    PromptTooLongError,
    PROVIDER_PROMPT_TOO_LONG,
)

# ── Paths ─────────────────────────────────────────────────────────────────────

_WORKTREE_ROOT = Path(__file__).resolve().parents[3]  # …/Submission-swe-adapter/
_REPO_ROOT = _WORKTREE_ROOT  # same for this adapter
_CONFIG = _WORKTREE_ROOT / "config" / "manifest.yaml"
_RUNS_DIR = _WORKTREE_ROOT / "data" / "runs"

# ── Constants ─────────────────────────────────────────────────────────────────

class _ParentSkipped(Exception):
    """Internal: skip parent after a worker-side infrastructure abort."""


PROTOCOL_VERSION = "swe-0.3.5"
BENCHMARK = "swebench"
# Historical swe-0.3 observation constants (80/160). swe-0.3.5 does not apply
# them; frozen swebench.caps (115/110 worker, 100/100 parent) are live.
SWE_OBS_TURNS = 80
SWE_OBS_TOOL_CALLS = 160
SWE_OBS_PRIOR_TURNS = 40
SWE_OBS_PRIOR_TOOL_CALLS = 100
SWE_OBS_NOTE = "loosened for SWE observation (swe-0.3: 80/160 vs swe-0.2: 40/100)"
# Supported experiment conditions.
# A0/A1: worker-only (analysis / no-analysis).  No parent phase; worker submits memo.
# P0/P1: full delegation (worker + parent).  Worker submits memo + candidate patch;
#         parent reviews and submits final patch.
_A_CONDITIONS = frozenset({"A0", "A1"})
_P_CONDITIONS = frozenset({"P0", "P1"})
_ALL_CONDITIONS = _A_CONDITIONS | _P_CONDITIONS
CONDITION = "P1"
TASK_SET_ALIASES = {"dev": "development"}
_ALLOWED_TASK_SETS = {
    "development", "engineering", "pilot", "main", "dev",
    "main_v035", "solo_v035",
}

# SWE task selection package v3 (immutable; sha prefix e426590d)
_SELECTION_PACKAGE = "config/task_selection_swe_v3"
_SELECTION_SHA = "e426590d9e9342af60ab48c5e3ddb174a73fbdfb05de5fff7ffb7d348752f0be"
# docker image digest (sha256 from inspect)
_DOCKER_DIGEST = "sha256:743b7bd1ab48273b53cc92635f21a5f2697d2a7bd4af7a2a17c399d0d24ebecd"


# ── Prompt builders ───────────────────────────────────────────────────────────

def _worker_system_prompt(max_turns: int, condition: str = "P1") -> str:
    """Build the worker system prompt.

    Parameters
    ----------
    max_turns:
        Exact turn cap for this role/episode (stated explicitly in prompt).
    condition:
        Experiment condition (A0/A1/P0/P1) — determines advisory vs production
        seal behaviour. Submit tools are optional logged actions in swe-0.3.
    """
    submit_reserve = max(1, max_turns - 3)
    is_a_cell = condition.startswith("A")
    if is_a_cell:
        seal_section = (
            f"Ending a turn with no tool call seals this role immediately; "
            f"your last assistant message becomes the memo. "
            f"Wrap up by turn {submit_reserve}. "
            f"submit_memo is optional."
        )
        tools_section = (
            "Tools: use exec_shell to explore the codebase and run tests (restricted allowlist); "
            "read_file and write_file to read and modify source files. "
            "submit_memo accepts a JSON object with 'summary' (string) and optional "
            "'files_changed' (list)."
        )
    else:
        seal_section = (
            f"Ending a turn with no tool call seals this role when /testbed has "
            f"changes. If you have not changed any files, you will be told once "
            f"to make an edit and call submit_solution; a second empty end-turn "
            f"seals as non-submission. "
            f"The git diff of /testbed is exported as the candidate patch. "
            f"Wrap up by turn {submit_reserve}. "
            f"submit_memo and submit_solution are optional logged actions. "
            f"An empty working tree after that one reminder yields no candidate "
            f"patch; the parent still runs."
        )
        tools_section = (
            "Tools: use exec_shell to explore the codebase and run tests (restricted allowlist); "
            "read_file and write_file to read and modify source files. "
            "submit_memo accepts a JSON object with 'summary' (string) and optional "
            "'files_changed' (list). "
            "submit_solution records a snapshot of your current patch — it does not "
            "end the role."
        )
    return (
        f"You are a software engineering assistant. "
        f"You are given a GitHub issue in a Python repository. "
        f"Your task is to identify the root cause and write a fix. "
        f"The repository is mounted at /testbed. "
        f"\n\nTURN BUDGET: You have exactly {max_turns} turns total. "
        f"Each tool call uses one turn. Watch the 'turns remaining: k' line appended to every result. "
        f"{seal_section} "
        f"\n\n{tools_section}"
    )


def _parent_system_prompt(max_turns: int) -> str:
    """Build the parent system prompt.

    This prompt is condition-independent; the task/handoff content is in the
    user message, not the system prompt.

    Parameters
    ----------
    max_turns:
        Exact turn cap for the parent role (stated explicitly in prompt).
    """
    submit_reserve = max(1, max_turns - 3)
    return (
        f"You are a senior software engineer reviewing a fix for a GitHub issue. "
        f"You receive the original issue description, a worker memo summarising the fix, "
        f"and information about a candidate patch pre-applied to /testbed. "
        f"\n\nTURN BUDGET: You have exactly {max_turns} turns total. "
        f"Each tool call uses one turn. Watch the 'turns remaining: k' line appended to every result. "
        f"Ending a turn with no tool call seals this role when /testbed has "
        f"changes. If you have not changed any files, you will be told once "
        f"to make an edit and call submit_solution; a second empty end-turn "
        f"seals as non-submission. "
        f"The git diff of /testbed is exported as the final patch. "
        f"Wrap up by turn {submit_reserve}. submit_solution is optional. "
        f"A partial or unchanged patch is acceptable. "
        f"\n\nTools: exec_shell to review and run tests; read_file and write_file to inspect or improve the fix. "
        f"submit_solution records a snapshot of the current patch — it does not end the role."
    )


def _swe_reprompt(missing_tools: set[str]) -> str:
    """Build the re-prompt injected when a role ends a turn with prose and no tool call.

    Names each missing required tool explicitly.  Called by the agent loop
    whenever stop_reason='end_turn' fires while required submissions remain.
    The message is short and directive so it does not inflate context unnecessarily.
    """
    tools_str = " and ".join(f"`{t}`" for t in sorted(missing_tools))
    return (
        f"You ended your turn without calling the required tool(s): {tools_str}. "
        f"You must call {tools_str} before finishing. "
        f"Please continue and call the missing tool(s) now."
    )


def _worker_required_tools(condition: str) -> set[str]:
    """Return the set of tools a worker MUST validly submit under swe-0.2."""
    if condition in ("A0", "A1"):
        return {"submit_memo"}
    # P0, P1
    return {"submit_memo", "submit_solution"}


def _parent_required_tools() -> set[str]:
    """Return the set of tools a parent MUST validly submit under swe-0.2."""
    return {"submit_solution"}


def _worker_user_message(task_public: dict) -> str:
    """Build the worker initial user message from safe public task data only."""
    return (
        f"# SWE-bench Task: {task_public['instance_id']}\n\n"
        f"**Repository:** {task_public['repo']}\n"
        f"**Base commit:** {task_public['base_commit']}\n\n"
        f"## Issue\n\n{task_public['problem_statement']}\n\n"
        "The repository is at /testbed. Explore the codebase, identify the fix, "
        "and end your turn (no tool call) to seal. Your last message is the memo; "
        "in production cells the git diff of /testbed is the candidate patch. "
        "If a production cell has no file changes, you will be reminded once "
        "to edit and call submit_solution."
    )


def _parent_user_message(task_public: dict, memo: dict | None, patch_content: str | None) -> str:
    """Build the parent user message (problem + memo + candidate patch info)."""
    parts = [
        f"# SWE-bench Task Review: {task_public['instance_id']}\n",
        f"**Repository:** {task_public['repo']}  |  **Base commit:** {task_public['base_commit']}\n",
        f"\n## Original Issue\n\n{task_public['problem_statement']}\n",
    ]
    if memo:
        parts.append(f"\n## Worker Memo\n\n{json.dumps(memo, indent=2)}\n")
    if patch_content:
        # Do NOT show patch content to parent — show only metadata
        sha = _sha256_str(patch_content)
        parts.append(
            f"\n## Candidate Patch\n\n"
            f"A candidate patch has been pre-applied to /testbed.\n"
            f"Patch SHA-256: {sha[:16]}... ({len(patch_content)} bytes)\n"
            "Review the applied changes and verify with exec_shell.\n"
        )
    else:
        parts.append("\n## Candidate Patch\n\nNo candidate patch was submitted by the worker.\n")
    parts.append(
        "\nWhen done, end your turn with no tool call to seal if /testbed has "
        "changes. If you have not changed any files you will be reminded once "
        "to edit and call submit_solution. "
        "The git diff of /testbed becomes the final patch. "
        "submit_solution is optional."
    )
    return "".join(parts)


# ── Utility ───────────────────────────────────────────────────────────────────

def expected_swe_image(instance_id: str, namespace: str = "swebench") -> str:
    """Docker image tag derived from *instance_id* (same path as workspace.create)."""
    return expected_image(instance_id, namespace=namespace)


def remote_matches_repo(remote_v: str, repo: str) -> bool:
    """True if ``git remote -v`` text names the SWE-bench ``repo`` field."""
    compact = remote_v.lower().replace(":", "/").replace(".git", "")
    return bool(repo) and repo.lower() in compact


def swe_image_exists(image_tag: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0


def swe_image_digest(image_tag: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_tag],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def collect_host_provenance(repo_root: Path) -> dict[str, Any]:
    """Record checkout HEAD, origin URL, and clean-tree flag. Never logs .env."""
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_root), text=True
    ).strip()
    try:
        remote = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=str(repo_root), text=True
        ).strip()
    except subprocess.CalledProcessError:
        remote = ""
    porcelain = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=str(repo_root), text=True
    )
    clean = porcelain.strip() == ""
    return {
        "git_head": head,
        "git_remote": remote,
        "clean_tree": clean,
        "git_dirty": not clean,
    }


def reset_container_to_base(container_name: str, base_commit: str) -> None:
    """Check the agent workspace out at the instance ``base_commit``.

    Official SWE-bench images carry one extra ``SWE-bench`` commit on top of
    the dataset base. Certification requires both roles to start at
    ``base_commit`` with a clean tree.
    """
    result = subprocess.run(
        ["docker", "exec", "--workdir", "/testbed", container_name,
         "git", "reset", "--hard", base_commit],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git reset --hard to base_commit failed rc={result.returncode}"
        )
    clean = subprocess.run(
        ["docker", "exec", "--workdir", "/testbed", container_name,
         "git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if clean.returncode != 0 or clean.stdout.strip():
        raise RuntimeError("working tree not clean after reset to base_commit")


def inspect_container_start(container_name: str) -> dict[str, Any]:
    """Harness-side start-state probe: container ID, image, HEAD, clean tree."""
    ident = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}} {{.Config.Image}}", container_name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    parts = ident.stdout.strip().split()
    container_id = parts[0] if parts else ""
    cfg_image = parts[1] if len(parts) > 1 else ""
    head = subprocess.run(
        ["docker", "exec", "--workdir", "/testbed", container_name, "git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    status = subprocess.run(
        ["docker", "exec", "--workdir", "/testbed", container_name, "git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    remote = subprocess.run(
        ["docker", "exec", "--workdir", "/testbed", container_name, "git", "remote", "-v"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "container_id": container_id,
        "image_tag": cfg_image,
        "head": head.stdout.strip(),
        "clean": status.returncode == 0 and status.stdout.strip() == "",
        "porcelain": status.stdout,
        "remote": remote.stdout,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skipped_eval_outcome(reports_dir: Path, suffix: str) -> dict[str, Any]:
    """Schema-valid outcome when certification skips the harness (A2/D smoke)."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    artifact = reports_dir / f"{suffix}_skipped.json"
    artifact.write_text(json.dumps({"skipped": True, "reason": "certification_no_harness"}))
    output_sha256 = _sha256_file(artifact)
    summary = {
        "evaluator_status": "ok",
        "full_task_success": 0,
        "target_unit_pass_rate": 0.0,
        "n_units": 1,
        "output_sha256": output_sha256,
    }
    summary_path = reports_dir / f"{suffix}_eval_summary.json"
    summary_path.write_text(json.dumps(summary))
    return {
        "evaluator_status": "ok",
        "full_task_success": 0,
        "target_unit_pass_rate": 0.0,
        "n_units": 1,
        "output_path": str(artifact),
        "evaluator_path": str(summary_path),
        "output_sha256": output_sha256,
    }


def _build_swe_outcome(
    score: Any,
    reports_dir: Path,
    suffix: str,
) -> dict[str, Any]:
    """Build a schema-compliant outcome dict for one SWE harness evaluation.

    The shared validator cross-checks the episode outcome dict against an
    evaluator artifact file (the file at ``evaluator_path``).  The SWE harness
    report uses a different JSON schema, so we write a small
    ``<suffix>_eval_summary.json`` that mirrors the required schema fields.
    This makes the validator's field-by-field comparison deterministic and
    independent of the harness report layout.

    Parameters
    ----------
    score:
        ``ScoreResult`` from ``score_patch()``, or ``None`` / any object with
        ``infra_failure=True`` to indicate evaluation failure.
    reports_dir:
        Directory where artifact files are written.
    suffix:
        Short label used in file names (e.g. ``"candidate"``, ``"final"``).

    Returns
    -------
    Outcome dict ready for embedding in the episode record.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)

    if score is None or getattr(score, "infra_failure", True):
        # Evaluation did not succeed — write a failure summary so the
        # validator can still find a valid evaluator artifact.
        summary: dict[str, Any] = {
            "evaluator_status": "evaluator_failure",
            "full_task_success": None,
            "target_unit_pass_rate": None,
            "n_units": 0,
            "output_sha256": None,
        }
        summary_path = reports_dir / f"{suffix}_eval_summary.json"
        summary_path.write_text(json.dumps(summary))
        return {
            "evaluator_status": "evaluator_failure",
            "full_task_success": None,
            "target_unit_pass_rate": None,
            "n_units": 0,
            "output_path": None,
            "evaluator_path": str(summary_path),
            "output_sha256": None,
        }

    # Copy the raw harness report even when the instance timed out / is an
    # infra failure — the report is the evidence, not a scored 0/1.
    report_dest = reports_dir / f"{suffix}_report.json"
    if score.report_path and score.report_path.is_file():
        shutil.copy2(score.report_path, report_dest)
    output_sha256 = _sha256_file(report_dest) if report_dest.is_file() else None

    # n_units: number of SWE instances evaluated (typically 1 per run).
    n_units = getattr(score, "total_instances", 1) or 1
    if getattr(score, "infra_failure", False):
        summary = {
            "evaluator_status": "evaluator_failure",
            "full_task_success": None,
            "target_unit_pass_rate": None,
            "n_units": n_units,
            "output_sha256": output_sha256,
        }
        summary_path = reports_dir / f"{suffix}_eval_summary.json"
        summary_path.write_text(json.dumps(summary))
        return {
            "evaluator_status": "evaluator_failure",
            "full_task_success": None,
            "target_unit_pass_rate": None,
            "n_units": n_units,
            "output_path": str(report_dest) if report_dest.is_file() else None,
            "evaluator_path": str(summary_path),
            "output_sha256": output_sha256,
        }

    full_task_success = int(score.resolved)
    # target_unit_pass_rate: fraction of evaluated instances resolved.
    # For single-instance runs this is 0.0 or 1.0.
    target_unit_pass_rate = float(score.resolved) if n_units > 0 else 0.0

    # Write the evaluator summary with the same fields the validator checks.
    summary = {
        "evaluator_status": "ok",
        "full_task_success": full_task_success,
        "target_unit_pass_rate": target_unit_pass_rate,
        "n_units": n_units,
        "output_sha256": output_sha256,
    }
    summary_path = reports_dir / f"{suffix}_eval_summary.json"
    summary_path.write_text(json.dumps(summary))

    return {
        "evaluator_status": "ok",
        "full_task_success": full_task_success,
        "target_unit_pass_rate": target_unit_pass_rate,
        "n_units": n_units,
        "output_path": str(report_dest) if report_dest.is_file() else None,
        "evaluator_path": str(summary_path),
        "output_sha256": output_sha256,
    }


def content_filter_failure(message: str) -> dict[str, Any]:
    """Infrastructure failure payload for a retried-and-failed content-filter 400."""
    return {
        "source": "infrastructure",
        "code": PROVIDER_CONTENT_FILTER,
        "message": message,
        "rerun_eligible": True,
    }


def prompt_too_long_failure(message: str) -> dict[str, Any]:
    """Infrastructure failure payload when context-window compaction cannot proceed."""
    return {
        "source": "infrastructure",
        "code": PROVIDER_PROMPT_TOO_LONG,
        "message": message,
        "rerun_eligible": True,
    }


def _load_budget(role: str) -> dict[str, Any]:
    cfg = yaml.safe_load(_CONFIG.read_text())
    swe_caps = ((cfg.get("swebench") or {}).get("caps") or {}).get(role)
    if isinstance(swe_caps, dict) and swe_caps:
        return dict(swe_caps)
    return dict(cfg["budgets"][role])


def apply_observation_caps(
    task_set: str,
    worker_budget: dict[str, Any],
    parent_budget: dict[str, Any],
) -> dict[str, Any] | None:
    """swe-0.3.5: never overwrite frozen ``swebench.caps``.

    Earlier swe-0.3 observation used 80/160 for dev/pilot. Those constants
    remain on the module for provenance notes; live budgets come from
    ``_load_budget`` (manifest ``swebench.caps``: 115/110 worker, 100/100
    parent). ``task_set`` is unused.
    """
    del task_set, worker_budget, parent_budget
    return None


def _load_model_name() -> str:
    cfg = yaml.safe_load(_CONFIG.read_text())
    return cfg["models"]["worker"]


def _load_temperature() -> float:
    cfg = yaml.safe_load(_CONFIG.read_text())
    return float(cfg["models"]["temperature"])


def _successful_submits(events_path: Path, role: str) -> set[str]:
    """Return tool names that completed without error for given role."""
    submitted: set[str] = set()
    if not events_path.exists():
        return submitted
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("role") == role and ev.get("event_type") == "tool_result":
            payload = ev.get("payload", {})
            if not payload.get("tool_error") and payload.get("tool_name") in (
                "submit_memo", "submit_solution"
            ):
                submitted.add(payload["tool_name"])
    return submitted


def _scan_events_for_forbidden(events_path: Path, raw: Any) -> dict[str, int]:
    """Scan events.jsonl for forbidden field values. Returns field→count."""
    if not events_path.exists():
        return {}
    text = events_path.read_text(encoding="utf-8")
    counts: dict[str, int] = {}
    # Check known confidential fields from _RawTaskRecord
    fields_to_check = {
        "hints_text": getattr(raw, "_hints_text", ""),
        "patch": getattr(raw, "_patch", ""),
        "test_patch": getattr(raw, "_test_patch", ""),
    }
    for field, val in fields_to_check.items():
        if isinstance(val, str) and val.strip() and val in text:
            counts[field] = 1
        elif isinstance(val, list):
            for item in val:
                s = str(item)
                if s and s in text:
                    counts[field] = counts.get(field, 0) + 1
    return counts


# ── Main episode runner ───────────────────────────────────────────────────────

def run_episode(
    task_id: str,
    condition: str = "P1",
    replicate: int = 1,
    *,
    task_set: str = "development",
    run_id: str | None = None,
    runs_dir: Path | None = None,
    client: Any | None = None,
    scripted: bool = False,
    probe_execution: bool = False,
    score_candidate: bool = True,
    score_final: bool = True,
) -> dict[str, Any]:
    """Run one complete SWE-bench P1 delegation episode.

    Parameters
    ----------
    task_id:
        SWE-bench instance ID (e.g. "django__django-14752").
    condition:
        Must be "P1" for this adapter.
    replicate:
        Replicate index (≥ 1).
    task_set:
        "development" or alias "dev".
    run_id:
        Override auto-generated UUID. Must be unique.
    runs_dir:
        Override default data/runs/ directory.

    Returns
    -------
    dict
        Final episode record (same as episode.json).
    """
    if condition not in _ALL_CONDITIONS:
        raise ValueError(
            f"SWE-bench adapter supports conditions {sorted(_ALL_CONDITIONS)}; got {condition!r}"
        )
    _is_a_cell = condition in _A_CONDITIONS  # informational only; parent always runs

    task_set = TASK_SET_ALIASES.get(task_set, task_set)
    if task_set not in _ALLOWED_TASK_SETS:
        raise ValueError(f"Unknown task_set {task_set!r}")

    image_tag = expected_swe_image(task_id)
    if not swe_image_exists(image_tag):
        raise RuntimeError(f"SWE image not present: {image_tag}")
    image_digest = swe_image_digest(image_tag) or _DOCKER_DIGEST
    host_prov = collect_host_provenance(_WORKTREE_ROOT)

    # ── Provenance & run directory ─────────────────────────────────────────
    run_id = run_id or str(uuid.uuid4())
    base_runs = runs_dir or _RUNS_DIR
    run_dir = base_runs / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise RuntimeError(f"run_id={run_id!r} already exists; refusing duplicate run")

    handoff_dir = run_dir / "handoff"
    handoff_dir.mkdir()
    logs_dir = run_dir / "logs"
    logs_dir.mkdir()
    reports_dir = run_dir / "reports"
    reports_dir.mkdir()

    # ── Durable stderr capture ─────────────────────────────────────────────
    # Open a line-buffered log file BEFORE any model/container work so that
    # exceptions or tracebacks printed to stderr are always captured here,
    # even when the launcher redirects stdout/stderr to a single file and the
    # process is killed before the caller's except block can flush.
    # On NORMAL completion we restore sys.stderr; on EXCEPTION we intentionally
    # leave the tee installed so the launcher's traceback.print_exc() writes to
    # the log before sys.exit(1) terminates the process.
    _stderr_log_path = logs_dir / "runner.stderr.log"
    _orig_stderr = sys.stderr
    _stderr_file = open(_stderr_log_path, "w", buffering=1, encoding="utf-8")
    _started_ts_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # Header: run identification only; no secrets/API keys/gold data.
    _stderr_file.write(
        f"[SWE runner stderr] run_id={run_id} task_id={task_id} "
        f"condition={condition} started_at={_started_ts_str}\n"
    )
    _stderr_file.flush()
    sys.stderr = _TeeStderr(_orig_stderr, _stderr_file)

    def _phase_marker(msg: str) -> None:
        """Write a phase marker directly to the stderr log (not to orig stderr)."""
        _ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _stderr_file.write(f"[{_ts}] {msg}\n")
        _stderr_file.flush()

    _phase_marker("phase=init")

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    t_episode_start = time.monotonic()

    # ── Load task (public only for agents; raw for evaluator via private boundary) ──
    public_task = load_task(task_id)
    raw_task = _load_raw(task_id)  # evaluator-only; never passed to agents

    # Build forbidden values dict from raw_task for prompt scanning
    def _build_forbidden_values(raw):
        return {
            "hints_text": raw._hints_text,
            "patch": raw._patch,
            "test_patch": raw._test_patch,
        }

    # ── Schema git commit ──────────────────────────────────────────────────
    schema_git = host_prov["git_head"]

    # ── Construct clients and budgets ──────────────────────────────────────
    # Scripted certification never constructs ModelClient (that path reads .env).
    if client is None:
        if scripted:
            from .scripted_client import ScriptedSWEClient
            client = ScriptedSWEClient(instance_id=task_id)
        else:
            client = ModelClient(config_path=_CONFIG)
    client = ConversationGuard(client)
    worker_budget = _load_budget("worker")
    parent_budget = _load_budget("parent")

    # ── SWE observation cap override for dev and pilot ─────────────────────
    # swe-0.3: loosened observation caps: 80 turns / 160 tool calls per role.
    # Prior swe-0.2 values were 40 turns / 100 tool calls.
    # Wall-clock and output-token caps remain from the manifest.
    swe_observation = apply_observation_caps(task_set, worker_budget, parent_budget)

    # ── Build prompts AFTER budgets are finalised (prompts embed exact cap) ──
    worker_max_turns = worker_budget["max_turns"]
    parent_max_turns = parent_budget["max_turns"]

    worker_sys = _worker_system_prompt(worker_max_turns, condition)
    worker_usr = _worker_user_message(public_task.public_dict())
    check_prompt_excludes_forbidden(worker_sys + "\n" + worker_usr, _build_forbidden_values(raw_task))
    parent_sys = _parent_system_prompt(parent_max_turns)
    model_name = (
        "scripted-swe-0.3.3"
        if scripted or getattr(client, "scripted", False)
        else _load_model_name()
    )
    temperature = _load_temperature()

    # ── Startup stale-container sweep ──────────────────────────────────────
    # Before any new workspace is created, sweep orphaned SWE containers.
    # Only containers with BOTH delegation-study=true AND benchmark=swebench
    # labels that have no episode.json in their run directory are removed.
    # Spreadsheet and other-benchmark containers are never touched.
    _swept = sweep_orphaned_swe_containers(base_runs)
    if _swept:
        import logging as _logging
        _logging.getLogger(__name__).info("Episode startup: swept containers %s", _swept)

    # ── Event loggers ──────────────────────────────────────────────────────
    events_path = run_dir / "events.jsonl"
    event_logger = EventLogger(events_path, run_id)
    private_logger = EventLogger(logs_dir / "worker_trace.jsonl", run_id)

    # Log sweep result if any
    if _swept:
        event_logger.log("system", "failure", {
            "phase": "preflight",
            "reason": f"orphaned_containers_swept",
            "containers": _swept,
        })

    # Track containers for cleanup
    worker_ws: SWEWorkspace | None = None
    parent_ws: SWEWorkspace | None = None

    # Track results
    worker_result = None
    parent_result = None
    candidate_patch: str | None = None
    final_patch: str | None = None
    patch_apply_failed = False
    worker_handoff: dict[str, Any] = {
        "memo_path": None, "memo_sha256": None,
        "candidate_script_path": None, "candidate_script_sha256": None,
        "worker_submitted_memo": False, "worker_submitted_candidate": False,
    }
    worker_feedback_calls = 0
    parent_finalized = False
    failures: list[dict[str, Any]] = []
    worker_outcome: dict | None = None
    final_outcome: dict | None = None
    candidate_score: ScoreResult | None = None
    final_score: ScoreResult | None = None
    # swe-0.2: A1 scratch destruction tracking
    worker_scratch_destroyed: bool = False
    worker_reprompted: int = 0
    worker_end_reason: str | None = None
    parent_reprompted: int = 0
    parent_end_reason: str | None = None
    # swe-0.3: state-seal tracking
    worker_seal_source: str | None = None
    parent_seal_source: str | None = None
    worker_terminal_text: str | None = None
    parent_terminal_text: str | None = None
    worker_sealed_after_reprompt: bool = False
    parent_sealed_after_reprompt: bool = False
    # swe-0.3: production artifact tracking
    candidate_patch_sha256: str | None = None
    final_patch_sha256: str | None = None
    production_artifact_missing: bool = False  # True if P-worker diff was empty at seal
    candidate_report_path: Path | None = None
    final_report_path: Path | None = None
    worker_memo: dict | None = None
    worker_start: dict[str, Any] | None = None
    parent_start: dict[str, Any] | None = None
    infra_failure: dict[str, Any] | None = None

    try:
        # ================================================================
        # WORKER PHASE
        # ================================================================
        _phase_marker(f"phase=worker_start condition={condition}")
        event_logger.log("system", "phase_started", {"phase": "worker", "condition": condition})

        worker_ws = SWEWorkspace.create(
            role="worker", run_id=run_id, runs_dir=base_runs, instance_id=task_id,
        )
        reset_container_to_base(worker_ws.container_name, public_task.base_commit)
        worker_start = inspect_container_start(worker_ws.container_name)
        if hasattr(client, "attach_workspace"):
            client.attach_workspace(
                role="worker",
                condition=condition,
                workspace=worker_ws,
                probe_execution=probe_execution,
            )
        worker_dispatcher = SWEToolDispatcher(
            workspace=worker_ws,
            condition=condition,
            role="worker",
            run_id=run_id,
            handoff_dir=handoff_dir,
            on_submit_memo=lambda m: None,
            events_path=events_path,
            max_turns=worker_max_turns,
        )

        try:
            worker_result = run_swe_agent_phase(
                role="worker",
                condition=condition,
                client=client,
                dispatcher=worker_dispatcher,
                system_prompt=worker_sys,
                initial_user_message=worker_usr,
                budget=worker_budget,
                event_logger=event_logger,
                private_logger=private_logger,
                swe_03_mode=True,
                model_call_text_dir=run_dir,
                tool_output_log_dir=run_dir,
                extract_diff=(
                    worker_ws.extract_diff if worker_ws is not None else None
                ),
                # P-cells: empty-diff reprompt-once. A-cells seal memo immediately.
                apply_empty_diff_rule=condition in _P_CONDITIONS,
            )
        except ProviderContentFilterError as exc:
            infra_failure = content_filter_failure(str(exc))
            failures.append(infra_failure)
            event_logger.log("system", "failure", {
                "phase": "worker",
                "code": PROVIDER_CONTENT_FILTER,
                "reason": PROVIDER_CONTENT_FILTER,
                "rerun_eligible": True,
                "message": str(exc),
            })
            worker_result = AgentPhaseResult()
            worker_result.end_reason = "infrastructure_failure"
            worker_end_reason = "infrastructure_failure"
        except PromptTooLongError as exc:
            infra_failure = prompt_too_long_failure(str(exc))
            failures.append(infra_failure)
            event_logger.log("system", "failure", {
                "phase": "worker",
                "code": PROVIDER_PROMPT_TOO_LONG,
                "reason": PROVIDER_PROMPT_TOO_LONG,
                "rerun_eligible": True,
                "message": str(exc),
            })
            worker_result = AgentPhaseResult()
            worker_result.end_reason = "infrastructure_failure"
            worker_end_reason = "infrastructure_failure"
        event_logger.log("system", "phase_completed", {
            "phase": "worker",
            "terminated_normally": worker_result.terminated_normally,
            "failure_code": worker_result.failure_code,
            "infrastructure_failure": infra_failure["code"] if infra_failure else None,
        })

        worker_feedback_calls = worker_dispatcher.feedback_call_count
        worker_reprompted = worker_result.reprompted_on_end_turn
        worker_end_reason = worker_result.end_reason
        worker_seal_source = worker_result.seal_source
        worker_terminal_text = worker_result.terminal_assistant_text
        worker_sealed_after_reprompt = bool(
            getattr(worker_result, "sealed_after_reprompt", False)
        )

        # swe-0.3: state-sealed artifact export
        # Production workers (P0/P1): export git diff as candidate.patch at seal time.
        # Advisory workers (A0/A1): no candidate patch; discard container changes.
        # Memo: terminal assistant text becomes the memo for all workers.
        # swe-0.3.5: sealed_after_reprompt is a genuine non-submission — no candidate.
        if condition in _P_CONDITIONS and not worker_sealed_after_reprompt:
            # Export git diff deterministically: git diff --binary (committed or not)
            _extract_failed = False
            try:
                _seal_diff = worker_ws.extract_diff() if worker_ws is not None else ""
            except Exception as _e:
                _seal_diff = ""
                _extract_failed = True
                failures.append({
                    "source": "infrastructure",
                    "code": "candidate_diff_extract_failed",
                    "message": f"Could not extract worker diff at seal time: {_e}",
                })
            if _seal_diff and _seal_diff.strip():
                # Write canonical candidate patch
                handoff_dir.mkdir(parents=True, exist_ok=True)
                candidate_path = handoff_dir / "candidate.patch"
                candidate_path.write_text(_seal_diff)
                candidate_patch_sha256 = _sha256_str(_seal_diff)
                event_logger.log("system", "artifact_sealed", {
                    "artifact": "candidate_patch",
                    "path": str(candidate_path),
                    "sha256": candidate_patch_sha256,
                    "seal_source": worker_seal_source,
                })
            else:
                # Empty or unreadable diff: no candidate patch. QC flags
                # production_missing_artifact. Record as an agent-source
                # failure only when extract succeeded (true empty tree).
                production_artifact_missing = True
                event_logger.log("system", "production_artifact_missing", {
                    "phase": "worker",
                    "condition": condition,
                    "reason": "extract_failed" if _extract_failed else "empty_diff_at_seal",
                    "seal_source": worker_seal_source,
                })
                if not _extract_failed:
                    failures.append({
                        "source": "agent",
                        "code": "production_artifact_missing",
                        "message": (
                            f"Worker {condition} diff was empty at seal time "
                            f"(no changes to /testbed)"
                        ),
                    })
        elif condition in _P_CONDITIONS and worker_sealed_after_reprompt:
            production_artifact_missing = True
            event_logger.log("system", "failure", {
                "code": SEALED_AFTER_REPROMPT,
                "phase": "worker",
                "condition": condition,
                "worker_submitted": False,
            })

        # Build memo from terminal text for all workers (swe-0.3)
        # Map terminal assistant text into memo.task_understanding; other fields empty.
        # This is valid even when terminal_text is empty (tool-only response).
        _memo_text = worker_terminal_text or ""
        _memo_from_text = {
            "task_understanding": _memo_text,
            "target_locations": [],
            "proposed_approach": "",
            "assumptions": [],
            "observed_feedback": [],
            "known_risks": [],
            "validation_plan": [],
        }
        # Preserve exact terminal text separately with hash if non-empty
        _terminal_text_sha = _sha256_str(_memo_text) if _memo_text else None
        handoff_dir.mkdir(parents=True, exist_ok=True)
        _memo_path = handoff_dir / "memo.json"
        _memo_json = json.dumps(_memo_from_text, indent=2) + "\n"
        _memo_path.write_text(_memo_json)
        if _memo_text:
            # Preserve exact terminal text separately
            _terminal_text_path = handoff_dir / "terminal_text.txt"
            _terminal_text_path.write_text(_memo_text)
        event_logger.log("system", "memo_sealed", {
            "memo_source": "terminal_text",
            "terminal_text_sha256": _terminal_text_sha,
            "seal_source": worker_seal_source,
            "condition": condition,
        })

        # swe-0.2 compat: seal handoff (reads memo.json and candidate.patch from handoff_dir)
        worker_handoff = worker_dispatcher.seal_handoff()

        # Read candidate patch from handoff dir
        candidate_path = handoff_dir / "candidate.patch"
        if candidate_path.exists():
            candidate_patch = candidate_path.read_text()

        # Read worker memo
        memo_path = handoff_dir / "memo.json"
        if memo_path.exists():
            worker_memo = json.loads(memo_path.read_text())

        if worker_result.failure_code and not worker_result.terminated_normally:
            failures.append({
                "source": "agent",
                "code": f"worker_{worker_result.failure_code}",
                "message": worker_result.failure_message or "",
            })

    finally:
        if worker_ws is not None:
            try:
                worker_ws.cleanup()
                # swe-0.2: record scratch destroyed for A1 (all conditions destroy
                # container, but only A1 requires verification for QC)
                worker_scratch_destroyed = True
                event_logger.log("system", "scratch_destroyed", {
                    "phase": "worker",
                    "condition": condition,
                    "container": worker_ws.container_name,
                })
            except Exception as e:
                worker_scratch_destroyed = False
                event_logger.log("system", "failure", {"phase": "worker", "error": str(e)})

    # ================================================================
    # CANDIDATE EVALUATION (offline, using evaluator-only raw task)
    # ================================================================
    if infra_failure is None and candidate_patch and score_candidate:
        try:
            t_cand_start = time.monotonic()
            candidate_score = score_patch(
                instance_id=task_id,
                patch_bytes=candidate_patch.encode(),
                run_id=f"{run_id}_candidate",
                model_name="swe_eval",
            )
            cand_elapsed = time.monotonic() - t_cand_start
            worker_outcome = _build_swe_outcome(candidate_score, reports_dir, "candidate")
            if candidate_score and candidate_score.report_path:
                candidate_report_path = reports_dir / "candidate_report.json"
        except Exception as e:
            worker_outcome = _build_swe_outcome(None, reports_dir, "candidate")
            failures.append({
                "source": "infrastructure",
                "code": "candidate_eval_failed",
                "message": f"Candidate evaluation error: {type(e).__name__}",
            })
    elif candidate_patch:
        worker_outcome = _skipped_eval_outcome(reports_dir, "candidate")
    else:
        # No candidate patch (A0/A1 conditions or worker did not submit).
        # Schema allows worker_outcome = null when no evaluation was performed.
        worker_outcome = None

    # ================================================================
    # PARENT PHASE  (always runs in every condition: A0, A1, P0, P1)
    #
    # A-cells transfer problem_statement + memo; no candidate patch is
    # mounted (A-cell workers lack submit_solution so candidate_patch is
    # None).  P-cells additionally have the candidate patch pre-applied.
    # The parent prompt is byte-identical across all conditions.
    # ================================================================
    _phase_marker(f"phase=parent_start condition={condition}")
    event_logger.log("system", "phase_started", {"phase": "parent", "condition": condition})
    parent_prompt_hash = hashlib.sha256(parent_sys.encode()).hexdigest()

    try:
        if infra_failure is not None:
            event_logger.log("system", "phase_completed", {
                "phase": "parent",
                "terminated_normally": False,
                "failure_code": infra_failure.get("code") or PROVIDER_CONTENT_FILTER,
                "skipped": True,
            })
            raise _ParentSkipped()
        parent_ws = SWEWorkspace.create(
            role="parent", run_id=run_id, runs_dir=base_runs, instance_id=task_id,
        )
        reset_container_to_base(parent_ws.container_name, public_task.base_commit)
        parent_start = inspect_container_start(parent_ws.container_name)
        if hasattr(client, "attach_workspace"):
            client.attach_workspace(
                role="parent",
                condition=condition,
                workspace=parent_ws,
                probe_execution=probe_execution,
            )
        parent_handoff_dir = run_dir / "parent_handoff"
        parent_handoff_dir.mkdir()

        parent_dispatcher = SWEToolDispatcher(
            workspace=parent_ws,
            condition=condition,
            role="parent",
            run_id=run_id,
            handoff_dir=parent_handoff_dir,
            events_path=events_path,
            max_turns=parent_max_turns,
        )

        # Apply candidate patch to parent container if available
        if candidate_patch:
            success, msg = parent_ws.apply_patch(candidate_patch)
            if not success:
                patch_apply_failed = True
                event_logger.log("system", "failure", {
                    "phase": "parent",
                    "reason": msg,
                })
                failures.append({
                    "source": "infrastructure",
                    "code": "failure",
                    "message": "Candidate patch did not apply cleanly in parent container",
                })

        parent_usr = _parent_user_message(
            public_task.public_dict(),
            worker_memo,
            candidate_patch,
        )
        # Verify parent message excludes forbidden values
        check_prompt_excludes_forbidden(parent_sys + "\n" + parent_usr, _build_forbidden_values(raw_task))

        parent_prompt_hash = hashlib.sha256(
            (parent_sys + "\n" + parent_usr).encode()
        ).hexdigest()

        try:
            parent_result = run_swe_agent_phase(
                role="parent",
                condition=condition,
                client=client,
                dispatcher=parent_dispatcher,
                system_prompt=parent_sys,
                initial_user_message=parent_usr,
                budget=parent_budget,
                event_logger=event_logger,
                private_logger=None,
                swe_03_mode=True,
                model_call_text_dir=run_dir,
                tool_output_log_dir=run_dir,
                extract_diff=(
                    parent_ws.extract_diff if parent_ws is not None else None
                ),
                apply_empty_diff_rule=True,
            )
        except ProviderContentFilterError as exc:
            infra_failure = content_filter_failure(str(exc))
            failures.append(infra_failure)
            event_logger.log("system", "failure", {
                "phase": "parent",
                "code": PROVIDER_CONTENT_FILTER,
                "reason": PROVIDER_CONTENT_FILTER,
                "rerun_eligible": True,
                "message": str(exc),
            })
            parent_result = AgentPhaseResult()
            parent_result.end_reason = "infrastructure_failure"
            parent_end_reason = "infrastructure_failure"
        except PromptTooLongError as exc:
            infra_failure = prompt_too_long_failure(str(exc))
            failures.append(infra_failure)
            event_logger.log("system", "failure", {
                "phase": "parent",
                "code": PROVIDER_PROMPT_TOO_LONG,
                "reason": PROVIDER_PROMPT_TOO_LONG,
                "rerun_eligible": True,
                "message": str(exc),
            })
            parent_result = AgentPhaseResult()
            parent_result.end_reason = "infrastructure_failure"
            parent_end_reason = "infrastructure_failure"
        event_logger.log("system", "phase_completed", {
            "phase": "parent",
            "terminated_normally": parent_result.terminated_normally,
            "failure_code": parent_result.failure_code,
            "infrastructure_failure": infra_failure["code"] if infra_failure else None,
        })

        parent_reprompted = parent_result.reprompted_on_end_turn
        parent_end_reason = parent_result.end_reason
        parent_seal_source = parent_result.seal_source
        parent_terminal_text = parent_result.terminal_assistant_text
        parent_sealed_after_reprompt = bool(
            getattr(parent_result, "sealed_after_reprompt", False)
        )

        # swe-0.3.5 genuine non-submission: no final.patch, ITT 0.
        if parent_sealed_after_reprompt:
            parent_finalized = False
            final_patch = None
            event_logger.log("system", "failure", {
                "code": SEALED_AFTER_REPROMPT,
                "phase": "parent",
                "parent_finalized": False,
            })
        else:
            # swe-0.3: export final.patch from git diff at seal time
            # Parent always exports a final diff (even if empty, for honest evaluation).
            try:
                _parent_diff = parent_ws.extract_diff() if parent_ws is not None else ""
            except Exception as _e:
                _parent_diff = ""
                failures.append({
                    "source": "infrastructure",
                    "code": "final_diff_extract_failed",
                    "message": f"Could not extract parent diff at seal time: {_e}",
                })

            if _parent_diff and _parent_diff.strip():
                dest = handoff_dir / "final.patch"
                dest.write_text(_parent_diff)
                final_patch = _parent_diff
                final_patch_sha256 = _sha256_str(_parent_diff)
                parent_finalized = True
                final_path = dest
                event_logger.log("system", "artifact_sealed", {
                    "artifact": "final_solution",
                    "path": str(dest),
                    "sha256": final_patch_sha256,
                    "seal_source": parent_seal_source,
                })
            else:
                # Empty final diff: write an empty file for traceability and
                # evaluate as unresolved (not_finalized). Do NOT emit
                # artifact_sealed — the validator requires that event iff
                # compliance.parent_finalized is True.
                dest = handoff_dir / "final.patch"
                dest.write_text("")
                final_patch = ""
                final_patch_sha256 = _sha256_str("")
                final_path = dest
                event_logger.log("system", "failure", {
                    "phase": "parent",
                    "code": "empty_final_diff",
                    "seal_source": parent_seal_source,
                    "note": "empty_diff_evaluated_unresolved",
                })

        parent_sealed = parent_dispatcher.seal_handoff()

        if parent_result.failure_code and not parent_result.terminated_normally:
            failures.append({
                "source": "agent",
                "code": f"parent_{parent_result.failure_code}",
                "message": parent_result.failure_message or "",
            })

    except _ParentSkipped:
        pass
    finally:
        if parent_ws is not None:
            try:
                parent_ws.cleanup()
            except Exception as e:
                event_logger.log("system", "failure", {"phase": "parent", "error": str(e)})

    # ================================================================
    # FINAL EVALUATION
    # ================================================================
    if final_patch and score_final:
        try:
            t_final_start = time.monotonic()
            final_score = score_patch(
                instance_id=task_id,
                patch_bytes=final_patch.encode(),
                run_id=f"{run_id}_final",
                model_name="swe_eval",
            )
            final_elapsed = time.monotonic() - t_final_start
            final_outcome = _build_swe_outcome(final_score, reports_dir, "final")
            if final_score and final_score.report_path:
                final_report_path = reports_dir / "final_report.json"
        except Exception as e:
            final_outcome = _build_swe_outcome(None, reports_dir, "final")
            failures.append({
                "source": "infrastructure",
                "code": "final_eval_failed",
                "message": f"Final evaluation error: {type(e).__name__}",
            })
    elif final_patch:
        final_outcome = _skipped_eval_outcome(reports_dir, "final")
    else:
        # No final patch — schema requires final_outcome to be set.
        # "not_finalized" is valid only for final_outcome (not worker_outcome).
        # The validator still requires evaluator_path for all complete outcomes,
        # so write a minimal summary file.
        reports_dir.mkdir(parents=True, exist_ok=True)
        _nf_summary = {
            "evaluator_status": "not_finalized",
            "full_task_success": 0,
            "target_unit_pass_rate": 0.0,
            "n_units": 0,
            "output_sha256": None,
        }
        _nf_summary_path = reports_dir / "final_eval_summary.json"
        _nf_summary_path.write_text(json.dumps(_nf_summary))
        final_outcome = {
            "evaluator_status": "not_finalized",
            "full_task_success": 0,
            "target_unit_pass_rate": 0.0,
            "n_units": 0,
            "output_path": None,
            "evaluator_path": str(_nf_summary_path),
            "output_sha256": None,
        }

    # Emit evaluation event required by the shared validator.
    # Payload must contain evaluator_status and n_units matching final_outcome.
    event_logger.log("system", "evaluation", {
        "phase": "final",
        "evaluator_status": final_outcome.get("evaluator_status"),
        "n_units": final_outcome.get("n_units"),
        "full_task_success": final_outcome.get("full_task_success"),
    })

    # ================================================================
    # BUILD EPISODE RECORD
    # ================================================================
    completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    wall_seconds = time.monotonic() - t_episode_start

    # Prompt hashes
    worker_prompt_hash = hashlib.sha256(
        (worker_sys + "\n" + worker_usr).encode()
    ).hexdigest()
    # Parent prompt hash computed above inside the parent try block.
    # Fallback: if an exception occurred before parent_prompt_hash was set.
    if 'parent_prompt_hash' not in dir():
        parent_prompt_hash = hashlib.sha256(parent_sys.encode()).hexdigest()

    # Usage per role
    def _usage_block(res) -> dict:
        """Build the role_usage dict required by episode_schema.json."""
        if res is None:
            return {
                "model_calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                "tool_calls": 0, "validation_cycles": 0,
                "wall_seconds": 0.0,
                "budget_exhausted": False, "budget_exhausted_on": None,
                "safety_exhausted": False, "safety_exhausted_on": None,
            }
        return {
            "model_calls": res.model_calls,
            "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens,
            "cache_read_input_tokens": res.cache_read_input_tokens,
            "cache_creation_input_tokens": res.cache_creation_input_tokens,
            "tool_calls": res.tool_calls,
            "validation_cycles": getattr(res, "validation_cycles", 0),
            "wall_seconds": round(getattr(res, "wall_seconds", 0.0), 2),
            "budget_exhausted": getattr(res, "budget_exhausted", False),
            "budget_exhausted_on": getattr(res, "budget_exhausted_on", None),
            "safety_exhausted": getattr(res, "safety_exhausted", False),
            "safety_exhausted_on": getattr(res, "safety_exhausted_on", None),
        }

    # Scan events for forbidden field leakage (field names only, no values)
    forbidden_counts = _scan_events_for_forbidden(events_path, raw_task)
    if forbidden_counts:
        event_logger.log("system", "failure", {
            "fields_found": list(forbidden_counts.keys()),
            "counts": list(forbidden_counts.values()),
        })

    # Final patch path for schema field
    final_patch_path = str(handoff_dir / "final.patch") if (handoff_dir / "final.patch").exists() else None
    final_patch_sha = _sha256_file(Path(final_patch_path)) if final_patch_path else None

    # Compute harness seconds from ScoreResult.wall_seconds if available
    _cand_wall = candidate_score.wall_seconds if candidate_score else None
    _final_wall = final_score.wall_seconds if final_score else None

    # Derive report paths from outcome dicts (set by _build_swe_outcome).
    # Using outcome["output_path"] avoids redundant re-computation and ensures
    # the paths are consistent with what the validator cross-checks.
    _cand_rpt = Path(worker_outcome["output_path"]) if (worker_outcome and worker_outcome.get("output_path")) else None
    _final_rpt = Path(final_outcome["output_path"]) if final_outcome.get("output_path") else None

    # swe-0.2: extract F2P/P2P fractions from saved final harness report.
    # Fraction = success/(success+failure) from tests_status counts (no test names).
    # Null when harness report absent, denominator zero, or schema_v2_no_tests_status.
    # Use FINAL patch metrics for the primary env_metrics fields.
    # Candidate metrics stored separately below for full audit trail.
    _final_f2p = final_score.f2p_fraction if final_score else None
    _final_p2p = final_score.p2p_fraction if final_score else None

    env_swe = {
        "fail_to_pass_fraction": _final_f2p,
        "pass_to_pass_fraction": _final_p2p,
        "f2p_numerator": final_score.f2p_numerator if final_score else None,
        "f2p_denominator": final_score.f2p_denominator if final_score else None,
        "p2p_numerator": final_score.p2p_numerator if final_score else None,
        "p2p_denominator": final_score.p2p_denominator if final_score else None,
        "f2p_p2p_null_reason": final_score.f2p_p2p_null_reason if final_score else "no_final_evaluation",
        "applies_cleanly": not patch_apply_failed and candidate_patch is not None,
        "candidate_resolved": (
            None if (not candidate_score or candidate_score.infra_failure)
            else int(candidate_score.resolved)
        ),
        "final_resolved": (
            None if (not final_score or final_score.infra_failure)
            else int(final_score.resolved)
        ),
        "candidate_harness_seconds": round(_cand_wall, 2) if _cand_wall is not None else None,
        "final_harness_seconds": round(_final_wall, 2) if _final_wall is not None else None,
        "candidate_report_sha256": (
            _sha256_file(_cand_rpt)[:16] if (_cand_rpt and _cand_rpt.is_file()) else None
        ),
        "final_report_sha256": (
            _sha256_file(_final_rpt)[:16] if (_final_rpt and _final_rpt.is_file()) else None
        ),
        # Candidate F2P/P2P (optional, separately namespaced to avoid mixing with final)
        "candidate_f2p_fraction": candidate_score.f2p_fraction if candidate_score else None,
        "candidate_p2p_fraction": candidate_score.p2p_fraction if candidate_score else None,
        # swe-0.3.3 provenance — container task repo (not the study checkout)
        "image_tag": image_tag,
        "image_digest": image_digest,
        "git_head": (worker_start or {}).get("head") or host_prov["git_head"],
        "git_remote": (
            ((worker_start or {}).get("remote") or "").strip()
            or f"(no remotes; task_repo={public_task.repo})"
        ),
        "task_repo": public_task.repo,
        "clean_tree": (worker_start or {}).get("clean"),
        "worker_container_id": (worker_start or {}).get("container_id"),
        "parent_container_id": (parent_start or {}).get("container_id"),
        "worker_start_commit": (worker_start or {}).get("head"),
        "parent_start_commit": (parent_start or {}).get("head"),
        "worker_start_clean": (worker_start or {}).get("clean"),
        "parent_start_clean": (parent_start or {}).get("clean"),
        "worker_start_remote": (worker_start or {}).get("remote"),
        "parent_start_remote": (parent_start or {}).get("remote"),
    }

    # Determine overall status
    # Primary resolved means final resolved=1
    primary_resolved = bool(final_score and final_score.resolved)
    # Non-finalize => 0 (ITT)
    status = "infrastructure_failure" if infra_failure else "complete"

    record: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "task_set": task_set,
        "condition": condition,
        "protocol_version": PROTOCOL_VERSION,
        "replicate": replicate,
        "started_at": started_at,
        "completed_at": completed_at,
        "benchmark": BENCHMARK,
        "reproducibility": {
            "schema_git_commit": schema_git,
            "benchmark_git_commit": public_task.base_commit,
            "dataset_archive_sha256": _SELECTION_SHA,
            "docker_image_digest": image_digest,
            "worker_model": model_name,
            "parent_model": model_name,
            "temperature": temperature,
            "git_dirty": bool(host_prov["git_dirty"]),
            "workspace_header_used": False,
            "worker_prompt_hash": worker_prompt_hash,
            "parent_prompt_hash": parent_prompt_hash,
            "libreoffice_version": None,  # N/A for SWE-bench
            "task_selection_package": _SELECTION_PACKAGE,
            "selection_sha256": _SELECTION_SHA,
        },
        "budgets": {
            "worker": worker_budget,
            "parent": parent_budget,
        },
        "usage": {
            "worker": _usage_block(worker_result),
            "parent": _usage_block(parent_result),
        },
        "handoff": worker_handoff,
        "worker_outcome": worker_outcome,
        "final_outcome": final_outcome,
        "unsealed_last_script_score": None,  # N/A for SWE
        "failures": failures,
        "compliance": {
            "artifact_crossed_handoff": candidate_patch is not None,
            "worker_feedback_calls": worker_feedback_calls,
            "parent_finalized": parent_finalized,
            # swe-0.2: worker_scratch_destroyed=True when A1 container was removed
            # before parent phase. True for all conditions (container always destroyed);
            # QC only checks A1. Derived from scratch_destroyed event in events.jsonl.
            "worker_scratch_destroyed": worker_scratch_destroyed,
            "worker_submitted": worker_handoff.get("worker_submitted_candidate", False),
        },
        # swe-0.3.5: per-role state-seal + empty-diff reprompt-once
        "swe_protocol_usage": {
            "worker": {
                "reprompted_on_end_turn": worker_reprompted,
                "end_reason": worker_end_reason,
                "seal_source": worker_seal_source,
                "memo_source": "terminal_text",
                "sealed_after_reprompt": worker_sealed_after_reprompt,
                "continued_on_max_tokens": (
                    worker_result.continued_on_max_tokens if worker_result else 0
                ),
                "unexpected_stop_reason": (
                    worker_result.unexpected_stop_reason if worker_result else None
                ),
            },
            "parent": {
                "reprompted_on_end_turn": parent_reprompted,
                "end_reason": parent_end_reason,
                "seal_source": parent_seal_source,
                "sealed_after_reprompt": parent_sealed_after_reprompt,
                "continued_on_max_tokens": (
                    parent_result.continued_on_max_tokens if parent_result else 0
                ),
                "unexpected_stop_reason": (
                    parent_result.unexpected_stop_reason if parent_result else None
                ),
            },
            "caps_loosened": True,
            "prior_swe02_max_turns": SWE_OBS_PRIOR_TURNS,
            "prior_swe02_max_tool_calls": SWE_OBS_PRIOR_TOOL_CALLS,
            "production_artifact_missing": production_artifact_missing,
        },
        "final_script_path": final_patch_path,
        "final_script_sha256": final_patch_sha,
        "accepted_unchanged": False,
        "status": status,
        "analysis_included": False,
        "analysis_inclusion_reason": (
            f"infrastructure_failure:{infra_failure['code']}"
            if infra_failure
            else "benchmark=swebench"
        ),
        **({"rerun_eligible": True} if infra_failure else {}),
        "env_metrics": {"swebench": env_swe},
    }

    # Record SWE observation cap override when applicable
    if swe_observation is not None:
        record["swe_observation"] = swe_observation

    # Write episode atomically
    episode_path = run_dir / "episode.json"
    atomic_write_json(episode_path, record)

    # Validate
    issues = validate_episode(record, episode_path)
    validation_passed = not issues
    if not validation_passed:
        event_logger.log("system", "failure", {
            "count": len(issues),
            "paths": [str(i) for i in issues[:5]],
        })

    # QC
    qc_flags = qc_episode(record, record_path=episode_path)
    qc_clean = not qc_flags

    record["_validation_passed"] = validation_passed
    record["_qc_flags"] = [{"code": f.code, "message": f.message} for f in qc_flags]
    record["_wall_seconds"] = round(wall_seconds, 1)

    # ── Restore stderr on CLEAN completion ─────────────────────────────────
    # On exception we intentionally do NOT restore here so the launcher's
    # except/traceback.print_exc() still writes to the run-local log file.
    _phase_marker("phase=complete")
    sys.stderr = _orig_stderr
    try:
        _stderr_file.flush()
        _stderr_file.close()
    except Exception:
        pass

    return record


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one SWE-bench P1 episode")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--condition", default="P1")
    parser.add_argument("--replicate", type=int, default=1)
    parser.add_argument("--task-set", default="development")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument(
        "--scripted",
        action="store_true",
        help="Use the in-process scripted client (no Anthropic API).",
    )
    parser.add_argument(
        "--probe-execution",
        action="store_true",
        help="Scripted client attempts pytest (for ±F cell smoke).",
    )
    args = parser.parse_args(argv)

    record = run_episode(
        task_id=args.task_id,
        condition=args.condition,
        replicate=args.replicate,
        task_set=args.task_set,
        run_id=args.run_id,
        runs_dir=args.runs_dir,
        scripted=args.scripted,
        probe_execution=args.probe_execution,
    )

    print(json.dumps({
        "run_id": record["run_id"],
        "status": record["status"],
        "validation": record.get("_validation_passed"),
        "qc_flags": record.get("_qc_flags"),
        "final_resolved": record.get("env_metrics", {}).get("swebench", {}).get("final_resolved"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
