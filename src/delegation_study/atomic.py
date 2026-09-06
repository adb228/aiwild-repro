"""Atomic file write helpers (write to .tmp sibling, then os.replace).

Provides crash-safe writes for episode.json and other critical artifacts.
``os.replace`` is atomic on POSIX when src and dst are on the same filesystem
(both in the same run directory), so a reader either sees the old file or the
new one — never a partial write.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a ``.tmp`` sibling.

    Creates parent directories if they do not exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Serialize *data* to JSON and write to *path* atomically."""
    content = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
    atomic_write_text(path, content)
