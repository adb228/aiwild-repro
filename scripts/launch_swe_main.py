#!/usr/bin/env python3
"""Launch SWE main: 8 A-2 issues × A0/A1/P0/P1 on swe-0.3.3.

Session: swe-main
Task set: main
Concurrency: 3
Protocol: swe-0.3.3
Caps: frozen swebench.caps in config/manifest.yaml

Do NOT run from a different worktree. Must run from <REPO>
Do not merge swe-adapter into main.
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
    f"swe-main-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    f"-{uuid.uuid4().hex[:8]}"
)
SESSION = "swe-main"
FINALIZER_SESSION = "swe-main-finalizer"
TASK_SET = "main"
PROTOCOL_EXPECTED = "swe-0.3.3"
CONCURRENCY = 3
TASK_IDS = [
    "sympy__sympy-24562",
    "django__django-13297",
    "matplotlib__matplotlib-24149",
    "scikit-learn__scikit-learn-13496",
    "pytest-dev__pytest-10081",
    "sympy__sympy-12481",
    "django__django-14855",
    "matplotlib__matplotlib-25332",
]
RESERVE = [
    "scikit-learn__scikit-learn-13439",
    "sympy__sympy-19495",
    "django__django-16100",
    "matplotlib__matplotlib-20676",
    "scikit-learn__scikit-learn-13142",
    "sympy__sympy-15345",
    "django__django-13551",
]
CELLS = ["A0", "A1", "P0", "P1"]
BATCH_DIR = WORKTREE / "data" / "batches" / BATCH_ID
STATUS_PATH = BATCH_DIR / "status.json"
LOG_PATH = BATCH_DIR / "batch.log"
RUNNER_PATH = BATCH_DIR / "run_batch.py"
FINALIZER_PATH = BATCH_DIR / "durable_finalizer.py"


def expected_image(instance_id: str) -> str:
    key = f"sweb.eval.x86_64.{instance_id}:latest".lower()
    return f"swebench/{key}".replace("__", "_1776_")


def image_exists(tag: str) -> bool:
    r = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    return r.returncode == 0


def pull_image(tag: str) -> bool:
    for attempt in range(1, 4):
        print(f"pull attempt {attempt}/3 {tag}")
        r = subprocess.run(["docker", "pull", tag])
        if r.returncode == 0 and image_exists(tag):
            return True
    return False


def resolve_tasks() -> tuple[list[str], list[str]]:
    resolved = []
    substitutions = []
    reserve_i = 0
    for tid in TASK_IDS:
        tag = expected_image(tid)
        if image_exists(tag):
            resolved.append(tid)
            continue
        print(f"missing image for {tid}; pulling")
        if pull_image(tag):
            resolved.append(tid)
            continue
        replaced = False
        while reserve_i < len(RESERVE):
            nxt = RESERVE[reserve_i]
            reserve_i += 1
            ntag = expected_image(nxt)
            print(f"substitute {tid} -> {nxt} (3 failed pulls)")
            if image_exists(ntag) or pull_image(ntag):
                substitutions.append(f"{tid}->{nxt}")
                resolved.append(nxt)
                replaced = True
                break
        if not replaced:
            sys.exit(f"ERROR: no image for {tid} and reserve exhausted")
    if len(resolved) != 8:
        sys.exit(f"ERROR: expected 8 tasks, got {resolved}")
    return resolved, substitutions


def write_status(tasks: list[str], head: str, substitutions: list[str]) -> None:
    episodes = [
        {"task_id": t, "cell": c, "state": "pending", "run_id": None, "error": None}
        for t in tasks for c in CELLS
    ]
    STATUS_PATH.write_text(json.dumps({
        "batch_id": BATCH_ID,
        "checkout": str(WORKTREE),
        "commit": head,
        "protocol_version": PROTOCOL_EXPECTED,
        "concurrency": CONCURRENCY,
        "task_set": TASK_SET,
        "tasks": tasks,
        "substitutions": substitutions,
        "n": len(episodes),
        "episodes": episodes,
    }, indent=2) + "\n")


def main() -> None:
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=WORKTREE, text=True
    ).strip()
    if branch != "swe-adapter":
        sys.exit(f"ERROR: must launch from swe-adapter, not {branch!r}")
    for session in (SESSION, FINALIZER_SESSION):
        if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
            sys.exit(f"ERROR: tmux session {session} already exists")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=WORKTREE, text=True).strip()
    tasks, substitutions = resolve_tasks()
    if substitutions:
        print("substitutions:", substitutions)
    write_status(tasks, head, substitutions)

    RUNNER_PATH.write_text(
        f'''#!/usr/bin/env python3
import logging, sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("{LOG_PATH}")])
log = logging.getLogger("swe_main")
sys.path.insert(0, "{WORKTREE}/src")
from delegation_study.swebench.batch import run_batch
from delegation_study.swebench.run import PROTOCOL_VERSION
assert PROTOCOL_VERSION == "{PROTOCOL_EXPECTED}", PROTOCOL_VERSION
TASKS = {tasks!r}
CELLS = {CELLS!r}
specs = [{{"task_id": t, "condition": c, "task_set": "{TASK_SET}", "replicate": 1}} for t in TASKS for c in CELLS]
assert len(specs) == 32, len(specs)
log.info("HEAD={head} protocol=%s n=%d concurrency={CONCURRENCY}", PROTOCOL_VERSION, len(specs))
results = run_batch(specs, max_workers={CONCURRENCY})
n_err = sum(1 for r in results if r.get("error"))
log.info("All %d episodes finished. errors=%d. Durable finalizer owns reports.", len(results), n_err)
print("RUNNER_EXIT:0")
'''
    )
    RUNNER_PATH.chmod(0o755)
    src = WORKTREE / "scripts" / "swe_main_durable_finalizer.py"
    if not src.is_file():
        sys.exit(f"ERROR: missing {src}")
    FINALIZER_PATH.write_text(src.read_text())
    FINALIZER_PATH.chmod(0o755)
    runner_cmd = (
        f"caffeinate -dis {PYTHON} {RUNNER_PATH} 2>&1 | tee -a {LOG_PATH} ; "
        f"echo 'Batch complete. Exit code:' $?"
    )
    finalizer_cmd = (
        f"caffeinate -dis {PYTHON} {FINALIZER_PATH} --batch-id {BATCH_ID} --head {head}"
    )
    subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, "-x", "220", "-y", "50"], check=True)
    subprocess.run(["tmux", "send-keys", "-t", SESSION, f"cd {WORKTREE} && {runner_cmd}", "Enter"], check=True)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", FINALIZER_SESSION, "-x", "160", "-y", "40", finalizer_cmd],
        check=True,
    )
    print(f"Launched {SESSION} + {FINALIZER_SESSION}")
    print(f"Batch {BATCH_ID} HEAD={head} n=32 concurrency={CONCURRENCY}")
    print(f"Log {LOG_PATH}")


if __name__ == "__main__":
    main()
