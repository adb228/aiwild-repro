"""Minimal SWE-bench container lifecycle and restricted exec wrapper.

Container lifecycle contract
-----------------------------
Every container managed here is:
  1. Given a unique name ``swe-<uuid12>`` for precise tracking.
  2. Labelled with ``delegation-study=true``, plus optional benchmark/milestone
     labels for observability.
  3. Started with ``--rm`` so Docker auto-removes it on clean exit/stop.
  4. Started with an explicit terminating command (``sleep <N>``) so it has a
     defined lifetime even if the finally-block is not reached.
  5. Stopped in a ``finally`` block; ``--rm`` then triggers auto-removal.
  6. Verified absent via ``docker ps -a --filter ancestor=<image>`` after cleanup.

Restricted exec wrapper
-----------------------
``exec_in_container`` is the ONLY path to run commands inside the container.
It enforces an explicit allowlist; any command not in the allowlist raises
``DisallowedCommandError`` before touching Docker.  Callers must NOT bypass
this wrapper with raw subprocess calls.

Security notes
--------------
- ``--network none``: no inbound or outbound network connections.
- ``--platform linux/amd64``: required for x86_64 SWE-bench images on ARM hosts.
- The container never mounts host paths; /testbed is entirely image-internal.
- No ``--privileged``, no capability additions beyond the image defaults.
"""
from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

LABEL_STUDY = "delegation-study"
LABEL_BENCHMARK = "benchmark"
LABEL_MILESTONE = "milestone"
LABEL_RUN_ID = "swe-run-id"  # exact run_id for ownership tracking

# ---------------------------------------------------------------------------
# Allowlisted exec commands
# ---------------------------------------------------------------------------
# Each entry is a tuple of the allowed argv prefix (exact positional match).
# The exec wrapper checks that the command starts with one of these prefixes.

_EXEC_ALLOWLIST: tuple[tuple[str, ...], ...] = (
    # Directory listing
    ("ls",),
    # Git queries — read-only, no writes
    ("git", "log"),
    ("git", "rev-parse"),
    ("git", "status"),
    ("git", "show"),
    ("git", "diff"),
)


class DisallowedCommandError(ValueError):
    """Raised when an exec command is not in the allowlist."""


class ContainerError(RuntimeError):
    """Raised when a container operation fails unexpectedly."""


class ImageMismatchError(ValueError):
    """Raised when a requested image does not belong to the given instance_id."""


# ---------------------------------------------------------------------------
# Image identity — derived from instance_id; mismatch is refused
# ---------------------------------------------------------------------------

def expected_image(instance_id: str, namespace: str = "swebench") -> str:
    """Return the Docker image name/tag the harness derives for *instance_id*.

    Matches ``swebench.task.checks.expected_image`` / evaluator._expected_image:
    ``{namespace}/sweb.eval.x86_64.{instance_id}:latest`` lowercased, with
    ``__`` replaced by ``_1776_``.
    """
    if not instance_id or not instance_id.strip():
        raise ImageMismatchError("instance_id is required to derive an image")
    key = f"sweb.eval.x86_64.{instance_id}:latest".lower()
    return f"{namespace}/{key}".replace("__", "_1776_")


def assert_image_matches_instance(image: str, instance_id: str) -> None:
    """Refuse *image* when it is not the image derived from *instance_id*."""
    expected = expected_image(instance_id)
    if image != expected:
        raise ImageMismatchError(
            f"Image {image!r} does not match instance_id {instance_id!r} "
            f"(expected {expected!r})"
        )


# Bind-mount flags that would expose host paths (including data/hidden, data/raw).
_MOUNT_FLAGS = frozenset({
    "-v", "--volume", "--mount", "--volumes-from",
})
_FORBIDDEN_HOST_TOKENS = ("data/hidden", "data/raw")


def assert_no_host_binds(docker_argv: list[str]) -> None:
    """Refuse docker argv that bind-mounts host paths or names hidden/raw data.

    Agent containers must stay image-internal: no ``-v`` / ``--volume`` /
    ``--mount``, and never ``data/hidden`` or ``data/raw`` on the command line.
    """
    for arg in docker_argv:
        flag = arg.split("=", 1)[0]
        if arg in _MOUNT_FLAGS or flag in _MOUNT_FLAGS:
            raise ContainerError(
                "Host bind mounts are forbidden on SWE containers"
            )
        for token in _FORBIDDEN_HOST_TOKENS:
            if token in arg:
                raise ContainerError(
                    "Benchmark hidden/raw paths must not appear on docker run"
                )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ExecResult:
    """Result of one ``docker exec`` invocation."""
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    wall_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Public: restricted exec wrapper
# ---------------------------------------------------------------------------

def exec_in_container(
    container_name: str,
    command: list[str],
    *,
    workdir: str = "/testbed",
    timeout: int = 30,
) -> ExecResult:
    """Run *command* inside *container_name* via ``docker exec``.

    Parameters
    ----------
    container_name:
        Name of the already-running container.
    command:
        Argv list for the command to run inside the container.
        Must start with an allowlisted prefix; raises ``DisallowedCommandError``
        otherwise.
    workdir:
        Working directory inside the container (default: ``/testbed``).
    timeout:
        Hard wall-clock timeout in seconds for this exec.

    Returns
    -------
    ExecResult
        Captured stdout, stderr, exit code, and wall time.

    Raises
    ------
    DisallowedCommandError
        If the command is not in the allowlist.
    subprocess.TimeoutExpired
        If the exec exceeds ``timeout`` seconds.
    """
    _check_allowlist(command)

    docker_cmd = [
        "docker", "exec",
        "--workdir", workdir,
        container_name,
        *command,
    ]
    t0 = time.monotonic()
    proc = subprocess.run(
        docker_cmd,
        capture_output=True,
        text=True,
        timeout=float(timeout),
    )
    wall = time.monotonic() - t0
    return ExecResult(
        command=command,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        wall_seconds=wall,
    )


def _check_allowlist(command: list[str]) -> None:
    """Raise DisallowedCommandError if *command* is not allowlisted."""
    if not command:
        raise DisallowedCommandError("Empty command is not allowed.")
    for prefix in _EXEC_ALLOWLIST:
        if len(command) >= len(prefix) and tuple(command[: len(prefix)]) == prefix:
            return
    raise DisallowedCommandError(
        f"Command {command!r} does not match any allowlisted prefix. "
        f"Allowed prefixes: {[list(p) for p in _EXEC_ALLOWLIST]}"
    )


# ---------------------------------------------------------------------------
# Public: container lifecycle
# ---------------------------------------------------------------------------

def start_swe_container(
    image: str,
    *,
    milestone: str = "M2",
    sleep_seconds: int = 300,
    platform: str = "linux/amd64",
    run_id: str | None = None,
    instance_id: str | None = None,
) -> str:
    """Start a detached SWE-bench container and return its unique name.

    The container is started with:
    - ``--rm``: auto-removed by Docker when stopped/killed.
    - ``--network none``: no network access.
    - ``--platform``: explicit platform for x86_64 images on ARM hosts.
    - An explicit ``sleep <N>`` command so the container terminates even if
      the caller's finally-block is not reached.
    - Labels: ``delegation-study=true``, ``benchmark=swebench``,
      ``milestone=<milestone>``.
    - No host bind mounts (``/testbed`` is image-internal).

    When *instance_id* is provided, *image* must be exactly
    ``expected_image(instance_id)``; a sibling-task image is refused before
    Docker is invoked.

    Parameters
    ----------
    image:
        Fully-qualified Docker image reference.
    milestone:
        Milestone label value (default: ``"M2"``).
    sleep_seconds:
        Lifetime of the container's sleep command (safety net; the caller's
        finally-block stops it well before this expires).
    platform:
        Container platform (must match image architecture).
    instance_id:
        Optional SWE-bench instance id.  When set, *image* is checked
        against the derived name/tag and a mismatch is refused.

    Returns
    -------
    str
        The unique container name ``swe-<uuid12>``.

    Raises
    ------
    ImageMismatchError
        If *instance_id* is set and *image* is not that instance's image.
    ContainerError
        If Docker fails to start the container, or the run argv would
        bind-mount host paths.
    """
    if instance_id is not None:
        assert_image_matches_instance(image, instance_id)

    uid = uuid.uuid4().hex[:12]
    name = f"swe-{uid}"

    cmd = [
        "docker", "run",
        "--detach",
        "--rm",                                         # auto-remove on stop
        f"--name={name}",
        f"--platform={platform}",
        "--network", "none",
        f"--label={LABEL_STUDY}=true",
        f"--label={LABEL_BENCHMARK}=swebench",
        f"--label={LABEL_MILESTONE}={milestone}",
        *(
            [f"--label={LABEL_RUN_ID}={run_id}"]
            if run_id is not None
            else []
        ),
        image,
        "sleep", str(sleep_seconds),                   # explicit terminating command
    ]
    assert_no_host_binds(cmd)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise ContainerError(
            f"Failed to start container from {image!r}: "
            f"rc={result.returncode} stderr={result.stderr.strip()!r}"
        )
    return name


def stop_swe_container(name: str) -> None:
    """Stop *name* (Docker ``--rm`` then auto-removes it).

    Best-effort: never raises.  Logs nothing — callers should log as needed.
    """
    try:
        subprocess.run(
            ["docker", "stop", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        pass  # Best-effort; fall through to force-remove


def force_remove_container(name: str) -> None:
    """Force-remove *name*.  Best-effort: never raises."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        pass


def verify_no_containers(image: str, *, timeout: int = 15) -> bool:
    """Return True if no containers (running or stopped) exist for *image*.

    Uses ``docker ps -a --filter ancestor=<image>`` to check.
    """
    result = subprocess.run(
        [
            "docker", "ps", "-a",
            "--filter", f"ancestor={image}",
            "--format", "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        timeout=float(timeout),
    )
    return not bool(result.stdout.strip())


def sweep_orphaned_swe_containers(
    runs_dir: "Path",
    *,
    log_fn: "Callable[[str], None] | None" = None,
) -> list[str]:
    """Remove orphaned SWE containers whose run directories have no episode.json.

    Only removes containers that have BOTH ``delegation-study=true`` AND
    ``benchmark=swebench`` labels.  Containers belonging to completed runs
    (with an episode.json), in-progress running episodes (run dir exists),
    and Spreadsheet containers are never touched.

    Parameters
    ----------
    runs_dir:
        The SWE worktree's local ``data/runs`` directory.
    log_fn:
        Optional callable for logging sanitized sweep events (no confidential data).

    Returns
    -------
    list[str]
        Names of containers that were removed.
    """
    from pathlib import Path as _Path  # avoid top-level import cycle

    def _log(msg: str) -> None:
        if log_fn is not None:
            log_fn(msg)

    # List all containers with BOTH required labels
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"label={LABEL_STUDY}=true",
                "--filter", f"label={LABEL_BENCHMARK}=swebench",
                "--format", "{{.Names}}\t{{.Label \"swe-run-id\"}}\t{{.State}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        _log(f"sweep_orphaned_swe_containers: docker ps failed: {type(exc).__name__}")
        return []

    removed: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        labeled_run_id = parts[1].strip() if len(parts) > 1 else ""
        state = parts[2].strip().lower() if len(parts) > 2 else ""

        # Decide if this container is orphaned:
        # - If labeled with a run_id, check if that run dir has episode.json.
        # - A running container whose run dir exists is an in-progress episode
        #   (concurrent siblings must not treat missing episode.json as orphan).
        # - If not labeled with run_id, check the ownership file.
        orphaned = False

        if labeled_run_id:
            run_dir = _Path(runs_dir) / labeled_run_id
            episode_json = run_dir / "episode.json"
            if episode_json.exists():
                orphaned = False
            elif state == "running" and run_dir.is_dir():
                orphaned = False
            elif not episode_json.exists():
                orphaned = True
        else:
            # Unlabeled with run_id: check ownership file by container name
            # (legacy containers without run_id label are swept conservatively)
            orphaned = True  # no way to map to a run → sweep

        if orphaned:
            _log(f"sweep: removing orphaned SWE container {name!r} (run_id={labeled_run_id!r})")
            force_remove_container(name)
            removed.append(name)
        else:
            _log(f"sweep: preserving active SWE container {name!r} (run_id={labeled_run_id!r})")

    return removed


def list_swe_containers() -> list[tuple[str, str]]:
    """Return (name, run_id) pairs for all SWE containers (both labels required)."""
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"label={LABEL_STUDY}=true",
                "--filter", f"label={LABEL_BENCHMARK}=swebench",
                "--format", "{{.Names}}\t{{.Label \"swe-run-id\"}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return []
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        name = parts[0].strip()
        run_id = parts[1].strip() if len(parts) > 1 else ""
        pairs.append((name, run_id))
    return pairs
