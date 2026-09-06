"""SWE-bench container workspace and tool dispatcher.

Each workspace owns exactly one Docker container (worker or parent).
The container is started at ``create()`` and torn down at ``cleanup()``.
File I/O uses ``docker cp``; shell commands use the M3 restricted exec wrapper.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .container import (
    start_swe_container,
    force_remove_container,
    sweep_orphaned_swe_containers,
    expected_image,
    assert_image_matches_instance,
)
from .tools import exec_restricted, DisallowedCommandError, ToolResult


class SubmitValidationError(Exception):
    """Raised by submit_memo / submit_solution on validation failure.

    The agent loop catches it as a generic Exception, stores str(exc) as
    tool_error, and returns the full message to the model as a tool_result
    with error != null.  The loop does NOT set submitted_terminal, so the
    phase continues and the model can correct and resubmit.
    """

# Global registry of live SWE containers: {container_name: ownership_file_path}
# Used for atexit cleanup so processes killed mid-run still clean up.
_LIVE_CONTAINERS: dict[str, Path] = {}

# ── Constants ────────────────────────────────────────────────────────────────

SWE_IMAGE = "swebench/sweb.eval.x86_64.django_1776_django-14752:latest"
TESTBED = "/testbed"


def _raw_docker_exec(
    container_name: str,
    command: list[str],
    workdir: str = TESTBED,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a raw docker exec command without the restricted allowlist.

    This is ONLY for internal workspace management (mkdir, git apply, etc.),
    where the command is fully controlled by the workspace manager, not agents.
    """
    return subprocess.run(
        ["docker", "exec", "--workdir", workdir, container_name] + command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_str(text: str) -> str:
    return _sha256_bytes(text.encode())


# ── SWE Workspace ────────────────────────────────────────────────────────────

class SWEWorkspace:
    """Container-backed workspace for one SWE-bench agent phase.

    Parameters
    ----------
    container_name:
        Name for the Docker container (must be unique).
    role:
        "worker" or "parent".
    run_id:
        Episode run ID (used as Docker label).
    batch_id:
        Optional batch ID label.
    """

    def __init__(
        self,
        container_name: str,
        role: str,
        run_id: str,
        batch_id: str | None = None,
    ) -> None:
        self.container_name = container_name
        self.role = role
        self.run_id = run_id
        self.batch_id = batch_id
        self._container_id: str | None = None
        self._started = False

    @classmethod
    def create(
        cls,
        role: str,
        run_id: str,
        batch_id: str | None = None,
        *,
        runs_dir: Path | None = None,
        instance_id: str | None = None,
        image: str | None = None,
    ) -> "SWEWorkspace":
        """Start a fresh container and return the workspace.

        Before starting, performs a preflight stale-container sweep using both
        SWE labels to ensure any orphaned containers from previous crashed runs
        are removed.  Only containers with BOTH ``delegation-study=true`` and
        ``benchmark=swebench`` labels are swept; Spreadsheet containers are
        never touched.

        When *instance_id* is provided the image is derived from that id
        (``expected_image``).  A caller-supplied *image* that does not match
        is refused before Docker is invoked, so task X never receives task Y's
        image.
        """
        from .run import _RUNS_DIR as _default_runs_dir  # avoid circular at module level
        _runs_dir = runs_dir or _default_runs_dir
        # Preflight: sweep orphaned SWE containers before starting a new one
        swept = sweep_orphaned_swe_containers(_runs_dir)
        if swept:
            import logging
            logging.getLogger(__name__).info(
                "Preflight sweep removed orphaned SWE containers: %s", swept
            )

        suffix = uuid.uuid4().hex[:8]
        name = f"swe-{role}-{run_id[:8]}-{suffix}"
        ws = cls(container_name=name, role=role, run_id=run_id, batch_id=batch_id)
        ws._runs_dir = _runs_dir
        ws._instance_id = instance_id
        ws._requested_image = image
        ws._start()
        return ws

    def _resolve_image(self) -> str:
        """Derive the container image, refusing a sibling-task mismatch."""
        instance_id = getattr(self, "_instance_id", None)
        requested = getattr(self, "_requested_image", None)
        if instance_id:
            derived = expected_image(instance_id)
            if requested is not None:
                assert_image_matches_instance(requested, instance_id)
            return derived
        return requested or SWE_IMAGE

    def _start(self) -> None:
        """Start the container, write ownership registry, register atexit cleanup."""
        image = self._resolve_image()
        generated_name = start_swe_container(
            image,
            milestone="M6",
            sleep_seconds=3600,  # safety net; explicit cleanup happens first
            run_id=self.run_id,  # label container with run_id for ownership tracking
            instance_id=getattr(self, "_instance_id", None),
        )
        self.container_name = generated_name
        self._container_id = generated_name
        self._started = True

        # Write per-run ownership file BEFORE agent execution so any
        # subsequent preflight sweep can identify this container.
        runs_dir = getattr(self, "_runs_dir", None)
        if runs_dir is not None:
            ownership_path = (
                Path(runs_dir) / self.run_id / f"container_{self.role}.txt"
            )
            ownership_path.parent.mkdir(parents=True, exist_ok=True)
            ownership_path.write_text(
                f"{generated_name}\n",
                encoding="utf-8",
            )
            _LIVE_CONTAINERS[generated_name] = ownership_path
        else:
            _LIVE_CONTAINERS[generated_name] = Path("/dev/null")

    def exec(self, command: str, cell: str = "P1") -> ToolResult:
        """Run a validated command in the container via M3 restricted exec.

        Parameters
        ----------
        command:
            Shell command string (validated against M3 allowlist).
        cell:
            Cell type: "P0" (read-only) or "P1" (read/write).

        Returns
        -------
        ToolResult
            The canonical shared result type.  Callers must call
            ``.as_tool_output()`` to get the agent-visible wire dict.
            On rejection or infrastructure error, returns a synthetic
            ``ToolResult`` via ``ToolResult.make_rejection()``.
        """
        if not self._started:
            return ToolResult.make_rejection(
                cell=cell,
                command_raw=command,
                reason="container not started",
            )
        try:
            return exec_restricted(
                container_name=self.container_name,
                command=command,
                cell=cell,
            )
        except DisallowedCommandError as e:
            return ToolResult.make_rejection(
                cell=cell,
                command_raw=command,
                reason=f"[REJECTED] {e}",
            )
        except Exception as e:
            return ToolResult.make_rejection(
                cell=cell,
                command_raw=command,
                reason=f"[ERROR] {e}",
            )

    def read_file(self, path: str) -> str:
        """Read a file from /testbed inside the container."""
        if not self._started:
            raise RuntimeError("Container not started")
        # Validate path is under /testbed
        if not path.startswith(TESTBED + "/") and path != TESTBED:
            raise ValueError(f"Path must be under {TESTBED}: {path!r}")
        if ".." in path:
            raise ValueError(f"Path traversal rejected: {path!r}")

        result = _raw_docker_exec(self.container_name, ["cat", path])
        if result.returncode != 0:
            raise FileNotFoundError(f"Cannot read {path!r}: {result.stderr}")
        return result.stdout

    def write_file(self, path: str, content: str) -> None:
        """Write text content to a path inside /testbed."""
        if not self._started:
            raise RuntimeError("Container not started")
        if not path.startswith(TESTBED + "/"):
            raise ValueError(f"Path must be under {TESTBED}: {path!r}")
        if ".." in path:
            raise ValueError(f"Path traversal rejected: {path!r}")

        # Write to a temp file and docker cp into container
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as tf:
            tf.write(content)
            tmp_path = tf.name
        try:
            import os
            os.chmod(tmp_path, 0o600)
            # Ensure parent directory exists in container
            parent = str(Path(path).parent)
            _raw_docker_exec(self.container_name, ["mkdir", "-p", parent])
            # Copy file into container
            subprocess.run(
                ["docker", "cp", tmp_path, f"{self.container_name}:{path}"],
                check=True,
                capture_output=True,
            )
        finally:
            import os
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def apply_patch(self, patch_content: str) -> tuple[bool, str]:
        """Apply a patch in /testbed. Returns (success, message)."""
        if not self._started:
            return False, "Container not started"
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False
        ) as tf:
            tf.write(patch_content)
            tmp_path = tf.name
        try:
            os.chmod(tmp_path, 0o600)
            # Copy patch to container
            container_patch = "/tmp/_apply.patch"
            subprocess.run(
                ["docker", "cp", tmp_path, f"{self.container_name}:{container_patch}"],
                check=True,
                capture_output=True,
            )
            # git apply --check
            check = _raw_docker_exec(
                self.container_name,
                ["git", "apply", "--check", container_patch],
                workdir=TESTBED,
            )
            if check.returncode != 0:
                return False, f"git apply --check failed: {check.stderr}"
            # git apply
            apply = _raw_docker_exec(
                self.container_name,
                ["git", "apply", container_patch],
                workdir=TESTBED,
            )
            if apply.returncode != 0:
                return False, f"git apply failed: {apply.stderr}"
            return True, "applied"
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def extract_diff(self) -> str:
        """Extract git diff --binary from /testbed (uncommitted changes)."""
        if not self._started:
            raise RuntimeError("Container not started")
        result = _raw_docker_exec(
            self.container_name,
            ["git", "diff", "--binary"],
            workdir=TESTBED,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git diff failed: {result.stderr}")
        return result.stdout

    def cleanup(self) -> None:
        """Force-remove the container and deregister from the atexit registry.

        Idempotent: safe to call multiple times.
        """
        if self._started and self.container_name:
            try:
                force_remove_container(self.container_name)
            except Exception:
                pass
            # Deregister from the global atexit registry so the handler
            # doesn't attempt to remove an already-gone container.
            _LIVE_CONTAINERS.pop(self.container_name, None)
            self._started = False


# ── Atexit cleanup ───────────────────────────────────────────────────────────

def _atexit_cleanup() -> None:
    """Best-effort cleanup of all registered live SWE containers on exit.

    Invoked from ``atexit`` (normal exit / unhandled exception) and from
    the SIGTERM handler.  Does NOT run on SIGKILL.
    Idempotent: already-removed containers are silently skipped.
    """
    for container_name in list(_LIVE_CONTAINERS.keys()):
        force_remove_container(container_name)
        _LIVE_CONTAINERS.pop(container_name, None)


# Previous SIGTERM handler, chained after our cleanup so other libraries
# (and tests) still see the signal.
_PREV_SIGTERM = signal.getsignal(signal.SIGTERM)
_SIGTERM_INSTALLED = False


def _handle_sigterm(signum: int, frame: object) -> None:
    """Remove every registered SWE container, then chain or exit."""
    _atexit_cleanup()
    previous = _PREV_SIGTERM
    if callable(previous) and previous not in (signal.SIG_DFL, signal.SIG_IGN):
        previous(signum, frame)
        return
    raise SystemExit(128 + signum)


def _install_sigterm_handler() -> None:
    """Install ``_handle_sigterm`` once per process."""
    global _SIGTERM_INSTALLED, _PREV_SIGTERM
    if _SIGTERM_INSTALLED:
        return
    _PREV_SIGTERM = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    _SIGTERM_INSTALLED = True


atexit.register(_atexit_cleanup)
_install_sigterm_handler()


# ── SWE Tool Dispatcher ──────────────────────────────────────────────────────

# A-cell worker: exec_shell, read_file, write_file, submit_memo (no patch submission)
_WORKER_A_TOOLS = frozenset({
    "exec_shell", "read_file", "write_file", "submit_memo"
})
# P-cell worker: exec_shell, read_file, write_file, submit_memo, submit_solution
_WORKER_P_TOOLS = frozenset({
    "exec_shell", "read_file", "write_file", "submit_memo", "submit_solution"
})
# Alias preserved for backward compatibility
_WORKER_P1_TOOLS = _WORKER_P_TOOLS
# Parent: exec_shell, read_file, write_file, submit_solution
_PARENT_TOOLS = frozenset({
    "exec_shell", "read_file", "write_file", "submit_solution"
})


class SWEToolDispatcher:
    """Duck-typed ToolDispatcher for SWE-bench container workspaces.

    Implements the same interface as spreadsheetbench ToolDispatcher:
    - ``allowed_tools() -> list[str]``
    - ``call(tool_name, **kwargs) -> Any``
    - ``seal_handoff() -> dict``
    - ``feedback_call_count: int``
    - ``scratch_destroyed: bool``  (always False for SWE)
    """

    def __init__(
        self,
        workspace: SWEWorkspace,
        condition: str,
        role: str,
        run_id: str,
        handoff_dir: Path,
        on_submit_memo: Callable[[dict], None] | None = None,
        events_path: Path | None = None,
        max_turns: int = 25,
    ) -> None:
        self.workspace = workspace
        self.condition = condition
        self.role = role
        self.run_id = run_id
        self.handoff_dir = handoff_dir
        self._on_submit_memo = on_submit_memo
        # Turn-tracking for "turns remaining: k" annotation
        self._events_path: Path | None = events_path
        self._max_turns: int = max_turns

        if role == "parent":
            self._allowed = _PARENT_TOOLS
        elif condition in ("A0", "A1"):
            self._allowed = _WORKER_A_TOOLS  # A-cells: memo only, no patch
        else:
            self._allowed = _WORKER_P_TOOLS  # P-cells: memo + solution patch

        # Compliance
        self.feedback_call_count: int = 0  # exec_shell with python/pytest
        self.scratch_destroyed: bool = False  # N/A for SWE
        self._sealed = False

        # Sealed artifacts
        self._memo: dict | None = None
        self._patch: str | None = None
        self._patch_path: Path | None = None

    def allowed_tools(self) -> list[str]:
        return sorted(self._allowed)

    # ── Turns-remaining tracking ─────────────────────────────────────────────

    def _current_turn(self) -> int:
        """Count model_call events for this role in the events log.

        All tool calls dispatched within one model response share the same
        model_call event, so this count equals the 1-based index of the
        current model turn.  Reading the append-only file on every dispatch
        is intentional: it gives accurate per-turn counts without requiring
        changes to the shared agent_loop.
        """
        if self._events_path is None or not self._events_path.exists():
            return 0
        count = 0
        try:
            with open(self._events_path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if (
                            ev.get("role") == self.role
                            and ev.get("event_type") == "model_call"
                        ):
                            count += 1
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
        return count

    def _turns_remaining(self) -> int:
        """Return clamped remaining turns for the current role."""
        return max(0, self._max_turns - self._current_turn())

    def _annotate_turns(self, result: dict[str, Any]) -> dict[str, Any]:
        """Append 'turns remaining: k' to every tool output dict.

        The line is appended to the 'output' string so it is visible to the
        agent on every tool result (success, rejection, or error).
        """
        k = self._turns_remaining()
        result = dict(result)  # shallow copy; do not mutate callers' dicts
        result["output"] = (result.get("output") or "") + f"\nturns remaining: {k}"
        return result

    def call(self, tool_name: str, **kwargs: Any) -> Any:
        if tool_name not in self._allowed:
            from ..spreadsheetbench.tools import ToolNotAllowedError
            raise ToolNotAllowedError(
                f"Tool '{tool_name}' not permitted for condition={self.condition!r} "
                f"role={self.role!r}. Allowed: {sorted(self._allowed)}"
            )
        impl = getattr(self, f"_tool_{tool_name}", None)
        if impl is None:
            raise NotImplementedError(f"No implementation for '{tool_name}'")
        result = impl(**kwargs)
        # Annotate every tool result with the current turns remaining.
        # _tool_* methods return dicts; wrap-through _annotate_turns is safe.
        if isinstance(result, dict):
            result = self._annotate_turns(result)
        return result

    # ── Tool implementations ─────────────────────────────────────────────────

    def _tool_exec_shell(self, command: str) -> dict[str, Any]:
        """Execute a validated shell command in the container.

        ``workspace.exec()`` returns the canonical ``ToolResult`` type.
        Serialization to the agent-visible wire format goes through
        ``ToolResult.as_tool_output()`` — the single authoritative path.

        Cell mapping (−F = no code execution; +F = code execution allowed):
          A0/P0 → "A0" / "P0" (-F restriction)
          A1/P1 → "A1" / "P1" (+F restriction)
        The condition is used directly as the cell label so exec_restricted
        enforces the scientifically correct restriction for each cell.
        """
        # Use the condition-appropriate cell so -F cells (A0/P0) correctly
        # restrict code execution and +F cells (A1/P1) allow it.
        cell = self.condition
        result: ToolResult = self.workspace.exec(command, cell=cell)

        cmd_lower = command.strip().split()[0] if command.strip() else ""
        # feedback_command: this command type CAN produce observable test output.
        is_feedback = cmd_lower in ("python", "pytest", "python3")
        # Only count commands that were ACTUALLY EXECUTED in the Docker container.
        # Rejected commands (policy/allowlist/metacharacter) return ToolResult
        # with executed=False — the container never ran anything, so they must
        # never increment the feedback counter regardless of command prefix.
        if result.executed and is_feedback:
            self.feedback_call_count += 1

        # Single canonical serialization — never construct the dict ad-hoc here.
        # Add feedback_command marker so event-derived QC can reconstruct the
        # executed-only count from events.jsonl without reading private traces.
        output = result.as_tool_output()
        output["feedback_command"] = is_feedback
        return output

    def _tool_read_file(self, path: str) -> dict[str, Any]:
        """Read file from container."""
        try:
            content = self.workspace.read_file(path)
            return {"content": content, "path": path}
        except Exception as e:
            return {"content": None, "path": path, "error": str(e)}

    def _tool_write_file(self, path: str, content: str) -> dict[str, Any]:
        """Write file to container."""
        try:
            self.workspace.write_file(path, content)
            return {"path": path, "written": True}
        except Exception as e:
            return {"path": path, "written": False, "error": str(e)}

    # ── Memo/solution validation constants ──────────────────────────────────

    # All 7 required memo fields, their expected type, and a human-readable
    # description.  Validated exhaustively before any write so the model
    # receives a single, complete error listing ALL defects at once.
    _MEMO_FIELDS: "list[tuple[str, type, str]]" = [
        ("task_understanding", str,  "non-empty string"),
        ("target_locations",   list, "array of strings"),
        ("proposed_approach",  list, "array of strings"),
        ("assumptions",        list, "array of strings"),
        ("observed_feedback",  list, "array of strings"),
        ("known_risks",        list, "array of strings"),
        ("validation_plan",    list, "array of strings"),
    ]

    def _tool_submit_memo(self, memo: dict[str, Any]) -> dict[str, Any]:
        """Validate all 7 memo fields then atomically persist.

        On any validation failure this method raises ``SubmitValidationError``
        which propagates through ``call()`` and is caught by the agent loop as
        a tool-level error.  The loop logs it, returns the message to the model
        (as a tool_result with ``error != null``), and continues without setting
        ``submitted_terminal``.  Only a successful return (no raise) counts as
        a submission.
        """
        issues: list[str] = []

        if not isinstance(memo, dict):
            raise SubmitValidationError(
                "submit_memo: memo must be a JSON object; "
                f"received type={type(memo).__name__!r}"
            )

        for field, expected_type, description in self._MEMO_FIELDS:
            if field not in memo:
                issues.append(
                    f"  - '{field}': MISSING (expected {description})"
                )
                continue
            val = memo[field]
            if not isinstance(val, expected_type):
                issues.append(
                    f"  - '{field}': wrong type — expected {description}, "
                    f"got {type(val).__name__!r} (value preview: "
                    f"{str(val)[:60]!r})"
                )
            elif expected_type is list:
                bad = [i for i, v in enumerate(val) if not isinstance(v, str)]
                if bad:
                    issues.append(
                        f"  - '{field}': items at indices {bad} are not strings"
                    )
            elif expected_type is str and not val:
                issues.append(f"  - '{field}': must not be empty")

        if issues:
            raise SubmitValidationError(
                "submit_memo validation failed — ALL issues must be fixed before "
                "resubmitting:\n" + "\n".join(issues)
            )

        # Validation passed — write atomically
        self._memo = memo
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        memo_path = self.handoff_dir / "memo.json"
        memo_path.write_text(json.dumps(memo, indent=2) + "\n")
        if self._on_submit_memo:
            self._on_submit_memo(memo)
        sha = _sha256_str(json.dumps(memo, indent=2) + "\n")
        return {"path": str(memo_path), "sha256": sha}

    def _tool_submit_solution(self) -> dict[str, Any]:
        """Extract git diff, validate non-empty, then atomically persist.

        Raises ``SubmitValidationError`` if the diff is unavailable or empty so
        the model can add changes and resubmit.  Only a successful return counts
        as a terminal submission.
        """
        try:
            diff = self.workspace.extract_diff()
        except Exception as e:
            raise SubmitValidationError(
                f"submit_solution: could not extract diff from /testbed — "
                f"{type(e).__name__}: {e}.  Make sure you have committed or "
                f"modified files in /testbed before calling submit_solution."
            )

        if not diff or not diff.strip():
            raise SubmitValidationError(
                "submit_solution: the diff is empty — no changes detected in "
                "/testbed relative to the base commit.  Apply your fix before "
                "calling submit_solution."
            )

        suffix = "candidate" if self.role == "worker" else "final"
        patch_name = f"{suffix}.patch"
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        patch_path = self.handoff_dir / patch_name
        patch_path.write_text(diff)
        self._patch = diff
        self._patch_path = patch_path
        sha = _sha256_str(diff)
        return {"path": str(patch_path), "sha256": sha}

    def seal_handoff(self) -> dict[str, Any]:
        """Return handoff metadata dict (compatible with schema)."""
        self._sealed = True
        memo_path = self.handoff_dir / "memo.json"
        patch_suffix = "candidate" if self.role == "worker" else "final"
        patch_path = self.handoff_dir / f"{patch_suffix}.patch"

        memo_p = str(memo_path) if memo_path.exists() else None
        memo_sha = _sha256_str(memo_path.read_text()) if memo_path.exists() else None
        patch_p = str(patch_path) if patch_path.exists() else None
        patch_sha = _sha256_str(patch_path.read_text()) if patch_path.exists() else None

        return {
            "memo_path": memo_p,
            "memo_sha256": memo_sha,
            "candidate_script_path": patch_p,   # reusing script field for patch
            "candidate_script_sha256": patch_sha,
            "worker_submitted_memo": memo_p is not None,
            "worker_submitted_candidate": patch_p is not None,
        }
