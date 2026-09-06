#!/usr/bin/env python3
"""Launch Spreadsheet fixed-artifact parent-replay episodes.

Never pooled with main.  Keys skip/completion on (source_run_id, arm).

    python scripts/launch_fixed_artifact.py --smoke --concurrency 4
    python scripts/launch_fixed_artifact.py --all --concurrency 6 --tmux
    python scripts/launch_fixed_artifact.py --enriched --concurrency 6 --tmux
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_VENV_PYTHON = _ROOT / ".venv" / "bin" / "python"
_RUNS = _ROOT / "data" / "runs"
_BATCHES = _ROOT / "data" / "batches"
_MODULE = "delegation_study.spreadsheetbench.parent_replay"

sys.path.insert(0, str(_ROOT / "src"))
from delegation_study.spreadsheetbench.parent_replay import (  # noqa: E402
    DESIGN_SEED,
    TASK_SET,
    assign_arm_order,
    discover_main_v04_p_candidates,
    gate_has_passed,
    select_enriched_candidates,
    select_smoke_candidates,
    valid_pairs,
    write_deferred,
)


def _python() -> str:
    return str(_VENV_PYTHON) if _VENV_PYTHON.is_file() else sys.executable


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def _already_done(runs_dir: Path) -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    for source_id, arms in valid_pairs(runs_dir).items():
        for arm in arms:
            done.add((source_id, arm))
    return done


def launch(
    *,
    smoke: bool,
    enriched: bool,
    concurrency: int,
    skip_stale_check: bool,
    skip_preflight: bool,
    skip_clean_tree: bool,
) -> int:
    if gate_has_passed() and not smoke:
        # Production 100/enriched must not start after the hard gate unless
        # valid pairs are already being produced (checked by caller).
        pass

    candidates = discover_main_v04_p_candidates(_RUNS)
    if smoke:
        candidates = select_smoke_candidates(candidates)
        sample_label = "smoke"
    elif enriched:
        candidates = select_enriched_candidates(candidates)
        sample_label = "correctness_enriched"
    else:
        sample_label = "all_p_cells"

    if not candidates:
        print("ERROR: no main_v04 P candidates found.", file=sys.stderr)
        return 1

    done = _already_done(_RUNS)
    jobs: list[dict[str, str]] = []
    for cand in candidates:
        order = assign_arm_order(cand.source_run_id, DESIGN_SEED)
        for arm in order:
            jobs.append({
                "source_run_id": cand.source_run_id,
                "task_id": cand.task_id,
                "source_condition": cand.source_condition,
                "arm": arm,
                "candidate_correct": (
                    "true" if cand.candidate_correct else
                    "false" if cand.candidate_correct is False else "null"
                ),
            })

    queued = [j for j in jobs if (j["source_run_id"], j["arm"]) not in done]
    skipped = [j for j in jobs if (j["source_run_id"], j["arm"]) in done]

    batch_id = str(uuid.uuid4())
    batch_dir = _BATCHES / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    episodes: list[dict[str, Any]] = []
    for job in skipped:
        episodes.append({
            **job,
            "run_id": None,
            "state": "skipped",
            "wall_seconds": None,
            "exit_code": None,
            "error": "already completed",
        })
    run_ids: dict[tuple[str, str], str] = {}
    for job in queued:
        rid = str(uuid.uuid4())
        run_ids[(job["source_run_id"], job["arm"])] = rid
        episodes.append({
            **job,
            "run_id": rid,
            "state": "queued",
            "wall_seconds": None,
            "exit_code": None,
            "error": None,
        })

    status_path = batch_dir / "status.json"
    assignment = {
        "batch_id": batch_id,
        "task_set": TASK_SET,
        "sample": sample_label,
        "design_seed": DESIGN_SEED,
        "created_at": created_at,
        "n_candidates": len(candidates),
        "n_jobs": len(jobs),
        "queued": len(queued),
        "skipped": len(skipped),
        "concurrency": concurrency,
        "episodes": episodes,
    }
    _atomic_write_json(status_path, assignment)
    _atomic_write_json(batch_dir / "assignments.json", {
        "design_seed": DESIGN_SEED,
        "sample": sample_label,
        "candidates": [
            {
                "source_run_id": c.source_run_id,
                "task_id": c.task_id,
                "source_condition": c.source_condition,
                "candidate_correct": c.candidate_correct,
                "arm_order": assign_arm_order(c.source_run_id),
            }
            for c in candidates
        ],
    })

    print(f"Batch ID: {batch_id}", flush=True)
    print(f"sample={sample_label} queued={len(queued)} skipped={len(skipped)}", flush=True)
    print(f"status={status_path}", flush=True)

    if not queued:
        print("Nothing to launch.", flush=True)
        return 0

    if not skip_stale_check:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", "label=delegation-study=true",
                "--filter", "label=benchmark=spreadsheetbench",
                "--format", r"{{.ID}}\t{{.Names}}",
            ],
            capture_output=True, text=True, timeout=15,
        )
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if lines:
            print(f"ERROR: {len(lines)} stale spreadsheetbench container(s).", file=sys.stderr)
            return 1
        print("Stale-container precheck: CLEAN.", flush=True)

    python_exe = _python()
    ep_index = {
        (ep["source_run_id"], ep["arm"]): i
        for i, ep in enumerate(episodes)
        if ep["state"] == "queued"
    }
    active: list[tuple[subprocess.Popen, tuple[str, str], float]] = []
    remaining = list(queued)
    any_failed = False

    def _write() -> None:
        assignment["episodes"] = episodes
        assignment["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _atomic_write_json(status_path, assignment)

    def _reap() -> None:
        nonlocal any_failed
        still = []
        for proc, pair, t0 in active:
            code = proc.poll()
            if code is None:
                still.append((proc, pair, t0))
                continue
            wall = time.monotonic() - t0
            idx = ep_index[pair]
            episodes[idx]["state"] = "complete" if code == 0 else "failed"
            episodes[idx]["wall_seconds"] = round(wall, 3)
            episodes[idx]["exit_code"] = code
            if code != 0:
                episodes[idx]["error"] = f"subprocess exited {code}"
                any_failed = True
                print(f"  FAILED {pair[0]} {pair[1]} exit={code} wall={wall:.1f}s", flush=True)
            else:
                print(f"  OK     {pair[0]} {pair[1]} exit=0 wall={wall:.1f}s", flush=True)
            _write()
        active.clear()
        active.extend(still)

    try:
        while remaining or active:
            _reap()
            while remaining and len(active) < concurrency:
                job = remaining.pop(0)
                pair = (job["source_run_id"], job["arm"])
                rid = run_ids[pair]
                cmd = [
                    python_exe, "-m", _MODULE,
                    "--source-run-id", job["source_run_id"],
                    "--arm", job["arm"],
                    "--run-id", rid,
                    "--batch-id", batch_id,
                    "--task-set", TASK_SET,
                    "--skip-stale-check",
                ]
                if skip_preflight:
                    cmd.append("--skip-preflight")
                if skip_clean_tree:
                    cmd.append("--skip-clean-tree")
                if enriched:
                    cmd.append("--enriched")
                idx = ep_index[pair]
                episodes[idx]["state"] = "running"
                _write()
                print(f"  START  {pair[0]} {pair[1]} run_id={rid}", flush=True)
                proc = subprocess.Popen(cmd, cwd=str(_ROOT))
                active.append((proc, pair, time.monotonic()))
            if active and (remaining or len(active) >= concurrency):
                time.sleep(2)
    except KeyboardInterrupt:
        print("Interrupted; leaving children running.", flush=True)
        return 2
    finally:
        _reap()
        _write()

    pairs = valid_pairs(_RUNS)
    print(f"valid_pairs_on_disk={len(pairs)}", flush=True)
    return 2 if any_failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch fixed-artifact FA episodes.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--smoke", action="store_true", help="2 candidates × 2 arms")
    group.add_argument("--all", action="store_true", help="50 candidates × 2 arms")
    group.add_argument("--enriched", action="store_true", help="correctness-enriched fallback")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--tmux", action="store_true")
    parser.add_argument("--skip-stale-check", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument(
        "--skip-clean-tree",
        action="store_true",
        help="Pass through to parent_replay (unrelated dirty files only).",
    )
    args = parser.parse_args(argv)

    if args.all or args.enriched:
        if gate_has_passed() and not valid_pairs(_RUNS):
            path = write_deferred(
                "14:00 EDT gate reached without valid paired FA records on disk."
            )
            print(f"DEFERRED: {path}", file=sys.stderr)
            return 5

    if args.tmux:
        session = "fixed-artifact"
        cmd = [
            _python(), str(Path(__file__).resolve()),
            "--smoke" if args.smoke else "--enriched" if args.enriched else "--all",
            "--concurrency", str(args.concurrency),
        ]
        if args.skip_stale_check:
            cmd.append("--skip-stale-check")
        if args.skip_preflight:
            cmd.append("--skip-preflight")
        if args.skip_clean_tree:
            cmd.append("--skip-clean-tree")
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        subprocess.check_call(["tmux", "new-session", "-d", "-s", session, " ".join(cmd)])
        fin = "fixed-artifact-finalizer"
        fin_cmd = (
            f"{_python()} { _ROOT / 'scripts' / 'fixed_artifact_durable_finalizer.py' }"
        )
        subprocess.run(["tmux", "kill-session", "-t", fin], capture_output=True)
        # Finalizer needs the batch id; it will attach to the newest FA batch.
        subprocess.check_call(["tmux", "new-session", "-d", "-s", fin, fin_cmd])
        print(f"tmux sessions: {session}, {fin}")
        return 0

    return launch(
        smoke=args.smoke,
        enriched=args.enriched,
        concurrency=args.concurrency,
        skip_stale_check=args.skip_stale_check,
        skip_preflight=args.skip_preflight,
        skip_clean_tree=args.skip_clean_tree,
    )


if __name__ == "__main__":
    sys.exit(main())
