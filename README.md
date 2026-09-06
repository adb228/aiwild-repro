# AIWILD 2026 reproducibility archive

This anonymous archive documents a four-cell factorial study of delegated task work with artifact handoff, with and without executable feedback. A worker proposes a memo (advisory) or a memo plus artifact (production); a parent finishes from a clean workspace. Feedback (−F / +F) is crossed with that pairing on SpreadsheetBench and SWE-bench Verified, using two separate model stacks that are never pooled.

The archive contains frozen runner source, contracts, confirmatory selection packages, protocol prompts with content hashes, analysis-ready episode tables, the paper’s tables and figures, methods notes, and four scrubbed representative traces. It does not contain raw `data/runs/` trees, benchmark datasets (`data/public`, `data/hidden`, `data/raw`), credentials, gold workbooks, or FAIL_TO_PASS test bodies.

Models. Confirmatory Sonnet cells used `claude-sonnet-4-5-20250929` at temperature 0 with no extended thinking. Second-model cells used `gpt-5.6-luna` with per-request `reasoning.effort=high` (the effort pin is not persisted on confirmatory events). Second-model runs used the model2 branch of the same code; the OpenAI client lives under `src/delegation_study/providers/`.

Reproduction requires API access; confirmatory tables regenerate offline from `analysis/`. paper_outputs/*.pdf are analyzer outputs from the frozen runs; the manuscript's figures are typeset in LaTeX from the tables in analysis/ and paper_outputs/.
