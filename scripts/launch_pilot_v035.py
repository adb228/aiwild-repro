#!/usr/bin/env python3
"""Launch swe-0.3.5 pilot: 3 tasks × 4 cells = 12 episodes.

Session: swe-pilot-v035
Protocol: swe-0.3.5
Concurrency: 3
Caps: frozen swebench.caps (worker 115/110, parent 100/100)
Tasks: astropy__astropy-7606, sympy__sympy-19783, django__django-13112
Conditions: A0, A1, P0, P1
Task set: pilot

The batch runner executes episodes and retries infrastructure failures
(connection / 5xx / 529 / 429) up to 3 attempts per pair. A separate
durable_finalizer (tmux session swe-pilot-v035-finalizer) waits for
each of the 12 pairs' final attempt, then gates.

Do NOT run from a different worktree. Must run from <REPO>
Do NOT merge into main. Do NOT launch SWE main from this script.
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
import uuid
from pathlib import Path

WORKTREE = Path("<REPO>")
PYTHON = str(WORKTREE / ".venv-swe-035" / "bin" / "python")
BATCH_ID = (
    f"swe-pilot-v035-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    f"-{uuid.uuid4().hex[:8]}"
)
SESSION = "swe-pilot-v035"
FINALIZER_SESSION = "swe-pilot-v035-finalizer"
TASK_SET = "pilot"
TASKS = [
    "astropy__astropy-7606",
    "sympy__sympy-19783",
    "django__django-13112",
]
CONDITIONS = ["A0", "A1", "P0", "P1"]
CONCURRENCY = 3
MAX_TURNS = 115
MAX_TOOL_CALLS = 110
PROTOCOL_EXPECTED = "swe-0.3.5"

BATCH_DIR = WORKTREE / "data" / "batches" / BATCH_ID
BATCH_DIR.mkdir(parents=True, exist_ok=True)
STATUS_PATH = BATCH_DIR / "status.json"
LOG_PATH = BATCH_DIR / "batch.log"
REPORT_OUT = WORKTREE / "data" / "dev" / "swe" / "pilot_report_v035.md"
CSV_OUT = WORKTREE / "data" / "dev" / "swe" / "usage_for_caps_v035.csv"
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
        "caps": "frozen_swebench",
        "created_at": _now(),
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
    if branch != "swe-0.3.5":
        sys.exit(f"ERROR: must launch from swe-0.3.5, not {branch!r}")

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
    print(f"Tasks: {TASKS}")
    print(f"Conditions: {CONDITIONS}")
    print(f"Total episodes: {len(TASKS) * len(CONDITIONS)}")

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
        }
        for task_id in TASKS
        for condition in CONDITIONS
    ]
    write_status(episodes_status, head)
    print(f"Status file: {STATUS_PATH}")

    RUNNER_PATH.write_text(
        f'''#!/usr/bin/env python3
"""Inner batch runner for pilot v035. Runs inside tmux session swe-pilot-v035."""
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
log = logging.getLogger("pilot_v035")
sys.path.insert(0, "{WORKTREE}/src")

from delegation_study.swebench.batch import run_batch
from delegation_study.swebench.run import PROTOCOL_VERSION

TASKS = {TASKS!r}
CONDITIONS = {CONDITIONS!r}
PROTOCOL_EXPECTED = "{PROTOCOL_EXPECTED}"
HEAD_SHA = "{head}"
MAX_INFRA_ATTEMPTS = 3
INFRA_MARKERS = (
    "connection error",
    "apiconnectionerror",
    "overloaded",
    "too many requests",
    "rate limit",
    "ratelimit",
    "error code: 429",
    "error code: 529",
    "error code: 500",
    "error code: 502",
    "error code: 503",
    "error code: 504",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def is_infra_error(err):
    if not err:
        return False
    low = str(err).lower()
    return any(m in low for m in INFRA_MARKERS)

log.info("HEAD=%s Protocol: %s (expected: %s)", HEAD_SHA, PROTOCOL_VERSION, PROTOCOL_EXPECTED)
assert PROTOCOL_VERSION == PROTOCOL_EXPECTED, f"Protocol mismatch: {{PROTOCOL_VERSION}}"

specs = []
for task_id in TASKS:
    for condition in CONDITIONS:
        specs.append({{
            "task_id": task_id,
            "condition": condition,
            "task_set": "{TASK_SET}",
            "replicate": 1,
        }})

log.info("Launching %d episodes with concurrency={CONCURRENCY}", len(specs))
results = run_batch(specs, max_workers={CONCURRENCY})
by_key = {{(r["spec"]["task_id"], r["spec"]["condition"]): r for r in results}}
for attempt in range(2, MAX_INFRA_ATTEMPTS + 1):
    retry_specs = [
        {{**r["spec"], "replicate": attempt}}
        for r in by_key.values()
        if r.get("error") and is_infra_error(r["error"])
    ]
    if not retry_specs:
        break
    log.info(
        "infra retry attempt %d/%d: %s",
        attempt,
        MAX_INFRA_ATTEMPTS,
        ", ".join(f"{{s['task_id']}}/{{s['condition']}}" for s in retry_specs),
    )
    more = run_batch(retry_specs, max_workers={CONCURRENCY})
    for r in more:
        by_key[(r["spec"]["task_id"], r["spec"]["condition"])] = r
n_err = sum(1 for r in by_key.values() if r.get("error"))
log.info("All %d pairs finished (final attempts). errors=%d. Durable finalizer owns reports.", len(by_key), n_err)
from pathlib import Path
Path("{BATCH_DIR / 'retries_complete'}").write_text(
    f"head={{HEAD_SHA}}\\nerrors={{n_err}}\\nembedded_infra_retry=1\\n"
)
print("RUNNER_EXIT:0")
'''
    )
    RUNNER_PATH.chmod(0o755)

    FINALIZER_SRC = WORKTREE / "scripts" / "swe_pilot_v035_durable_finalizer.py"
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
