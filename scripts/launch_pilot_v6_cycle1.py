#!/usr/bin/env python3
"""Launch swe-0.3.3 pilot v6 cycle 1: 3 interface-failure cells only.

Session: swe-pilot-v6c1
Protocol: swe-0.3.3
Concurrency: 3
Max turns: 80 / Max tool calls: 160 (observation, per role)
Task set: pilot

Rerun cells (v6 400 dangling tool_use):
  scikit-learn__scikit-learn-14983/P0
  scikit-learn__scikit-learn-14983/P1
  matplotlib__matplotlib-25479/P0

Do not relaunch retained v5 or the two successful v6 cells.
The durable finalizer gates the mixed union (7×0.3.1 + 2×0.3.2 + 3×0.3.3).

Do NOT run from a different worktree. Must run from <REPO>
Do NOT launch SWE main from this script.
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
import uuid
from pathlib import Path

WORKTREE = Path("<REPO>")
PYTHON = str(WORKTREE / ".venv" / "bin" / "python")
BATCH_ID = (
    f"swe-pilot-v6c1-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    f"-{uuid.uuid4().hex[:8]}"
)
SESSION = "swe-pilot-v6c1"
FINALIZER_SESSION = "swe-pilot-v6c1-finalizer"
TASK_SET = "pilot"
RERUN_CELLS = [
    ("scikit-learn__scikit-learn-14983", "P0"),
    ("scikit-learn__scikit-learn-14983", "P1"),
    ("matplotlib__matplotlib-25479", "P0"),
]
CONCURRENCY = 3
MAX_TURNS = 80
MAX_TOOL_CALLS = 160
PROTOCOL_EXPECTED = "swe-0.3.3"

BATCH_DIR = WORKTREE / "data" / "batches" / BATCH_ID
BATCH_DIR.mkdir(parents=True, exist_ok=True)
STATUS_PATH = BATCH_DIR / "status.json"
LOG_PATH = BATCH_DIR / "batch.log"
REPORT_OUT = WORKTREE / "data" / "dev" / "swe" / "pilot_report_v6_cycle1.md"
CSV_OUT = WORKTREE / "data" / "dev" / "swe" / "usage_for_caps_v6.csv"
FINALIZER_PATH = BATCH_DIR / "durable_finalizer.py"
RUNNER_PATH = BATCH_DIR / "run_batch.py"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_status(episodes: list[dict], head: str) -> None:
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    payload = {
        "batch_id": BATCH_ID,
        "checkout": str(WORKTREE),
        "commit": head,
        "protocol_version": PROTOCOL_EXPECTED,
        "concurrency": CONCURRENCY,
        "max_turns": MAX_TURNS,
        "max_tool_calls": MAX_TOOL_CALLS,
        "caps": "observation",
        "created_at": _now(),
        "n_rerun": len(RERUN_CELLS),
        "n_retained_v5": 7,
        "n_retained_v6": 2,
        "episodes": episodes,
    }
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    tmp.replace(STATUS_PATH)


def main() -> None:
    if not (WORKTREE / ".git").exists() and not (WORKTREE / ".git").is_file():
        sys.exit("ERROR: not a git repo — run from the SWE adapter checkout only")

    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=WORKTREE, text=True
    ).strip()
    if branch != "swe-adapter":
        sys.exit(f"ERROR: must launch from swe-adapter, not {branch!r}")

    for session in (SESSION, FINALIZER_SESSION):
        existing = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
        )
        if existing.returncode == 0:
            sys.exit(f"ERROR: tmux session {session} already exists. Not killing it.")

    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=WORKTREE, text=True
    ).strip()

    print(f"Batch ID: {BATCH_ID}")
    print(f"HEAD: {head}")
    print(f"Protocol: {PROTOCOL_EXPECTED}")
    print(f"Rerun cells: {RERUN_CELLS}")
    print(f"Total launched episodes: {len(RERUN_CELLS)} (gate is 7 v5 + 2 v6 + 3 cycle1)")

    episodes_status = [
        {
            "task_id": task_id,
            "cell": condition,
            "state": "pending",
            "run_id": None,
            "pid": None,
            "started_at": None,
            "completed_at": None,
            "exit_code": None,
            "error": None,
            "wall_seconds": None,
            "cohort": "rerun",
        }
        for task_id, condition in RERUN_CELLS
    ]
    write_status(episodes_status, head)
    print(f"Status file: {STATUS_PATH}")

    RUNNER_PATH.write_text(
        f'''#!/usr/bin/env python3
"""Inner batch runner for pilot v6 cycle 1 (3 interface cells). Runs inside tmux swe-pilot-v6c1."""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("{LOG_PATH}"),
    ],
)
log = logging.getLogger("pilot_v6c1")
sys.path.insert(0, "{WORKTREE}/src")

from delegation_study.swebench.batch import run_batch
from delegation_study.swebench.run import PROTOCOL_VERSION

RERUN_CELLS = {RERUN_CELLS!r}
PROTOCOL_EXPECTED = "{PROTOCOL_EXPECTED}"
HEAD_SHA = "{head}"

log.info("HEAD=%s Protocol: %s (expected: %s)", HEAD_SHA, PROTOCOL_VERSION, PROTOCOL_EXPECTED)
assert PROTOCOL_VERSION == PROTOCOL_EXPECTED, f"Protocol mismatch: {{PROTOCOL_VERSION}}"

specs = []
for task_id, condition in RERUN_CELLS:
    specs.append({{
        "task_id": task_id,
        "condition": condition,
        "task_set": "{TASK_SET}",
        "replicate": 1,
    }})

log.info("Launching %d rerun episodes with concurrency={CONCURRENCY}", len(specs))
results = run_batch(specs, max_workers={CONCURRENCY})
n_err = sum(1 for r in results if r.get("error"))
log.info("All %d episodes finished. errors=%d. Durable finalizer owns reports.", len(results), n_err)
print("RUNNER_EXIT:0")
'''
    )
    RUNNER_PATH.chmod(0o755)

    FINALIZER_SRC = WORKTREE / "scripts" / "swe_pilot_v6c1_durable_finalizer.py"
    if not FINALIZER_SRC.is_file():
        sys.exit(f"ERROR: missing {FINALIZER_SRC}")
    FINALIZER_PATH.write_text(FINALIZER_SRC.read_text())
    FINALIZER_PATH.chmod(0o755)

    runner_cmd = (
        f"caffeinate -dis {PYTHON} {RUNNER_PATH} 2>&1 | tee -a {LOG_PATH} ; "
        f"echo 'Batch complete. Exit code:' $?"
    )
    finalizer_cmd = (
        f"caffeinate -dis {PYTHON} {FINALIZER_PATH} "
        f"--batch-id {BATCH_ID} --head {head}"
    )

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", SESSION, "-x", "220", "-y", "50"],
        check=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", SESSION, f"cd {WORKTREE} && {runner_cmd}", "Enter"],
        check=True,
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", FINALIZER_SESSION, "-x", "160", "-y", "40", finalizer_cmd],
        check=True,
    )

    print(f"\nLaunched tmux session: {SESSION}")
    print(f"Launched durable finalizer: {FINALIZER_SESSION}")
    print(f"Monitor: tmux attach -t {SESSION}")
    print(f"Finalizer: tmux attach -t {FINALIZER_SESSION}")
    print(f"Log: tail -f {LOG_PATH}")
    print(f"Finalizer log: {BATCH_DIR / 'durable_finalizer.log'}")
    print(f"\nBatch ID: {BATCH_ID}")
    print(f"Status: {STATUS_PATH}")
    print(f"Report will be: {REPORT_OUT}")
    print(f"CSV will be: {CSV_OUT}")
    print(f"Finalizer path: {FINALIZER_PATH}")


if __name__ == "__main__":
    main()
