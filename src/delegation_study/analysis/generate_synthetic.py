"""
Deterministic synthetic data generator for the delegation study analysis package.

Produces 100 records:
  80 core records — 10 tasks × 4 conditions (A0/A1/P0/P1) × 2 replicates
  20 stock rows  — 10 stock dates × 2 synthetic conditions (SA/SP)

SCHEMA NOTE — STOCK ROWS
-----------
The 20 stock rows CANNOT be episode-schema-valid under the current schema:
  * The condition enum only admits {A0, A1, P0, P1}; SA/SP are not present.
  * There are no stock-market fields (ticker, date, return_agent, etc.)
    in contracts/episode_schema.json, which enforces additionalProperties=false.
These rows therefore use an analysis-specific normalised representation
(plain dicts with clearly named fields) rather than being forced into the
episode schema.  This caveat is intentional and must NOT be resolved by
editing contracts/episode_schema.json, which is frozen for the duration of
the smoke run.

SCHEMA NOTE — CORE ROWS
-----------
All 80 core records are constructed to satisfy contracts/episode_schema.json
(JSON-schema validation) except that every record carries
status="infrastructure_failure".  This is deliberate: the synthetic records
have no real artifact files on disk, and validate_episode() skips its
file-existence checks when status != "complete".  The full_task_success
field in final_outcome is the primary outcome variable used for all
estimand calculations.

PLANTED ESTIMANDS (task-weighted, grand means)
-------------------
  tau_0 = mean(Y_P0) − mean(Y_A0) = −0.10
  tau_1 = mean(Y_P1) − mean(Y_A1) = +0.20
  delta  = tau_1 − tau_0           = +0.30

Design of per-task outcome means (10 tasks, 2 replicates per condition):
  A0: tasks 0-5 → mean 1.0;  tasks 6-9  → mean 0.0   grand mean = 0.60
  P0: tasks 0-4 → mean 1.0;  tasks 5-9  → mean 0.0   grand mean = 0.50
  A1: tasks 0-4 → mean 1.0;  task 5 → 0.5; tasks 6-9 → 0.0  grand mean = 0.55
  P1: tasks 0-6 → mean 1.0;  task 7 → 0.5; tasks 8-9 → 0.0  grand mean = 0.75

SPECIAL EPISODES planted in the 80 core records:
  (syn-task-00, P1, r1) worker fail → parent success   [worker_outcome=0, final=1]
  (syn-task-08, P1, r1) worker success → parent fail   [worker_outcome=1, final=0]
  (syn-task-09, A1, r1) agent failure (worker_timeout)  [QC flag: worker_timeout]
  (syn-task-06, A1, r2) infrastructure failure          [source: infrastructure]
  (syn-task-06, A0, r1) advisory artifact leak          [QC flag: advisory_artifact_leak]
  (syn-task-05, P0, r2) withheld feedback violation     [QC flag: withheld_feedback_call]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# ── planted values ────────────────────────────────────────────────────────────
PLANTED_TAU0:  float = -0.10
PLANTED_TAU1:  float = +0.20
PLANTED_DELTA: float = +0.30

# ── fixed stub hashes (64-char hex) used throughout ──────────────────────────
_MEMO_SHA     = "a" * 64
_SCRIPT_SHA   = "b" * 64
_GIT_COMMIT   = "0" * 40   # benchmark_git_commit (40-char)
_ARCHIVE_SHA  = "c" * 64   # dataset_archive_sha256
_WORKER_PROMPT_SHA = "d" * 64
_PARENT_PROMPT_SHA = "e" * 64
_SELECTION_SHA = "cf818c1c9be7568223245e89a705d94d64aa7d99e4341604414378571f11752d"

# ── shared scaffold values ────────────────────────────────────────────────────
_PROTOCOL = "0.3"
_TASK_SET  = "main"  # synthetic core records represent main-tier data
_WORKER_MODEL = "claude-sonnet-4-5-20250929"
_PARENT_MODEL = "claude-sonnet-4-5-20250929"


def _outcome(
    success: int | None,
    n_units: int = 4,
) -> dict[str, Any]:
    """Build a minimal outcome block."""
    if success is None:
        # evaluator failure — scores must be null
        return {
            "evaluator_status": "evaluator_failure",
            "full_task_success": None,
            "target_unit_pass_rate": None,
            "n_units": 0,
            "output_path": None,
            "evaluator_path": "evaluator.json",
        }
    return {
        "evaluator_status": "ok",
        "full_task_success": success,
        "target_unit_pass_rate": float(success),
        "n_units": n_units,
        "output_path": "output.xlsx",
        "evaluator_path": "evaluator.json",
    }


def _usage_block(
    input_tokens: int = 500,
    output_tokens: int = 100,
    model_calls: int = 3,
    tool_calls: int = 5,
    feedback_calls: int = 0,
) -> dict[str, Any]:
    """Build one role-usage block."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        # Cache accounting remains separate from ordinary input-token usage.
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "validation_cycles": feedback_calls,
        "wall_seconds": 45.0,
        "budget_exhausted": False,
        "budget_exhausted_on": None,
        "safety_exhausted": False,
        "safety_exhausted_on": None,
    }


def _budget_block() -> dict[str, Any]:
    return {
        "max_turns": 12,
        "max_tool_calls": 30,
        "max_wall_seconds": 1200,
        "max_tokens": 8192,
        "max_output_tokens_total": 64000,
        "max_input_tokens_safety": 1500000,
    }


def _make_episode(
    *,
    task_id: str,
    condition: str,
    replicate: int,
    # final outcome — 0=fail, 1=success, None=evaluator_failure
    final_success: int | None,
    # worker outcome for production conditions (P0/P1)
    # None means no worker evaluation (advisory A0/A1)
    worker_success: int | None,
    # failures list
    failures: list[dict[str, Any]] | None = None,
    # compliance overrides
    compliance_overrides: dict[str, Any] | None = None,
    # handoff overrides (e.g., script in advisory)
    handoff_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one episode-schema-compatible synthetic record."""
    run_id = f"syn-{task_id}-{condition}-r{replicate}"

    is_production = condition.startswith("P")
    is_feedback = condition.endswith("1")

    # handoff: production conditions transfer a candidate script
    handoff: dict[str, Any] = {
        "memo_path": "memo.json",
        "memo_sha256": _MEMO_SHA,
        "candidate_script_path": "solution.py" if is_production else None,
        "candidate_script_sha256": _SCRIPT_SHA if is_production else None,
        # submission flags added in protocol v0.2 (schema 7e4ce99)
        "worker_submitted_memo": True,
        "worker_submitted_candidate": is_production,
    }
    if handoff_overrides:
        handoff.update(handoff_overrides)

    # compliance
    worker_feedback_calls = 1 if is_feedback else 0
    compliance: dict[str, Any] = {
        "artifact_crossed_handoff": is_production,
        "worker_feedback_calls": worker_feedback_calls,
        "parent_finalized": True,
        "worker_scratch_destroyed": True,
    }
    if compliance_overrides:
        compliance.update(compliance_overrides)

    # reproducibility
    reproducibility = {
        "schema_git_commit": "<commit>",
        "benchmark_git_commit": _GIT_COMMIT,
        "dataset_archive_sha256": _ARCHIVE_SHA,
        "docker_image_digest": "sha256:synthetic",
        "worker_model": _WORKER_MODEL,
        "parent_model": _PARENT_MODEL,
        "temperature": 0.0,
        "git_dirty": False,
        "workspace_header_used": False,
        "worker_prompt_hash": _WORKER_PROMPT_SHA,
        "parent_prompt_hash": _PARENT_PROMPT_SHA,
        "libreoffice_version": "26.8.0.3",
        "task_selection_package": "config/task_selection_v03",
        "selection_sha256": _SELECTION_SHA,
    }

    # worker_outcome: null for advisory; scored for production
    if worker_success is None:
        worker_outcome: Any = None
    else:
        worker_outcome = _outcome(worker_success)

    record: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "task_set": _TASK_SET,
        "condition": condition,
        "protocol_version": _PROTOCOL,
        "replicate": replicate,
        "started_at": f"2026-09-02T10:{replicate * 10:02d}:00Z",
        "completed_at": f"2026-09-02T10:{replicate * 10 + 5:02d}:00Z",
        "reproducibility": reproducibility,
        "budgets": {
            "worker": _budget_block(),
            "parent": _budget_block(),
        },
        "usage": {
            "worker": _usage_block(feedback_calls=worker_feedback_calls),
            "parent": _usage_block(),
        },
        "handoff": handoff,
        "worker_outcome": worker_outcome,
        "final_outcome": _outcome(final_success),
        # Null: synthetic records never run the unsealed evaluator.
        "unsealed_last_script_score": None,
        "failures": failures or [],
        "compliance": compliance,
        # All synthetic records use infrastructure_failure to avoid file-
        # existence checks in validate_episode (which only run for "complete").
        "status": "infrastructure_failure",
        "analysis_included": True,
        "analysis_inclusion_reason": (
            "Synthetic episode; infrastructure_failure status used to allow "
            "schema validation without real artifact files on disk."
        ),
    }
    return record


# ── Outcome table ─────────────────────────────────────────────────────────────
#
# _OUTCOMES[task_index][condition] = (replicate_1_success, replicate_2_success)
# 0 = fail, 1 = success
#
# This encodes the planted estimand design:
#   A0 grand mean = 6/10 = 0.60
#   P0 grand mean = 5/10 = 0.50  → tau0 = −0.10
#   A1 grand mean = 5.5/10 = 0.55
#   P1 grand mean = 7.5/10 = 0.75 → tau1 = +0.20
#   delta = tau1 − tau0 = +0.30
#
def _build_outcome_table() -> list[dict[str, tuple[int, int]]]:
    rows = []
    for i in range(10):
        a0 = (1, 1) if i <= 5 else (0, 0)
        a1 = (1, 1) if i <= 4 else ((1, 0) if i == 5 else (0, 0))
        p0 = (1, 1) if i <= 4 else (0, 0)
        p1 = (1, 1) if i <= 6 else ((1, 0) if i == 7 else (0, 0))
        rows.append({"A0": a0, "A1": a1, "P0": p0, "P1": p1})
    return rows


_OUTCOME_TABLE = _build_outcome_table()


def generate_core_records() -> list[dict[str, Any]]:
    """Return 80 episode-schema-compatible synthetic records.

    The records embed the planted estimands:
        tau0 = -0.10, tau1 = +0.20, delta = +0.30
    and include six engineered special-case episodes.
    """
    records: list[dict[str, Any]] = []

    for task_i in range(10):
        task_id = f"syn-task-{task_i:02d}"
        outcomes = _OUTCOME_TABLE[task_i]

        for condition in ("A0", "A1", "P0", "P1"):
            r1_success, r2_success = outcomes[condition]
            is_production = condition.startswith("P")

            for replicate, final_success in enumerate([r1_success, r2_success], start=1):
                # Default worker success equals final success for production
                worker_success = final_success if is_production else None

                failures: list[dict[str, Any]] = []
                compliance_overrides: dict[str, Any] | None = None
                handoff_overrides: dict[str, Any] | None = None

                # ── Special episode: worker fail → parent success ────────────
                # task 0, P1, r1: worker_outcome=0, final_outcome=1
                if task_i == 0 and condition == "P1" and replicate == 1:
                    worker_success = 0   # worker failed
                    # final_success stays 1 per outcome table

                # ── Special episode: worker success → parent fail ────────────
                # task 8, P1, r1: worker_outcome=1, final_outcome=0
                elif task_i == 8 and condition == "P1" and replicate == 1:
                    worker_success = 1   # worker succeeded
                    # final_success stays 0 per outcome table

                # ── Special episode: agent failure (worker_timeout) ──────────
                # task 9, A1, r1: agent ran out of turns
                elif task_i == 9 and condition == "A1" and replicate == 1:
                    failures = [{
                        "source": "agent",
                        "code": "worker_timeout",
                        "message": "Worker exceeded max_turns budget (12).",
                    }]

                # ── Special episode: infrastructure failure ──────────────────
                # task 6, A1, r2: container OOM
                elif task_i == 6 and condition == "A1" and replicate == 2:
                    failures = [{
                        "source": "infrastructure",
                        "code": "docker_oom",
                        "message": "Docker container killed (OOM) during worker evaluation.",
                    }]
                    # Infra failure → evaluator could not run → null scores
                    final_success = None

                # ── Special episode: advisory artifact leak (QC flag) ────────
                # task 6, A0, r1: advisory condition leaks a candidate script
                elif task_i == 6 and condition == "A0" and replicate == 1:
                    handoff_overrides = {
                        "candidate_script_path": "solution.py",
                        "candidate_script_sha256": _SCRIPT_SHA,
                    }
                    # compliance.artifact_crossed_handoff should be False for A0
                    # (the script appears but shouldn't have)
                    compliance_overrides = {"artifact_crossed_handoff": False}

                # ── Special episode: withheld feedback violation (QC flag) ───
                # task 5, P0, r2: feedback calls made despite being withheld
                elif task_i == 5 and condition == "P0" and replicate == 2:
                    compliance_overrides = {"worker_feedback_calls": 2}

                record = _make_episode(
                    task_id=task_id,
                    condition=condition,
                    replicate=replicate,
                    final_success=final_success,
                    worker_success=worker_success,
                    failures=failures,
                    compliance_overrides=compliance_overrides,
                    handoff_overrides=handoff_overrides,
                )
                records.append(record)

    assert len(records) == 80, f"Expected 80 core records, got {len(records)}"
    return records


# ── Stock rows ─────────────────────────────────────────────────────────────────
#
# 10 dates × 2 conditions (SA / SP) = 20 rows.
#
# SA = Synthetic-Autonomous : agent trades without human prompting
# SP = Synthetic-Prompted   : agent trades with curated human prompts
#
# These rows use an analysis-specific normalised representation (plain dicts)
# because the current episode schema:
#   (a) does not admit "SA" or "SP" in its condition enum {A0,A1,P0,P1}, and
#   (b) enforces additionalProperties=false, so stock-market fields (ticker,
#       date, return_agent, return_baseline) cannot be added.
#
# The 20 stock rows are therefore SCHEMA-INCOMPATIBLE under
# contracts/episode_schema.json and must not be force-fitted there.
# They are stored separately from the core records in all downstream consumers.
#
_STOCK_DATES = [
    "2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26",
    "2026-02-02", "2026-02-09", "2026-02-16", "2026-02-23",
    "2026-03-02", "2026-03-09",
]

# Planted return values: SP conditions consistently outperform SA by ~0.005
# These are NOT linked to tau0/tau1/delta (different study arm).
_BASE_RETURN_SA = [
    0.012, -0.008, 0.021, 0.005,
    -0.015, 0.018, 0.009, -0.003,
    0.014, -0.011,
]
_BASE_RETURN_SP = [r + 0.005 for r in _BASE_RETURN_SA]


def generate_stock_rows() -> list[dict[str, Any]]:
    """Return 20 analysis-specific stock rows (SA/SP × 10 dates).

    SCHEMA CAVEAT: These rows cannot be episode-schema-valid.
    See module docstring for details.
    """
    rows: list[dict[str, Any]] = []
    for i, date in enumerate(_STOCK_DATES):
        rows.append({
            "record_type": "stock",          # not in episode schema
            "date": date,
            "condition": "SA",              # not in episode schema condition enum
            "return_agent": _BASE_RETURN_SA[i],
            "return_baseline": 0.0,
            "return_delta": _BASE_RETURN_SA[i],
        })
        rows.append({
            "record_type": "stock",
            "date": date,
            "condition": "SP",              # not in episode schema condition enum
            "return_agent": _BASE_RETURN_SP[i],
            "return_baseline": 0.0,
            "return_delta": _BASE_RETURN_SP[i],
        })
    assert len(rows) == 20, f"Expected 20 stock rows, got {len(rows)}"
    return rows


def generate_all() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (core_records, stock_rows) — 80 + 20 = 100 records total."""
    return generate_core_records(), generate_stock_rows()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("paper_outputs/synthetic"),
        help="Directory to write synthetic records JSON files.",
    )
    args = parser.parse_args(argv)
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    core_records, stock_rows = generate_all()

    core_path = out / "core_records.json"
    stock_path = out / "stock_rows.json"

    core_path.write_text(json.dumps(core_records, indent=2) + "\n")
    stock_path.write_text(json.dumps(stock_rows, indent=2) + "\n")

    print(f"Wrote {len(core_records)} core records → {core_path}")
    print(f"Wrote {len(stock_rows)} stock rows    → {stock_path}")
    print()
    print("SCHEMA CAVEAT: The 20 stock rows use an analysis-specific")
    print("representation and are NOT episode-schema-valid.")
    print("See generate_synthetic.py module docstring for details.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
