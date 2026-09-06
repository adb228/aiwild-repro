# Appendix C — StockBench-derived Portfolio-Construction Tasks: Design and Implementation

**Status:** design frozen 2026-09-02; date selection run; harness built and self-tested on all 11 dates; **blocked on one check (C.2) before any episode**.
**Role in the paper:** exploratory boundary case for delayed, noisy feedback. 8 main dates × 4 cells = 32 episodes, plus 1 pilot date (one-night plan, C.12).
**Files:**
- `select_stock_dates.py`, `stock_date_selection.json`, `stock_pilot.json` — date selection
- `stockbench_snapshot.py` — bundle builder, allocation validator, scorer (`--selftest`)
- `backtest_trailing.py` — the +F tool (trailing 20-day backtest; refuses dates after the decision date)
- `check_memo_compliance.py` — advisory-memo weight-list detector (item #5)
- `prompts_stock.json` — worker (A/P × ±F) and parent prompts, identical except the role and tool paragraphs
- `llm_client.py` — the two LLM calls (Anthropic SDK): one agent loop, one tool, budgets from env
- `run_stock_episode.py` — episode runner; imports `llm_client`, writes one JSON log per episode
- `analyze_stock.py` — paired table, P−A differences, δ, paired-bootstrap CIs, secondaries from the logs
- `README_stock.md` — the five commands, in order

The whole chain was dry-run end to end with a mock model (5 cells × 2 dates → 20 logs → analysis table). Only the API key and the model string are missing.

---

## C.1 What StockBench is (and what we are not using)

StockBench (Chen et al., 2025; `ChenYXxxx/stockbench`) is a **continuous trading simulator**. An LLM agent runs every trading day over a multi-month window, receives a feature bundle per stock (7-day closes, fundamentals, top-k news, its own position state and decision history), and emits `increase / hold / decrease / close` with a target cash amount per stock. A backtest engine fills at next-day open with configurable slippage and commission, applies dividends and splits, enforces a max-positions constraint, and reports cumulative return, Sortino, and max drawdown against a buy-and-hold basket. The canonical setup is a two-agent pipeline (a "fundamental filter" agent chooses which stocks get fundamental data, then a "decision" agent trades).

What it ships offline, and what we rely on:

| Asset | Coverage | Point-in-time cutoff available? |
|---|---|---|
| Daily OHLCV, 20 DJIA names | 2024-12-13 → 2025-07-31, 148 trading days | yes (by date) |
| News per symbol per day (Finnhub/Polygon) | thin before Feb 2025; full Mar–Jul (median 20/20 symbols) | yes — `published_utc` per item; 0 of 1,069 checked items post-date their file |
| Quarterly financials | all names | yes — `filing_date` per record |
| Daily indicators (market cap, P/E, yield, 52-wk hi/lo) | Feb–Jul | yes (file date) |
| SPY | 14 days only | **not usable** as benchmark |

**What we do not use:** the daily loop, the position/history state, the two-agent pipeline, the LLM decision prompt, slippage/commission, and the `total_return / Sortino / max DD` headline metrics over a multi-month path. All of that is path-dependent and gives dozens of correlated decisions per run instead of independent experimental units.

**What we keep:** the data, the universe, offline mode, and next-day-open execution semantics for scoring. This is why the paper calls the environment **StockBench-derived**, never "StockBench."

---

## C.2 The blocking issue: outcome contamination

The data are from **March–July 2025**. If the frozen model's training data extends past July 2025, the model may know how these stocks moved, and every cell can recall outcomes rather than reason under uncertainty. This is different from SWE contamination (where pairing absorbs it): here it destroys the environment's only reason to exist, which is that the future is unverifiable at decision time.

**Rule:** before any StockBench episode, record the model's training cutoff `[CHECK — #1]`.

- Cutoff **before 2025-03-26** → environment is valid as designed.
- Cutoff **between 2025-03-26 and 2025-07-31** → use only decision dates after the cutoff; if fewer than 6 remain, demote.
- Cutoff **after 2025-07-31** → demote to a labeled case study, or re-source data for a post-cutoff window with the same selection script (StockBench's data adapters can fetch Polygon/Finnhub data for any date range with free-tier keys; this is a day of work and only worth it if the two core environments are done).

No prompt instruction ("do not use knowledge after date t") fixes this; it is stated as a hard gate.

---

## C.3 Task definition

A **task** is a decision date *t*. The system receives the information set through *t* and outputs one target portfolio. The portfolio is bought at the open of *t+1*, held without rebalancing, and marked at the close of *t+H*, with *H = 10* trading days.

**Information set through *t*** (assembled by the adapter, offline mode only):

- daily bars for all 20 names from *t−60* trading days to *t* inclusive;
- news items with `published_utc ≤ t 23:59:59` from the two calendar days before *t* through *t* (StockBench's `lookback_days: 2`, applied at the item level, not the file level);
- the most recent quarterly financials with `filing_date ≤ t`;
- the indicator file dated ≤ *t* (latest available);
- the trading calendar (so the agent knows *H* and the execution convention).

**Never provided:** any bar, news item, filing, or indicator dated after *t*; the equal-weight benchmark's realised return; other tasks' outcomes.

**Frozen parameters:**

| Parameter | Value | Note |
|---|---|---|
| Universe | the 20 names in `config.yaml` | fixed for all tasks |
| Horizon *H* | 10 trading days | matches the report |
| Long-only, fully specified | weights ≥ 0, sum = 1, `CASH` allowed | |
| Max weight per name | 0.25 `[CHECK]` | prevents single-name bets from dominating a 10-day return |
| Execution | buy at open of *t+1*, mark at close of *t+H* | StockBench convention |
| Costs | zero slippage, zero commission | stated; costs would add noise, not information, at *H=10* |
| Benchmark | equal-weight basket of the 20, same execution | SPY unavailable; EW is StockBench's own "simple average" benchmark |
| Corporate actions | bars are split/dividend adjusted (`adjusted: true`) | no separate handling |

---

## C.4 Conditions

| Cell | Worker sees | Worker execution | Worker returns |
|---|---|---|---|
| A0 (Advisory, −F) | information set as a static text bundle | **none** | memo |
| A1 (Advisory, +F) | bundle + Python sandbox | yes | memo |
| P0 (Production, −F) | static bundle | **none** | `allocation.json` |
| P1 (Production, +F) | bundle + sandbox | yes | `allocation.json` + `worker_backtest.txt` |
| Solo | bundle + sandbox (parent) | yes | — |

**The feedback factor here.** +F gives the worker a sandbox in which it can compute anything on the information set — including a **trailing backtest** of a candidate allocation over the last 20 trading days (the only "test" this environment admits). −F gives the static bundle and no execution. This keeps the definition identical to A.8 and B.9 (execution vs none). It must be stated plainly that in **no** cell can the worker verify the future; +F verifies only internal consistency and in-sample behaviour. That is precisely what makes this the boundary case.

**Advisory memo** (A0/A1): market-regime view, ranked names with rationale, catalysts, risk factors, concentration warnings, directional recommendations. **May not** contain a per-name weight list, a JSON object, or a target allocation. Compliance flag `[CHECK — #5]`: any line matching `<TICKER>: <number>` for more than *k* names, or a JSON object with ticker keys.

**Production artifact** (P0/P1): `allocation.json`, `{"AAPL": 0.10, ..., "CASH": 0.08}`, validated on receipt: keys ⊆ universe ∪ {CASH}, all values ≥ 0, sum ∈ [0.999, 1.001], no value > cap. Invalid → compliance flag; parent still receives it (with the validator message) and must produce a valid final.

**Parent** (identical in every cell): receives the information set plus the designated handoff only; has the sandbox; produces `final_allocation.json` and submits. The backtester scores only the parent's final allocation.

---

## C.5 Outcomes

**Primary:** excess return over *H*,
`ExcessReturn = R(final allocation) − R(equal-weight)`, both executed at open *t+1*, marked at close *t+H*.

Using excess rather than raw return removes the common market move in the window, which is the dominant source of variance at *H=10* and is identical across cells on the same date.

**Secondary:**
- beat-benchmark indicator (`ExcessReturn > 0`);
- within-window max drawdown of the final portfolio (daily closes);
- worst single-day return in the window;
- concentration: HHI of final weights; number of names with weight > 0;
- turnover vs equal-weight: `‖w_final − w_EW‖₁`;
- raw worker allocation return *Y_worker* (P cells) and `Δ_parent = Y_final − Y_worker`;
- `‖w_worker − w_final‖₁` and accept-without-change flag;
- parent post-handoff tokens, calls, sandbox executions.

---

## C.6 Date selection (`select_stock_dates.py`)

Every rule is a property of the data calendar. No date is chosen by looking at what happened in the market.

| # | Rule | Threshold | Purpose |
|---|---|---|---|
| 1 | History available | ≥ 60 trading days before *t* (pilot: ≥ 40) | the 60-day bar window and the 20-day trailing backtest must exist |
| 2 | Future available | ≥ 10 trading days after *t* within the cache | scoring needs *t+H* |
| 3 | Complete prices | bar present for all 20 names on *t* | execution and marking |
| 4 | News coverage | ≥ 15 of 20 names have a news file in [*t−2*, *t*] | information set is not degenerate; excludes Dec–Jan where news is thin |

Rules 1–4: 148 trading days → **78 eligible dates**, 2025-03-26 → 2025-07-17.

**Spacing.** Main dates are taken greedily from the earliest eligible date with a gap of ≥ *H* trading days, so that no two main windows overlap and dates are independent experimental units. This yields exactly **8 non-overlapping windows** — the report's target — and no room for a seeded draw; the set is fully determined by the calendar, which is the strongest possible form of pre-registration.

| Set | Size | Dates |
|---|---|---|
| Pilot | 3 | `2025-02-26`, `2025-03-03`, `2025-03-06` (windows end before the first main window; pilot windows may overlap each other — pilot is never analysed) |
| Main | 8 | `2025-03-26`, `2025-04-09`, `2025-04-24`, `2025-05-08`, `2025-05-22`, `2025-06-06`, `2025-06-23`, `2025-07-08` |
| Reserve (in order) | 7 | `2025-04-02`, `2025-04-16`, `2025-05-01`, `2025-05-15`, `2025-05-30`, `2025-06-13`, `2025-06-30` — the midpoints of the main windows. **These overlap main windows by construction.** They are used only if a main date fails validation; if any is used, the analysis switches to a block bootstrap over dates (C.7). |

**Selection SHA-256:** `7055e699f382da784a139f7acdb548860ee2af615b6b636eda21aa24f8f82785`

The main dates span the April 2025 tariff shock and its recovery; that is a feature (regime variety), not a selection choice, and it is recorded here before any result is seen.

---

## C.7 Analysis

- **Unit:** the decision date. Eight paired observations per contrast.
- **Primary estimand:** mean and median paired difference in excess return, Production − Advisory, within each feedback level; δ as in the other environments.
- **Uncertainty:** paired bootstrap over dates (8 units → wide intervals; report them). If any reserve date is used, windows overlap and the bootstrap becomes a circular block bootstrap over dates with block length 2.
- **Never:** treat daily returns within a window as independent observations; pool with the other environments; annualise anything.
- **Presentation:** the Stock row of the forest plot; a strip plot of the 8 paired differences; equity curves only as a descriptive figure.
- **Framing:** exploratory. The paper says "the pattern is consistent / inconsistent with the verifiability hypothesis," not "StockBench shows."

---

## C.8 Validation (Stage 3, co-author B)

Environment: `pip install -r stockbench/requirements.txt`; run with `--data-mode offline_only` everywhere (never `auto`, which would call Polygon/Finnhub and could pull post-*t* data).

| # | Check | Pass condition |
|---|---|---|
| 5 | Information-set builder for date *t* | no bar, news item, filing, or indicator dated after *t* (assert on every record) |
| 6 | Scorer determinism | the same `allocation.json` scored twice returns identical excess return to 1e-12 |
| 7 | Equal-weight sanity | scoring the EW allocation returns excess = 0 exactly |
| 8 | Validator | rejects negative, >cap, sum≠1, unknown tickers; accepts a valid file |
| 9 | Cap feasibility | with 20 names and cap 0.25, a valid allocation exists (trivially yes; check the validator agrees) |
| 10 | All 8 main dates + 3 pilot dates build and score with a dummy EW allocation | no error |

A main date failing check 5 or 10 is replaced by the next reserve date in order and the overlap is logged.

---

## C.9 Go / no-go (from the report, made operational)

Proceed only if all of: C.2 passes; checks 5–10 pass; ≥ 6 main dates valid; the scorer is a ≤ 100-line deterministic function (not the sequential engine). If any fails by end of pilot day, demote to a labeled exploratory case study and do not let it consume core-environment time.

---

## C.10 The harness (`stockbench_snapshot.py`) — built and verified

Three functions and nothing else; `BacktestEngine` is never invoked.

| Function | Does | Verified |
|---|---|---|
| `build_bundle(repo, t)` | assembles the information set through *t*: last 60 bars per name, `close_7d`, latest indicator file ≤ *t*, latest filing with `filing_date ≤ t`, top-5 news items with `published_utc ≤ t 23:59:59` from calendar days *t−2..t*. Asserts on any record after *t*. Returns dict + SHA-256; `bundle_to_text()` renders the agent-facing document. | builds for all 11 pilot+main dates; no assertion fires; 63–98 news items per bundle |
| `validate_allocation(alloc, cap)` | keys ⊆ universe ∪ {CASH}; weights ≥ 0; each name ≤ cap; sum ∈ [0.999, 1.001] | accepts EW; rejects negative, over-cap, sum≠1, unknown ticker |
| `score(repo, t, alloc, H)` | buys at open *t+1*, marks at close *t+H*; returns portfolio return, EW return, excess, beat flag, within-window max drawdown, worst day, HHI, n_positions, L1 vs EW | deterministic (identical on repeat); EW excess = 0 to 1e-17 on every date |

`python stockbench_snapshot.py --repo stockbench --selftest --dates stock_date_selection.json` reproduces the checks. `--bundle 2025-04-09` prints the text bundle.

**Bundle size:** ≈ 20k characters ≈ 5k tokens. Cheap; the worker's budget is not the constraint here.

**Noise floor (measured, C.11).** For random valid allocations, the standard deviation of excess return within a window is **≈ 0.95 pp** (range 0.6–1.6 across the eight dates). The best achievable single-name tilt at the 0.25 cap is +0.9 to +3.5 pp. With 8 paired dates the standard error of a mean paired difference is **≈ 0.5 pp**. So the environment can detect a Production–Advisory difference of roughly 1 pp or more; it cannot resolve anything smaller, and the paper says so before results are seen.

| Date | EW window return | sd(excess), random | 95th pct \|excess\| | best 1-name tilt |
|---|---|---|---|---|
| 2025-03-26 | −4.72% | 0.95% | 1.71% | +3.53% |
| 2025-04-09 | +1.92% | 1.59% | 3.04% | +2.49% |
| 2025-04-24 | +3.44% | 1.04% | 2.09% | +2.06% |
| 2025-05-08 | +0.98% | 1.23% | 2.23% | +1.06% |
| 2025-05-22 | +3.31% | 0.60% | 1.18% | +0.93% |
| 2025-06-06 | −1.17% | 0.77% | 1.46% | +1.90% |
| 2025-06-23 | +2.76% | 0.72% | 1.44% | +1.26% |
| 2025-07-08 | +0.50% | 0.68% | 1.34% | +1.54% |

- **Point-in-time filter is item-level for news** (by `published_utc`), record-level for filings (`filing_date`), file-level for bars and indicators.
- **Compliance:** advisory memo weight-list detector; allocation validator; −F worker made no execution; parent submitted.
- **Per-episode log:** date, cell, replicate, model string, prompt hashes, bundle hash, tokens/calls, worker and final allocations, validator messages, raw and final excess returns, secondary metrics, L1 distances, parent post-handoff activity, failures.

---

## C.11 Expert assessment

*Is this a hard design?* No. The snapshot protocol is a clean simplification and the bundled data support it with almost no engineering: the eligible calendar falls out mechanically, gives exactly eight independent windows, and the scorer is trivial.

*What is hard is making the result mean anything.* Three things decide that:

1. **Contamination (C.2).** If the model knows 2025 market outcomes, the environment is not a delayed-feedback environment at all. Check the cutoff first; everything else is moot otherwise.
2. **Power.** Eight dates with a noisy continuous outcome will produce wide intervals. The environment can show whether the Production advantage *disappears* relative to the other two (a qualitative pattern), not estimate its size. Write the results section accordingly before running.
3. **The feedback contrast is weaker here by nature.** +F lets the worker check arithmetic and trailing behaviour, not correctness. That is the point of the boundary case, but it means δ for Stock is not comparable in kind to δ for Spreadsheet. State it once, clearly.

Given the deadline, the right allocation is in C.12.

---

## C.12 Implementation kit — what co-author B wires up

Everything below is written and smoke-tested; the two LLM loops are the only code left.

### Files and their contracts

| File | Contract |
|---|---|
| `stockbench_snapshot.py` | `build_bundle(repo, t)` → dict with `bundle_sha256`; `bundle_to_text(b)` → the agent-facing document; `validate_allocation(alloc, cap=0.25)` → `(ok, msg)`; `score(repo, t, alloc, H=10)` → outcome dict. Self-test: `--selftest --dates stock_date_selection.json`. |
| `backtest_trailing.py` | CLI the +F worker and the parent may run: `--repo --date --alloc file.json`. Scores the allocation over the 20 trading days ending at `--date`. Reads `DECISION_DATE` from the environment and refuses any later `--date`. Validates the allocation first. |
| `check_memo_compliance.py` | `check(memo, k=3)` → `(compliant, reasons)`. Flags a JSON allocation object, > k ticker–number pairs, or > k explicit weight statements. Flag only; the episode still runs. |
| `prompts_stock.json` | `worker_common` + `worker_A` or `worker_P` + `feedback_1` or `feedback_0`; `parent`. Diff `worker_A` against `worker_P`: they differ only in the role paragraph. Diff `feedback_1` against `feedback_0`: only the tool paragraph. |
| `llm_client.py` | `call_worker(cell, bundle_text, sandbox_dir)` and `call_parent(bundle_text, handoff, sandbox_dir)` on the Anthropic Messages API. One loop for both roles; `tools=[backtest_trailing]` is attached only for +F workers and the parent; the tool runs `backtest_trailing.py` as a subprocess with `DECISION_DATE` inherited. Parses the last JSON object as the allocation. Reads `PAPER_A_MODEL`, `PAPER_A_MAX_CALLS`, `PAPER_A_MAX_TOKENS`, `PAPER_A_TEMPERATURE` from the environment — those are #1 and #2, set once and frozen. |
| `run_stock_episode.py` | `--repo --date --cell {A0,A1,P0,P1,SOLO} --replicate --out`. Builds the bundle, sets `DECISION_DATE`, calls the worker (if any), runs compliance/validation, scores the raw candidate (P cells), calls the parent, validates and scores the final, computes L1 and Δ_parent, writes one JSON log. Refuses to start if `llm_client.py` is absent. |
| `analyze_stock.py` | `python analyze_stock.py logs/main` → per-date excess by cell, P1−A1, P0−A0, δ with paired-bootstrap 95% CIs, and the secondary columns. |

### Per-cell tool manifest (this is item #4 for Stock)

| Cell | Worker prompt | Worker tools | Parent tools |
|---|---|---|---|
| A0 | common + A + feedback_0 | none | `backtest_trailing.py` |
| A1 | common + A + feedback_1 | `backtest_trailing.py` only | `backtest_trailing.py` |
| P0 | common + P + feedback_0 | none | `backtest_trailing.py` |
| P1 | common + P + feedback_1 | `backtest_trailing.py` only | `backtest_trailing.py` |
| SOLO | — | — | `backtest_trailing.py` |

The parent row is identical in every cell by construction. No cell has file-system access beyond writing its allocation JSON in the sandbox. −F compliance is `tool_calls == 0`, logged.

### What is left to set

- `PAPER_A_MODEL` (#1) — and its training cutoff (C.2).
- `PAPER_A_MAX_CALLS`, `PAPER_A_MAX_TOKENS` (#2) — from the pilot, then frozen.
- If a different SDK is used, replace `_loop()` in `llm_client.py`; the two public functions' contracts are documented in the runner's stubs.

### Running

```
python stockbench_snapshot.py --repo stockbench --selftest --dates stock_date_selection.json   # once
for d in $(jq -r .pilot[0] stock_pilot.json); do for c in A0 A1 P0 P1 SOLO; do
  python run_stock_episode.py --repo stockbench --date $d --cell $c --out logs/pilot; done; done
# inspect the five logs, then:
for d in $(jq -r '.main[]' stock_date_selection.json); do for c in A0 A1 P0 P1; do
  python run_stock_episode.py --repo stockbench --date $d --cell $c --out logs/main & done; done; wait
```

### Analysis from the logs

For each date, `excess_return` from `parent.final_score` per cell. Paired differences P1−A1, P0−A0, δ = (P1−A1) − (P0−A0). Mean, median, paired bootstrap over the 8 dates. Secondary columns straight from the log: `l1_worker_to_final`, `accepted_unchanged`, `delta_parent_excess`, `parent.trailing_backtests`, `parent.tokens_out`, compliance flags.

## C.13 One-night plan

The harness exists and is tested, so the build is done. What remains:

| Step | Time | Owner |
|---|---|---|
| 1. Model cutoff vs 2025-03-26 → 2025-07-31 (C.2). If it fails, stop; Stock becomes a designed-not-run paragraph. | 5 min | co-author B |
| 2. `pip install anthropic`, set the three env vars, run the self-test (README). | 10 min | co-author B |
| 3. Nothing — compliance and validation are already in the runner. | 0 | — |
| 4. Pilot: **one date** (`2025-02-26`) through all four cells + Solo. Inspect the five traces. | 30 min | both |
| 5. Main: 8 dates × 4 cells = 32 episodes in parallel. | < 1 h wall-clock | co-author B |
| 6. Scores via `score()`; paired differences; strip plot. | 15 min | co-author A |

Cuts relative to the full design, all pre-declared: pilot is 1 date not 3; reserve dates are not pre-validated (used only on failure, overlap logged); the +F sandbox is a fixed script rather than free execution. Nothing about the frozen parameters, the outcome, or the analysis changes.
