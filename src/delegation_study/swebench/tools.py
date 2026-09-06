"""Per-cell restricted command execution for SWE-bench episodes.

Cell definitions (Appendix A.8)
--------------------------------
  A0 — Advisory,   -F: read-only file + git access; no code execution.
  A1 — Advisory,   +F: adds safe ``python`` script/module-pytest execution.
  P0 — Production, -F: same read-only restrictions as A0.
  P1 — Production, +F: same as A1; write_file/apply_edit are separate tools.

Security model (two-pass)
--------------------------
Pass 1 – Metacharacter scan on the raw string BEFORE any parsing.
  Any shell-special character ( ; & | < > ` \n, sequences && || $( ${ )
  causes immediate ``DisallowedCommandError``.  This blocks injection even
  when a caller builds a command from user-controlled strings.

Pass 2 – Argv allowlist check after ``shlex.split``.
  The parsed command must start with an allowlisted prefix for the cell.
  Per-command argument rules are enforced (e.g. sed requires -n; pytest
  requires an explicit path inside /testbed; python rejects -c).

Commands are executed via ``docker exec`` (NOT via a shell) so
metacharacters cannot have effect even if they pass validation.
This is defense-in-depth: validate THEN shell-free exec.

Adapter-level setup (e.g. ``pip install --no-index`` from a pre-downloaded
wheel directory) is NOT routed through this wrapper — it is performed by
the adapter before handing the container to the agent.
"""
from __future__ import annotations

import pathlib
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Cell taxonomy
# ---------------------------------------------------------------------------

class Cell(str, Enum):
    """Four-condition cell matrix from Appendix A.8."""
    A0 = "A0"  # Advisory, -F (no code execution)
    A1 = "A1"  # Advisory, +F
    P0 = "P0"  # Production, -F (no code execution)
    P1 = "P1"  # Production, +F

MINUS_F_CELLS: frozenset[Cell] = frozenset({Cell.A0, Cell.P0})
PLUS_F_CELLS: frozenset[Cell] = frozenset({Cell.A1, Cell.P1})


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class DisallowedCommandError(ValueError):
    """Raised when a command fails the metacharacter or allowlist check."""


# ---------------------------------------------------------------------------
# Metacharacter guard (Pass 1)
# ---------------------------------------------------------------------------

# Matches any character or sequence that would have shell-special meaning:
# semicolon, ampersand, pipe, redirections, backtick, newline, $() ${}
_METACHAR_RE = re.compile(
    r"[;&|<>`\n]"        # single-character metacharacters
    r"|\|\|"             # ||
    r"|&&"               # &&
    r"|\$\("             # $(
    r"|\$\{"             # ${
)


def _check_metacharacters(cmd_str: str) -> None:
    """Raise DisallowedCommandError if *cmd_str* contains shell metacharacters."""
    m = _METACHAR_RE.search(cmd_str)
    if m:
        raise DisallowedCommandError(
            f"Shell metacharacter {m.group()!r} detected in command: {cmd_str!r}"
        )


# ---------------------------------------------------------------------------
# Path guard
# ---------------------------------------------------------------------------

_TESTBED = "/testbed"


def _check_testbed_path(path: str) -> None:
    """Raise DisallowedCommandError if *path* escapes /testbed or uses ``..``."""
    p = pathlib.PurePosixPath(path)
    if ".." in p.parts:
        raise DisallowedCommandError(
            f"Path traversal ('..') is not allowed: {path!r}"
        )
    if p.is_absolute() and not str(p).startswith(_TESTBED):
        raise DisallowedCommandError(
            f"Absolute path must be under {_TESTBED!r}, got {path!r}"
        )


# ---------------------------------------------------------------------------
# Per-cell allowlist (Pass 2)
# ---------------------------------------------------------------------------

def _check_no_path_traversal(argv: list[str]) -> None:
    """Reject any non-flag argument that contains a ``..`` path component.

    Applies to all -F commands to prevent reads outside /testbed.
    """
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        parts = pathlib.PurePosixPath(arg).parts
        if ".." in parts:
            raise DisallowedCommandError(
                f"Path traversal ('..') is not allowed in argument {arg!r}"
            )


def _check_minus_f(argv: list[str]) -> None:
    """Enforce read-only allowlist for -F cells (A0, P0).

    Allowed: ls  cat  head  tail  grep  find
             sed -n ...
             git log | git show | git diff
    """
    cmd = argv[0]

    # Plain read-only filesystem commands — check for path traversal in args.
    if cmd in ("ls", "cat", "head", "tail", "grep", "find"):
        _check_no_path_traversal(argv)
        return

    # sed: ONLY the ``sed -n ...`` form is allowed (prevents in-place editing
    # with -i and arbitrary script injection).
    if cmd == "sed":
        if len(argv) < 2 or argv[1] != "-n":
            raise DisallowedCommandError(
                "sed is only allowed as 'sed -n ...' in -F cells; "
                "-i and other flags are not permitted"
            )
        return

    # git: read-only subcommands only.
    if cmd == "git":
        if len(argv) < 2:
            raise DisallowedCommandError("Bare 'git' is not allowed; use git log/show/diff")
        sub = argv[1]
        if sub in ("log", "show", "diff"):
            return
        raise DisallowedCommandError(
            f"'git {sub}' is not allowed in -F cells; allowed subcommands: log, show, diff"
        )

    raise DisallowedCommandError(
        f"Command {cmd!r} is not allowed in -F cells (A0/P0). "
        f"Allowed: ls, cat, head, tail, grep, find, sed -n, git log/show/diff"
    )


# Safe pytest flags that cannot execute arbitrary code or alter scope.
_PYTEST_SAFE_FLAGS: frozenset[str] = frozenset({
    "-v", "-vv", "-x", "-s", "-q", "--no-header",
    "--tb=short", "--tb=long", "--tb=line", "--tb=no", "--tb=auto",
})

# Pytest flags that can load plugins, exec code, or change paths — reject these.
_PYTEST_DANGEROUS_PREFIXES: tuple[str, ...] = (
    "--override-ini", "--confcutdir", "--rootdir", "--import-mode",
    "--assert", "-p ", "--fixtures", "--setup-plan", "--setup-show",
    "--last-failed", "--cache-", "--stepwise", "--collect-only",
    "--co", "--pyargs", "--noconftest",
)


def _check_pytest_args(args: list[str]) -> None:
    """Validate pytest arguments: require an explicit safe path; reject dangerous flags."""
    if not args:
        raise DisallowedCommandError(
            "pytest requires an explicit test path inside /testbed in +F cells; "
            "bare 'pytest' or 'python -m pytest' without a path is not allowed"
        )
    has_path = False
    for arg in args:
        if arg.startswith("-"):
            # Check against safe-flag set first
            if arg in _PYTEST_SAFE_FLAGS:
                continue
            # Check against dangerous prefixes
            for bad in _PYTEST_DANGEROUS_PREFIXES:
                if arg == bad.strip() or arg.startswith(bad.rstrip()):
                    raise DisallowedCommandError(
                        f"pytest flag {arg!r} is not allowed; it could execute arbitrary "
                        f"code or escape test scope"
                    )
            # Any other unknown flag is rejected conservatively
            raise DisallowedCommandError(
                f"pytest flag {arg!r} is not in the safe-flag list: "
                f"{sorted(_PYTEST_SAFE_FLAGS)}"
            )
        else:
            # Path argument
            _check_testbed_path(arg)
            has_path = True

    if not has_path:
        raise DisallowedCommandError(
            "pytest invocation has no test path argument; an explicit path "
            "inside /testbed is required in +F cells"
        )


def _check_python_safe(argv: list[str]) -> None:
    """Validate a python/python3 invocation for +F cells.

    Allowed:
      python -m pytest <path> [safe flags]   — pytest via module
      python <script.py>                     — run a script inside /testbed

    Rejected:
      python -c ...                          — inline code execution
      python -m <anything-other-than-pytest> — arbitrary module exec
      python (bare)                          — no op spec
    """
    if len(argv) < 2:
        raise DisallowedCommandError("Bare 'python' is not allowed; provide a script or -m pytest")

    flag = argv[1]

    if flag == "-c":
        raise DisallowedCommandError(
            "'python -c' is not allowed; it executes arbitrary inline code"
        )

    if flag == "-m":
        if len(argv) < 3:
            raise DisallowedCommandError("'python -m' requires a module name")
        module = argv[2]
        if module != "pytest":
            raise DisallowedCommandError(
                f"'python -m {module}' is not allowed; only 'python -m pytest' is permitted"
            )
        _check_pytest_args(argv[3:])
        return

    if flag.startswith("-"):
        raise DisallowedCommandError(
            f"python flag {flag!r} is not allowed in +F cells"
        )

    # Running a script: python <path>
    _check_testbed_path(flag)


def _check_plus_f(argv: list[str]) -> None:
    """Enforce +F allowlist (A1, P1): -F set plus safe python/pytest."""
    cmd = argv[0]

    # First check if it's a -F command (read-only commands stay available).
    try:
        _check_minus_f(argv)
        return
    except DisallowedCommandError:
        pass

    # +F additions: python/python3 (with restrictions) and bare pytest.
    if cmd in ("python", "python3"):
        _check_python_safe(argv)
        return

    # Allow bare ``pytest <path>`` as syntactic sugar; execution translates
    # it to ``python -m pytest <path>`` since the binary may not be in PATH.
    if cmd == "pytest":
        _check_pytest_args(argv[1:])
        return

    raise DisallowedCommandError(
        f"Command {cmd!r} is not allowed in +F cells (A1/P1). "
        f"Allowed: all -F commands, python/python3 (no -c), pytest <path>"
    )


# ---------------------------------------------------------------------------
# Public validation entry point
# ---------------------------------------------------------------------------

def validate_command(command: str | list[str], cell: Cell | str) -> list[str]:
    """Validate *command* for *cell* and return the parsed argv.

    Parameters
    ----------
    command:
        Either a shell-style string (parsed via shlex) or a pre-split argv list.
        String form triggers the metacharacter guard; list form skips it (caller
        is responsible for ensuring each token is safe).
    cell:
        One of A0, A1, P0, P1 (or a Cell enum value).

    Returns
    -------
    list[str]
        The validated argv ready for ``docker exec``.

    Raises
    ------
    DisallowedCommandError
        If the command fails the metacharacter or allowlist check.
    ValueError
        If *cell* is not a recognized cell label.
    """
    c = Cell(cell)

    if isinstance(command, str):
        # Pass 1: metacharacter scan
        _check_metacharacters(command)
        # Parse to argv
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise DisallowedCommandError(f"Cannot parse command string: {exc}") from exc
        if not argv:
            raise DisallowedCommandError("Empty command is not allowed")
    else:
        argv = list(command)
        if not argv:
            raise DisallowedCommandError("Empty command list is not allowed")

    # Pass 2: per-cell allowlist
    if c in MINUS_F_CELLS:
        _check_minus_f(argv)
    else:
        _check_plus_f(argv)

    return argv


# ---------------------------------------------------------------------------
# Execution result
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Result of one restricted ``docker exec`` invocation.

    This is the canonical shared result type for the SWE-bench adapter.
    All tool dispatch boundaries must use this type; serialization to the
    agent-visible wire format goes through ``as_tool_output()``.
    """
    cell: str
    command_raw: str | list[str]
    argv_executed: list[str]  # may differ from input (e.g. pytest → python -m pytest)
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    wall_seconds: float
    # True when the command was actually sent to and executed by the Docker
    # container and a result was returned.  False for policy/allowlist/
    # metacharacter rejections where no Docker exec was performed.
    executed: bool = True

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_tool_output(self) -> dict[str, Any]:
        """Serialize to the agent-visible tool output dict.

        This is the single canonical serialization path from ToolResult to the
        wire format consumed by the dispatcher and, ultimately, by the model.
        All dispatcher tool implementations must call this method; they must
        not construct the output dict ad-hoc from individual attributes.

        ``executed`` and ``rejected`` are machine-verifiable markers so that
        event-derived compliance analysis can deterministically distinguish
        container-executed commands from policy rejections.
        """
        return {
            "output": self.stdout,
            "stderr": self.stderr,
            "returncode": self.exit_code if self.exit_code is not None else 1,
            "timed_out": self.timed_out,
            "wall_seconds": round(self.wall_seconds, 3),
            # Machine-verifiable execution markers (see AGENTS.md §accounting).
            "executed": self.executed,
            "rejected": not self.executed,
        }

    @classmethod
    def make_rejection(
        cls,
        cell: str,
        command_raw: str | list[str],
        reason: str,
    ) -> "ToolResult":
        """Factory for a rejection result (command not allowed or container down).

        Use this instead of constructing ad-hoc dicts when exec_restricted
        raises ``DisallowedCommandError`` or the container is unavailable.
        The resulting ``ToolResult`` can be passed through ``as_tool_output()``
        like any other result.  ``executed=False`` marks this as a non-executed
        result so that compliance accounting never counts it as a feedback call.
        """
        return cls(
            cell=cell,
            command_raw=command_raw,
            argv_executed=[],
            exit_code=1,
            stdout="",
            stderr=reason,
            timed_out=False,
            wall_seconds=0.0,
            executed=False,
        )


# ---------------------------------------------------------------------------
# Public execution entry point
# ---------------------------------------------------------------------------

def exec_restricted(
    container_name: str,
    cell: Cell | str,
    command: str | list[str],
    *,
    workdir: str = "/testbed",
    timeout: int = 60,
) -> ToolResult:
    """Validate *command* for *cell* and execute it inside *container_name*.

    Parameters
    ----------
    container_name:
        Name of an already-running SWE-bench container.
    cell:
        One of A0, A1, P0, P1 (or a Cell enum value).
    command:
        Shell-style string or argv list.  String triggers metacharacter guard.
    workdir:
        Working directory inside the container (default ``/testbed``).
    timeout:
        Hard wall-clock timeout in seconds for the exec call.

    Returns
    -------
    ToolResult
        Contains stdout, stderr, exit code, and validation metadata.

    Raises
    ------
    DisallowedCommandError
        If the command fails validation.  The container is NOT touched.
    """
    argv = validate_command(command, cell)

    # Translate bare ``pytest <path>`` to ``python -m pytest <path>``
    # because the SWE-bench image may not have pytest on PATH.
    if argv[0] == "pytest":
        argv = ["python", "-m", "pytest"] + argv[1:]

    docker_cmd = [
        "docker", "exec",
        "--workdir", workdir,
        container_name,
        *argv,
    ]
    t0 = time.monotonic()
    proc = subprocess.Popen(
        docker_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=float(timeout))
        exit_code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_b, stderr_b = proc.communicate()
        stdout = stdout_b if isinstance(stdout_b, str) else (stdout_b or b"").decode(errors="replace")
        stderr = stderr_b if isinstance(stderr_b, str) else (stderr_b or b"").decode(errors="replace")
        exit_code = None
        timed_out = True

    wall = time.monotonic() - t0
    return ToolResult(
        cell=str(cell),
        command_raw=command,
        argv_executed=argv,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        wall_seconds=wall,
    )
