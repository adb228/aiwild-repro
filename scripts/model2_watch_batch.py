#!/usr/bin/env python3
"""Write a 10-minute batch snapshot. Designed for tmux + sleep 600.

Never polls from a chat session. Safe to run forever.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BATCHES = _ROOT / "data" / "batches"
_DEV = _ROOT / "data" / "dev" / "model2"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _latest_batch(task_set: str | None) -> Path | None:
    newest: tuple[float, Path] | None = None
    if not _BATCHES.is_dir():
        return None
    for path in _BATCHES.iterdir():
        status = path / "status.json"
        if not status.is_file():
            continue
        try:
            data = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if task_set and data.get("task_set") != task_set:
            continue
        mtime = status.stat().st_mtime
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return None if newest is None else newest[1]


def _remove_stopped_study_containers() -> list[str]:
    """Remove stopped study containers. Never images."""
    removed: list[str] = []
    try:
        out = subprocess.check_output(
            [
                "docker", "ps", "-a",
                "--filter", "status=exited",
                "--format", "{{.ID}} {{.Names}}",
            ],
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return removed
    for line in out.splitlines():
        if not line.strip():
            continue
        cid, _, name = line.partition(" ")
        if "spreadsheet" in name or "delegation" in name or name.startswith("ssb-"):
            subprocess.run(["docker", "rm", cid], check=False, capture_output=True)
            removed.append(name)
    return removed


def snapshot(batch_dir: Path) -> dict:
    status_path = batch_dir / "status.json"
    data = json.loads(status_path.read_text(encoding="utf-8"))
    episodes = data.get("episodes") or []
    counts: dict[str, int] = {}
    for ep in episodes:
        state = str(ep.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {
        "checked_at": _now(),
        "batch_id": data.get("batch_id") or batch_dir.name,
        "task_set": data.get("task_set"),
        "n": len(episodes),
        "counts": counts,
        "status_path": str(status_path),
        "removed_stopped_containers": _remove_stopped_study_containers(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--task-set", default=None)
    args = parser.parse_args()
    batch_dir = (
        _BATCHES / args.batch_id if args.batch_id else _latest_batch(args.task_set)
    )
    _DEV.mkdir(parents=True, exist_ok=True)
    out = _DEV / "WATCH.md"
    if batch_dir is None:
        out.write_text(
            f"# model2 watch\n\nNo batch found at {_now()}.\n",
            encoding="utf-8",
        )
        return 0
    snap = snapshot(batch_dir)
    lines = [
        "# model2 watch",
        "",
        f"- checked_at: `{snap['checked_at']}`",
        f"- batch_id: `{snap['batch_id']}`",
        f"- task_set: `{snap['task_set']}`",
        f"- n: {snap['n']}",
        f"- counts: `{json.dumps(snap['counts'], sort_keys=True)}`",
        f"- status: `{snap['status_path']}`",
        f"- removed_stopped_containers: {snap['removed_stopped_containers']}",
        "",
        "Next check: 10 minutes (tmux `model2-watch`).",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out.read_text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
