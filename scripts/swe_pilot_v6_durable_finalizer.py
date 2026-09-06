#!/usr/bin/env python3
"""Durable swe-0.3.2 pilot v6 finalizer (5 reruns + 7 retained v5).

Waits for 5/5 rerun terminals (or an honest shortfall after the runner
dies). Gates the union of those reruns and the 7 retained v5 records.
Writes mixed-provenance reports only after a terminal state.

Do not launch SWE main from this script. If main is launched later it
must run entirely on swe-0.3.2 (no mixed provenance).
"""
from __future__ import annotations

import argparse
import csv
import datetime
import fcntl
import hashlib
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
PROTOCOL_RERUN = "swe-0.3.2"
PROTOCOL_RETAINED = "swe-0.3.1"
RETAINED_HEAD_PREFIX = "<commit>"
TASKS = [
    "django__django-12155",
    "scikit-learn__scikit-learn-14983",
    "matplotlib__matplotlib-25479",
]
CONDITIONS = ["A0", "A1", "P0", "P1"]
UNION_CELLS = [(t, c) for t in TASKS for c in CONDITIONS]
RERUN_CELLS = [
    ("django__django-12155", "P1"),
    ("scikit-learn__scikit-learn-14983", "A1"),
    ("scikit-learn__scikit-learn-14983", "P0"),
    ("scikit-learn__scikit-learn-14983", "P1"),
    ("matplotlib__matplotlib-25479", "P0"),
]
RETAINED_V5: dict[tuple[str, str], str] = {
    ("django__django-12155", "A0"): "9e3dd119-7a58-475f-8605-032de13337ca",
    ("django__django-12155", "A1"): "cae87060-531c-4676-b1d8-486bbcfb4561",
    ("django__django-12155", "P0"): "d6b236a4-e246-43ca-8ca9-aeebee408bd9",
    ("scikit-learn__scikit-learn-14983", "A0"): "d6a6d67c-1fd6-4c19-b195-d3795f015f7e",
    ("matplotlib__matplotlib-25479", "A0"): "8edc54d4-f810-4c56-ae3d-94721aa240ce",
    ("matplotlib__matplotlib-25479", "A1"): "ffa92d77-6f83-49d5-9b6e-300f57153cba",
    ("matplotlib__matplotlib-25479", "P1"): "1f0a5734-d152-4916-b055-528ae2f3cd6a",
}
N_RERUN = len(RERUN_CELLS)
POLL_SECONDS = 30
POST_DEATH_GRACE_SECONDS = 180
POST_COMPLETE_GRACE_SECONDS = 900

sys.path.insert(0, str(WORKTREE / "src"))

from delegation_study.qc import qc_episode
from delegation_study.swebench.run import remote_matches_repo
from delegation_study.swebench.task_loader import load_task
from delegation_study.validate import validate_episode, validate_event_log


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Durable swe-0.3.2 pilot v6 finalizer")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--head", required=True)
    return parser.parse_args()


ARGS = _parse_args()
BATCH_ID = ARGS.batch_id
HEAD_RERUN = ARGS.head
BATCH_DIR = WORKTREE / "data/batches" / BATCH_ID
STATUS_PATH = BATCH_DIR / "status.json"
LOG_PATH = BATCH_DIR / "batch.log"
REPORT_OUT = WORKTREE / "data/dev/swe/pilot_report_v6.md"
CSV_OUT = WORKTREE / "data/dev/swe/usage_for_caps_v6.csv"
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
log = logging.getLogger("pilot_v6_durable")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _atomic_write(path: pathlib.Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_provenance() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for task_id in TASKS:
        task = load_task(task_id)
        out[task_id] = {"repo": task.repo, "base_commit": task.base_commit}
    return out


def runner_pids() -> list[int]:
    try:
        out = subprocess.check_output(["pgrep", "-f", RUNNER_NEEDLE], text=True)
    except subprocess.CalledProcessError:
        return []
    return [int(line) for line in out.splitlines() if line.strip().isdigit()]


def docker_swe_ids() -> list[str]:
    try:
        out = subprocess.check_output(
            [
                "docker", "ps",
                "--filter", "label=benchmark=swebench",
                "--format", '{{.ID}} {{.Names}} {{.Label "swe-run-id"}}',
            ],
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
    text = LOG_PATH.read_text(errors="replace")
    for line in text.splitlines():
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
                "state": "finished",
                "run_id": m.group(3),
                "error": None,
            }
            continue
        m = error_re.search(line)
        if m:
            states[(m.group(1), m.group(2))] = {
                "state": "error",
                "run_id": None,
                "error": m.group(3).split("\n", 1)[0][:500],
            }
    return states


def load_episode(run_id: str) -> tuple[dict | None, pathlib.Path | None]:
    path = WORKTREE / "data/runs" / run_id / "episode.json"
    if not path.is_file():
        return None, path
    try:
        return json.loads(path.read_text()), path
    except json.JSONDecodeError:
        return None, path


def terminal_count(log_states: dict) -> int:
    return sum(1 for s in log_states.values() if s["state"] in {"finished", "error"})


def runner_finished_marker() -> bool:
    if not LOG_PATH.is_file():
        return False
    text = LOG_PATH.read_text(errors="replace")
    return "All " in text and "episodes finished" in text


def wait_for_terminal() -> dict:
    death_since: float | None = None
    complete_since: float | None = None
    while True:
        log_states = parse_batch_log()
        n_term = terminal_count(log_states)
        pids = runner_pids()
        dockers = docker_swe_ids()
        started = [k for k, s in log_states.items() if s["state"] == "running"]
        queued = [k for k in RERUN_CELLS if k not in log_states]
        log.info(
            "progress terminals=%d/%d running=%d queued=%d runner_pids=%s docker=%d",
            n_term, N_RERUN, len(started), len(queued), pids, len(dockers),
        )
        if n_term >= N_RERUN:
            if not pids:
                log.info("%d terminals and runner gone; proceeding.", N_RERUN)
                return log_states
            if runner_finished_marker():
                if complete_since is None:
                    complete_since = time.time()
                if time.time() - complete_since >= 60:
                    log.info("Runner marked all episodes finished; proceeding.")
                    return log_states
            else:
                if complete_since is None:
                    complete_since = time.time()
                    log.info("%d terminals; waiting for runner to exit.", N_RERUN)
                elif time.time() - complete_since >= POST_COMPLETE_GRACE_SECONDS:
                    log.warning(
                        "%d terminals but runner still alive after %ss; honest takeover.",
                        N_RERUN, POST_COMPLETE_GRACE_SECONDS,
                    )
                    return log_states
        elif not pids:
            if death_since is None:
                death_since = time.time()
                log.warning(
                    "Runner dead with %d/%d terminals; starting grace period.",
                    n_term, N_RERUN,
                )
            elif time.time() - death_since >= POST_DEATH_GRACE_SECONDS:
                log.warning(
                    "Honest shortfall: runner dead, %d/%d terminals.",
                    n_term, N_RERUN,
                )
                return log_states
        else:
            death_since = None
            complete_since = None
        time.sleep(POLL_SECONDS)


def _events(run_id: str) -> list[dict]:
    path = WORKTREE / "data/runs" / run_id / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def assistant_text_ok(run_id: str) -> bool:
    events = _events(run_id)
    mc = [e for e in events if e.get("event_type") == "model_call"]
    if not mc:
        return False
    run_dir = WORKTREE / "data/runs" / run_id
    for e in mc:
        payload = e.get("payload") or {}
        has_inline = "assistant_text" in payload
        has_file = "raw_payload_path" in payload
        if has_inline == has_file:
            return False
        if has_file and not (run_dir / payload["raw_payload_path"]).is_file():
            return False
        if has_inline and payload.get("assistant_text") is None:
            return False
    return True


def provenance_ok(
    ep: dict,
    expected: dict[str, dict[str, str]],
    *,
    retained: bool,
) -> tuple[bool, str]:
    task_id = ep.get("task_id")
    exp = expected.get(task_id)
    if not exp:
        return False, f"unknown task_id {task_id}"
    em = (ep.get("env_metrics") or {}).get("swebench") or {}
    repro = ep.get("reproducibility") or {}
    protocol = ep.get("protocol_version")
    want_protocol = PROTOCOL_RETAINED if retained else PROTOCOL_RERUN
    if protocol != want_protocol:
        return False, f"protocol_version={protocol!r} expected {want_protocol}"
    schema_git = str(repro.get("schema_git_commit") or "")
    if retained:
        if not schema_git.startswith(RETAINED_HEAD_PREFIX):
            return False, (
                f"retained schema_git_commit={schema_git!r} "
                f"expected prefix {RETAINED_HEAD_PREFIX}"
            )
    elif schema_git != HEAD_RERUN:
        return False, f"rerun schema_git_commit={schema_git!r} expected {HEAD_RERUN}"
    repo = em.get("task_repo")
    if repo != exp["repo"]:
        return False, f"task_repo={repo!r} expected {exp['repo']!r}"
    base = exp["base_commit"]
    if repro.get("benchmark_git_commit") != base:
        return False, (
            f"benchmark_git_commit={repro.get('benchmark_git_commit')!r} expected {base}"
        )
    head = em.get("worker_start_commit") or em.get("git_head")
    if head != base:
        return False, f"container HEAD={head!r} expected base {base}"
    remote = (em.get("worker_start_remote") or em.get("git_remote") or "")
    fallback = f"(no remotes; task_repo={exp['repo']})"
    if not (remote_matches_repo(remote, exp["repo"]) or exp["repo"] in remote or remote.strip() == fallback):
        return False, f"remote does not name {exp['repo']!r}"
    return True, "ok"


def truncation_count(run_id: str, role: str) -> int:
    d = WORKTREE / "data/runs" / run_id / "logs" / "tool_outputs"
    if not d.is_dir():
        return 0
    n = 0
    for path in d.glob(f"{role}_*_tool_output.json"):
        try:
            obj = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if obj.get("_output_truncated") is True:
            n += 1
    return n


def patch_info(run_dir: pathlib.Path, rel_or_abs: str | None, recorded_sha: str | None) -> tuple[bool, bool, str]:
    if not rel_or_abs:
        return False, False, ""
    path = pathlib.Path(rel_or_abs)
    if not path.is_absolute():
        path = run_dir / path
    if not path.is_file():
        return False, False, recorded_sha or ""
    text = path.read_text(errors="replace")
    sha = recorded_sha or _sha256_file(path)
    return True, bool(text.strip()), sha


def finalize(log_states: dict) -> tuple[bool, str]:
    expected = expected_provenance()
    status = {}
    if STATUS_PATH.is_file():
        try:
            status = json.loads(STATUS_PATH.read_text())
        except json.JSONDecodeError:
            status = {}
    status.setdefault("batch_id", BATCH_ID)
    status.setdefault("checkout", str(WORKTREE))
    status.setdefault("commit", HEAD_RERUN)
    status.setdefault("concurrency", 3)
    status.setdefault("protocol_version", PROTOCOL_RERUN)
    status["finalized_at"] = _now()
    status["finalizer"] = "durable_tmux_caffeinate"
    status["provenance"] = {
        "mixed": True,
        "retained_v5_head": RETAINED_HEAD_PREFIX,
        "retained_protocol": PROTOCOL_RETAINED,
        "rerun_head": HEAD_RERUN,
        "rerun_protocol": PROTOCOL_RERUN,
        "main_constraint": "If SWE main is launched later it must run entirely on swe-0.3.2 (no mixed provenance).",
    }
    by_cell = {
        (e.get("task_id"), e.get("cell")): e
        for e in status.get("episodes", [])
    }
    if not status.get("episodes"):
        status["episodes"] = [
            {
                "task_id": t, "cell": c, "state": "pending", "run_id": None,
                "pid": None, "started_at": None, "completed_at": None,
                "exit_code": None, "error": None, "wall_seconds": None,
            }
            for t, c in RERUN_CELLS
        ]
        by_cell = {(e["task_id"], e["cell"]): e for e in status["episodes"]}

    csv_rows: list[dict] = []
    report_lines = [
        f"# Pilot v6 Report ({BATCH_ID})",
        f"Protocol: mixed — retained v5 `{PROTOCOL_RETAINED}` @ `{RETAINED_HEAD_PREFIX}`; "
        f"reruns `{PROTOCOL_RERUN}` @ `{HEAD_RERUN}`",
        f"Rerun HEAD: {HEAD_RERUN}",
        f"Generated: {_now()}",
        "Caps: 80 turns / 160 tool calls per role (observation)",
        "Launch: 5 rerun cells only. Gate: union of 7 retained v5 + 5 reruns = 12 logical cells.",
        "Finalizer: durable tmux/caffeinate watcher (does not interrupt the batch runner)",
        "",
        "## Provenance (mixed)",
        "",
        f"- Retained v5 records: swe-adapter `{RETAINED_HEAD_PREFIX}` protocol `{PROTOCOL_RETAINED}`",
        f"- Reruns: swe-adapter `{HEAD_RERUN}` protocol `{PROTOCOL_RERUN}`",
        "- If SWE **main** is launched later, it must run **entirely on swe-0.3.2** (no mixed provenance).",
        "",
        "## Episodes",
        "",
    ]

    n_resolved = 0
    n_total = 0
    n_valid = 0
    n_a_no_patch = 0
    n_p_diff_export = 0
    n_p_nonempty = 0
    n_a1_scratch = 0
    n_assistant = 0
    n_provenance = 0
    n_a_cells = 0
    n_p_cells = 0
    n_a1_cells = 0
    discrepancies: list[str] = []
    gate_failures: list[str] = []
    missing: list[str] = []

    for task_id, condition in UNION_CELLS:
        is_a = condition.startswith("A")
        is_p = condition.startswith("P")
        retained = (task_id, condition) in RETAINED_V5
        if is_a:
            n_a_cells += 1
        if is_p:
            n_p_cells += 1
        if condition == "A1":
            n_a1_cells += 1

        if retained:
            run_id = RETAINED_V5[(task_id, condition)]
            st = {"state": "finished", "run_id": run_id, "error": None}
            cohort = "retained_v5"
            cell_head = RETAINED_HEAD_PREFIX
        else:
            st = log_states.get(
                (task_id, condition),
                {"state": "missing", "run_id": None, "error": None},
            )
            run_id = st.get("run_id")
            cohort = "rerun"
            cell_head = HEAD_RERUN[:8]

        ep, ep_path = load_episode(run_id) if run_id else (None, None)
        slot = by_cell.get((task_id, condition), {})

        if st["state"] == "missing":
            missing.append(f"{task_id}/{condition}: never started")
            report_lines.append(f"- **{task_id}/{condition}** ({cohort}): MISSING (never started)")
            _append_empty_role_rows(csv_rows, run_id or "NONE", task_id, condition, "missing", cohort)
            slot.update({"state": "missing", "error": "never started", "cohort": cohort})
            continue

        if st["state"] == "error" or ep is None:
            err = st.get("error") or "episode.json absent after batch error/finish"
            discrepancies.append(f"{task_id}/{condition}: ERROR {err}")
            report_lines.append(f"- **{task_id}/{condition}** ({cohort}): ERROR: {err}")
            _append_empty_role_rows(csv_rows, run_id or "NONE", task_id, condition, "error", cohort)
            slot.update({"state": "error", "run_id": run_id, "error": err, "cohort": cohort})
            continue

        assert ep_path is not None
        events_path = ep_path.parent / "events.jsonl"
        issues = validate_episode(ep, ep_path)
        event_issues = (
            validate_event_log(events_path, ep)
            if events_path.is_file()
            else ["missing events.jsonl"]
        )
        flags = qc_episode(ep, record_path=ep_path)
        valid = not issues and not event_issues
        if valid:
            n_valid += 1
        else:
            discrepancies.append(
                f"{run_id} ({task_id}/{condition}): validation={[str(i) for i in issues]} "
                f"events={[str(i) for i in event_issues]}"
            )
        if flags:
            discrepancies.append(
                f"{run_id} ({task_id}/{condition}): QC flags={[f.code for f in flags]}"
            )

        em_swe = (ep.get("env_metrics") or {}).get("swebench") or {}
        final_resolved = em_swe.get("final_resolved")
        if final_resolved == 1:
            n_resolved += 1
        n_total += 1

        prot = ep.get("swe_protocol_usage") or {}
        worker_prot = prot.get("worker") or {}
        parent_prot = prot.get("parent") or {}
        usage = ep.get("usage") or {}
        compliance = ep.get("compliance") or {}
        handoff = ep.get("handoff") or {}
        run_dir = ep_path.parent

        cand_path_recorded = handoff.get("candidate_script_path")
        conventional_cand = run_dir / "handoff" / "candidate.patch"
        cand_present, cand_nonempty, cand_sha = patch_info(
            run_dir, cand_path_recorded, handoff.get("candidate_script_sha256")
        )
        if not cand_present and conventional_cand.is_file():
            cand_present, cand_nonempty, cand_sha = patch_info(
                run_dir, str(conventional_cand), handoff.get("candidate_script_sha256")
            )
        final_present, final_nonempty, final_sha = patch_info(
            run_dir, ep.get("final_script_path"), ep.get("final_script_sha256")
        )

        if is_a and not cand_path_recorded and not conventional_cand.exists():
            n_a_no_patch += 1
        elif is_a:
            gate_failures.append(f"{task_id}/{condition}: A cell has a patch")

        if is_p:
            if cand_present:
                n_p_diff_export += 1
                if cand_nonempty:
                    n_p_nonempty += 1
            elif prot.get("production_artifact_missing"):
                n_p_diff_export += 1
            else:
                gate_failures.append(
                    f"{task_id}/{condition}: P cell missing deterministic diff export"
                )

        if condition == "A1":
            if compliance.get("worker_scratch_destroyed"):
                n_a1_scratch += 1
            else:
                gate_failures.append(f"{task_id}/A1: scratch not destroyed")

        if assistant_text_ok(run_id):
            n_assistant += 1
        else:
            gate_failures.append(f"{task_id}/{condition}: assistant text not persisted")

        prov_ok, prov_msg = provenance_ok(ep, expected, retained=retained)
        if prov_ok:
            n_provenance += 1
        else:
            gate_failures.append(f"{task_id}/{condition}: provenance {prov_msg}")

        resolved_str = (
            "YES" if final_resolved == 1 else ("NO" if final_resolved == 0 else "N/A")
        )
        qc_str = "CLEAN" if not flags else f"FLAGS:{[f.code for f in flags]}"
        report_lines.append(
            f"- **{task_id}/{condition}** cohort={cohort} run={run_id[:8]} "
            f"sha={cell_head} protocol={ep.get('protocol_version')} valid={valid} "
            f"resolved={resolved_str} qc={qc_str} "
            f"end_w={worker_prot.get('end_reason', '?')} "
            f"end_p={parent_prot.get('end_reason', '?')} "
            f"seal_w={worker_prot.get('seal_source', '?')} "
            f"cont_w={worker_prot.get('continued_on_max_tokens', 0)} "
            f"cont_p={parent_prot.get('continued_on_max_tokens', 0)} "
            f"repo={em_swe.get('task_repo')} "
            f"head={(em_swe.get('worker_start_commit') or em_swe.get('git_head'))} "
            f"missing_artifact={prot.get('production_artifact_missing')} "
            f"caps_loosened={prot.get('caps_loosened')} "
            f"status={ep.get('status')}"
        )

        w_usage = usage.get("worker") or {}
        p_usage = usage.get("parent") or {}
        wall_total = ""
        try:
            wall_total = float(w_usage.get("wall_seconds") or 0) + float(
                p_usage.get("wall_seconds") or 0
            )
        except (TypeError, ValueError):
            wall_total = ""

        slot.update({
            "state": "complete" if ep.get("status") == "complete" else ep.get("status"),
            "run_id": run_id,
            "error": None,
            "exit_code": 0 if ep.get("status") == "complete" else 1,
            "started_at": ep.get("started_at"),
            "completed_at": ep.get("completed_at"),
            "wall_seconds": wall_total,
            "cohort": cohort,
        })
        if not retained and (task_id, condition) not in by_cell:
            status.setdefault("episodes", []).append(slot)

        for role, role_usage, role_prot in (
            ("worker", w_usage, worker_prot),
            ("parent", p_usage, parent_prot),
        ):
            if role == "worker":
                submitted = compliance.get("worker_submitted", "")
                submitted_memo = handoff.get("worker_submitted_memo", "")
                submitted_solution = handoff.get("worker_submitted_candidate", "")
            else:
                submitted = compliance.get("parent_finalized", "")
                submitted_memo = ""
                submitted_solution = bool(ep.get("final_script_path")) if ep.get("final_script_path") else False
            csv_rows.append({
                "run_id": run_id,
                "task_id": task_id,
                "condition": condition,
                "role": role,
                "cohort": cohort,
                "protocol_version": ep.get("protocol_version", ""),
                "task_set": ep.get("task_set", "pilot"),
                "status": ep.get("status", "unknown"),
                "rerun_eligible": ep.get("rerun_eligible", ""),
                "submitted": submitted,
                "submitted_memo": submitted_memo,
                "submitted_solution": submitted_solution,
                "memo_source": role_prot.get("memo_source", ""),
                "seal_source": role_prot.get("seal_source", ""),
                "end_reason": role_prot.get("end_reason", ""),
                "continued_on_max_tokens": role_prot.get("continued_on_max_tokens", 0),
                "unexpected_stop_reason": role_prot.get("unexpected_stop_reason", ""),
                "reprompted_on_end_turn": role_prot.get("reprompted_on_end_turn", 0),
                "candidate_diff_present": cand_present,
                "candidate_diff_nonempty": cand_nonempty,
                "candidate_diff_sha256": cand_sha,
                "final_diff_present": final_present,
                "final_diff_nonempty": final_nonempty,
                "final_diff_sha256": final_sha,
                "turns": role_usage.get("model_calls", ""),
                "tool_calls": role_usage.get("tool_calls", ""),
                "input_tokens": role_usage.get("input_tokens", ""),
                "output_tokens": role_usage.get("output_tokens", ""),
                "cache_read_input_tokens": role_usage.get("cache_read_input_tokens", ""),
                "cache_creation_input_tokens": role_usage.get("cache_creation_input_tokens", ""),
                "wall_seconds": role_usage.get("wall_seconds", ""),
                "caps_loosened": prot.get("caps_loosened", ""),
                "prior_swe02_max_turns": prot.get("prior_swe02_max_turns", ""),
                "prior_swe02_max_tool_calls": prot.get("prior_swe02_max_tool_calls", ""),
                "truncation_count": truncation_count(run_id, role),
                "production_artifact_missing": prot.get("production_artifact_missing", ""),
                "final_resolved": final_resolved,
                "schema_git_commit": (ep.get("reproducibility") or {}).get("schema_git_commit", ""),
                "task_repo": em_swe.get("task_repo", ""),
                "worker_start_commit": em_swe.get("worker_start_commit", ""),
                "benchmark_git_commit": (ep.get("reproducibility") or {}).get("benchmark_git_commit", ""),
            })

    rerun_errors = sum(
        1 for k, s in log_states.items()
        if k in set(RERUN_CELLS) and s["state"] == "error"
    )
    shortfall = bool(missing) or n_total < 12

    gate_ok = (
        not shortfall
        and n_valid == 12
        and n_a_no_patch == n_a_cells == 6
        and n_p_diff_export == n_p_cells == 6
        and n_a1_scratch == n_a1_cells == 3
        and n_p_nonempty >= 3
        and n_assistant == n_total == 12
        and n_provenance == n_total == 12
    )

    report_lines += [
        "",
        "## Retained v5 run_ids",
        "",
    ]
    for (t, c), rid in RETAINED_V5.items():
        report_lines.append(f"- {t}/{c}: `{rid}`")
    report_lines += [
        "",
        "## Summary",
        f"- Rerun HEAD: {HEAD_RERUN}",
        f"- Retained HEAD prefix: {RETAINED_HEAD_PREFIX}",
        f"- Protocols: retained={PROTOCOL_RETAINED} rerun={PROTOCOL_RERUN}",
        f"- Total episodes with records: {n_total}",
        f"- Structurally valid: {n_valid}/12",
        f"- Resolved: {n_resolved}/{n_total}",
        f"- A cells without patch: {n_a_no_patch}/{n_a_cells}",
        f"- P cells deterministic diff export: {n_p_diff_export}/{n_p_cells}",
        f"- P workers nonempty diff: {n_p_nonempty}/6",
        f"- A1 scratch destroyed: {n_a1_scratch}/{n_a1_cells}",
        f"- Assistant text persisted: {n_assistant}/{n_total}",
        f"- Provenance repo+base correct: {n_provenance}/{n_total}",
        f"- Rerun batch errors: {rerun_errors}",
        f"- Shortfall: {'YES' if shortfall else 'NO'}",
        f"- Finalizer gate: {'PASS' if gate_ok else 'FAIL'}",
        "",
        "## Main constraint",
        "",
        "Do not launch SWE main as a mixed 0.3.1/0.3.2 union. Main, if launched later, "
        "runs entirely on swe-0.3.2.",
        "",
    ]
    if missing:
        report_lines += ["## Missing / shortfall", ""]
        for d in missing:
            report_lines.append(f"- {d}")
        report_lines.append("")
    if discrepancies:
        report_lines += ["## Discrepancies", ""]
        for d in discrepancies:
            report_lines.append(f"- {d}")
        report_lines.append("")
    if gate_failures:
        report_lines += ["## Gate failures", ""]
        for d in gate_failures:
            report_lines.append(f"- {d}")
        report_lines.append("")

    _atomic_write(STATUS_PATH, json.dumps(status, indent=2, default=str) + "\n")
    _atomic_write(REPORT_OUT, "\n".join(report_lines) + "\n")
    buf = io.StringIO()
    if csv_rows:
        writer = csv.DictWriter(buf, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)
    _atomic_write(CSV_OUT, buf.getvalue())
    log.info("Wrote %s and %s", REPORT_OUT, CSV_OUT)

    commit_sha = ""
    git_ok = True
    try:
        subprocess.run(
            ["git", "add", "-f", str(REPORT_OUT), str(CSV_OUT), str(STATUS_PATH)],
            cwd=str(WORKTREE),
            check=True,
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(WORKTREE),
            check=True,
            capture_output=True,
            text=True,
        )
        if staged.stdout.strip():
            subprocess.run(
                [
                    "git", "commit", "-m",
                    f"Pilot v6 reports: {n_valid}/12 valid gate={'PASS' if gate_ok else 'FAIL'} "
                    f"(mixed {PROTOCOL_RETAINED}+{PROTOCOL_RERUN} {BATCH_ID})",
                ],
                cwd=str(WORKTREE),
                check=True,
            )
        else:
            log.info("No staged report changes; skipping commit.")
        commit_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(WORKTREE), text=True
        ).strip()
        push = subprocess.run(
            ["git", "push", "origin", "swe-adapter"],
            cwd=str(WORKTREE),
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            git_ok = False
            log.error("git push failed: %s %s", push.stdout, push.stderr)
        else:
            log.info("Pushed origin/swe-adapter at %s", commit_sha)
    except subprocess.CalledProcessError as exc:
        git_ok = False
        log.error("Git commit/push failed: %s", exc)

    done = {
        "batch_id": BATCH_ID,
        "generated": _now(),
        "gate": "PASS" if gate_ok else "FAIL",
        "n_valid": n_valid,
        "n_total": n_total,
        "n_provenance": n_provenance,
        "shortfall": shortfall,
        "mixed": True,
        "report": str(REPORT_OUT),
        "csv": str(CSV_OUT),
        "commit": commit_sha,
        "git_ok": git_ok,
    }
    _atomic_write(DONE_PATH, json.dumps(done, indent=2) + "\n")
    log.info("Finalizer complete. gate=%s commit=%s", gate_ok, commit_sha)
    return gate_ok, commit_sha


def _append_empty_role_rows(rows, run_id, task_id, condition, status, cohort):
    for role in ("worker", "parent"):
        rows.append({
            "run_id": run_id,
            "task_id": task_id,
            "condition": condition,
            "role": role,
            "cohort": cohort,
            "protocol_version": "",
            "task_set": "pilot",
            "status": status,
            "rerun_eligible": "",
            "submitted": "",
            "submitted_memo": "",
            "submitted_solution": "",
            "memo_source": "",
            "seal_source": "",
            "end_reason": "",
            "continued_on_max_tokens": "",
            "unexpected_stop_reason": "",
            "reprompted_on_end_turn": "",
            "candidate_diff_present": "",
            "candidate_diff_nonempty": "",
            "candidate_diff_sha256": "",
            "final_diff_present": "",
            "final_diff_nonempty": "",
            "final_diff_sha256": "",
            "turns": "",
            "tool_calls": "",
            "input_tokens": "",
            "output_tokens": "",
            "cache_read_input_tokens": "",
            "cache_creation_input_tokens": "",
            "wall_seconds": "",
            "caps_loosened": "",
            "prior_swe02_max_turns": "",
            "prior_swe02_max_tool_calls": "",
            "truncation_count": "",
            "production_artifact_missing": "",
            "final_resolved": "",
            "schema_git_commit": "",
            "task_repo": "",
            "worker_start_commit": "",
            "benchmark_git_commit": "",
        })


def main() -> int:
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()) + "\n")
    lock_fh = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Another durable finalizer already holds the lock; exiting.")
        return 0
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(f"pid={os.getpid()} started={_now()}\n")
    lock_fh.flush()

    log.info("Durable finalizer starting. runner_pids=%s head=%s", runner_pids(), HEAD_RERUN)
    if DONE_PATH.is_file() and REPORT_OUT.is_file() and CSV_OUT.is_file():
        log.info("Done marker already present; re-validating from disk.")
    log_states = wait_for_terminal()
    time.sleep(5)
    log_states = parse_batch_log()
    gate_ok, commit_sha = finalize(log_states)
    log.info("done gate=%s commit=%s", gate_ok, commit_sha)
    return 0 if gate_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
