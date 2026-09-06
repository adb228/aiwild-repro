# Appendix B — SpreadsheetBench Verified: Task Selection and Implementation

**Status:** selection frozen 2026-09-02 (amended pre-freeze after peer review, B.3 Rule 10; hard extension sealed 2026-09-03, B.4a). Pilot run 2026-09-03 on all three pilot tasks × four cells (B.12). Four pre-freeze changes from the pilot are listed in B.12 and must land before the pilot rerun that constitutes the freeze check.
**Role in the paper:** primary anchor. 20 main tasks × 4 cells (A0/A1/P0/P1) = 80 core episodes, plus 3 pilot tasks.
**Files:** `select_spreadsheet_tasks.py`, `ss_task_selection.json` (now includes the sealed hard extension), `ss_pilot.json`, `pilot_assessment_spreadsheet.md`.

---

## B.1 Principle

Every selection rule is a property of the task itself — the instruction text, the input workbook, or the benchmark's own metadata. No rule depends on how any agent performs, and no task is chosen by hand. The filter and draw were written and run before any agent episode. The selection is committed with a SHA-256 over `{seed, filter, pilot, main, reserve}` so it can be re-derived and verified.

Each rule serves one of three purposes:

- **(G) Gradeability** — the benchmark evaluator can compare the output workbook to the golden one at the answer position.
- **(T) Treatment integrity** — Advisory and Production are actually different on this task, and worker and parent see identical information.
- **(F) Feasibility** — a worker on half the inference budget could plausibly produce a candidate transformation.

---

## B.2 Data source

`spreadsheetbench_verified_400/dataset.json` and per-task folders from the official repository (`RUCKBReasoning/SpreadsheetBench`, `data/spreadsheetbench_verified_400.tar.gz`), 400 instances.
Fields used: `id`, `instruction`, `instruction_type`, `answer_position`, `answer_sheet`, `data_position`, `spreadsheet_path`, `exclude`.

Facts about the set that shape the filter (verified on the data 2026-09-02):

- Each task has exactly one `*_init.xlsx` and one `*_golden.xlsx`. The multi-test-case design (several input workbooks per instruction) exists only in the 912-task set. Consequences for evaluation are handled in B.9 (hard-coding flag) and stated as a limitation in B.10.
- `instruction_type` takes two values: `Cell-Level Manipulation` (275) and `Sheet-Level Manipulation` (125). There is no chart/visual category in Verified.
- `answer_sheet` is present only on sheet-level tasks; cell-level tasks are graded on the first sheet unless `answer_position` carries a sheet qualifier.
- `answer_position` may contain a sheet qualifier (`'Sheet1'!A1:B82`), several comma-separated ranges, or whole-column ranges (`A:G`).

Two filter axes requested in review do not apply to Verified: "drop chart/visual category" (no such category exists) and "number of test workbooks" (always one; Rule 2 enforces the single-workbook layout).

---

## B.3 Stage 1 — Filter (`select_spreadsheet_tasks.py`)

Rules 1–9 are applied to all 400 instances before the draw. Any single failure excludes the instance. Rejections are attributed to the first failing rule in script evaluation order (R1, R2, R9, R7, R8, R4, R3, R5, R6).

| # | Rule | Threshold | Purpose | Rationale | Rejected |
|---|---|---|---|---|---|
| 1 | No author `exclude` annotation | — | G | The benchmark authors flagged these as invalid. | 2 |
| 2 | Standard folder layout | exactly one `*_init.xlsx` and one `*_golden.xlsx` | G | Five folders use `initial.xlsx` / `golden.xlsx`; excluded so the adapter has one file contract. (One of the five is also author-flagged and is counted under Rule 1.) | 4 |
| 3 | Init workbook loads in openpyxl | — | G | If the runtime cannot open it, no transform can be written. | 0 |
| 4 | Init workbook size | ≤ 200 KB | F | Large workbooks exceed context limits and slow every episode. | 9 |
| 5 | Sheet count in init workbook | ≤ 6 | F | Beyond this, sheet-level tasks become navigation tests rather than manipulation tests. | 4 |
| 6 | `answer_position` parseable and bounded | not whole-column, parseable A1 notation after stripping sheet qualifiers, ≤ 2,000 cells | G, F | Whole-column answers make the comparison ill-defined; very large ranges make partial-credit measures noisy. | 16 |
| 7 | `instruction` contains no URL | regex | T | Identical-information invariant: linked content cannot be guaranteed identical or available to both agents. | 1 |
| 8 | `instruction` contains no image/screenshot reference | regex | T | Same. | 1 |
| 9 | `instruction` length | 80 – 1,500 characters | T, F | Under 80 is not self-contained. Over 1,500 is excluded for tractability and because such texts are typically pasted multi-post forum threads whose content cannot be cleanly separated into instruction versus discussion. Note this is *not* a rule against partial solutions in the instruction: an instruction that includes the user's own attempted formula or code is admissible because both agents receive the same text. | 1 short, 5 long |

**Rules 1–9: 400 → 357 eligible.** Pool by stratum: cell/single 208, cell/multi 44, sheet/single 65, sheet/multi 40.

**Rule 10 — minimum answer complexity (added 2026-09-02, pre-freeze).**

| # | Rule | Threshold | Purpose | Rationale |
|---|---|---|---|---|
| 10 | `answer_position` cell count | ≥ 5 | T | Analogue of the SWE ≥ 5-source-line rule. A one-cell or one-formula answer can be fully stated in an advisory memo ("put `=SUMIF(…)` in F4"), making the memo indistinguishable from the artifact and collapsing the A/P treatment. Five cells is the same threshold used for SWE. |

Rule 10 was added after peer review, before any validation or agent episode. It is applied **after** the seeded draw as a like-for-like replacement from reserve (next unused reserve ID of the same stratum, in order), rather than by re-drawing, because a re-draw under the new pool would have replaced the entire pilot and 18 of 20 main tasks and discarded validation already in progress. Applied to the eligible pool, Rule 10 would remove 98 tasks (pool 357 → 259: cell/single 126, cell/multi 34, sheet/single 61, sheet/multi 38). Applied to the draw, it replaced four main tasks and removed two reserve entries; the replacements are logged in B.4 and in `ss_task_selection.json → amendments`. All three pilot tasks pass Rule 10.

---

## B.4 Stage 2 — Stratified seeded draw

1. Sort the eligible pool by `id`.
2. Bucket into four strata: `instruction_type` (cell / sheet) × sheet count in the init workbook (single / multi).
3. Within each stratum, sort by `id`, then shuffle with `random.Random(20260902)`. Shuffle the stratum order with the same generator.
4. Deal round-robin across the four strata; cap any stratum at 60% of the draw. Because all four strata are large, this yields an equal 5/5/5/5 allocation.
5. Draw in order without replacement: pilot (3), main (20), reserve (40).
6. Apply Rule 10 with like-for-like replacement (B.3).

| Set | Size | IDs |
|---|---|---|
| Pilot | 3 | `55965`, `469-9`, `48643` |
| Main | 20 | `57232`, `42216`, `567-21`, `599-9`, `46121`, `37229`, `408-5`, `267-21`, `54474`, `6239`, `416-27`, `388-47`, `44296`, `58723`, `97-36`, `534-26`, `44266`, `44389`, `402-43`, `341-14` |
| Reserve (in order) | 31 | `91-34`, `160-6`, `423-16`, `48975`, `42526`, `493-5`, `408-39`, `55049`, `367-23`, `325-44`, `1818`, `41348`, `192-22`, `382-29`, `52532`, `374-18`, `84-40`, `9448`, `440-24`, `194-19`, `53161`, `353-6`, `130-9`, `16511`, `142-12`, `188-39`, `47827`, `35739`, `382-10`, `409-45`, `9391` |

**Selection SHA-256:** `cf818c1c9be7568223245e89a705d94d64aa7d99e4341604414378571f11752d` (covers pilot, main, reserve, and the sealed hard extension)

Rule 10 replacements (all same-stratum):

| Removed | Answer cells | Stratum | Replaced by | Answer cells |
|---|---|---|---|---|
| `333-29` | 1 | sheet/multi | `599-9` | 18 |
| `48608` | 4 | cell/single | `37229` | 60 |
| `8942` | 1 | cell/multi | `44296` | 102 |
| `59358` | 3 | cell/single | `44389` | 6 |

Reserve entries `44628` (4 cells) and `58701` (2 cells) were removed by Rule 10. Reserve was extended 12 → 24 → 40 drawn on the same day, before validation; after Rule 10 and the four swaps it stands at 31 (sheet/single 10, sheet/multi 9, cell/single 6, cell/multi 6 — Rule 10 thins cell-level tasks, hence the skew). Each extension appended to the list; earlier entries and their order are unchanged. All 31 pass Rules 1–10 (answer cells 5–740, ≤ 65 KB, ≤ 4 sheets).

**Allocation (decided 2026-09-02, revised after review).** Equal allocation across strata (5/5/5/5) is retained. With five tasks per stratum the design **does not support per-stratum effect estimates**, and the paper makes none; stratification serves only to guarantee that all four task types are represented. Because the equal-weight aggregate is not representative of Verified (≈ 58% cell/single), the paper reports **both** the design-weighted aggregate (simple mean over the 20 tasks) and a benchmark-weighted aggregate (post-stratified by eligible-pool shares 208/44/65/40). The design-weighted estimate is primary.

Pilot is for infrastructure and evaluator shakedown only, is permanently excluded from main, and supports no scientific conclusions.

### B.4a Ceiling contingency and sealed hard extension (pre-declared 2026-09-03, before any main episode)

The pilot (B.12) scored 1.0 in 9 of 12 episodes, and two of the three non-1.0 results were target-location guessing that the `answer_position` fix removes. A ceiling on the binary primary outcome in main is therefore plausible.

**Contingency, pre-registered:** if pooled `full_task_success` across all cells on **main wave 1** (the first five main tasks in list order: `57232`, `42216`, `567-21`, `599-9`, `46121`) exceeds **0.85**, then

1. the confirmatory Spreadsheet analysis is on **parent post-handoff effort** (model calls, output tokens, write/run cycles, wall-seconds) and `target_unit_pass_rate`, with `full_task_success` reported descriptively; this is hypothesis H2 and was in the design from the start;
2. the **hard extension** runs: ten additional tasks, drawn and sealed today from the stratum *Sheet-Level AND (answer range ≥ 100 cells OR multi-sheet init)*, using the same seeded order after pilot/main/reserve and excluding all of them and the two dev-burned tasks. Reported separately from main, never pooled without a flag.

Sealed hard-extension IDs (pool 37): `108-24`, `157-4`, `230-16`, `60-7`, `17-35`, `177-6`, `13-1`, `61-4`, `66-24`, `384-4`.

If wave-1 success is ≤ 0.85 the extension does not run and the analysis proceeds as originally planned. Whether the extension runs depends only on this trigger; which tasks it contains was fixed before any main result existed. The stratum is the closest objective proxy to "longer, harder" the metadata offers; it is not a difficulty label, and the paper says so.

**Dev-burned tasks.** `79-7` and `343-20` were used for engineering smoke tests under this harness and are excluded from reserve and the hard extension post-draw (they were never in pilot or main). The seeded draw itself is unchanged.

---

## B.5 Threshold sensitivity (run 2026-09-02)

Each threshold varied independently with the others frozen. Eligible pool (Rules 1–9) by stratum, out of 400:

| Threshold | Value | cell/single | cell/multi | sheet/single | sheet/multi | Total |
|---|---|---|---|---|---|---|
| `max_init_kb` | 100 | 205 | 43 | 63 | 40 | 351 |
| | **200** | **208** | **44** | **65** | **40** | **357** |
| | 400 | 210 | 46 | 66 | 40 | 362 |
| `max_sheets` | 3 | 208 | 43 | 65 | 35 | 351 |
| | 4 | 208 | 44 | 65 | 38 | 355 |
| | **6** | **208** | **44** | **65** | **40** | **357** |
| | 10 | 208 | 47 | 65 | 40 | 360 |
| `max_answer_cells` | 500 | 208 | 43 | 57 | 38 | 346 |
| | 1000 | 208 | 44 | 63 | 39 | 354 |
| | **2000** | **208** | **44** | **65** | **40** | **357** |
| `max_instr_chars` | 1000 | 200 | 41 | 63 | 38 | 342 |
| | **1500** | **208** | **44** | **65** | **40** | **357** |
| | 2500 | 212 | 45 | 65 | 40 | 362 |
| `min_instr_chars` | 0 | 209 | 44 | 65 | 40 | 358 |
| | **80** | **208** | **44** | **65** | **40** | **357** |
| | 150 | 202 | 44 | 65 | 40 | 351 |

No threshold changes any stratum by more than 8 tasks; composition is stable, not only size. The only rule that materially reshapes the pool is Rule 10 (B.3), which removes 39% of cell/single tasks and ≤ 23% of the others — as intended, since one-cell answers concentrate in that stratum. Any change to any threshold reshuffles the seeded draw and yields a different set; thresholds are therefore frozen rather than tuned.

---

## B.6 Review of the drawn set (read, not edited)

All pilot and main instructions were read after the draw. No task was removed by hand. Observations for the protocol:

- Ten main tasks phrase the request as a VBA macro or Power Query (`469-9` pilot; `567-21`, `599-9`, `408-5`, `416-27`, `388-47`, `97-36`, `534-26`, `402-43`, `341-14`). The evaluator grades the output workbook, not the method; agents implement the transformation in Python. Stated in the paper.
- Two main tasks include the user's own non-working code (`416-27`, `388-47`). Both agents receive the same text; see Rule 9 rationale.
- `402-43` is stratified single-sheet (init has one sheet) but the answer is written to a `Sheet2` the transformation must create. Stratification is by init workbook, so this is correct.
- `54474` is briefly worded. It passes every rule; there is no objective filter for underspecification, and hand-removal would violate the selection principle.
- Reserve tasks were read on the same basis. `47827` (reserve) requires bold and fill colour in addition to values; the evaluator compares fill and font colour, so it is gradeable, but it tests formatting as well as computation.

---

## B.7 Stage 3 — Evaluator validation (co-author B)

**Environment.** `pip install -r SpreadsheetBench/requirements.txt` and LibreOffice **pinned to one version** for the whole study — record it here `[CHECK: LibreOffice x.y.z]` and in every run log. The evaluator loads workbooks with `data_only=True`, so any formula written by a transform must be recalculated before grading; the benchmark's `open_spreadsheet.py` does this through headless LibreOffice. Recalculation results can differ across LibreOffice versions and some Excel-only functions do not evaluate; pinning the version and running check 12 catches this per task.

**Important:** the benchmark's `evaluation.py` command line expects the 912-set file names (`<k>_<id>_input.xlsx` / `<k>_<id>_answer.xlsx`). Verified uses `1_<id>_init.xlsx` / `1_<id>_golden.xlsx`. Call the comparison function directly:

```python
from evaluation import compare_workbooks   # SpreadsheetBench/evaluation/evaluation.py
ok, msg = compare_workbooks(golden_path, output_path, instruction_type, answer_position)
```

`compare_workbooks` returns `(bool, message)`; `ok=True` is the primary outcome.

For each pilot and main ID, **in listed order**:

| # | Check | How | Pass condition |
|---|---|---|---|
| 10 | Golden vs itself | `compare_workbooks(golden, golden, type, pos)` | `True` |
| 11 | Init vs golden | `compare_workbooks(golden, init, type, pos)` | `False` (task is not already solved) |
| 12 | Recalculation preserves golden | copy golden → `python open_spreadsheet.py --dir_path <copy_dir> --backend libreoffice` → `compare_workbooks(golden, recalculated, type, pos)` | `True` |
| 13 | Harness round-trip | a no-op `solution.py` (copies init to output) loads, writes, recalculates, and is scored | scored without error (score `False` expected) |
| 14 | Perturbation harness | the perturbed-copy generator (B.9) produces a valid workbook for this task | no error |

**Replacement rule.** A task failing any check is removed and replaced by the **next unused reserve ID of the same stratum, in list order** (fall back to any stratum only if that stratum's reserve is exhausted). Every replacement is logged with the task ID, the failing check number, and the reserve ID that took its place.

**Freeze check.** After the four pilot-driven changes (B.12), the three pilot tasks are rerun × four cells. Freeze requires ≥ 11/12 episodes finalized, zero target-location failures, zero tool errors, and no budget-exhaustion events. Then commit and tag `freeze-spreadsheet-v0.3`.

**Logging template** (`ss_validation_log.md`):

```
| id | stratum | 10 golden=golden | 11 init≠golden | 12 recalc | 13 harness | 14 perturb | status | replaced_by |
```

---

## B.8 Freeze

When all pilot and main IDs pass Stage 3:

```
git add select_spreadsheet_tasks.py ss_task_selection.json ss_pilot.json ss_validation_log.md
git commit -m "freeze Spreadsheet task selection (sha <commit>)"
```

This must happen **before the first main episode**. After the freeze the main set is never reduced after results are observed; it may be extended by drawing further reserve IDs in order (like-for-like where possible); any change to `--n-main` before main runs is logged with date and reason.

---

## B.9 Implementation contract for the Spreadsheet adapter

Items marked `[CHECK]` are owned by co-author B and pasted in from the tool manifests when fixed.

### Task loading

- Each episode starts from a fresh copy of `1_<id>_init.xlsx` in an isolated working directory. No state carries between episodes or cells.
- The agent-visible task is `instruction`, `instruction_type`, and `answer_position`, plus the init workbook — **exactly the fields SpreadsheetBench's official inference prompt provides** (`inference/prompt_format.py`: "you only need to modify or fill in values within the cell range specified by answer_position"). Identical in every cell. **The golden workbook and its values are never shown to any agent.**
  *Correction 2026-09-03:* the previous version of this appendix withheld `answer_position`. The pilot showed that turns tasks whose instruction does not name the target column into location guessing (B.12) — noise identical across cells and pure loss of power — and deviates from the benchmark's own protocol. Withholding it was an error and is reversed pre-freeze.
- Agents may inspect the workbook in all cells via read-only tools (`list_sheets`, `workbook_metadata`, `read_range` over any sheet and any range, values and formulas, `search_workbook`).

### The feedback factor (+F / −F) — definition for this environment

The factor is **the ability to execute code against the workbook**. This is the Spreadsheet analogue of "can run the test suite" in SWE and is a discrete capability, not a matter of degree.

- **−F:** the worker has the read-only inspection tools (any sheet, any range, values and formulas) and **no execution tool of any kind in its API tool list** — no `run_ephemeral_python`, no `run_solution`. Enforced by the tool manifest, not by prompt text. It reasons from what it reads and writes its memo (A0) or `solution.py` (P0) blind. (Implemented as arbitrary read tools rather than the first-*N*-rows snapshot originally proposed; strictly more information, no arbitrary *N*, same absence of execution.)
- **+F:** the worker additionally has execution: `run_ephemeral_python` on a scratch copy (A1; scratch is destroyed at handoff and never reaches the parent) or `write_solution` + `run_solution` (P1), with recalculation, iterating within budget.

Pilot evidence (B.12): across six −F episodes the workers made zero execution calls; across six +F episodes they made 1–7. That is the contrast the mechanism claim rests on.

### Worker cells

| Cell | Read tools | Execution tools | Handoff |
|---|---|---|---|
| A0 (Advisory, −F) | yes | **none** | `memo.json` |
| A1 (Advisory, +F) | yes | `run_ephemeral_python` (scratch copy) | `memo.json`; scratch destroyed |
| P0 (Production, −F) | yes | **none** (`write_solution` only) | `memo.json` + `solution.py` |
| P1 (Production, +F) | yes | `write_solution`, `run_solution` | `memo.json` + `solution.py` |

Tool allow-lists per cell are those in `config/tool_manifests.yaml` (item #4, done). No worker tool reads the golden file.

### Artifacts

- **Structured memo** (all four cells): `memo.json` with fields `task_understanding`, `target_locations`, `proposed_approach`, `assumptions`, `observed_feedback`, `known_risks`, `validation_plan`. Identical schema in A and P, so the information content of the handoff differs only by the artifact. The memo must not contain an executable script; the enforceable treatment is *whether an executable artifact crosses the handoff*, checked by the `artifact_crossed_handoff` compliance flag from the sealed handoff, not by memo text.
- **Production artifact** (P0/P1 only): `solution.py`, the candidate transformation, submitted via `submit_solution`, copied and hashed into `handoff/` before workspace cleanup. In P1 the worker's execution feedback is its own; the parent sees the script and the memo, not the worker's run output.
- **Worker candidate is scored only after the parent finishes.** No evaluator output exists during the parent phase.

### Parent (identical in every cell)

- Receives the task (instruction, `instruction_type`, `answer_position`), a clean copy of the init workbook, and the handoff package only — `memo.json`, plus `solution.py` in P cells. Never the worker trajectory, tool history, scratch workspace, or cell label.
- Has the full tool set in every cell (item #6, done): read tools, `write_solution`, `run_solution`, `submit_solution`. Identical manifest across A0/A1/P0/P1.
- Produces the final `solution.py` (may be the candidate unchanged, edited, or rewritten) and submits it. The parent always submits. `accepted_unchanged` is computed by hash equality between the handoff candidate and the final script.

**Non-finalization and non-submission (decided 2026-09-03).**
- *Harness loss* — worker's `submit_memo`/`submit_solution` succeeded but the file is absent at copy time → `infrastructure_failure: handoff_lost`, parent never invoked, excluded from analysis.
- *Worker non-submission* — no successful submit → the parent proceeds with an empty handoff, is scored, and the episode carries `compliance.worker_submitted = false`; kept in analysis. A Production worker that fails to produce is a result about production delegation, not a harness event. `[CHECK — co-author B to confirm the runner implements this rather than aborting]`
- *Parent not finalized* — the parent never submits → agent failure, `full_task_success = 0`, kept in analysis, `unsealed_last_script_score` reported as a diagnostic. Defensible only once budgets are not the cause (B.12 change 2).

### Budgets (item #2, revised 2026-09-03 from the pilot)

Model `claude-sonnet-4-5-20250929`, temperature 0, `max_tokens` 8192 per call, both roles. Stopping rules per role: `max_turns` 25, `max_tool_calls` 60, `max_output_tokens_total` 64,000, `max_wall_seconds` 1,200. **Cumulative input tokens are a reported cost outcome, not a stopping rule.** The pilot showed that with only the system prompt cached, cumulative input grows quadratically with turns and a 200k cap fired at turn 18 on a parent that was reviewing a correct candidate — censoring that lands hardest on the role that inspects most. Prompt caching is applied to the conversation prefix (`cache_control` on the latest block); cached tokens are reported separately. Budgets are identical across cells and roles; Solo gets the sum.

### Evaluation

1. Run the final `solution.py` on a clean init copy in the sandbox container (`--rm`, labelled, force-removed on timeout) with a timeout of 120 s.
2. Recalculate `output_path` with the pinned LibreOffice (`open_spreadsheet.py`).
3. `compare_workbooks(golden_path, output_path, instruction_type, answer_position)` → **primary outcome** (bool).
4. In P cells, run steps 1–3 on the worker's handoff `solution.py` as well (after the parent finishes) → raw worker score *Y_worker*.
5. **Hard-coding flag** (secondary; addresses the single-test-case limitation). Because Verified grades one input file, a transform that writes literal answer values passes. To detect this: (a) generate a **perturbed copy** of the init workbook by applying a fixed, seeded perturbation to numeric cells inside `data_position` (multiply by a factor drawn from U(0.8, 1.2), seeded by task id; non-numeric cells untouched); (b) run the final transform on the perturbed copy and recalculate; (c) if the golden answer region depends on the data (checked once per task by applying the same perturbation to golden and recalculating — if golden's answer cells change, the task is data-dependent), then an output whose answer cells are **identical** between original and perturbed runs is flagged `hardcoded=True`. Also record `answer_has_formulas` (whether the output's answer cells contain formulas when golden's do). Report the hard-coded rate per cell alongside the primary outcome. Tasks whose golden answer is data-independent are exempt from the flag.
6. **Unintended modifications** (secondary): after recalculation, the set of cells *outside* `answer_position` (on all sheets present in init) whose value differs between the output workbook and the init workbook, plus any sheets added or removed. Report count; list in the episode log. The evaluator ignores these cells, so this is the only place unintended damage is measured.
7. Other secondary: fraction of answer cells matching (from `cell_level_compare` diagnostics); `Δ_parent = Y_final − Y_worker`; script edit distance between the handoff `solution.py` and the final `solution.py`; accept-without-change flag.

### Per-episode log

As implemented in `episode.json` / `events.jsonl` (protocol 0.2): run/task/cell/replicate/task_set; reproducibility (schema commit, benchmark commit, dataset hash, image digest, models, temperature, `git_dirty`, workspace header); budgets and per-role usage (input, output, cache-read tokens, calls, tool calls, validation cycles, wall-seconds, `budget_exhausted[_on]`); handoff paths and hashes with `worker_submitted_memo/candidate`; worker and final outcomes with `output_sha256`; `accepted_unchanged`; `unsealed_last_script_score`; failures; compliance (`artifact_crossed_handoff`, `worker_feedback_calls`, `parent_finalized`, `worker_scratch_destroyed`, `worker_submitted`); `analysis_included` with reason. Every `model_call` event persists assistant text inline (< 4 KiB) or by path. Still to add: `hardcoded` / `answer_has_formulas` flags and the unintended-modification count (B.9 §5–6).

---

## B.10 Limitations specific to this environment

- **Single-file grading.** The primary outcome is correctness on one input workbook, not generality of the transformation. The hard-coding flag (B.9 §5) is a detector, not a fix; a transform that is data-responsive but wrong on other inputs would still pass. The 912-set's multi-input design would be preferable and is future work.
- **Equal allocation.** The aggregate is an equal-weight average over task types; the benchmark-weighted aggregate is reported alongside it (B.4).
- **Recalculation engine.** Grading depends on LibreOffice's evaluation of formulas, which may differ from Excel's. The engine version is pinned and check 12 verifies golden survives it; an agent formula that Excel would evaluate but LibreOffice does not is scored as wrong.

---

## B.12 Pilot results (2026-09-03) and pre-freeze changes

Tasks `55965`, `469-9`, `48643` × A0/A1/P0/P1. Model `claude-sonnet-4-5-20250929`, temperature 0. Full assessment: `pilot_assessment_spreadsheet.md`.

| Task | A0 | A1 | P0 (worker→final) | P1 (worker→final) |
|---|---|---|---|---|
| 469-9 | 1.0 | 1.0 | 1.0 → 1.0, unchanged | 1.0 → 1.0, unchanged |
| 48643 | 1.0 | 0.0 (wrote col F; target H) | 1.0 → 1.0, edited | 0.0 → 0.0 (wrote col G), accepted unchanged |
| 55965 | 1.0 | 1.0 | 1.0 → 1.0, unchanged | 1.0 → not finalized (parent input-token cap at turn 18) |

Treatment compliance from `events.jsonl`: −F worker execution calls 0 in all six; +F 4, 6, 7 (A1) and 1, 4, 3 (P1); `solution.py` in handoff for P cells only; A1 scratch destroyed; parent submitted 11/12; memo schema identical across cells; validator and QC clean in all 12. Per-cell means (worker out-tokens / parent out-tokens / parent calls): A0 2.1k / 3.5k / 10.0; A1 7.1k / 2.4k / 7.7; P0 3.3k / 1.8k / 6.3; P1 4.5k / 3.0k / 10.0. Cost ≈ $0.40 and ≈ 2 min per episode.

**Verdict:** treatment enforced and harness trustworthy; four pre-freeze changes required, all logged below: (1) provide `answer_position`; (2) remove the input-token stopping rule and cache the prefix; (3) fix `search_workbook`; (4) pre-register the ceiling contingency and seal the hard extension. Freeze check = pilot rerun with these in place (B.7).

## B.11 Change log

| Date | Change | Reason |
|---|---|---|
| 2026-09-02 | Initial filter (Rules 1–9), seed 20260902, draw 3/20/12 | — |
| 2026-09-02 | Reserve extended 12 → 24 | headroom for Stage 3; pilot/main unchanged |
| 2026-09-02 | Rule 10 (≥ 5 answer cells) added post-draw with like-for-like replacement; 4 main tasks replaced, 2 reserve removed; hash → `<commit>` | peer review: Spreadsheet lacked the SWE minimum-complexity rule |
| 2026-09-02 | −F defined as "no code execution; static snapshot"; hard-coding flag and unintended-modification metric added; like-for-like replacement rule; LibreOffice version pinned; Rule 9 rationale corrected; allocation claims narrowed and benchmark-weighted aggregate added | peer review |
| 2026-09-02 | Reserve extended 18 → 31 (40 drawn); pilot, main, and first 18 reserve unchanged; hash → `<commit>` | headroom after Rule 10 |
| 2026-09-03 | Pilot run: 3 tasks × 4 cells (B.12) | — |
| 2026-09-03 | `answer_position` + `instruction_type` provided to both roles (matches official benchmark prompt); previous "withhold" rule reversed | pilot: location guessing on `48643` |
| 2026-09-03 | Cumulative input tokens removed as a stopping rule; prefix caching required; other caps retained | pilot: 200k cap censored a correct `55965 P1` parent at turn 18 |
| 2026-09-03 | `search_workbook` EmptyCell crash fixed | pilot: tool errors in 2 episodes |
| 2026-09-03 | Ceiling contingency pre-registered; hard extension (10 IDs) drawn and sealed; dev-burned tasks excluded post-draw; hash → `<commit>` | pilot: 9/12 at 1.0 |
| 2026-09-03 | −F documented as read tools (not snapshot); memo schema common to A and P; worker scored after parent; non-submission and non-finalization rules decided; model and budgets recorded | align appendix with implementation |
