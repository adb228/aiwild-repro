#!/usr/bin/env python3
"""Write handwritten infra-failure records for 4 missing main cells, then main_report.md.

Does not fabricate resolved=1. Missing / infrastructure cells score 0 (ITT).
Does not call Anthropic. Overwrites only the four assigned handwritten IDs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from delegation_study.qc import qc_episode
from delegation_study.validate import validate_episode

WORKTREE = Path("<REPO>")
RUNS = WORKTREE / "data" / "runs"
REPORT = WORKTREE / "data" / "dev" / "swe" / "main_report.md"
BOOTSTRAP_SEED = 20260902
BOOTSTRAP_DRAWS = 10_000

TASKS = [
    "sympy__sympy-24562",
    "django__django-13297",
    "matplotlib__matplotlib-24149",
    "scikit-learn__scikit-learn-13496",
    "pytest-dev__pytest-10081",
    "sympy__sympy-12481",
    "django__django-14855",
    "matplotlib__matplotlib-25332",
]
CELLS = ["A0", "A1", "P0", "P1"]

# Prefer these complete records when several exist for a cell.
PREFERRED = {
    ("matplotlib__matplotlib-24149", "A1"): "dbd8ebdf-f0f5-4209-92bb-ff01e70954a4",
}

MISSING = [
    {
        "run_id": "16bf4093-11ce-49e2-bf29-eb0b8a271394",
        "task_id": "sympy__sympy-24562",
        "condition": "P0",
        "orphan": "1f14ca5f-aba4-4f15-b53c-9ff5c93701eb",
        "rerun": "d8bee9ac-34e5-4597-89b5-0e076b4510d6",
        "sibling": "15c51462-4b7c-47c1-9367-894e11f504bf",
        "prompt_too_long": (
            "Main batch swe-main-20260904T085320Z-<id> orphan "
            "1f14ca5f-aba4-4f15-b53c-9ff5c93701eb: parent mid-loop then Anthropic "
            "HTTP 400 invalid_request_error prompt is too long 209834>200000 "
            "(<request_id>)."
        ),
        "usage_limit": (
            "swe-0.3.4 rerun d8bee9ac-34e5-4597-89b5-0e076b4510d6: Anthropic HTTP 400 "
            "invalid_request_error provider usage limit until 2026-10-01 00:00 UTC "
            "(<request_id>). No behavioral outcome recorded."
        ),
    },
    {
        "run_id": "2389d180-0479-4c13-8da1-512f781a49ca",
        "task_id": "matplotlib__matplotlib-24149",
        "condition": "A0",
        "orphan": "53e885b8-061d-40e9-bc2f-b38abd3a4099",
        "rerun": "3f8fdee5-1444-47cf-94d1-5253b7f0978f",
        "sibling": "dbd8ebdf-f0f5-4209-92bb-ff01e70954a4",
        "prompt_too_long": (
            "Main orphan 53e885b8-061d-40e9-bc2f-b38abd3a4099: worker Anthropic HTTP 400 "
            "invalid_request_error prompt is too long 250256>200000 "
            "(<request_id>)."
        ),
        "usage_limit": (
            "swe-0.3.4 rerun 3f8fdee5-1444-47cf-94d1-5253b7f0978f: Anthropic HTTP 400 "
            "invalid_request_error provider usage limit "
            "(<request_id>). No behavioral outcome recorded."
        ),
    },
    {
        "run_id": "40a7ca7f-6d28-45c9-8e39-07fe378ac439",
        "task_id": "matplotlib__matplotlib-24149",
        "condition": "P0",
        "orphan": "35f4b943-d563-4092-ba1e-029ed13a18e2",
        "rerun": "ee581b17-b7ee-4eb3-8ddc-5dfb2d00320c",
        "sibling": "dbd8ebdf-f0f5-4209-92bb-ff01e70954a4",
        "prompt_too_long": (
            "Main orphan 35f4b943-d563-4092-ba1e-029ed13a18e2: worker Anthropic HTTP 400 "
            "invalid_request_error prompt is too long 251907>200000 "
            "(<request_id>)."
        ),
        "usage_limit": (
            "swe-0.3.4 rerun ee581b17-b7ee-4eb3-8ddc-5dfb2d00320c: Anthropic HTTP 400 "
            "invalid_request_error provider usage limit "
            "(<request_id>). No behavioral outcome recorded."
        ),
    },
    {
        "run_id": "0a8eef47-5256-4114-945c-a7ad9c71f4bb",
        "task_id": "matplotlib__matplotlib-24149",
        "condition": "P1",
        "orphan": "3a301d0f-51d0-4342-866e-952b912f6e9b",
        "rerun": "9d122813-1658-4bc3-b565-cc8e66c2582c",
        "sibling": "dbd8ebdf-f0f5-4209-92bb-ff01e70954a4",
        "prompt_too_long": (
            "Main orphan 3a301d0f-51d0-4342-866e-952b912f6e9b: worker Anthropic HTTP 400 "
            "invalid_request_error prompt is too long 232444>200000 "
            "(<request_id>)."
        ),
        "usage_limit": (
            "swe-0.3.4 rerun 9d122813-1658-4bc3-b565-cc8e66c2582c: Anthropic HTTP 400 "
            "invalid_request_error provider usage limit "
            "(<request_id>). No behavioral outcome recorded."
        ),
    },
]


def _zero_usage() -> dict:
    return {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "tool_calls": 0,
        "validation_cycles": 0,
        "wall_seconds": 0.0,
        "budget_exhausted": False,
        "budget_exhausted_on": None,
        "safety_exhausted": False,
        "safety_exhausted_on": None,
    }


def _usage_from_events(orphan: str, role: str) -> dict:
    ev = RUNS / orphan / "events.jsonl"
    u = _zero_usage()
    if not ev.is_file():
        return u
    calls = 0
    tools = 0
    inp = out = cr = cw = 0
    for line in ev.read_text().splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        if o.get("role") != role:
            continue
        if o.get("event_type") == "model_call":
            calls += 1
            pl = o.get("payload") or {}
            inp += int(pl.get("input_tokens") or 0)
            out += int(pl.get("output_tokens") or 0)
            cr += int(pl.get("cache_read_input_tokens") or 0)
            cw += int(pl.get("cache_creation_input_tokens") or 0)
        elif o.get("event_type") == "tool_call":
            tools += 1
    u.update({
        "model_calls": calls,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
        "tool_calls": tools,
    })
    return u


def _event_times(orphan: str) -> tuple[str, str]:
    ev = RUNS / orphan / "events.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    if not ev.is_file():
        return now, now
    stamps = []
    for line in ev.read_text().splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        ts = o.get("timestamp")
        if ts:
            stamps.append(ts)
    if not stamps:
        return now, now
    return stamps[0], stamps[-1]


def write_failure_records() -> list[str]:
    written = []
    for spec in MISSING:
        sibling = json.loads((RUNS / spec["sibling"] / "episode.json").read_text())
        run_id = spec["run_id"]
        started, completed = _event_times(spec["orphan"])
        record = {
            "run_id": run_id,
            "task_id": spec["task_id"],
            "task_set": "main",
            "condition": spec["condition"],
            "protocol_version": "swe-0.3.3",
            "replicate": 1,
            "started_at": started,
            "completed_at": completed,
            "benchmark": "swebench",
            "reproducibility": dict(sibling["reproducibility"]),
            "budgets": sibling["budgets"],
            "usage": {
                "worker": _usage_from_events(spec["orphan"], "worker"),
                "parent": _usage_from_events(spec["orphan"], "parent"),
            },
            "handoff": {
                "memo_path": None,
                "memo_sha256": None,
                "candidate_script_path": None,
                "candidate_script_sha256": None,
                "worker_submitted_memo": False,
                "worker_submitted_candidate": False,
            },
            "worker_outcome": None,
            "final_outcome": None,
            "unsealed_last_script_score": None,
            "failures": [
                {
                    "source": "infrastructure",
                    "code": "provider_prompt_too_long",
                    "message": spec["prompt_too_long"],
                    "rerun_eligible": True,
                },
                {
                    "source": "infrastructure",
                    "code": "provider_usage_limit",
                    "message": spec["usage_limit"],
                    "rerun_eligible": True,
                },
            ],
            "compliance": {
                "artifact_crossed_handoff": False,
                "worker_feedback_calls": 0,
                "parent_finalized": False,
                "worker_scratch_destroyed": True,
                "worker_submitted": False,
            },
            "swe_protocol_usage": {
                "worker": {
                    "reprompted_on_end_turn": 0,
                    "end_reason": "infrastructure_failure",
                    "seal_source": None,
                    "memo_source": "none",
                    "continued_on_max_tokens": 0,
                    "unexpected_stop_reason": None,
                },
                "parent": {
                    "reprompted_on_end_turn": 0,
                    "end_reason": "infrastructure_failure",
                    "seal_source": None,
                    "continued_on_max_tokens": 0,
                    "unexpected_stop_reason": None,
                },
                "caps_loosened": True,
                "prior_swe02_max_turns": 40,
                "prior_swe02_max_tool_calls": 100,
                "production_artifact_missing": spec["condition"] in {"P0", "P1"},
                "orphan_run_id": spec["orphan"],
                "rerun_run_id": spec["rerun"],
                "provenance_measured": False,
            },
            "final_script_path": None,
            "final_script_sha256": None,
            "accepted_unchanged": False,
            "status": "infrastructure_failure",
            "rerun_eligible": True,
            "analysis_included": True,
            "analysis_inclusion_reason": (
                "infrastructure_failure:provider_prompt_too_long+provider_usage_limit; "
                f"ITT scored as 0; orphan={spec['orphan']}; rerun={spec['rerun']}; "
                "provenance_measured=false"
            ),
            "env_metrics": {
                "swebench": {
                    "fail_to_pass_fraction": None,
                    "pass_to_pass_fraction": None,
                    "f2p_numerator": None,
                    "f2p_denominator": None,
                    "p2p_numerator": None,
                    "p2p_denominator": None,
                    "f2p_p2p_null_reason": "infrastructure_failure_no_evaluation",
                    "applies_cleanly": False,
                    "candidate_resolved": None,
                    "final_resolved": 0,
                    "candidate_harness_seconds": None,
                    "final_harness_seconds": None,
                    "candidate_report_sha256": None,
                    "final_report_sha256": None,
                    "candidate_f2p_fraction": None,
                    "candidate_p2p_fraction": None,
                    "image_tag": (sibling.get("env_metrics") or {}).get("swebench", {}).get("image_tag"),
                    "image_digest": sibling["reproducibility"].get("docker_image_digest"),
                    "git_head": sibling["reproducibility"].get("benchmark_git_commit"),
                    "git_remote": (sibling.get("env_metrics") or {}).get("swebench", {}).get("git_remote"),
                    "task_repo": (sibling.get("env_metrics") or {}).get("swebench", {}).get("task_repo"),
                    "clean_tree": True,
                    "worker_container_id": None,
                    "parent_container_id": None,
                    "worker_start_commit": None,
                    "parent_start_commit": None,
                    "worker_start_clean": None,
                    "parent_start_clean": None,
                    "worker_start_remote": None,
                    "parent_start_remote": None,
                }
            },
        }
        # Keep sibling prompt hashes (same prompts) but mark this record's commit.
        record["reproducibility"] = dict(sibling["reproducibility"])
        dest = RUNS / run_id
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "episode.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        issues = validate_episode(record, path)
        if issues:
            raise SystemExit(
                f"failure record {run_id} invalid: "
                + "; ".join(f"{i.path}: {i.message}" for i in issues[:8])
            )
        written.append(run_id)
        print(f"wrote {spec['condition']} {spec['task_id']} {run_id} issues=0")
    return written


def load_matrix() -> dict[tuple[str, str], dict]:
    by_cell: dict[tuple[str, str], list[dict]] = {}
    for ep in RUNS.glob("*/episode.json"):
        rec = json.loads(ep.read_text())
        if rec.get("benchmark") != "swebench" or rec.get("task_set") != "main":
            continue
        key = (rec["task_id"], rec["condition"])
        if key[0] not in TASKS:
            continue
        rec["_path"] = str(ep)
        rec["_qc"] = [f.code for f in qc_episode(rec, record_path=ep)]
        by_cell.setdefault(key, []).append(rec)
    chosen: dict[tuple[str, str], dict] = {}
    for task in TASKS:
        for cell in CELLS:
            key = (task, cell)
            cands = by_cell.get(key, [])
            if not cands:
                raise SystemExit(f"missing record for {task}/{cell}")
            pref = PREFERRED.get(key)
            if pref:
                hit = [r for r in cands if r["run_id"] == pref]
                if hit:
                    chosen[key] = hit[0]
                    continue
            complete = [r for r in cands if r.get("status") == "complete"]
            chosen[key] = complete[0] if complete else cands[-1]
    return chosen


def resolved(rec: dict) -> int:
    swe = (rec.get("env_metrics") or {}).get("swebench") or {}
    val = swe.get("final_resolved")
    if val is None:
        return 0
    return int(val)


def effects(chosen: dict) -> tuple[float, float, float]:
    tau0 = float(np.mean([resolved(chosen[(t, "P0")]) - resolved(chosen[(t, "A0")]) for t in TASKS]))
    tau1 = float(np.mean([resolved(chosen[(t, "P1")]) - resolved(chosen[(t, "A1")]) for t in TASKS]))
    return tau0, tau1, tau1 - tau0


def bootstrap_ci(chosen: dict) -> dict[str, tuple[float, float, float]]:
    y = {
        t: {c: resolved(chosen[(t, c)]) for c in CELLS}
        for t in TASKS
    }
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    draws = {"tau0": [], "tau1": [], "delta": []}
    n = len(TASKS)
    for _ in range(BOOTSTRAP_DRAWS):
        idx = rng.integers(0, n, size=n)
        sample = [TASKS[i] for i in idx]
        t0 = float(np.mean([y[t]["P0"] - y[t]["A0"] for t in sample]))
        t1 = float(np.mean([y[t]["P1"] - y[t]["A1"] for t in sample]))
        draws["tau0"].append(t0)
        draws["tau1"].append(t1)
        draws["delta"].append(t1 - t0)
    out = {}
    point = dict(zip(["tau0", "tau1", "delta"], effects(chosen)))
    for name in ("tau0", "tau1", "delta"):
        arr = np.array(draws[name])
        lo, hi = np.quantile(arr, [0.025, 0.975])
        out[name] = (point[name], float(lo), float(hi))
    return out


def usage_cost(u: dict) -> float:
    if not u:
        return 0.0
    return (
        int(u.get("input_tokens") or 0) * 3.0
        + int(u.get("output_tokens") or 0) * 15.0
        + int(u.get("cache_read_input_tokens") or 0) * 0.30
        + int(u.get("cache_creation_input_tokens") or 0) * 3.75
    ) / 1e6


def write_report(chosen: dict, failure_ids: list[str]) -> None:
    cis = bootstrap_ci(chosen)
    lines = [
        "# SWE main report",
        "",
        "Batch `swe-main-20260904T085320Z-<id>` (swe-0.3.3 @ `<commit>`) plus",
        "one swe-0.3.4 rerun (`matplotlib-24149/A1` `<commit>` @ `<commit>`) and",
        "four handwritten `infrastructure_failure` records for cells that never",
        "got `episode.json`. Anthropic usage limits closed the account until",
        "2026-10-01 00:00 UTC, so the remaining four 0.3.4 reruns could not finish.",
        "",
        "`analyze.py --task-set main` is not implemented on this checkout;",
        "this report is computed from raw `episode.json` records with a",
        "task-clustered bootstrap (seed 20260902, 10000 draws).",
        "",
        "## 8×4 matrix (`final_resolved`; None→0 ITT)",
        "",
        "| task | A0 | A1 | P0 | P1 |",
        "|---|---:|---:|---:|---:|",
    ]
    cell_success = {c: 0 for c in CELLS}
    missing_art = {c: 0 for c in CELLS}
    not_final = {c: 0 for c in CELLS}
    infra = {c: 0 for c in CELLS}
    cost = 0.0
    proto_rows = []
    for task in TASKS:
        row = [task.split("__")[-1]]
        for cell in CELLS:
            rec = chosen[(task, cell)]
            val = resolved(rec)
            cell_success[cell] += val
            row.append(str(val))
            if rec.get("status") == "infrastructure_failure":
                infra[cell] += 1
            qc = rec.get("_qc") or []
            if "production_missing_artifact" in qc or (
                rec.get("swe_protocol_usage") or {}
            ).get("production_artifact_missing"):
                if cell.startswith("P"):
                    missing_art[cell] += 1
            if not (rec.get("compliance") or {}).get("parent_finalized"):
                not_final[cell] += 1
            u = rec.get("usage") or {}
            cost += usage_cost(u.get("worker") or {}) + usage_cost(u.get("parent") or {})
            proto_rows.append(
                f"| {task.split('__')[-1]} | {cell} | `{rec['run_id']}` | "
                f"{rec.get('protocol_version')} | "
                f"`{(rec.get('reproducibility') or {}).get('schema_git_commit', '')[:8]}` | "
                f"{rec.get('status')} | {val} |"
            )
        lines.append("| " + " | ".join(row) + " |")
    lines += [
        "",
        f"Cell success counts: A0 {cell_success['A0']}/8 · A1 {cell_success['A1']}/8 · "
        f"P0 {cell_success['P0']}/8 · P1 {cell_success['P1']}/8",
        "",
        "## Treatment effects (task-clustered bootstrap 95% CI)",
        "",
        f"- τ0 = P0−A0 = **{cis['tau0'][0]:+.4f}**  95% CI ({cis['tau0'][1]:+.4f}, {cis['tau0'][2]:+.4f})",
        f"- τ1 = P1−A1 = **{cis['tau1'][0]:+.4f}**  95% CI ({cis['tau1'][1]:+.4f}, {cis['tau1'][2]:+.4f})",
        f"- δ  = τ1−τ0 = **{cis['delta'][0]:+.4f}**  95% CI ({cis['delta'][1]:+.4f}, {cis['delta'][2]:+.4f})",
        "",
        "## Missing artifact and non-finalization (QC / ITT)",
        "",
        f"- production_missing_artifact: P0 {missing_art['P0']}/8 · P1 {missing_art['P1']}/8",
        f"- parent_not_finalized: A0 {not_final['A0']}/8 · A1 {not_final['A1']}/8 · "
        f"P0 {not_final['P0']}/8 · P1 {not_final['P1']}/8",
        f"- infrastructure_failure rows (handwritten ITT zeros): "
        f"A0 {infra['A0']} · A1 {infra['A1']} · P0 {infra['P0']} · P1 {infra['P1']}",
        "",
        "## Cost (rough, from recorded usage; excludes some crashed-prefix tokens)",
        "",
        f"Estimated Anthropic spend on the 32 matrix rows: **${cost:.2f}**.",
        "Cycle-1 + crashed prefixes + the failed 0.3.4 retries are extra.",
        "Provider then returned usage-limit 400s until 2026-10-01 00:00 UTC.",
        "",
        "## Provenance",
        "",
        "| task | cell | run_id | protocol | schema_git | status | resolved |",
        "|---|---|---|---|---|---|---:|",
    ]
    lines.extend(proto_rows)
    lines += [
        "",
        "## Notes",
        "",
        "- Handwritten failure run_ids: " + ", ".join(f"`{i}`" for i in failure_ids),
        "- matplotlib-24149/A1 is the only successful swe-0.3.4 rerun (`resolved=0`).",
        "- ConversationGuard preemptive compact fired on the 0.3.4 retries before quota.",
        "- Leftover gold container `sweb.eval.django__django-14752.m5_gold_cdc0143d` was",
        "  already running before this goal; not removed.",
        "- `analyze.py` still rejects `task_set=main`; flag for analyzer reconciliation.",
        "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")
    print(f"wrote {REPORT}")
    print("effects", cis)


def main() -> None:
    failure_ids = write_failure_records()
    chosen = load_matrix()
    assert len(chosen) == 32
    write_report(chosen, failure_ids)


if __name__ == "__main__":
    main()
