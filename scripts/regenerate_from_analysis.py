#!/usr/bin/env python3
"""Rebuild paper tables from analysis/ CSVs (no data/runs/ required).

Recomputes τ₀, τ₁, δ_Y with the analyzer's task-clustered bootstrap, writes
table1_effects.tex files, asserts documented byte-identity hashes, and
recomputes process A1/A3 where the frozen process episode tables have the
needed columns.
"""
from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from delegation_study.analysis.analyze import (  # noqa: E402
    BOOTSTRAP_DRAWS,
    BOOTSTRAP_SEED,
    V041_BOOTSTRAP_DRAWS,
    V041_BOOTSTRAP_SEED,
    bootstrap_estimands,
    bootstrap_swe_estimands,
    compute_estimands,
    compute_swe_estimands,
    make_table1,
)
from delegation_study.analysis.process import (  # noqa: E402
    ProcessEpisode,
    _cell_rows,
    _merge_sensitivity,
    _theta_rows,
    _write_csv,
    mark_overflow_missing,
    repair_damage_by_cell,
    theta_v,
    verification_by_cell,
)

FROZEN_V03 = "53744ab6d6c47f5545469883ee51a6499a61d06948f19e6d83ff88b9c66dd5f7"
FROZEN_V041 = "6a805057fa117e5eed74fc1e6c5dd516834f5011649b8716a87bbf2e86fb4184"

V041_ITT_NOTE = (
    "ITT includes 57232 A0 as a zero-valued censored agent outcome "
    "(context_window_exceeded)."
)
SWE_ITT_NOTE = (
    "SWE ITT: evaluator_failure and infrastructure_failure are missing, never 0; "
    "pairwise-complete tasks; seed 20260902, $B=10{,}000$."
)

FACTORIAL = ("A0", "A1", "P0", "P1")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None or (isinstance(value, float) and np.isnan(value)):
        return False
    return str(value).strip().lower() in {"true", "1", "yes"}


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none"}:
        return None
    return float(text)


def _opt_bool(value: Any) -> bool | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(value).strip().lower()
    if text in {"", "nan", "none"}:
        return None
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return bool(value)


def _opt_int(value: Any, default: int = 0) -> int:
    parsed = _opt_float(value)
    return default if parsed is None else int(parsed)


def spreadsheet_task_means(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    cell_col = "cell" if "cell" in df.columns else "condition"
    df = df[df[cell_col].isin(FACTORIAL)].copy()
    return pd.DataFrame({
        "task_id": df["task_id"].astype(str),
        "condition": df[cell_col].astype(str),
        "task_mean": df["final_outcome"].astype(float),
    })


def swe_task_means(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    cell_col = "cell" if "cell" in df.columns else "condition"
    means: list[float] = []
    for _, row in df.iterrows():
        if _truthy(row.get("evaluator_failure")) or _truthy(row.get("infrastructure_failure")):
            means.append(float("nan"))
            continue
        val = _opt_float(row.get("final_outcome"))
        means.append(0.0 if val is None else float(val))
    return pd.DataFrame({
        "task_id": df["task_id"].astype(str),
        "condition": df[cell_col].astype(str),
        "task_mean": means,
    })


def write_v03_published_table(
    estimates: dict[str, float],
    boot: dict[str, Any],
    out: Path,
) -> None:
    """Published v0.3 companion table (B=10{,}000 caption form)."""

    def fmt(value: float, signed: bool = False) -> str:
        return f"{value:+.3f}" if signed else f"{value:.3f}"

    tex = (
        "% Table 1: Main effects (task-clustered bootstrap, seed=20260902, B=10000)\n"
        "\\begin{table}[ht]\n"
        "\\centering\n"
        "\\caption{Estimated treatment effects (task-clustered bootstrap, "
        "$B=10{,}000$, seed \\texttt{20260902}).\n"
        "  $\\hat\\tau_0$: effect of crossing a candidate script into the handoff "
        "when feedback was withheld.\n"
        "  $\\hat\\tau_1$: effect of crossing a candidate script when feedback "
        "was available.\n"
        "  $\\hat\\delta$: interaction (difference of contrasts).\n"
        "  95\\,\\% percentile bootstrap CI.}\n"
        "\\label{tab:effects}\n"
        "\\begin{tabular}{lccccc}\n"
        "\\toprule\n"
        "Estimand & Point est.\\ & Bootstrap SE & 95\\,\\% CI (lo) & "
        "95\\,\\% CI (hi) \\\\\n"
        "\\midrule\n"
        f"  $\\hat\\tau_0$ (P0 $-$ A0) & ${fmt(estimates['tau0'], True)}$ & "
        f"${fmt(boot['tau0_se'])}$ & ${fmt(boot['tau0_ci'][0], True)}$ & "
        f"${fmt(boot['tau0_ci'][1], True)}$ \\\\\n"
        f"  $\\hat\\tau_1$ (P1 $-$ A1) & ${fmt(estimates['tau1'], True)}$ & "
        f"${fmt(boot['tau1_se'])}$ & ${fmt(boot['tau1_ci'][0], True)}$ & "
        f"${fmt(boot['tau1_ci'][1], True)}$ \\\\\n"
        f"  $\\hat\\delta$ (interaction) & ${fmt(estimates['delta'], True)}$ & "
        f"${fmt(boot['delta_se'])}$ & ${fmt(boot['delta_ci'][0], True)}$ & "
        f"${fmt(boot['delta_ci'][1], True)}$ \\\\\n"
        "\\bottomrule\n"
        "\\end{tabular}\n"
        "\\end{table}\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tex, encoding="utf-8")
    print(f"  wrote {out}")


def assert_identity(path: Path, expected: str, *, label: str) -> bool:
    digest = sha256_file(path)
    ok = digest == expected
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label} {path.relative_to(ROOT)}")
    print(f"         got {digest}")
    print(f"         exp {expected}")
    return ok


def report_diff(path: Path, expected_hex: str) -> None:
    """Show a unified diff against the documented frozen bytes if a sibling exists."""
    print(f"  identity failed for {path}; not fudging numbers.")
    print(f"  documented hash {expected_hex}")
    print(f"  written hash    {sha256_file(path)}")
    print("  written file:")
    print(path.read_text(encoding="utf-8"))


def episodes_from_process_csv(path: Path) -> list[ProcessEpisode]:
    episodes: list[ProcessEpisode] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            episodes.append(
                ProcessEpisode(
                    environment=row["environment"],
                    run_id=row["run_id"],
                    task_id=str(row["task_id"]),
                    condition=row["condition"],
                    run_dir=row.get("run_dir") or "",
                    candidate_success=_opt_float(row.get("candidate_success")),
                    final_success=_opt_float(row.get("final_success")),
                    candidate_hash=row.get("candidate_hash") or None,
                    final_hash=row.get("final_hash") or None,
                    disposition=row.get("disposition") or "",
                    accepted_unchanged_flag=_opt_bool(row.get("accepted_unchanged_flag")),
                    parent_finalized=bool(_opt_bool(row.get("parent_finalized"))),
                    context_overflow=bool(_opt_bool(row.get("context_overflow"))),
                    empty_final=bool(_opt_bool(row.get("empty_final"))),
                    parent_output_tokens=_opt_float(row.get("parent_output_tokens")),
                    parent_input_tokens=_opt_float(row.get("parent_input_tokens")),
                    parent_cache_read_input_tokens=_opt_float(
                        row.get("parent_cache_read_input_tokens")
                    ),
                    parent_cache_creation_input_tokens=_opt_float(
                        row.get("parent_cache_creation_input_tokens")
                    ),
                    parent_model_calls=_opt_float(row.get("parent_model_calls")),
                    executions=_opt_int(row.get("executions")),
                    inspections=_opt_int(row.get("inspections")),
                    inspections_explicit=_opt_int(row.get("inspections_explicit")),
                    inspections_auto=_opt_int(row.get("inspections_auto")),
                    revisions=_opt_int(row.get("revisions")),
                    turns_to_finalize=_opt_float(row.get("turns_to_finalize")),
                    any_verification=bool(_opt_bool(row.get("any_verification"))),
                    missing_cell=bool(_opt_bool(row.get("missing_cell"))),
                )
            )
    return episodes


def process_columns_permit(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open(encoding="utf-8", newline="") as handle:
        header = csv.DictReader(handle).fieldnames or []
    needed = {
        "environment",
        "task_id",
        "condition",
        "candidate_success",
        "final_success",
        "executions",
        "inspections",
        "revisions",
        "parent_output_tokens",
        "any_verification",
    }
    return needed.issubset(set(header))


def write_a1_a3(
    episodes: list[ProcessEpisode],
    out_dir: Path,
    *,
    with_sensitivity: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    primary_a1 = repair_damage_by_cell(episodes)
    primary_a3 = verification_by_cell(episodes)
    primary_theta = theta_v(episodes)
    if with_sensitivity:
        sens = mark_overflow_missing(episodes)
        _write_csv(
            out_dir / "a1_repair_damage.csv",
            _merge_sensitivity(
                primary_a1,
                repair_damage_by_cell(sens),
                key_fields=("condition",),
            ),
        )
        _write_csv(
            out_dir / "a3_verification_by_cell.csv",
            _merge_sensitivity(
                primary_a3,
                verification_by_cell(sens),
                key_fields=("condition",),
            ),
        )
        theta_primary = _theta_rows(primary_theta, "")
        theta_sens = {
            row["metric"]: row
            for row in _theta_rows(theta_v(sens), "")
        }
        merged = []
        for row in theta_primary:
            item = {"metric": row["metric"]}
            for field, value in row.items():
                if field == "metric":
                    continue
                item[f"primary_{field}"] = value
            for field, value in theta_sens.get(row["metric"], {}).items():
                if field == "metric":
                    continue
                item[f"sensitivity_overflow_missing_{field}"] = value
            merged.append(item)
        _write_csv(out_dir / "a3_theta_v.csv", merged)
    else:
        _write_csv(out_dir / "a1_repair_damage.csv", _cell_rows(primary_a1))
        _write_csv(out_dir / "a3_verification_by_cell.csv", _cell_rows(primary_a3))
        _write_csv(out_dir / "a3_theta_v.csv", _theta_rows(primary_theta, ""))
    print(f"  wrote A1/A3 under {out_dir}")


def main() -> int:
    failures: list[str] = []

    v041_csv = ROOT / "analysis" / "episodes_spreadsheet_v041.csv"
    v03_csv = ROOT / "analysis" / "episodes_spreadsheet_v03.csv"
    swe_csv = ROOT / "analysis" / "episodes_swe_v035.csv"

    print("== Spreadsheet v0.4.1  (seed=0, B=5000) ==")
    v041_means = spreadsheet_task_means(v041_csv)
    v041_est = compute_estimands(v041_means)
    v041_boot = bootstrap_estimands(
        v041_means, seed=V041_BOOTSTRAP_SEED, n_draws=V041_BOOTSTRAP_DRAWS
    )
    v041_out = ROOT / "paper_outputs" / "v041" / "table1_effects.tex"
    make_table1(
        v041_est,
        v041_boot,
        v041_out,
        itt_note=V041_ITT_NOTE,
        seed=V041_BOOTSTRAP_SEED,
        n_draws=V041_BOOTSTRAP_DRAWS,
    )
    if not assert_identity(v041_out, FROZEN_V041, label="v041"):
        report_diff(v041_out, FROZEN_V041)
        failures.append("v041 table1_effects.tex")

    print("== Spreadsheet v0.3 companion  (seed=20260902, B=10000) ==")
    v03_means = spreadsheet_task_means(v03_csv)
    v03_est = compute_estimands(v03_means)
    v03_boot = bootstrap_estimands(
        v03_means, seed=BOOTSTRAP_SEED, n_draws=BOOTSTRAP_DRAWS
    )
    v03_out = ROOT / "paper_outputs" / "table1_effects.tex"
    write_v03_published_table(v03_est, v03_boot, v03_out)
    if not assert_identity(v03_out, FROZEN_V03, label="v03"):
        report_diff(v03_out, FROZEN_V03)
        failures.append("table1_effects.tex")

    print("== SWE-0.3.5  (seed=20260902, B=10000, pairwise-complete) ==")
    swe_means = swe_task_means(swe_csv)
    swe_est = compute_swe_estimands(swe_means)
    swe_boot = bootstrap_swe_estimands(
        swe_means, seed=BOOTSTRAP_SEED, n_draws=BOOTSTRAP_DRAWS
    )
    swe_out = ROOT / "paper_outputs" / "swe_main_v035" / "table1_effects.tex"
    make_table1(
        swe_est,
        swe_boot,
        swe_out,
        itt_note=SWE_ITT_NOTE,
        seed=BOOTSTRAP_SEED,
        n_draws=BOOTSTRAP_DRAWS,
    )
    print(
        "  SWE estimates "
        f"τ0={swe_est['tau0']} τ1={swe_est['tau1']} δ={swe_est['delta']} "
        f"n=({swe_est['n_tau0']},{swe_est['n_tau1']},{swe_est['n_delta']})"
    )
    print("  no documented frozen hash for SWE table1_effects.tex")

    process_jobs = [
        (
            ROOT / "analysis" / "process" / "spreadsheet" / "episodes.csv",
            ROOT / "paper_outputs" / "process" / "spreadsheet",
            True,
        ),
        (
            ROOT / "analysis" / "process" / "swe" / "episodes.csv",
            ROOT / "paper_outputs" / "process" / "swe",
            False,
        ),
        (
            ROOT / "analysis" / "process" / "model2" / "spreadsheet" / "episodes.csv",
            ROOT / "paper_outputs" / "process" / "model2" / "spreadsheet",
            True,
        ),
        (
            ROOT / "analysis" / "process" / "model2" / "swe" / "episodes.csv",
            ROOT / "paper_outputs" / "process" / "model2" / "swe",
            False,
        ),
    ]
    print("== Process A1/A3 (CSV columns only; no data/runs/) ==")
    for src, dest, sensitivity in process_jobs:
        if not process_columns_permit(src):
            print(f"  skip {src.relative_to(ROOT)} (columns insufficient)")
            continue
        print(f"  {src.relative_to(ROOT)} → {dest.relative_to(ROOT)}")
        write_a1_a3(
            episodes_from_process_csv(src),
            dest,
            with_sensitivity=sensitivity,
        )

    if failures:
        print("IDENTITY FAILURES:", ", ".join(failures))
        return 1
    print("all documented table1 hashes matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
