# Appendix A — SWE-bench Verified: Task Selection and Implementation

**Status:** selection frozen 2026-09-02; awaiting infrastructure validation (Stage 3).
**Role in the paper:** small confirmatory arm. 8 main tasks × 4 cells (A0/A1/P0/P1) = 32 core episodes, plus 3 pilot tasks.
**Files:** `select_swe_tasks.py`, `build_swe_local.py`, `swe_verified_local.json`, `swe_task_selection.json`, `swe_pilot.json`.

---

## A.1 Principle

Every selection rule is a property of the task itself — the gold patch, the FAIL_TO_PASS test list, or the issue text. No rule depends on how any agent performs, and no task is chosen by hand. The filter and draw were written and run before any agent episode. The selection is committed with a SHA-256 over `{seed, filter, pilot, main, reserve}` so it can be re-derived and verified.

Each rule serves one of three purposes:

- **(G) Gradeability** — the SWE-bench harness can produce a meaningful resolved/not-resolved verdict.
- **(T) Treatment integrity** — Advisory and Production are actually different on this task, and worker and parent see identical information.
- **(F) Feasibility** — a worker on half the inference budget could plausibly produce a candidate patch.

---

## A.2 Data source

**Canonical:** `princeton-nlp/SWE-bench_Verified`, split `test`, 500 instances.
Fields used by the filter: `instance_id`, `repo`, `patch` (gold), `FAIL_TO_PASS`, `difficulty`, `problem_statement`.

**Local copy actually used:** `swe_verified_local.json`, assembled by `build_swe_local.py` from two public GitHub mirrors:

- the 500 rows from `OpenAgentsInc/swe-bench-verified/swe_bench_data.jsonl` (a copy of the HF dataset minus the `difficulty` column);
- the `difficulty` label for each instance from the official `SWE-bench/swe-bench-tasks` repository, `tasks/<instance_id>/task.yaml`.

The joined label distribution — 194 `<15 min fix` / 261 `15 min - 1 hour` / 42 `1-4 hours` / 3 `>4 hours` — matches OpenAI's published breakdown for Verified exactly. Running `select_swe_tasks.py` without `--local` loads from Hugging Face and produces the same rows and the same draw.

**Field that must never reach an agent:** `hints_text` (the GitHub issue comment thread up to the fix PR). It is not used by the filter. It frequently contains the fix. The adapter must strip it from every prompt in every cell.

---

## A.3 Stage 1 — Metadata filter (`select_swe_tasks.py`)

Applied to all 500 instances. Any single failure excludes the instance. Rejections are attributed to the first failing rule in the order below.

| # | Rule | Threshold | Purpose | Rationale | Rejected |
|---|---|---|---|---|---|
| 1 | Gold patch touches at least one non-test source file | — | T | Test-only fixes leave nothing to produce; A and P collapse. Test files are identified by path (`/test`, `b/test…`, `tests/`, `_test.py`, `/testing/`, `conftest`); checked against the 500 gold patches — no source directory is misclassified. | 0 |
| 2 | Gold patch file count | ≤ 5 | F | Multi-file refactors are unlikely to complete on B/2 and produce floor effects. | 2 |
| 3 | Gold patch total changed lines (added + deleted, all files) | ≤ 150 | F | Same. | 3 |
| 4 | Gold patch changed lines in non-test files | ≥ 5 | T | A one- or two-line fix can be fully specified in an advisory memo ("change `x` to `x or 0`"), making the memo indistinguishable from the patch. The treatment requires that producing the artifact is non-trivially more than describing it. | 191 |
| 5 | `FAIL_TO_PASS` count | 1 – 15 | G, F | Zero means nothing goes fail→pass (nothing to grade). More than 15 usually indicates a sweeping behavioural change rather than a bug fix. | 5 |
| 6 | `difficulty` label | `<15 min fix` or `15 min - 1 hour` | F | Human annotations shipped with Verified. `1-4 hours` and `>4 hours` excluded. | 38 |
| 7 | `problem_statement` contains no URL | regex `https?://` | T | Worker and parent must have identical information; linked content cannot be guaranteed identical or available. Conservative: excluded even when the link is incidental. | 113 |
| 8 | `problem_statement` contains no image | markdown image syntax or `<img` | T | Same. | 0 |
| 9 | `problem_statement` length | ≥ 200 characters | T | Very short issues are typically not self-contained. | 3 |

**Total: 500 → 145 eligible.**

Two rules do most of the work. Rule 4 removes 191 tasks whose gold fix changes fewer than five source lines — consistent with Verified's known skew toward trivial fixes (the `<15 min` bucket averages about five changed lines). Rule 7 removes 113 tasks whose issue contains a URL, most often a link to a code line or docs page. Both are treatment-integrity rules and are expected to bite; they are reported so the pool is not mistaken for a representative sample of Verified.

---

## A.4 Stage 2 — Stratified seeded draw

1. Sort the eligible pool by `instance_id` (removes dependence on download order).
2. Bucket into strata by `(repo, difficulty)`.
3. Within each stratum, sort by `instance_id`, then shuffle with `random.Random(20260902)`.
4. Shuffle the stratum order with the same generator.
5. Deal round-robin: one instance from each stratum in turn, skipping any stratum whose repo has already reached 30% of the draw size (for n = 8 this is 2 per repo).
6. Continue until the target size is reached or the pool is exhausted.

Draws are taken in order, without replacement: pilot, then main, then reserve.

| Set | Size | IDs |
|---|---|---|
| Pilot | 3 | `django__django-12155`, `scikit-learn__scikit-learn-14983`, `matplotlib__matplotlib-25479` |
| Main | 8 | `mwaskom__seaborn-3069`, `pydata__xarray-3095`, `sympy__sympy-19954`, `scikit-learn__scikit-learn-13142`, `pytest-dev__pytest-10081`, `django__django-16877`, `matplotlib__matplotlib-24970`, `pylint-dev__pylint-6528` |
| Reserve (in order) | 8 | `astropy__astropy-7606`, `pytest-dev__pytest-7490`, `scikit-learn__scikit-learn-12682`, `sympy__sympy-24562`, `matplotlib__matplotlib-20676`, `django__django-14792`, `matplotlib__matplotlib-25775`, `sphinx-doc__sphinx-8593` |

**Selection SHA-256:** `92c7e033b0dd127463aa75b6e83fbd2900bbba8b1513d0c05bbda65d28f68451`

Main-set composition: 8 distinct repositories, one task each; difficulty 3 `<15 min` / 5 `15 min–1 hr`. Because the draw is round-robin over `(repo, difficulty)` strata, repositories are represented roughly equally rather than in proportion to Verified (where Django is 46%). For a small confirmatory arm this maximises repository diversity and is stated in the paper.

Pilot is for infrastructure and evaluator shakedown only, is permanently excluded from main, and supports no scientific conclusions.

---

## A.5 Threshold sensitivity (run 2026-09-02)

Each threshold varied independently with the others frozen. Eligible pool out of 500:

| Threshold | Values tried | Pool sizes |
|---|---|---|
| `min_src_lines` | 1 / 3 / **5** / 8 / 12 | 242 / 183 / **145** / 101 / 60 |
| `max_lines` | 80 / **150** / 300 | 143 / **145** / 145 |
| `max_files` | 3 / **5** / 8 | 143 / **145** / 145 |
| `max_fail_to_pass` | 5 / **15** / 50 | 141 / **145** / 147 |
| `allowed_difficulty` | easy only / **easy+medium** / +hard | 42 / **145** / 165 |
| link rule | **on** / off | **145** / 241 |

`min_src_lines` and the link rule are load-bearing; all other thresholds change the pool by ≤ 4. The pool of 145 is ample for the 19 IDs drawn. Any change to any threshold reshuffles the seeded draw and yields a different set; thresholds are therefore frozen rather than tuned.

---

## A.6 Stage 3 — Infrastructure validation (co-author B)

Run on the x86 evaluation host with Docker. For each pilot and main ID, **in listed order**:

| # | Check | Command | Pass condition |
|---|---|---|---|
| 10 | Container builds | `swebench images build verified -i <id>` | exit 0 |
| 11 | Gold patch resolves | `swebench eval verified --gold -i <id> --run-id val_gold` | `resolved: true` in report |
| 12 | Empty patch does not resolve | `swebench eval verified -p empty.jsonl -i <id> --run-id val_empty` where `empty.jsonl` has `{"instance_id": "<id>", "model_name_or_path": "empty", "model_patch": ""}` | `resolved: false` |
| 13 | Gold patch resolves on a second run (determinism) | repeat 11 with `--run-id val_gold2` | `resolved: true` |
| 14 | Evaluation wall-clock | time of 11 | < 10 min `[CHECK — set from pilot]` |

(`swebench` is the CLI from the `swebench` PyPI package, v5.x. Older harness invocation: `python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified --predictions_path gold --instance_ids <id> --run_id val_gold`.)

**Replacement rule.** A task failing any check is removed and replaced by the **next unused reserve ID of the same repository, in list order**; if none remains, the next unused reserve of any repository. Every replacement is logged with the instance ID, the failing check number, and the reserve ID that took its place.

**Logging template** (`swe_validation_log.md`):

```
| instance_id | 10 build | 11 gold | 12 empty | 13 gold2 | 14 runtime | status | replaced_by |
```

---

## A.7 Freeze

When all pilot and main IDs pass Stage 3:

```
git add select_swe_tasks.py build_swe_local.py swe_verified_local.json \
        swe_task_selection.json swe_pilot.json swe_validation_log.md
git commit -m "freeze SWE task selection (sha <commit>)"
```

This must happen **before the first main episode**. After the freeze:

- the main set is never reduced after results are observed;
- it may be extended by drawing further reserve IDs in order if the schedule permits;
- any change to `--n-main` before main runs is logged with date and reason.

---

## A.8 Implementation contract for the SWE adapter

This section states what the runner must guarantee for the SWE environment so that the selection above yields a valid experiment. Items marked `[CHECK]` are owned by co-author B and pasted in from the tool manifests when fixed.

### Task loading

- Start every episode from the SWE-bench Docker image for the instance (`base_commit` checked out). No state carries between episodes or cells.
- The agent-visible task is `problem_statement` only. **`hints_text`, `patch`, `test_patch`, `FAIL_TO_PASS`, and `PASS_TO_PASS` are never shown to any agent.**
- `test_patch` is not applied to the agent's workspace. FAIL_TO_PASS tests are typically added by the gold PR and therefore do not exist in the repo at `base_commit`; the harness applies `test_patch` only at evaluation time, in its own container.

### Worker cells

| Cell | Read repo | Run existing tests | Write files | Returns |
|---|---|---|---|---|
| A0 (Advisory, −F) | yes | **no execution** | no | memo |
| A1 (Advisory, +F) | yes | yes | no | memo |
| P0 (Production, −F) | yes | **no execution** | yes, in isolated worktree | `candidate.patch` |
| P1 (Production, +F) | yes | yes | yes, in isolated worktree | `candidate.patch` + `worker_test_log.txt` |

Exact tool allow-lists per cell: `[CHECK — co-author B, #4]`. The feedback factor is **the ability to execute code**: in +F cells "run existing tests" means `pytest` (or the repo's runner) on tests already present at `base_commit`, plus running scripts/REPL for diagnosis; it never means the hidden FAIL_TO_PASS set. In −F cells the worker has **no code execution of any kind** — read-only access to the repository (file viewing, search), and in P0 write access to its worktree — so a −F worker cannot verify anything it writes. This mirrors the Spreadsheet definition in Appendix B.9.

### Artifacts

- **Advisory memo** (A0/A1): free text. Must not contain a unified diff or a complete replacement function body. Compliance check: `[CHECK — co-author B, #5]`; recommended automatic flags are (a) any line starting with `diff --git`, `---`, `+++`, or `@@`, (b) a fenced code block longer than N lines `[CHECK N]`.
- **Production artifact** (P0/P1): `candidate.patch`, a unified diff against `base_commit` covering non-test files only, produced from an isolated worktree. In P1, `worker_test_log.txt` is preserved alongside it but is **not** shown to the parent unless the parent manifest says so `[CHECK — #6]`.

### Parent (identical in every cell)

- Receives `problem_statement` plus the designated handoff only — the memo, or `candidate.patch`. Never the worker trajectory, tool history, or cell label.
- Starts from the canonical clean repo at `base_commit`; in P cells the candidate patch is provided as a file, not pre-applied, so the parent's decision to apply, edit, or discard it is observable.
- Has the full tool set `[CHECK — co-author B, #6]` in every cell, including running existing tests.
- Produces `final.patch` and submits it. The parent always submits.

### Evaluation

- Score `final.patch` with the official harness: `swebench eval verified -p preds.jsonl -i <id> --run-id <cell>_<replicate>`.
- In P cells, also score `candidate.patch` on its own (raw worker score, *Y_worker*) with a separate `--run-id`.
- Primary outcome: `resolved` (all FAIL_TO_PASS and PASS_TO_PASS pass). Secondary: fraction of FAIL_TO_PASS passed; PASS_TO_PASS retained; `Δ_parent = Y_final − Y_worker`.

### Per-episode log

`instance_id`, cell, replicate, model string, prompt hashes, worker/parent tokens and calls, post-handoff parent tool calls and test invocations, paths to `candidate.patch` and `final.patch`, files and lines changed by the parent relative to the candidate (`git diff --stat`), accept-without-change flag, raw and final harness reports, wall-clock, API and infrastructure failures, compliance flags (memo contained diff; candidate parsed; parent submitted; clean start verified; `hints_text` absent from prompts).

---

## A.9 Known limitation

SWE-bench Verified is present in model training data. The within-task paired design controls for this across cells (all four share any memorisation advantage), but a parent recalling a known patch is a ceiling risk for absolute scores. Scores are reported as organisational comparisons, not capability estimates. There is no task-level filter for contamination; it is a limitations-paragraph item.
