"""Tool dispatcher and implementations for the SpreadsheetBench delegation study.

Allowlist enforcement
---------------------
Every tool call passes through ``ToolDispatcher.call()``, which checks the
tool name against the condition-role allowlist loaded from
``config/tool_manifests.yaml``.  Attempting to call a tool not on the
allowlist raises ``ToolNotAllowedError`` immediately — no side effects occur.

Condition/role → allowed tools
-------------------------------
A0  worker  : inspection tools + submit_memo
A1  worker  : inspection tools + run_ephemeral_python + submit_memo
P0  worker  : inspection tools + write_solution + submit_memo + submit_solution
P1  worker  : inspection tools + write_solution + run_solution + submit_memo
              + submit_solution
parent      : inspection tools + write_solution + run_solution + submit_solution

Inspection tools (read-only, no execution):
    list_sheets, workbook_metadata, read_range, search_workbook

These inspect ``workspace.input_path`` using openpyxl in read-only mode.
They never touch the raw benchmark archive or any evaluator output.

read_range returns BOTH cached cell values (``rows``) and formula text
(``formulas``).  The ``formulas`` matrix has the formula string (e.g.
``"=SUM(A1:A10)"``) where the cell contains a formula, and ``null``
elsewhere.  Workbook is opened separately with ``data_only=True`` (for
cached values) and ``data_only=False`` (for formula text).

search_workbook searches both cached values and formula text across every
sheet and every row.  ``max_results`` caps the number of returned matches;
it does NOT limit the inspection scope.

Execution tools:
    run_ephemeral_python
        A1 only.  Runs *arbitrary* Python code in the Docker sandbox against a
        disposable scratch copy of the input.  stdout/stderr are returned; any
        output files are discarded with the scratch directory.  Scratch is
        destroyed when ``ToolDispatcher.seal_handoff()`` is called.

    run_solution
        P1 and parent only.  Executes ``workspace.solution_path`` (must have
        been written by ``write_solution`` first) in the Docker sandbox against
        the clean input, writing output to ``workspace.output_dir``.  Returns
        the ``ExecutionResult``.

Artifact tools:
    write_solution  : write/replace ``artifacts/solution.py`` (no execution)
    submit_memo     : validate and save ``artifacts/memo.json``
    submit_solution : mark the current ``solution.py`` as the candidate

The parent role uses ``starts_from_clean_input`` — its workspace is always
created from the original clean task input, not from any worker scratch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Callable, Optional

import openpyxl
try:  # openpyxl moved the read-only placeholder between releases
    from openpyxl.cell.cell import EmptyCell
except ImportError:  # pragma: no cover - version-dependent import location
    from openpyxl.cell.read_only import EmptyCell
from openpyxl.utils import get_column_letter, column_index_from_string
from openpyxl.utils.cell import coordinate_from_string
from openpyxl.utils import range_boundaries

from .execute import DEFAULT_IMAGE, ExecutionResult, run_in_sandbox
from .workspace import TaskWorkspace


# ── read_output_range constants ───────────────────────────────────────────────

#: Default soffice binary (macOS LibreOffice app bundle).
DEFAULT_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"

#: Maximum cells read_output_range will return (reuse read_range's cap).
_MAX_CELLS_OUTPUT = 2000

#: Recalculation timeout in seconds.
_RECALC_TIMEOUT_S = 60


# ── read_output_range helpers ─────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    """Return hex SHA-256 of *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _soffice_version(soffice: str) -> str:
    """Return the first line of `soffice --version`, e.g. 'LibreOffice 26.8.0.3'."""
    try:
        out = subprocess.run(
            [soffice, "--version"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out.splitlines()[0] if out else "libreoffice unknown"
    except Exception:
        return "libreoffice unknown"


def _recalc_copy(src_xlsx: str, soffice: str, scratch_dir: str) -> str:
    """Recalculate a COPY of *src_xlsx* with headless LibreOffice.

    Uses a private LibreOffice user-profile directory so concurrent episodes
    do not collide on a shared profile lock.  Returns path to the recalculated
    output file (always named ``in.xlsx`` in a subdirectory of *scratch_dir*).
    """
    os.makedirs(scratch_dir, exist_ok=True)
    work = os.path.join(scratch_dir, "in.xlsx")
    shutil.copyfile(src_xlsx, work)
    out_dir = os.path.join(scratch_dir, "out")
    os.makedirs(out_dir, exist_ok=True)
    # Private profile prevents lock-file collisions between concurrent episodes.
    profile = os.path.join(scratch_dir, "lo_profile")
    cmd = [
        soffice,
        f"-env:UserInstallation=file://{profile}",
        "--headless", "--calc",
        "--convert-to", "xlsx",
        "--outdir", out_dir,
        work,
    ]
    subprocess.run(cmd, capture_output=True, text=True,
                   timeout=_RECALC_TIMEOUT_S, check=True)
    res = os.path.join(out_dir, "in.xlsx")
    if not os.path.exists(res):
        raise RuntimeError("LibreOffice produced no output during recalculation")
    return res


# ── Allowlists (mirrors config/tool_manifests.yaml) ─────────────────────────

#: Inspection tools available to every condition/role.
INSPECTION_TOOLS: frozenset[str] = frozenset({
    "list_sheets",
    "workbook_metadata",
    "read_range",
    "search_workbook",
})

#: Full allowlist by (condition, role).  "parent" uses the key ("parent", "parent").
#: SOLO uses the same full parent allowlist (no worker phase, no memo/submit_memo).
_ALLOWLISTS: dict[tuple[str, str], frozenset[str]] = {
    # A0 worker: NO read_output_range (no execution, no observation)
    ("A0", "worker"): INSPECTION_TOOLS | {"submit_memo"},
    # A1 worker: has read_output_range (can observe output of run_ephemeral_python)
    ("A1", "worker"): INSPECTION_TOOLS | {"run_ephemeral_python", "read_output_range", "submit_memo"},
    # P0 worker: NO read_output_range (no execution, no observation)
    ("P0", "worker"): INSPECTION_TOOLS | {"write_solution", "submit_memo", "submit_solution"},
    # P1 worker: has read_output_range (can observe output of run_solution)
    ("P1", "worker"): INSPECTION_TOOLS | {"write_solution", "run_solution", "read_output_range",
                                          "submit_memo", "submit_solution"},
    # Parent: has read_output_range in ALL cells including SOLO; byte-identical across cells.
    ("parent", "parent"): INSPECTION_TOOLS | {"write_solution", "run_solution",
                                               "read_output_range", "submit_solution"},
    # SOLO condition runs with role="parent"; mapped to the same parent allowlist.
    ("SOLO", "parent"): INSPECTION_TOOLS | {"write_solution", "run_solution",
                                             "read_output_range", "submit_solution"},
}

# Handoff artifacts per condition
_HANDOFF_ARTIFACTS: dict[str, list[str]] = {
    "A0": ["memo.json"],
    "A1": ["memo.json"],          # no script; scratch destroyed before handoff
    "SOLO": [],                   # no handoff artifacts (no worker phase)
    "P0": ["memo.json", "solution.py"],
    "P1": ["memo.json", "solution.py"],
}


class ToolNotAllowedError(PermissionError):
    """Raised when a caller requests a tool outside its condition/role allowlist."""


class ToolDispatcher:
    """Routes tool calls through condition-role allowlists.

    Parameters
    ----------
    condition:
        One of ``"A0"``, ``"A1"``, ``"P0"``, ``"P1"``.
    role:
        ``"worker"`` or ``"parent"``.
    workspace:
        The ``TaskWorkspace`` for this run.  All tool implementations read and
        write exclusively within this workspace.
    sandbox_image:
        Docker image to use for sandbox execution (must be
        ``spreadsheet-exec:protocol-v01`` or a digest alias).
    wall_timeout:
        Wall-clock seconds limit for each sandbox execution.
    host_uid_gid:
        Passed to ``run_in_sandbox`` for macOS fallback.  None → auto-detect.
    """

    def __init__(
        self,
        condition: str,
        role: str,
        workspace: TaskWorkspace,
        *,
        sandbox_image: str = DEFAULT_IMAGE,
        wall_timeout: int = 120,
        host_uid_gid: tuple[int, int] | None = None,
        run_id: str = "unknown",
        batch_id: str | None = None,
        on_scratch_destroyed: Callable[[], None] | None = None,
        soffice: str | None = None,
        answer_position: str | None = None,
    ) -> None:
        # All parent roles (including SOLO running as "parent") use the parent allowlist.
        # SOLO is a special case: condition="SOLO", role="parent" → parent allowlist.
        if role == "parent":
            key: tuple[str, str] = ("parent", "parent")
        else:
            key = (condition, role)
        if key not in _ALLOWLISTS:
            raise ValueError(
                f"Unknown condition/role combination: condition={condition!r} role={role!r}. "
                f"Valid: {sorted(_ALLOWLISTS)}"
            )

        self.condition = condition
        self.role = role
        self.workspace = workspace
        self.sandbox_image = sandbox_image
        self.wall_timeout = wall_timeout
        self.host_uid_gid = host_uid_gid
        self.run_id = run_id
        self.batch_id = batch_id  # shared across concurrent conditions; applied as Docker label
        self._on_scratch_destroyed = on_scratch_destroyed

        # soffice binary for read_output_range recalculation
        self._soffice: str = soffice or DEFAULT_SOFFICE

        # v0.4.1: task answer_position for automatic observation after run tools
        self._answer_position: str | None = answer_position

        # v0.4.1: auto-observation is enabled for A1/P1 workers and all parents (incl. SOLO).
        # A0/P0 workers have no run tools so they can never trigger auto-obs, but we still
        # set the flag False for them to make the logic explicit and testable.
        self._auto_obs_enabled: bool = (
            role == "parent"
            or (condition in ("A1", "P1") and role == "worker")
        )

        self._allowed: frozenset[str] = _ALLOWLISTS[key]

        # Compliance counters
        self._tool_counts: dict[str, int] = {}
        # feedback_calls: times run_ephemeral_python or run_solution was called
        self.feedback_call_count: int = 0

        # v0.4 observable feedback tracking
        # Number of times the worker called read_output_range EXPLICITLY
        self._observed_output_call_count: int = 0
        # True if write_solution was called AFTER any observation (explicit or auto)
        self._wrote_after_observation: bool = False
        # True once any observation (explicit or auto) has been made this dispatch session
        self._any_observation_made: bool = False
        # mtime of the latest output.xlsx at the time write_solution was called (for staleness)
        self._last_write_solution_mtime: float | None = None
        # Per-instance cache keyed by (output_sha256, range_ref)
        self._read_output_range_cache: dict[tuple[str, str], dict] = {}

        # v0.4.1: count of automatic observations delivered alongside run tool results
        self._auto_observation_count: int = 0

        # User mode used by the most recent sandbox execution
        self.last_user_mode: str = ""

        # Whether seal_handoff() has been called
        self._sealed: bool = False

    # ── Public API ───────────────────────────────────────────────────────────

    def allowed_tools(self) -> list[str]:
        """Return the sorted list of tools available to this dispatcher."""
        return sorted(self._allowed)

    def call(self, tool_name: str, **kwargs: Any) -> Any:
        """Call *tool_name* with the given keyword arguments.

        Raises
        ------
        ToolNotAllowedError
            If *tool_name* is not in the allowlist for this condition/role.
        """
        if tool_name not in self._allowed:
            raise ToolNotAllowedError(
                f"Tool '{tool_name}' is not permitted for "
                f"condition={self.condition!r} role={self.role!r}. "
                f"Allowed: {sorted(self._allowed)}"
            )

        impl = getattr(self, f"_tool_{tool_name}", None)
        if impl is None:
            raise NotImplementedError(f"No implementation for tool '{tool_name}'")  # pragma: no cover

        self._tool_counts[tool_name] = self._tool_counts.get(tool_name, 0) + 1

        return impl(**kwargs)

    def seal_handoff(self) -> dict[str, Any]:
        """Finalise the worker's turn and return handoff metadata.

        For A1: destroys the scratch directory before returning.  The handoff
        dict contains metadata for each allowed artifact that currently exists.
        Missing artifacts are omitted so a partial handoff can be persisted.

        Returns a dict suitable for embedding in the ``handoff`` field of an
        episode record.
        """
        if self._sealed:
            raise RuntimeError("seal_handoff() called more than once")

        # A1 MUST destroy scratch before handing off.  Emit through a callback
        # so the dispatcher remains independent of the public event logger.
        if self.condition == "A1":
            if self.workspace.destroy_scratch() and self._on_scratch_destroyed is not None:
                self._on_scratch_destroyed()

        self._sealed = True

        artifacts = _HANDOFF_ARTIFACTS.get(self.condition, [])
        result: dict[str, Any] = {}

        if "memo.json" in artifacts:
            if self.workspace.memo_exists():
                memo_path = self.workspace.memo_path
                result["memo_path"] = str(memo_path)
                result["memo_sha256"] = TaskWorkspace.sha256(memo_path)

        if "solution.py" in artifacts:
            if self.workspace.solution_exists():
                sol_path = self.workspace.solution_path
                result["candidate_script_path"] = str(sol_path)
                result["candidate_script_sha256"] = TaskWorkspace.sha256(sol_path)

        return result

    @property
    def tool_counts(self) -> dict[str, int]:
        """Read-only copy of per-tool call counts."""
        return dict(self._tool_counts)

    @property
    def observed_output_call_count(self) -> int:
        """Number of times read_output_range was called EXPLICITLY on this dispatcher."""
        return self._observed_output_call_count

    @property
    def auto_observation_count(self) -> int:
        """Number of automatic observations delivered alongside run tool results (v0.4.1)."""
        return self._auto_observation_count

    @property
    def worker_revised_after_observation(self) -> bool:
        """True if write_solution was called after any observation (explicit or automatic)."""
        return self._wrote_after_observation

    # ── Inspection tools (read-only, no Docker) ──────────────────────────────

    def _tool_list_sheets(self) -> dict[str, Any]:
        """Return the names of all worksheets in the workbook.

        Returns
        -------
        {"sheets": ["Sheet1", "Sheet2", ...]}
        """
        wb = _open_wb(self.workspace.input_path)
        try:
            return {"sheets": wb.sheetnames}
        finally:
            wb.close()

    def _tool_workbook_metadata(self) -> dict[str, Any]:
        """Return structural metadata about the workbook.

        Returns
        -------
        {
            "sheets": [
                {"name": str, "dimensions": str | None,
                 "max_row": int, "max_col": int}
            ],
            "named_ranges": [{"name": str, "value": str}],
        }
        """
        wb = _open_wb(self.workspace.input_path)
        try:
            sheets_meta = []
            for ws in wb.worksheets:
                sheets_meta.append({
                    "name": ws.title,
                    "dimensions": _ws_dimensions(ws),
                    "max_row": ws.max_row or 0,
                    "max_col": ws.max_column or 0,
                })
            named_ranges = [
                {"name": nr.name, "value": str(nr.value)}
                for nr in wb.defined_names.definedName
            ] if hasattr(wb, "defined_names") and wb.defined_names else []
            return {"sheets": sheets_meta, "named_ranges": named_ranges}
        finally:
            wb.close()

    def _tool_read_range(
        self,
        range_ref: str,
        sheet: str | None = None,
    ) -> dict[str, Any]:
        """Read a rectangular cell range and return cached values and formula text.

        Parameters
        ----------
        range_ref:
            Accepts ``"A1:C5"``, ``"Sheet1!A1:C5"``, a single cell like
            ``"B3"`` / ``"Sheet2!B3"``, a column-only range like ``"A:G"``
            or ``"Sheet1!A:G"``.  Sheet names with spaces must be quoted
            in the reference, e.g. ``"'My Sheet'!A1:B5"``.
        sheet:
            Optional sheet name override.  If *range_ref* contains a
            sheet prefix (``"SheetName!..."``), that takes precedence.

        Returns
        -------
        {
            "sheet": str,
            "range": str,               # normalised cell range (e.g. "A1:C5")
            "rows": [[value, ...], ...], # cached cell values (data_only=True)
            "formulas": [[str|null, ...], ...],  # formula text where present, null elsewhere
        }

        Backward-compatible: ``rows`` continues to carry cached values.
        ``formulas`` is a parallel matrix where non-formula cells are null.
        """
        return read_range(self.workspace.input_path, range_ref, sheet)

    def _tool_search_workbook(
        self,
        query: str,
        *,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> dict[str, Any]:
        """Search all cells across all sheets for *query*.

        Searches BOTH cached values (data_only=True) and formula text
        (data_only=False) for every cell in every sheet.  ``max_results``
        caps the number of returned match entries but does NOT limit the
        inspection scope — every cell on every sheet is examined regardless.

        Returns
        -------
        {
            "query": str,
            "matches": [
                {"sheet": str, "cell": str, "value": str,
                 "formula": str | null}, ...
            ],
            "truncated": bool,
        }
        """
        return search_workbook(
            self.workspace.input_path,
            query,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )

    # ── Execution tools (Docker sandbox) ─────────────────────────────────────

    def _tool_run_ephemeral_python(self, code: str) -> dict[str, Any]:
        """Run *code* in the sandbox against a disposable scratch copy.

        The scratch copy is separate from ``workspace.input_path``.  Any files
        written to ``/output`` inside the container are discarded when the
        ephemeral output dir is cleaned up.  Call ``seal_handoff()`` to destroy
        the entire scratch directory before handoff.

        Parameters
        ----------
        code:
            Arbitrary Python source.  The script may read
            ``/input/input.xlsx`` and may write anywhere under ``/output``.
            It does **not** need to accept ``--input/--output`` CLI args.

        Returns
        -------
        {
            "exit_code": int | None,
            "stdout": str,
            "stderr": str,
            "timed_out": bool,
            "user_mode": str,
        }
        """
        # Write the code to a temp script in the scratch scripts dir
        scripts_dir = self.workspace.scratch_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_num = self._tool_counts.get("run_ephemeral_python", 1)
        script_path = scripts_dir / f"ephemeral_{script_num:03d}.py"
        script_path.write_text(code, encoding="utf-8")

        # Isolated output dir (discarded after this call)
        scratch_output = self.workspace.scratch_dir / f"output_{script_num:03d}"
        scratch_output.mkdir(parents=True, exist_ok=True)

        result = run_in_sandbox(
            script_path=script_path,
            input_path=self.workspace.scratch_dir / "input" / "input.xlsx",
            output_dir=scratch_output,
            image=self.sandbox_image,
            wall_timeout=self.wall_timeout,
            pass_std_args=False,   # ephemeral code has no --input/--output args
            host_uid_gid=self.host_uid_gid,
            run_id=self.run_id,
            role=self.role,
            batch_id=self.batch_id,
        )
        self.last_user_mode = result.user_mode
        self.feedback_call_count += 1

        payload: dict[str, Any] = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "user_mode": result.user_mode,
        }

        # v0.4.1: automatically deliver observed answer_position for eligible roles
        if self._auto_obs_enabled:
            payload["auto_observation"] = self._do_auto_observe()

        return payload

    def _tool_run_solution(self) -> dict[str, Any]:
        """Execute the current ``solution.py`` in the sandbox.

        ``write_solution`` must have been called first.  The solution is run
        against the clean ``workspace.input_path``; output is written to
        ``workspace.output_dir``.

        Returns
        -------
        {
            "exit_code": int | None,
            "stdout": str,
            "stderr": str,
            "timed_out": bool,
            "succeeded": bool,
            "user_mode": str,
            "output_path": str | None,
        }
        """
        if not self.workspace.solution_exists():
            raise RuntimeError(
                "run_solution called but no solution.py exists. "
                "Call write_solution first."
            )

        result = run_in_sandbox(
            script_path=self.workspace.solution_path,
            input_path=self.workspace.input_path,
            output_dir=self.workspace.output_dir,
            image=self.sandbox_image,
            wall_timeout=self.wall_timeout,
            pass_std_args=True,
            host_uid_gid=self.host_uid_gid,
            run_id=self.run_id,
            role=self.role,
            batch_id=self.batch_id,
        )
        self.last_user_mode = result.user_mode
        self.feedback_call_count += 1

        payload: dict[str, Any] = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "succeeded": result.succeeded,
            "user_mode": result.user_mode,
            "output_path": str(result.output_path) if result.output_path else None,
        }

        # v0.4.1: automatically deliver observed answer_position for eligible roles
        if self._auto_obs_enabled:
            payload["auto_observation"] = self._do_auto_observe()

        return payload

    def _do_read_output_range(self, range_ref: str) -> dict[str, Any]:
        """Core implementation of output-range reading — NO counter side-effects.

        Called by both the explicit ``read_output_range`` tool and the v0.4.1
        automatic observation path.  Counters are managed by the caller so that
        explicit and automatic observations are tracked independently.

        Returns the same payload as ``_tool_read_output_range`` or an error dict.
        Does NOT update ``_observed_output_call_count`` or ``_auto_observation_count``.
        """
        # Refuse if the path would escape the workspace
        root = str(self.workspace.base.resolve())
        out_path = self.workspace.output_path
        real_out = str(out_path.resolve())
        if not real_out.startswith(root + os.sep):
            return {"error": "refused: output path outside workspace"}

        if not out_path.exists():
            return {"error": "no output workbook yet; run your solution first"}

        sha = _sha256_file(str(out_path))

        # Stale check: output older than most recent write_solution call?
        stale = bool(
            self._last_write_solution_mtime is not None
            and out_path.stat().st_mtime < self._last_write_solution_mtime
        )

        cache_key = (sha, range_ref)
        if cache_key in self._read_output_range_cache:
            cached = dict(self._read_output_range_cache[cache_key])
            cached["cached"] = True
            cached["stale"] = stale  # staleness can change even on cache hit
            return cached

        # Recalculate into a temporary scratch directory
        scratch = tempfile.mkdtemp(prefix="recalc_out_")
        try:
            rec = _recalc_copy(str(out_path), self._soffice, scratch)
            wb_v = openpyxl.load_workbook(rec, data_only=True, read_only=True)
            wb_f = openpyxl.load_workbook(rec, data_only=False, read_only=True)

            # Parse range_ref
            sheet_name, rng = _parse_range_ref(range_ref, None)
            # Normalise single-cell ref
            if ":" not in rng and not _is_column_range(rng):
                rng = f"{rng}:{rng}"

            ws_v = wb_v[sheet_name] if sheet_name else wb_v.worksheets[0]
            ws_f = wb_f[sheet_name] if sheet_name else wb_f.worksheets[0]

            # Expand column-only range
            rng = _expand_column_range(rng, ws_v)

            min_c, min_r, max_c, max_r = range_boundaries(rng)
            n_cells = (max_c - min_c + 1) * (max_r - min_r + 1)
            if n_cells > _MAX_CELLS_OUTPUT:
                return {"error": f"range has {n_cells} cells; limit {_MAX_CELLS_OUTPUT}",
                        "output_sha256": sha}

            values: list[list[Any]] = []
            formulas: list[list[Any]] = []
            errors: list[list[Any]] = []

            for row_v, row_f in zip(
                ws_v.iter_rows(min_row=min_r, max_row=max_r,
                               min_col=min_c, max_col=max_c),
                ws_f.iter_rows(min_row=min_r, max_row=max_r,
                               min_col=min_c, max_col=max_c),
            ):
                rv_row: list[Any] = []
                rf_row: list[Any] = []
                for cv, cf in zip(row_v, row_f):
                    v = _serialise_cell(cv.value)
                    f = cf.value if isinstance(cf.value, str) and cf.value.startswith("=") else None
                    rv_row.append(v)
                    rf_row.append(f)
                    # Error: string starting with "#", or formula with None value
                    if (isinstance(v, str) and v.startswith("#")) or (f is not None and v is None):
                        errors.append([cv.coordinate, v if isinstance(v, str) else "EMPTY_FORMULA_RESULT"])
                values.append(rv_row)
                formulas.append(rf_row)

            wb_v.close()
            wb_f.close()

            result: dict[str, Any] = {
                "sheet": ws_v.title,
                "range": rng,
                "values": values,
                "formulas": formulas,
                "errors": errors,
                "recalc_engine": _soffice_version(self._soffice),
                "output_sha256": sha,
                "stale": stale,
            }
            self._read_output_range_cache[cache_key] = result
            return result

        except subprocess.TimeoutExpired:
            return {
                "error": f"recalculation timed out after {_RECALC_TIMEOUT_S}s",
                "output_sha256": sha,
            }
        except Exception as e:  # noqa: BLE001 — never raise into the agent loop
            return {
                "error": f"read_output_range failed: {type(e).__name__}: {e}",
                "output_sha256": sha,
            }
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _do_auto_observe(self) -> dict[str, Any]:
        """Perform automatic observation of the answer_position range (v0.4.1).

        Called after every eligible run_solution / run_ephemeral_python dispatch.
        Increments ``_auto_observation_count`` exactly once; sets
        ``_any_observation_made``; does NOT touch ``_observed_output_call_count``.

        Returns the observation payload (may contain ``error`` key if no output
        exists or recalc fails).  Never raises.
        """
        if not self._answer_position:
            payload: dict[str, Any] = {"error": "no answer_position configured for auto_observation"}
        else:
            payload = self._do_read_output_range(self._answer_position)

        self._auto_observation_count += 1
        self._any_observation_made = True
        return payload

    def _tool_read_output_range(self, range_ref: str) -> dict[str, Any]:
        """Read a range from the recalculated output workbook (EXPLICIT call).

        Reads the calling role's OWN produced workbook
        (``<workspace>/output/output.xlsx`` written by the last
        ``run_solution`` or scratch code in A1), never the input workbook
        and never any path outside the role's workspace.

        Recalculates a COPY of the output using the same LibreOffice
        binary/version the evaluator uses (private profile to avoid
        lock contention between concurrent episodes).

        Parameters
        ----------
        range_ref:
            Standard range reference (e.g. ``"Sheet1!H2:H40"``).
            Limit: 2,000 cells.

        Returns
        -------
        {
            "sheet": str,
            "range": str,
            "values": [[...], ...],      # recalculated values
            "formulas": [[str|null,...], ...],  # formula text or null
            "errors": [[cell, code], ...],  # cells with #ERR values
            "recalc_engine": str,        # e.g. "LibreOffice 26.8.0.3"
            "output_sha256": str,        # SHA-256 of the raw output.xlsx
            "stale": bool,               # True if output predates last write_solution
        }
        or {"error": str, ...} on failure.
        """
        result = self._do_read_output_range(range_ref)
        # Count every explicit call (success or error) as an observation
        self._observed_output_call_count += 1
        self._any_observation_made = True
        return result

    # ── Artifact tools ───────────────────────────────────────────────────────

    def _tool_write_solution(self, code: str) -> dict[str, Any]:
        """Write (or overwrite) ``artifacts/solution.py`` without executing it.

        Parameters
        ----------
        code:
            Complete Python source.  Must accept ``--input`` and ``--output``
            CLI arguments.

        Returns
        -------
        {"path": str, "sha256": str, "bytes": int}
        """
        sol_path = self.workspace.solution_path
        sol_path.write_text(code, encoding="utf-8")

        # v0.4.1: track revision after ANY observation (explicit or automatic)
        if self._any_observation_made:
            self._wrote_after_observation = True

        # Track mtime so read_output_range can flag stale output
        out_path = self.workspace.output_path
        self._last_write_solution_mtime = (
            out_path.stat().st_mtime if out_path.exists() else None
        )

        return {
            "path": str(sol_path),
            "sha256": TaskWorkspace.sha256(sol_path),
            "bytes": sol_path.stat().st_size,
        }

    def _tool_submit_memo(self, memo: dict[str, Any]) -> dict[str, Any]:
        """Validate and save the memo dict to ``artifacts/memo.json``.

        Validates the required keys against the memo schema.  Does not enforce
        the full JSON-Schema (that is the validator's job); performs a minimal
        structural check here so the tool fails fast on obviously malformed memos.

        Returns
        -------
        {"path": str, "sha256": str}
        """
        _validate_memo_structure(memo)
        memo_path = self.workspace.memo_path
        memo_path.write_text(json.dumps(memo, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        return {
            "path": str(memo_path),
            "sha256": TaskWorkspace.sha256(memo_path),
        }

    def _tool_submit_solution(self) -> dict[str, Any]:
        """Seal the current ``solution.py`` as the candidate script.

        The script must already exist (written by ``write_solution``).

        Returns
        -------
        {"path": str, "sha256": str, "bytes": int}
        """
        if not self.workspace.solution_exists():
            raise RuntimeError(
                "submit_solution called but no solution.py exists. "
                "Call write_solution first."
            )
        sol_path = self.workspace.solution_path
        return {
            "path": str(sol_path),
            "sha256": TaskWorkspace.sha256(sol_path),
            "bytes": sol_path.stat().st_size,
        }


# ── Module-level convenience wrappers (for tests / external use) ─────────────

def list_sheets(input_path: Path) -> dict[str, Any]:
    """Read-only inspection: return sheet names."""
    wb = _open_wb(input_path)
    try:
        return {"sheets": wb.sheetnames}
    finally:
        wb.close()


def workbook_metadata(input_path: Path) -> dict[str, Any]:
    """Read-only inspection: return structural metadata."""
    wb = _open_wb(input_path)
    try:
        sheets_meta = []
        for ws in wb.worksheets:
            sheets_meta.append({
                "name": ws.title,
                "dimensions": _ws_dimensions(ws),
                "max_row": ws.max_row or 0,
                "max_col": ws.max_column or 0,
            })
        named_ranges = [
            {"name": nr.name, "value": str(nr.value)}
            for nr in wb.defined_names.definedName
        ] if hasattr(wb, "defined_names") and wb.defined_names else []
        return {"sheets": sheets_meta, "named_ranges": named_ranges}
    finally:
        wb.close()


def read_range(
    input_path: Path,
    range_ref: str,
    sheet: str | None = None,
) -> dict[str, Any]:
    """Read-only inspection: return both cached values and formula text for a range.

    Opens the workbook twice — once with ``data_only=True`` (cached values)
    and once with ``data_only=False`` (formula text).  This ensures formula
    cells are visible in both representations.

    Parameters
    ----------
    input_path:
        Path to the workbook to read.
    range_ref:
        Standard cell range reference.  Accepts:
        - ``"A1:C5"`` — rectangular range on the active / specified sheet
        - ``"Sheet1!A1:C5"`` — range with sheet prefix
        - ``"B3"`` — single cell (normalised to "B3:B3")
        - ``"A:G"`` — column-only range (expanded to all rows on the sheet)
        - ``"'My Sheet'!A1:B5"`` — sheet name with spaces (quotes stripped)
    sheet:
        Optional sheet name override.  Overridden by an inline sheet prefix.

    Returns
    -------
    {
        "sheet": str,
        "range": str,               # normalised cell range
        "rows": [[value, ...], ...], # cached values (data_only=True); null for empty
        "formulas": [[str|null, ...], ...],  # formula text ("=...") or null
    }
    """
    sheet_name, cell_range = _parse_range_ref(range_ref, sheet)

    # Normalise single-cell ref ("A1") to "A1:A1"
    if ":" not in cell_range and not _is_column_range(cell_range):
        cell_range = f"{cell_range}:{cell_range}"

    # ── Values pass (data_only=True — cached values) ──────────────────────
    wb_data = _open_wb(input_path)
    try:
        ws_data = _get_sheet(wb_data, sheet_name)
        actual_sheet_name = ws_data.title

        # Expand column-only range to full cell range using sheet dimensions
        effective_range = _expand_column_range(cell_range, ws_data)

        rows: list[list[Any]] = []
        for row in ws_data[effective_range]:
            if not isinstance(row, (list, tuple)):
                row = [row]
            rows.append([_serialise_cell(c.value) for c in row])
    finally:
        wb_data.close()

    # ── Formula pass (data_only=False — formula text) ─────────────────────
    wb_formula = _open_wb_formulatext(input_path)
    try:
        ws_formula = _get_sheet(wb_formula, sheet_name)

        formulas: list[list[Any]] = []
        for row in ws_formula[effective_range]:
            if not isinstance(row, (list, tuple)):
                row = [row]
            formulas.append([
                c.value if isinstance(c.value, str) and c.value.startswith("=") else None
                for c in row
            ])
    finally:
        wb_formula.close()

    return {
        "sheet": actual_sheet_name,
        "range": effective_range,
        "rows": rows,
        "formulas": formulas,
    }


def search_workbook(
    input_path: Path,
    query: str,
    *,
    case_sensitive: bool = False,
    max_results: int = 100,
) -> dict[str, Any]:
    """Read-only inspection: substring search across all cells on all sheets.

    Searches BOTH cached values (``data_only=True``) and formula text
    (``data_only=False``) for every cell across every sheet.  ``max_results``
    caps the number of returned match entries; it does NOT limit the
    inspection scope — every row and column on every sheet is examined.

    Returns
    -------
    {
        "query": str,
        "matches": [
            {"sheet": str, "cell": str, "value": str | null,
             "formula": str | null}, ...
        ],
        "truncated": bool,
    }
    """
    needle = query if case_sensitive else query.lower()

    # ── Formula pass: build lookup dict of formula-bearing cells ─────────
    # (sheet_title, coordinate) -> formula_text
    formula_lookup: dict[tuple[str, str], str] = {}
    wb_formula = _open_wb_formulatext(input_path)
    try:
        for ws_f in wb_formula.worksheets:
            for row in ws_f.iter_rows():
                for cell in row:
                    if isinstance(cell, EmptyCell):
                        continue
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formula_lookup[(ws_f.title, cell.coordinate)] = cell.value
    finally:
        wb_formula.close()

    # ── Data pass: iterate cached values and match against query ──────────
    matches: list[dict[str, Any]] = []
    truncated = False

    wb_data = _open_wb(input_path)
    try:
        for ws_d in wb_data.worksheets:
            if truncated:
                break
            for row in ws_d.iter_rows():
                if truncated:
                    break
                for cell in row:
                    if truncated:
                        break
                    if isinstance(cell, EmptyCell):
                        continue
                    coord = cell.coordinate
                    data_val = cell.value
                    formula_val = formula_lookup.get((ws_d.title, coord))

                    # Determine what to match against
                    value_str: str | None = None
                    if data_val is not None:
                        value_str = str(data_val)

                    value_haystack = (value_str or "") if case_sensitive else (value_str or "").lower()
                    formula_haystack = (formula_val or "") if case_sensitive else (formula_val or "").lower()

                    value_hit = bool(value_str) and needle in value_haystack
                    formula_hit = bool(formula_val) and needle in formula_haystack

                    if value_hit or formula_hit:
                        entry: dict[str, Any] = {
                            "sheet": ws_d.title,
                            "cell": coord,
                            "value": value_str,
                            "formula": formula_val,
                        }
                        matches.append(entry)
                        if len(matches) >= max_results:
                            truncated = True
                            break
    finally:
        wb_data.close()

    return {"query": query, "matches": matches, "truncated": truncated}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _open_wb(path: Path) -> openpyxl.Workbook:
    """Open *path* read-only with cached (data-only) values."""
    return openpyxl.load_workbook(str(path), read_only=True, data_only=True)


def _open_wb_formulatext(path: Path) -> openpyxl.Workbook:
    """Open *path* read-only to expose formula text (data_only=False)."""
    return openpyxl.load_workbook(str(path), read_only=True, data_only=False)


def _ws_dimensions(ws) -> str | None:
    """Return worksheet dimensions string, compatible with ReadOnlyWorksheet."""
    # Regular Worksheet has .dimensions; ReadOnlyWorksheet uses .calculate_dimension()
    if hasattr(ws, "dimensions"):
        return ws.dimensions
    try:
        return ws.calculate_dimension()
    except Exception:
        return None


def _get_sheet(wb: openpyxl.Workbook, name: str | None):
    """Return the worksheet named *name*, or the active sheet if None."""
    if name is None:
        return wb.active
    if name not in wb.sheetnames:
        raise KeyError(
            f"Sheet {name!r} not found. Available: {wb.sheetnames}"
        )
    return wb[name]


def _parse_range_ref(
    range_ref: str,
    default_sheet: str | None,
) -> tuple[str | None, str]:
    """Parse an optional 'SheetName!A1:B5' or ``'My Sheet'!A1:B5`` string.

    Returns (sheet_name_or_None, cell_range).
    Strips surrounding single/double quotes from sheet names with spaces.
    """
    if "!" in range_ref:
        sheet_part, cell_part = range_ref.split("!", 1)
        return sheet_part.strip("'\""), cell_part.strip()
    return default_sheet, range_ref.strip()


def _is_column_range(cell_range: str) -> bool:
    """Return True if *cell_range* is a column-only ref like ``"A:G"``."""
    return bool(re.match(r"^[A-Za-z]+:[A-Za-z]+$", cell_range))


def _expand_column_range(cell_range: str, ws) -> str:
    """If *cell_range* is column-only (e.g. ``"A:G"``), expand to a cell range.

    Uses ``ws.max_row`` to bound the range.  Returns *cell_range* unchanged
    if it is already a standard cell range.
    """
    if not _is_column_range(cell_range):
        return cell_range
    first_col, last_col = cell_range.split(":")
    max_row = ws.max_row or 1
    return f"{first_col.upper()}1:{last_col.upper()}{max_row}"


def _serialise_cell(value: Any) -> Any:
    """Convert a cell value to a JSON-serialisable type."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    # datetime, date, time → ISO string
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _validate_memo_structure(memo: dict[str, Any]) -> None:
    """Raise ValueError if *memo* is missing required top-level keys."""
    required = {
        "task_understanding",
        "target_locations",
        "proposed_approach",
        "assumptions",
        "observed_feedback",
        "known_risks",
        "validation_plan",
    }
    missing = required - set(memo.keys())
    if missing:
        raise ValueError(
            f"Memo is missing required fields: {sorted(missing)}"
        )
