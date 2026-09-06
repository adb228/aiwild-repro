#!/usr/bin/env python3
"""Concurrent four-condition engineering smoke for task 343-20.

Launches EXACTLY four child processes concurrently — one each for
A0, A1, P0, P1 — all targeting task 343-20, replicate 1, task-set
development.  All four Popen calls are made BEFORE any wait() so the
processes run truly in parallel.

Race-safety
-----------
This orchestrator performs the stale-container check ONCE before spawning.
Child processes receive --skip-stale-check so they don't incorrectly flag
each other's containers as stale.  The single orchestrator precheck is
equivalent to the ordinary single-run stale protection because no containers
are running when the check fires.

Collision safety
----------------
Each process receives an explicit, pre-generated UUID run_id via --run-id.
Run directories are created with exist_ok=False inside run_episode, so any
collision (e.g. duplicate run_id) is detected and fails immediately.

Usage
-----
    python scripts/run_four_conditions.py [--task-id 343-20] [--replicate 1]
                                          [--runs-dir data/runs] [--dry-run]

Output
------
Per-process log files are written to data/dev/logs/ with names like:
    four_cond_<timestamp>_A0_<run_id>.log
A summary JSON is written to data/batches/<batch_id>/four_condition_<task_id>_summary.json.
The batch_id is printed at startup.  Pass --batch-id to share one directory
across a multi-task replay so all per-task summaries land in the same folder.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYTHON = sys.executable
_RUN_MODULE = "delegation_study.spreadsheetbench.run"
_VENV_PYTHON = _ROOT / ".venv" / "bin" / "python"

# Conditions to launch, in this order
_CONDITIONS = ("A0", "A1", "P0", "P1")


def _resolve_python() -> str:
    """Return the Python executable to use (venv if present, else sys.executable)."""
    if _VENV_PYTHON.is_file():
        return str(_VENV_PYTHON)
    return _PYTHON


def _do_stale_check() -> None:
    """Run the stale-container check once before spawning any children.

    Filters on BOTH ``delegation-study=true`` AND ``benchmark=spreadsheetbench``
    so that containers from other benchmarks (e.g. ``benchmark=swebench``)
    running concurrently are not mistakenly flagged as stale Spreadsheet containers.
    """
    import subprocess as sp
    try:
        result = sp.run(
            [
                "docker", "ps",
                "--filter", "label=delegation-study=true",
                "--filter", "label=benchmark=spreadsheetbench",
                "--format", r"{{.ID}}\t{{.Names}}\t{{.Status}}",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        print("WARNING: docker not found; skipping stale-container precheck.", flush=True)
        return
    except sp.TimeoutExpired:
        print("ERROR: docker ps timed out during orchestrator stale precheck.", flush=True)
        sys.exit(1)

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if lines:
        print(
            f"ERROR: Found {len(lines)} stale delegation-study+spreadsheetbench "
            "container(s) still running.",
            flush=True,
        )
        for ln in lines:
            print(f"  {ln}", flush=True)
        print(
            "Kill them first:  docker rm -f $(docker ps "
            "--filter label=delegation-study=true "
            "--filter label=benchmark=spreadsheetbench -q)",
            flush=True,
        )
        sys.exit(1)
    print("Orchestrator stale-container precheck: CLEAN (0 containers).", flush=True)


def run_four_conditions(
    task_id: str = "343-20",
    replicate: int = 1,
    runs_dir: Path | None = None,
    dry_run: bool = False,
    task_set: str = "development",
    batch_id: str | None = None,
) -> dict:
    """Spawn four concurrent child processes and collect their results.

    Returns
    -------
    dict
        Per-condition summary with run_ids, exit_codes, and log paths.
    """
    python_exe = _resolve_python()
    base_runs = runs_dir or (_ROOT / "data" / "runs")
    log_dir = _ROOT / "data" / "dev" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")

    # Pre-generate one unique run_id per condition (collision-proof) and one
    # shared batch_id so concurrent stale-container checks don't block each other.
    run_ids = {cond: str(uuid.uuid4()) for cond in _CONDITIONS}
    # Allow an injectable batch_id so multi-task replays can share one directory
    batch_id = batch_id or str(uuid.uuid4())
    print(f"\nOrchestrator: launching task={task_id} replicate={replicate} {ts}", flush=True)
    print(f"  batch_id={batch_id}", flush=True)
    for cond in _CONDITIONS:
        print(f"  {cond}: run_id={run_ids[cond]}", flush=True)

    # Orchestrator stale check (before spawning, so children don't see each other)
    if not dry_run:
        _do_stale_check()

    log_paths: dict[str, Path] = {}
    procs: dict[str, subprocess.Popen] = {}

    # ── Spawn ALL four before waiting (true concurrent start) ─────────────────
    for cond in _CONDITIONS:
        rid = run_ids[cond]
        log_path = log_dir / f"four_cond_{ts}_{cond}_{rid[:8]}.log"
        log_paths[cond] = log_path

        cmd = [
            python_exe, "-m", _RUN_MODULE,
            "--task-id", task_id,
            "--condition", cond,
            "--replicate", str(replicate),
            "--task-set", task_set,
            "--run-id", rid,
            "--batch-id", batch_id,   # shared batch; stale check exempts siblings
            "--skip-stale-check",     # orchestrator already checked before spawn
        ]

        if dry_run:
            print(f"  [DRY-RUN] {cond}: would run: {' '.join(cmd)}", flush=True)
            procs[cond] = None  # type: ignore[assignment]
            continue

        log_fh = log_path.open("w", encoding="utf-8")
        # Header
        log_fh.write(f"# condition={cond} run_id={rid} task_id={task_id}\n")
        log_fh.write(f"# started_at={datetime.datetime.now(datetime.UTC).isoformat()}\n")
        log_fh.write(f"# cmd={' '.join(cmd)}\n\n")
        log_fh.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        procs[cond] = proc
        print(f"  Spawned {cond}: pid={proc.pid}", flush=True)

    # ── Now wait for all (after all four are spawned) ─────────────────────────
    exit_codes: dict[str, int | None] = {}
    for cond in _CONDITIONS:
        if dry_run:
            exit_codes[cond] = None
            continue
        proc = procs[cond]
        proc.wait()
        exit_codes[cond] = proc.returncode
        status = "OK" if proc.returncode == 0 else f"FAILED (exit={proc.returncode})"
        print(f"  {cond}: {status}  log={log_paths[cond]}", flush=True)

    if dry_run:
        print("\n[DRY-RUN] No processes were started.", flush=True)
        return {"dry_run": True, "run_ids": run_ids, "conditions": _CONDITIONS}

    # ── Collect episode records ───────────────────────────────────────────────
    print("\nCollecting episode records...", flush=True)
    summaries = {}
    for cond in _CONDITIONS:
        rid = run_ids[cond]
        run_dir = base_runs / rid
        episode_path = run_dir / "episode.json"

        summary: dict = {
            "condition": cond,
            "run_id": rid,
            "exit_code": exit_codes[cond],
            "log_path": str(log_paths[cond]),
            "run_dir": str(run_dir),
            "episode_found": episode_path.is_file(),
        }

        if episode_path.is_file():
            try:
                ep = json.loads(episode_path.read_text())
                summary["analysis_included"] = ep.get("analysis_included")
                summary["analysis_inclusion_reason"] = ep.get("analysis_inclusion_reason")

                ho = ep.get("handoff", {})
                summary["memo_sha256"] = ho.get("memo_sha256")
                summary["candidate_sha256"] = ho.get("candidate_script_sha256")

                wo = ep.get("worker_outcome")
                summary["worker_outcome_null"] = wo is None
                if wo is not None:
                    summary["worker_evaluator_status"] = wo.get("evaluator_status")
                    summary["worker_full_task_success"] = wo.get("full_task_success")
                    summary["worker_score"] = wo.get("target_unit_pass_rate")

                fo = ep.get("final_outcome", {})
                summary["final_evaluator_status"] = fo.get("evaluator_status")
                summary["final_full_task_success"] = fo.get("full_task_success")
                summary["final_score"] = fo.get("target_unit_pass_rate")
                summary["final_output_sha256"] = fo.get("output_sha256")

                comp = ep.get("compliance", {})
                summary["worker_feedback_calls"] = comp.get("worker_feedback_calls")
                summary["artifact_crossed_handoff"] = comp.get("artifact_crossed_handoff")
                summary["parent_finalized"] = comp.get("parent_finalized")
                summary["worker_scratch_destroyed"] = comp.get("worker_scratch_destroyed")

                final_sha = ep.get("final_script_sha256")
                candidate_sha = ho.get("candidate_script_sha256")
                if candidate_sha and final_sha:
                    if candidate_sha == final_sha:
                        summary["disposition"] = "accepted_unchanged"
                    else:
                        summary["disposition"] = "revised"
                elif candidate_sha and not final_sha:
                    summary["disposition"] = "missing_candidate"
                else:
                    summary["disposition"] = "memo_only"

                vali = ep.get("_meta", {}).get("validate_issues", [])
                qci = ep.get("_meta", {}).get("qc_flags", [])
                summary["validate_issues"] = vali
                summary["qc_flags"] = [f.get("code") for f in qci] if qci else []

                wu = ep.get("usage", {}).get("worker", {})
                pu = ep.get("usage", {}).get("parent", {})
                summary["worker_input_tokens"] = wu.get("input_tokens")
                summary["worker_output_tokens"] = wu.get("output_tokens")
                summary["parent_input_tokens"] = pu.get("input_tokens")
                summary["parent_output_tokens"] = pu.get("output_tokens")

            except Exception as exc:
                summary["parse_error"] = str(exc)
        else:
            summary["episode_found"] = False
            summary["error"] = "episode.json not found"

        summaries[cond] = summary

    # ── Check for run directory collisions (by inode) ─────────────────────────
    inodes: dict[str, int] = {}
    collision_detected = False
    for cond in _CONDITIONS:
        rd = Path(summaries[cond]["run_dir"])
        if rd.is_dir():
            inode = rd.stat().st_ino
            if inode in inodes.values():
                print(f"COLLISION DETECTED: {cond} shares inode with another condition!", flush=True)
                collision_detected = True
            inodes[cond] = inode
    # Verify all 4 are distinct
    if len(set(inodes.values())) == 4:
        print("Collision check: PASS (all 4 run directories have distinct inodes)", flush=True)
    else:
        print(f"Collision check: FAIL (inodes: {inodes})", flush=True)
        collision_detected = True

    # ── Collect Docker ps output ───────────────────────────────────────────────
    print("\nFinal Docker ps (should show only header, 0 containers):", flush=True)
    try:
        docker_result = subprocess.run(
            ["docker", "ps", "--filter", "label=delegation-study=true"],
            capture_output=True, text=True, timeout=15,
        )
        docker_output = docker_result.stdout
        print(docker_output, flush=True)
    except Exception as exc:
        docker_output = f"(docker ps failed: {exc})"

    # ── Write summary JSON ────────────────────────────────────────────────────
    summary_out = {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "batch_id": batch_id,
        "task_id": task_id,
        "replicate": replicate,
        "task_set": task_set,
        "collision_detected": collision_detected,
        "run_inodes": {k: v for k, v in inodes.items()},
        "docker_ps_output": docker_output,
        "conditions": summaries,
    }

    # Write summary to data/batches/<batch_id>/ (gitignored) so it never
    # dirties the working tree and the clean-tree pilot/main gate is preserved
    # across multi-task replay sequences.
    batch_dir = _ROOT / "data" / "batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    summary_path = batch_dir / f"four_condition_{task_id}_summary.json"
    summary_path.write_text(json.dumps(summary_out, indent=2) + "\n", encoding="utf-8")
    print(f"\nSummary written to: {summary_path}", flush=True)

    # ── Print episode checks ──────────────────────────────────────────────────
    print("\n=== Episode Checks ===", flush=True)
    checks_pass = True
    for cond, s in summaries.items():
        print(f"\n[{cond}] run_id={s['run_id'][:8]}...")
        expected_candidate = cond.startswith("P")
        expected_feedback = cond in ("A1", "P1")

        ok_candidate = (s.get("candidate_sha256") is not None) == expected_candidate
        ok_worker_null = (s.get("worker_outcome_null", True)) == (not cond.startswith("P"))
        ok_feedback = (
            (s.get("worker_feedback_calls", 0) > 0) == expected_feedback
            if expected_feedback
            else (s.get("worker_feedback_calls", 0) == 0) == (not expected_feedback)
        )
        ok_scratch = (
            (s.get("worker_scratch_destroyed") is True) if cond == "A1" else True
        )
        ok_analysis = (s.get("analysis_included") is False)
        ok_episode = s.get("episode_found", False)
        ok_exit = (s.get("exit_code", 1) == 0)

        def _tick(b):
            return "✓" if b else "✗"

        print(f"  exit_code==0:              {_tick(ok_exit)} ({s.get('exit_code')})")
        print(f"  episode_found:             {_tick(ok_episode)}")
        print(f"  analysis_included=false:   {_tick(ok_analysis)}")
        print(f"  candidate_present=={str(expected_candidate):5}: {_tick(ok_candidate)} ({s.get('candidate_sha256', 'null')[:8] if s.get('candidate_sha256') else 'null'})")
        print(f"  worker_outcome_null=={str(not cond.startswith('P')):5}: {_tick(ok_worker_null)}")
        print(f"  feedback_calls>0=={str(expected_feedback):5}:  {_tick(ok_feedback)} ({s.get('worker_feedback_calls')})")
        if cond == "A1":
            print(f"  scratch_destroyed:         {_tick(ok_scratch)} ({s.get('worker_scratch_destroyed')})")

        if not all([ok_exit, ok_episode, ok_analysis, ok_candidate, ok_worker_null, ok_feedback, ok_scratch]):
            checks_pass = False

    print(f"\nOverall checks: {'PASS' if checks_pass else 'FAIL'}", flush=True)
    return summary_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default="343-20")
    parser.add_argument("--replicate", type=int, default=1)
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--task-set", default="development",
                        help="Task set label passed to --task-set of the runner.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them.",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help=(
            "Optional batch UUID shared across multi-task replay sequences. "
            "When provided, per-task summaries are written to "
            "data/batches/<batch_id>/ so multiple tasks share one directory. "
            "If omitted, a fresh UUID is generated per invocation."
        ),
    )
    args = parser.parse_args(argv)

    run_four_conditions(
        task_id=args.task_id,
        replicate=args.replicate,
        runs_dir=args.runs_dir,
        dry_run=args.dry_run,
        task_set=args.task_set,
        batch_id=args.batch_id,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
