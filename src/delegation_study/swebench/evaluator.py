"""SWE-bench no-LLM evaluator path.

Handles:
1. Applying a candidate patch inside a fresh SWE-bench container (with network none).
2. Extracting the resulting ``git diff --binary`` from the container.
3. Running the official harness against a prediction file.
4. Parsing harness reports safely (counts and resolved-flag only; no test names).

Confidentiality invariants
--------------------------
- Patch bytes never appear on command lines, in logs, or in exception text.
- Prediction JSONL files and patch temp files are chmod 0600 immediately on
  creation and deleted in ``finally`` blocks before the function returns.
- Only sanitized report JSON (instance IDs, counts, resolved flag) is kept.
- ``applies_cleanly`` is a boolean evidence flag with no patch content.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from delegation_study.swebench.container import (
    force_remove_container,
    stop_swe_container,
    verify_no_containers,
)

# ---------------------------------------------------------------------------
# Image and harness constants
# ---------------------------------------------------------------------------

SWE_IMAGE = "swebench/sweb.eval.x86_64.django_1776_django-14752:latest"

# Path to the swebench harness Python interpreter (installed in third_party).
# Resolved relative to this file's location in the worktree.
_WORKTREE_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
HARNESS_PYTHON = pathlib.Path(
    "<REPO>"
)

# Local SWE-bench dataset file (via ignored symlink in the worktree).
# Used by task_loader; NOT used directly by the v5 harness (which needs v5 fields).
LOCAL_DATASET = _WORKTREE_ROOT / "config" / "task_selection_v03" / "swe_verified_local.json"

# HuggingFace dataset name for the v5 harness (has eval_script, image, etc. fields).
# Must match what was used for gate0 validation (see data/dev/swe/gate0.md).
HF_DATASET_NAME = "SWE-bench/SWE-bench_Verified"

# Where sanitized harness reports are written (gitignored).
REPORT_DIR = _WORKTREE_ROOT / "data" / "dev" / "swe" / "reports"

# swe-0.3.5: harness evaluation wall. Timeout is retried once, then the
# outcome is evaluator_failure (missing, never scored 0). Exported so the
# loop can import the constant without owning the timeout policy.
HARNESS_EVAL_TIMEOUT_SECONDS = 1800
HARNESS_TIMEOUT_RETRIES = 1


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    """Result of applying a patch inside a SWE-bench container."""
    applies_cleanly: bool          # True iff git apply --check AND git apply both exit 0
    check_rc: int | None           # exit code of git apply --check
    apply_rc: int | None           # exit code of git apply
    diff_bytes: bytes              # stdout of git diff --binary (empty if apply failed)
    wall_seconds: float

    @property
    def diff_sha256(self) -> str:
        """SHA-256 hex digest of the extracted diff (for logging without content)."""
        return hashlib.sha256(self.diff_bytes).hexdigest()


@dataclass
class ScoreResult:
    """Sanitized result of one harness evaluation run.

    F2P/P2P fractions are computed from test counts only — no test names are
    stored or exposed.  When tests_status is absent from the report (schema v2),
    all fraction fields are None with an explicit null_reason.
    """
    instance_id: str
    run_id: str
    resolved: bool
    completed: bool
    infra_failure: bool
    empty_patch: bool
    total_instances: int
    wall_seconds: float
    report_path: pathlib.Path
    report_sha256: str             # SHA-256 of report JSON (for evidence, no test names)

    # swe-0.2: F2P/P2P fractions from tests_status counts (None when unavailable)
    f2p_fraction: float | None = None      # success/(success+failure)
    p2p_fraction: float | None = None
    f2p_numerator: int | None = None
    f2p_denominator: int | None = None     # None when no tests_status; 0 when empty
    p2p_numerator: int | None = None
    p2p_denominator: int | None = None
    f2p_p2p_null_reason: str | None = None  # why fractions are null if they are

    @property
    def summary(self) -> dict[str, Any]:
        """Sanitized summary dict — safe for logging."""
        return {
            "instance_id": self.instance_id,
            "run_id": self.run_id,
            "resolved": self.resolved,
            "completed": self.completed,
            "infra_failure": self.infra_failure,
            "empty_patch": self.empty_patch,
            "total_instances": self.total_instances,
            "wall_seconds": self.wall_seconds,
            "report_sha256_prefix": self.report_sha256[:16],
            "f2p_fraction": self.f2p_fraction,
            "p2p_fraction": self.p2p_fraction,
        }


# ---------------------------------------------------------------------------
# Container-level patch apply
# ---------------------------------------------------------------------------

def apply_patch_in_container(
    container_name: str,
    patch_bytes: bytes,
    *,
    workdir: str = "/testbed",
    timeout: int = 60,
) -> ApplyResult:
    """Apply *patch_bytes* inside the running *container_name* via git apply.

    Strategy:
      1. Write patch to a host temp file (chmod 0600) — never on command line.
      2. ``docker cp`` the temp file into the container.
      3. ``docker exec git apply --check`` (dry-run).
      4. ``docker exec git apply`` (actual apply).
      5. ``docker exec git diff --binary`` → capture stdout as bytes.
      6. Delete host temp file in finally.

    Parameters
    ----------
    container_name:
        Name of a running SWE-bench container.
    patch_bytes:
        Patch content as bytes.  Never logged or printed.
    workdir:
        Directory inside the container where git commands run.
    timeout:
        Timeout for each docker exec call.

    Returns
    -------
    ApplyResult
        ``applies_cleanly`` is True iff both git apply calls exit 0.
    """
    tmp_dir = tempfile.mkdtemp(prefix="swe_patch_")
    host_patch = pathlib.Path(tmp_dir) / "candidate.patch"
    container_patch = "/tmp/candidate.patch"

    t0 = time.monotonic()
    check_rc: int | None = None
    apply_rc: int | None = None
    diff_bytes = b""

    try:
        # Write patch bytes to chmod-0600 temp file on host.
        host_patch.write_bytes(patch_bytes)
        os.chmod(host_patch, 0o600)

        # Copy into container (no command-line content).
        cp_result = subprocess.run(
            ["docker", "cp", str(host_patch), f"{container_name}:{container_patch}"],
            capture_output=True, text=True, timeout=30,
        )
        if cp_result.returncode != 0:
            return ApplyResult(
                applies_cleanly=False,
                check_rc=None, apply_rc=None,
                diff_bytes=b"",
                wall_seconds=time.monotonic() - t0,
            )

        # git apply --check (dry-run, does not modify working tree).
        chk = subprocess.run(
            ["docker", "exec", "--workdir", workdir, container_name,
             "git", "apply", "--check", container_patch],
            capture_output=True, timeout=timeout,
        )
        check_rc = chk.returncode

        if check_rc == 0:
            # Actual apply.
            app = subprocess.run(
                ["docker", "exec", "--workdir", workdir, container_name,
                 "git", "apply", container_patch],
                capture_output=True, timeout=timeout,
            )
            apply_rc = app.returncode

            if apply_rc == 0:
                # Extract diff --binary through structured capture (not shell redirect).
                diff_proc = subprocess.run(
                    ["docker", "exec", "--workdir", workdir, container_name,
                     "git", "diff", "--binary"],
                    capture_output=True, timeout=timeout,
                )
                diff_bytes = diff_proc.stdout  # bytes, never logged

    finally:
        # Delete host patch file immediately.
        try:
            host_patch.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass

    applies_cleanly = (check_rc == 0 and apply_rc == 0)
    return ApplyResult(
        applies_cleanly=applies_cleanly,
        check_rc=check_rc,
        apply_rc=apply_rc,
        diff_bytes=diff_bytes,
        wall_seconds=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Official harness evaluation
# ---------------------------------------------------------------------------

def _expected_image(instance_id: str, namespace: str = "swebench") -> str:
    """Compute the Docker image name the harness derives for *instance_id*.

    Matches the formula in ``swebench.task.checks.expected_image``.
    """
    key = f"sweb.eval.x86_64.{instance_id}:latest".lower()
    return f"{namespace}/{key}".replace("__", "_1776_")


def _make_augmented_dataset(
    instance_id: str,
    data_path: pathlib.Path,
    tmp_dir: pathlib.Path,
) -> pathlib.Path:
    """Write a temp dataset JSON containing *instance_id* augmented with 'image'.

    The harness v5 requires an ``"image"`` field in dataset records.
    Our local JSON doesn't have it; we add it here at runtime.

    The temp file is chmod 0600 (contains evaluator-only fields like gold patch).
    Caller is responsible for deletion.
    """
    with data_path.open("r", encoding="utf-8") as fh:
        all_records: list[dict] = json.load(fh)
    target = [r for r in all_records if r.get("instance_id") == instance_id]
    if not target:
        raise KeyError(f"instance_id {instance_id!r} not found in {data_path}")
    rec = dict(target[0])
    rec["image"] = _expected_image(instance_id)

    aug_path = tmp_dir / "dataset.json"
    aug_path.write_bytes(json.dumps([rec], indent=2).encode("utf-8"))
    os.chmod(aug_path, 0o600)
    return aug_path


def _score_is_timeout(score: ScoreResult) -> bool:
    """True when the harness wall or ``tests_timed_out`` fired."""
    if score.f2p_p2p_null_reason == "tests_timed_out":
        return True
    reason = score.f2p_p2p_null_reason or ""
    return score.infra_failure and (
        "timed_out" in reason or reason.endswith("timeout")
    )


def _timeout_score_result(
    instance_id: str,
    run_id: str,
    *,
    report_dir: pathlib.Path,
    wall_seconds: float,
    model_name: str,
) -> ScoreResult:
    """Write a sanitized timeout report and parse it as infra/evaluator failure."""
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "total_instances": 1,
        "submitted_instances": 1,
        "completed_instances": 0,
        "resolved_instances": 0,
        "unresolved_instances": 0,
        "infra_failure_instances": 0,
        "ambiguous_failure_instances": 1,
        "empty_patch_instances": 0,
        "error_instances": 1,
        "completed_ids": [],
        "incomplete_ids": [],
        "empty_patch_ids": [],
        "submitted_ids": [instance_id],
        "resolved_ids": [],
        "unresolved_ids": [],
        "infra_failure_ids": [],
        "ambiguous_failure_ids": [instance_id],
        "failure_reasons": {instance_id: "tests_timed_out"},
        "error_ids": [instance_id],
        "schema_version": 2,
        "unstopped_instances": 0,
        "unstopped_containers": [],
        "unremoved_images": [],
    }
    report_path = report_dir / f"{model_name.replace('/', '__')}.{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return _parse_report_safe(report_path, instance_id, run_id, wall_seconds)


def _run_harness_once(
    instance_id: str,
    patch_bytes: bytes,
    run_id: str,
    *,
    model_name: str,
    report_dir: pathlib.Path,
    harness_python: pathlib.Path | None,
    timeout: int,
) -> ScoreResult:
    """One official-harness invocation.  Raises ``TimeoutExpired`` on the wall."""
    dataset_name = HF_DATASET_NAME
    harness_py = harness_python or HARNESS_PYTHON
    report_dir.mkdir(parents=True, exist_ok=True)

    work_dir = pathlib.Path(tempfile.mkdtemp(prefix="swe_work_"))
    pred_path = work_dir / f"pred_{uuid.uuid4().hex[:8]}.jsonl"

    t0 = time.monotonic()
    try:
        # Write prediction JSONL (chmod 0600) — patch bytes never on command line.
        pred_path.write_bytes(
            json.dumps({
                "instance_id": instance_id,
                "model_patch": patch_bytes.decode("utf-8", errors="replace"),
                "model_name_or_path": model_name,
            }).encode("utf-8")
        )
        os.chmod(pred_path, 0o600)

        # Run harness from a temp working dir to avoid polluting the worktree.
        run_cwd = work_dir / "harness_run"
        run_cwd.mkdir()
        harness_cmd = [
            str(harness_py),
            "-m", "swebench.harness.run_evaluation",
            "--dataset_name", dataset_name,
            "--instance_ids", instance_id,
            "--predictions_path", str(pred_path),
            "--run_id", run_id,
            "--report_dir", str(report_dir),
            "--max_workers", "1",
            "--timeout", str(int(timeout)),
        ]
        subprocess.run(
            harness_cmd,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            cwd=str(run_cwd),
        )
        # Harness exit code is informational; we check the report file instead.

    finally:
        # Delete prediction JSONL — contains confidential patch content.
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except OSError:
            pass

    wall = time.monotonic() - t0
    report_path = _find_report(report_dir, run_id, model_name)
    return _parse_report_safe(report_path, instance_id, run_id, wall)


def score_patch(
    instance_id: str,
    patch_bytes: bytes,
    run_id: str,
    *,
    model_name: str = "m5_eval",
    data_path: pathlib.Path | None = None,
    report_dir: pathlib.Path | None = None,
    harness_python: pathlib.Path | None = None,
    timeout: int = HARNESS_EVAL_TIMEOUT_SECONDS,
) -> ScoreResult:
    """Run the official SWE-bench harness against *patch_bytes* for *instance_id*.

    Strategy:
      1. Write prediction JSONL (chmod 0600) to a temp directory.
      2. Run harness subprocess pointing at the prediction file.
      3. Delete prediction JSONL in ``finally`` (before returning).
      4. Parse the harness report for the resolved/completed flags.
      5. Return sanitized ``ScoreResult``; keep report JSON.

    A harness wall / ``tests_timed_out`` is retried once.  A second timeout
    returns ``infra_failure=True`` (mapped to ``evaluator_failure`` / missing
    by the runner and analyzer).  It is never scored as resolved=0.

    Parameters
    ----------
    instance_id:
        SWE-bench instance identifier.
    patch_bytes:
        Candidate patch content.  Written to chmod-0600 file, never logged.
    run_id:
        Harness run identifier (used in report filename).
    model_name:
        Value for ``model_name_or_path`` in the prediction file.
    data_path:
        Override for the local dataset JSON path.  Unused (HF dataset).
    report_dir:
        Override for the report output directory.
    harness_python:
        Override for the harness Python interpreter path.
    timeout:
        Harness ``--timeout`` and Python wall, in seconds (default 1800).

    Returns
    -------
    ScoreResult
        Sanitized result; no test names or patch content.
    """
    del data_path  # HF dataset name is used; local JSON lacks v5 fields.
    rpt_dir = report_dir or REPORT_DIR
    rpt_dir.mkdir(parents=True, exist_ok=True)

    last: ScoreResult | None = None
    attempts = 1 + HARNESS_TIMEOUT_RETRIES
    t0 = time.monotonic()
    for attempt in range(attempts):
        attempt_run_id = run_id if attempt == 0 else f"{run_id}_retry{attempt}"
        try:
            last = _run_harness_once(
                instance_id,
                patch_bytes,
                attempt_run_id,
                model_name=model_name,
                report_dir=rpt_dir,
                harness_python=harness_python,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last = _timeout_score_result(
                instance_id,
                attempt_run_id,
                report_dir=rpt_dir,
                wall_seconds=time.monotonic() - t0,
                model_name=model_name,
            )
        if last is None:
            raise RuntimeError("harness returned no ScoreResult")
        if _score_is_timeout(last) and attempt + 1 < attempts:
            continue
        return last
    assert last is not None
    return last


# Public alias: evaluator-owned 1800s timeout + single retry.
evaluate_patch = score_patch


def _find_report(report_dir: pathlib.Path, run_id: str, model_name: str) -> pathlib.Path:
    """Locate the harness report file for *run_id* in *report_dir*.

    The harness names report files as ``<model_name>.<run_id>.json``.
    Falls back to the most recently modified JSON file if the expected name
    is not found.
    """
    expected = report_dir / f"{model_name.replace('/', '__')}.{run_id}.json"
    if expected.exists():
        return expected
    # Fallback: find most recent JSON with run_id in name.
    candidates = sorted(
        report_dir.glob(f"*.{run_id}.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    raise FileNotFoundError(
        f"No harness report found for run_id={run_id!r} in {report_dir}"
    )


def parse_report_safe(
    report_path: pathlib.Path,
    instance_id: str,
    run_id: str,
    wall_seconds: float = 0.0,
) -> ScoreResult:
    """Public wrapper — parse a report file and return sanitized ScoreResult."""
    return _parse_report_safe(report_path, instance_id, run_id, wall_seconds)


def _parse_report_safe(
    report_path: pathlib.Path,
    instance_id: str,
    run_id: str,
    wall_seconds: float,
) -> ScoreResult:
    """Parse the harness report JSON — expose only counts and boolean flags.

    Never exposes FAIL_TO_PASS or PASS_TO_PASS test names.
    The ``resolved_ids`` / ``unresolved_ids`` lists contain only instance IDs
    (not test names), which are retained as routing metadata.

    F2P/P2P fractions are computed from ``tests_status`` counts when present
    (success_count / (success_count + failure_count)).  Empty denominator =>
    null fraction + explicit reason.  When tests_status is absent (schema v2),
    fractions are null with null_reason = "schema_v{N}_no_tests_status".
    """
    report_bytes = report_path.read_bytes()
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    report: dict[str, Any] = json.loads(report_bytes)

    resolved_ids: list[str] = report.get("resolved_ids", [])
    completed_ids: list[str] = report.get("completed_ids", [])
    infra_ids: list[str] = report.get("infra_failure_ids", [])
    empty_ids: list[str] = report.get("empty_patch_ids", [])
    schema_version = report.get("schema_version")
    failure_reasons = report.get("failure_reasons") or {}
    instance_reason = (
        failure_reasons.get(instance_id) if isinstance(failure_reasons, dict) else None
    )
    # tests_timed_out / wall timeout is an incomplete harness, not a scored
    # unresolved instance.  The SWE-bench report puts these in
    # ambiguous_failure_ids with failure_reasons, not infra_failure_ids.
    timed_out = (
        isinstance(instance_reason, str)
        and ("timed_out" in instance_reason or instance_reason.endswith("timeout"))
    )

    # --- F2P / P2P extraction (version-aware) ---
    # Tests_status is present in some harness report versions.
    # When present, extract counts only (no test names).
    f2p_fraction: float | None = None
    p2p_fraction: float | None = None
    f2p_numerator: int | None = None
    f2p_denominator: int | None = None
    p2p_numerator: int | None = None
    p2p_denominator: int | None = None
    f2p_p2p_null_reason: str | None = None

    if "tests_status" in report:
        ts = report["tests_status"]
        f2p_data = ts.get("FAIL_TO_PASS", {})
        p2p_data = ts.get("PASS_TO_PASS", {})

        f2p_success = len(f2p_data.get("success", []))
        f2p_failure = len(f2p_data.get("failure", []))
        p2p_success = len(p2p_data.get("success", []))
        p2p_failure = len(p2p_data.get("failure", []))

        f2p_denom = f2p_success + f2p_failure
        p2p_denom = p2p_success + p2p_failure

        if f2p_denom > 0:
            f2p_fraction = f2p_success / f2p_denom
            f2p_numerator = f2p_success
            f2p_denominator = f2p_denom
        else:
            f2p_denominator = 0
            f2p_p2p_null_reason = "f2p_denominator_zero"

        if p2p_denom > 0:
            p2p_fraction = p2p_success / p2p_denom
            p2p_numerator = p2p_success
            p2p_denominator = p2p_denom
        else:
            p2p_denominator = 0
            if f2p_p2p_null_reason is None:
                f2p_p2p_null_reason = "p2p_denominator_zero"
    else:
        # Schema version 2 (current SWE-bench harness): no tests_status field.
        # F2P/P2P cannot be computed; null with explicit reason.
        f2p_p2p_null_reason = (
            "tests_timed_out" if timed_out else (
                f"schema_v{schema_version}_no_tests_status"
                if schema_version is not None
                else "no_tests_status_field"
            )
        )

    return ScoreResult(
        instance_id=instance_id,
        run_id=run_id,
        resolved=instance_id in resolved_ids,
        completed=instance_id in completed_ids,
        infra_failure=instance_id in infra_ids or timed_out,
        empty_patch=instance_id in empty_ids,
        total_instances=report.get("total_instances", 0),
        wall_seconds=wall_seconds,
        report_path=report_path,
        report_sha256=report_sha256,
        f2p_fraction=f2p_fraction,
        p2p_fraction=p2p_fraction,
        f2p_numerator=f2p_numerator,
        f2p_denominator=f2p_denominator,
        p2p_numerator=p2p_numerator,
        p2p_denominator=p2p_denominator,
        f2p_p2p_null_reason=f2p_p2p_null_reason,
    )


# ---------------------------------------------------------------------------
# Container stale-check (for post-eval audit)
# ---------------------------------------------------------------------------

def check_no_swe_containers() -> bool:
    """Return True if no evaluation containers remain for the SWE image."""
    return verify_no_containers(SWE_IMAGE)
