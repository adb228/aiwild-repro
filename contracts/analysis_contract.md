# Analysis contract v0.3

## Conditions

| Condition | Worker handoff | Pre-handoff executable feedback |
| --- | --- | --- |
| A0 | Common memo only | Withheld |
| A1 | Common memo only | Available in disposable scratch |
| P0 | Common memo plus `solution.py` | Withheld |
| P1 | Common memo plus `solution.py` | Available |

The treatment is whether an executable candidate artifact crosses the
worker-to-parent handoff boundary. The parent starts from a clean copy of the
original input, sees no worker trace, and has normal execution and
recalculation access.

## Outcomes

Primary: `full_task_success`, one only when every required target cell is
correct.

Secondary: `target_unit_pass_rate`, the fraction of required target cells that
pass. `n_units` is always reported. Zero evaluated units is an evaluator
failure, never a successful outcome.

## Estimands

`tau_0 = mean(Y_P0) - mean(Y_A0)`

`tau_1 = mean(Y_P1) - mean(Y_A1)`

`delta = tau_1 - tau_0`

With replicates, average within task-condition, weight tasks equally, and
bootstrap whole tasks.

## Record handling

Malformed records are rejected by structural validation. Valid observations
of agent failure or treatment noncompliance are retained and flagged by
scientific QC. Infrastructure failures may be retried; agent failures may not
be silently replaced.
