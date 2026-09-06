#!/usr/bin/env python3
"""Pre-launch guards for model-2 Spreadsheet batches.

Dirty-tree, prompt-hash, runner pins, spend ceiling. Never prints keys.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from delegation_study.analysis.model2 import (  # noqa: E402
    MODEL2_ALIAS,
    SPEND_CEILING_USD,
    episode_cost_usd,
    spend_exceeds_ceiling,
)
from delegation_study.model_client_openai import (  # noqa: E402
    REASONING_EFFORT,
    assert_frozen_prompt_hashes,
)
from delegation_study.spreadsheetbench.run import (  # noqa: E402
    _compute_git_dirty,
)


def _spend_so_far(runs_dir: Path) -> Decimal:
    total = Decimal(0)
    if not runs_dir.is_dir():
        return total
    for run_dir in runs_dir.iterdir():
        ep = run_dir / "episode.json"
        if not ep.is_file():
            continue
        try:
            rec = json.loads(ep.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        repro = rec.get("reproducibility") or {}
        models = {repro.get("worker_model"), repro.get("parent_model")}
        if not any(
            isinstance(m, str) and (m == MODEL2_ALIAS or m.startswith(MODEL2_ALIAS))
            for m in models
        ):
            continue
        total += episode_cost_usd(rec)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=_ROOT / "data" / "runs")
    args = parser.parse_args(argv)

    dirty = _compute_git_dirty(_ROOT)
    if dirty is None or dirty:
        print("DIRTY TREE: commit or stash before launch.", file=sys.stderr)
        return 3

    try:
        report = assert_frozen_prompt_hashes()
    except AssertionError as exc:
        deferred = _ROOT / "data" / "dev" / "model2_deferred.md"
        deferred.write_text(
            "# model2 deferred\n\n"
            "Prompt-hash guard failed. Prompts were not edited.\n\n"
            f"{exc}\n",
            encoding="utf-8",
        )
        print(f"PROMPT HASH MISMATCH — wrote {deferred}", file=sys.stderr)
        return 4

    parent = report["spreadsheet_v041"]["parent"]
    print(f"prompt-hash parent={parent}")
    print(f"runner pin: alias={MODEL2_ALIAS} effort={REASONING_EFFORT} "
          f"max_output_tokens=128000")

    spend = _spend_so_far(args.runs_dir)
    print(f"spend_so_far_usd={spend}")
    if spend_exceeds_ceiling(spend):
        print(
            f"SPEND CEILING ${SPEND_CEILING_USD} reached ({spend}). "
            "Launch nothing new.",
            file=sys.stderr,
        )
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
