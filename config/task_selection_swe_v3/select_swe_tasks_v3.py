#!/usr/bin/env python3
"""
SWE-bench Verified selection, package swe_selection_v3 — frozen for swe-0.3.5.

Copy of for_coauthor_swe/swe_selection_v3/select_swe_tasks_v3.py. The draw, seed,
filters, exclusions, and selection_sha256 payload are unchanged.

The as-received script set files=None because base.files_touched did not exist.
This copy fills files / changed_lines / src_lines_changed from base.patch_stats
after the draw. Those fields are not in the hashed payload.

Rules, written before the draw (all mechanical):
  * Filter R1–R9 exactly as in select_swe_tasks.py (v0.3 thresholds: ≤5 files, ≤150 lines,
    ≥5 non-test lines, 1–15 FAIL_TO_PASS, difficulty in {<15 min, 15 min–1 hour}, no links/images,
    ≥200-char issue, no test-only patches). The A-2 tightening (≤2 files, ≤40 lines, <15 min only)
    is NOT applied: it produced a ceiling grid (4/8 issues resolved in every cell).
  * Repo cap 0.25 of the main set.
  * Exclusions (infrastructure / already-used, not outcome-based):
      - the 3 pilot IDs, the 8 v2 main IDs, the v2 reserve IDs, and the dev issue django-14752
        (all have already been shown to the model under earlier protocols);
      - repo matplotlib/matplotlib: harness evaluation timed out on 3 of 3 attempted cells
        and the official images are the slowest under emulation (infrastructure exclusion,
        pre-declared for swe-0.4; recorded here).
  * Seed 20260905 (new package; the v2 seed must not be reused because the pool changed).
  * Draw 8 main + 8 reserve by the same stratified round-robin as v0.3.
  * Gate 0 per issue (gold resolves / empty fails / repeat identical, within the harness timeout)
    runs on every drawn ID before it enters main; an ID failing Gate 0 is replaced by the next
    reserve ID in order, with the swap logged. This is an infrastructure check, not an outcome.

swe-0.3.5 study assignment (does not change the draw):
  * pilot = first 3 of reserve_in_order
  * main = the 8 drawn main IDs
  * remaining reserve = the last 5 of reserve_in_order, Gate 0 replacements only
"""
import json, random, hashlib, sys, argparse
from collections import Counter
sys.path.insert(0, ".")
import select_swe_tasks as base   # reuse eligible() and stratified_draw() unchanged

SEED = 20260905
EXPECTED_SHA_PREFIX = "e426590d"
EXCLUDE_REPOS = {"matplotlib/matplotlib"}
ALREADY_USED = {
    # pilot v2
    "django__django-12155", "scikit-learn__scikit-learn-14983", "matplotlib__matplotlib-25479",
    # main v2
    "sympy__sympy-24562", "django__django-13297", "matplotlib__matplotlib-24149", "scikit-learn__scikit-learn-13496",
    "pytest-dev__pytest-10081", "sympy__sympy-12481", "django__django-14855", "matplotlib__matplotlib-25332",
    # reserve v2
    "scikit-learn__scikit-learn-13439", "sympy__sympy-19495", "django__django-16100", "matplotlib__matplotlib-20676",
    "scikit-learn__scikit-learn-13142", "sympy__sympy-15345", "django__django-13551",
    # dev issue
    "django__django-14752",
    # v0.3 package main/pilot IDs (earlier draw, also exposed)
    "mwaskom__seaborn-3069", "pydata__xarray-3095", "sympy__sympy-19954", "scikit-learn__scikit-learn-13142",
    "django__django-16877", "matplotlib__matplotlib-24970", "pylint-dev__pylint-6528",
}


def brief(xs):
    """Table row after the draw. Counts only — does not emit patch text."""
    rows = []
    for x in xs:
        files, total, src = base.patch_stats(x["patch"])
        rows.append({
            "instance_id": x["instance_id"],
            "repo": x["repo"],
            "difficulty": x["difficulty"],
            "files": len(files),
            "changed_lines": total,
            "src_lines_changed": src,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default="swe_verified_local.json")
    ap.add_argument("--n-main", type=int, default=8)
    ap.add_argument("--n-reserve", type=int, default=8)
    ap.add_argument("--out", default="swe_task_selection_v3.json")
    ap.add_argument("--pilot-out", default="swe_pilot.json")
    a = ap.parse_args()
    rows = json.load(open(<host>)); rows.sort(key=lambda r: r["instance_id"])
    base.MAX_REPO_SHARE = 0.25
    pool, rej = [], Counter()
    for r in rows:
        ok, why = base.eligible(r)
        if not ok: rej[why] += 1; continue
        if r["repo"] in EXCLUDE_REPOS: rej["X1 repo excluded (harness timeouts)"] += 1; continue
        if r["instance_id"] in ALREADY_USED: rej["X2 already used in an earlier protocol"] += 1; continue
        pool.append(r)
    rng = random.Random(SEED); taken = set()
    def draw(n):
        picked = base.stratified_draw([r for r in pool if r["instance_id"] not in taken], n, rng)
        taken.update(r["instance_id"] for r in picked); return picked
    main_set = draw(a.n_main); reserve = draw(a.n_reserve)
    out = {"package": "swe_selection_v3", "protocol": "swe-0.4", "date": "2026-09-04", "seed": SEED,
           "source_dataset_sha256": hashlib.sha256(open(<host>, "rb").read()).hexdigest(),
           "filter": "R1-R9 at v0.3 thresholds; A-2 tightening not applied", "max_repo_share": 0.25,
           "exclusions": {"repos": sorted(EXCLUDE_REPOS), "already_used_ids": sorted(ALREADY_USED)},
           "eligible_pool": len(pool), "rejection_reasons": dict(rej),
           "main": brief(main_set), "reserve_in_order": brief(reserve),
           "replacement_rule": "Gate 0 failure (gold/empty/repeat or harness timeout) → next reserve in order; log the swap. Never replace on an agent outcome."}
    out["selection_sha256"] = hashlib.sha256(json.dumps({"seed": SEED, "filter": out["filter"], "exclusions": out["exclusions"],
        "main": [x["instance_id"] for x in main_set], "reserve": [x["instance_id"] for x in reserve]}, sort_keys=True).encode()).hexdigest()
    if not out["selection_sha256"].startswith(EXPECTED_SHA_PREFIX):
        raise SystemExit(
            "selection_sha256 prefix mismatch: "
            f"got {out['selection_sha256'][:8]}…, expected {EXPECTED_SHA_PREFIX}…. "
            "Draw not written."
        )
    # Study assignment for swe-0.3.5. Not part of the hashed payload.
    out["study_assignment"] = {
        "protocol": "swe-0.3.5",
        "pilot": [x["instance_id"] for x in reserve[:3]],
        "main": [x["instance_id"] for x in main_set],
        "remaining_reserve_in_order": [x["instance_id"] for x in reserve[3:]],
        "remaining_reserve_use": "Gate 0 replacements only",
        "table_annotation": "files/changed_lines/src_lines_changed from patch_stats; draw unchanged",
    }
    json.dump(out, open(a.out, "w"), indent=1)
    json.dump({"pilot": [x["instance_id"] for x in reserve[:3]], "seed": SEED,
               "source": "first 3 of reserve_in_order"}, open(a.pilot_out, "w"), indent=2)
    print("eligible pool:", len(pool)); print("rejections:", dict(rej))
    print("MAIN:"); [print(f"  {x['instance_id']:<42} {x['difficulty']:<18}") for x in main_set]
    print("RESERVE (in order):"); [print(f"  {x['instance_id']:<42} {x['difficulty']:<18}") for x in reserve]
    print("PILOT (first 3 reserve):"); [print(f"  {x['instance_id']}") for x in reserve[:3]]
    print("REMAINING RESERVE (Gate 0 only):"); [print(f"  {x['instance_id']}") for x in reserve[3:]]
    print("repo mix (main):", dict(Counter(x["repo"] for x in main_set)))
    print("selection_sha256:", out["selection_sha256"])
if __name__ == "__main__":
    main()
