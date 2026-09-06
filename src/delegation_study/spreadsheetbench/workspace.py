"""Task-local workspace management for one run of the delegation study.

Directory layout
----------------
    <base>/
    ├── input/
    │   └── input.xlsx          ← clean task input (never modified)
    ├── output/                 ← Docker writes output.xlsx here
    ├── scratch/                ← A1-only ephemeral copy; destroyed at handoff
    │   ├── input/
    │   │   └── input.xlsx
    │   ├── output/
    │   └── scripts/
    ├── artifacts/
    │   ├── memo.json           ← submitted by worker
    │   └── solution.py         ← submitted by P0/P1 worker; read by parent
    └── scripts/                ← ephemeral script files for sandbox runs

Security invariants
-------------------
* ``input_path`` always points into ``<base>/input/`` — a copy of the
  original, never the raw benchmark file or an evaluator output.
* ``scratch_dir`` is only created on first use; it is entirely removed by
  ``destroy_scratch()``, which sets ``scratch_destroyed = True``.
* Calling ``destroy_scratch()`` a second time is a no-op.
* ``cleanup()`` removes the entire ``<base>`` tree.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TaskWorkspace:
    """All filesystem state for a single run.

    Callers should use the factory ``TaskWorkspace.create()`` rather than
    constructing directly.
    """

    run_id: str
    task_id: str

    # Root directory for this run
    base: Path

    # ── Permanent subdirectories ─────────────────────────────────────────────
    input_dir: Path     # contains the clean input copy
    output_dir: Path    # Docker writes output.xlsx here
    artifacts_dir: Path # memo.json, solution.py
    scripts_dir: Path   # temp scripts for sandbox invocations

    # ── Derived paths ────────────────────────────────────────────────────────
    input_path: Path    # base/input/input.xlsx

    # ── A1 scratch (lazily created) ──────────────────────────────────────────
    _scratch_base: Path = field(init=False)  # base/scratch/
    scratch_destroyed: bool = field(default=False, init=False)

    # ── Compliance tracking ──────────────────────────────────────────────────
    # Count of calls to run_ephemeral_python / run_solution (feedback calls)
    feedback_call_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_scratch_base", self.base / "scratch")

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        run_id: str,
        task_id: str,
        source_input: Path,
        *,
        parent_dir: Path | None = None,
    ) -> "TaskWorkspace":
        """Create a new workspace and copy *source_input* into it.

        Parameters
        ----------
        run_id:     Unique run identifier (used as directory name).
        task_id:    Task identifier (recorded but not used for paths).
        source_input:
            Path to the original input.xlsx on the host.  A clean copy is
            made; the original is never modified.
        parent_dir:
            Directory in which to create the run subdirectory.  If None a
            temporary directory is created and the workspace owns it.
        """
        source_input = source_input.resolve()
        if not source_input.is_file():
            raise FileNotFoundError(f"Source input not found: {source_input}")

        if parent_dir is not None:
            base = Path(parent_dir) / run_id
            base.mkdir(parents=True, exist_ok=False)
            _owns_parent = False
        else:
            _tmp = tempfile.mkdtemp(prefix=f"sbench_{run_id}_")
            base = Path(_tmp)
            _owns_parent = True  # noqa: F841 (used in cleanup)

        # Create subdirectory structure
        input_dir = base / "input"
        output_dir = base / "output"
        artifacts_dir = base / "artifacts"
        scripts_dir = base / "scripts"
        for d in (input_dir, output_dir, artifacts_dir, scripts_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Copy the clean input (never symlink — symlinks would allow
        # the container to follow them back to raw data)
        input_path = input_dir / "input.xlsx"
        shutil.copy2(source_input, input_path)
        # Make input readable by nobody (uid 65534) inside the container
        input_path.chmod(0o644)

        return cls(
            run_id=run_id,
            task_id=task_id,
            base=base,
            input_dir=input_dir,
            output_dir=output_dir,
            artifacts_dir=artifacts_dir,
            scripts_dir=scripts_dir,
            input_path=input_path,
        )

    # ── Scratch (A1 ephemeral execution) ────────────────────────────────────

    @property
    def scratch_dir(self) -> Path:
        """Return the scratch base directory, creating it on first access."""
        if not self._scratch_base.exists():
            (self._scratch_base / "input").mkdir(parents=True, exist_ok=True)
            (self._scratch_base / "output").mkdir(parents=True, exist_ok=True)
            (self._scratch_base / "scripts").mkdir(parents=True, exist_ok=True)
            # Copy input into scratch so the sandbox never touches the real input
            scratch_input = self._scratch_base / "input" / "input.xlsx"
            shutil.copy2(self.input_path, scratch_input)
            scratch_input.chmod(0o644)
        return self._scratch_base

    def destroy_scratch(self) -> bool:
        """Irrecoverably remove the scratch directory.

        Must be called at A1 handoff before transferring the memo to the
        parent.  Sets ``scratch_destroyed = True`` so the episode record
        can record compliance.  Returns True only when a scratch directory
        actually existed and was removed.
        """
        if self.scratch_destroyed:
            return False  # idempotent
        existed = self._scratch_base.exists()
        if existed:
            shutil.rmtree(self._scratch_base, ignore_errors=False)
        self.scratch_destroyed = True
        return existed

    # ── Artifact helpers ─────────────────────────────────────────────────────

    @property
    def solution_path(self) -> Path:
        return self.artifacts_dir / "solution.py"

    @property
    def memo_path(self) -> Path:
        return self.artifacts_dir / "memo.json"

    @property
    def output_path(self) -> Path:
        """Expected location of the final output.xlsx."""
        return self.output_dir / "output.xlsx"

    def solution_exists(self) -> bool:
        return self.solution_path.is_file()

    def memo_exists(self) -> bool:
        return self.memo_path.is_file()

    def output_exists(self) -> bool:
        return self.output_path.is_file()

    # ── Hash helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def sha256(path: Path) -> str:
        """Return the hex SHA-256 of *path*."""
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Remove the entire workspace directory tree.

        Safe to call multiple times.
        """
        if self.base.exists():
            shutil.rmtree(self.base, ignore_errors=True)

    # ── Context manager ──────────────────────────────────────────────────────

    def __enter__(self) -> "TaskWorkspace":
        return self

    def __exit__(self, *_: object) -> None:
        self.cleanup()
