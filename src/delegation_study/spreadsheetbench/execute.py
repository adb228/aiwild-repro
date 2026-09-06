"""Docker sandbox execution for SpreadsheetBench solution scripts.

Security contract
-----------------
Every invocation enforces ALL of the following flags; none may be overridden
by callers:
    --rm                    remove container on exit (always present)
    --network none          no outbound or inbound connections
    --read-only             container root FS is read-only
    --pids-limit 128        fork-bomb / runaway process guard
    --memory 2g             OOM guard
    --cpus 2                CPU cap
    --security-opt no-new-privileges  prevent privilege escalation
    --tmpfs /tmp            small writable tmpfs for /tmp only

Container lifecycle contract
-----------------------------
Every container is:
  1. Given a unique name  ``sbench-{execution_id}``  for precise tracking.
  2. Labelled with  delegation-study=true, benchmark=spreadsheetbench,
     run_id, role, execution_id.
  3. Started with ``--rm`` so a clean exit removes it automatically.
  4. Given a hard wall-timeout via subprocess.Popen + communicate(timeout=…).
  5. On timeout: ``docker rm -f <name>`` is called immediately, then
     verified via ``docker ps -a``, before the result is returned.

Because ``subprocess.run(…, timeout=…)`` raises TimeoutExpired WITHOUT
killing the child docker-run process, we use Popen + communicate() instead.
subprocess.run is NOT used for container invocations.

Stale container detection
--------------------------
Call ``check_stale_containers()`` before starting any episode to detect
containers from a previous interrupted run.  It fails closed with
``StaleContainerError`` if any delegation-study+spreadsheetbench containers
are still running.  The check filters on BOTH ``delegation-study=true`` AND
``benchmark=spreadsheetbench`` so that containers from other benchmarks
(e.g. ``benchmark=swebench``) running concurrently are not flagged.

Mounts (per-invocation, never shared):
    /input/input.xlsx   :ro   clean task input  (never raw benchmark data)
    /output             :rw   task-local output directory
    /solution/solution.py :ro  the script being executed

macOS uid fallback
------------------
`docker run --user nobody` can fail on macOS (Docker Desktop) because the
bind-mounted output directory is owned by the host user and nobody (uid 65534)
cannot write to it.  ``run_in_sandbox`` tries nobody first; if the container
exits within 5 s with "Permission denied" in its output it retries with the
host uid:gid and records which user mode was actually used in
``ExecutionResult.user_mode``.  Callers should propagate that value to the
manifest so reproducibility records stay accurate.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Default image built from docker/Dockerfile
DEFAULT_IMAGE = "spreadsheet-exec:protocol-v01"

# Label key used on every delegation-study container.
# All episode containers carry this label for stale-detection and cleanup.
LABEL_STUDY = "delegation-study"
LABEL_RUN_ID = "run_id"
LABEL_ROLE = "role"
LABEL_EXECUTION_ID = "execution_id"
# Optional batch label: when set, containers from the SAME batch are treated
# as intentionally concurrent (not stale).  Used when four conditions run
# simultaneously in separate tmux panes that share a batch UUID.
LABEL_BATCH_ID = "delegation-study-batch"

# Benchmark-scope labels: every Spreadsheet container carries BOTH of these so
# that stale checks, preflight gates, and post-batch assertions can use two
# Docker ``--filter label=...`` arguments and thereby ignore containers started
# by other benchmarks (e.g. ``benchmark=swebench``) running concurrently.
LABEL_BENCHMARK = "benchmark"
BENCHMARK_VALUE = "spreadsheetbench"

# These security flags are non-negotiable and applied to every run.
_FIXED_FLAGS: tuple[str, ...] = (
    "--network", "none",
    "--read-only",
    "--pids-limit", "128",
    "--memory", "2g",
    "--cpus", "2",
    "--security-opt", "no-new-privileges",
    # Small writable tmpfs so Python can create temp files (e.g. NamedTemp*)
    "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777",
)

# Threshold: if the container exits this quickly with a permission error, it
# almost certainly never ran the user script → safe to retry with host uid.
_PERM_FAILURE_WALL_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Stale container detection
# ---------------------------------------------------------------------------

class StaleContainerError(RuntimeError):
    """Raised before an episode starts if delegation-study containers are running.

    This indicates a previous episode was interrupted without cleaning up its
    containers.  Resolve by running ``docker rm -f <IDs>`` then retry.
    """


def check_stale_containers(batch_id: str | None = None) -> None:
    """Fail closed if any running Spreadsheet-study containers are detected.

    Checks ``docker ps`` for containers carrying BOTH
    ``delegation-study=true`` AND ``benchmark=spreadsheetbench``.  Using
    two ``--filter label=...`` arguments (Docker AND semantics) means
    containers from other benchmarks (e.g. ``benchmark=swebench``) that
    happen to share the ``delegation-study=true`` label are ignored —
    only Spreadsheet containers can trigger this gate.

    Raises ``StaleContainerError`` listing their IDs and names so the caller
    (or operator) can remove them before starting a new episode.

    This must be called before any agent prompt or API call is constructed.

    Parameters
    ----------
    batch_id:
        Optional UUID shared by all concurrent conditions in the same run
        batch (e.g. four tmux panes launched together).  Containers that carry
        the matching ``delegation-study-batch=<batch_id>`` label are expected
        concurrent peers — they are NOT treated as stale.  Containers from a
        different batch (or with no batch label) are still treated as stale.

    Raises
    ------
    StaleContainerError
        If one or more stale containers (not belonging to *batch_id*) are found.
    OSError / subprocess errors
        If Docker is not available (treated as infrastructure failure by callers).
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", f"label={LABEL_STUDY}=true",
                "--filter", f"label={LABEL_BENCHMARK}={BENCHMARK_VALUE}",
                # Include labels so we can filter by batch_id in Python
                "--format", r"{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Labels}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        # Docker not installed — let the caller handle as infrastructure failure
        raise
    except subprocess.TimeoutExpired:
        raise StaleContainerError(
            "docker ps timed out during stale-container check; "
            "Docker may be unresponsive."
        )

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    stale_entries: list[str] = []
    for ln in lines:
        parts = ln.split("\t")
        cid = parts[0] if parts else ln
        name = parts[1] if len(parts) > 1 else "?"
        status = parts[2] if len(parts) > 2 else "?"
        labels_str = parts[3] if len(parts) > 3 else ""

        # If a batch_id was provided, check whether this container belongs to
        # the same batch.  If so, it is an intentional concurrent peer — skip.
        if batch_id:
            # Docker formats labels as "key=value,key=value,..."
            container_batch: str | None = None
            for kv in labels_str.split(","):
                kv = kv.strip()
                if kv.startswith(f"{LABEL_BATCH_ID}="):
                    container_batch = kv[len(f"{LABEL_BATCH_ID}="):]
                    break
            if container_batch == batch_id:
                continue  # Same batch — not stale

        stale_entries.append(f"{cid} ({name}) [{status}]")

    if stale_entries:
        detail = "; ".join(stale_entries)
        raise StaleContainerError(
            f"Found {len(stale_entries)} stale delegation-study+spreadsheetbench "
            f"container(s) still running. "
            f"Details: {detail}. "
            "Kill them first with:  docker rm -f <ID>  "
            "then verify with:  docker ps "
            "--filter label=delegation-study=true "
            "--filter label=benchmark=spreadsheetbench"
        )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Full result of one sandboxed execution."""

    # Exit code from the container process.  None means the wall timeout
    # expired before the container exited.
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool

    # "nobody" or "{uid}:{gid}" – which Docker user flag was actually used.
    user_mode: str

    # Wall-clock seconds the container was running (includes Docker startup).
    wall_seconds: float

    # Path to output.xlsx if it was produced; None otherwise.
    output_path: Path | None

    # The container name used for this execution (for traceability).
    container_name: str = ""

    @property
    def succeeded(self) -> bool:
        """True iff exit code is 0 and output was produced."""
        return self.exit_code == 0 and self.output_path is not None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_in_sandbox(
    script_path: Path,
    input_path: Path,
    output_dir: Path,
    *,
    image: str = DEFAULT_IMAGE,
    wall_timeout: int = 120,
    pass_std_args: bool = True,
    host_uid_gid: tuple[int, int] | None = None,
    run_id: str = "unknown",
    role: str = "unknown",
    execution_id: str | None = None,
    batch_id: str | None = None,
) -> ExecutionResult:
    """Execute *script_path* inside the Docker security sandbox.

    Parameters
    ----------
    script_path:
        Path to the Python script on the host.  It is mounted read-only at
        ``/solution/solution.py`` inside the container.
    input_path:
        Host path to the input XLSX file, mounted read-only at
        ``/input/input.xlsx``.  Must be the clean task input — never raw
        benchmark data or a hidden evaluator file.
    output_dir:
        Host directory for output, mounted read-write at ``/output``.
        Must be a task-local directory (never a shared path).
    image:
        Docker image to run (must be ``spreadsheet-exec:protocol-v01`` or a
        content-addressable digest thereof).
    wall_timeout:
        Maximum seconds to wait for the container before killing it and
        returning a timed-out result.  On timeout the container is explicitly
        force-removed and removal is verified.
    pass_std_args:
        If True (default, used for production scripts), the container command
        is ``python /solution/solution.py --input /input/input.xlsx
        --output /output/output.xlsx``.
        If False (used for ephemeral exploratory code), the command is just
        ``python /solution/solution.py`` — the script accesses
        ``/input/input.xlsx`` directly.
    host_uid_gid:
        Override the fallback uid:gid (default: ``os.getuid():os.getgid()``).
        Ignored when ``--user nobody`` succeeds.
    run_id:
        Passed as the ``run_id`` container label for traceability.
    role:
        Passed as the ``role`` container label ("worker", "parent", etc.).
    execution_id:
        Unique identifier for this specific container invocation.  Auto-
        generated (UUID4 prefix) if not provided.  Used as the container name
        ``sbench-<execution_id>`` and the ``execution_id`` label.

    Returns
    -------
    ExecutionResult
        Includes ``user_mode`` and ``container_name`` fields.
    """
    script_path = script_path.resolve()
    input_path = input_path.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate a unique execution_id for this container invocation
    eid = execution_id if execution_id is not None else uuid.uuid4().hex[:12]
    # Docker container name: must be [a-zA-Z0-9][a-zA-Z0-9_.-]*
    container_name = f"sbench-{eid}"

    # Build the in-container command
    if pass_std_args:
        container_cmd: list[str] = [
            "python", "/solution/solution.py",
            "--input", "/input/input.xlsx",
            "--output", "/output/output.xlsx",
        ]
    else:
        container_cmd = ["python", "/solution/solution.py"]

    # Build the optional batch label (used when concurrent conditions run together)
    _batch_labels: list[str] = (
        [f"--label={LABEL_BATCH_ID}={batch_id}"] if batch_id else []
    )

    def _build_cmd(user: str) -> list[str]:
        return [
            "docker", "run",
            "--rm",                                # auto-remove on clean exit
            f"--name={container_name}",            # unique name for force-remove on timeout
            f"--user={user}",
            # Ownership/traceability labels (both study + benchmark for scoped filtering)
            f"--label={LABEL_STUDY}=true",
            f"--label={LABEL_BENCHMARK}={BENCHMARK_VALUE}",
            f"--label={LABEL_RUN_ID}={run_id}",
            f"--label={LABEL_ROLE}={role}",
            f"--label={LABEL_EXECUTION_ID}={eid}",
            *_batch_labels,                        # empty unless batch_id provided
            *_FIXED_FLAGS,
            # Mounts (task-local only; never benchmark archive or hidden data)
            "-v", f"{input_path}:/input/input.xlsx:ro",
            "-v", f"{output_dir}:/output",
            "-v", f"{script_path}:/solution/solution.py:ro",
            image,
            *container_cmd,
        ]

    # ── First attempt: --user nobody ─────────────────────────────────────────
    result = _docker_run(_build_cmd("nobody"), wall_timeout, container_name)
    user_mode = "nobody"

    # ── macOS fallback ───────────────────────────────────────────────────────
    # If the container failed extremely quickly with a permission error it
    # almost certainly hit a Docker Desktop bind-mount ownership issue.
    if not result.timed_out and result.exit_code != 0 and _is_mount_perm_error(result):
        uid, gid = host_uid_gid if host_uid_gid is not None else (os.getuid(), os.getgid())
        user_mode = f"{uid}:{gid}"
        # Generate a new execution_id for the fallback container (different name)
        fallback_eid = uuid.uuid4().hex[:12]
        fallback_name = f"sbench-{fallback_eid}"
        _fallback_batch_labels: list[str] = (
            [f"--label={LABEL_BATCH_ID}={batch_id}"] if batch_id else []
        )

        def _build_fallback_cmd(u: str) -> list[str]:
            return [
                "docker", "run",
                "--rm",
                f"--name={fallback_name}",
                f"--user={u}",
                f"--label={LABEL_STUDY}=true",
                f"--label={LABEL_BENCHMARK}={BENCHMARK_VALUE}",
                f"--label={LABEL_RUN_ID}={run_id}",
                f"--label={LABEL_ROLE}={role}",
                f"--label={LABEL_EXECUTION_ID}={fallback_eid}",
                *_fallback_batch_labels,
                *_FIXED_FLAGS,
                "-v", f"{input_path}:/input/input.xlsx:ro",
                "-v", f"{output_dir}:/output",
                "-v", f"{script_path}:/solution/solution.py:ro",
                image,
                *container_cmd,
            ]
        result = _docker_run(_build_fallback_cmd(user_mode), wall_timeout, fallback_name)
        container_name = fallback_name

    result.user_mode = user_mode
    result.container_name = container_name

    # Determine whether output was produced
    output_xlsx = output_dir / "output.xlsx"
    result.output_path = output_xlsx if output_xlsx.is_file() else None

    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _force_remove_container(name: str, *, timeout: int = 30) -> bool:
    """Force-remove a container by name and verify it's gone.

    Returns True if the container was confirmed removed, False if it still
    exists after removal attempt.  Never raises — best-effort cleanup.
    """
    try:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            text=True,
            timeout=float(timeout),
        )
    except Exception:
        pass  # Best-effort; proceed to verification

    # Verify removal: docker ps -a --filter name=^/<name>$
    try:
        check = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"name=^/{name}$",
                "--format", "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        still_present = bool(check.stdout.strip())
        return not still_present
    except Exception:
        return False  # Can't confirm; assume not removed


def _is_mount_perm_error(result: ExecutionResult) -> bool:
    """Return True if *result* looks like a Docker bind-mount permission failure."""
    combined = (result.stdout + result.stderr).lower()
    return (
        result.wall_seconds < _PERM_FAILURE_WALL_SECONDS
        and "permission denied" in combined
    )


def _docker_run(cmd: list[str], wall_timeout: int, container_name: str) -> ExecutionResult:
    """Launch a docker container via Popen and wait with a hard wall-clock timeout.

    IMPORTANT: We use ``Popen + communicate(timeout=…)`` instead of
    ``subprocess.run(timeout=…)`` because ``subprocess.run`` does NOT kill the
    child ``docker run`` process when the Python timeout fires — the container
    keeps running.  With Popen we can explicitly kill the process and
    force-remove the container on timeout.

    Parameters
    ----------
    cmd:
        Full docker run command (must include ``--rm`` and ``--name``).
    wall_timeout:
        Seconds to wait before killing.
    container_name:
        The ``--name`` value from *cmd*, used for ``docker rm -f`` on timeout.

    Returns
    -------
    ExecutionResult
        ``user_mode`` and ``output_path`` are sentinel blanks for the caller
        to fill.  ``container_name`` is empty (caller fills it).
    """
    t_start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=float(wall_timeout))
        wall = time.monotonic() - t_start
        return ExecutionResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            user_mode="",
            wall_seconds=wall,
            output_path=None,
            container_name="",
        )
    except subprocess.TimeoutExpired:
        wall = time.monotonic() - t_start
        # Step 1: Force-remove the container (--rm won't fire until it exits)
        _force_remove_container(container_name)
        # Step 2: Kill the Python subprocess that's waiting for docker
        proc.kill()
        stdout_b, stderr_b = proc.communicate()
        stdout = stdout_b if isinstance(stdout_b, str) else (stdout_b or b"").decode(errors="replace")
        stderr = stderr_b if isinstance(stderr_b, str) else (stderr_b or b"").decode(errors="replace")
        return ExecutionResult(
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            user_mode="",
            wall_seconds=wall,
            output_path=None,
            container_name="",
        )
