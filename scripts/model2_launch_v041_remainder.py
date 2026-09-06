#!/usr/bin/env python3
"""After the 20-task main_v04_m2 wave, launch the 5 reserve IDs that complete
the frozen v0.4.1 25-task grid (main + reserve[:5] from ss_task_selection.json
/ paper_outputs/v041). Does not edit the task-selection package.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BATCH = _ROOT / "data" / "batches" / "7ee0737d-7ba4-42a1-ad95-9121c7a27b2e"
_REMAINDER = "91-34,160-6,423-16,48975,42526"
POLL = 30


def _terminal(status: dict) -> bool:
    return all(
        ep.get("state") in {"complete", "failed", "skipped"}
        for ep in (status.get("episodes") or [])
    )


def main() -> int:
    status_path = _BATCH / "status.json"
    while True:
        if not status_path.is_file():
            print("waiting for first-wave status", flush=True)
            time.sleep(POLL)
            continue
        data = json.loads(status_path.read_text(encoding="utf-8"))
        if _terminal(data):
            break
        print("first wave still running; waiting", flush=True)
        time.sleep(POLL)
    cmd = [
        str(_ROOT / ".venv" / "bin" / "python"),
        "-u",
        str(_ROOT / "scripts" / "launch_model2_batch.py"),
        "--tasks",
        _REMAINDER,
        "--cells",
        "A0,A1,P0,P1",
        "--task-set",
        "main_v04_m2",
        "--concurrency",
        "6",
    ]
    log = (_ROOT / "data" / "dev" / "model2" / "main_remainder_launch.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    print("launching remainder", _REMAINDER, flush=True)
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(_ROOT), stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
