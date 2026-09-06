"""Script to build SWE metric backfill sidecars from saved harness reports.

Reads saved harness report JSONs (read-only); never re-evaluates.
Writes sidecar files to data/dev/swe/metric_backfills/.

Usage:
    python -m delegation_study.analysis.build_swe_sidecars

This is a one-time backfill tool for the study team.  It may be re-run
safely (idempotent: sidecars are overwritten, not appended).
"""
from __future__ import annotations

import pathlib
import sys

# Adjust path for direct invocation
_HERE = pathlib.Path(__file__).parent
_REPO_ROOT = _HERE.parent.parent.parent  # src/.. -> root

from delegation_study.analysis.swe_metric_backfill import (  # noqa: E402
    build_all_sidecars,
    build_metric_backfill_report,
    DEFAULT_SIDECAR_DIR,
)

# ---------------------------------------------------------------------------
# Scoped run IDs (frozen at task time — never expand without explicit instruction)
# ---------------------------------------------------------------------------

DEV_RUN_IDS: list[str] = [
    "e6e1461a-60e7-4b96-a879-26fa8bb70eab",  # A0 django/14752 adapter
    "44150f17-23cf-43bd-9c2d-e95718b54c3b",  # A1 django/14752 adapter
    "e10c3450-0916-4504-bf05-80b79701c5e0",  # P0 django/14752 adapter
    "632abbb8-7e0a-4751-94d7-9e935e678d15",  # P1 django/14752 adapter
    "e8d155c7-cb62-4fab-ba12-8ba35404f6b2",  # A0 django/14752 primary (clean)
]

# Batch swe-pilot-20260903T212758Z-<id> — all 12 episodes (all complete)
PILOT_BATCH_ID = "swe-pilot-20260903T212758Z-<id>"

# All 12 pilot run IDs (from batch status.json); includes both finalized and
# not-finalized episodes.  Unfinalized episodes will have no_harness_report_reason
# in their sidecar rather than being excluded.
PILOT_RUN_IDS: list[str] = [
    # django__django-12155
    "7edcc194-507f-4f4a-8d2d-ac427375edc2",  # A0
    "0dfa6211-d192-4422-8985-44933e602a59",  # A1
    "c5162b5f-8159-47f3-9e81-fa86f5e6a165",  # P0
    "d739bba6-6a2a-47fe-8da6-bec31b255c74",  # P1
    # scikit-learn__scikit-learn-14983
    "7a3355a6-615a-4edf-bab6-18ee4e6ae450",  # A0 (not finalized, no harness report)
    "20afecf8-1622-4f2d-a6e4-8bed76f3a204",  # A1 (not finalized)
    "a4733e48-24b0-4718-85d9-27c637e9d60d",  # P0 (not finalized)
    "e7cec8d6-ca3c-4df5-8911-abd63402b07f",  # P1 (not finalized)
    # matplotlib__matplotlib-25479
    "c02c6a54-6094-4c49-a947-4818c05c86b9",  # A0
    "3d486ddc-bbfe-4dbf-a2f8-d894c3ec6e7b",  # A1 (not finalized)
    "4cbd654c-deba-403a-8586-2826a82e7d85",  # P0 (not finalized)
    "546c6930-614a-4125-a8ad-99a51e79d071",  # P1
]

# Source repositories
ADAPTER_REPO_ROOT = pathlib.Path("<REPO>")
PRIMARY_REPO_ROOT = pathlib.Path("<REPO>")


def main() -> int:
    sidecar_dir = ADAPTER_REPO_ROOT / DEFAULT_SIDECAR_DIR
    primary_root = PRIMARY_REPO_ROOT if PRIMARY_REPO_ROOT.is_dir() else None

    print(f"Building sidecars in {sidecar_dir}")
    print(f"Adapter root: {ADAPTER_REPO_ROOT}")
    print(f"Primary root: {primary_root or '(not found)'}")
    print(f"Dev runs: {len(DEV_RUN_IDS)}")
    print(f"Pilot runs: {len(PILOT_RUN_IDS)}")
    print()

    index = build_all_sidecars(
        dev_run_ids=DEV_RUN_IDS,
        pilot_run_ids=PILOT_RUN_IDS,
        batch_id=PILOT_BATCH_ID,
        adapter_repo_root=ADAPTER_REPO_ROOT,
        primary_repo_root=primary_root,
        sidecar_dir=sidecar_dir,
    )

    print(f"\nDone: {index['n_written']} sidecars written, "
          f"{index['n_skipped']} skipped.")
    if index["errors"]:
        print("Errors:")
        for err in index["errors"]:
            print(f"  - {err}")

    # Write backfill report
    report_path = ADAPTER_REPO_ROOT / "data/dev/swe/metric_backfill_report.md"
    build_metric_backfill_report(index, report_path)
    print(f"Wrote {report_path}")

    return 0 if index["n_skipped"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
