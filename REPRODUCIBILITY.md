# Reproducibility

## Environment

- Python 3.13 (see `.python-version`); `requires-python = ">=3.11"`
- `uv` for the locked environment (`uv.lock` is the Sonnet freeze)
- Docker Desktop (Spreadsheet sandbox + SWE official images)
- LibreOffice 26.8.0.3; manifest executable `/Applications/LibreOffice.app/Contents/MacOS/soffice`
- `swebench==5.0.2` for SWE Gate 0 / harness evaluation

```bash
uv sync
```

`pyproject.toml` also declares `openai>=1.80` and `httpx>=0.27` for model-2. Those packages are not in the Sonnet-era lockfile; install them from the project metadata if reproducing Luna cells.

Copy `.env.example` to `.env` and set empty-named variables. Do not commit `.env`.

## Benchmark prep

SpreadsheetBench Verified-400 archive (manifest `archive_sha256`):

`10ef893dd29cb13ab97143ea787e68cdc9574a13873ab9a54e50b31dc03fc949`

```bash
python -m delegation_study.prepare \
  --archive spreadsheetbench_verified_400.tar.gz \
  --public data/public \
  --hidden data/hidden
```

`data/hidden` (golden workbooks) must never be mounted into agent sandboxes.

SWE-bench images follow the official harness name:

`swebench/sweb.eval.x86_64.{instance_id}:latest`

lowercased, with `__` replaced by `_1776_`. Runner pin `_DOCKER_DIGEST` is `sha256:743b7bd1ab48273b53cc92635f21a5f2697d2a7bd4faf7a2a17c399d0d24ebecd`. Host evaluation uses x86 emulation on Apple silicon.

Selection content hashes (keep; these are package hashes, not git commits):

- Spreadsheet v0.3 package `selection_sha256` `cf818c1c9be7568223245e89a705d94d64aa7d99e4341604414378571f11752d`
- SWE-0.3.5 package `selection_sha256` `e426590d9e9342af60ab48c5e3ddb174a73fbdfb05de5fff7ffb7d348752f0be`
- SWE-0.3.5 JSON file sha256 `4c6809942c24100391548760f2c5bedbd787755fb19a6fce933f8a593c253adb`

Prompt file hashes are in `prompts/PROMPT_HASHES.md`.

## Caps (confirmatory)

Spreadsheet v0.4.1 (`config/manifest.yaml`): worker and parent 40 turns / 100 tools / 1200 s wall / 8192 max_tokens / 64000 output-token total. Solo sums turns, tools, output-token total, and wall (80 / 200 / 128000 / 2400) and does not sum `max_tokens`.

SWE-0.3.5 frozen caps: worker 115 / 110 / 1800 / 8192 / 36000; parent 100 / 100 / 1800 / 8192 / 41000. Wall 1800 s is a safety override.

Luna Spreadsheet binding: 9 turns / 14 tools; output safety 77550 worker / 24760 parent; wall 1200 retained. Luna SWE binding: worker 92 / 108 / 138590 / 5290; parent 83 / 123 / 166320 / 3328. See `docs/model2_deviations.md`.

## Run commands (API required)

Dirty working trees are refused for confirmatory `task_set` values. Launch from a clean checkout of this archive after `uv sync`.

Spreadsheet v0.3 companion (protocol 0.3 runner at the v0.3 freeze; this archive’s runner is 0.4.1):

```bash
python scripts/launch_batch.py --task-set main --cells A0,A1,P0,P1 --concurrency 4
python scripts/launch_batch.py --task-set main --cells SOLO --concurrency 4
python scripts/launch_batch.py --task-set hard --cells A0,A1,P0,P1,SOLO --concurrency 4
```

Spreadsheet v0.4.1 confirmatory:

```bash
python scripts/launch_batch.py --task-set main_v04 --cells A0,A1,P0,P1 --concurrency 4
python scripts/launch_batch.py --task-set solo_v04 --cells SOLO --concurrency 4
```

Single cell:

```bash
python -m delegation_study.spreadsheetbench.run --task-id 6239 --condition P1 --task-set main_v04
```

SWE-0.3.5 confirmatory (8 issues × A0/A1/P0/P1; no Solo):

```bash
python -m delegation_study.swebench.run --task-id pylint-dev__pylint-6386 --condition A0 --task-set main_v035
```

Batch launchers used for the reported SWE matrix live in `scripts/launch_pilot_v035.py` and `scripts/launch_swe_main.py` (adapter-era). Caps are those in `config/manifest.yaml` `swebench.caps`. Concurrency 3. Infra retries ≤ 3 then missing.

Fixed-artifact (Spreadsheet S2; never pooled):

```bash
python scripts/launch_fixed_artifact.py --all --concurrency 6
```

Model-2 (same code, `providers/` client; `DELEGATION_MODEL2=1`):

```bash
python scripts/launch_model2_batch.py --task-set main_v04_m2 --cells A0,A1,P0,P1 --concurrency 4
python scripts/launch_model2_batch.py --task-set solo_v04_m2 --cells SOLO --concurrency 4
python scripts/launch_model2_swe.py --task-set swe_main_m2 --concurrency 3
```

## Offline regeneration (no API, no `data/runs/`)

`scripts/analyze.py`, `scripts/process.py`, and `scripts/coverage.py` wrap the study modules. Those modules expect `data/runs/` episode records and cannot rebuild estimates from CSV alone.

The intended offline path is: use `analysis/` as the analysis-ready source. Then:

```bash
python scripts/regenerate_from_analysis.py
```

That command recomputes τ₀, τ₁, δ_Y with the analyzer’s task-clustered bootstrap (no `data/runs/`):

- Spreadsheet v0.4.1 from `analysis/episodes_spreadsheet_v041.csv` (seed 0, B=5,000) → `paper_outputs/v041/table1_effects.tex`. After a clean run the file must be byte-identical to `6a805057fa117e5eed74fc1e6c5dd516834f5011649b8716a87bbf2e86fb4184`.
- Spreadsheet v0.3 companion from `analysis/episodes_spreadsheet_v03.csv` (seed 20260902, B=10,000) → `paper_outputs/table1_effects.tex`. After a clean run the file must be byte-identical to `53744ab6d6c47f5545469883ee51a6499a61d06948f19e6d83ff88b9c66dd5f7`.
- SWE-0.3.5 from `analysis/episodes_swe_v035.csv` (seed 20260902, B=10,000, pairwise-complete) → `paper_outputs/swe_main_v035/table1_effects.tex` (no documented frozen hash).
- Process A1/A3 from `analysis/process/*/episodes.csv` where those columns exist.

paper_outputs/*.pdf are analyzer outputs from the frozen runs; the manuscript's figures are typeset in LaTeX from the tables in analysis/ and paper_outputs/.

If `analyze.py` is pointed at a private `data/runs/` tree (not in this archive):

```bash
python scripts/analyze.py --benchmark spreadsheetbench --runs-dir data/runs --task-set main_v04 --out-dir paper_outputs/v041
python scripts/analyze.py --benchmark spreadsheetbench --runs-dir data/runs --task-set solo_v04 --out-dir paper_outputs/v041/solo
python scripts/analyze.py --benchmark swebench --runs-dir data/runs --task-set main_v035 --out-dir paper_outputs/swe_main_v035
python scripts/process.py --spreadsheet-out paper_outputs/process/spreadsheet --swe-out paper_outputs/process/swe
python scripts/coverage.py --model sonnet --out-dir paper_outputs/coverage
```

## Expected frozen artifacts

| File | SHA-256 |
|---|---|
| `paper_outputs/table1_effects.tex` (v0.3 companion, B=10000 seed 20260902) | `53744ab6d6c47f5545469883ee51a6499a61d06948f19e6d83ff88b9c66dd5f7` |
| `paper_outputs/v041/table1_effects.tex` (Spreadsheet v0.4.1, B=5000 seed 0) | `6a805057fa117e5eed74fc1e6c5dd516834f5011649b8716a87bbf2e86fb4184` |
| `paper_outputs/tableS1_condition_summary.tex` | `ccec0079ae1546677bdb66be6fafd14784632f195d4450bbb196461a6a2f3e96` |
| `paper_outputs/tableS2_hard_probe.tex` | `cb2277fdafeba61172cbbf69b7b3d794e41a0b7282c25d63b039542323497485` |
| `paper_outputs/tableS3_qc.tex` | `e5a8afbb4d8cb07f41a9f1bd0b6087d2c776c23dd28a507000974b6783638710` |
| `paper_outputs/fig1_performance_interaction.pdf` | `bf080f2d81a2711de818097dd97b13e01d302994b51e181956f8b51e461e608e` |
| `paper_outputs/fig2_handoff_mechanism.pdf` | `916300d9149a2978ded7776c22ebbff4f009f45577926bfb024e3bbf65cee739` |

`analysis/episodes_spreadsheet_v03.csv` has no per-role token or wall columns in the frozen v0.3 table. Those cost/role-wall figures in `COMPUTE.md` come from live episode `usage` fields (not copied here). Token/cost/wall columns are populated for v0.4.1, Solo, SWE, fixed-artifact, and model-2.

## Frozen `paper_outputs/` copies (not rewritten by `regenerate_from_analysis.py`)

| File | Role |
|---|---|
| `paper_outputs/tableS1_condition_summary.tex` | frozen paper supplement |
| `paper_outputs/tableS2_hard_probe.tex` | frozen paper supplement |
| `paper_outputs/tableS3_qc.tex` | frozen paper supplement |
| `paper_outputs/fig1_performance_interaction.pdf` | analyzer output from frozen runs; manuscript figures are typeset in LaTeX from tables in analysis/ and paper_outputs/ |
| `paper_outputs/fig2_handoff_mechanism.pdf` | analyzer output from frozen runs; manuscript figures are typeset in LaTeX from tables in analysis/ and paper_outputs/ |
| `paper_outputs/v041/fig1_performance_interaction.pdf` | analyzer output from frozen runs; manuscript figures are typeset in LaTeX from tables in analysis/ and paper_outputs/ |
| `paper_outputs/v041/fig2_handoff_mechanism.pdf` | analyzer output from frozen runs; manuscript figures are typeset in LaTeX from tables in analysis/ and paper_outputs/ |
| `paper_outputs/v041/tableS1_condition_summary.tex` | frozen analyzer table |
| `paper_outputs/v041/tableS2_disposition.tex` | frozen analyzer table |
| `paper_outputs/v041/{effects,condition_summary,cost_*}.{csv,json}` | frozen analyzer sidecars |
| `paper_outputs/v041/qc_report.json` | frozen |
| `paper_outputs/v041/solo/*` | frozen Solo analyzer outputs |
| `paper_outputs/{analysis,collection}_summary.json` | frozen v0.3 analyzer summaries |
| `paper_outputs/qc_report.json` | frozen |
| `paper_outputs/swe_main*/analysis_summary.json` | frozen SWE analyzer summaries |
| `paper_outputs/process/*/a2_*.csv`, `a4_*.csv`, `a5_*.csv` | frozen process tables (A1/A3 are recomputed) |
| `paper_outputs/model2/*` | frozen Luna analyzer outputs (except any A1/A3 written under `process/model2/`) |
| `paper_outputs/process/*/analysis_summary.json` | frozen process summaries |

## Missing notes

- SWE Solo was pre-registered as secondary and was not launched (`_ALL_CONDITIONS` is A0/A1/P0/P1).
- v0.3 per-episode tokens were not in the freeze `episodes.csv`. Cost/role-wall in `COMPUTE.md` were filled from live `usage` fields only; those episode records are not in this archive.
- Coverage C3 narrative case files are omitted (free text). C1/C2/C4/C5 numeric tables are in `analysis/coverage/`.
- `contracts/example_artifacts/output.xlsx` is omitted (no workbooks in this archive except none: traces also omit init/output xlsx).
