"""Resolve the immutable task-selection package named by the manifest."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = _ROOT / "config" / "manifest.yaml"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class SpreadsheetSelection:
    """Validated Spreadsheet task-selection metadata."""

    package: str
    selection_path: Path
    selection_sha256: str
    seed: int
    pilot: tuple[str, ...]
    main: tuple[str, ...]


def _task_ids(items: Any, field: str) -> tuple[str, ...]:
    if not isinstance(items, list):
        raise ValueError(f"Spreadsheet selection field {field!r} must be a list")
    ids: list[str] = []
    for item in items:
        raw_id = item.get("id") if isinstance(item, dict) else item
        if raw_id is None or not str(raw_id).strip():
            raise ValueError(f"Spreadsheet selection {field!r} contains an empty ID")
        ids.append(str(raw_id))
    return tuple(ids)


def load_spreadsheet_selection(
    manifest_path: Path | str = _DEFAULT_MANIFEST,
) -> SpreadsheetSelection:
    """Load Spreadsheet selection metadata through ``task_selection_package``.

    Relative package pointers are resolved from the repository root containing
    the manifest's ``config`` directory. The package is read-only.
    """
    manifest_path = Path(manifest_path)
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    package_raw = manifest.get("task_selection_package")
    if not isinstance(package_raw, str) or not package_raw.strip():
        raise ValueError("manifest task_selection_package must be a non-empty string")

    package_pointer = package_raw.strip()
    package_path = Path(package_pointer)
    if not package_path.is_absolute():
        package_path = manifest_path.parent.parent / package_path
    selection_path = package_path / "ss_task_selection.json"
    try:
        data = json.loads(selection_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Spreadsheet task selection not found via manifest pointer: "
            f"{selection_path}"
        ) from exc

    selection_sha256 = data.get("selection_sha256")
    if (
        not isinstance(selection_sha256, str)
        or _SHA256_RE.fullmatch(selection_sha256) is None
    ):
        raise ValueError(
            "Spreadsheet selection_sha256 must be exactly 64 lowercase hex characters"
        )
    seed = data.get("seed")
    if not isinstance(seed, int):
        raise ValueError("Spreadsheet selection seed must be an integer")

    return SpreadsheetSelection(
        package=package_pointer,
        selection_path=selection_path,
        selection_sha256=selection_sha256,
        seed=seed,
        pilot=_task_ids(data.get("pilot"), "pilot"),
        main=_task_ids(data.get("main"), "main"),
    )
