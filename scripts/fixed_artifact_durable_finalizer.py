#!/usr/bin/env python3
"""Durable finalizer for the Spreadsheet fixed-artifact batch.

Watches the newest (or --batch-id) FA batch status file.  When every queued
episode is terminal, runs the pre-registered FA analysis and writes
``data/dev/fixed_artifact_report.md``.  Never touches main records.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import logging
import os
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BATCHES = _ROOT / "data" / "batches"
_DEV = _ROOT / "data" / "dev"
POLL_SECONDS = 30

sys.path.insert(0, str(_ROOT / "src"))
from delegation_study.spreadsheetbench.parent_replay import (  # noqa: E402
    TASK_SET,
    analyze_fixed_artifact,
    valid_pairs,
    write_deferred,
    gate_has_passed,
)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _latest_fa_batch() -> pathlib.Path | None:
    newest: tuple[float, pathlib.Path] | None = None
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
        if data.get("task_set") != TASK_SET:
            continue
        mtime = status.stat().st_mtime
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return None if newest is None else newest[1]


def _terminal(episodes: list[dict]) -> bool:
    return all(ep.get("state") in {"complete", "failed", "skipped"} for ep in episodes)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--poll", type=int, default=POLL_SECONDS)
    args = parser.parse_args()

    batch_dir = _BATCHES / args.batch_id if args.batch_id else _latest_fa_batch()
    if batch_dir is None:
        print("No fixed_artifact batch found.", file=sys.stderr)
        return 1

    lock_path = batch_dir / "durable_finalizer.lock"
    done_path = batch_dir / "durable_finalizer.done"
    pid_path = batch_dir / "durable_finalizer.pid"
    log_path = batch_dir / "durable_finalizer.log"
    status_path = batch_dir / "status.json"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )
    log = logging.getLogger("fa_finalizer")

    lock_fh = lock_path.open("a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("another finalizer holds the lock; exiting")
        return 0

    pid_path.write_text(str(os.getpid()) + "\n")
    log.info("watching %s", status_path)

    while True:
        if not status_path.is_file():
            time.sleep(args.poll)
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(args.poll)
            continue
        episodes = status.get("episodes") or []
        states = {}
        for ep in episodes:
            states[ep.get("state", "?")] = states.get(ep.get("state", "?"), 0) + 1
        log.info("states=%s valid_pairs=%s", states, len(valid_pairs()))
        if _terminal(episodes):
            break
        if gate_has_passed() and not valid_pairs():
            path = write_deferred(
                "14:00 EDT gate reached; batch not producing valid paired records."
            )
            log.error("deferred at gate: %s", path)
            done_path.write_text(_now() + " deferred\n")
            return 5
        time.sleep(args.poll)

    summary = analyze_fixed_artifact()
    log.info("analysis n_pairs=%s report=%s", summary["n_pairs"], summary["report_path"])
    done_path.write_text(_now() + f" pairs={summary['n_pairs']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
