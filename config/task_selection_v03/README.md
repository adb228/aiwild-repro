# Paper A — Task Selection & StockBench Kit: Read Me First

You know the research: a parent agent delegates to a worker under a fixed budget; the worker either
**advises** (memo) or **produces** (patch / transform script / portfolio); and separately the worker
either **can execute code** to check its work (+F) or **cannot** (−F). Four cells — A0, A1, P0, P1 —
on every task, in three environments. The parent always submits the final answer.

This zip covers two things that were on co-author A's plate:

1. **Which tasks we run** in each environment, chosen by a fixed rule before any agent ran.
2. **The StockBench environment**, designed and built end to end so it runs in one evening.

Everything for SWE and Spreadsheet is *selection only* — you're building those adapters.
Everything for StockBench is a *complete runnable environment*.

---

## 1. The idea behind task selection (30 seconds)

We can't run every task four times. If we hand-pick a subset, a reviewer can say we picked the ones
that flatter our result. So for each environment a script applies a written rule to every task
(rules are about the task itself: size of the gold fix, whether the problem text has a link, etc.),
then a seeded shuffle picks from the survivors. Output = pilot IDs (shakedown only), main IDs (the
experiment), reserve IDs (ordered backups). The rule, the seed and the IDs get one git commit before
the first real episode. That commit is our pre-registration.

Your job on selection: **validate that each chosen task actually works on your machine**, in the order
listed. If one is broken, take the next reserve of the same type — don't choose a substitute — and
log the swap.

---

## 2. What's in the zip

### SWE-bench (selection only)

| File | What it is | What you do with it |
|---|---|---|
| `swe_pilot.json` | 3 pilot IDs | validate these first |
| `swe_task_selection.json` | 8 main + 8 reserve IDs, rejection counts, seed, hash | the main list to validate |
| `appendix_A_swe.md` | the rules, why each exists, **your validation commands (A.6)**, freeze commit (A.7), and the adapter contract — what each cell's worker may do, what the artifact is, what the parent receives, how to score (A.8) | read A.6–A.8 |
| `select_swe_tasks.py`, `build_swe_local.py`, `swe_verified_local.json` | the script that produced the IDs, and its input data | reference only; rerun reproduces the same IDs |

Two adapter facts from this that you need: never show `hints_text` (the issue comment thread) to any
agent — it often contains the fix; and −F means the worker gets **no code execution at all**, not just
"no tests."

### SpreadsheetBench (selection only)

| File | What it is | What you do with it |
|---|---|---|
| `ss_pilot.json` | 3 pilot IDs | validate these first |
| `ss_task_selection.json` | 20 main + 31 reserve, rejection counts, the 4 swaps made after review, hash | the main list to validate |
| `appendix_B_spreadsheet.md` | same structure as A: rules, **validation (B.7)**, freeze (B.8), adapter contract (B.9), limitations, change log | read B.7–B.9 |
| `select_spreadsheet_tasks.py` | the script | reference |

Three adapter facts: the benchmark's `evaluation.py` **command line won't work** on Verified files
(different filenames) — call `compare_workbooks(golden, output, instruction_type, answer_position)`
directly; outputs must be **recalculated in LibreOffice** before grading (pin the version); and B.9
defines two extra metrics to log — a hard-coding flag and an unintended-modification count.

### StockBench-derived (complete environment)

| File | What it is |
|---|---|
| `README_stock.md` | **the five commands to run it, in order** |
| `appendix_C_stockbench.md` | the full design: what StockBench is and what we changed, the contamination gate (C.2), task definition, cells, outcomes, date selection, analysis, the implementation kit (C.12), the one-night plan (C.13) |
| `stock_pilot.json`, `stock_date_selection.json`, `select_stock_dates.py` | the decision dates (1 pilot used, 8 main, 7 backups) and how they were chosen |
| `stockbench_snapshot.py` | builds the point-in-time information bundle, validates allocations, scores them. `--selftest` proves it. |
| `backtest_trailing.py` | the only tool +F workers and the parent get: backtest a candidate over the 20 days *before* the decision date. Refuses later dates. |
| `check_memo_compliance.py` | flags an advisory memo that smuggles in a portfolio |
| `prompts_stock.json` | worker A vs P differ only in the role paragraph; ±F differ only in the tool paragraph; one parent prompt |
| `llm_client.py` | the two LLM calls, on the Anthropic SDK. Model and budgets come from env vars. |
| `run_stock_episode.py` | one command = one episode: bundle → worker → compliance → parent → validate → score → JSON log |
| `analyze_stock.py` | reads the logs, prints the paired table and bootstrap CIs |

The whole chain was dry-run with a mock model: 20 episodes → 20 logs → analysis table. It's waiting
on an API key and a model string.

---

## 3. What to do, in order

**Tonight (Spreadsheet is the anchor — do this first)**
1. Validate the 3 Spreadsheet pilot IDs per B.7. Then the 20 main.
2. Wire the Spreadsheet adapter to B.9. The −F worker gets a static text snapshot of the workbook and
   no execution; +F gets a sandbox. Parent always has the sandbox.

**StockBench (background job, ~2–3 h mostly waiting)**
3. `README_stock.md`. First command is the self-test; it must say "all passed."
4. **Check the model's training cutoff against 2025-03-26 → 2025-07-31 (C.2).** If the model may have
   seen those months, stop — the environment can't work, and the paper stands on the other two.
5. One pilot episode, read the log. Then all five cells on the pilot date. Set the budget env vars
   from what the pilot used, freeze them.
6. Fire the 32 main episodes. Run `analyze_stock.py`.

**SWE (small arm, when Spreadsheet is running)**
7. Validate the 3 pilot IDs per A.6, then the 8 main. Adapter per A.8.

**Then:** one git commit per environment with the script + JSONs + your validation log (commands in
A.7 / B.8 / C.12). Before the first main episode.

---

## 4. Things still yours to fill in

| # | Item | Where it lands |
|---|---|---|
| 1 | Model string + training cutoff | `PAPER_A_MODEL`; C.2 gate |
| 2 | Budgets (max calls / tokens per role) | from pilot; env vars for Stock, your adapter for the others |
| 4 | Worker tool manifest per cell | Stock's is done (C.12); Spreadsheet/SWE per B.9 / A.8 |
| 5 | Advisory compliance thresholds | Stock's is `check_memo_compliance.py`; Spreadsheet/SWE per B.9 / A.8 suggestions |
| 6 | Parent tool manifest | identical in every cell — confirm |
| 9 | Evaluator commands + validation results | A.6 / B.7 / C.8 give the commands; you supply the results |

## 5. Two calls co-author A made that you should sign off on

- **Four cells on every task** in all three environments — that's what every script assumes.
- **Spreadsheet main is split evenly** across the four task-type buckets (5/5/5/5), not proportional to
  the benchmark. Reason in B.4. Say if you disagree.

Questions → co-author A. Anything ambiguous in the appendices is marked `[CHECK]` rather than decided.
