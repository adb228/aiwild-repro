"""
LibreOffice recalculation helper.

Opens each xlsx/xls file in a directory headlessly with LibreOffice Calc,
which forces formula recalculation and caches computed values so that
openpyxl can later read them with data_only=True.

This is a thin, project-integrated wrapper around the logic from
third_party/SpreadsheetBench/evaluation/open_spreadsheet.py, adapted for
use as a Python module rather than a standalone script.

Usage (module)
--------------
    from delegation_study.recalc import recalc_dir, recalc_file

    # Recalculate all xlsx files in a directory (in-place):
    recalc_dir(Path("data/hidden/spreadsheet/13-1"))

    # Recalculate a single file:
    recalc_file(Path("data/hidden/spreadsheet/13-1/1_13-1_golden.xlsx"))

Usage (CLI)
-----------
    python -m delegation_study.recalc --dir data/hidden/spreadsheet/13-1
    python -m delegation_study.recalc --file path/to/file.xlsx
    python -m delegation_study.recalc --dir path/ --soffice /Applications/LibreOffice.app/Contents/MacOS/soffice
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# Supported extensions (in lower-case)
_SPREADSHEET_EXTS = {".xlsx", ".xls"}


# ---------------------------------------------------------------------------
# LibreOffice binary discovery
# ---------------------------------------------------------------------------

def find_libreoffice() -> str | None:
    """Return path to the LibreOffice binary, or None if not found."""
    candidates = [
        "libreoffice",
        "soffice",
        # macOS app bundle
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        # Common Linux installations
        "/usr/bin/libreoffice",
        "/usr/bin/soffice",
        "/usr/local/bin/libreoffice",
    ]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Single-file recalculation
# ---------------------------------------------------------------------------

def recalc_file(
    path: Path,
    soffice: str | None = None,
    timeout: int = 120,
) -> bool:
    """
    Recalculate *path* in-place using LibreOffice.

    Opens the workbook headlessly, converts to xlsx (which forces formula
    evaluation), then overwrites the original file.

    Parameters
    ----------
    path:
        Absolute or relative path to an xlsx or xls file.
    soffice:
        Override for the LibreOffice binary path.  Auto-detected if None.
    timeout:
        Seconds to wait for LibreOffice before killing the process.

    Returns
    -------
    True on success, False on failure (error message printed to stderr).
    """
    path = Path(path).resolve()

    if not path.is_file():
        print(f"recalc: file not found: {path}", file=sys.stderr)
        return False

    if path.suffix.lower() not in _SPREADSHEET_EXTS:
        print(
            f"recalc: unsupported extension {path.suffix!r} for {path.name}",
            file=sys.stderr,
        )
        return False

    soffice_bin = soffice or find_libreoffice()
    if not soffice_bin:
        print(
            "recalc: LibreOffice not found.\n"
            "  Install with:  brew install --cask libreoffice  (macOS)\n"
            "              or  sudo apt install libreoffice-calc  (Linux)\n"
            "  Or pass --soffice /path/to/soffice",
            file=sys.stderr,
        )
        return False

    original_dir = path.parent
    stem = path.stem  # filename without extension

    with tempfile.TemporaryDirectory(prefix="delegation_recalc_") as tmpdir:
        try:
            result = subprocess.run(
                [
                    soffice_bin,
                    "--headless",
                    "--calc",
                    "--convert-to", "xlsx:Calc MS Excel 2007 XML",
                    "--outdir", tmpdir,
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(
                f"recalc: timeout ({timeout}s) processing {path.name}",
                file=sys.stderr,
            )
            return False
        except Exception as exc:
            print(f"recalc: subprocess error for {path.name}: {exc}", file=sys.stderr)
            return False

        if result.returncode != 0:
            print(
                f"recalc: LibreOffice error for {path.name}: {result.stderr.strip()}",
                file=sys.stderr,
            )
            return False

        converted = Path(tmpdir) / (stem + ".xlsx")
        if not converted.is_file():
            print(
                f"recalc: LibreOffice produced no output for {path.name}",
                file=sys.stderr,
            )
            return False

        dest = original_dir / (stem + ".xlsx")
        shutil.move(str(converted), str(dest))

        # If original was .xls, remove it (we now have .xlsx)
        if path.suffix.lower() == ".xls" and path != dest:
            path.unlink(missing_ok=True)

    return True


# ---------------------------------------------------------------------------
# Directory recalculation
# ---------------------------------------------------------------------------

def recalc_dir(
    dir_path: Path,
    soffice: str | None = None,
    timeout: int = 120,
) -> tuple[int, int]:
    """
    Recalculate all xlsx/xls files in *dir_path* (non-recursive).

    Returns (success_count, failure_count).
    """
    dir_path = Path(dir_path)
    if not dir_path.is_dir():
        raise NotADirectoryError(f"recalc: not a directory: {dir_path}")

    files = sorted(
        f for f in dir_path.iterdir()
        if f.is_file() and f.suffix.lower() in _SPREADSHEET_EXTS
    )
    if not files:
        print(f"recalc: no spreadsheet files found in {dir_path}")
        return 0, 0

    soffice_bin = soffice or find_libreoffice()
    print(f"recalc: processing {len(files)} file(s) in {dir_path}")

    success = 0
    failure = 0
    for f in files:
        ok = recalc_file(f, soffice=soffice_bin, timeout=timeout)
        if ok:
            success += 1
        else:
            failure += 1

    print(f"recalc: done — {success} succeeded, {failure} failed")
    return success, failure


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def libreoffice_available(soffice: str | None = None) -> bool:
    """Return True if LibreOffice can be located."""
    return bool(soffice or find_libreoffice())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dir", type=Path, metavar="DIR",
        help="Recalculate all xlsx/xls files in this directory",
    )
    group.add_argument(
        "--file", type=Path, metavar="FILE",
        help="Recalculate a single xlsx/xls file",
    )
    parser.add_argument(
        "--soffice", type=str, default=None,
        help="Override path to the LibreOffice binary",
    )
    parser.add_argument(
        "--timeout", type=int, default=120,
        help="Seconds to wait per file before timeout (default: 120)",
    )
    args = parser.parse_args(argv)

    soffice_bin = args.soffice or find_libreoffice()

    if not soffice_bin:
        print(
            "ERROR: LibreOffice not found.\n"
            "  macOS: brew install --cask libreoffice\n"
            "  Linux: sudo apt install libreoffice-calc\n"
            "  Or pass --soffice /path/to/soffice",
            file=sys.stderr,
        )
        return 1

    if args.file:
        ok = recalc_file(args.file, soffice=soffice_bin, timeout=args.timeout)
        return 0 if ok else 1
    else:
        success, failure = recalc_dir(args.dir, soffice=soffice_bin, timeout=args.timeout)
        return 0 if failure == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
