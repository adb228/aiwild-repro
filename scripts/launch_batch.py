#!/usr/bin/env python3
"""Batch launcher for delegation-study episodes.

Launches one subprocess per (task, cell) pair using the existing runner
``python -m delegation_study.spreadsheetbench.run``.

Usage
-----
    python scripts/launch_batch.py \\
        --tasks 343-20,79-7           # comma-separated IDs or a file path
        --cells A0,A1,P0,P1           # subset of cells (default: all four)
        --task-set dev                # dev | pilot | main  (default: dev)
        --concurrency 4               # max parallel children (default: 4)

Task file formats (auto-detected)
----------------------------------
- JSON:  list of strings or list of dicts with a "task_id" or "id" key
- CSV:   must have a header row; first column named "task_id" or "id" is used
- Plain text: one task ID per line (# comments and blank lines ignored)

Skip logic
----------
A (task_id, condition) pair is skipped only when at least one episode.json in
``data/runs/`` is BOTH structurally valid under the current contracts AND has
``protocol_version`` equal to the current runner protocol (``0.3``).

Historical-protocol (0.1) episodes and structurally-invalid records do not
block a new 0.3 launch.  Malformed or unreadable records never crash
discovery.

Status file
-----------
``data/batches/<batch_id>/status.json`` is written atomically on every state
transition.  Each episode entry records task_id, cell, state, run_id,
wall_seconds, exit_code, and error.

Exit codes
----------
0  — all launched episodes succeeded (or nothing to launch)
1  — preflight failed (nothing was launched)
2  — one or more launched episodes failed
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Project roots
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_RUNS_DIR = _ROOT / "data" / "runs"
_BATCHES_DIR = _ROOT / "data" / "batches"
_MANIFEST = _ROOT / "config" / "manifest.yaml"
_VENV_PYTHON = _ROOT / ".venv" / "bin" / "python"
_RUN_MODULE = "delegation_study.spreadsheetbench.run"

_ALLOWED_CELLS = ("A0", "A1", "P0", "P1", "SOLO")

# ---------------------------------------------------------------------------
# Protocol-aware completion constants
# ---------------------------------------------------------------------------

# Import the current protocol version directly from the runner source so that
# the batch launcher and the runner always agree.  A local literal is kept as
# a safety fallback for interpreter contexts that cannot import the full
# package (e.g. bare-Python invocations outside the venv).
try:
    from delegation_study.spreadsheetbench.run import (  # noqa: PLC0415
        PROTOCOL_VERSION as _CURRENT_PROTOCOL,
    )
except Exception:  # noqa: BLE001 — package unavailable; use inline fallback
    _CURRENT_PROTOCOL: str = "0.3"

# Canonical task_set values; also accepted by the child runner.
# "dev" → "development" both here (for status.json) and by the runner CLI.
_TASK_SET_ALIASES: dict[str, str] = {
    "dev": "development",
    "development": "development",
    "pilot": "pilot",
    "main": "main",
    # v0.4 versioned task sets — resolve to the same canonical selection lists
    # ("main_v04" → main selection, "solo_v04" → solo selection)
    "main_v04": "main_v04",
    "solo_v04": "solo_v04",
    "engineering": "engineering",
    "hard": "hard",
}
# Values accepted as --task-set by this script's CLI
_ACCEPTED_TASK_SETS = tuple(_TASK_SET_ALIASES.keys())


# ---------------------------------------------------------------------------
# Python executable
# ---------------------------------------------------------------------------

def _resolve_python() -> str:
    """Return the venv Python if present, else the current interpreter."""
    if _VENV_PYTHON.is_file():
        return str(_VENV_PYTHON)
    return sys.executable


# ---------------------------------------------------------------------------
# API preflight (injectable for tests)
# ---------------------------------------------------------------------------

def default_api_preflight() -> None:
    """Perform a cheap Anthropic API connectivity check.

    Uses the same environment conventions as model_client.py:
    ANTHROPIC_API_KEY from the gitignored .env file or the shell environment.
    Calls models.list(limit=1) — the cheapest available endpoint.

    Raises
    ------
    RuntimeError
        When the API key is absent or the API call fails.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
        load_dotenv()
    except ImportError:
        pass  # dotenv optional; fall through to env lookup

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found. "
            "Add it to the gitignored .env file or export it in the shell."
        )

    import anthropic  # available in .venv
    # Mirror model_client.py: read workspace ID transiently; inject header iff set.
    # Never log, print, or store the ID.
    _ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if _ws:
        hdr = "anthropic-" + "workspace" + "-id"
        client = anthropic.Anthropic(
            api_key=api_key,
            max_retries=0,
            default_headers={hdr: _ws},
        )
    else:
        client = anthropic.Anthropic(api_key=api_key, max_retries=0)
    del _ws  # discard after construction — never keep in scope
    # models.list is the cheapest read endpoint; limit=1 minimises data transfer.
    client.models.list(limit=1)


def run_api_preflight(
    preflight_fn: Callable[[], None] | None = None,
) -> None:
    """Run the API preflight gate.

    Parameters
    ----------
    preflight_fn:
        Callable that raises ``RuntimeError`` on failure. When ``None``,
        ``default_api_preflight`` is used.  Inject a mock in unit tests to
        avoid real network calls.

    Raises
    ------
    RuntimeError
        Propagated from *preflight_fn* on failure.
    """
    fn = preflight_fn if preflight_fn is not None else default_api_preflight
    fn()


# ---------------------------------------------------------------------------
# Task file / ID resolution
# ---------------------------------------------------------------------------

def _parse_task_file(path: Path) -> list[str]:
    """Read a task-ID list from a file.

    Supported formats (auto-detected by extension then content):
    - JSON: list[str] or list[dict] with a 'task_id' or 'id' key
    - CSV:  header row required; first column named 'task_id' or 'id' is used
    - Plain text: one ID per non-blank, non-comment line
    """
    raw = path.read_text(encoding="utf-8")

    # ── JSON ────────────────────────────────────────────────────────────────
    if path.suffix.lower() == ".json" or raw.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(data, list):
                ids: list[str] = []
                for item in data:
                    if isinstance(item, str):
                        ids.append(item.strip())
                    elif isinstance(item, dict):
                        tid = item.get("task_id") or item.get("id")
                        if tid:
                            ids.append(str(tid).strip())
                return [t for t in ids if t]
            raise ValueError(
                f"JSON task file must be a list; got {type(data).__name__}"
            )

    # ── CSV ─────────────────────────────────────────────────────────────────
    if path.suffix.lower() == ".csv":
        reader = csv.DictReader(io.StringIO(raw))
        col = None
        if reader.fieldnames:
            for candidate in ("task_id", "id"):
                if candidate in reader.fieldnames:
                    col = candidate
                    break
        if col is None:
            # fall back to first column
            col = (reader.fieldnames or [None])[0]
        if col is None:
            raise ValueError("CSV task file has no columns")
        return [str(row[col]).strip() for row in reader if row.get(col, "").strip()]

    # ── Plain text (default) ─────────────────────────────────────────────────
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def resolve_tasks(tasks_arg: str) -> list[str]:
    """Resolve --tasks argument to a list of task IDs.

    Accepts a path to an existing file or a comma-separated list of IDs.
    """
    candidate_path = Path(tasks_arg)
    if candidate_path.is_file():
        return _parse_task_file(candidate_path)
    # Treat as comma-separated IDs
    return [t.strip() for t in tasks_arg.split(",") if t.strip()]


def resolve_requested_tasks(
    tasks_arg: str | None,
    task_set: str,
    *,
    manifest_path: Path | str = _MANIFEST,
) -> list[str]:
    """Resolve explicit tasks or the selected pilot/main package defaults.

    For pilot and main the task list is read from the immutable task-selection
    package.  Hard, development, and engineering task-sets always require an
    explicit --tasks argument because their task lists are not part of the
    package (hard is an exploratory probe with a user-supplied list).
    """
    if tasks_arg is not None:
        return resolve_tasks(tasks_arg)
    if task_set not in {"pilot", "main", "main_v04", "solo_v04"}:
        raise ValueError(
            "--tasks is required for development, engineering, and hard task sets"
        )
    from delegation_study.task_selection import (  # noqa: PLC0415
        load_spreadsheet_selection,
    )
    selection = load_spreadsheet_selection(manifest_path)
    # main_v04 and solo_v04 use the same canonical main selection list
    use_pilot = task_set == "pilot"
    return list(selection.pilot if use_pilot else selection.main)


# ---------------------------------------------------------------------------
# Completed-pair discovery
# ---------------------------------------------------------------------------

def _episode_qualifies_as_completed(data: dict, ep_path: Path) -> bool:
    """Return True iff *data* should cause its (task_id, condition) to be skipped.

    Both of the following must hold:

    1. ``data["protocol_version"]`` equals ``_CURRENT_PROTOCOL`` (``"0.3"``).
    2. The record passes structural validation under the current contracts
       (``validate_episode`` returns no issues).

    Historical-protocol episodes (e.g. ``"0.1"``) and structurally-invalid
    ``"0.3"`` records return ``False`` so that old runs never block a fresh
    0.3 launch.  If the validation package is unavailable (e.g. the script is
    invoked outside the venv), the record is conservatively treated as
    *not* qualifying — the launch proceeds rather than being incorrectly
    suppressed.
    """
    # Fast path: wrong protocol version → never qualifies
    if data.get("protocol_version") != _CURRENT_PROTOCOL:
        return False
    # Structural validation — lazy import so the script degrades gracefully
    # when the package is not on sys.path (import failure → treat as invalid).
    try:
        from delegation_study.validate import (  # noqa: PLC0415
            validate_episode,
        )
        return len(validate_episode(data, ep_path)) == 0
    except Exception:  # noqa: BLE001 — validation unavailable; cannot confirm
        return False


def discover_completed_pairs(
    runs_dir: Path | None = None,
) -> set[tuple[str, str]]:
    """Scan *runs_dir* for (task_id, condition) pairs already recorded.

    Each subdirectory is expected to be a UUID run directory.  A pair counts
    as completed only when a readable ``episode.json`` is found that is BOTH
    structurally valid under the current contracts AND has
    ``protocol_version`` equal to ``_CURRENT_PROTOCOL`` (currently ``"0.3"``).

    Historical-protocol (0.1) or structurally-invalid records do not count as
    completed and do not block a new 0.3 launch.  Malformed or unreadable
    records are silently skipped and never crash discovery.

    SOLO episodes are recognized independently of four-cell (A0/A1/P0/P1)
    records: a valid SOLO record causes (task_id, "SOLO") to be skipped, and
    a valid A0 record does not affect the SOLO skip decision.

    Returns
    -------
    set of (task_id, condition) tuples
    """
    base = runs_dir if runs_dir is not None else _RUNS_DIR
    completed: set[tuple[str, str]] = set()
    if not base.is_dir():
        return completed
    for run_dir in base.iterdir():
        if not run_dir.is_dir():
            continue
        ep_path = run_dir / "episode.json"
        if not ep_path.is_file():
            continue
        try:
            data = json.loads(ep_path.read_text(encoding="utf-8"))
            task_id = data.get("task_id")
            condition = data.get("condition")
            if task_id and condition and _episode_qualifies_as_completed(data, ep_path):
                completed.add((str(task_id), str(condition)))
        except Exception:  # noqa: BLE001 — malformed/unreadable; skip safely
            continue
    return completed


# ---------------------------------------------------------------------------
# Atomic status.json writer
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically via a .tmp sibling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Status management
# ---------------------------------------------------------------------------

def _make_episode_entry(
    task_id: str,
    cell: str,
    run_id: str,
    state: str = "queued",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "cell": cell,
        "run_id": run_id,
        "state": state,
        "wall_seconds": None,
        "exit_code": None,
        "error": None,
    }


def _write_status(
    status_path: Path,
    batch_id: str,
    task_set: str,
    episodes: list[dict[str, Any]],
    *,
    created_at: str,
) -> None:
    """Atomically write the batch status file."""
    _atomic_write_json(status_path, {
        "batch_id": batch_id,
        "task_set": task_set,
        "created_at": created_at,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "episodes": episodes,
    })


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _print_summary(episodes: list[dict[str, Any]], batch_id: str) -> None:
    """Print a readable summary table — no new dependencies."""
    header = f"{'task_id':<20} {'cell':<6} {'state':<10} {'wall_s':>8} {'exit':>6}"
    sep = "-" * len(header)
    print(f"\n{'=' * len(header)}")
    print(f"Batch: {batch_id}")
    print(sep)
    print(header)
    print(sep)
    for ep in episodes:
        wall = f"{ep['wall_seconds']:.1f}" if ep["wall_seconds"] is not None else "—"
        exit_code = str(ep["exit_code"]) if ep["exit_code"] is not None else "—"
        print(
            f"{ep['task_id']:<20} {ep['cell']:<6} {ep['state']:<10} "
            f"{wall:>8} {exit_code:>6}"
        )
    print("=" * len(header))

    total = len(episodes)
    complete = sum(1 for e in episodes if e["state"] == "complete")
    failed = sum(1 for e in episodes if e["state"] == "failed")
    skipped = sum(1 for e in episodes if e["state"] == "skipped")
    queued = sum(1 for e in episodes if e["state"] == "queued")
    print(
        f"Total: {total}  complete: {complete}  failed: {failed}  "
        f"skipped: {skipped}  queued: {queued}"
    )


# ---------------------------------------------------------------------------
# Stale-container orchestrator check (mirrors run_four_conditions.py)
# ---------------------------------------------------------------------------

def _orchestrator_stale_check() -> None:
    """Run stale-container precheck once before spawning any children.

    Filters on BOTH ``delegation-study=true`` AND ``benchmark=spreadsheetbench``
    (Docker AND semantics) so that containers from other benchmarks — e.g.
    ``benchmark=swebench`` — running concurrently are not mistakenly flagged
    as stale Spreadsheet containers.
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", "label=delegation-study=true",
                "--filter", "label=benchmark=spreadsheetbench",
                "--format", r"{{.ID}}\t{{.Names}}\t{{.Status}}",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        print(
            "WARNING: docker not found; skipping stale-container precheck.",
            flush=True,
        )
        return
    except subprocess.TimeoutExpired:
        print("ERROR: docker ps timed out; aborting.", flush=True, file=sys.stderr)
        sys.exit(1)

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if lines:
        print(
            f"ERROR: Found {len(lines)} stale delegation-study+spreadsheetbench container(s).",
            file=sys.stderr, flush=True,
        )
        for ln in lines:
            print(f"  {ln}", file=sys.stderr, flush=True)
        print(
            "Kill them first:  "
            "docker rm -f $(docker ps "
            "--filter label=delegation-study=true "
            "--filter label=benchmark=spreadsheetbench -q)",
            file=sys.stderr, flush=True,
        )
        sys.exit(1)
    print("Stale-container precheck: CLEAN.", flush=True)


def _orchestrator_post_batch_check() -> None:
    """Assert no spreadsheetbench containers remain after the batch.

    Filters on BOTH ``delegation-study=true`` AND ``benchmark=spreadsheetbench``
    so that concurrently-running containers from other benchmarks (e.g.
    ``benchmark=swebench``) are not reported as lingering Spreadsheet containers.

    This is a pure observational check — it never kills anything.  It runs on
    both the normal-completion and exception paths (via ``finally``) so
    leftover containers from any exit path are detected.
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", "label=delegation-study=true",
                "--filter", "label=benchmark=spreadsheetbench",
                "--format", r"{{.ID}}\t{{.Names}}\t{{.Status}}",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        print(
            "WARNING: docker not found; skipping post-batch container check.",
            flush=True,
        )
        return
    except subprocess.TimeoutExpired:
        print(
            "WARNING: docker ps timed out during post-batch container check.",
            file=sys.stderr, flush=True,
        )
        return

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if lines:
        print(
            f"WARNING: {len(lines)} spreadsheetbench container(s) still running "
            "after batch completes:",
            file=sys.stderr, flush=True,
        )
        for ln in lines:
            print(f"  {ln}", file=sys.stderr, flush=True)
    else:
        print(
            "Post-batch container check: CLEAN "
            "(0 spreadsheetbench containers running).",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Core launcher
# ---------------------------------------------------------------------------

def launch_batch(
    *,
    tasks: list[str],
    cells: list[str],
    task_set: str,
    concurrency: int,
    runs_dir: Path | None = None,
    batches_dir: Path | None = None,
    preflight_fn: Callable[[], None] | None = None,
    skip_stale_check: bool = False,
) -> int:
    """Launch a batch of episodes and return an exit code.

    Parameters
    ----------
    tasks:
        Ordered list of task IDs to run.
    cells:
        Ordered list of cells (conditions) to run per task.
    task_set:
        Canonical task-set string (e.g. ``"development"``).
    concurrency:
        Maximum number of child processes active at once.
    runs_dir:
        Override the default ``data/runs/`` directory (for tests).
    batches_dir:
        Override the default ``data/batches/`` directory (for tests).
    preflight_fn:
        Injectable API preflight callable.  ``None`` → ``default_api_preflight``.
    skip_stale_check:
        When True, skips the orchestrator docker stale-container precheck.

    Returns
    -------
    int
        0 on full success, 1 on preflight failure, 2 on any episode failure.
    """
    # ── 1. API preflight FIRST — before creating any directory ────────────────
    print("Running API preflight check...", flush=True)
    try:
        run_api_preflight(preflight_fn)
        print("API preflight: OK", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"API preflight FAILED: {exc}", file=sys.stderr, flush=True)
        return 1  # abort — no batch directory created, no subprocesses spawned

    # ── 2. Validate inputs ────────────────────────────────────────────────────
    bad_cells = [c for c in cells if c not in _ALLOWED_CELLS]
    if bad_cells:
        print(
            f"ERROR: Unknown cells: {bad_cells}. Allowed: {list(_ALLOWED_CELLS)}",
            file=sys.stderr,
        )
        return 1
    if not tasks:
        print("ERROR: No task IDs resolved.", file=sys.stderr)
        return 1

    # ── 3. Discover already-completed (task, cell) pairs ─────────────────────
    completed = discover_completed_pairs(runs_dir)
    print(f"Discovered {len(completed)} completed (task, cell) pairs in data/runs/", flush=True)

    # ── 4. Build episode queue — skip completed pairs ─────────────────────────
    # Deduplicate pairs deterministically (preserve first-occurrence order) so
    # that repeated task IDs or cells do not spawn competing episodes for the
    # same (task, cell) pair or share a single run_id between duplicates.
    _seen_pairs: set[tuple[str, str]] = set()
    all_pairs: list[tuple[str, str]] = []
    for _task_id in tasks:
        for _cell in cells:
            _p = (_task_id, _cell)
            if _p not in _seen_pairs:
                _seen_pairs.add(_p)
                all_pairs.append(_p)

    queue: list[tuple[str, str]] = []
    skipped_entries: list[dict[str, Any]] = []
    for task_id, cell in all_pairs:
        if (task_id, cell) in completed:
            skipped_entries.append({
                "task_id": task_id,
                "cell": cell,
                "run_id": None,
                "state": "skipped",
                "wall_seconds": None,
                "exit_code": None,
                "error": "already completed",
            })
        else:
            queue.append((task_id, cell))

    if skipped_entries:
        print(f"Skipping {len(skipped_entries)} already-completed pair(s).", flush=True)

    # ── 5. Generate batch UUID and per-episode run UUIDs ─────────────────────
    batch_id = str(uuid.uuid4())
    run_ids: dict[tuple[str, str], str] = {
        pair: str(uuid.uuid4()) for pair in queue
    }

    # ── 6. Initialise status file BEFORE spawning ─────────────────────────────
    base_batches = batches_dir if batches_dir is not None else _BATCHES_DIR
    status_path = base_batches / batch_id / "status.json"
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    episodes: list[dict[str, Any]] = list(skipped_entries) + [
        _make_episode_entry(task_id, cell, run_ids[(task_id, cell)])
        for task_id, cell in queue
    ]
    _write_status(status_path, batch_id, task_set, episodes, created_at=created_at)

    print(f"\nBatch ID: {batch_id}", flush=True)
    print(f"Queued:   {len(queue)} episode(s)", flush=True)
    print(f"Skipped:  {len(skipped_entries)} already-completed", flush=True)
    print(f"Status:   {status_path}", flush=True)

    if not queue:
        print("Nothing to launch.", flush=True)
        _print_summary(episodes, batch_id)
        return 0

    # ── 7. Stale-container precheck (once, before spawning) ───────────────────
    if not skip_stale_check:
        _orchestrator_stale_check()

    # ── 8. Launch episodes with concurrency limit ─────────────────────────────
    python_exe = _resolve_python()

    # Index into episodes list for queued entries (skipped entries come first)
    ep_index: dict[tuple[str, str], int] = {
        (ep["task_id"], ep["cell"]): i
        for i, ep in enumerate(episodes)
        if ep["state"] == "queued"
    }

    active: list[tuple[subprocess.Popen, tuple[str, str], float]] = []
    remaining = list(queue)
    any_failed = False

    def _reap_active() -> None:
        """Poll active processes; collect any that finished."""
        nonlocal any_failed
        still_active = []
        for proc, pair, t_start in active:
            retcode = proc.poll()
            if retcode is None:
                still_active.append((proc, pair, t_start))
            else:
                # Finished
                wall = time.monotonic() - t_start
                idx = ep_index[pair]
                episodes[idx]["state"] = "complete" if retcode == 0 else "failed"
                episodes[idx]["wall_seconds"] = round(wall, 3)
                episodes[idx]["exit_code"] = retcode
                if retcode != 0:
                    episodes[idx]["error"] = f"subprocess exited {retcode}"
                    any_failed = True
                    print(
                        f"  FAILED {pair[0]} {pair[1]}  exit={retcode}  "
                        f"wall={wall:.1f}s",
                        flush=True,
                    )
                else:
                    print(
                        f"  OK     {pair[0]} {pair[1]}  exit=0  wall={wall:.1f}s",
                        flush=True,
                    )
                _write_status(
                    status_path, batch_id, task_set, episodes,
                    created_at=created_at,
                )
        active.clear()
        active.extend(still_active)

    try:
        while remaining or active:
            # Drain any finished processes first
            _reap_active()

            # Spawn up to concurrency limit
            while remaining and len(active) < concurrency:
                pair = remaining.pop(0)
                task_id, cell = pair
                run_id = run_ids[pair]

                cmd = [
                    python_exe, "-m", _RUN_MODULE,
                    "--task-id", task_id,
                    "--condition", cell,
                    "--task-set", task_set,  # runner resolves "dev" alias
                    "--run-id", run_id,
                    "--batch-id", batch_id,
                    "--skip-stale-check",  # orchestrator already checked above
                ]

                # Update state to running before Popen
                idx = ep_index[pair]
                episodes[idx]["state"] = "running"
                _write_status(
                    status_path, batch_id, task_set, episodes,
                    created_at=created_at,
                )

                print(f"  Spawning {task_id} {cell}  run_id={run_id[:8]}...", flush=True)
                t_start = time.monotonic()
                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(_ROOT),
                        env={**os.environ, "PYTHONUNBUFFERED": "1"},
                        # Inherit stdout/stderr so output flows to the terminal/log.
                        # shell=False (default) — required by spec.
                    )
                    active.append((proc, pair, t_start))
                except Exception as popen_exc:  # noqa: BLE001
                    # Popen itself raised — transition to failed atomically so the
                    # entry is never left in the "running" state.
                    wall = time.monotonic() - t_start
                    episodes[idx]["state"] = "failed"
                    episodes[idx]["wall_seconds"] = round(wall, 3)
                    episodes[idx]["exit_code"] = None
                    episodes[idx]["error"] = f"Popen failed: {popen_exc}"
                    any_failed = True
                    print(
                        f"  POPEN FAILED {task_id} {cell}: {popen_exc}",
                        flush=True,
                        file=sys.stderr,
                    )
                    _write_status(
                        status_path, batch_id, task_set, episodes,
                        created_at=created_at,
                    )

            if active:
                time.sleep(0.5)

        # Final reap
        _reap_active()

    finally:
        # ── 9a. Post-batch container assertion ───────────────────────────────────
        # Runs on both normal-completion and exception paths so no spreadsheetbench
        # containers go undetected after the batch exits.  Uses the same two-label
        # filter as the pre-check so swebench containers are not reported as stale.
        # Skipped when skip_stale_check=True (same gate as the pre-check) for test
        # environments that do not have Docker available.
        if not skip_stale_check:
            _orchestrator_post_batch_check()

    # ── 9. Print summary ──────────────────────────────────────────────────────
    _print_summary(episodes, batch_id)

    return 2 if any_failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tasks",
        help=(
            "Task IDs: a path to a file (JSON list, CSV, or plain text) "
            "or a comma-separated list of IDs. If omitted for pilot/main, "
            "tasks come from the package named by manifest.task_selection_package."
        ),
    )
    parser.add_argument(
        "--cells",
        default="A0,A1,P0,P1",
        help=(
            "Comma-separated conditions to run per task "
            "(default: A0,A1,P0,P1). Pass SOLO to run the SOLO condition; "
            "default does not include SOLO so existing batches are unaffected."
        ),
    )
    parser.add_argument(
        "--task-set",
        default="dev",
        choices=list(_ACCEPTED_TASK_SETS),
        help="Collection tier (default: dev).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        metavar="N",
        help="Maximum parallel child processes (default: 4).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve cells
    cells = [c.strip().upper() for c in args.cells.split(",") if c.strip()]

    # Resolve task_set alias
    task_set = _TASK_SET_ALIASES.get(args.task_set, args.task_set)

    # Resolve tasks
    try:
        tasks = resolve_requested_tasks(
            args.tasks,
            task_set,
            manifest_path=_MANIFEST,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR resolving tasks: {exc}", file=sys.stderr)
        return 1

    if not tasks:
        print("ERROR: --tasks resolved to an empty list.", file=sys.stderr)
        return 1

    if args.concurrency < 1:
        print("ERROR: --concurrency must be at least 1.", file=sys.stderr)
        return 1

    return launch_batch(
        tasks=tasks,
        cells=cells,
        task_set=task_set,
        concurrency=args.concurrency,
    )


if __name__ == "__main__":
    sys.exit(main())
