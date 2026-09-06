#!/usr/bin/env python3
"""Durable finalizer for a model-2 Spreadsheet batch.

Waits until every queued episode is terminal, then writes a gate snapshot
under data/dev/model2/. Does not kill or restart episodes.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import logging
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BATCHES = _ROOT / "data" / "batches"
_DEV = _ROOT / "data" / "dev" / "model2"
POLL_SECONDS = 30

sys.path.insert(0, str(_ROOT / "src"))
from delegation_study.analysis.model2 import (  # noqa: E402
    validate_model2_episode,
)
from delegation_study.qc import qc_episode  # noqa: E402


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _latest(task_set: str | None) -> Path | None:
    newest: tuple[float, Path] | None = None
    if not _BATCHES.is_dir():
        return None
    for path in _BATCHES.iterdir():
        status = path / "status.json"
        if not status.is_file():
            continue
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if task_set and data.get("task_set") != task_set:
            continue
        mtime = status.stat().st_mtime
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return None if newest is None else newest[1]


def _terminal(episodes: list[dict]) -> bool:
    return all(
        ep.get("state") in {"complete", "failed", "skipped"}
        for ep in episodes
    )


def _gate(batch_dir: Path, data: dict) -> dict:
    runs = _ROOT / "data" / "runs"
    rows = []
    for ep in data.get("episodes") or []:
        run_id = ep.get("run_id")
        rec_path = runs / str(run_id) / "episode.json" if run_id else None
        rec = None
        issues = []
        flags = []
        if rec_path and rec_path.is_file():
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            issues = [str(i) for i in validate_model2_episode(rec, rec_path)]
            flags = [f.code for f in qc_episode(rec)]
        rows.append({
            "task_id": ep.get("task_id"),
            "cell": ep.get("cell"),
            "state": ep.get("state"),
            "run_id": run_id,
            "exit_code": ep.get("exit_code"),
            "valid": rec is not None and not issues,
            "qc_flags": flags,
            "validate_issues": issues,
            "parent_hash": (
                (rec or {}).get("reproducibility", {}).get("parent_prompt_hash")
            ),
        })
    n_valid = sum(1 for r in rows if r["valid"])
    return {
        "checked_at": _now(),
        "batch_id": data.get("batch_id") or batch_dir.name,
        "task_set": data.get("task_set"),
        "n": len(rows),
        "n_valid": n_valid,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--task-set", default=None)
    parser.add_argument("--poll", type=int, default=POLL_SECONDS)
    args = parser.parse_args()

    batch_dir = _BATCHES / args.batch_id if args.batch_id else _latest(args.task_set)
    if batch_dir is None:
        print("No model2 batch found.", file=sys.stderr)
        return 1

    lock_path = batch_dir / "durable_finalizer.lock"
    done_path = batch_dir / "durable_finalizer.done"
    pid_path = batch_dir / "durable_finalizer.pid"
    log_path = batch_dir / "durable_finalizer.log"
    status_path = batch_dir / "status.json"
    _DEV.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )
    log = logging.getLogger("model2_finalizer")

    lock_fh = lock_path.open("a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another finalizer holds the lock", file=sys.stderr)
        return 0

    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    while True:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        episodes = data.get("episodes") or []
        if _terminal(episodes):
            gate = _gate(batch_dir, data)
            out = _DEV / f"{data.get('task_set', 'batch')}_gate.json"
            out.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n")
            done_path.write_text(json.dumps(gate, indent=2) + "\n")
            log.info("terminal gate written %s", out)
            return 0
        log.info("waiting; states=%s", [ep.get("state") for ep in episodes])
        time.sleep(max(5, args.poll))


if __name__ == "__main__":
    raise SystemExit(main())
