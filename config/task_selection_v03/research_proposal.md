# Production vs. Advisory Delegation in LLM Agent Teams: Does Verifiability Decide When Building Beats Advising?

**Target venue:** Agents in the Wild Workshop, NeurIPS 2026 — short paper track (4 pages)
**Submission deadline:** September 5, 2026 (AoE)
**Authors:** co-author A [CHECK surname], co-author B [CHECK surname]
**Status:** Protocol v0.1 — design frozen 2026-09-02 (four-cell 2×2 on every task; supersedes the three-condition + side-ablation version), pilot phase

---

## 1. Motivation

Multi-agent LLM systems increasingly split work between a coordinating "parent" agent and one or more "worker" subagents. Practitioners face a basic design choice: should a worker *advise* the parent (diagnose, plan, recommend) or *produce* a candidate artifact (a patch, a script, an allocation) that the parent then reviews and finalizes?

Production delegation intuitively saves the parent work. But a candidate artifact can be confidently wrong, and reviewing it may cost as much as building from scratch. We hypothesize that the value of production delegation depends on whether the worker can verify its own candidate before handoff: when validation is cheap and immediate, production should dominate; when quality is only revealed by delayed or noisy outcomes, the advantage should weaken or reverse.

No existing work isolates production responsibility as a treatment while holding models, information, tools, budget, and final-submission authority fixed. This paper does.

## 2. Research question

> Under matched inference budgets, when does delegating *production* of a candidate artifact to an LLM subagent improve final task performance and reduce parent-agent rework, relative to *advisory* delegation or solo completion — and does that advantage weaken when candidate quality cannot be verified before finalization?

## 3. Hypotheses

- **H1 (Production effect).** Within each environment, Production delegation yields higher final task success than Advisory delegation at equal budget.
- **H2 (Rework).** Production delegation reduces parent post-handoff effort (tokens, tool calls, validation cycles) relative to Advisory.
- **H3 (Verifiability moderates).** The Production–Advisory gap is larger when the worker has pre-handoff verifier access (+F) than when feedback is withheld (−F). Formally, the interaction δ > 0. This is tested on every task, not on a side subset.
- **H4 (Boundary case, exploratory).** In a delayed-feedback environment (portfolio construction), the Production advantage is smaller or absent.

## 4. Experimental design

### 4.1 Treatment structure

A 2 × 2 factorial run on **every task in both core environments**, with a secondary baseline. (An earlier draft treated the feedback factor as a small side ablation; that version is superseded by the mechanism-study document.)

| | **+F: worker has verifier access** | **−F: feedback withheld until handoff** |
|---|---|---|
| **A: Advisory** | A+F | A−F |
| **P: Production** | P+F | P−F |

**Solo** (no worker; parent does everything) is a secondary baseline, not part of the primary contrast.

The primary comparison is **Production vs. Advisory**, estimated within each feedback level. The headline mechanism estimate is the interaction:

δ = (Y_{P+F} − Y_{A+F}) − (Y_{P−F} − Y_{A−F})

### 4.2 Conditions

| Condition | Worker output | Who produces the final artifact |
|---|---|---|
| Solo | none | Parent |
| Advisory | structured memo (task understanding, target locations, approach, assumptions, observed feedback, risks, validation plan). **No executable artifact.** | Parent |
| Production | the same structured memo **plus** the candidate artifact | Worker drafts; parent reviews, edits, and submits |

The memo schema is identical in Advisory and Production, so the handoff differs only by whether an executable artifact crosses it. That is the enforceable treatment.

### 4.3 Invariants (held fixed across every cell)

- Same task, same clean starting state, same evaluator.
- Same model snapshot (`claude-sonnet-4-5-20250929`), temperature 0, and inference settings for worker and parent.
- Parent receives **only** the task and the worker's designated handoff (memo or artifact). Never the worker's trajectory, tool history, or cell label.
- Parent has the full tool set in every cell.
- **Parent always submits the final artifact.** Commit authority is never delegated. (Worker authority is explicitly a follow-up paper.)
- Hidden evaluator tests/answers are inaccessible to both agents.

### 4.4 Budget matching

Per-role stopping rules, identical across cells: `max_turns` 25, `max_tool_calls` 60, `max_output_tokens_total` 64k, `max_wall_seconds` 1,200, `max_tokens` 8,192 per call. Solo gets the sum. **Cumulative input tokens are a reported cost outcome, not a stopping rule** (the pilot showed an input cap censors the role that inspects most; Appendix B.9). Prompt caching on the conversation prefix; cached tokens reported separately. Model `claude-sonnet-4-5-20250929`, temperature 0 (#1 and #2, set from the pilot 2026-09-03).

### 4.5 Feedback manipulation (+F / −F)

The factor is **the worker's ability to execute code against the task environment** — a discrete capability, not a degree of feedback.
+F: the worker has an execution sandbox (SWE: run the repository's existing tests and scripts; Spreadsheet: run its transform on a scratch copy, recalculate, read back values) and may iterate before handoff.
−F: the worker has **no code execution of any kind**. In SWE it reads the repository; in Spreadsheet it receives a static textual snapshot of the workbook (values and formulas, first *N* rows per sheet). It reasons and produces blind.
The parent has the sandbox in every cell. Exact definitions per environment: Appendix A.8, Appendix B.9.

## 5. Environments

Three environments chosen to span pre-finalization verifiability.

| Environment | Role | Production artifact | Verifiability profile |
|---|---|---|---|
| **SpreadsheetBench Verified** | Primary anchor | `transform.py` exposing `run(input_path, output_path)` | High; short feedback |
| **SWE-bench Verified** (audited subset) | Confirmatory, small arm | `candidate.patch` + test log | Medium–high; short–medium feedback |
| **StockBench-derived snapshot tasks** | Exploratory boundary case | allocation JSON `{ticker: weight}` | Low; delayed, noisy feedback |

**SWE split:** SWE-bench Verified, selected by the pre-registered filter in Appendix A. Verified is contaminated; the paired design absorbs this across conditions, but a solo parent recalling a known patch is a ceiling risk, so scores are presented as organizational comparisons, not capability evidence. The `hints_text` field (issue comment thread) is never shown to any agent.

**StockBench adaptation:** not the full sequential loop. Each task is one decision date; agents see only information through *t*; output one target portfolio; evaluate over a predeclared forward horizon (e.g., 10 trading days) against an equal-weight benchmark. Labeled "StockBench-derived," not canonical.

## 6. Outcomes

**Primary (per environment)**
- Spreadsheet: output workbook matches golden at `answer_position` after recalculation (binary). Verified has one input workbook per task, so this measures correctness on one file, not transformation generality (see hard-coding flag below and Appendix B.10). **Pre-registered ceiling contingency (Appendix B.4a):** if pooled success on main wave 1 exceeds 0.85, the confirmatory Spreadsheet analysis is on parent post-handoff effort (H2) with success reported descriptively, and a sealed 10-task hard extension runs and is reported separately.
- SWE: all issue and regression tests pass (binary).
- Stock: excess return over equal-weight benchmark (continuous).

**Secondary**
- Partial credit (fraction of test cases / target cells / issue tests).
- Raw worker artifact score *Y_worker* (Production only) and parent contribution Δ_parent = Y_final − Y_worker.
- Parent rework: post-handoff tokens, calls, tool calls, validation cycles, elapsed time, artifact edit distance (script diff / lines changed / L1 weight distance), accept-without-change rate.
- Resource use: tokens, cost, latency, infrastructure failures.
- Treatment compliance flags (advisory worker returned no executable; production artifact parsed; parent — not worker — submitted; clean start state; evaluator inaccessible; −F worker executed no code). Reported, not discarded.
- Spreadsheet only: **hard-coding flag** (transform output unchanged under a seeded perturbation of the input data, on data-dependent tasks) and **unintended-modification count** (cells outside `answer_position` that differ from init). Definitions in Appendix B.9.

## 7. Task selection and sample size

Tasks are selected by an **operational filter written before any inference**, applied mechanically by seeded stratified draw; full rules, thresholds, rationale, IDs, validation steps, and adapter contracts are in **Appendix A (SWE)** and **Appendix B (Spreadsheet)**. Pilot tasks are permanently excluded from the main set. Reserve lists are ordered; a task failing infrastructure validation is replaced by the next reserve ID (stratum not preserved), and the main set may be extended from reserve if ahead of schedule but never reduced after results are seen.

| Environment | Pilot | Main | Reserve | Status |
|---|---|---|---|---|
| SpreadsheetBench Verified | 3 | 20 (5 per stratum) + sealed hard extension of 10 (conditional) | 31 | Pilot complete (Appendix B.12); four pre-freeze fixes in progress; freeze on pilot rerun |
| SWE-bench Verified | 3 | 8 | 8 | IDs drawn (145 eligible); awaiting infra validation |
| StockBench-derived | 3 dates | 8–12 dates | — | deferred |

Stratification: Spreadsheet by task level (cell/sheet) × sheet count (single/multi), equal allocation. With five tasks per stratum no per-stratum estimates are made; the paper reports both the design-weighted (primary) and a benchmark-weighted (post-stratified) Spreadsheet aggregate (Appendix B.4). SWE by repository × difficulty label, ≤30% per repo. One replicate per cell; unique tasks preferred over replicates. Main matrix: (20 + 8) × 4 cells = 112 core episodes, plus (3 + 3) × 4 = 24 pilot episodes, plus Solo on a subset [CHECK].

## 8. Analysis plan

- **Estimand:** τ_e = E[Y_e(Production) − Y_e(Advisory)] per environment; Solo contrasts secondary.
- **Binary outcomes:** paired risk difference; task-clustered paired bootstrap CI; McNemar as supplementary.
- **Partial scores:** paired mean difference; task-clustered bootstrap.
- **Stock:** paired excess returns on identical dates; mean and median; block bootstrap if horizons overlap; equity curves descriptive only.
- **Mechanism:** report δ with task-clustered bootstrap CI, estimated on the full task set in each environment. If the −F cells cannot be completed for some tasks, δ is reported on the completed subset with the shortfall stated; the paper does not claim a causal mechanism from cross-environment comparison alone.
- **Presentation:** forest plot of three environment-specific effects. No pooled "overall accuracy." Spreadsheet shows design-weighted and benchmark-weighted estimates side by side.
- The task is the unit of analysis; individual tests or cells are never treated as independent.

## 9. Infrastructure

One thin runner with three adapters:

```
run(task, role, feedback, replicate):      # role ∈ {ADVISORY, PRODUCTION, SOLO}; feedback ∈ {+F, −F}
    reset_environment(); load_task()
    worker_tools = manifest[role][feedback]  # −F removes test/recalc tools from the worker only
    SOLO:        final = run_solo_parent()
    ADVISORY:    memo = run_worker_adviser(worker_tools); final = run_parent(task, memo)
    PRODUCTION:  cand = run_worker_producer(worker_tools); raw = evaluate(cand)
                 final = run_parent(task, cand)
    final_score = evaluate(final); write_log(role, feedback, ...)
```

The parent's tool manifest is identical in every cell; only `worker_tools` varies.

Each adapter implements `load_task`, `create_isolated_workspace`, `serialize_worker_artifact`, `validate_artifact_format`, `evaluate_artifact`, `measure_parent_modification`, `reset`. Every run log records task ID, cell, replicate, model version, prompt hashes, tokens, calls, artifacts, raw/final scores, parent modifications, runtime, failures, and compliance flags. SWE runs on x86 cloud infrastructure.

## 10. Go / no-go

- **Spreadsheet:** reference solutions evaluate; recalculation works; ≥90% of pilot runs scorable; A/P prompts behaviorally distinct.
- **SWE:** gold patches evaluate; clean resets work; infra failure <10%; candidate and final patches evaluable separately; acceptable median runtime.
- **Stock:** point-in-time cutoff verified; allocations parse; constraints deterministic; backtest reproducible; ≥6 dates. If not working by end of pilot day, demote to a labeled case study.

## 11. Schedule

| Date | Work |
|---|---|
| Sep 1 | Protocol v0.1; freeze treatments and schemas; start SWE x86 env; validate evaluators |
| Sep 2 | Run pilots; inspect every trace; freeze model, budgets, prompts, task rules, Stock horizon, SWE split; begin main Spreadsheet runs |
| Sep 3 | Full Spreadsheet and SWE matrices; Stock snapshots in parallel; automated analysis; no redesign |
| Sep 4 | Reruns; figures; write paper |
| Sep 5 | Corruption reruns, stats checks, anonymization, compression, submit |

## 12. Scope limits (do-not-add list)

Single model family. Single budget split. One replicate per cell. No dynamic delegation selection. No full sequential StockBench. No worker commit authority. No additional environments or mechanism ablations.

## 13. Expected contribution

A clean within-task causal estimate of production vs. advisory delegation across three environments, plus a small identified test of whether pre-handoff verifiability moderates the effect. The anticipated story: production delegation helps when subagent artifacts are modular and cheaply testable, and becomes unpredictable when quality is revealed only through delayed, noisy outcomes.

## 14. Open items

| # | Item | Owner | Status |
|---|---|---|---|
| 1 | Model string, provider, temperature, thinking setting | co-author B | `claude-sonnet-4-5-20250929`, temp 0 — recorded |
| 2 | Budget: turns, tool calls, output tokens, wall-clock; input tokens not a stopping rule | set from pilot | recorded (Appendix B.9) |
| 3 | Task ID lists + selection filter (pilot / main / reserve) | co-author A | Done for both environments (Appendices A, B); awaiting co-author B's infra validation |
| 4 | Worker tool manifest per cell | co-author B | done (`tool_manifests.yaml`; pilot-verified) |
| 5 | Advisory compliance | co-author B | enforced structurally: `artifact_crossed_handoff` from the sealed handoff |
| 6 | Parent tool manifest | co-author B | done; identical across cells (pilot-verified) |
| 7 | Parent input invariant | co-author A | written (§4.3) |
| 8 | Artifact definitions | co-author A | written (§5) |
| 9 | Evaluator command, isolation, validation (incl. `hints_text` exclusion) | co-author B | pending |
