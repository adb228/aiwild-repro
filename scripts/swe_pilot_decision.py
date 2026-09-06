#!/usr/bin/env python3
"""Gate the SWE pilot union (7 retained v5 + later reruns).

PASS = 12/12 valid, provenance ok, A cells no patch, A1 scratch destroyed,
assistant text on every model call, and ≥3/6 P workers nonempty diff.

≥2/6 P nonempty (but <3) is PASS-with-caveat (proceed to main).
<2/6 P nonempty, or any other criterion fail → FAIL.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WORKTREE = Path("<REPO>")
sys.path.insert(0, str(WORKTREE / "src"))

from delegation_study.qc import qc_episode
from delegation_study.swebench.run import remote_matches_repo
from delegation_study.swebench.task_loader import load_task
from delegation_study.validate import validate_episode, validate_event_log

TASKS = [
    "django__django-12155",
    "scikit-learn__scikit-learn-14983",
    "matplotlib__matplotlib-25479",
]
CONDITIONS = ["A0", "A1", "P0", "P1"]

# Default v6 union. Missing run_id = no episode.json (batch ERROR).
DEFAULT_UNION: dict[tuple[str, str], dict] = {
    ("django__django-12155", "A0"): {
        "run_id": "9e3dd119-7a58-475f-8605-032de13337ca",
        "cohort": "retained_v5",
        "protocol": "swe-0.3.1",
        "head_prefix": "<commit>",
    },
    ("django__django-12155", "A1"): {
        "run_id": "cae87060-531c-4676-b1d8-486bbcfb4561",
        "cohort": "retained_v5",
        "protocol": "swe-0.3.1",
        "head_prefix": "<commit>",
    },
    ("django__django-12155", "P0"): {
        "run_id": "d6b236a4-e246-43ca-8ca9-aeebee408bd9",
        "cohort": "retained_v5",
        "protocol": "swe-0.3.1",
        "head_prefix": "<commit>",
    },
    ("django__django-12155", "P1"): {
        "run_id": "5d214962-bc4a-4cfc-bd6f-8ce475a6f235",
        "cohort": "rerun_v6",
        "protocol": "swe-0.3.2",
        "head_prefix": "<commit>",
    },
    ("scikit-learn__scikit-learn-14983", "A0"): {
        "run_id": "d6a6d67c-1fd6-4c19-b195-d3795f015f7e",
        "cohort": "retained_v5",
        "protocol": "swe-0.3.1",
        "head_prefix": "<commit>",
    },
    ("scikit-learn__scikit-learn-14983", "A1"): {
        "run_id": "ed1f371e-f1ab-4f49-93a2-26f4f7d957ad",
        "cohort": "rerun_v6",
        "protocol": "swe-0.3.2",
        "head_prefix": "<commit>",
    },
    ("scikit-learn__scikit-learn-14983", "P0"): {
        "run_id": None,
        "cohort": "rerun_v6",
        "protocol": "swe-0.3.2",
        "head_prefix": "<commit>",
        "orphan_run_id": "4a437ad3-3d28-4810-8706-e008e8150721",
    },
    ("scikit-learn__scikit-learn-14983", "P1"): {
        "run_id": None,
        "cohort": "rerun_v6",
        "protocol": "swe-0.3.2",
        "head_prefix": "<commit>",
        "orphan_run_id": "d24ce124-56ae-4746-a0be-c6221ad6ac42",
    },
    ("matplotlib__matplotlib-25479", "A0"): {
        "run_id": "8edc54d4-f810-4c56-ae3d-94721aa240ce",
        "cohort": "retained_v5",
        "protocol": "swe-0.3.1",
        "head_prefix": "<commit>",
    },
    ("matplotlib__matplotlib-25479", "A1"): {
        "run_id": "ffa92d77-6f83-49d5-9b6e-300f57153cba",
        "cohort": "retained_v5",
        "protocol": "swe-0.3.1",
        "head_prefix": "<commit>",
    },
    ("matplotlib__matplotlib-25479", "P0"): {
        "run_id": None,
        "cohort": "rerun_v6",
        "protocol": "swe-0.3.2",
        "head_prefix": "<commit>",
        "orphan_run_id": "df9e5ad4-01dc-4150-9778-9b322941fd6b",
    },
    ("matplotlib__matplotlib-25479", "P1"): {
        "run_id": "1f0a5734-d152-4916-b055-528ae2f3cd6a",
        "cohort": "retained_v5",
        "protocol": "swe-0.3.1",
        "head_prefix": "<commit>",
    },
}


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


def provenance_ok(ep: dict, meta: dict, expected: dict[str, dict[str, str]]) -> tuple[bool, str]:
    task_id = ep.get("task_id")
    exp = expected.get(task_id)
    if not exp:
        return False, f"unknown task_id {task_id}"
    em = (ep.get("env_metrics") or {}).get("swebench") or {}
    repro = ep.get("reproducibility") or {}
    protocol = ep.get("protocol_version")
    want = meta.get("protocol")
    if want and protocol != want:
        return False, f"protocol_version={protocol!r} expected {want}"
    schema_git = str(repro.get("schema_git_commit") or "")
    prefix = meta.get("head_prefix") or ""
    if prefix and not schema_git.startswith(prefix):
        return False, f"schema_git_commit={schema_git!r} expected prefix {prefix}"
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


def load_union(path: Path | None) -> dict[tuple[str, str], dict]:
    if path is None:
        return DEFAULT_UNION
    raw = json.loads(path.read_text())
    out: dict[tuple[str, str], dict] = {}
    for row in raw:
        out[(row["task_id"], row["condition"])] = row
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="SWE pilot union gate")
    parser.add_argument("--union-json", type=Path, default=None)
    args = parser.parse_args()
    union = load_union(args.union_json)

    expected = {tid: {"repo": t.repo, "base_commit": t.base_commit} for tid, t in
                ((tid, load_task(tid)) for tid in TASKS)}

    n_valid = 0
    n_present = 0
    n_a_no_patch = 0
    n_a = 0
    n_a1_scratch = 0
    n_a1 = 0
    n_p_nonempty = 0
    n_p = 0
    n_assistant = 0
    n_provenance = 0
    rows = []
    failures = []

    for task_id in TASKS:
        for condition in CONDITIONS:
            meta = union.get((task_id, condition), {})
            run_id = meta.get("run_id")
            is_a = condition.startswith("A")
            is_p = condition.startswith("P")
            if is_a:
                n_a += 1
            if is_p:
                n_p += 1
            if condition == "A1":
                n_a1 += 1

            row = {
                "task_id": task_id,
                "condition": condition,
                "run_id": run_id,
                "cohort": meta.get("cohort", ""),
                "valid": False,
                "notes": "",
            }
            if not run_id:
                row["notes"] = "no episode.json"
                failures.append(f"{task_id}/{condition}: no episode.json")
                rows.append(row)
                continue

            ep_path = WORKTREE / "data/runs" / run_id / "episode.json"
            if not ep_path.is_file():
                row["notes"] = "episode.json missing on disk"
                failures.append(f"{task_id}/{condition}: episode.json missing")
                rows.append(row)
                continue

            ep = json.loads(ep_path.read_text())
            n_present += 1
            events_path = ep_path.parent / "events.jsonl"
            issues = validate_episode(ep, ep_path)
            event_issues = (
                validate_event_log(events_path, ep)
                if events_path.is_file()
                else ["missing events.jsonl"]
            )
            valid = not issues and not event_issues
            row["valid"] = valid
            if valid:
                n_valid += 1
            else:
                failures.append(
                    f"{task_id}/{condition}: validation={issues} events={event_issues}"
                )

            run_dir = ep_path.parent
            handoff = ep.get("handoff") or {}
            cand = handoff.get("candidate_script_path")
            conventional = run_dir / "handoff" / "candidate.patch"
            if is_a:
                if not cand and not conventional.exists():
                    n_a_no_patch += 1
                else:
                    failures.append(f"{task_id}/{condition}: A cell has a patch")
            if is_p:
                path = Path(cand) if cand else conventional
                if not path.is_absolute():
                    path = run_dir / path
                if path.is_file() and path.read_text(errors="replace").strip():
                    n_p_nonempty += 1
            if condition == "A1":
                if (ep.get("compliance") or {}).get("worker_scratch_destroyed"):
                    n_a1_scratch += 1
                else:
                    failures.append(f"{task_id}/A1: scratch not destroyed")
            if assistant_text_ok(run_id):
                n_assistant += 1
            else:
                failures.append(f"{task_id}/{condition}: assistant text missing")
            ok, msg = provenance_ok(ep, meta, expected)
            if ok:
                n_provenance += 1
            else:
                failures.append(f"{task_id}/{condition}: provenance {msg}")
            row["notes"] = "ok" if valid and ok else "; ".join(
                x for x in failures if x.startswith(f"{task_id}/{condition}")
            )
            rows.append(row)

    print("cell\tcohort\trun_id\tvalid\tnotes")
    for r in rows:
        print(
            f"{r['task_id']}/{r['condition']}\t{r['cohort']}\t"
            f"{r['run_id'] or 'NONE'}\t{r['valid']}\t{r['notes']}"
        )

    print()
    print(f"valid: {n_valid}/12 (present={n_present})")
    print(f"provenance: {n_provenance}/{n_present}")
    print(f"A no patch: {n_a_no_patch}/{n_a}")
    print(f"A1 scratch destroyed: {n_a1_scratch}/{n_a1}")
    print(f"assistant text: {n_assistant}/{n_present}")
    print(f"P nonempty diff: {n_p_nonempty}/6")
    if failures:
        print("failures:")
        for f in failures:
            print(f"  - {f}")

    core_ok = (
        n_valid == 12
        and n_a_no_patch == n_a == 6
        and n_a1_scratch == n_a1 == 3
        and n_assistant == 12
        and n_provenance == 12
    )
    if core_ok and n_p_nonempty >= 3:
        verdict = "PASS"
        rc = 0
    elif core_ok and n_p_nonempty >= 2:
        verdict = "PASS-with-caveat"
        rc = 0
    else:
        verdict = "FAIL"
        rc = 2
    print(f"GATE: {verdict}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
