# Spreadsheet Pilot — PI Assessment (2026-09-03)

Tasks `55965`, `469-9`, `48643` × cells A0/A1/P0/P1 = 12 episodes. Model `claude-sonnet-4-5-20250929`, temperature 0, protocol v0.2.

## 1. Verdict

**The treatment is real and the harness is trustworthy. Do not run main yet.** Four things must change first — one is a protocol error of mine, one is a budget mechanism that quietly killed a correct episode, one is a tool bug, and one is a framing decision the ceiling forces. All four are pre-freeze changes and take under two hours.

## 2. What happened

| Task | Cell | Worker | Final | Parent changed candidate? | Notes |
|---|---|---|---|---|---|
| 469-9 | A0 | — | 1.0 | — | |
| 469-9 | A1 | — | 1.0 | — | worker ran scratch Python 4× |
| 469-9 | P0 | 1.0 | 1.0 | no | |
| 469-9 | P1 | 1.0 | 1.0 | no | |
| 48643 | A0 | — | 1.0 | — | parent needed 5 write/run cycles |
| 48643 | A1 | — | **0.0** | — | parent wrote answer to column F; grader wanted H |
| 48643 | P0 | 1.0 | 1.0 | yes | |
| 48643 | P1 | **0.0** | **0.0** | no | worker wrote to column G; parent accepted it |
| 55965 | A0 | — | 1.0 | — | |
| 55965 | A1 | — | 1.0 | — | worker ran scratch Python 7× |
| 55965 | P0 | 1.0 | 1.0 | no | |
| 55965 | P1 | 1.0 | **not finalized** | — | parent hit 200k input-token cap on call 18; never submitted |

Treatment compliance from `events.jsonl`, all 12: −F workers made **zero** execution calls; +F workers made 1–7; `solution.py` crossed the handoff in P cells only; A1 scratch destroyed; parent submitted in 11/12 (the 12th is the budget case). Memo schema identical across A and P. Validator and QC clean everywhere. **This is the four-cell contrast, enforced by the tool manifest, working.**

## 3. The four findings

### 3a. Give both agents `answer_position`. My appendix was wrong.

Three of the four non-1.0 results are the same failure: the agent solved the problem and wrote the answer in the wrong column. `48643`'s instruction never names a column; the grader checks `H2:H40`. A0 and P0 guessed H by luck; A1 chose F, P1 chose G.

SpreadsheetBench's **own inference protocol** (`inference/prompt_format.py`) gives the model `answer_position` and `instruction_type` in the prompt — "you only need to modify or fill in values within the cell range specified by answer_position." Appendix B.9 said never to show `answer_position`. That was my error; it deviates from the benchmark and turns a reasoning task into a coin flip on location, adding noise that is identical across cells and therefore pure loss of power.

**Change:** pass `answer_position` and `instruction_type` to worker and parent in every cell, exactly as the official prompt does. Golden values stay hidden. Record as a pre-freeze protocol amendment.

### 3b. The cumulative input-token cap is a length penalty, not a budget. It killed a correct episode.

`55965 P1`: the worker's candidate scored 1.0. The parent then read ranges, rewrote the script three times, and was cut off at call 18 with 201k cumulative input tokens — never submitted, scored as "not finalized."

The trace shows why: prompt caching is covering only ~1.3k tokens (the system prompt), not the conversation. Every call re-sends the whole growing transcript uncached, so cumulative input grows quadratically with turns. A 200k cap therefore fires at ~18 turns regardless of how much real work is done, and it fires hardest on the role that inspects most — which is the parent in P cells and the A0 parent. That is a treatment-correlated censoring mechanism.

**Change (either, preferably both):** (1) put `cache_control` on the last message block so the conversation prefix is cached; cached tokens are ~10% cost and should not count toward the cap. (2) Drop `max_input_tokens_total` as a stopping rule; keep `max_turns` (25), `max_tool_calls` (60), `max_output_tokens_total`, and wall-clock as the backstops. Input tokens remain a *reported* cost outcome, not a stopping rule. Then decide: an episode that still fails to finalize is an agent failure, scored 0, kept in analysis — which is the right call once the cap is no longer the cause.

### 3c. `search_workbook` is broken.

`'EmptyCell' object has no attribute 'coordinate'` — hit the A0 worker on 48643 twice and the P1 worker on 55965 once. A tool that errors on some workbooks is a harness defect that lands unevenly across cells. Fix (skip `EmptyCell` in the iterator) and add a test on a sheet with gaps.

### 3d. Expect a ceiling. Pre-declare what carries the result.

With `answer_position` supplied, all three pilot tasks would very likely be 1.0 in all four cells. If main looks like that, the binary primary outcome has no variance and the Production–Advisory contrast on *success* is uninformative. That is not a failure of the study — it is the H2 case: when the worker can build a correct artifact cheaply, the difference shows up in **parent rework**, not in success. The pilot already shows it:

| Cell | Worker out-tokens | Parent out-tokens | Parent calls | Parent seconds |
|---|---|---|---|---|
| A0 | 2.1k | 3.5k | 10.0 | 67 |
| A1 | 7.1k | 2.4k | 7.7 | 48 |
| P0 | 3.3k | 1.8k | 6.3 | 39 |
| P1 | 4.5k | 3.0k | 10.0 | 61 |

Parents in P0 do about half the work of parents in A0. Feedback (+F) moves effort from the parent to the worker in A cells. Those are the effects the paper can measure at n=20 even if success is flat.

**Change:** in the protocol, state now — before main — that if the primary binary outcome is at ceiling (> 85% success pooled), the confirmatory analysis is on parent post-handoff effort (calls, output tokens, write/run cycles, seconds) and `target_unit_pass_rate`, with success reported descriptively. Written today, this is a pre-registered contingency; written Friday, it's a post-hoc rescue.

## 4. What is working and should not be touched

- Enforcement by tool manifest, not prompt. Zero −F execution across 6 episodes.
- Same memo schema in A and P; production adds only the artifact. Clean operationalization of "does an executable cross the boundary."
- Worker candidate scored after the parent finishes. Three-way hashes. Immutable runs, quarantine with reasons. Dirty-tree refusal.
- `48643 P1` is a textbook "parent accepts a wrong artifact unchanged" — exactly the worker→final transition the paper wants to show.
- Cost and speed: ~$0.40 and ~2 minutes per episode. **80 main episodes ≈ $31, under an hour in parallel.** This means Solo on all 20 tasks and a second replicate are both affordable. Run them.

## 5. Model note

The frozen model is `claude-sonnet-4-5-20250929`, not the model that printed in an earlier check. Record it in the protocol. Its training data extends into mid-2025, so the StockBench contamination gate (C.2) is still a problem for the March–July 2025 window; that decision is separate from Spreadsheet and unchanged.

## 6. Instructions — in order

1. **Amend protocol, then code:** add `answer_position` + `instruction_type` to the task text for both roles (3a). One prompt change, identical in every cell. Log the amendment with today's date and reason.
2. **Fix budgets** (3b): prefix caching on; remove the input-token stopping rule or raise it 5×; keep turn/tool/output/wall backstops. Regression test: an episode with 25 turns of `read_range` must not be stopped by input tokens.
3. **Fix `search_workbook`** (3c). Test on a sparse sheet.
4. **Rerun the same 3 pilot tasks × 4 cells** with the fixes. Expect ≥ 11/12 finalized and no location failures. This is the freeze check.
5. **Write the ceiling contingency into the protocol** (3d). Freeze. Commit. Tag `freeze-spreadsheet-v0.3`.
6. **Main, wave 1:** first 5 main tasks (`57232`, `42216`, `567-21`, `599-9`, `46121`) × 4 cells + Solo = 25 episodes. Run the analysis script on them. Look at success rate and the parent-effort table.
7. **Main, wave 2:** remaining 15 tasks × 5 conditions = 75 episodes. Then replicate 2 of all 20 × 4 cells if wave 1 was clean — it's an hour and $30.
8. Do not touch prompts, budgets, tools, or task lists between step 5 and the end.

Estimated: fixes 1–3, 90 minutes. Rerun pilot, 15 minutes. Main, under 2 hours wall-clock including both waves. Done by mid-afternoon.
