#!/usr/bin/env python3
"""Deterministic study-notebook updater for delegation-study results.

Appends a results summary section to the existing study notebook artifact
after episodes complete.  The notebook is a Markdown-formatted research log
(not a Jupyter notebook); its path is resolved in this order:

  1. Explicit --notebook PATH argument.
  2. The path recorded in config/study/notebook_path.txt (one line, path
     relative to the repository root or absolute).
  3. config/study/STUDY_NOTEBOOK.md as a conventional default.

If no notebook file exists at the resolved path, the script fails closed
with a clear error message rather than creating a new file.  This is
intentional: the notebook is a human-curated artifact and should be
initialised by a human before automated updates are permitted.

The update is idempotent with respect to a given run_id: if the notebook
already contains a section tagged with the run_id, the script exits
successfully without writing.

Usage
-----
    python scripts/notebook_update.py --run-id RUN_ID [--notebook PATH]
        [--results-json PATH] [--dry-run]

Options
-------
--run-id RUN_ID         UUID of the completed batch or episode to document.
--notebook PATH         Path to the study notebook to update.
--results-json PATH     Path to a JSON file with batch/analysis results to
                        embed.  If omitted, only the run-id header is written.
--dry-run               Print the would-be appended section without writing.
--task-set TASK_SET     Task-set label to include in the section header
                        (e.g. main, hard, SOLO).

Exit codes
----------
0  Success (update written or already present).
1  Notebook not found (fail-closed).
2  Other error.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_NOTEBOOK_POINTER = _ROOT / "config" / "study" / "notebook_path.txt"
_DEFAULT_NOTEBOOK = _ROOT / "config" / "study" / "STUDY_NOTEBOOK.md"

# Sentinel tag embedded in the notebook to detect existing entries.
_TAG_PREFIX = "<!-- delegation-study-update: "
_TAG_SUFFIX = " -->"


def _resolve_notebook(explicit: Path | None) -> Path:
    """Resolve the study notebook path (fail-closed if not found)."""
    if explicit is not None:
        return explicit
    if _NOTEBOOK_POINTER.is_file():
        raw = _NOTEBOOK_POINTER.read_text(encoding="utf-8").strip()
        if raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = _ROOT / candidate
            return candidate
    return _DEFAULT_NOTEBOOK


def check_notebook(notebook_path: Path) -> int:
    """Verify that the configured study notebook exists and is readable."""
    if not notebook_path.is_file():
        print(
            f"ERROR: Study notebook not found at {notebook_path}.",
            file=sys.stderr,
        )
        return 1
    try:
        content = notebook_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Cannot read notebook: {exc}", file=sys.stderr)
        return 2
    if not content.startswith("# Delegation Study Notebook"):
        print(
            "ERROR: Study notebook does not have the expected title.",
            file=sys.stderr,
        )
        return 2
    print(f"Study notebook check passed: {notebook_path}")
    return 0


def _make_section(
    run_id: str,
    task_set: str | None,
    results: dict[str, Any] | None,
    timestamp: str,
) -> str:
    """Build the Markdown section to append to the notebook."""
    tag = f"{_TAG_PREFIX}{run_id}{_TAG_SUFFIX}"
    header_parts = [f"## Update: {run_id[:8]}…"]
    if task_set:
        header_parts.append(f"task-set={task_set}")
    header = " — ".join(header_parts)

    lines: list[str] = [
        tag,
        "",
        header,
        "",
        f"**Timestamp:** {timestamp}",
        f"**Run ID:** `{run_id}`",
    ]
    if task_set:
        lines.append(f"**Task set:** `{task_set}`")

    if results:
        lines.append("")
        lines.append("**Results summary:**")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(results, indent=2))
        lines.append("```")

    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def update_notebook(
    notebook_path: Path,
    run_id: str,
    task_set: str | None,
    results: dict[str, Any] | None,
    *,
    dry_run: bool = False,
) -> int:
    """Append a results section to the notebook, idempotently.

    Returns 0 on success, 1 if notebook not found, 2 on other error.
    """
    # Fail-closed: notebook must already exist.
    if not notebook_path.is_file():
        print(
            f"ERROR: Study notebook not found at {notebook_path}. "
            "Initialise it manually before running notebook_update.py.",
            file=sys.stderr,
        )
        return 1

    try:
        current = notebook_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Cannot read notebook: {exc}", file=sys.stderr)
        return 2

    # Idempotency check: skip if this run_id is already recorded.
    tag = f"{_TAG_PREFIX}{run_id}{_TAG_SUFFIX}"
    if tag in current:
        print(
            f"Notebook already contains an entry for run_id={run_id!r}; "
            "no update written.",
        )
        return 0

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    section = _make_section(run_id, task_set, results, timestamp)

    if dry_run:
        print("=== DRY RUN — would append ===")
        print(section)
        print("=== end dry run ===")
        return 0

    try:
        with notebook_path.open("a", encoding="utf-8") as fh:
            # Ensure we start on a new line.
            if current and not current.endswith("\n"):
                fh.write("\n")
            fh.write(section)
    except OSError as exc:
        print(f"ERROR: Cannot write notebook: {exc}", file=sys.stderr)
        return 2

    print(f"Appended update for run_id={run_id!r} to {notebook_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--run-id",
        help="UUID of the completed batch or episode.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Verify the configured notebook exists and is readable; do not write.",
    )
    parser.add_argument(
        "--notebook",
        type=Path,
        default=None,
        help="Path to the study notebook (fail-closed if not found).",
    )
    parser.add_argument(
        "--results-json",
        type=Path,
        default=None,
        help="Path to a JSON file with results to embed.",
    )
    parser.add_argument(
        "--task-set",
        default=None,
        help="Task-set label (e.g. main, hard, SOLO).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the section without writing.",
    )
    args = parser.parse_args(argv)

    notebook_path = _resolve_notebook(args.notebook)
    if args.check:
        if args.results_json is not None or args.task_set is not None or args.dry_run:
            parser.error("--check cannot be combined with update-only options")
        return check_notebook(notebook_path)
    assert args.run_id is not None

    results: dict[str, Any] | None = None
    if args.results_json is not None:
        try:
            results = json.loads(args.results_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: Cannot read results JSON: {exc}", file=sys.stderr)
            return 2

    return update_notebook(
        notebook_path,
        args.run_id,
        args.task_set,
        results,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
