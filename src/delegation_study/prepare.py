"""
Prepare the SpreadsheetBench Verified-400 dataset for the delegation study.

Splits the raw archive into:
  data/public/   — init workbooks + task metadata (what agents receive)
  data/hidden/   — golden workbooks (ground truth; NEVER exposed to agents)

File naming convention inside the archive:
  {test_case_idx}_{task_id}_init.xlsx    → public
  {test_case_idx}_{task_id}_golden.xlsx  → hidden
  prompt.txt                              → public (per-task directory)
  dataset.json                            → drives everything

Outputs
-------
  data/public/dataset.json          task metadata without golden paths
  data/public/spreadsheet/<id>/     init workbooks + prompt.txt
  data/hidden/spreadsheet/<id>/     golden workbooks (no metadata)
  data/public/inventory.json        inventory of public tasks
  data/hidden/inventory.json        inventory of hidden (golden) tasks

Usage
-----
  python -m delegation_study.prepare \\
      --archive  third_party/SpreadsheetBench/data/spreadsheetbench_verified_400.tar.gz \\
      --public   data/public \\
      --hidden   data/hidden

Important: data/raw and data/hidden are NEVER mounted into agent sandboxes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Filename pattern recognition
# ---------------------------------------------------------------------------

# Primary pattern: "1_13-1_init.xlsx", "2_467-9_golden.xlsx", "10_task_init.xls"
_FILE_PATTERN = re.compile(
    r"^(?P<idx>\d+)_(?P<task_id>.+?)_(?P<kind>init|golden)\.(xlsx|xls)$",
    re.IGNORECASE,
)

# Secondary pattern: bare "golden.xlsx", "initial.xlsx" (5 tasks in Verified-400)
# "initial" maps to kind "init" for consistency.
_FILE_PATTERN_BARE = re.compile(
    r"^(?P<kind>golden|initial|init)\.(xlsx|xls)$",
    re.IGNORECASE,
)

_KIND_ALIASES = {"initial": "init"}


def _classify_file(filename: str) -> tuple[str | None, str | None, str | None]:
    """
    Return (task_id_from_name_or_None, kind, extension) for a spreadsheet.

    kind is 'init' or 'golden'.  Returns (None, None, None) for non-matching
    files (prompt.txt, dataset.json, etc.).
    """
    name = Path(filename).name
    ext = Path(filename).suffix.lower()

    m = _FILE_PATTERN.match(name)
    if m:
        return m.group("task_id"), m.group("kind").lower(), ext

    m2 = _FILE_PATTERN_BARE.match(name)
    if m2:
        raw_kind = m2.group("kind").lower()
        kind = _KIND_ALIASES.get(raw_kind, raw_kind)
        return None, kind, ext

    return None, None, None


# ---------------------------------------------------------------------------
# SHA-256 helper
# ---------------------------------------------------------------------------

def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Dataclasses for inventory
# ---------------------------------------------------------------------------

@dataclass
class PublicTask:
    id: str
    instruction: str
    instruction_type: str
    answer_position: str
    answer_sheet: str | None
    data_position: str | None
    spreadsheet_path: str          # relative path inside data/public/
    init_files: list[str]          # basenames of init workbooks
    init_sha256: list[str]


@dataclass
class HiddenTask:
    id: str
    answer_position: str
    answer_sheet: str | None
    golden_files: list[str]        # basenames of golden workbooks
    golden_sha256: list[str]
    spreadsheet_path: str          # relative path inside data/hidden/


# ---------------------------------------------------------------------------
# Main split logic
# ---------------------------------------------------------------------------

def prepare(
    archive: Path,
    public_dir: Path,
    hidden_dir: Path,
) -> tuple[list[PublicTask], list[HiddenTask]]:
    """
    Extract *archive* and populate *public_dir* / *hidden_dir*.

    Returns (public_tasks, hidden_tasks) inventory lists.
    """
    if not archive.is_file():
        raise FileNotFoundError(f"Archive not found: {archive}")

    public_dir.mkdir(parents=True, exist_ok=True)
    hidden_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="delegation_prepare_") as tmpdir:
        tmp = Path(tmpdir)

        # ---- Extract archive ----
        print(f"Extracting {archive.name} …")
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(tmp, filter="data")

        # ---- Find root dataset directory (first-level directory) ----
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise ValueError(
                f"Expected exactly one top-level directory in archive, got: {[r.name for r in roots]}"
            )
        dataset_root = roots[0]

        # ---- Load dataset.json ----
        dataset_json_path = dataset_root / "dataset.json"
        if not dataset_json_path.is_file():
            raise FileNotFoundError(
                f"dataset.json not found in archive root {dataset_root}"
            )
        dataset: list[dict[str, Any]] = json.loads(dataset_json_path.read_text())
        print(f"Loaded dataset.json: {len(dataset)} tasks")

        public_tasks: list[PublicTask] = []
        hidden_tasks: list[HiddenTask] = []

        for meta in dataset:
            task_id = str(meta["id"])   # ensure string even if JSON emits an int
            spreadsheet_dir = dataset_root / meta["spreadsheet_path"]

            if not spreadsheet_dir.is_dir():
                print(f"  WARNING: spreadsheet dir missing for {task_id}, skipping")
                continue

            # ---- Classify all files in this task directory ----
            init_files: list[Path] = []
            golden_files: list[Path] = []
            other_files: list[Path] = []

            for f in sorted(spreadsheet_dir.iterdir()):
                if not f.is_file():
                    continue
                _, kind, _ = _classify_file(f.name)
                if kind == "init":
                    init_files.append(f)
                elif kind == "golden":
                    golden_files.append(f)
                else:
                    other_files.append(f)  # prompt.txt etc.

            # ---- Destination directories (flat: public/<task_id>/, hidden/<task_id>/) ----
            pub_spread = public_dir / task_id
            hid_spread = hidden_dir / task_id
            pub_spread.mkdir(parents=True, exist_ok=True)
            hid_spread.mkdir(parents=True, exist_ok=True)

            # ---- Copy init files → public ----
            init_basenames: list[str] = []
            init_hashes: list[str] = []
            for src in init_files:
                dst = pub_spread / src.name
                shutil.copy2(src, dst)
                init_basenames.append(src.name)
                init_hashes.append(_sha256_path(dst))

            # ---- Copy golden files → hidden ----
            golden_basenames: list[str] = []
            golden_hashes: list[str] = []
            for src in golden_files:
                dst = hid_spread / src.name
                shutil.copy2(src, dst)
                golden_basenames.append(src.name)
                golden_hashes.append(_sha256_path(dst))

            # ---- Copy other files (prompt.txt etc.) → public only ----
            for src in other_files:
                shutil.copy2(src, pub_spread / src.name)

            # ---- Build inventory entries ----
            public_tasks.append(
                PublicTask(
                    id=task_id,
                    instruction=meta.get("instruction", ""),
                    instruction_type=meta.get("instruction_type", ""),
                    answer_position=meta.get("answer_position", ""),
                    answer_sheet=meta.get("answer_sheet"),
                    data_position=meta.get("data_position"),
                    spreadsheet_path=task_id,
                    init_files=init_basenames,
                    init_sha256=init_hashes,
                )
            )
            hidden_tasks.append(
                HiddenTask(
                    id=task_id,
                    answer_position=meta.get("answer_position", ""),
                    answer_sheet=meta.get("answer_sheet"),
                    golden_files=golden_basenames,
                    golden_sha256=golden_hashes,
                    spreadsheet_path=task_id,
                )
            )

        # ---- Write public dataset.json (strip golden-path info) ----
        public_meta = [
            {
                "id": t.id,
                "instruction": t.instruction,
                "instruction_type": t.instruction_type,
                "answer_position": t.answer_position,
                **({"answer_sheet": t.answer_sheet} if t.answer_sheet else {}),
                **({"data_position": t.data_position} if t.data_position else {}),
                "spreadsheet_path": t.spreadsheet_path,
                "init_files": t.init_files,
            }
            for t in public_tasks
        ]
        (public_dir / "dataset.json").write_text(
            json.dumps(public_meta, indent=2, ensure_ascii=False) + "\n"
        )

        # ---- Write inventories ----
        (public_dir / "inventory.json").write_text(
            json.dumps([asdict(t) for t in public_tasks], indent=2, ensure_ascii=False) + "\n"
        )
        (hidden_dir / "inventory.json").write_text(
            json.dumps([asdict(t) for t in hidden_tasks], indent=2, ensure_ascii=False) + "\n"
        )

        print(
            f"Done. {len(public_tasks)} public tasks → {public_dir}  "
            f"| {len(hidden_tasks)} hidden tasks → {hidden_dir}"
        )
        return public_tasks, hidden_tasks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path,
        default=Path("third_party/SpreadsheetBench/data/spreadsheetbench_verified_400.tar.gz"),
        help="Path to the Verified-400 tar.gz archive",
    )
    parser.add_argument(
        "--public", type=Path, default=Path("data/public"),
        help="Destination for public (init) files",
    )
    parser.add_argument(
        "--hidden", type=Path, default=Path("data/hidden"),
        help="Destination for hidden (golden) files",
    )
    args = parser.parse_args(argv)

    prepare(
        archive=args.archive,
        public_dir=args.public,
        hidden_dir=args.hidden,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
