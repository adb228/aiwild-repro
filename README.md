# Stagewise Error Coverage in Hierarchical LLM Agents — reproducibility archive

Anonymized code and data for the AIWILD @ NeurIPS 2026 submission *Stagewise Error Coverage in Hierarchical LLM Agents: Worker Feedback, Intermediate Quality, and Downstream Recovery*.

## What the study is

A worker agent receives a task and hands off to a parent agent that holds final commit authority. Two factors are crossed:

| | Worker cannot execute (−F) | Worker can execute and observe its output (+F) |
|---|---|---|
| **Advisory** handoff (memo only) | A0 | A1 |
| **Production** handoff (memo + reusable artifact) | P0 | P1 |

The parent is identical in every cell: same model, prompt, tools, budget, clean starting state. Production candidates are scored by the host *before* the parent acts, and the final submission is scored afterward, so final success decomposes exactly into upstream success, downstream repair, fallback success, and damage (Eq. 2 in the paper).

Environments: 25 SpreadsheetBench Verified tasks (primary), 8 SWE-bench Verified issues (prospectively pre-registered), a parent-only fixed-artifact replay of the 50 production candidates, and a second worker–parent model stack repeating both environments.

## Start here: verify the paper's numbers without an API key

```bash
uv sync
python scripts/regenerate_from_analysis.py
```

This recomputes τ₀, τ₁, δ_Y with the task-clustered bootstrap from `analysis/episodes_*.csv` and rewrites the Table 1 files. After a clean run they are byte-identical to the frozen copies (SHA-256 values in `REPRODUCIBILITY.md`); the script asserts this. It also rebuilds the process tables A1 (repair/damage) and A3 (verification by cell) for all four model–environment settings.

Runtime: under a minute. Requires Python 3.11+ and `uv`.

## Layout

| Path | Contents |
|---|---|
| `src/delegation_study/` | Runner, tool implementations, sandboxes, evaluators, analysis modules; `providers/` holds the second-model client |
| `contracts/` | JSON schemas for episode records, event logs, and the seven-field memo |
| `config/` | Tool manifests per cell, protocol caps, frozen task-selection packages (`task_selection_v03/`, `task_selection_swe_v3/`) with seeds and content hashes, the decision log, pricing |
| `prompts/` | Every frozen system prompt per protocol version and cell, with SHA-256 in `PROMPT_HASHES.md` |
| `analysis/` | Analysis-ready episode tables (one row per episode: task, cell, candidate and final outcomes, disposition, per-role calls/tokens/wall/cost, compliance flags, error class), process tables, coverage tables, advisory-arm table |
| `paper_outputs/` | Frozen analyzer outputs from the reported runs (tables, summaries, QC reports). The PDFs here are analyzer diagnostics; the manuscript's figures are typeset in LaTeX from the tables in `analysis/` and `paper_outputs/` |
| `traces/` | Four representative episodes with full event logs, memos, and artifacts (see `traces/README.md`) |
| `docs/` | Protocol chronology, failure-handling rules, compliance summary, second-model deviations, methods facts with sources |
| `scripts/` | Batch launchers for every arm, the offline regeneration script, post-hoc analyses |
| `REPRODUCIBILITY.md` | Environment, benchmark preparation, exact run commands and caps per arm, expected output hashes |
| `COMPUTE.md` | Episodes, API cost, and wall-clock per arm |

## Models and settings

- Primary stack: `claude-sonnet-4-5-20250929`, temperature 0, no extended thinking, `max_tokens` 8192, prompt caching on the system and task prefix.
- Second stack: `gpt-5.6-luna`, reasoning effort configured high process-wide (per-request confirmation is not persisted in event logs; see `docs/model2_deviations.md`). Run from the same code with the `providers/` client.
- Role caps are identical across cells within each environment and stack; Solo receives the summed worker and parent caps. Values in `REPRODUCIBILITY.md`.

## Rerunning experiments

Requires API keys in the environment variables named in `.env.example`, Docker Desktop, LibreOffice 26.8.0.3, and (for SWE-bench) `swebench==5.0.2` with the official instance images. Benchmark data are downloaded by the preparation scripts, not redistributed here; dataset and image hashes are recorded in `REPRODUCIBILITY.md`. Representative commands:

```bash
# SpreadsheetBench v0.4.1, four cells and Solo
python scripts/launch_batch.py --task-set main_v04 --cells A0,A1,P0,P1 --concurrency 4
python scripts/launch_batch.py --task-set solo_v04 --cells SOLO --concurrency 4

# SWE-bench v0.3.5, one cell
python -m delegation_study.swebench.run --task-id pylint-dev__pylint-6386 --condition A0 --task-set main_v035

# Fixed-artifact replay
python scripts/launch_fixed_artifact.py --all --concurrency 6
```

Model outputs are not deterministic across API versions, so reruns will not reproduce episode records bit-for-bit; the frozen tables in `analysis/` are the record of what was run.

## What is not here

Raw run directories, benchmark datasets and gold answers, hidden SWE-bench tests, credentials, and anything identifying the authors. Free-text coverage case notes are omitted; their numeric tables are in `analysis/coverage/`.

## Provenance

Every episode record carries the prompt hashes, dataset hash, sandbox image digest, model string, temperature, protocol version, and selection-package hash under which it ran. Pre-registration timestamps for the SWE-bench and fixed-artifact hypotheses are in `config/study/decisions.md`. The protocol chronology, including the two Spreadsheet protocol corrections and the SWE-bench adapter versions, is in `docs/protocol_chronology.md`.
