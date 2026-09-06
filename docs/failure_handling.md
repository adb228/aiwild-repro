# Failure handling (ITT, missingness, replacement, timeouts)

Spreadsheet ITT: a parent that does not finalize is retained with success 0. Budget censoring is kept in the primary estimand. A cell is flagged only if two or more parents in that cell are censored. Spreadsheet v0.4.1 `57232` A0 is an analysis-only ITT 0 (context window 225,243 > 200,000; events present; no `episode.json`). Sensitivity may treat that row as missing. Two other v0.4.1 main cells (`44266` A0, `54474` A1) hit the 40-turn parent cap and are retained as 0. Solo `42216` is not_finalized (`max_tokens`) and is ITT 0 in the Solo table only.

SWE-0.3.5 ITT: infrastructure_failure and evaluator_failure are missing, never coerced to 0. `not_finalized` / empty-patch measured cells use None→0. The confirmatory 32-cell matrix has 0 handwritten rows and 0 replacements. `sympy-16766` A0 is measured not_finalized (output-token cap) and is ITT 0.

SWE A-2 / 0.3.3–0.3.4 (closed, not the 0.3.5 confirmatory matrix): 28 measured records + 3 evaluator_failure cells among those + 4 handwritten infrastructure_failure rows = 32 canonical IDs. Three matplotlib harness `tests_timed_out` cells were recoded from scored 0 to `evaluator_failure` (missing). Four prompt-too-long / usage-limit orphans were handwritten ITT-0 fillers on the closed matrix and were not reused for swe-0.3.5.

Replacement: confirmatory cells are replaced only for infrastructure that left no usable record (dirty-tree launcher refusal; host network loss on an abandoned SWE-0.3.5 pilot). Scientific 0s on complete records are not replaced. Treatment-violating records are QC-flagged and retained; only malformed records are rejected by the validator.

Timeouts: Spreadsheet wall 1200 s (Solo 2400). SWE confirmatory wall 1800 s (safety). SWE harness evaluation 1800 s, one retry, then `evaluator_failure`. Provider 400 `prompt is too long` and content-filter 400 have protocol-specific retry-then-`infrastructure_failure` rules (swe-0.3.4 compact-and-retry; swe-0.3.2 conversation guard).

Luna Spreadsheet: 100/100 main + 25/25 Solo complete. QC-retained missingness includes parent_not_finalized (5), production_missing_artifact/memo (2), advisory_missing_memo (2). Luna SWE: 32/32 complete; 0 null/missing cells on main.

See `docs/protocol_chronology.md` for the version timeline.
