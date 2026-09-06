#!/usr/bin/env python3
"""Launch a model-2 SWE batch (OpenAI / gpt-5.6-luna).

Does not launch from the Sonnet SWE worktree. Does not merge to main.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from delegation_study.analysis.model2 import MODEL2_SWE_TASK_SETS  # noqa: E402
from delegation_study.model_client_openai import maybe_drop_concurrency  # noqa: E402

_PILOT_TASKS = [
    "astropy__astropy-7606",
    "sympy__sympy-19783",
    "django__django-13112",
]
_MAIN_TASKS = [
    "pylint-dev__pylint-6386",
    "sphinx-doc__sphinx-8593",
    "sympy__sympy-12419",
    "django__django-14792",
    "sympy__sympy-16766",
    "scikit-learn__scikit-learn-14087",
    "astropy__astropy-8872",
    "django__django-11292",
]
_CELLS = ("A0", "A1", "P0", "P1")
_INFRA_MARKERS = (
    "connection error",
    "apiconnectionerror",
    "overloaded",
    "too many requests",
    "rate limit",
    "ratelimit",
    "error code: 429",
    "status code: 429",
    "error code: 529",
    "error code: 500",
    "error code: 502",
    "error code: 503",
    "error code: 504",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "apitimeouterror",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_infra(err: str | None) -> bool:
    if not err:
        return False
    low = str(err).lower()
    return any(m in low for m in _INFRA_MARKERS)


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _default_tasks(task_set: str) -> list[str]:
    if task_set == "swe_pilot_m2":
        return list(_PILOT_TASKS)
    if task_set == "swe_main_m2":
        return list(_MAIN_TASKS)
    raise ValueError(f"unknown SWE model-2 task_set {task_set!r}")


def _already_complete(task_set: str, task_id: str, cell: str) -> bool:
    """True when this worktree already has a complete episode.json for the cell."""
    runs = _ROOT / "data" / "runs"
    if not runs.is_dir():
        return False
    for run_dir in runs.iterdir():
        path = run_dir / "episode.json"
        if not path.is_file():
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("task_set") != task_set:
            continue
        if rec.get("task_id") != task_id:
            continue
        if rec.get("condition") != cell:
            continue
        if rec.get("status") == "complete":
            return True
    return False


def _stderr_tail(run_id: str, limit: int = 2000) -> str:
    path = _ROOT / "data" / "runs" / run_id / "logs" / "runner.stderr.log"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def launch(
    *,
    task_set: str,
    tasks: list[str],
    cells: list[str],
    concurrency: int,
) -> int:
    batch_id = str(uuid.uuid4())
    batch_dir = _ROOT / "data" / "batches" / batch_id
    status_path = batch_dir / "status.json"
    created = _now()
    episodes = [
        {
            "task_id": task_id,
            "cell": cell,
            "run_id": str(uuid.uuid4()),
            "state": "queued",
            "wall_seconds": None,
            "exit_code": None,
            "error": None,
        }
        for task_id in tasks
        for cell in cells
    ]
    payload = {
        "batch_id": batch_id,
        "task_set": task_set,
        "created_at": created,
        "updated_at": created,
        "episodes": episodes,
    }
    _write_status(status_path, payload)
    print(f"Batch ID: {batch_id}")
    print(f"Queued:   {len(episodes)} episode(s)")
    print(f"Status:   {status_path}")

    python = str(_ROOT / ".venv" / "bin" / "python")
    live = max(2, int(concurrency))
    remaining = list(range(len(episodes)))
    active: list[tuple[int, subprocess.Popen, float]] = []

    def reap() -> None:
        still = []
        for idx, proc, t0 in active:
            ret = proc.poll()
            if ret is None:
                still.append((idx, proc, t0))
                continue
            wall = time.monotonic() - t0
            ep = episodes[idx]
            ep["wall_seconds"] = round(wall, 3)
            ep["exit_code"] = ret
            if ret == 0:
                ep["state"] = "complete"
                ep["error"] = None
                print(f"  OK     {ep['task_id']} {ep['cell']}  exit=0  wall={wall:.1f}s")
            else:
                tail = _stderr_tail(str(ep["run_id"]))
                ep["state"] = "failed"
                ep["error"] = (
                    f"subprocess exited {ret}\n{tail}" if tail else f"subprocess exited {ret}"
                )
                print(f"  FAILED {ep['task_id']} {ep['cell']}  exit={ret}  wall={wall:.1f}s")
            payload["episodes"] = episodes
            payload["updated_at"] = _now()
            _write_status(status_path, payload)
        active[:] = still

    while remaining or active:
        reap()
        nxt = maybe_drop_concurrency(live)
        if nxt != live:
            print(f"429 backoff: concurrency {live} -> {nxt}")
            live = nxt
        while remaining and len(active) < live:
            idx = remaining.pop(0)
            ep = episodes[idx]
            if _already_complete(task_set, ep["task_id"], ep["cell"]):
                ep["state"] = "skipped"
                ep["exit_code"] = 0
                ep["error"] = None
                ep["wall_seconds"] = 0.0
                print(f"  SKIP   {ep['task_id']} {ep['cell']}  already complete")
                payload["episodes"] = episodes
                payload["updated_at"] = _now()
                _write_status(status_path, payload)
                continue
            cmd = [
                python, "-m", "delegation_study.swebench.run",
                "--task-id", ep["task_id"],
                "--condition", ep["cell"],
                "--task-set", task_set,
                "--run-id", ep["run_id"],
            ]
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "DELEGATION_MODEL2": "1"}
            print(f"  Spawning {ep['task_id']} {ep['cell']}  run_id={ep['run_id'][:8]}...")
            ep["state"] = "running"
            payload["episodes"] = episodes
            payload["updated_at"] = _now()
            _write_status(status_path, payload)
            proc = subprocess.Popen(cmd, cwd=str(_ROOT), env=env)
            active.append((idx, proc, time.monotonic()))
        if remaining or active:
            time.sleep(1)
    reap()

    counts = Counter(ep["state"] for ep in episodes)
    print(f"Total: {len(episodes)}  {dict(counts)}")
    print(f"Status: {status_path}")
    return 0 if counts.get("failed", 0) == 0 else 1


def _retry_infra(task_set: str, cells: list[str], concurrency: int) -> int:
    batches = _ROOT / "data" / "batches"
    newest = None
    newest_mtime = -1.0
    for path in batches.iterdir():
        status = path / "status.json"
        if not status.is_file():
            continue
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("task_set") != task_set:
            continue
        mtime = status.stat().st_mtime
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest = data
    if not newest:
        return 0
    failed = [
        ep for ep in (newest.get("episodes") or [])
        if ep.get("state") == "failed" and _is_infra(ep.get("error"))
    ]
    if not failed:
        return 0
    tasks = sorted({ep["task_id"] for ep in failed})
    retry_cells = sorted({ep["cell"] for ep in failed})
    log = _ROOT / "data" / "dev" / "model2" / "swe_infra_retry.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"infra retry task_set={task_set} tasks={tasks} cells={retry_cells}\n")
    return launch(
        task_set=task_set,
        tasks=tasks,
        cells=retry_cells,
        concurrency=concurrency,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-set", required=True, choices=sorted(MODEL2_SWE_TASK_SETS))
    parser.add_argument("--tasks", default=None)
    parser.add_argument("--cells", default="A0,A1,P0,P1")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args(argv)

    os.environ["DELEGATION_MODEL2"] = "1"
    guard = subprocess.run(
        [str(_ROOT / ".venv" / "bin" / "python"),
         str(_ROOT / "scripts" / "model2_prelaunch_guards.py")],
        cwd=str(_ROOT),
    )
    if guard.returncode != 0:
        return guard.returncode

    cells = [c.strip().upper() for c in args.cells.split(",") if c.strip()]
    if any(c not in _CELLS for c in cells):
        print(f"ERROR: cells must be subset of {_CELLS}", file=sys.stderr)
        return 1
    tasks = (
        [t.strip() for t in args.tasks.split(",") if t.strip()]
        if args.tasks
        else _default_tasks(args.task_set)
    )
    concurrency = max(2, int(args.concurrency))
    rc = launch(task_set=args.task_set, tasks=tasks, cells=cells, concurrency=concurrency)
    for _attempt in (2, 3):
        if not _latest_infra(args.task_set):
            return rc
        rc = _retry_infra(args.task_set, cells, concurrency)
    if _latest_infra(args.task_set):
        missing = _ROOT / "data" / "dev" / "model2" / "infra_missing.log"
        with missing.open("a", encoding="utf-8") as fh:
            fh.write(f"infra retry exhausted (3) task_set={args.task_set}\n")
    return rc


def _latest_infra(task_set: str) -> list[tuple[str, str]]:
    batches = _ROOT / "data" / "batches"
    newest = None
    newest_mtime = -1.0
    if not batches.is_dir():
        return []
    for path in batches.iterdir():
        status = path / "status.json"
        if not status.is_file():
            continue
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("task_set") != task_set:
            continue
        mtime = status.stat().st_mtime
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest = data
    if not newest:
        return []
    return [
        (ep.get("task_id"), ep.get("cell"))
        for ep in (newest.get("episodes") or [])
        if ep.get("state") == "failed" and _is_infra(ep.get("error"))
    ]


if __name__ == "__main__":
    raise SystemExit(main())
