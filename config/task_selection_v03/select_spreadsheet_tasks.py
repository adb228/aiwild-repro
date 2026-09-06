"""
SpreadsheetBench Verified task selection — Paper A (Production vs Advisory delegation).

Applies a pre-registered, purely operational filter to the 400-task Verified set
and draws pilot + main IDs by a seeded, stratified rule. No task is hand-picked.
Run once, commit the outputs, never re-run after an agent episode.

Usage:
    git clone https://github.com/RUCKBReasoning/SpreadsheetBench
    cd SpreadsheetBench/data && tar xzf spreadsheetbench_verified_400.tar.gz
    python select_spreadsheet_tasks.py --data SpreadsheetBench/data/spreadsheetbench_verified_400 \
        --seed 20260902 --n-pilot 3 --n-main 20 --n-reserve 40

Outputs:
    ss_pilot.json           -> send to co-author B immediately for evaluator validation
    ss_task_selection.json  -> full record (filter, seed, pool, pilot, main, reserve, hash)

Verified facts about the dataset used by this filter (checked 2026-09-02):
  * one init workbook + one golden workbook per task (no multi-test-case here)
  * instruction_type is Cell-Level (275) or Sheet-Level (125)
  * answer_sheet is present only on sheet-level tasks
  * 2 tasks carry an author 'exclude' note; 5 use nonstandard filenames
  * no chart/visual category exists in Verified (those are in the 912 set)

Frozen design (co-author B, 2026-09-02): four cells (A0/A1/P0/P1) on every task.
Spreadsheet is the anchor arm: 20 main tasks. Extend from reserve IN ORDER if
ahead; never shrink after results. Reserve replacement does not preserve the
stratum of the task it replaces.

Post-filter validation (co-author B, per task, in listed order):
  1. golden workbook scores 1.0 against itself with evaluation.py
  2. unmodified init workbook scores 0 (task is not already solved)
  3. LibreOffice recalculation of the golden file preserves the score
  4. transform.py harness can load init and write output for this file
A task failing any check is replaced by the next reserve ID. Log every swap.
"""

import argparse
import hashlib
import json
import os
import random
import re
import warnings
from collections import Counter, defaultdict

import openpyxl

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------- filter --

MIN_INSTR_CHARS = 80          # enough to be self-contained
MAX_INSTR_CHARS = 1500        # very long instructions are usually pasted threads
MAX_INIT_KB = 200             # keeps episodes fast and within context limits
MAX_SHEETS = 6                # beyond this, sheet-level tasks become navigation tests
MAX_ANSWER_CELLS = 2000       # keeps evaluator and diffs tractable
MAX_STRATUM_SHARE = 0.60      # no single stratum dominates the main set
CEILING_TRIGGER = 0.85        # pooled full_task_success on wave 1 above this -> run the hard extension
N_HARD = 10                   # hard-extension size
HARD_MIN_ANSWER_CELLS = 100   # hard stratum: sheet-level AND (answer cells >= this OR multi-sheet init)
DEV_BURNED = {"79-7", "343-20"}   # used for engineering smoke tests under this harness. Applied POST-DRAW
                                   # (removed from reserve and hard-extension; asserted absent from pilot/main)
                                   # so the seeded draw itself is unchanged.
MIN_ANSWER_CELLS = 5          # Rule 10 (added 2026-09-02, pre-freeze): answer range must span
                              # >= 5 cells. Analogue of the SWE >=5-source-line rule: a one-cell
                              # / one-formula answer can be fully stated in an advisory memo, so
                              # the A/P treatment collapses. Applied POST-DRAW with like-for-like
                              # replacement from reserve so that pilot and already-validated
                              # main tasks are preserved; see apply_rule_10().

URL_RE = re.compile(r"https?://\S+")
IMG_RE = re.compile(r"!\[.*?\]\(.*?\)|<img\b|\.png|\.jpg|screenshot", re.IGNORECASE)


def range_cell_count(pos: str) -> int:
    """Approximate number of cells across a comma-separated answer position."""
    total = 0
    # split on commas that are outside quoted sheet names
    parts = re.split(r",(?=(?:[^']*'[^']*')*[^']*$)", pos)
    for part in parts:
        part = part.strip().replace("$", "")
        if "!" in part:                      # drop 'Sheet name'!
            part = part.rsplit("!", 1)[1]
        if ":" not in part:
            total += 1
            continue
        a, b = part.split(":")
        if a.isalpha() and b.isalpha():       # whole columns, e.g. A:G -> treat as very large
            total += MAX_ANSWER_CELLS + 1
            continue
        try:
            (c1, r1) = openpyxl.utils.cell.coordinate_from_string(a)
            (c2, r2) = openpyxl.utils.cell.coordinate_from_string(b if not b.isdigit() else c1 + b)
            w = abs(openpyxl.utils.column_index_from_string(c2) - openpyxl.utils.column_index_from_string(c1)) + 1
            total += w * (abs(r2 - r1) + 1)
        except Exception:
            total += MAX_ANSWER_CELLS + 1     # unparseable -> reject downstream
    return total


def workbook_info(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    return len(wb.sheetnames), wb.sheetnames


def eligible(row, data_dir):
    """Return (ok, reason, info)."""
    info = {}
    if row.get("exclude"):
        return False, "R1 author-flagged exclude", info

    p = os.path.join(data_dir, row["spreadsheet_path"])
    files = os.listdir(p)
    inits = [f for f in files if f.endswith("_init.xlsx")]
    golds = [f for f in files if f.endswith("_golden.xlsx")]
    if len(inits) != 1 or len(golds) != 1:
        return False, "R2 nonstandard file layout", info

    ins = row["instruction"]
    if len(ins) < MIN_INSTR_CHARS:
        return False, "R9 instruction too short", info
    if len(ins) > MAX_INSTR_CHARS:
        return False, "R9 instruction too long", info
    if URL_RE.search(ins):
        return False, "R7 instruction has link", info
    if IMG_RE.search(ins):
        return False, "R8 instruction references image", info

    init_path = os.path.join(p, inits[0])
    kb = os.path.getsize(init_path) // 1024
    if kb > MAX_INIT_KB:
        return False, "R4 workbook too large", info
    try:
        n_sheets, _ = workbook_info(init_path)
    except Exception:
        return False, "R3 workbook unloadable", info
    if n_sheets > MAX_SHEETS:
        return False, "R5 too many sheets", info

    n_cells = range_cell_count(row["answer_position"])
    if n_cells > MAX_ANSWER_CELLS:
        return False, "R6 answer range whole-column, unparseable, or too large", info

    info = {"init_kb": kb, "n_sheets": n_sheets, "n_answer_cells": n_cells}
    return True, "", info


def is_hard(r):
    return r["_stratum"].startswith("sheet") and (r["n_answer_cells"] >= HARD_MIN_ANSWER_CELLS or r["n_sheets"] > 1)


def stratum(row, info):
    lvl = "cell" if row["instruction_type"].startswith("Cell") else "sheet"
    ms = "single" if info["n_sheets"] == 1 else "multi"
    return f"{lvl}/{ms}"


# ------------------------------------------------------------------ draw --

def stratified_draw(pool, n, rng):
    by = defaultdict(list)
    for r in pool:
        by[r["_stratum"]].append(r)
    for s in by.values():
        s.sort(key=lambda r: str(r["id"]))
        rng.shuffle(s)
    strata = sorted(by)
    rng.shuffle(strata)
    cap = max(1, int(n * MAX_STRATUM_SHARE))
    chosen, count, progress = [], Counter(), True
    while len(chosen) < n and progress:
        progress = False
        for s in strata:
            if len(chosen) >= n:
                break
            if not by[s] or count[s] >= cap:
                continue
            chosen.append(by[s].pop(0))
            count[s] += 1
            progress = True
    return chosen


def replace_like_for_like(task, reserve, used):
    """Next unused reserve of the same stratum; falls back to next unused of any stratum."""
    for r in reserve:
        if r["id"] not in used and r["_stratum"] == task["_stratum"]:
            return r
    for r in reserve:
        if r["id"] not in used:
            return r
    raise RuntimeError("reserve exhausted")


def apply_rule_10(pilot, main_set, reserve):
    """Post-draw minimum-complexity rule with like-for-like replacement."""
    amendments = []
    ok = lambda r: r["n_answer_cells"] >= MIN_ANSWER_CELLS
    reserve = [r for r in reserve if ok(r)]           # reserve entries failing R10 are dropped
    used = set()
    new_main = []
    for r in main_set:
        if ok(r):
            new_main.append(r)
        else:
            rep = replace_like_for_like(r, reserve, used)
            used.add(rep["id"])
            new_main.append(rep)
            amendments.append({"rule": "R10 min_answer_cells", "removed": r["id"],
                               "removed_cells": r["n_answer_cells"], "replaced_by": rep["id"],
                               "stratum": r["_stratum"], "same_stratum": rep["_stratum"] == r["_stratum"]})
    for r in pilot:
        if not ok(r):
            raise RuntimeError(f"pilot task {r['id']} fails R10; extend logic before freezing")
    reserve = [r for r in reserve if r["id"] not in used]
    return pilot, new_main, reserve, amendments


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="path to spreadsheetbench_verified_400/")
    ap.add_argument("--seed", type=int, default=20260902)
    ap.add_argument("--n-pilot", type=int, default=3)
    ap.add_argument("--n-main", type=int, default=20)
    ap.add_argument("--n-reserve", type=int, default=40)
    ap.add_argument("--solo", action="store_true")
    ap.add_argument("--out", default="ss_task_selection.json")
    ap.add_argument("--pilot-out", default="ss_pilot.json")
    args = ap.parse_args()

    rows = json.load(open(os.path.join(args.data, "dataset.json")))
    rows.sort(key=lambda r: str(r["id"]))

    pool, rejected = [], Counter()
    for r in rows:
        ok, why, info = eligible(r, args.data)
        if ok:
            r = dict(r, _stratum=stratum(r, info), **info)
            pool.append(r)
        else:
            rejected[why] += 1

    rng = random.Random(args.seed)
    taken = set()

    def draw(n):
        picked = stratified_draw([r for r in pool if r["id"] not in taken], n, rng)
        taken.update(r["id"] for r in picked)
        return picked

    pilot, main_set, reserve = draw(args.n_pilot), draw(args.n_main), draw(args.n_reserve)
    pilot, main_set, reserve, amendments = apply_rule_10(pilot, main_set, reserve)
    burned = lambda r: str(r["id"]) in DEV_BURNED
    assert not any(burned(r) for r in pilot + main_set), "burned dev task in pilot/main"
    reserve = [r for r in reserve if not burned(r)]
    taken = {r["id"] for r in pilot + main_set + reserve} | DEV_BURNED
    hard_pool = [r for r in pool if r["id"] not in taken and r["n_answer_cells"] >= MIN_ANSWER_CELLS and is_hard(r)]
    hard_pool.sort(key=lambda r: str(r["id"])); rng.shuffle(hard_pool)
    hard_ext = hard_pool[:N_HARD]

    def brief(xs):
        return [{"id": x["id"], "stratum": x["_stratum"], "instruction_type": x["instruction_type"],
                 "n_sheets": x["n_sheets"], "n_answer_cells": x["n_answer_cells"],
                 "init_kb": x["init_kb"], "spreadsheet_path": x["spreadsheet_path"]} for x in xs]

    filt = {"min_instr_chars": MIN_INSTR_CHARS, "max_instr_chars": MAX_INSTR_CHARS,
            "max_init_kb": MAX_INIT_KB, "max_sheets": MAX_SHEETS,
            "max_answer_cells": MAX_ANSWER_CELLS, "exclude_links": True,
            "exclude_image_refs": True, "exclude_author_flagged": True,
            "require_standard_layout": True, "max_stratum_share": MAX_STRATUM_SHARE,
            "min_answer_cells_post_draw": MIN_ANSWER_CELLS,
            "dev_burned_excluded_post_draw": sorted(DEV_BURNED),
            "strata": "instruction_type x single/multi-sheet"}
    out = {"dataset": "SpreadsheetBench Verified 400",
           "design": "A0/A1/P0/P1 on every task; one replicate",
           "allocation": "equal across strata; no per-stratum claims at n=5; report both design-weighted and benchmark-weighted (post-stratified) aggregates",
           "replacement_rule": "next unused reserve of the SAME stratum, in list order; fall back to any stratum only if exhausted",
           "libreoffice_version": "[CHECK - pin before freeze]",
           "amendments": amendments,
           "seed": args.seed, "filter": filt,
           "total_rows": len(rows), "eligible_pool": len(pool),
           "pool_by_stratum": dict(Counter(r["_stratum"] for r in pool)),
           "rejection_reasons": dict(rejected),
           "pilot": brief(pilot), "main": brief(main_set), "reserve_in_order": brief(reserve),
           "hard_extension": {"trigger": f"pooled full_task_success on main wave 1 > {CEILING_TRIGGER}",
                              "stratum": f"sheet-level AND (answer cells >= {HARD_MIN_ANSWER_CELLS} OR multi-sheet init)",
                              "pool_size": len(hard_pool), "sealed_ids": brief(hard_ext),
                              "reporting": "separate from main; never pooled without a flag"}}
    out["selection_sha256"] = hashlib.sha256(json.dumps(
        {k: out[k] for k in ("seed", "filter", "pilot", "main", "reserve_in_order", "hard_extension")},
        sort_keys=True).encode()).hexdigest()

    json.dump({"pilot": [x["id"] for x in pilot], "seed": args.seed}, open(args.pilot_out, "w"), indent=2)
    json.dump(out, open(args.out, "w"), indent=2)

    n_cond = 4 + (1 if args.solo else 0)   # A0/A1/P0/P1 frozen
    print(f"Total: {len(rows)}   Eligible: {len(pool)}   by stratum: {out['pool_by_stratum']}")
    print("Rejections:", dict(rejected))
    print(f"\nPILOT -> {args.pilot_out}:", [x["id"] for x in pilot])
    print(f"MAIN ({len(main_set)}):", [x["id"] for x in main_set])
    print(f"RESERVE in order ({len(reserve)}):", [x["id"] for x in reserve])
    if amendments:
        print("\nRule 10 replacements (like-for-like):")
        for a in amendments: print(f"   {a['removed']} ({a['removed_cells']} cells, {a['stratum']}) -> {a['replaced_by']}")
    print(f"HARD EXTENSION (sealed, runs only if wave-1 success > {CEILING_TRIGGER}; pool {len(hard_pool)}):", [x["id"] for x in hard_ext])
    print(f"\nMain stratum mix: {dict(Counter(r['_stratum'] for r in main_set))}")
    print(f"Episode budget: {len(main_set)} x {n_cond} = {len(main_set) * n_cond} main episodes "
          f"(+{len(pilot) * n_cond} pilot). Runtime: see pilot.")
    print(f"selection_sha256: {out['selection_sha256']}")
    print("\nFreeze: git add select_spreadsheet_tasks.py ss_task_selection.json ss_pilot.json "
          "&& git commit -m 'freeze Spreadsheet task selection'")


if __name__ == "__main__":
    main()
