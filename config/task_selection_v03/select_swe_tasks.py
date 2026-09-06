"""
SWE-bench Verified task selection — Paper A (Production vs Advisory delegation).

Applies a pre-registered, purely operational filter to the Verified split and
draws pilot + main task IDs by a seeded, stratified rule. No task is chosen by
hand. Run once, commit the outputs, and never re-run after an agent episode.

Usage (either source; both give the same 500 rows):
    # A. Hugging Face
    pip install datasets && huggingface-cli login
    python select_swe_tasks.py
    # B. GitHub mirrors (no HF login): rows from OpenAgentsInc/swe-bench-verified,
    #    difficulty labels from SWE-bench/swe-bench-tasks task.yaml files
    python select_swe_tasks.py --local swe_verified_local.json

Outputs:
    swe_pilot.json           -> send to co-author B immediately for infra validation
    swe_task_selection.json  -> full record (filter, seed, pool, pilot, main, reserve, hash)

Infra checks (container builds, gold resolves, empty patch fails, determinism,
runtime) are NOT applied here; co-author B validates candidates in listed order and
failures are replaced by the next reserve ID. Log every replacement.

Frozen design (co-author B, 2026-09-02): four cells (A0/A1/P0/P1) on every task in
both environments. SWE is the small confirmatory arm: 8 main tasks. If ahead of
schedule, extend by drawing from reserve IN ORDER; never shrink after results.
Reserve replacement does not preserve the stratum of the task it replaces.

Adapter requirements surfaced by this dataset (co-author B):
  * `hints_text` (issue comment thread) is NOT used by this filter and must
    NEVER be shown to worker or parent; it frequently contains the fix.
  * Verified is in training data. The paired design absorbs this across
    cells; report scores as organizational comparisons, not capability.

Rule numbers below match Appendix A.1 of the protocol.
"""

import argparse
import hashlib
import json
import random
import re
from collections import Counter, defaultdict


# ---------------------------------------------------------------- filter --

MAX_FILES = 5           # gold patch touches at most this many files
MAX_LINES = 150         # gold patch adds+deletes at most this many lines (all files)
MIN_SRC_LINES = 5       # gold patch changes at least this many NON-TEST lines.
                        # Below this an advisory memo is indistinguishable from
                        # the patch itself, so the A/P treatment collapses.
MIN_FAIL_TO_PASS = 1    # need at least one issue-specific test
MAX_FAIL_TO_PASS = 15   # very large FTP sets tend to be sweeping refactors
ALLOWED_DIFFICULTY = {"<15 min fix", "15 min - 1 hour"}
MAX_REPO_SHARE = 0.30   # no single repo may exceed this fraction of the main set

URL_RE = re.compile(r"https?://\S+")
IMG_RE = re.compile(r"!\[.*?\]\(.*?\)|<img\b", re.IGNORECASE)


def is_test_file(path: str) -> bool:
    p = path.lower()
    return ("/test" in p or p.startswith("b/test") or "tests/" in p
            or p.endswith("_test.py") or "/testing/" in p or "conftest" in p)


def patch_stats(patch: str):
    """Return (files, total_changed_lines, changed_lines_in_non_test_files)."""
    files, total, src = set(), 0, 0
    current = None
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            current = line.split()[-1]
            files.add(current)
        elif (line.startswith("+") and not line.startswith("+++")) or \
             (line.startswith("-") and not line.startswith("---")):
            total += 1
            if current and not is_test_file(current):
                src += 1
    return files, total, src


def eligible(row):
    """Return (ok, reason)."""
    files, changed, src_changed = patch_stats(row["patch"])
    if not files or all(is_test_file(f) for f in files):
        return False, "R1 test-only or empty patch"
    if len(files) > MAX_FILES:
        return False, "R2 too many files"
    if changed > MAX_LINES:
        return False, "R3 patch too large"
    if src_changed < MIN_SRC_LINES:
        return False, "R4 patch too small (memo == patch)"

    ftp = row["FAIL_TO_PASS"]
    if isinstance(ftp, str):
        ftp = json.loads(ftp)
    if not (MIN_FAIL_TO_PASS <= len(ftp) <= MAX_FAIL_TO_PASS):
        return False, "R5 FAIL_TO_PASS count out of range"

    if row.get("difficulty") not in ALLOWED_DIFFICULTY:
        return False, "R6 difficulty excluded"

    ps = row["problem_statement"]
    if URL_RE.search(ps):
        return False, "R7 issue has link"
    if IMG_RE.search(ps):
        return False, "R8 issue has image"
    if len(ps.strip()) < 200:
        return False, "R9 issue too short"

    return True, ""


# ------------------------------------------------------------------ draw --

def stratified_draw(pool, n, rng):
    """Round-robin across (repo, difficulty) strata; cap any repo at MAX_REPO_SHARE."""
    by_stratum = defaultdict(list)
    for r in pool:
        by_stratum[(r["repo"], r["difficulty"])].append(r)
    for s in by_stratum.values():
        s.sort(key=lambda r: r["instance_id"])   # fixed order before shuffle
        rng.shuffle(s)
    strata = sorted(by_stratum.keys())
    rng.shuffle(strata)

    chosen, repo_count = [], Counter()
    max_per_repo = max(1, int(n * MAX_REPO_SHARE))
    progress = True
    while len(chosen) < n and progress:
        progress = False
        for s in strata:
            if len(chosen) >= n:
                break
            if not by_stratum[s]:
                continue
            cand = by_stratum[s][0]
            if repo_count[cand["repo"]] >= max_per_repo:
                continue
            chosen.append(by_stratum[s].pop(0))
            repo_count[cand["repo"]] += 1
            progress = True
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20260902)
    ap.add_argument("--n-pilot", type=int, default=3)
    ap.add_argument("--n-main", type=int, default=8)
    ap.add_argument("--n-reserve", type=int, default=8)
    ap.add_argument("--solo", action="store_true", help="add Solo baseline to the episode estimate")
    ap.add_argument("--out", default="swe_task_selection.json")
    ap.add_argument("--pilot-out", default="swe_pilot.json")
    ap.add_argument("--local", default=None,
                    help="JSON list of the 500 Verified rows incl. 'difficulty' (built from GitHub mirrors)")
    args = ap.parse_args()

    if <host>:
        rows = json.load(open(<host>))
    else:
        from datasets import load_dataset
        rows = [dict(r) for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")]
    rows.sort(key=lambda r: r["instance_id"])
    assert len(rows) == 500 and all(r.get("difficulty") for r in rows), "expected 500 rows with difficulty labels"

    pool, rejected = [], Counter()
    for r in rows:
        ok, why = eligible(r)
        if ok:
            pool.append(r)
        else:
            rejected[why] += 1

    rng = random.Random(args.seed)
    taken = set()

    def draw(n):
        picked = stratified_draw([r for r in pool if r["instance_id"] not in taken], n, rng)
        taken.update(r["instance_id"] for r in picked)
        return picked

    pilot = draw(args.n_pilot)
    main_set = draw(args.n_main)
    reserve = draw(args.n_reserve)

    def ids(xs):
        return [x["instance_id"] for x in xs]

    def brief(xs):
        return [{"instance_id": x["instance_id"], "repo": x["repo"],
                 "difficulty": x["difficulty"],
                 "src_lines_changed": patch_stats(x["patch"])[2]} for x in xs]

    filt = {
        "max_files": MAX_FILES, "max_lines": MAX_LINES, "min_src_lines": MIN_SRC_LINES,
        "fail_to_pass_range": [MIN_FAIL_TO_PASS, MAX_FAIL_TO_PASS],
        "allowed_difficulty": sorted(ALLOWED_DIFFICULTY),
        "exclude_images": True, "exclude_links": True, "min_issue_chars": 200,
        "exclude_test_only_patches": True, "max_repo_share": MAX_REPO_SHARE,
    }
    out = {
        "dataset": "princeton-nlp/SWE-bench_Verified", "split": "test",
        "source": <host> or "huggingface",
        "design": "A0/A1/P0/P1 on every task; one replicate",
        "hints_text_used": False,
        "seed": args.seed, "filter": filt,
        "total_rows": len(rows), "eligible_pool": len(pool),
        "rejection_reasons": dict(rejected),
        "pilot": brief(pilot), "main": brief(main_set), "reserve_in_order": brief(reserve),
    }
    out["selection_sha256"] = hashlib.sha256(
        json.dumps({k: out[k] for k in ("seed", "filter", "pilot", "main", "reserve_in_order")},
                   sort_keys=True).encode()).hexdigest()

    with open(args.pilot_out, "w") as f:
        json.dump({"pilot": ids(pilot), "seed": args.seed}, f, indent=2)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)

    # ------------------------------------------------------------ report --
    n_cond = 4 + (1 if args.solo else 0)   # A0/A1/P0/P1 frozen
    print(f"Total: {len(rows)}   Eligible: {len(pool)}")
    print("Rejections:", dict(rejected))
    print(f"\nPILOT  -> {args.pilot_out}  (send to co-author B now)")
    for i in ids(pilot): print("  ", i)
    print("\nMAIN:")
    for i in ids(main_set): print("  ", i)
    print("\nRESERVE (use in order when a main task fails infra validation):")
    for i in ids(reserve): print("  ", i)
    print(f"\nRepo mix (main):       {dict(Counter(r['repo'] for r in main_set))}")
    print(f"Difficulty mix (main): {dict(Counter(r['difficulty'] for r in main_set))}")
    print(f"\nEpisode budget: {len(main_set)} tasks x {n_cond} conditions = "
          f"{len(main_set) * n_cond} main episodes (+{len(pilot) * n_cond} pilot). Runtime: see pilot.")
    print(f"\nselection_sha256: {out['selection_sha256']}")
    print("""
Freeze: git add select_swe_tasks.py swe_task_selection.json swe_pilot.json && git commit -m "freeze SWE task selection"
Do this BEFORE the first main episode.

Post-filter validation (co-author B, per task, in listed order):
  1. container builds in the SWE-bench harness
  2. gold patch -> resolved
  3. empty patch -> NOT resolved
  4. gold patch resolved again on a second run (determinism)
  5. full eval runtime < 10 min   [CHECK threshold]
A task failing any check is replaced by the next reserve ID. Log every replacement.
""")


if __name__ == "__main__":
    main()
