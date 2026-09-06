#!/usr/bin/env python3
"""Durable finalizer for swe-0.3.4 main crash reruns (5 interface cells)."""
from __future__ import annotations

import argparse
import csv
import datetime
import fcntl
import io
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time

WORKTREE = pathlib.Path("<REPO>")
PROTOCOL = "swe-0.3.4"
POLL_SECONDS = 30
POST_DEATH_GRACE_SECONDS = 180
POST_COMPLETE_GRACE_SECONDS = 900

sys.path.insert(0, str(WORKTREE / "src"))
from delegation_study.qc import qc_episode
from delegation_study.validate import validate_episode, validate_event_log

parser = argparse.ArgumentParser()
parser.add_argument("--batch-id", required=True)
parser.add_argument("--head", required=True)
ARGS = parser.parse_args()
BATCH_ID = ARGS.batch_id
HEAD = ARGS.head
BATCH_DIR = WORKTREE / "data/batches" / BATCH_ID
STATUS_PATH = BATCH_DIR / "status.json"
LOG_PATH = BATCH_DIR / "batch.log"
REPORT_OUT = WORKTREE / "data/dev/swe/main_rerun_034_report.md"
CSV_OUT = WORKTREE / "data/dev/swe/usage_for_caps_main_rerun034.csv"
LOCK_PATH = BATCH_DIR / "durable_finalizer.lock"
DONE_PATH = BATCH_DIR / "durable_finalizer.done"
PID_PATH = BATCH_DIR / "durable_finalizer.pid"
RUNNER_NEEDLE = str(BATCH_DIR / "run_batch.py")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BATCH_DIR / "durable_finalizer.log"),
    ],
)
log = logging.getLogger("swe_034_durable")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _atomic_write(path: pathlib.Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def runner_pids() -> list[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", RUNNER_NEEDLE], text=True)
    except subprocess.CalledProcessError:
        return []
    return [int(line) for line in out.splitlines() if line.strip().isdigit()]


def docker_swe_ids() -> list[str]:
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--filter", "label=benchmark=swebench",
             "--format", '{{.ID}} {{.Names}} {{.Label "swe-run-id"}}'],
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def parse_batch_log() -> dict[tuple[str, str], dict]:
    states: dict[tuple[str, str], dict] = {}
    if not LOG_PATH.is_file():
        return states
    start_re = re.compile(r"batch: start\s+(\S+)\s+/\s+(\S+)")
    finish_re = re.compile(r"batch: finish\s+(\S+)\s+/\s+(\S+)\s+run_id=(\S+)")
    error_re = re.compile(r"batch: error\s+(\S+)\s+/\s+(\S+):\s+(.*)")
    for line in LOG_PATH.read_text(errors="replace").splitlines():
        m = start_re.search(line)
        if m:
            key = (m.group(1), m.group(2))
            states.setdefault(key, {"state": "running", "run_id": None, "error": None})
            if states[key]["state"] not in {"finished", "error"}:
                states[key]["state"] = "running"
            continue
        m = finish_re.search(line)
        if m:
            states[(m.group(1), m.group(2))] = {
                "state": "finished", "run_id": m.group(3), "error": None,
            }
            continue
        m = error_re.search(line)
        if m:
            states[(m.group(1), m.group(2))] = {
                "state": "error", "run_id": None,
                "error": m.group(3).split("\n", 1)[0][:500],
            }
    return states


def load_status() -> dict:
    if STATUS_PATH.is_file():
        try:
            return json.loads(STATUS_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def expected_cells(status: dict) -> list[tuple[str, str]]:
    cells = []
    for ep in status.get("episodes") or []:
        task_id = ep.get("task_id")
        cell = ep.get("cell") or ep.get("condition")
        if task_id and cell:
            cells.append((task_id, cell))
    return cells


def wait_for_terminal(n_expected: int) -> dict:
    death_since = None
    complete_since = None
    while True:
        log_states = parse_batch_log()
        n_term = sum(1 for s in log_states.values() if s["state"] in {"finished", "error"})
        pids = runner_pids()
        dockers = docker_swe_ids()
        log.info(
            "progress terminals=%d/%d runner_pids=%s docker=%d",
            n_term, n_expected, pids, len(dockers),
        )
        finished_marker = LOG_PATH.is_file() and "episodes finished" in LOG_PATH.read_text(errors="replace")
        if n_term >= n_expected:
            if not pids:
                return log_states
            if finished_marker:
                if complete_since is None:
                    complete_since = time.time()
                if time.time() - complete_since >= 60:
                    return log_states
            elif complete_since is None:
                complete_since = time.time()
            elif time.time() - complete_since >= POST_COMPLETE_GRACE_SECONDS:
                return log_states
        elif not pids:
            if death_since is None:
                death_since = time.time()
                log.warning("Runner dead with %d/%d terminals", n_term, n_expected)
            elif time.time() - death_since >= POST_DEATH_GRACE_SECONDS:
                return log_states
        else:
            death_since = None
            complete_since = None
        time.sleep(POLL_SECONDS)


def finalize(log_states: dict, cells: list[tuple[str, str]]) -> str:
    lines = [
        f"# SWE main swe-0.3.4 rerun report ({BATCH_ID})",
        f"Protocol: `{PROTOCOL}` @ `{HEAD}`",
        f"Generated: {_now()}",
        f"Cells: {', '.join(f'{t}/{c}' for t, c in cells)}",
        "",
        "## Episodes",
        "",
    ]
    csv_rows = []
    n_valid = 0
    n_total = 0
    for task_id, condition in cells:
        st = log_states.get((task_id, condition), {"state": "missing", "run_id": None, "error": None})
        run_id = st.get("run_id")
        ep_path = WORKTREE / "data/runs" / run_id / "episode.json" if run_id else None
        if st["state"] != "finished" or not ep_path or not ep_path.is_file():
            err = st.get("error") or st["state"]
            lines.append(f"- **{task_id}/{condition}**: {err}")
            continue
        ep = json.loads(ep_path.read_text())
        n_total += 1
        issues = validate_episode(ep, ep_path)
        evp = ep_path.parent / "events.jsonl"
        event_issues = validate_event_log(evp, ep) if evp.is_file() else ["missing events"]
        flags = qc_episode(ep, record_path=ep_path)
        valid = not issues and not event_issues
        if valid:
            n_valid += 1
        em = (ep.get("env_metrics") or {}).get("swebench") or {}
        resolved = em.get("final_resolved")
        lines.append(
            f"- **{task_id}/{condition}** run={run_id[:8]} valid={valid} "
            f"resolved={resolved} qc={[f.code for f in flags] if flags else 'CLEAN'} "
            f"status={ep.get('status')} protocol={ep.get('protocol_version')}"
        )
        usage = ep.get("usage") or {}
        for role in ("worker", "parent"):
            ru = usage.get(role) or {}
            csv_rows.append({
                "run_id": run_id, "task_id": task_id, "condition": condition,
                "role": role, "status": ep.get("status", ""),
                "turns": ru.get("model_calls", ""), "tool_calls": ru.get("tool_calls", ""),
                "input_tokens": ru.get("input_tokens", ""),
                "output_tokens": ru.get("output_tokens", ""),
                "cache_read_input_tokens": ru.get("cache_read_input_tokens", ""),
                "cache_creation_input_tokens": ru.get("cache_creation_input_tokens", ""),
                "wall_seconds": ru.get("wall_seconds", ""),
                "final_resolved": resolved,
                "schema_git_commit": (ep.get("reproducibility") or {}).get("schema_git_commit", ""),
            })
    lines += ["", "## Summary", f"- valid {n_valid}/{len(cells)} present={n_total}", ""]
    _atomic_write(REPORT_OUT, "\n".join(lines) + "\n")
    buf = io.StringIO()
    if csv_rows:
        w = csv.DictWriter(buf, fieldnames=list(csv_rows[0].keys()))
        w.writeheader()
        w.writerows(csv_rows)
    _atomic_write(CSV_OUT, buf.getvalue())
    try:
        subprocess.run(["git", "add", "-f", str(REPORT_OUT), str(CSV_OUT)], cwd=WORKTREE, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"], cwd=WORKTREE, capture_output=True, text=True, check=True
        )
        if staged.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", f"SWE main 0.3.4 rerun report: {n_valid}/{len(cells)} valid {BATCH_ID}"],
                cwd=WORKTREE, check=True,
            )
        subprocess.run(["git", "push", "origin", "swe-adapter"], cwd=WORKTREE, check=False)
    except subprocess.CalledProcessError as exc:
        log.error("git failed: %s", exc)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=WORKTREE, text=True).strip()
    _atomic_write(DONE_PATH, json.dumps({
        "batch_id": BATCH_ID, "gate_records": n_valid, "n_union": len(cells),
        "commit": sha, "generated": _now(),
    }, indent=2) + "\n")
    log.info("finalizer done valid=%s/%s commit=%s", n_valid, len(cells), sha)
    return sha


def main() -> int:
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()) + "\n")
    lock_fh = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("another finalizer holds the lock")
        return 0
    status = load_status()
    cells = expected_cells(status)
    n_expected = len(cells) or 5
    log.info("034 finalizer start head=%s n_expected=%s cells=%s", HEAD, n_expected, cells)
    states = wait_for_terminal(n_expected)
    time.sleep(5)
    finalize(parse_batch_log() or states, cells)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
