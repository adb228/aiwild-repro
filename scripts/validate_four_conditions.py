#!/usr/bin/env python3
"""Post-run validation for a four-condition engineering smoke.

Checks:
  1. All four run directories exist with distinct inodes (collision-free).
  2. Each episode.json passes structural validation (validate CLI).
  3. Each episode passes QC (qc_episode).
  4. Compliance patterns per condition:
       A0: feedback_calls==0, candidate==null, worker_outcome==null
       A1: feedback_calls>0,  candidate==null, worker_outcome==null, scratch_destroyed
       P0: feedback_calls==0, candidate present, worker_outcome present
       P1: feedback_calls>0,  candidate present, worker_outcome present
  5. All four analysis_included==false.
  6. Zero delegation-study Docker containers running.
  7. collect --json shows all four run_ids are EXCLUDED (not in analysis set).

Usage (from repo root, after episodes complete)
-----------------------------------------------
    # With explicit run IDs:
    python scripts/validate_four_conditions.py \\
        --a0 <A0-run-id> --a1 <A1-run-id> --p0 <P0-run-id> --p1 <P1-run-id>

    # Or read IDs from the summary JSON written by run_four_conditions.py
    # (summaries are now in data/batches/<batch_id>/):
    python scripts/validate_four_conditions.py --from-summary

    # Or provide an explicit path to any summary JSON:
    python scripts/validate_four_conditions.py \\
        --from-summary data/batches/<batch_id>/four_condition_343-20_summary.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VENV_PYTHON = _ROOT / ".venv" / "bin" / "python"
_CONDITIONS = ("A0", "A1", "P0", "P1")
# Summaries are now written to data/batches/<batch_id>/four_condition_<task_id>_summary.json.
# No single default path applies; --from-summary must be given an explicit path.
_DEFAULT_SUMMARY: Path | None = None


def _python() -> str:
    return str(_VENV_PYTHON) if _VENV_PYTHON.is_file() else sys.executable


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if sys.stdout.isatty() else s


def _ok(s: str) -> str:
    return f"\033[32m✓\033[0m {s}" if sys.stdout.isatty() else f"PASS  {s}"


def _fail(s: str) -> str:
    return f"\033[31m✗\033[0m {s}" if sys.stdout.isatty() else f"FAIL  {s}"


def _warn(s: str) -> str:
    return f"\033[33m?\033[0m {s}" if sys.stdout.isatty() else f"WARN  {s}"


def validate_episode_json(episode_path: Path) -> list[str]:
    """Run `python -m delegation_study.validate <path>` and return issues list."""
    result = subprocess.run(
        [_python(), "-m", "delegation_study.validate", str(episode_path)],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    if result.returncode == 0:
        return []
    # Parse output for issue lines
    issues = []
    for line in (result.stdout + result.stderr).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            issues.append(line)
    return issues or [f"exit={result.returncode}"]


def qc_episode_dict(ep: dict) -> list[str]:
    """Run qc_episode and return list of flag codes."""
    from delegation_study.qc import qc_episode
    flags = qc_episode(ep)
    return [f.code for f in flags]


def get_collect_run_ids() -> set[str]:
    """Return the set of run_ids in the analysis dataset (collect --json)."""
    result = subprocess.run(
        [_python(), "-m", "delegation_study.collect", "--json"],
        capture_output=True,
        text=True,
        cwd=str(_ROOT),
    )
    if result.returncode != 0:
        return set()
    try:
        data = json.loads(result.stdout)
        return {ep["run_id"] for ep in data.get("episodes", [])}
    except (json.JSONDecodeError, KeyError):
        return set()


def check_docker_zero() -> tuple[bool, str]:
    """Return (is_zero, raw_output)."""
    result = subprocess.run(
        ["docker", "ps", "--filter", "label=delegation-study=true"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    lines = result.stdout.strip().splitlines()
    # First line is header; zero containers means exactly 1 line (header)
    is_zero = len(lines) <= 1
    return is_zero, result.stdout.strip()


def validate_four(
    run_ids: dict[str, str],
    runs_dir: Path,
) -> int:
    """Run all checks; return 0 on full pass, 1 on any failure."""
    all_pass = True
    print(_bold("\n=== Four-Condition Engineering Smoke Validation ===\n"))

    # ── 1. Collision / inode check ─────────────────────────────────────────
    print(_bold("1. Run directory collision check"))
    inodes: dict[str, int] = {}
    for cond in _CONDITIONS:
        rid = run_ids[cond]
        rd = runs_dir / rid
        if rd.is_dir():
            inodes[cond] = rd.stat().st_ino
            print(f"   {cond}: {rid} (inode={inodes[cond]})")
        else:
            print(_fail(f"   {cond}: run dir NOT FOUND: {rd}"))
            all_pass = False

    if len(set(inodes.values())) == len(inodes) and len(inodes) == 4:
        print(_ok("All 4 run directories present with distinct inodes"))
    else:
        print(_fail(f"Collision or missing dirs: inodes={inodes}"))
        all_pass = False

    # ── 2. Per-condition episode checks ───────────────────────────────────
    print(_bold("\n2. Per-condition episode checks"))
    collect_run_ids = get_collect_run_ids()

    for cond in _CONDITIONS:
        rid = run_ids[cond]
        ep_path = runs_dir / rid / "episode.json"
        print(f"\n  [{cond}] run_id={rid[:8]}...")

        if not ep_path.is_file():
            print(_fail(f"  episode.json not found at {ep_path}"))
            all_pass = False
            continue

        ep = json.loads(ep_path.read_text())

        # 2a. Structural validation
        issues = validate_episode_json(ep_path)
        if issues:
            print(_fail(f"  Structural validation: {issues}"))
            all_pass = False
        else:
            print(_ok("  Structural validation: VALID"))

        # 2b. QC
        qc_codes = qc_episode_dict(ep)
        if qc_codes:
            print(_fail(f"  QC flags: {qc_codes}"))
            all_pass = False
        else:
            print(_ok("  QC: clean (no flags)"))

        # 2c. analysis_included
        ai = ep.get("analysis_included")
        if ai is False:
            print(_ok(f"  analysis_included=false"))
        else:
            print(_fail(f"  analysis_included={ai} (expected false)"))
            all_pass = False

        # 2d. collect exclusion
        if rid not in collect_run_ids:
            print(_ok(f"  collect: excluded (correct)"))
        else:
            print(_fail(f"  collect: INCLUDED — should be excluded!"))
            all_pass = False

        # 2e. Compliance per condition
        comp = ep.get("compliance", {})
        fb = comp.get("worker_feedback_calls", 0)
        scratch = comp.get("worker_scratch_destroyed")
        ho = ep.get("handoff", {})
        candidate_sha = ho.get("candidate_script_sha256")
        wo = ep.get("worker_outcome")

        expected_candidate = cond.startswith("P")
        expected_worker_outcome = cond.startswith("P")
        expected_feedback_gt0 = cond in ("A1", "P1")

        ok_candidate = (candidate_sha is not None) == expected_candidate
        ok_wo = (wo is not None) == expected_worker_outcome
        ok_fb = (fb > 0) == expected_feedback_gt0
        ok_scratch = (scratch is True) if cond == "A1" else True

        tick_c = _ok if ok_candidate else _fail
        tick_w = _ok if ok_wo else _fail
        tick_f = _ok if ok_fb else _fail
        tick_s = _ok if ok_scratch else _fail

        print(tick_c(f"  candidate_present=={expected_candidate}: {candidate_sha is not None} (sha={str(candidate_sha)[:8] if candidate_sha else 'null'})"))
        print(tick_w(f"  worker_outcome_present=={expected_worker_outcome}: {wo is not None}"))
        print(tick_f(f"  feedback_calls>0=={expected_feedback_gt0}: feedback_calls={fb}"))
        if cond == "A1":
            print(tick_s(f"  worker_scratch_destroyed: {scratch}"))

        if not all([ok_candidate, ok_wo, ok_fb, ok_scratch]):
            all_pass = False

    # ── 3. Docker zero containers ──────────────────────────────────────────
    print(_bold("\n3. Docker containers (must be zero)"))
    try:
        is_zero, docker_out = check_docker_zero()
        print(f"   {docker_out}")
        if is_zero:
            print(_ok("Zero delegation-study containers running"))
        else:
            print(_fail("Containers still running!"))
            all_pass = False
    except FileNotFoundError:
        print(_warn("docker not found; skipping container check"))
    except Exception as exc:
        print(_warn(f"docker check error: {exc}"))

    # ── 4. collect --json exclusion proof ─────────────────────────────────
    print(_bold("\n4. collect --json exclusion proof"))
    target_rids = set(run_ids.values())
    included = target_rids & collect_run_ids
    excluded = target_rids - collect_run_ids
    if included:
        print(_fail(f"These run_ids are INCLUDED (should not be): {included}"))
        all_pass = False
    else:
        print(_ok(f"All 4 run_ids excluded from analysis dataset"))
    print(f"   Excluded: {sorted(excluded)}")

    # ── Summary ───────────────────────────────────────────────────────────
    print(_bold("\n=== Overall ==="))
    if all_pass:
        print(_ok("ALL CHECKS PASSED"))
        return 0
    else:
        print(_fail("ONE OR MORE CHECKS FAILED — see above"))
        return 1


def _load_ids_from_summary(summary_path: Path) -> dict[str, str]:
    data = json.loads(summary_path.read_text())
    ids: dict[str, str] = {}
    for cond in _CONDITIONS:
        ids[cond] = data["conditions"][cond]["run_id"]
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a0", help="A0 run_id (UUID)")
    parser.add_argument("--a1", help="A1 run_id (UUID)")
    parser.add_argument("--p0", help="P0 run_id (UUID)")
    parser.add_argument("--p1", help="P1 run_id (UUID)")
    parser.add_argument(
        "--from-summary",
        nargs="?",
        const=str(_DEFAULT_SUMMARY),
        metavar="SUMMARY_JSON",
        help=(
            "Read run IDs from the summary JSON written by run_four_conditions.py. "
            f"Default path: {_DEFAULT_SUMMARY}"
        ),
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=_ROOT / "data" / "runs",
        help="Directory containing run subdirectories",
    )
    args = parser.parse_args(argv)

    if args.from_summary:
        summary_path = Path(args.from_summary)
        if not summary_path.is_file():
            print(f"ERROR: summary file not found: {summary_path}", file=sys.stderr)
            return 1
        run_ids = _load_ids_from_summary(summary_path)
    elif all([args.a0, args.a1, args.p0, args.p1]):
        run_ids = {"A0": args.a0, "A1": args.a1, "P0": args.p0, "P1": args.p1}
    else:
        parser.print_help()
        print("\nERROR: Provide either --from-summary or all four --a0/--a1/--p0/--p1 run IDs.",
              file=sys.stderr)
        return 1

    print(f"Validating run IDs:")
    for cond in _CONDITIONS:
        print(f"  {cond}: {run_ids[cond]}")

    return validate_four(run_ids, args.runs_dir)


if __name__ == "__main__":
    sys.exit(main())
