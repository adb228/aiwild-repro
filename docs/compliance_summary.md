# Compliance summary (pass counts per arm)

Audit method: event-backed `tool_call` / `tool_result` by role; auto-observation on eligible error-free run results; artifact presence from episode path/hash fields (hashes only in this archive); QC from frozen `qc.py`. Flags are retained. Only malformed records are rejected.

| Arm | Structural / schema | Finalized | Auto-observation / isolation | Treatment violations | QC retained |
|---|---|---|---|---|---|
| Spreadsheet v0.4.1 pilot (Sonnet) | 12/12 complete | 11/12 parent-finalized | 33/33 worker + 46/46 parent auto-obs; 0 explicit `read_output_range` | 0 | 1 parent cap |
| Spreadsheet v0.4.1 main (Sonnet) | 99/99 schema valid (+ 1 analysis-only ITT row) | 2 `parent_not_finalized` | 285/285 worker, 215/215 parent auto-obs; A0/P0 worker obs 0 | 0 | 2 not_finalized |
| Spreadsheet v0.4.1 Solo (Sonnet) | 25/25 valid | 1 `parent_not_finalized` | 58/58 auto-obs | 0 | 1 |
| SWE A-2 closed matrix (Sonnet) | 28 measured + 3 EF + 4 handwritten = 32 IDs | handwritten rows are infra fillers | n/a (closed) | handwritten ≠ treatment scores | see chronology |
| SWE-0.3.5 main (Sonnet) | 32/32 structurally valid | 1 measured not_finalized | HEAD=base 32/32; worker≠parent containers 32/32; assistant text 32/32 | 0 | 6 agent QC flags |
| Spreadsheet v0.4.1 main+Solo (Luna) | 125/125 validator issues empty | 5 parent_not_finalized | model-2 client | 0 | production_missing_artifact/memo 2; advisory_missing_memo 2 |
| SWE-0.3.5 main (Luna) | 32/32 validate empty (schema-lag filter) | 0 missing cells | pilot +F FAIL on django-13112 A1 (disclosed; proceeded) | 0 | `advisory_missing_memo` 11 retained |

SWE-0.3.5 QC-flagged Sonnet agent cells: sphinx-8593 A0; sympy-12419 A0; sympy-16766 A0; sympy-16766 P0; scikit-learn-14087 P0; astropy-8872 A0.
