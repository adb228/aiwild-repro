"""Assert the official answer_position prompt line is identical across cells.

Public events omit prompt text by design. The runner builds the worker and
parent task text from ``data/public/dataset.json`` with a fixed two-line
block:

    ### answer_position
    {value}

This module reconstructs that exact block for every main_v04 cell and
checks that A0/A1/P0/P1 are byte-identical per task. It also scans
persisted events/traces for any copy of the heading and compares
auto-observation sheet+range bytes across cells when present.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from .analyze import load_real_records

ANSWER_POSITION_HEADING = "### answer_position\n"
_HEADING_RE = re.compile(r"### answer_position\n(.*?)(?:\n|$)", re.DOTALL)
_CONDITIONS = ("A0", "A1", "P0", "P1")


def official_answer_position_line(value: str) -> str:
    """Return the exact prompt block used by the runner."""
    return f"{ANSWER_POSITION_HEADING}{value}"


def load_public_answer_positions(dataset_path: Path) -> dict[str, str]:
    """Load only task id → answer_position from public metadata."""
    raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else list(raw.values())
    mapping: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        task_id = item.get("id", item.get("task_id"))
        value = item.get("answer_position")
        if task_id is None or not isinstance(value, str):
            continue
        mapping[str(task_id)] = value
    return mapping


def extract_prompt_answer_position_line(text: str) -> str | None:
    """Return the first heading+value block, or None if the heading is absent."""
    match = _HEADING_RE.search(text)
    if match is None:
        return None
    return f"{ANSWER_POSITION_HEADING}{match.group(1)}"


def _event_observation_line(event: dict[str, Any]) -> str | None:
    payload = event.get("payload") or {}
    observation = payload.get("auto_observation")
    if not isinstance(observation, dict):
        return None
    sheet = observation.get("sheet")
    range_ref = observation.get("range")
    if not isinstance(sheet, str) or not isinstance(range_ref, str):
        return None
    if "!" in range_ref:
        return official_answer_position_line(range_ref)
    quoted = f"'{sheet}'!{range_ref}"
    return official_answer_position_line(quoted)


def scan_run_answer_position_lines(run_dir: Path) -> dict[str, Any]:
    """Scan events and worker_trace for heading bytes and observation ranges."""
    sources = [run_dir / "events.jsonl", run_dir / "logs" / "worker_trace.jsonl"]
    prompt_lines: list[str] = []
    observation_lines: list[str] = []
    for path in sources:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        extracted = extract_prompt_answer_position_line(text)
        if extracted is not None:
            prompt_lines.append(extracted)
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            observed = _event_observation_line(event)
            if observed is not None:
                observation_lines.append(observed)
    return {
        "persisted_prompt_lines": prompt_lines,
        "observation_lines": observation_lines,
    }


def evaluate_answer_position_invariant(
    runs_dir: Path,
    *,
    dataset_path: Path,
) -> dict[str, Any]:
    """Evaluate the four-cell answer_position-line invariant for main_v04."""
    records = load_real_records(runs_dir, "main_v04")
    positions = load_public_answer_positions(dataset_path)
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        if record.get("condition") not in _CONDITIONS:
            continue
        task_id = str(record["task_id"])
        condition = str(record["condition"])
        run_id = str(record["run_id"])
        official = positions.get(task_id)
        if official is None:
            raise ValueError(f"public dataset has no answer_position for {task_id}")
        official_line = official_answer_position_line(official)
        scan = scan_run_answer_position_lines(runs_dir / run_id)
        by_task.setdefault(task_id, {})[condition] = {
            "run_id": run_id,
            "official_line": official_line,
            "official_line_bytes": list(official_line.encode("utf-8")),
            "persisted_prompt_lines": scan["persisted_prompt_lines"],
            "observation_lines": scan["observation_lines"],
        }

    rows: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for task_id in sorted(by_task):
        cells = by_task[task_id]
        missing = [condition for condition in _CONDITIONS if condition not in cells]
        official_lines = {
            condition: cells[condition]["official_line"]
            for condition in _CONDITIONS
            if condition in cells
        }
        unique_official = set(official_lines.values())
        official_identical = len(unique_official) == 1 and not missing
        observation_sets = {
            condition: set(cells[condition]["observation_lines"])
            for condition in _CONDITIONS
            if condition in cells
        }
        nonempty_obs = {
            condition: values
            for condition, values in observation_sets.items()
            if values
        }
        observation_identical = (
            len({frozenset(values) for values in nonempty_obs.values()}) <= 1
        )
        persisted_prompt_found = any(
            cells[condition]["persisted_prompt_lines"]
            for condition in cells
        )
        row = {
            "task_id": task_id,
            "missing_cells": missing,
            "official_identical": official_identical,
            "observation_identical": observation_identical,
            "persisted_prompt_found": persisted_prompt_found,
            "official_line": next(iter(unique_official)) if unique_official else None,
            "A0_run_id": cells.get("A0", {}).get("run_id"),
            "A1_run_id": cells.get("A1", {}).get("run_id"),
            "P0_run_id": cells.get("P0", {}).get("run_id"),
            "P1_run_id": cells.get("P1", {}).get("run_id"),
        }
        rows.append(row)
        if not official_identical or missing:
            mismatches.append(row)

    return {
        "n_tasks": len(rows),
        "n_cells": sum(len(cells) for cells in by_task.values()),
        "all_official_lines_identical": not mismatches and len(rows) == 25,
        "n_mismatches": len(mismatches),
        "mismatches": mismatches,
        "prompt_text_persisted_in_events": any(
            row["persisted_prompt_found"] for row in rows
        ),
        "rows": rows,
    }


def write_answer_position_invariant_report(
    result: dict[str, Any],
    out_dir: Path,
) -> dict[str, Path]:
    """Write markdown, CSV, and JSON reports."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "answer_position_invariant.json"
    csv_path = out_dir / "answer_position_invariant.csv"
    md_path = out_dir / "answer_position_invariant.md"
    json_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fieldnames = [
        "task_id",
        "official_identical",
        "observation_identical",
        "persisted_prompt_found",
        "official_line",
        "A0_run_id",
        "A1_run_id",
        "P0_run_id",
        "P1_run_id",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["rows"])
    status = "PASS" if result["all_official_lines_identical"] else "FAIL"
    lines = [
        "# answer_position prompt-line invariant (main_v04)",
        "",
        f"**Result:** {status}",
        "",
        f"- Tasks: {result['n_tasks']}",
        f"- Cells: {result['n_cells']}",
        f"- Official `### answer_position` line byte-identical across A0/A1/P0/P1: "
        f"{result['all_official_lines_identical']}",
        f"- Mismatches: {result['n_mismatches']}",
        f"- Prompt text persisted in events/traces: "
        f"{result['prompt_text_persisted_in_events']}",
        "",
        "Public `events.jsonl` stores model-call metadata only. The official "
        "line is reconstructed from `data/public/dataset.json` using the same "
        "`### answer_position\\n{value}` block the runner embeds in every "
        "worker and parent user message. Auto-observation `sheet`+`range` "
        "bytes are compared when present.",
        "",
        "| Task | Identical | Official line |",
        "|---|---|---|",
    ]
    for row in result["rows"]:
        mark = "yes" if row["official_identical"] else "NO"
        value = (row["official_line"] or "").replace("\n", " / ")
        lines.append(f"| {row['task_id']} | {mark} | `{value}` |")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "md": md_path}
