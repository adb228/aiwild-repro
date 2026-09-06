"""Collect episode records from the run directory for analysis.

Default behaviour
-----------------
* Loads all ``episode.json`` files from ``data/runs/``.
* Excludes records where ``analysis_included=False`` from the analysis
  collection; those runs are reported in the QC summary with their exclusion
  reason.
* Skips and reports ``.tmp`` files, incomplete directories (no episode.json),
  and malformed/unparseable records.

Diagnostic mode
---------------
Pass ``--include-excluded`` to load excluded records into a separate list
without mixing them into the default analysis output.

CLI usage
---------
    python -m delegation_study.collect [--runs-dir PATH] [--include-excluded] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[2]
_RUNS_DEFAULT = _ROOT / "data" / "runs"


@dataclass
class CollectionSummary:
    """Summary of a collection pass."""
    included: list[dict[str, Any]]      # analysis_included=True records
    excluded: list[dict[str, Any]]      # analysis_included=False records (diagnostic only)
    skipped: list[dict[str, str]]       # malformed/incomplete runs with run_dir + reason
    excluded_summary: list[dict[str, str]]  # run_id + reason for quarantined runs


def collect_runs(
    runs_dir: Path = _RUNS_DEFAULT,
    *,
    include_excluded: bool = False,
) -> CollectionSummary:
    """Collect episode records from ``runs_dir``.

    Parameters
    ----------
    runs_dir:
        Directory containing one subdirectory per run, each with an
        ``episode.json``.
    include_excluded:
        If True, populate the ``excluded`` list in the returned summary
        (for diagnostic inspection).  Excluded records are NEVER mixed
        into the ``included`` list regardless of this flag.

    Returns
    -------
    CollectionSummary
        Included records, excluded records (if requested), skipped run info,
        and a quarantine summary listing excluded run IDs and reasons.
    """
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    excluded_summary: list[dict[str, str]] = []

    if not runs_dir.is_dir():
        return CollectionSummary(
            included=included,
            excluded=excluded,
            skipped=[{"run_dir": str(runs_dir), "reason": "runs directory does not exist"}],
            excluded_summary=excluded_summary,
        )

    for run_dir in sorted(runs_dir.iterdir()):
        # Skip non-directories and .tmp entries
        if not run_dir.is_dir():
            continue
        if run_dir.name.endswith(".tmp") or run_dir.name.startswith("."):
            skipped.append({
                "run_dir": str(run_dir),
                "reason": "skipped: temporary or hidden directory",
            })
            continue

        episode_path = run_dir / "episode.json"
        if not episode_path.is_file():
            skipped.append({
                "run_dir": str(run_dir),
                "reason": "skipped: no episode.json (incomplete or aborted run)",
            })
            continue

        try:
            record = json.loads(episode_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append({
                "run_dir": str(run_dir),
                "reason": f"skipped: malformed episode.json — {exc}",
            })
            continue

        if not isinstance(record, dict):
            skipped.append({
                "run_dir": str(run_dir),
                "reason": "skipped: episode.json is not a JSON object",
            })
            continue

        # Determine inclusion.  Missing field is treated as included=True to
        # preserve backward compatibility with pre-schema records, but the
        # reason is noted as missing.
        analysis_included = record.get("analysis_included", True)
        analysis_reason = record.get(
            "analysis_inclusion_reason",
            "(legacy record: analysis_inclusion_reason not present)",
        )
        run_id = record.get("run_id", str(run_dir.name))

        if not analysis_included:
            excluded_summary.append({
                "run_id": run_id,
                "run_dir": str(run_dir),
                "reason": analysis_reason,
            })
            if include_excluded:
                excluded.append(record)
        else:
            included.append(record)

    return CollectionSummary(
        included=included,
        excluded=excluded,
        skipped=skipped,
        excluded_summary=excluded_summary,
    )


def print_collection_summary(summary: CollectionSummary, *, as_json: bool = False) -> None:
    """Print the collection summary to stdout."""
    if as_json:
        print(json.dumps({
            "included_count": len(summary.included),
            "excluded_count": len(summary.excluded_summary),
            "skipped_count": len(summary.skipped),
            "excluded_runs": summary.excluded_summary,
            "skipped_runs": summary.skipped,
        }, indent=2))
        return

    print(f"Collection summary:")
    print(f"  Included for analysis : {len(summary.included)}")
    print(f"  Quarantined (excluded): {len(summary.excluded_summary)}")
    print(f"  Skipped (malformed)   : {len(summary.skipped)}")

    if summary.excluded_summary:
        print("\nQuarantined runs (excluded from default analysis):")
        for entry in summary.excluded_summary:
            print(f"  [{entry['run_id']}] {entry['reason']}")

    if summary.skipped:
        print("\nSkipped runs:")
        for entry in summary.skipped:
            print(f"  {entry['run_dir']}: {entry['reason']}")

    if summary.excluded and not summary.excluded_summary:
        # include_excluded was set but no excluded records
        pass
    elif summary.excluded:
        print(f"\n(Diagnostic mode: {len(summary.excluded)} excluded record(s) loaded separately)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect delegation-study episode records for analysis.",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=_RUNS_DEFAULT,
        help="Directory containing run subdirectories (default: data/runs/)",
    )
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help="Load quarantined records into a separate diagnostic list (not mixed into analysis)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output summary as JSON",
    )
    args = parser.parse_args(argv)

    summary = collect_runs(
        runs_dir=args.runs_dir,
        include_excluded=args.include_excluded,
    )
    print_collection_summary(summary, as_json=args.as_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
