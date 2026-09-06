"""SWE-bench worktree-local batch episode launcher.

Provides ``run_batch`` — a simple bounded-concurrency runner that launches
multiple ``run_episode`` calls in parallel using a thread pool.  This is a
SWE-worktree-local implementation; it does NOT touch the shared Spreadsheet
launcher.
"""

from __future__ import annotations

import concurrent.futures
import logging
import traceback
from typing import Any

from .run import run_episode

log = logging.getLogger(__name__)


def run_batch(
    specs: list[dict[str, Any]],
    *,
    max_workers: int = 2,
) -> list[dict[str, Any]]:
    """Run a list of episode specs with bounded concurrency.

    Parameters
    ----------
    specs:
        Each element is a dict of keyword arguments forwarded to
        ``run_episode`` (task_id, condition, task_set, replicate, …).
    max_workers:
        Maximum number of simultaneous episodes.  Defaults to 2.

    Returns
    -------
    list[dict]
        One result dict per spec (same order), each with keys:
        - ``spec``: the original spec dict
        - ``run_id``: the run_id returned by run_episode, or None
        - ``episode``: the full episode record dict, or None
        - ``error``: exception string if the episode raised, or None
    """
    results: list[dict[str, Any]] = [{}] * len(specs)

    def _run_one(idx: int, spec: dict[str, Any]) -> dict[str, Any]:
        task_id = spec.get("task_id", "?")
        condition = spec.get("condition", "?")
        log.info("batch: start  %s / %s", task_id, condition)
        try:
            ep = run_episode(**spec)
            run_id = ep.get("run_id")
            log.info("batch: finish %s / %s  run_id=%s", task_id, condition, run_id)
            return {"spec": spec, "run_id": run_id, "episode": ep, "error": None}
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error(
                "batch: error  %s / %s: %s\n%s", task_id, condition, exc, tb
            )
            return {"spec": spec, "run_id": None, "episode": None, "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx: dict[concurrent.futures.Future, int] = {}
        for i, spec in enumerate(specs):
            fut = pool.submit(_run_one, i, spec)
            future_to_idx[fut] = i

        for fut in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results[idx] = {
                    "spec": specs[idx],
                    "run_id": None,
                    "episode": None,
                    "error": str(exc),
                }

    return results
