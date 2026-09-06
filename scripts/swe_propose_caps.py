#!/usr/bin/env python3
"""Propose swebench: caps from a pilot usage CSV (by-role 1.5× rule).

Missing ``budget_exhausted`` is treated as false when status is complete.
Wall and per-call max_tokens are not taken from the proposal: callers must
apply the freeze overrides (wall=1800 safety ceiling; max_tokens=8192).
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

MULTIPLIER = 1.5
MAX_TRUNCATIONS = 1
STEPS = {"turns": 5, "tool_calls": 5, "wall_seconds": 60, "output_tokens": 1000}
MANIFEST = {
    "turns": "max_turns",
    "tool_calls": "max_tool_calls",
    "wall_seconds": "max_wall_seconds",
    "output_tokens": "max_output_tokens_total",
}


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step)) * step


def _int(row: dict, key: str) -> int | None:
    raw = (row.get(key) or "").strip()
    if raw == "":
        return None
    return int(float(raw))


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            turns = _int(row, "turns")
            tools = _int(row, "tool_calls")
            out_tok = _int(row, "output_tokens")
            wall = row.get("wall_seconds", "").strip()
            if turns is None or tools is None or out_tok is None or wall == "":
                print(f"skip row {i}: missing metric", file=sys.stderr)
                continue
            exhausted = str(row.get("budget_exhausted", "")).strip().lower() in {
                "true", "1", "yes",
            }
            rows.append({
                "role": row["role"].strip().lower(),
                "turns": turns,
                "tool_calls": tools,
                "output_tokens": out_tok,
                "wall_seconds": float(wall),
                "budget_exhausted": exhausted,
                "run_id": row.get("run_id", ""),
                "status": row.get("status", ""),
            })
    return rows


def propose_role(role_rows: list[dict], role: str) -> dict[str, int]:
    non_trunc = [r for r in role_rows if not r["budget_exhausted"]]
    if not non_trunc:
        raise ValueError(f"no non-truncated {role} rows")
    proposed: dict[str, int] = {}
    print(f"\n{role.upper()} n={len(role_rows)} non_trunc={len(non_trunc)}")
    for metric in ("turns", "tool_calls", "output_tokens", "wall_seconds"):
        mx = max(float(r[metric]) for r in non_trunc)
        raw = MULTIPLIER * mx
        cap = _round_up(raw, STEPS[metric])
        if cap < mx:
            cap = _round_up(mx, STEPS[metric])
            print(f"  {metric}: raised to observed max {mx} -> {cap}")
        n_cf = sum(1 for r in role_rows if float(r[metric]) >= cap)
        if n_cf > MAX_TRUNCATIONS:
            ordered = sorted((float(r[metric]) for r in role_rows), reverse=True)
            cap = _round_up(ordered[MAX_TRUNCATIONS] + 1e-9, STEPS[metric])
            n_cf = sum(1 for r in role_rows if float(r[metric]) >= cap)
        print(
            f"  {metric}: max_non_trunc={mx} raw={raw:.2f} proposed={cap} "
            f"cf_trunc={n_cf}/{len(role_rows)}"
        )
        if cap < mx:
            raise ValueError(f"{role} {metric} proposed {cap} < observed max {mx}")
        proposed[metric] = cap
    return proposed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()
    rows = load_rows(args.csv)
    worker = propose_role([r for r in rows if r["role"] == "worker"], "worker")
    parent = propose_role([r for r in rows if r["role"] == "parent"], "parent")
    print("\n# Freeze overrides (not from 1.5x): wall=1800, max_tokens=8192")
    print("swebench:")
    print("  caps:")
    for role, caps in (("worker", worker), ("parent", parent)):
        print(f"    {role}:")
        print(f"      max_turns: {caps['turns']}")
        print(f"      max_tool_calls: {caps['tool_calls']}")
        print("      max_wall_seconds: 1800")
        print("      max_tokens: 8192")
        print("      max_input_tokens_safety: 1500000")
        print(f"      max_output_tokens_total: {caps['output_tokens']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
