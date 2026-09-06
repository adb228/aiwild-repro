"""SWE-bench metric backfill: extract and persist F2P/P2P from saved harness reports.

PURPOSE
-------
The SWE-bench runner intentionally sets fail_to_pass_fraction and
pass_to_pass_fraction to null (confidentiality / schema_version 2 reports do
not include per-instance tests_status).  This module implements a study-owned
derived sidecar mechanism that:

  1. Reads the SAVED harness report JSON from each run's reports/ directory
     (read-only; never re-evaluates or modifies source files).
  2. Extracts whatever metrics are available with full provenance (source path,
     report SHA-256, extraction version, timestamp).
  3. Saves a sidecar JSON under data/dev/swe/metric_backfills/<run_id>.json
     and writes an index file at data/dev/swe/metric_backfills/_index.json.
  4. Provides overlay_sidecars_on_records() that the analysis pipeline calls to
     inject sidecar metrics into in-memory copies of records without touching
     source episode.json files.

SCHEMA VERSION 2 NOTE
---------------------
All saved final_report.json files in this study are schema_version 2 (SWE-bench
harness v5).  Schema version 2 reports contain aggregate counts (resolved_instances,
completed_instances, etc.) but do NOT include per-instance tests_status
(FAIL_TO_PASS / PASS_TO_PASS test name lists).  Therefore:

  - fail_to_pass_fraction = null  (reason: "schema_v2_no_tests_status")
  - pass_to_pass_fraction = null  (reason: "schema_v2_no_tests_status")

If a future report exposes tests_status, the VERSION-AWARE PARSING section will
need updating.  The extraction version field in each sidecar documents what
parsing logic was applied.

CANONICAL FIELD
---------------
The canonical primary outcome field is env_metrics.swebench.final_resolved.
The deprecated field name "resolved" (referenced in some spec appendices) is
NOT used.  Any discrepancy between them is noted but the canonical field wins.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
from datetime import datetime, timezone
from typing import Any

# Sidecar schema version — bump when extraction logic changes.
EXTRACTION_VERSION = "1.0.0"

# Default sidecar directory relative to the project root.
DEFAULT_SIDECAR_DIR = pathlib.Path("data/dev/swe/metric_backfills")

# Sentinel for a field that is present but explicitly null.
_NULL = object()


# ---------------------------------------------------------------------------
# Report parsing — version-aware
# ---------------------------------------------------------------------------

def _sha256_file(path: pathlib.Path) -> str:
    """Return hex SHA-256 of file bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_harness_report(
    report_path: pathlib.Path,
    run_id: str,
    instance_id: str,
) -> dict[str, Any]:
    """Parse a saved SWE-bench harness report JSON.

    Returns a dict with keys:
      schema_version, resolved, completed, infra_failure, ambiguous_failure,
      empty_patch, total_instances, fail_to_pass_fraction, pass_to_pass_fraction,
      f2p_numerator, f2p_denominator, p2p_numerator, p2p_denominator,
      f2p_p2p_null_reason.

    Never fabricates denominators.  Missing / empty tests_status => null metrics
    with an explicit reason string.
    """
    if not report_path.is_file():
        raise FileNotFoundError(f"Harness report not found: {report_path}")

    raw_bytes = report_path.read_bytes()
    try:
        report: dict[str, Any] = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed harness report JSON at {report_path}: {exc}") from exc

    schema_version = report.get("schema_version")

    # --- Resolved / completed flags from instance ID lists ---
    resolved_ids: list[str] = report.get("resolved_ids", [])
    completed_ids: list[str] = report.get("completed_ids", [])
    infra_ids: list[str] = report.get("infra_failure_ids", [])
    ambiguous_ids: list[str] = report.get("ambiguous_failure_ids", [])
    empty_ids: list[str] = report.get("empty_patch_ids", [])

    resolved = instance_id in resolved_ids
    completed = instance_id in completed_ids
    infra_failure = instance_id in infra_ids
    ambiguous_failure = instance_id in ambiguous_ids
    empty_patch = instance_id in empty_ids

    # Counts from report
    total_instances = report.get("total_instances", None)

    # --- F2P / P2P extraction: version-aware ---
    #
    # Schema version 2 (SWE-bench harness v5): summary-level report only.
    # No per-instance tests_status field is present.
    # F2P / P2P fractions must remain null.
    #
    # If a future schema version adds tests_status, add a branch here:
    #   elif schema_version == 3: ...
    #
    f2p_fraction: float | None = None
    p2p_fraction: float | None = None
    f2p_numerator: int | None = None
    f2p_denominator: int | None = None
    p2p_numerator: int | None = None
    p2p_denominator: int | None = None
    f2p_p2p_null_reason: str | None = None

    if "tests_status" in report:
        # Future schema: per-instance tests_status is present.
        ts = report["tests_status"]
        f2p_data = ts.get("FAIL_TO_PASS", {})
        p2p_data = ts.get("PASS_TO_PASS", {})

        f2p_success = f2p_data.get("success", [])
        f2p_failure = f2p_data.get("failure", [])
        p2p_success = p2p_data.get("success", [])
        p2p_failure = p2p_data.get("failure", [])

        f2p_num = len(f2p_success)
        f2p_denom = f2p_num + len(f2p_failure)
        p2p_num = len(p2p_success)
        p2p_denom = p2p_num + len(p2p_failure)

        if f2p_denom > 0:
            f2p_fraction = f2p_num / f2p_denom
            f2p_numerator = f2p_num
            f2p_denominator = f2p_denom
        else:
            # Empty denominator: explicitly null, never 0.
            f2p_fraction = None
            f2p_numerator = None
            f2p_denominator = 0
            f2p_p2p_null_reason = "f2p_denominator_zero"

        if p2p_denom > 0:
            p2p_fraction = p2p_num / p2p_denom
            p2p_numerator = p2p_num
            p2p_denominator = p2p_denom
        else:
            p2p_fraction = None
            p2p_numerator = None
            p2p_denominator = 0
            if f2p_p2p_null_reason is None:
                f2p_p2p_null_reason = "p2p_denominator_zero"
    else:
        # Schema version 2 (and any other version without tests_status):
        # cannot compute F2P/P2P.  This is the case for all current saved reports.
        f2p_p2p_null_reason = (
            f"schema_v{schema_version}_no_tests_status"
            if schema_version is not None
            else "no_tests_status_field"
        )

    return {
        "schema_version": schema_version,
        "resolved": resolved,
        "completed": completed,
        "infra_failure": infra_failure,
        "ambiguous_failure": ambiguous_failure,
        "empty_patch": empty_patch,
        "total_instances": total_instances,
        "fail_to_pass_fraction": f2p_fraction,
        "pass_to_pass_fraction": p2p_fraction,
        "f2p_numerator": f2p_numerator,
        "f2p_denominator": f2p_denominator,
        "p2p_numerator": p2p_numerator,
        "p2p_denominator": p2p_denominator,
        "f2p_p2p_null_reason": f2p_p2p_null_reason,
    }


# ---------------------------------------------------------------------------
# Sidecar builder
# ---------------------------------------------------------------------------

def build_sidecar_for_run(
    run_id: str,
    episode: dict[str, Any],
    source_repo_root: pathlib.Path,
) -> dict[str, Any]:
    """Build a metric backfill sidecar dict for a single SWE-bench episode.

    Reads the saved final harness report from
      <source_repo_root>/data/runs/<run_id>/reports/final_report.json
    (or records no_harness_report if absent).

    Returns the sidecar dict (not yet written to disk).

    Parameters
    ----------
    run_id:
        Episode run ID (matches directory name and episode.json run_id).
    episode:
        Parsed episode.json dict (source; not mutated).
    source_repo_root:
        Root of the checkout containing this run's data/runs/<run_id>/.

    Returns
    -------
    dict
        Sidecar with full provenance, extracted metrics, and null reasons.
    """
    run_dir = source_repo_root / "data" / "runs" / run_id
    reports_dir = run_dir / "reports"
    final_report_path = reports_dir / "final_report.json"

    # Episode-level provenance
    task_id: str = episode.get("task_id", "")
    condition: str = episode.get("condition", "")
    task_set: str = episode.get("task_set", "")
    status: str = episode.get("status", "")

    # Canonical primary outcome from episode.json.env_metrics.swebench.final_resolved
    em_swe: dict[str, Any] = (
        episode.get("env_metrics", {}) or {}
    ).get("swebench", {}) or {}

    # The canonical field is final_resolved.
    # Note: some spec appendices refer to "resolved"; actual field is "final_resolved".
    # Code wins over appendix; we use final_resolved.
    episode_final_resolved = em_swe.get("final_resolved")  # may be 0, 1, or None
    episode_candidate_resolved = em_swe.get("candidate_resolved")
    episode_applies_cleanly = em_swe.get("applies_cleanly")

    # Full report SHA from episode.json (for cross-reference)
    episode_final_report_sha256 = em_swe.get("final_report_sha256")

    now_utc = datetime.now(timezone.utc).isoformat()

    # --- Try to load the saved harness report ---
    harness_metrics: dict[str, Any] | None = None
    final_report_sha256: str | None = None
    no_harness_report_reason: str | None = None
    final_harness_report_path_relative: str | None = None

    if final_report_path.is_file():
        # Read only; no writes to source.
        final_report_sha256 = _sha256_file(final_report_path)

        # Verify SHA consistency with episode.json (if episode recorded it)
        episode_sha_prefix = (
            episode_final_report_sha256[:16]
            if episode_final_report_sha256 and len(episode_final_report_sha256) >= 16
            else None
        )
        sha_matches = (
            final_report_sha256.startswith(episode_sha_prefix)
            if episode_sha_prefix
            else None  # unknown / not recorded
        )

        try:
            harness_metrics = _parse_harness_report(
                final_report_path,
                run_id=run_id,
                instance_id=task_id,
            )
            harness_metrics["report_sha256"] = final_report_sha256
            harness_metrics["sha_matches_episode_prefix"] = sha_matches
        except (ValueError, OSError) as exc:
            harness_metrics = None
            no_harness_report_reason = f"parse_error: {exc}"

        # Relative path descriptor (no absolute path in sidecar for portability)
        final_harness_report_path_relative = (
            f"data/runs/{run_id}/reports/final_report.json"
        )
    else:
        # Check if there's only an eval_summary (not_finalized)
        eval_summary_path = reports_dir / "final_eval_summary.json"
        if eval_summary_path.is_file():
            try:
                summary = json.loads(eval_summary_path.read_bytes())
                ev_status = summary.get("evaluator_status", "")
                no_harness_report_reason = (
                    f"no_harness_report_evaluator_status_{ev_status}"
                )
            except (json.JSONDecodeError, OSError):
                no_harness_report_reason = "no_harness_report_eval_summary_unreadable"
        else:
            no_harness_report_reason = "no_harness_report_no_eval_summary"

    # --- Build sidecar ---
    if harness_metrics is not None:
        final_metrics = {
            "fail_to_pass_fraction": harness_metrics["fail_to_pass_fraction"],
            "pass_to_pass_fraction": harness_metrics["pass_to_pass_fraction"],
            "f2p_numerator": harness_metrics["f2p_numerator"],
            "f2p_denominator": harness_metrics["f2p_denominator"],
            "p2p_numerator": harness_metrics["p2p_numerator"],
            "p2p_denominator": harness_metrics["p2p_denominator"],
            "f2p_p2p_null_reason": harness_metrics["f2p_p2p_null_reason"],
            "resolved_from_report": harness_metrics["resolved"],
            "completed_from_report": harness_metrics["completed"],
            "schema_version": harness_metrics["schema_version"],
            "sha_matches_episode_prefix": harness_metrics.get(
                "sha_matches_episode_prefix"
            ),
        }
    else:
        final_metrics = {
            "fail_to_pass_fraction": None,
            "pass_to_pass_fraction": None,
            "f2p_numerator": None,
            "f2p_denominator": None,
            "p2p_numerator": None,
            "p2p_denominator": None,
            "f2p_p2p_null_reason": no_harness_report_reason,
            "resolved_from_report": None,
            "completed_from_report": None,
            "schema_version": None,
            "sha_matches_episode_prefix": None,
        }

    sidecar = {
        "sidecar_schema": "swe_metric_backfill_v1",
        "extraction_version": EXTRACTION_VERSION,
        "generated_at": now_utc,
        "run_id": run_id,
        "task_id": task_id,
        "condition": condition,
        "task_set": task_set,
        "episode_status": status,
        "source_repo_root": str(source_repo_root.resolve()),
        "final_harness_report_path_relative": final_harness_report_path_relative,
        "final_report_sha256": final_report_sha256,
        "episode_final_report_sha256_prefix": episode_final_report_sha256,
        # Canonical primary outcome from episode.json (immutable source)
        "canonical": {
            "final_resolved": episode_final_resolved,
            "candidate_resolved": episode_candidate_resolved,
            "applies_cleanly": episode_applies_cleanly,
            "field_used": "env_metrics.swebench.final_resolved",
            "deprecated_field_note": (
                "Some appendices reference 'env_metrics.swebench.resolved'; "
                "the actual recorded field is 'env_metrics.swebench.final_resolved'. "
                "Code wins; this sidecar uses the actual field."
            ),
        },
        # Metrics extracted from harness report (null if report absent/schema_v2)
        "final_metrics": final_metrics,
        "no_harness_report_reason": no_harness_report_reason,
    }
    return sidecar


def build_all_sidecars(
    dev_run_ids: list[str],
    pilot_run_ids: list[str],
    batch_id: str,
    adapter_repo_root: pathlib.Path,
    primary_repo_root: pathlib.Path | None,
    sidecar_dir: pathlib.Path,
) -> dict[str, Any]:
    """Build and write sidecar files for dev and pilot runs.

    Parameters
    ----------
    dev_run_ids:
        Ordered list of development run IDs to backfill (5 dev records).
    pilot_run_ids:
        Ordered list of pilot run IDs from the batch.
    batch_id:
        Batch identifier string for the pilot batch.
    adapter_repo_root:
        Root of the adapter checkout (contains dev runs).
    primary_repo_root:
        Root of the primary checkout (contains some dev + all pilot runs).
        May be None if not available.
    sidecar_dir:
        Directory to write sidecar JSON files.  Created if absent.

    Returns
    -------
    dict
        Index dict (also written to sidecar_dir/_index.json).
    """
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(timezone.utc).isoformat()

    index_entries: list[dict[str, Any]] = []
    n_written = 0
    n_skipped = 0
    errors: list[str] = []

    def _process_run(
        run_id: str,
        source_roots: list[pathlib.Path],
        scope: str,  # "dev" or "pilot"
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        nonlocal n_written, n_skipped

        # Find the source checkout that has this run's episode.json
        source_root = None
        episode_path = None
        for root in source_roots:
            ep_path = root / "data" / "runs" / run_id / "episode.json"
            if ep_path.is_file():
                source_root = root
                episode_path = ep_path
                break

        if source_root is None or episode_path is None:
            msg = f"episode.json not found for {run_id} in any source root"
            errors.append(msg)
            index_entries.append({
                "run_id": run_id,
                "scope": scope,
                "status": "error",
                "error": msg,
            })
            n_skipped += 1
            return

        try:
            raw = episode_path.read_bytes()
            episode = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"could not load episode for {run_id}: {exc}"
            errors.append(msg)
            index_entries.append({
                "run_id": run_id,
                "scope": scope,
                "status": "error",
                "error": msg,
            })
            n_skipped += 1
            return

        try:
            sidecar = build_sidecar_for_run(run_id, episode, source_root)
        except Exception as exc:
            msg = f"sidecar build failed for {run_id}: {exc}"
            errors.append(msg)
            index_entries.append({
                "run_id": run_id,
                "scope": scope,
                "status": "error",
                "error": msg,
            })
            n_skipped += 1
            return

        if extra_meta:
            sidecar["extra_meta"] = extra_meta

        sidecar_path = sidecar_dir / f"{run_id}.json"
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        n_written += 1

        m = sidecar["final_metrics"]
        index_entries.append({
            "run_id": run_id,
            "scope": scope,
            "task_id": sidecar.get("task_id"),
            "condition": sidecar.get("condition"),
            "task_set": sidecar.get("task_set"),
            "episode_status": sidecar.get("episode_status"),
            "sidecar_file": f"{run_id}.json",
            "status": "ok",
            "final_report_sha256": sidecar.get("final_report_sha256"),
            "final_resolved": sidecar["canonical"]["final_resolved"],
            "applies_cleanly": sidecar["canonical"]["applies_cleanly"],
            "fail_to_pass_fraction": m["fail_to_pass_fraction"],
            "pass_to_pass_fraction": m["pass_to_pass_fraction"],
            "f2p_numerator": m["f2p_numerator"],
            "f2p_denominator": m["f2p_denominator"],
            "p2p_numerator": m["p2p_numerator"],
            "p2p_denominator": m["p2p_denominator"],
            "f2p_p2p_null_reason": m["f2p_p2p_null_reason"],
            "no_harness_report_reason": sidecar.get("no_harness_report_reason"),
        })

    # Source roots ordered by preference: adapter first, then primary
    all_source_roots = [adapter_repo_root]
    if primary_repo_root is not None:
        all_source_roots.append(primary_repo_root)

    for run_id in dev_run_ids:
        _process_run(run_id, all_source_roots, "dev")

    for run_id in pilot_run_ids:
        _process_run(run_id, all_source_roots, "pilot",
                     extra_meta={"batch_id": batch_id})

    index = {
        "index_schema": "swe_metric_backfill_index_v1",
        "extraction_version": EXTRACTION_VERSION,
        "generated_at": now_utc,
        "batch_id": batch_id,
        "n_total": len(dev_run_ids) + len(pilot_run_ids),
        "n_written": n_written,
        "n_skipped": n_skipped,
        "errors": errors,
        "entries": index_entries,
    }
    index_path = sidecar_dir / "_index.json"
    index_path.write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return index


# ---------------------------------------------------------------------------
# Sidecar overlay (analysis path — read-only, no source mutation)
# ---------------------------------------------------------------------------

def load_sidecar(
    sidecar_dir: pathlib.Path,
    run_id: str,
) -> dict[str, Any] | None:
    """Load the sidecar JSON for a run, or return None if absent."""
    sidecar_path = sidecar_dir / f"{run_id}.json"
    if not sidecar_path.is_file():
        return None
    try:
        return json.loads(sidecar_path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return None


def overlay_sidecars_on_records(
    records: list[dict[str, Any]],
    sidecar_dir: pathlib.Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return in-memory copies of records with sidecar metrics overlaid.

    Reads sidecar files from sidecar_dir (default: DEFAULT_SIDECAR_DIR).
    Overlays only `env_metrics.swebench.fail_to_pass_fraction` and
    `env_metrics.swebench.pass_to_pass_fraction` from the sidecar onto a
    DEEP COPY of each record.  The source record dicts are NEVER mutated.

    The overlay is explicit:
    - If a sidecar exists, its final_metrics values are injected.
    - If the source record already has a non-null value for these fields,
      the sidecar does NOT override (source wins; discrepancy is noted).
    - final_resolved is NOT overlaid — the episode.json canonical value
      (already in the record) is used as-is.

    Parameters
    ----------
    records:
        List of loaded SWE episode record dicts.
    sidecar_dir:
        Directory containing sidecar JSONs.  Defaults to DEFAULT_SIDECAR_DIR.

    Returns
    -------
    (records_with_overlays, summary)
        records_with_overlays: list of deep-copied records with overlays applied.
        summary: dict with overlay statistics and per-run details.
    """
    sd = sidecar_dir if sidecar_dir is not None else DEFAULT_SIDECAR_DIR

    n_overlaid = 0
    n_missing_sidecar = 0
    per_run: list[dict[str, Any]] = []
    result_records: list[dict[str, Any]] = []

    for rec in records:
        run_id = rec.get("run_id", "")
        # Deep copy so we never mutate the source record
        rec_copy = copy.deepcopy(rec)

        sidecar = load_sidecar(sd, run_id)
        if sidecar is None:
            n_missing_sidecar += 1
            per_run.append({
                "run_id": run_id,
                "sidecar_found": False,
                "f2p_overlaid": False,
                "p2p_overlaid": False,
            })
            result_records.append(rec_copy)
            continue

        # Ensure nested path env_metrics.swebench exists
        em = rec_copy.setdefault("env_metrics", {})
        em_swe = em.setdefault("swebench", {})

        m = sidecar.get("final_metrics", {})
        sidecar_f2p = m.get("fail_to_pass_fraction")
        sidecar_p2p = m.get("pass_to_pass_fraction")

        # Only overlay if source is null (sidecar doesn't override existing data)
        source_f2p = em_swe.get("fail_to_pass_fraction")
        source_p2p = em_swe.get("pass_to_pass_fraction")

        f2p_overlaid = False
        p2p_overlaid = False
        f2p_discrepancy = None
        p2p_discrepancy = None

        if source_f2p is None and sidecar_f2p is not None:
            em_swe["fail_to_pass_fraction"] = sidecar_f2p
            em_swe["_sidecar_f2p_injected"] = True
            f2p_overlaid = True
        elif source_f2p is not None and sidecar_f2p is not None and source_f2p != sidecar_f2p:
            # Discrepancy: source has a value, sidecar has a different one. Source wins.
            f2p_discrepancy = {
                "source_value": source_f2p,
                "sidecar_value": sidecar_f2p,
                "winner": "source",
            }

        if source_p2p is None and sidecar_p2p is not None:
            em_swe["pass_to_pass_fraction"] = sidecar_p2p
            em_swe["_sidecar_p2p_injected"] = True
            p2p_overlaid = True
        elif source_p2p is not None and sidecar_p2p is not None and source_p2p != sidecar_p2p:
            p2p_discrepancy = {
                "source_value": source_p2p,
                "sidecar_value": sidecar_p2p,
                "winner": "source",
            }

        # Annotate with sidecar provenance (for audit trail)
        em_swe["_sidecar_extraction_version"] = sidecar.get("extraction_version")
        em_swe["_sidecar_generated_at"] = sidecar.get("generated_at")
        em_swe["_f2p_p2p_null_reason"] = m.get("f2p_p2p_null_reason")
        em_swe["_f2p_numerator"] = m.get("f2p_numerator")
        em_swe["_f2p_denominator"] = m.get("f2p_denominator")
        em_swe["_p2p_numerator"] = m.get("p2p_numerator")
        em_swe["_p2p_denominator"] = m.get("p2p_denominator")

        if f2p_overlaid or p2p_overlaid:
            n_overlaid += 1

        per_run.append({
            "run_id": run_id,
            "sidecar_found": True,
            "f2p_overlaid": f2p_overlaid,
            "p2p_overlaid": p2p_overlaid,
            "f2p_discrepancy": f2p_discrepancy,
            "p2p_discrepancy": p2p_discrepancy,
            "f2p_null_reason": m.get("f2p_p2p_null_reason"),
            "final_report_sha256": sidecar.get("final_report_sha256"),
        })
        result_records.append(rec_copy)

    summary = {
        "sidecar_dir": str(sd),
        "n_records": len(records),
        "n_overlaid": n_overlaid,
        "n_missing_sidecar": n_missing_sidecar,
        "per_run": per_run,
    }
    return result_records, summary


# ---------------------------------------------------------------------------
# Markdown backfill report
# ---------------------------------------------------------------------------

def build_metric_backfill_report(
    index: dict[str, Any],
    out_path: pathlib.Path,
) -> None:
    """Write a human-readable Markdown summary of the metric backfill."""
    lines: list[str] = [
        "# SWE Metric Backfill Report",
        "",
        f"- **Extraction version:** {index.get('extraction_version')}",
        f"- **Generated at:** {index.get('generated_at')}",
        f"- **Batch ID:** {index.get('batch_id')}",
        f"- **Total runs scoped:** {index.get('n_total')}",
        f"- **Sidecars written:** {index.get('n_written')}",
        f"- **Skipped / errors:** {index.get('n_skipped')}",
        "",
        "## Schema Note",
        "",
        "All saved `final_report.json` files in this study are SWE-bench harness "
        "**schema_version 2**.  Schema v2 reports contain aggregate counts "
        "(`resolved_instances`, `completed_instances`, etc.) but do **not** "
        "include per-instance `tests_status` (FAIL_TO_PASS / PASS_TO_PASS test "
        "name lists).  Therefore `fail_to_pass_fraction` and "
        "`pass_to_pass_fraction` remain **null** for all runs with "
        "`f2p_p2p_null_reason = schema_v2_no_tests_status`.",
        "",
        "Empty denominators are reported as null with explicit reason; "
        "no fabricated zeros.",
        "",
    ]

    entries = index.get("entries", [])
    dev_entries = [e for e in entries if e.get("scope") == "dev"]
    pilot_entries = [e for e in entries if e.get("scope") == "pilot"]

    def _table_rows(scope_entries: list[dict[str, Any]]) -> list[str]:
        rows: list[str] = [
            "| Run ID | Task ID | Cell | F2P | P2P | F2P num/denom "
            "| P2P num/denom | final_resolved | applies_cleanly | Report SHA-256 | null_reason |",
            "|--------|---------|------|-----|-----|---------------|"
            "---------------|----------------|----------------|----------------|-------------|",
        ]
        for e in scope_entries:
            run_id_short = (e.get("run_id") or "")[:8]
            task_id = e.get("task_id") or "—"
            cond = e.get("condition") or "—"
            f2p = e.get("fail_to_pass_fraction")
            p2p = e.get("pass_to_pass_fraction")
            f2p_str = f"{f2p:.4f}" if f2p is not None else "null"
            p2p_str = f"{p2p:.4f}" if p2p is not None else "null"
            f2p_nd = (
                f"{e.get('f2p_numerator')}/{e.get('f2p_denominator')}"
                if e.get("f2p_denominator") is not None
                else "—"
            )
            p2p_nd = (
                f"{e.get('p2p_numerator')}/{e.get('p2p_denominator')}"
                if e.get("p2p_denominator") is not None
                else "—"
            )
            resolved = e.get("final_resolved")
            applies = e.get("applies_cleanly")
            sha = (e.get("final_report_sha256") or "—")[:16] + "…" if e.get("final_report_sha256") else "—"
            null_reason = e.get("f2p_p2p_null_reason") or e.get("no_harness_report_reason") or "—"
            if e.get("status") != "ok":
                null_reason = f"ERROR: {e.get('error', '?')}"
            rows.append(
                f"| `{run_id_short}` | `{task_id}` | {cond} "
                f"| {f2p_str} | {p2p_str} | {f2p_nd} | {p2p_nd} "
                f"| {resolved} | {applies} | `{sha}` | {null_reason} |"
            )
        return rows

    lines += ["## Development Records (5 runs)", ""]
    lines += _table_rows(dev_entries)
    lines += [""]

    if pilot_entries:
        lines += ["## Pilot Records", ""]
        lines += _table_rows(pilot_entries)
        lines += [""]

    errors = index.get("errors", [])
    if errors:
        lines += ["## Errors", ""]
        for err in errors:
            lines += [f"- {err}"]
        lines += [""]

    lines += [
        "## Provenance",
        "",
        "- Source run files (`episode.json`, `final_report.json`) are **read-only**; "
        "no source files were modified.",
        "- Sidecars live under `data/dev/swe/metric_backfills/` and are "
        "study-owned derived artifacts.",
        "- Canonical `final_resolved` is taken from "
        "`env_metrics.swebench.final_resolved` in each `episode.json`.  "
        "The deprecated field name `resolved` (referenced in some appendices) "
        "is not used.",
        "",
    ]

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
