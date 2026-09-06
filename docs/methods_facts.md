# METHODS_FACTS

Columns: fact | exact value | source path. Gaps say **MISSING**. Sonnet vs Luna: different model; not pooled. Code wins over appendices (`config/study/RECONCILIATION.md`).

## Intervention semantics

| ID | fact | exact value | source path |
|---|---|---|---|
| I-01 | Factorial cells | A0, A1, P0, P1 on every confirmatory task; one replicate. A = advisory (memo only). P = production (memo + artifact). 0 = −F. 1 = +F. | `data/dev/method_source_pack/protocols.md`; `config/task_selection_v03/ss_task_selection.json` `design` |
| I-02 | Spreadsheet v0.4.1 worker tools A0 | `list_sheets`, `workbook_metadata`, `read_range`, `search_workbook`, `submit_memo` | `data/dev/method_source_pack/protocols.md` v0.4.1 tool manifest (`config/tool_manifests.yaml@<commit>`) |
| I-03 | −F worker observation (A0/P0) | Inspection of the **input** workbook only. No execution tools. No auto-observation. Not blind to the input. A0/P0 worker auto-obs = 0 on confirmatory main. | `protocols.md`; `data/dev/method_source_pack/main_v041_report.md` (A0/P0 worker auto_observations 0); `config/study/RECONCILIATION.md` (“−F is read-only inspection over the whole workbook”) |
| I-04 | +F worker observation (A1/P1) Spreadsheet v0.4.1 | A1: `run_ephemeral_python` + scratch destroyed at handoff. P1: `run_solution`. Every successful run for eligible roles includes LibreOffice-recalculated `answer_position` (`auto_observation`). Explicit `read_output_range` retained. | `protocols.md`; `src/delegation_study/spreadsheetbench/tools.py` (cited in protocols.md) |
| I-05 | v0.4.1 auto-observation (parents) | All parents including SOLO: every successful `run_solution` carries `auto_observation`. Pilot: 33/33 worker payloads, 46/46 parent payloads, 0 explicit ROR. Main: 285/285 worker, 215/215 parent, 0 missing. | `data/dev/method_source_pack/compliance_summary.md`; `main_v041_report.md` |
| I-06 | v0.3 vs v0.4 vs v0.4.1 feedback | v0.3: execution without guaranteed observation (companion). v0.4: optional explicit ROR; gates FAIL (0/6 A1/P1 workers, 1/12 parents). v0.4.1: auto-delivers observation. | `protocols.md`; `docs/supplement/chronology.md`; `config/study/decisions.md` (v0.4 observable-feedback trigger) |
| I-07 | Spreadsheet parent tools v0.4.1 | `list_sheets`, `workbook_metadata`, `read_range`, `search_workbook`, `write_solution`, `run_solution`, `read_output_range`, `submit_solution`. Clean input. No worker trace. No worker evaluator output. | `protocols.md` parent block |
| I-08 | What crosses the Spreadsheet handoff | A: `memo.json` only. P: `memo.json` + `solution.py`. Parent starts from clean input. | `protocols.md` |
| I-09 | 7-field memo schema | Required: `task_understanding`, `target_locations`, `proposed_approach`, `assumptions`, `observed_feedback`, `known_risks`, `validation_plan`. `additionalProperties: false`. | `contracts/memo_schema.json` |
| I-10 | Memo fields vs SYSTEM prompt | Worker SYSTEM prompts do **not** enumerate the seven fields. `submit_memo` tool schema does. Code wins. | `contracts/memo_schema.json`; `data/dev/method_source_pack/prompts/v041_*_system.md`; `config/study/RECONCILIATION.md` |
| I-11 | SWE worker / parent tools | A worker: `exec_shell`, `read_file`, `write_file`, `submit_memo`. P worker: those + `submit_solution`. Parent: `exec_shell`, `read_file`, `write_file`, `submit_solution`. | `src/delegation_study/swebench/workspace.py` |
| I-12 | SWE −F/+F and handoff | −F: `exec_shell` rejects python/pytest execution. +F: allowed. Count only `executed=true` + `feedback_command`. A handoff: memo. P: memo + `candidate.patch`. A1 scratch_destroyed. Parent: clean `/testbed`; P applies candidate. | `workspace.py`; `run.py`; `RECONCILIATION.md` 13 (`compliance.worker_feedback_calls` is SoT) |
| I-13 | SWE Solo | **None.** `_ALL_CONDITIONS` = A0/A1/P0/P1. Schema alias `solo_v035` exists. Documented hole. | `src/delegation_study/swebench/run.py`; `data/dev/method_source_pack/swebench/main_report_v035.md` |

---

## Models and sampling

| ID | fact | exact value | source path |
|---|---|---|---|
| M-01 | Sonnet model strings | worker and parent `claude-sonnet-4-5-20250929`. **Not** a reasoning-model pin. | `config/manifest.yaml` `models.*` |
| M-02 | Sonnet temperature | `0.0` | `config/manifest.yaml` |
| M-03 | Sonnet prompt caching | `prompt_caching: true`. Implementation: ephemeral breakpoint on final conversation block, system tail, and last tool definition (not “conversation-prefix” as an older appendix said). | `config/manifest.yaml`; `data/dev/method_source_pack/protocols.md`; `config/study/RECONCILIATION.md` item 3 |
| M-04 | Sonnet per-call max_tokens | 8192 | `config/manifest.yaml` `budgets.*.max_tokens` |
| M-05 | Luna model | alias `gpt-5.6-luna`. OpenAI Responses. **different model; not pooled.** | `<REPO>`; `decisions_model2.md` |
| M-06 | Luna effort | Process pin `reasoning.effort=high` on every request. **Not stored on confirmatory events** (3071 events). | `<REPO>` |
| M-07 | Luna temperature | Omitted at API (reasoning models reject temperature when `reasoning.effort` is set). Episode field remains `0.0`. | `deviation_table.md`; `decisions_model2.md` |
| M-08 | Luna caching / client | `prompt_cache_options` mode=`explicit`, ttl=`30m`. `previous_response_id` chaining `store=true`. `max_output_tokens=128000`. | `MORNING.md`; `decisions_model2.md` |
| M-09 | Luna freeze tags | Spreadsheet `freeze-spreadsheet-v0.4.1-m2` = `<commit>`. SWE `freeze-swe-0.3.5-m2` = `<commit>`. | `spreadsheet_report.md`; `swe_report.md` (model2) |
| M-10 | Mix-model rule | `analyze.py` refuses mixed model strings in one estimate. | model2 `spreadsheet_report.md` |

---

## Budgets

| ID | fact | exact value | source path |
|---|---|---|---|
| B-01 | Pilot / cap rule (SWE Sonnet freeze) | Caps from `usage_for_caps_v6.csv`, 1.5× max non-truncated, rounded, never below observed max, 0 counterfactual truncations. Worker 74→115 turns, 73→110 tools, 23653→36000 output. Parent 65→100, 64→100, 27252→41000. Wall override 1800s safety (computed 720/840 unused as budget). per-call max_tokens 8192. Spreadsheet `budgets:` / model / temperature unchanged. | `config/study/decisions.md` 2026-09-04T04:52:00-04:00; `data/dev/method_source_pack/swebench/frozen_swebench_caps.yaml` |
| B-02 | Spreadsheet v0.3 budgets | turns 25/25; tools 60/60; output 64000/64000; wall 1200/1200; max_tokens 8192; input safety 1,500,000 (non-budget emergency ceiling) | `data/dev/method_source_pack/protocols.md` |
| B-03 | Spreadsheet v0.4.1 / current manifest | turns 40/40; tools 100/100; wall 1200; max_tokens 8192; output 64000; input safety 1,500,000 | `config/manifest.yaml` |
| B-04 | Solo = sum | Sum `max_turns`, `max_tool_calls`, `max_output_tokens_total`, `max_wall_seconds`. Do **not** sum `max_tokens` or `max_input_tokens_safety`. v0.4.1 Solo: 80 / 200 / 128000 / 2400 / 8192 / 1,500,000 | `src/delegation_study/spreadsheetbench/run.py` `_compute_solo_budget` |
| B-05 | Safety ceilings ≠ scientific budget | `max_input_tokens_safety` 1,500,000 is a non-budget emergency ceiling. SWE wall 1800s is a safety override. | `config/manifest.yaml` comments; `protocols.md`; `decisions.md` 2026-09-04T04:52 |
| B-06 | SWE Sonnet frozen caps | worker 115/110/1800/8192/36000; parent 100/100/1800/8192/41000 | `config/manifest.yaml` `swebench.caps` |
| B-07 | SWE Solo budget | **MISSING as a launched confirmatory budget.** Solo-8 was pre-registered as secondary and not launched. | `decisions.md` 2026-09-04T17:59; `run.py` `_ALL_CONDITIONS` |
| B-08 | Luna Spreadsheet binding | 9 turns / 14 tools; output safety 77550 worker / 24760 parent vs Sonnet 64000; wall kept 1200 | model2 `spreadsheet_report.md`; `deviation_table.md`; `decisions_model2.md` |
| B-09 | Luna SWE binding | worker 92/108 / 138590 / 5290; parent 83/123 / 166320 / 3328. Observation 80/160 interpolated into SWE **pilot** prompts; hash guard compared builders at frozen 115/100 | `deviation_table.md`; `swe_report.md` |
| B-10 | Luna cap rule | Same by-role 1.5× max on 12 non-truncated pilot episodes; identical across cells; output-token total = 10× observed max (safety); wall 1200 retained so wall does not bind | `decisions_model2.md` 2026-09-05T04:06:32 Phase 1 1d |

---

## Task selection

| ID | fact | exact value | source path |
|---|---|---|---|
| T-01 | Spreadsheet seed / strata | seed `20260902`; strata `instruction_type × single/multi-sheet` | `config/task_selection_v03/ss_task_selection.json` |
| T-02 | Spreadsheet eligible / pool | 357/400. cell/multi 44, cell/single 208, sheet/multi 40, sheet/single 65 | same |
| T-03 | Spreadsheet SHA | `cf818c1c9be7568223245e89a705d94d64aa7d99e4341604414378571f11752d` | same; `data/dev/method_source_pack/selection.md` |
| T-04 | Spreadsheet Rules 1–10 | Full list and rejection counts in `SELECTION.md`. R10 ≥5 answer cells, post-draw like-for-like. | `selection.md`; `appendix_B_spreadsheet.md` |
| T-05 | Spreadsheet pilot 3 | `55965` cell/multi; `469-9` sheet/single; `48643` cell/single | `ss_task_selection.json` |
| T-06 | Spreadsheet frozen main 20 | `57232`, `42216`, `567-21`, `599-9`, `46121`, `37229`, `408-5`, `267-21`, `54474`, `6239`, `416-27`, `388-47`, `44296`, `58723`, `97-36`, `534-26`, `44266`, `44389`, `402-43`, `341-14` | `ss_task_selection.json` |
| T-07 | Spreadsheet reserve after draw | 31 IDs starting `91-34` … `9391` | `selection.md`; JSON `reserve_in_order` |
| T-08 | Study extension to n=25 | First five unused reserve in order: `91-34`, `160-6`, `423-16`, `48975`, `42526`. Package not mutated. Power, post-Wave 2. | `config/study/decisions.md` “Main-task extension to n=25” |
| T-09 | Frozen draw was 20 not 25 | Frozen package main size is 20. n=25 is study-owned. | `ss_task_selection.json` `main`; `decisions.md` |
| T-10 | R10 swaps | 333-29→599-9; 48608→37229; 8942→44296; 59358→44389 (all same-stratum) | JSON `amendments`; `selection.md` |
| T-11 | Burns | Spreadsheet `79-7`, `343-20`; SWE `django__django-14752` | `config/study/BURNED.md` (cited in `selection.md`); JSON `dev_burned_excluded_post_draw` |
| T-12 | Hard list / trigger | 10 sealed IDs (see `SELECTION.md`). Trigger: pooled Wave-1 full_task_success > 0.85. Evaluated false at 0.70. Post-hoc probe: first five under v0.3 only; never rerun under v0.4; never pooled. | `ss_task_selection.json` `hard_extension`; `decisions.md` |
| T-13 | SWE v0.3 R1–R9 | 500→145 eligible; seed 20260902; SHA `92c7e033b0dd127463aa75b6e83fbd2900bbba8b1513d0c05bbda65d28f68451`. Full rules in `SELECTION.md`. | `config/task_selection_v03/appendix_A_swe.md`; `swe_task_selection.json` |
| T-14 | A-2 amendment | 8 main + 7 reserve; SHA `087855dc153547ea3543fc293c8491a5a5789d926cedff6ca468eab962f0c225`. Tightened: `<15 min` only; max files 2; max src lines 40; repo share 0.25. Eligible 42; draw pool 39. | `config/study/swe_selection_v2/AMENDMENT_A2.md` |
| T-15 | A-2 reversal / swe-0.3.5 | R1–R9 at v0.3 thresholds; A-2 tightening not applied; matplotlib excluded; previously exposed IDs excluded; seed `20260905`; 8 main + 8 reserve; SHA `e426590d9e9342af60ab48c5e3ddb174a73fbdfb05de5fff7ffb7d348752f0be`; eligible 117; pilot = first 3 reserve | `config/task_selection_swe_v3/swe_task_selection_v3.json`; `decisions.md` 2026-09-04T17:59 |
| T-16 | matplotlib exclusion | Infrastructure: 3/3 harness timeouts on A-2 confirmatory cells (recoded EF). Repo `matplotlib/matplotlib` excluded from v3. | `decisions.md` 2026-09-04T10:45 and 17:59; v3 JSON `exclusions.repos` |
| T-17 | Exposure exclusions | v3 `already_used_ids` count 20 (pilots, A-2 mains/reserves, burned, original v0.3 draws as listed in JSON) | `swe_task_selection_v3.json` |
| T-18 | v3 protocol string vs code | JSON `"protocol": "swe-0.4"`; runner `PROTOCOL_VERSION = "swe-0.3.5"`. Code wins. | JSON; `src/delegation_study/swebench/run.py` |

---

## Evaluators

| ID | fact | exact value | source path |
|---|---|---|---|
| E-01 | Spreadsheet evaluator version | `evaluator.py` identical blob v0.3 and v0.4.1. SHA-256 `1f4fb7225b02b819e3e55d8a260700414aa8db06b8f4233d54d492b513268f64`. Git blob `<commit>` at v0.4.1 freeze. | `data/dev/method_source_pack/evaluator.md` |
| E-02 | 46/46 cross-validation | Isolated upstream `compare_workbooks` agreement **46/46**. Also 23/23 golden-self, 23/23 init≠golden, 23/23 JSON equality. | `evaluator.md` |
| E-03 | LibreOffice pin | `26.8.0.3` (`<commit>`). Manifest executable `/Applications/LibreOffice.app/Contents/MacOS/soffice`. | `config/manifest.yaml`; `evaluator.md` |
| E-04 | Scoring rule | Numbers to 2 decimals; full success = all units. | `evaluator.md` |
| E-05 | SWE Gate 0 original | **PASS** on `django__django-12155`; gold / empty / gold-repeat; `swebench==5.0.2`. | `data/dev/method_source_pack/swebench/gate0.md` |
| E-06 | SWE Gate 0 v3 | **11/11 PASS**; wall 1800 s per issue; no reserve applied. Pilot 3 + main 8. | `data/dev/method_source_pack/swebench/gate0_v3.md` |
| E-07 | SWE evaluator timeout | 1800 s; one retry then `evaluator_failure`. | `src/delegation_study/swebench/evaluator.py` `HARNESS_EVAL_TIMEOUT_SECONDS` |
| E-08 | Harness timeout recode | Three A-2 cells `tests_timed_out` recoded EF (missing, not ITT 0): matplotlib-24149/A1 `<commit>`; matplotlib-25332/A1 `<commit>`; matplotlib-25332/P1 `<commit>`. | `config/study/decisions.md` 2026-09-04T10:45 |

---

## Missingness and ITT

| ID | fact | exact value | source path |
|---|---|---|---|
| N-01 | Spreadsheet ITT rule | Parent not finalized kept; success = 0. Budget censoring kept in primary. Cell flagged only if ≥2 censored parents. 57232 A0 primary ITT 0 (agent overflow); sensitivity missing. | `config/study/v04_package/analysis_contract_spreadsheet.md` §8; `process.py` docstring; `decisions.md` D-v0.4.1-gate |
| N-02 | 57232 A0 | `80bdf704-cec3-4669-96f4-ffb134c13eec`; 225,243 > 200,000; no `episode.json`; events present (110 events; 12 worker + 22 parent calls then overflow). ITT 0. | `data/dev/method_source_pack/main_v041_report.md` |
| N-03 | Spreadsheet v0.4.1 main not-finalized | `44266` A0 `27544c03-f049-4fb4-b898-e3e2bf6b5700` parent max_turns=40; `54474` A1 `81c565fc-4676-457c-a1ff-2885eddc1fb6` parent max_turns=40. Retained, success 0. | `main_v041_report.md` |
| N-04 | Spreadsheet v0.4.1 pilot not-finalized | `55965` A1 `a15eb550-5ab4-41a6-a9f8-a745df4ca862` parent max_turns=40. | `decisions.md` D-v0.4.1-gate; `compliance_summary.md` |
| N-05 | Spreadsheet SOLO Sonnet | 25/25 records. 42216 `db26c597-…` not_finalized (`max_tokens`); ITT 0. 57232 finalized success 0; 534-26 success 0. Success 22/25. | `data/dev/method_source_pack/solo_v041_report.md` |
| N-06 | SWE A-2 / 0.3.3–0.4 missingness | 28 measured + 3 EF among those + 4 handwritten infra = 32 IDs. Handwritten ITT-0 (later swe-0.3.5 recodes IF/EF missing): sympy-24562/P0 `<commit>`; matplotlib-24149 A0 `<commit>`, P0 `<commit>`, P1 `<commit>`. matplotlib-24149 `task_unevaluable`. | `config/study/decisions.md` 2026-09-04T17:20 and 10:20; `RECONCILIATION.md` 14 |
| N-07 | SWE swe-0.3.5 Sonnet ITT | 32/32 complete; 0 handwritten; 0 replacements. Missing ≠ 0. sympy-16766/A0 `2dd75e38-0260-4d27-909f-773f1f1323f6` not_finalized → ITT 0 (measured). | `main_report_v035.md` |
| N-08 | SWE v035 QC-flagged agent cells | sphinx-8593 A0 `<commit>`; sympy-12419 A0 `<commit>`; sympy-16766 A0 `<commit>`; sympy-16766 P0 `<commit>`; scikit-learn-14087 P0 `<commit>`; astropy-8872 A0 `<commit>`. | `main_report_v035.md` |
| N-09 | Luna Spreadsheet missingness | 100/100 main + 25/25 solo complete. QC retained: parent_not_finalized 5; production_missing_artifact/memo 2 (6239 P1, 44389 P1); advisory_missing_memo 2. P1 A1/A2 n=23. Remainder: 14 dirty-tree refusals then relaunch `20bbc9b8-…`. | model2 `spreadsheet_report.md` |
| N-10 | Luna SWE missingness | 32/32 complete. Pilot behavior FAIL django-13112 A1 +F executed 0; proceeded. 0 null/missing cells on main. | model2 `swe_report.md` |
| N-11 | SWE swe-0.3.5 missing coding | IF and EF missing, never 0. None→0 only for measured not_finalized / empty-patch. | `RECONCILIATION.md` 15; `process.py` `_swe_outcomes` |
| N-12 | Analysis-contract bootstrap vs later analyzer | Contract: B=5000 seed 0. Later `analyze.py` / process: B=10000 seed 20260902. Cite both. | `analysis_contract_spreadsheet.md` §5; `RECONCILIATION.md`; `process.py` `BOOTSTRAP_*` |

---

## Compliance audit

| ID | fact | exact value | source path |
|---|---|---|---|
| C-01 | Audit method | Event-backed: `tool_call` / `tool_result` by role; auto-obs key on eligible error-free run results; artifact presence from episode path/hash fields; QC from frozen `qc.py` (flags retained; only malformed records rejected). | `data/dev/method_source_pack/compliance_summary.md`; `src/delegation_study/qc.py` |
| C-02 | SS v0.4.1 pilot | 12/12 complete; 11/12 finalized; 33/33 + 46/46 auto-obs; 0 explicit ROR; one parent hash across cells. | `compliance_summary.md`; `protocols.md` |
| C-03 | SS v0.4.1 main | 99/99 schema valid; QC `parent_not_finalized` 2; auto-obs 285/285 worker, 215/215 parent; A0/P0 worker obs 0; 0 treatment violations. | `main_v041_report.md` |
| C-04 | SS v0.4.1 SOLO | 25/25 structurally valid; auto-obs 58/58; QC `parent_not_finalized` 1; 0 treatment violation. | `solo_v041_report.md`; `compliance_summary.md` |
| C-05 | SWE A-2 closed matrix | 28 measured episode records + 3 evaluator-failure cells among those + 4 handwritten infrastructure_failure = 32 canonical IDs. Handwritten rows are incomplete infra fillers, not treatment-violation scores. | `config/study/decisions.md` 2026-09-04T17:20:00-04:00 |
| C-06 | SWE v035 compliance | 32/32 structurally valid after `task_set` enum-lag filter; HEAD=base all 32; worker≠parent containers all 32; assistant text yes on all 32. | `data/dev/method_source_pack/swebench/compliance_v035.md` |
| C-07 | Luna SS compliance | 125/125 `validate_model2_episode` issues empty. | model2 `spreadsheet_report.md` |
| C-08 | Luna SWE compliance | 32/32 validate empty (schema lag filtered). QC `advisory_missing_memo` 11 retained. Pilot +F FAIL on django-13112 A1. | model2 `swe_report.md` |

---

## Sandbox isolation

| ID | fact | exact value | source path |
|---|---|---|---|
| S-01 | Spreadsheet sandbox | image `spreadsheet-exec:protocol-v01`; digest `sha256:8c861ae2b9511d19225f4f2433f4366311a32a55ad13dc29735a76c6fd165ffb`; user `nobody`; network `none`; 2 cpus; 2g; pids 128 | `config/manifest.yaml` |
| S-02 | Spreadsheet isolation check | **PASS**: user `nobody`; prohibited mounts 0; only `/input/input.xlsx`, `/output`, `/solution/solution.py`, `/tmp` | `evaluator.md` (`evaluator_validation.json` `sandbox_isolation.*`) |
| S-03 | SWE isolation | Distinct fresh network-isolated worker and parent containers; `worker_container_id ≠ parent_container_id` on all 32 v035 cells; official SWE images; docker digest in runner `_DOCKER_DIGEST` `sha256:743b7bd1…` (image pin in `run.py`) | `run.py` hard invariants; `compliance_v035.md`; `certification.md` |

---

## Reproducibility checks

| ID | fact | exact value | source path |
|---|---|---|---|
| R-01 | Spreadsheet selection checksum | Package SHA `cf818c1c…` exact-equality gate PASS | `selection.md` |
| R-02 | Spreadsheet prompt hashes on main | All 99 records: parent `0193f4a73fd2c1c8…`; A0 `ba7d362f…`; A1 `aea29069…`; P0 `76fb1043…`; P1 `321328a2…`. Parent identical across cells. | `main_v041_report.md` |
| R-03 | Spreadsheet benchmark git | All 99 `reproducibility.benchmark_git_commit` = `<commit>` | `main_v041_report.md`; `config/manifest.yaml` `benchmark.git_commit` |
| R-04 | SWE v035 provenance | 32/32 `protocol_version=swe-0.3.5`; `schema_git_commit` starts with `<commit>`; HEAD=`base_commit`; freeze tag `freeze-swe-0.3.5` | `compliance_v035.md`; `main_report_v035.md` |
| R-05 | SWE Gate 0 gold-repeat | Original Gate 0 gold1 SHA = gold2 SHA `2f570f4fa801840b81f7606085a9b04278398fc80baca138b38af6065bf3fce2`. v3: repeat identical on all 11. | `gate0.md`; `gate0_v3.md` |
| R-06 | Luna prompt hashes | SS parent `0193f4a7…` on 100 factorial cells; SOLO `cdbbeac0…`. SWE hash guard at 115/100: A `d6b3ba17…`; P `637cb71e…`; parent `c4e904ff…`. | model2 `spreadsheet_report.md`; `decisions_model2.md` |

---

## Episode counts, calls, tokens, cost per arm

| ID | fact | exact value | source path |
|---|---|---|---|
| K-01 | Sonnet SS main episodes | 99 records + 1 analysis-only ITT row (57232 A0) = 100 ITT. Success ITT: A0 20/25, A1 19/25, P0 22/25, P1 21/25. | `main_v041_report.md`; `git show <commit>:paper_outputs/v041/condition_summary.csv` |
| K-02 | Sonnet SS main parent calls | A0 266; A1 254; P0 255; P1 166 (totals). Means 10.64 / 10.16 / 10.20 / 6.64. Combined model calls W+P 1,951. | `condition_summary.csv`; `main_v041_report.md` |
| K-03 | Sonnet SS main tokens (combined 100 ITT rows) | input 9,901; output 755,890; cache create 2,681,590; cache read 19,306,549 | `main_v041_report.md`; `paper_outputs/v041/cost_summary.json` |
| K-04 | Sonnet SS main cost by cell×role (USD exact) | A0 W 1.68068145 / P 5.14503075; A1 W 5.79093075 / P 3.05762445; P0 W 2.69271435 / P 3.21652215; P1 W 4.09022535 / P 1.54225095. Combined **$27.2159802**. Rates $3/$15/$0.30/$3.75 per M. | `git show <commit>:paper_outputs/v041/cost_by_cell_role.csv`; `cost_summary.json` |
| K-05 | Sonnet SS SOLO | 25/25; success 22/25; calls 267; tools 302; out 92,702; cost **$3.7757769**; auto-obs 58/58 | `solo_v041_report.md`; `paper_outputs/v041/solo/cost_summary.json` (inventory) |
| K-06 | Sonnet SWE v035 episodes | 32/32. Success: A0 3/8, A1 4/8, P0 2/8, P1 4/8. | `main_report_v035.md` |
| K-07 | Sonnet SWE v035 parent calls (mean) | A0 61.9; A1 61.1; P0 64.5; P1 58.1. Tokens-after-handoff mean: 23669.1 / 19170.5 / 22531.8 / 17724.4 | `main_report_v035.md` |
| K-08 | Sonnet SWE v035 cost / full tokens per arm | Combined **$104.59005420** (worker $58.21211685 + parent $46.37793735). Tokens W+P: input 20,251; output 1,355,974; cache create 5,757,642; cache read 208,661,779. Role-wall 24,616.94 s. Rates $3/$15/$0.30/$3.75 per M. | `analysis/episodes_swe_v035.csv`; `COMPUTE.md` |
| K-09 | Luna SS main | 100/100. Success 22/25, 22/25, 23/25, 22/25. Cost analyze **$1.89720902**; ledger $1.742. Token totals: in 6,112,111; out 547,235; cache create 14,411; cache read 761,131. Parent calls 110/118/112/114. | model2 `spreadsheet_report.md`; `analyze_main/analysis_summary.json`; `analyze_main/cost_by_cell_role.csv` |
| K-10 | Luna SS main tokens by cell×role | A0 W in 425490 out 92294; A0 P in 823692 out 53884 cache_read 104720; A1 W in 549707 out 105786 cache_read 74408 cache_create 1048; A1 P in 736317 out 45556 cache_read 110880 cache_create 3696; P0 W in 962195 out 83972 cache_read 100298; P0 P in 663604 out 44242 cache_read 104720 cache_create 2464; P1 W in 1238954 out 79658 cache_read 160153 cache_create 3507; P1 P in 712152 out 41843 cache_read 105952 cache_create 3696. CSV `cost_usd_exact` column applies $3/$15-style rates (sum ≠ $1.897) and is **not** the Luna list total. Per-arm Luna-rate USD as a committed consistent column: **MISSING** — use `analysis_summary.json` total $1.89720902 with formula `(uncached*0.20 + cached*0.02 + output*1.20)/1e6`. | `<REPO>`; `analyze_main/analysis_summary.json` |
| K-11 | Luna SS SOLO | 25/25; success 22/25; analyze **$0.36121552**; ledger $0.334; parent calls 152; tools 231; out 71,488 | `analyze_solo/analysis_summary.json` |
| K-12 | Luna SWE | 32/32. Success 5/8, 6/8, 6/8, 6/8. Cost **$14.91523740**. Role-wall 16,007.90 s. Per-arm token table: **MISSING** in opened SWE report (parent means only: calls 35.88/31.50/28.50/22.12). | `analysis/episodes_model2_swe_v035.csv`; `COMPUTE.md`; model2 `swe_report.md` |

---

## Instrumentation limits

| ID | fact | exact value | source path |
|---|---|---|---|
| L-01 | Spreadsheet assistant text | v0.4.1 public events: `model_call` token counts only — **assistant text not persisted** (`model_call_text_dir=None`). | `src/delegation_study/agent_loop.py` (~699); `src/delegation_study/analysis/coverage.py` (~1316) |
| L-02 | SWE assistant text | Persist `assistant_text` (≤4096 UTF-8) or `raw_payload_path`. Empty string is recorded. All 32 v035 cells: yes. | `agent_loop.py`; `compliance_v035.md` |
| L-03 | Luna `reasoning_tokens` | Confirmatory events **estimated**, not API-confirmed. confirmed-from-API **0** / estimated **3071** / missing-estimate **0**. | model2 `deviation_table.md` |
| L-04 | 500-char summary truncation | `tool_result.summary` capped at 500 chars; `executed` / `feedback_command` can fall off. `compliance.worker_feedback_calls` is SoT. | `config/study/RECONCILIATION.md` 13 |
| L-05 | SWE `accepted_unchanged` flag | Hardcoded `false` in runner. Analysis uses hashes. | `RECONCILIATION.md` 12 |
| L-06 | Prompt hashes not an episode field | Appendix A.7 listed prompt hashes as persisted; harness does not emit that field. Hashes are source-derived / report-side. | `RECONCILIATION.md` item 5 |
