# Study decisions

## 2026-09-04T17:20:00-04:00 — StockBench deferred; SWE closed (no reruns)

**StockBench** is deferred to future work. The co-author’s predeclaration
(v0.3 archive stock pipeline; contamination gate C.2 in the v0.4 package
pilot assessment) remains an **appendix**, not a confirmatory arm. The
stock runner does not share the `episode.json` / `events.jsonl` contract
(`RECONCILIATION.md` item 6). No StockBench episodes were launched in this
close-out.

**SWE is closed.** The confirmatory matrix is **28 measured episode records
+ 3 evaluator-failure cells (among those 28) + 4 handwritten
infrastructure_failure cells** = 32 canonical IDs. No reruns. Account
quota remains until 2026-10-01 00:00 UTC. matplotlib-24149 is
`task_unevaluable` (A1 evaluator_failure + A0/P0/P1 infrastructure /
overflow). Overflow case: `data/dev/swe/context_overflow_case.md`. Four
terminal infra records left unchanged.

## 2026-09-04T10:45:00-04:00 — matplotlib harness timeouts are evaluator_failure, not ITT 0

On-disk SWE-bench harness reports (schema v2) for three confirmatory cells
completed with `failure_reasons=tests_timed_out`, `completed_instances=0`,
`completed_ids=[]`, `ambiguous_failure_instances=1`, and no `tests_status` /
FAIL_TO_PASS results. The runner had treated `resolved_instances=0` as a
scored fail (`evaluator_status=ok`, `final_resolved=0`). That coding is
wrong: the evaluator did not finish.

Reclassified as **`evaluator_failure`**, excluded from the binary ITT 0/1
(missing outcome, not a 0). `not_finalized` / empty-patch / handwritten
`infrastructure_failure` cells remain None→0.

| cell | run_id | old coding | new coding |
|---|---|---|---|
| matplotlib-24149/A1 | `dbd8ebdf-f0f5-4209-92bb-ff01e70954a4` | ITT 0 (`final_resolved=0`, eval=ok) | `evaluator_failure` (missing) |
| matplotlib-25332/A1 | `f396d0e3-3111-499c-ae4c-8b86d2a540d0` | ITT 0 | `evaluator_failure` (missing) |
| matplotlib-25332/P1 | `49235be6-4b58-4849-bfcb-73e4f715606c` | ITT 0 (candidate and final) | `evaluator_failure` (missing) |

matplotlib-25332 A0 `<commit>` and P0 `<commit>` never ran the harness
(`not_finalized`, empty `final.patch`, `f2p_p2p_null_reason=no_final_evaluation`).
Those stay ITT 0. No other main-task harness report has `tests_timed_out`
(the `incomplete_ids` field on completed reports is an empty list, not a timeout).

ITT rule: pairwise-complete cases. τ0 uses 8 tasks (A0 and P0 both valid).
τ1 uses 6 tasks (drops matplotlib-24149 and matplotlib-25332). δ uses the
same 6 tasks (needs all four cells). Remaining complete pairs are still
A0=P0 and A1=P1, so τ0=τ1=δ=0 with degenerate CIs (seed 20260902, 10000
task-clustered draws). Old matrix 0s on those three cells were misclassified
timeouts.

No launches. Manifest budgets unchanged. Episode `data/runs/` records and
eval summaries were rewritten in place (gitignored) to match the taxonomy.
Analysis detects timeouts from `evaluator_status` or on-disk
`failure_reasons`.

## 2026-09-04T04:25:00-04:00 — Pilot v6 gate FAIL; 3b cycle 1 (swe-0.3.3)

`scripts/swe_pilot_decision.py` on the union of 7 retained v5 + 5 v6 reruns: **FAIL** (9/12 valid). Failed criterion is 12/12 valid records, not the P-activation bar (3/6 nonempty among the union).

Three v6 P reruns died as batch ERRORs with no `episode.json`:
- sklearn-14983/P0 orphan `4a437ad3-3d28-4810-8706-e008e8150721`
- sklearn-14983/P1 orphan `d24ce124-56ae-4746-a0be-c6221ad6ac42`
- matplotlib-25479/P0 orphan `df9e5ad4-01dc-4150-9778-9b322941fd6b`

Event evidence on all three: worker `stop_reason=max_tokens` at 8192 output tokens, `continued_on_max_tokens` index=1, then Anthropic 400 `tool_use` without following `tool_result`. Classification: **interface** for all three (swe-0.3.2 continuation appended prose after a truncated assistant message that still contained `tool_use` blocks). No agent-class failure.

Authorized 3b cycle 1: swe-0.3.3 conversation guard in `swebench/`, unit test, rerun only those three pairs, recompute gate on mixed provenance. Caps/prompts/selection unchanged. Do not launch main until that gate PASSes (or PASS-with-caveat at ≥2/6 P nonempty). Details: `data/dev/swe/pilot_v6_failure.md`.

## 2026-09-04T04:32:00-04:00 — Launch v6 cycle-1 (3 interface cells, swe-0.3.3)

Fix landed at `<commit>` (`ConversationGuard` + `tests/test_swe_033_conversation_guard.py`). Launching only sklearn-14983/P0, sklearn-14983/P1, matplotlib-25479/P0 from `<REPO>` on `swe-adapter`. Caps remain 80/160 observation; concurrency 3; tmux `swe-pilot-v6c1` + `swe-pilot-v6c1-finalizer`. No prompt/selection change. No episode restart. Main not launched.

Launched batch `swe-pilot-v6c1-20260904T082632Z-<id>` at HEAD `<commit>`. Three study containers up; API 200s on first calls; finalizer watching.

## 2026-09-04T04:52:00-04:00 — Pilot v6 cycle-1 gate PASS; freeze caps; launch main

`scripts/swe_pilot_decision.py --union-json data/dev/swe/pilot_v6c1_union.json`: **PASS**.
12/12 valid, provenance 12/12, A no patch 6/6, A1 scratch 3/3, assistant text 12/12, P nonempty **4/6**. Mixed 0.3.1+0.3.2+0.3.3. Conversation guard fired on the three reruns; no 400s.

Caps from `usage_for_caps_v6.csv` (12 complete / 12 non-truncated per role), 1.5× max, rounded, never below observed max, 0 counterfactual truncations:
- worker turns 74→115, tools 73→110, output 23653→36000
- parent turns 65→100, tools 64→100, output 27252→41000
- wall override 1800s safety ceiling (computed 720/840 unused as budget)
- per-call max_tokens 8192 (continuation + 0.3.3 guard)

`_load_budget` now prefers `swebench.caps` so main does not inherit Spreadsheet 25/60. Spreadsheet `budgets:` / model / temperature unchanged.

Main: 8 A-2 issues × A0/A1/P0/P1, task-set main, concurrency 3, tmux `swe-main`, protocol swe-0.3.3 entirely. Tag name `freeze-swe-0.3.2` kept as specified; frozen runner is swe-0.3.3.

Launched `swe-main-20260904T085320Z-<id>` at freeze HEAD `<commit>` / tag `freeze-swe-0.3.2` at 04:53 EDT. First three cells sympy-24562 A0/A1/P0; API 200s.

## 2026-09-04T05:05:00-04:00 — Main sympy-24562/P0 harness 400 (prompt too long)

Batch still running (3/32). sympy-24562 A0/A1 complete. P0 orphan `1f14ca5f-aba4-4f15-b53c-9ff5c93701eb`: worker sealed (`phase_completed`, `memo_sealed`), parent mid-loop (73 model_calls, last `stop_reason=tool_use`, cache_read 161400) then Anthropic 400 `prompt is too long: 209834 tokens > 200000 maximum` (`<request_id>`). No episode.json. Classification: **interface** (unhandled context-window 400; harness exception, not an agent seal). Do not restart in-flight episodes. After the batch is terminal, write a failure record and/or 0.3.4 catch+rerun of this pair only if a swebench-local fix is testable. Other cells continue.

## 2026-09-04T05:36:00-04:00 — Main matplotlib-24149 all four cells prompt-too-long 400

Batch still running (12/32 terminals = 7 complete + 5 errors). django-13297 A0/A1/P0/P1 all complete (`final_resolved=1`). matplotlib-24149 wiped all four cells in the **worker** phase (no memo seal, no episode.json):

- A0 orphan `53e885b8-061d-40e9-bc2f-b38abd3a4099` — 48 worker model_calls, last `tool_use`, cache_read 150951, then 400 `250256 tokens > 200000` (`<request_id>`)
- A1 orphan `43d2b400-b293-4fbf-a4e3-edd904266ca8` — 42 worker calls, cache_read 147995, 400 `247195` (`<request_id>`)
- P0 orphan `35f4b943-d563-4092-ba1e-029ed13a18e2` — 51 worker calls, cache_read 152459, 400 `251907` (`<request_id>`)
- P1 orphan `3a301d0f-51d0-4342-866e-952b912f6e9b` — 21 worker calls, cache_read 133672, 400 `232444` (`<request_id>`)

Classification: **interface** for all four (same unhandled context-window 400 as sympy-24562/P0). In-flight episodes not restarted. sklearn-13496 A0/A1/P0 continue. swe-0.3.4 catch+compact is drafted on worktree `<REPO>` branch `agent/swe-034-context` (not merged into the launch checkout while this batch is imported). After the batch is terminal, merge/rerun only crashed pairs and disclose mixed provenance.

## 2026-09-04T07:12:00-04:00 — Main batch terminal 27/32; merge swe-0.3.4; rerun 5 interface cells

`swe-main-20260904T085320Z-<id>` finished. Finalizer `<commit>`: valid 27/32. No new 400s after matplotlib-24149. matplotlib-25332 all four completed. Runner dead; study containers gone.

Five interface orphans remain (no episode.json). Merged `agent/swe-034-context` `<commit>` into `swe-adapter` (`<commit>`). swe-0.3.4 ConversationGuard compacts older tool_results (last-resort assistant prose) on `prompt is too long` and retries; if compact cannot proceed, `PromptTooLongError` writes `infrastructure_failure` / `provider_prompt_too_long`. Tests: `tests/test_swe_034_context_window.py`. Caps/prompts/selection unchanged. Frozen `swebench.caps` still apply (`task_set=main`).

Launching only the five crashed pairs from `<REPO>`: sympy-24562/P0 and matplotlib-24149 A0/A1/P0/P1. tmux `swe-main-034` + `swe-main-034-finalizer`. Mixed provenance will be disclosed (27× swe-0.3.3 @ `<commit>` + 5× swe-0.3.4). Do not restart the 27 completed cells.

Launched `swe-main-034-20260904T111326Z-<id>` at HEAD `<commit>`. First three cells sympy-24562/P0 + matplotlib-24149 A0/A1; protocol assert swe-0.3.4; API 200s.

## 2026-09-04T07:40:00-04:00 — Provider usage limit; stop new launches; write main_report

swe-0.3.4 rerun finished 1/5: matplotlib-24149/A1 `<commit>` complete `final_resolved=0`. The other four died with Anthropic 400 `You have reached your specified API usage limits. You will regain access on 2026-10-01 at 00:00 UTC.` Guard compact fired first; this is **infrastructure** (account quota), not an agent outcome. No further launches. Four handwritten `infrastructure_failure` episode records (ITT 0) fill the matrix. `data/dev/swe/main_report.md`: 8×4, τ0=τ1=δ=0 with degenerate CIs (every task A0=P0 and A1=P1). `analyze.py` still cannot take `--task-set main`.

## 2026-09-04T10:20:00-04:00 — Rewrite four handwritten infra records; empty-memo QC

The four ITT-0 `infrastructure_failure` fillers were rewritten in place under
`data/runs/` (gitignored) so each assigned ID carries its own audited orphan,
0.3.4 rerun, and both HTTP 400 failure codes. IDs copied from
`data/dev/swe/main_compliance.md` / batch logs; none invented. Records stay
`infrastructure_failure`, ITT 0, `analysis_included: true`. Sibling-copied
reproducibility is now marked `provenance_measured: false`. ITT matrix
unchanged (no reruns).

| assigned | cell | orphan (0.3.3 prompt-too-long) | rerun (0.3.4 usage-limit) |
|---|---|---|---|
| `16bf4093-11ce-49e2-bf29-eb0b8a271394` | sympy-24562/P0 | `1f14ca5f-aba4-4f15-b53c-9ff5c93701eb` | `d8bee9ac-34e5-4597-89b5-0e076b4510d6` |
| `2389d180-0479-4c13-8da1-512f781a49ca` | matplotlib-24149/A0 | `53e885b8-061d-40e9-bc2f-b38abd3a4099` | `3f8fdee5-1444-47cf-94d1-5253b7f0978f` |
| `40a7ca7f-6d28-45c9-8e39-07fe378ac439` | matplotlib-24149/P0 | `35f4b943-d563-4092-ba1e-029ed13a18e2` | `ee581b17-b7ee-4eb3-8ddc-5dfb2d00320c` |
| `0a8eef47-5256-4114-945c-a7ad9c71f4bb` | matplotlib-24149/P1 | `3a301d0f-51d0-4342-866e-952b912f6e9b` | `9d122813-1658-4bc3-b565-cc8e66c2582c` |

Failure codes on every row: `provider_prompt_too_long` (Anthropic HTTP 400
`invalid_request_error` / prompt too long on `swe-main-20260904T085320Z-<id>`)
and `provider_usage_limit` (Anthropic HTTP 400 `invalid_request_error` / account
usage limit until 2026-10-01 00:00 UTC on `swe-main-034-20260904T111326Z-<id>`).
Both orphan and rerun directories exist under `data/runs/`; no ID left null.

QC: empty memo body (`task_understanding` blank) is `advisory_missing_memo`,
not “memo present”. Analysis-side only (`qc.py` / `analyze.py`). Unit test:
`tests/test_qc.py`. Observed case: matplotlib-25332/A0 `<commit>`.

## 2026-09-03 — Wave 1 extension decision

Wave 1 has mixed provenance: 17 task-condition episodes ran at `<commit>`, while the three authorized `46121` reruns ran at `<commit>`. The only code difference affecting those reruns is the narrowed dirty-tree gate. The B.4a trigger evaluated false at a pooled success rate of `0.70`, so the hard extension does not run.

## 2026-09-03 — Main-task extension to n=25 (power decision)

The main task set is extended from 20 to 25 tasks by drawing the next five unused reserve IDs in package list order (`91-34`, `160-6`, `423-16`, `48975`, `42526`). This decision was made on power grounds after Wave 2: 20 tasks × 4 cells underpower the confirmatory test when the outcome distribution is more concentrated than the pilot suggested. The extension uses the pre-specified reserve list in order with no hand-selection. All five reserve tasks pass Rules 1–10 and the Stage 3 evaluator checks. The task-selection package is immutable; this decision is recorded here as a study-owned extension log entry.

## 2026-09-03 — Hard exploratory difficulty probe (post-Wave 2, supplement-only)

B.4a trigger evaluated false at 0.70 (threshold 0.85), so the sealed 10-task B.4a hard extension does not run. Independently of that contingency, after reviewing Wave 2 results the study team decided to run five tasks drawn from the sealed hard-extension pool (`108-24`, `157-4`, `230-16`, `60-7`, `17-35`) as an exploratory difficulty probe. These tasks are stratum Sheet-Level AND (answer range ≥ 100 cells OR multi-sheet init), representing a harder subset. This probe was decided after Wave 2 data existed and is therefore supplement-only: results are reported in a separate hard-probe table (Table S2) and are never pooled with main estimates. The task_set flag `hard` is used throughout the codebase to enforce this separation.

## 2026-09-03 — v0.4 observable-feedback trigger and companion-study status

The v0.4 trigger was the co-author's review of the v0.3 pilot traces for task `48643`
in P1 and task `55965` in A1. That review showed that `+F` permitted execution without
observation: the worker could execute its candidate but could not inspect the recalculated
output before handoff. The co-author had not seen any main-study results when identifying
this issue and proposing the observable-feedback correction.

The completed v0.3 main study is retained as the **“execution-only” companion study**;
it is not replaced or reinterpreted as v0.4. The exploratory hard probe ran once under
v0.3 and will never be rerun. The v0.3 SOLO result is retained.

## D-v0.4.1-gate — 2026-09-03T19:43:31Z / 2026-09-03T15:43:31-04:00

**Decision:** Correct the protocol-v0.4.1 combined pilot characterization and
continue the already-authorized main launch under the pre-registered
censoring rule.

**Corrected facts:** All 12 combined pilot records exist, are schema-valid, and
have `status=complete`, but only 11/12 have
`compliance.parent_finalized=true`. The sole exception is task `55965`, cell
A1, run `a15eb550-5ab4-41a6-a9f8-a745df4ca862`: final evaluator status
`not_finalized`; parent `budget_exhausted=true`,
`budget_exhausted_on=max_turns`, with `max_turns=40`. The other 11 records have
parent finalized, evaluator status `ok`, and no budget exhaustion. All records
are retained.

**Rule precedence and rationale:** The pre-registered analysis contract in
`config/study/v04_package/analysis_contract_spreadsheet.md` §8 at revision
`<commit>` says: “If any cell has ≥ 2 censored parents, the effort estimates for
that cell are flagged as right-censored in the text.” The observed censoring is
A1=1 and A0/P0/P1=0, satisfying the operational cap of at most one censored
parent per cell. The stricter fix-kit criterion at the same revision,
`config/study/v04_package/fix_spec_v04_observable_feedback.md` Part 3, requires
“12/12 finalized; 0 location failures; 0 tool errors; 0 budget exhaustion;
validator VALID, QC clean.” The replay fails that fix-kit criterion. The source
episode record controls over the earlier report summary. By explicit user
decision, the pre-registered censoring rule controls whether main continues;
the fix-kit failure is not hidden, waived retroactively, or relabeled as a
clean 12/12 pass.

**Tag and launch disclosure:** Annotated tag object
`<commit>` remains unchanged and peels to
`<commit>`. Main launch commit
`<commit>` is launcher-alias-only relative to
the tagged report state. Batch `dab91361-0497-4f0c-a92b-426e99adaa9f`
(`task_set=main_v04`) was active when this decision was recorded. Future
reporting must disclose 11/12 finalization, the one A1 parent `max_turns`
exhaustion, final status `not_finalized`, the stricter fix-kit failure, and the
explicit decision to proceed under the pre-registered rule. This decision does
not determine any unrelated analysis-inclusion question.

## 2026-09-03 — Amendment A-2 SWE selection proposal

**Status:** proposed / awaiting co-author approval.

The Amendment A-2 package record is
`config/study/swe_selection_v2/AMENDMENT_A2.md` (SHA-256
`269eb4647f92e945d90825b9a92dec1aca96bc455ecc81261c8d15e4c70dc9ab`).
The requested 8-main/8-reserve selection was blocked by the 0.25 repo-share
constraint under the unchanged seeded v0.3 algorithm, so no selection JSON or
selection SHA was generated and no partial draw is approved. No manifest
change or image action was taken.

## 2026-09-03 — Amendment A-2 approved by both authors

**Status:** approved by both authors. Do not duplicate this entry.

Both authors accept Amendment A-2: **8 main + 7 reserve** under the **0.25
repo cap**. The 7/8 reserve shortfall is retained; an eighth reserve was not
invented, thresholds were not relaxed, and no task was hand-picked.

- Package: `config/study/swe_selection_v2/swe_task_selection_v2.json`
- Amendment record: `config/study/swe_selection_v2/AMENDMENT_A2.md`
- `selection_sha256`: `087855dc153547ea3543fc293c8491a5a5789d926cedff6ca468eab962f0c225`
- Launcher pointer: `scripts/launch_swe_main.sh` pins that JSON path and SHA
  and refuses to run if the file or SHA is missing. `scripts/swe_status.sh`
  uses the same pin. Runner/orchestration code was not copied onto main.
- Main images: all 8 pulled with `--platform linux/amd64` (see
  `data/dev/swe/a2_main_image_pull.md`). No containers or images were run,
  rebuilt, or pruned.
- SWE main was **not launched**.
- `config/manifest.yaml` `swebench:` caps were **not added or changed**.
  Manifest cap freeze is not done yet.

## 2026-09-04T17:59:05-04:00 — swe-0.3.5 pre-registered

swe-0.3.5 pre-registered. Selection: R1–R9 at v0.3 thresholds, matplotlib excluded as infrastructure (3/3 harness timeouts), all previously exposed IDs excluded, seed 20260905, 8 main + 8 reserve, sha e426590d…; pilot = first 3 reserve. Protocol change from 0.3.4: empty-diff reprompt-once rule; evaluator timeout 1800 s. Caps frozen at swebench block. Primary: resolved. Missing ≠ 0; pairwise-complete; n reported. Predictions: P1 τ0, τ1 ≥ 0; P3 parents accept observed candidates unchanged more often; P4 non-finalization concentrates in −F parents. Contingency: if <3 of 8 issues are discordant on resolved, parent effort and non-finalization rate are the reported outcomes. Solo on the 8 main issues as secondary.

Package: `config/task_selection_swe_v3/`.
`selection_sha256`: `e426590d9e9342af60ab48c5e3ddb174a73fbdfb05de5fff7ffb7d348752f0be`.
Table `files`/`lines` filled from `patch_stats` after the draw; draw unchanged.
No episodes launched.

## 2026-09-04T18:00:00-04:00 — Selection accepted; waiting on protocol + Gate 0

Coordinator accepted selection commit `<commit>` on `swe-0.3.5`. SHA prefix `<commit>` matches the frozen package. Draw not rerun.

Pilot IDs remain first three reserve: astropy-7606, sympy-19783, django-13112.
Main remains the eight v3 main IDs. Gate 0 replacement order unchanged: django-14155, sphinx-11445, sklearn-12682, pytest-7490, astropy-14096.

Gate 0 in progress under `data/dev/swe/gate0_v3/` (read-only on source). Early results: sympy-19783 PASS; django-13112 PASS; others pending. Empty-diff and evaluator/analysis siblings still own their files; protocol not committed; no pilot launch; no merge to `main`.

## 2026-09-04T18:02:00-04:00 — Eval/analysis accepted; empty-diff and Gate 0 still open

Coordinator accepted eval/analysis commit `<commit>` on `swe-0.3.5`. Not redone. Harness timeout 1800 s with one retry then `evaluator_failure` (missing). ITT recode IF/EF missing never 0; pairwise n τ0=6, τ1=6, δ=5; cell counts A0 4/7, A1 5/6, P0 4/6, P1 5/6. Reported 98 tests passed.

Gate 0 pilots all three PASS (astropy-7606, sympy-19783, django-13112). Eight main IDs still running. Empty-diff sibling still owns loop/seal (`run.py` / `seal.py` uncommitted; `PROTOCOL_VERSION` still swe-0.3.4). No protocol commit, no pilot launch, no merge to `main`.

## 2026-09-04T18:08:00-04:00 — Protocol swe-0.3.5; empty-diff accepted; waiting on Gate 0

Coordinator accepted empty-diff commit `<commit>` (not redone). Event is `failure` code `sealed_after_reprompt` plus `swe_protocol_usage.{worker,parent}.sealed_after_reprompt`. Schema has no dedicated event type.

`PROTOCOL_VERSION` bumped to `swe-0.3.5`. Wiring only: `apply_observation_caps` is a no-op so frozen `swebench.caps` (worker 115/110, parent 100/100, max_tokens 8192, wall 1800) stay; `_SELECTION_PACKAGE` / `_SELECTION_SHA` point at v3 (`e426590d…`); `_ALLOWED_TASK_SETS` includes `main_v035` / `solo_v035`. Launch scripts: `scripts/launch_pilot_v035.py` and `scripts/swe_pilot_v035_durable_finalizer.py`. No merge to `main`.

Pilot 3×4 launches the moment Gate 0’s 11-row table is complete (PASS or reserve-replaced). Until then, no launch.

## 2026-09-04T18:11:26-04:00 — Gate 0 11/11 PASS; launched 3×4 pilot

Gate 0 table complete: 11/11 PASS, no reserve applied, selection SHA `e426590d…` unchanged. Coordinator did not rewrite `data/dev/swe/gate0_v3/`.

Launched swe-0.3.5 pilot immediately from `<REPO>` HEAD `<commit>`. Batch ID `swe-pilot-v035-20260904T221126Z-<id>`. Protocol `swe-0.3.5`. Tasks: astropy-7606, sympy-19783, django-13112 × A0/A1/P0/P1. `--task-set pilot`. Concurrency 3. Frozen caps. Sonnet 4.5. tmux `swe-pilot-v035` + durable finalizer `swe-pilot-v035-finalizer`. No merge to `main`. Hard gate 22:30 EDT.

## 2026-09-04T22:06:00-04:00 — Abandon pilot v035 ee1e89c8; re-pilot v035b; gate 00:30 EDT

Abandoned batch `swe-pilot-v035-20260904T221126Z-<id>` for **host network loss**. Two connectivity gaps; 7/12 connection errors; protocol untested. Not a scientific fail.

Killed tmux `swe-pilot-v035`, `swe-pilot-v035-finalizer`, `swe-pilot-v035-retry`. Removed live `swe-*` study containers. Did **not** delete the batch directory. Leftover gold `sweb.eval.django__django-14752.m5_gold_cdc0143d` left in place (pre-existing).

Re-pilot **from scratch** under protocol commit `<commit>` (no protocol/code change). Launcher retry policy `<commit>` active (connection/5xx/529/429, up to 3). Same 3 issues × 4 cells, frozen caps, concurrency 3, tmux `swe-pilot-v035b` + durable finalizer, reports `pilot_report_v035b.md`.

**Gate extended to 00:30 EDT 2026-09-05** for the same host-network reason. PASS → tag `freeze-swe-0.3.5`, launch main 8×4 + Solo 8 overnight as pre-registered. FAIL → `data/dev/swe/pilot_v035_failure.md`, stop, no main.



## 2026-09-04T22:25:00-0400 — swe-autolaunch armed

Durable tmux job waiting on `<REPO>`. Deadline 00:30 EDT 2026-09-05. PASS → tag `freeze-swe-0.3.5` + `swe_freeze_and_launch.sh`. FAIL or no verdict by deadline → `data/dev/swe/pilot_v035_failure.md`. Protocol pin `<commit>`. No Cursor agent on this path.

## 2026-09-04T23:10:31-0400 — v035b PASS; starting freeze + main/Solo launch

Finalizer gate PASS for `swe-pilot-v035b-20260905T020727Z-<id>`. Protocol pin `<commit>`. Next: tag `freeze-swe-0.3.5`, commit decisions, run `scripts/swe_freeze_and_launch.sh` (main 8×4 + Solo 8, frozen caps, retries ≤3, tmux `swe-main-035` + finalizer).

```
{
  "batch_id": "swe-pilot-v035b-20260905T020727Z-<id>",
  "generated": "2026-09-05T03:10:16.952483+00:00",
  "gate": "PASS",
  "n_valid": 12,
  "n_total": 12,
  "n_provenance": 12,
  "shortfall": false,
  "report": "<REPO>",
  "csv": "<REPO>",
  "commit": "<commit>",
  "git_ok": true
}

```

## 2026-09-04T23:10:31-0400 — freeze start

v035b PASS accepted. Freezing protocol pin <commit>. Tag freeze-swe-0.3.5. Then launch main 8×4 + Solo 8 under frozen swebench caps (worker 115/110, parent 100/100, max_tokens 8192, wall 1800), concurrency 3, retries connection/5xx/529/429 up to 3, tmux swe-main-035 + durable finalizer. After the tag: no code, prompt, cap, or selection change.

## 2026-09-04T23:10:31-0400 — freeze commit

Committed decisions.md as <commit>. Protocol files unchanged from <commit>.

## 2026-09-04T23:10:31-0400 — tagged freeze-swe-0.3.5

Annotated tag freeze-swe-0.3.5 at <commit>. Launch proceeds under this freeze. No merge to main. No push.

## 2026-09-04T23:10:31-0400 — launching main 8×4 + Solo 8

Invoking <REPO> Task-sets main_v035 / solo_v035. Session swe-main-035. Frozen caps. Infra retries ≤3. Own durable finalizer.

## 2026-09-04T23:10:32-0400 — Solo 8 documented hole

Pre-registration asked for Solo on the 8 main issues as secondary (`--task-set solo_v035`). Schema allows SOLO, but frozen `run.py` `_ALL_CONDITIONS` is ['A0', 'A1', 'P0', 'P1']. After `freeze-swe-0.3.5`, no protocol change. Solo 8 is a documented hole. Main 8×4 still launches.

## 2026-09-04T23:10:32-0400 — main/Solo launched

Batch `swe-main-v035-20260905T031032Z-<id>`. tmux `swe-main-035` + `swe-main-035-finalizer`. Episodes: 32 (main 32, Solo 0 hole). Concurrency 3. Frozen caps. Protocol swe-0.3.5 pin <commit>. Log `<REPO>`.

## 2026-09-04T23:10:32-0400 — main-035 finalizer started

Waiting on `swe-main-v035-20260905T031032Z-<id>` (32 cells). Will write `data/dev/swe/main_v035_batch_status.md` at terminal.

## 2026-09-04T23:10:32-0400 — swe_freeze_and_launch.sh exited 0

Freeze/launch script finished with exit `0`. Autolaunch will now exit. Main/Solo continue under `swe-main-035` if launch succeeded.

## 2026-09-05T01:43:49-0400 — main-035 finalizer complete

Batch `swe-main-v035-20260905T031032Z-<id>`: 32 finished, 0 error, 0 missing. Report `<REPO>`.

## 2026-09-05T02:05:00-0400 — S1 process analyses (main_v04 + main_v035)

`src/delegation_study/analysis/process.py` is the shared A1–A5 path. Outputs: `paper_outputs/process/spreadsheet/` and `paper_outputs/process/swe/`. Tests: `tests/test_analysis/test_process.py`.

Disposition is hash-only (`handoff.candidate_script_sha256` vs `final_script_sha256`). The episode `accepted_unchanged` flag is never used. On main_v035 it is False on every P cell, including the 11 hash-identical accepts.

SWE outcomes are `env_metrics.swebench.{candidate,final}_resolved` with null left missing (never 0). Spreadsheet keeps pre-registered ITT 0, including 57232 A0 context overflow; every spreadsheet table also reports harmonized sensitivity treating that cell as missing.

Attenuation gap Δ_W − Δ_F is descriptive, not a mediation estimate. θ_V and the gap use task-clustered bootstrap (seed 20260902, B=10000). Parent inspections count spreadsheet `read_output_range` (explicit plus v0.4.1 `auto_observation`) and SWE `read_file` plus recovered `git diff|show|log`.

## 2026-09-05T02:12:00-0400 — S0 swe-0.3.5 main_v035 freeze report

S0 confirmatory analysis of batch `swe-main-v035-20260905T031032Z-<id>` (32/32 complete, 0 errors, 0 handwritten, 0 replacements). Protocol pin `<commit>`, freeze tag `freeze-swe-0.3.5` at `<commit>`. Analysis-side only: `--task-set main_v035` on `analyze.py` (does not reuse `SWE_MAIN_RUN_IDS`).

Primary `env_metrics.swebench.final_resolved` via `swe_itt_resolved` (missing ≠ 0). All 8 issues pairwise-complete. τ0=−0.125 (n=8) 95% CI (−0.375, 0); τ1=0 (n=8); δ=+0.125 (n=8). Discordant issues: 1/8 (pylint-6386 A0=1 vs P0=0). Pre-registered contingency triggered (<3). Report leads with primary null, then parent effort and non-finalization.

32/32 structurally valid after documenting frozen `episode_schema.json` `task_set` enum lag (`main_v035` recorded; contracts not opened). Six QC-flagged cells classified agent with event evidence. P disposition from patch hashes: unchanged 11, revised 3, rebuilt 2, discarded 0. Archive copies at `data/dev/swe/archive/main_v035/`. Report: `data/dev/swe/main_report_v035.md`. Tests: `tests/test_analysis/test_swe_main_v035.py`. No merge to `main`.

## 2026-09-05T02:00:00-04:00 — Fixed-artifact (FA) parent-replay design

Pre-register the Spreadsheet **fixed-artifact** experiment (S2). This entry
locks the design before the runner is written. No main_v04 record is altered.
`task_set` is `fixed_artifact` and is never pooled with main / main_v04 /
solo_v04 / pilot / hard.

**Sample.** All 50 completed `main_v04` P0/P1 cells (25 tasks × P0/P1,
protocol 0.4.1, candidate `solution.py` + memo on disk). Source
`worker_outcome.full_task_success` is a host-only stratum label (38 correct,
12 incorrect). It is never shown to the parent.

**Design.** Each candidate is replayed to a **fresh parent** on two arms:
`evidence_withheld` vs `evidence_supplied`. Task text, parent system prompt,
parent tools, parent budget, and clean public workbook are **byte-identical**
to `main_v04` (`_parent_system_prompt` SHA-256
`0193f4a73fd2c1c837363dd75e890b03e1fba21f221e043f33b5d968c76ef209`;
manifest parent budget 40/100/1200/8192/64000; LibreOffice 26.8.0.3).
Arm order is randomized **per candidate** with design seed **20260905**
(logged on every assignment and episode). Replicate encoding: 1 =
withheld, 2 = supplied. Pairing key = source `run_id`.

**Handoff.** Parent receives the original task, the frozen candidate
`solution.py`, and a **sanitized** memo. Sanitizer is deterministic and
correctness-blind: drop keys `observed_feedback` and `validation_status`;
drop any sentence matching verification/execution stems
`test|verif|check|confirm|observ` (prefix) or whole-word `ran`/`running`
(`range` is kept). The sanitized memo bytes are identical across arms.
The parent never sees hidden data, gold, scores, source traces, or
`.env` / `data/hidden` / `data/raw`.

**Evidence arm.** After sanitization, the supplied arm appends **one**
standardized block generated by the host: run the candidate on a clean
workbook, then call the host `read_output_range` implementation on
`answer_position`. The block reports recalculated values, formulas, and
error cells, labeled “execution output of the candidate.” It is never a
correctness judgment (no pass/fail/gold/score language). Withheld arm
omits the block. User-message SHA-256 matches across arms after the block
is stripped.

**Hypotheses (paired within candidate).**
- H_FA1: evidence reduces parent verification (primary).
- H_FA2: evidence increases unchanged adoption.
- H_FA3: among incorrect candidates, evidence reduces repair.
- H_FA4: among correct candidates, evidence reduces checking without
  reducing final success.

**Outcomes.** Primary = parent execution+inspection actions
(`run_solution` + explicit `read_output_range` tool_result counts), paired
within candidate. Secondaries = any-verification (primary>0 or
auto-observation>0), unchanged adoption, revision, final success, repair
among incorrect, damage among correct, parent tokens, non-finalization.

**Inference.** Mean of (supplied − withheld) over candidates. Uncertainty:
task-clustered percentile bootstrap, B=10000, seed 20260902 (study seed),
α=0.05. Candidates from the same task (P0 and P1) are clustered together.

**Operations.** Standalone CLI
`python -m delegation_study.spreadsheetbench.parent_replay`. No edit to
`run.py` / `tools.py` / `agent_loop.py`. Frozen episode schema is not
widened; FA records use a local schema extension (`task_set=fixed_artifact`
plus a `fixed_artifact` object) and a sidecar `fixed_artifact.json`.
Smoke: 2 candidates × 2 arms must write valid paired records. Hard gate
14:00 EDT 2026-09-05: if valid pairs are not being produced, write
`data/dev/fixed_artifact_deferred.md` and do not launch the 100. Else
launch 100 episodes, concurrency 6, tmux, durable finalizer. Fallback if
time is short: all 12 incorrect candidates plus an equal seeded sample of
correct ones, labeled `correctness_enriched`. Analysis writes
`data/dev/fixed_artifact_report.md` only.
