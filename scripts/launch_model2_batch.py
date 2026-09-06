#!/usr/bin/env python3
"""Launch a model-2 Spreadsheet batch (OpenAI / gpt-5.6-luna).

Sets DELEGATION_MODEL2=1, uses OpenAI preflight, and records 429 backoff
notes. Does not launch SWE. Does not merge to main.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "src"))

import launch_batch as lb  # noqa: E402
from delegation_study.analysis.model2 import (  # noqa: E402
    MODEL2_SPREADSHEET_TASK_SETS,
    is_model2_task_set,
)

_INFRA_MARKERS = (
    "connection error",
    "apiconnectionerror",
    "overloaded",
    "too many requests",
    "rate limit",
    "ratelimit",
    "error code: 429",
    "status code: 429",
    "error code: 529",
    "error code: 500",
    "error code: 502",
    "error code: 503",
    "error code: 504",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "apitimeouterror",
)


def _is_infra_error(err: str | None) -> bool:
    if not err:
        return False
    low = str(err).lower()
    return any(marker in low for marker in _INFRA_MARKERS)


def _latest_status(task_set: str) -> dict | None:
    batches = _ROOT / "data" / "batches"
    if not batches.is_dir():
        return None
    newest: tuple[float, dict] | None = None
    for path in batches.iterdir():
        status = path / "status.json"
        if not status.is_file():
            continue
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("task_set") != task_set:
            continue
        mtime = status.stat().st_mtime
        if newest is None or mtime > newest[0]:
            newest = (mtime, data)
    return None if newest is None else newest[1]


def _retry_failed_infra(
    *,
    task_set: str,
    cells: list[str],
    concurrency: int,
) -> int:
    """Retry infrastructure failures. Caller bounds total attempts at 3."""
    status = _latest_status(task_set)
    if not status:
        return 0
    failed = [
        ep for ep in (status.get("episodes") or [])
        if ep.get("state") == "failed" and _is_infra_error(ep.get("error"))
    ]
    if not failed:
        return 0
    retry_log = _ROOT / "data" / "dev" / "model2" / "infra_retry.log"
    retry_log.parent.mkdir(parents=True, exist_ok=True)
    tasks = sorted({ep["task_id"] for ep in failed})
    retry_cells = sorted({ep["cell"] for ep in failed})
    with retry_log.open("a", encoding="utf-8") as fh:
        fh.write(
            f"infra retry task_set={task_set} tasks={tasks} cells={retry_cells}\n"
        )
    return lb.launch_batch(
        tasks=tasks,
        cells=retry_cells,
        task_set=task_set,
        concurrency=concurrency,
        preflight_fn=lb.openai_api_preflight,
        skip_stale_check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=None)
    parser.add_argument("--cells", default="A0,A1,P0,P1")
    parser.add_argument(
        "--task-set",
        required=True,
        help="development (smoke) | pilot_m2 | main_v04_m2 | solo_v04_m2",
    )
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--no-skip", action="store_true")
    args = parser.parse_args(argv)

    task_set = lb._TASK_SET_ALIASES.get(args.task_set, args.task_set)
    if task_set not in MODEL2_SPREADSHEET_TASK_SETS and task_set != "development":
        print(
            f"ERROR: model-2 launcher accepts development or {sorted(MODEL2_SPREADSHEET_TASK_SETS)}",
            file=sys.stderr,
        )
        return 1
    if is_model2_task_set(task_set) and task_set.startswith("swe_"):
        print("ERROR: SWE is not launched from this script.", file=sys.stderr)
        return 1

    os.environ["DELEGATION_MODEL2"] = "1"
    guard = subprocess.run(
        [str(_ROOT / ".venv" / "bin" / "python"),
         str(_ROOT / "scripts" / "model2_prelaunch_guards.py")],
        cwd=str(_ROOT),
    )
    if guard.returncode != 0:
        return guard.returncode

    cells = [c.strip().upper() for c in args.cells.split(",") if c.strip()]
    try:
        tasks = lb.resolve_requested_tasks(args.tasks, task_set, manifest_path=lb._MANIFEST)
    except Exception as exc:
        print(f"ERROR resolving tasks: {exc}", file=sys.stderr)
        return 1

    concurrency = max(2, int(args.concurrency))

    rc = lb.launch_batch(
        tasks=tasks,
        cells=cells,
        task_set=task_set,
        concurrency=concurrency,
        preflight_fn=lb.openai_api_preflight,
        skip_stale_check=False,
    )
    # Infrastructure retry ≤3 then missing. Do not retry behavior.
    for _attempt in (2, 3):
        if not _latest_infra_failures(task_set):
            return rc
        rc = _retry_failed_infra(
            task_set=task_set,
            cells=cells,
            concurrency=concurrency,
        )
    if _latest_infra_failures(task_set):
        missing_log = _ROOT / "data" / "dev" / "model2" / "infra_missing.log"
        missing_log.parent.mkdir(parents=True, exist_ok=True)
        with missing_log.open("a", encoding="utf-8") as fh:
            fh.write(
                f"infra retry exhausted (3) task_set={task_set} "
                f"pairs={_latest_infra_failures(task_set)}\n"
            )
    return rc


def _latest_infra_failures(task_set: str) -> list[tuple[str, str]]:
    status = _latest_status(task_set)
    if not status:
        return []
    return [
        (ep.get("task_id"), ep.get("cell"))
        for ep in (status.get("episodes") or [])
        if ep.get("state") == "failed" and _is_infra_error(ep.get("error"))
    ]


if __name__ == "__main__":
    sys.exit(main())
