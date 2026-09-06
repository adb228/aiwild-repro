"""Construction-safe SWE-bench task loader.

Public / private boundary
--------------------------
This module enforces a strict two-tier design:

  Public tier — ``SWETask``
    The only type accepted by prompt-rendering and logging APIs.
    Contains ``problem_statement``, ``instance_id``, ``base_commit``,
    ``repo``, ``version``, and ``difficulty``.
    **Never** contains ``hints_text``, ``patch``, ``test_patch``,
    ``FAIL_TO_PASS``, or ``PASS_TO_PASS``.

  Private tier — ``_RawTaskRecord``
    Holds every field from the source JSON, including the five forbidden
    fields.  Used exclusively by the evaluator pipeline.  Never returned
    from ``load_task()`` and never accepted by any prompt/logging function.

The name-based privacy (``_`` prefix) plus the separate type make it
structurally impossible to accidentally pass evaluator-only data to a
prompt renderer: the renderer's type annotation accepts only ``SWETask``.

Data source
-----------
Tasks are loaded from the local ignored file:
  ``config/task_selection_v03/swe_verified_local.json``

This file is gitignored in both the primary checkout and this worktree.
The worktree accesses it through an ignored symlink that points at the
primary checkout's copy.  The file is never printed, logged, or echoed
by this module.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Forbidden field names — single source of truth used by loader + tests
# ---------------------------------------------------------------------------

FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "hints_text",
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
})

# Safe fields surfaced on the public object
_PUBLIC_FIELDS: frozenset[str] = frozenset({
    "instance_id",
    "problem_statement",
    "base_commit",
    "repo",
    "version",
    "difficulty",
})

# Default path to the task data file (relative to the worktree root)
_DEFAULT_DATA_PATH = (
    pathlib.Path(__file__).resolve().parent  # swebench/
    .parent.parent.parent                    # src/ -> worktree root
    / "config" / "task_selection_v03" / "swe_verified_local.json"
)


# ---------------------------------------------------------------------------
# Public type — safe for prompt rendering and logging
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SWETask:
    """Agent-visible task object.

    Construction-safe: only ``_PUBLIC_FIELDS`` are present.
    No ``hints_text``, ``patch``, ``test_patch``, ``FAIL_TO_PASS``,
    or ``PASS_TO_PASS`` can appear here.

    This type is the ONLY type accepted by ``build_worker_prompt``.
    """
    instance_id: str
    problem_statement: str
    base_commit: str
    repo: str
    version: str
    difficulty: str

    def public_dict(self) -> dict[str, str]:
        """Return a dict suitable for logging — contains no forbidden fields."""
        return {
            "instance_id": self.instance_id,
            "problem_statement": self.problem_statement,
            "base_commit": self.base_commit,
            "repo": self.repo,
            "version": self.version,
            "difficulty": self.difficulty,
        }


# ---------------------------------------------------------------------------
# Private type — evaluator-only; never exposed to prompts or logs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _RawTaskRecord:
    """Full raw record including all evaluator-only fields.

    NEVER pass this to any prompt-rendering or logging function.
    The leading underscore is a deliberate barrier; any attempt to
    reference ``_RawTaskRecord`` outside this module is an error.
    """
    instance_id: str
    problem_statement: str
    base_commit: str
    repo: str
    version: str
    difficulty: str
    # Forbidden fields stored with private names to prevent accidental use
    _hints_text: str
    _patch: str
    _test_patch: str
    _fail_to_pass: list[str]
    _pass_to_pass: list[str]

    @classmethod
    def _from_raw(cls, record: dict[str, Any]) -> "_RawTaskRecord":
        """Build from a raw JSON record dict (private factory, evaluator only)."""
        return cls(
            instance_id=str(record.get("instance_id", "")),
            problem_statement=str(record.get("problem_statement", "")),
            base_commit=str(record.get("base_commit", "")),
            repo=str(record.get("repo", "")),
            version=str(record.get("version", "")),
            difficulty=str(record.get("difficulty", "")),
            _hints_text=str(record.get("hints_text", "")),
            _patch=str(record.get("patch", "")),
            _test_patch=str(record.get("test_patch", "")),
            _fail_to_pass=list(record.get("FAIL_TO_PASS", [])),
            _pass_to_pass=list(record.get("PASS_TO_PASS", [])),
        )

    def to_public(self) -> SWETask:
        """Strip forbidden fields and return the public-safe ``SWETask``."""
        return SWETask(
            instance_id=self.instance_id,
            problem_statement=self.problem_statement,
            base_commit=self.base_commit,
            repo=self.repo,
            version=self.version,
            difficulty=self.difficulty,
        )


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------

def _build_index(data_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Load the JSON file and return a dict keyed by instance_id.

    Raises FileNotFoundError if the file does not exist.
    Never prints or logs any record values.
    """
    with data_path.open("r", encoding="utf-8") as fh:
        records: list[dict[str, Any]] = json.load(fh)
    if not isinstance(records, list):
        raise TypeError(f"Expected JSON list; got {type(records).__name__}")
    return {str(r["instance_id"]): r for r in records if "instance_id" in r}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_task(
    instance_id: str,
    *,
    data_path: pathlib.Path | None = None,
) -> SWETask:
    """Load *instance_id* and return a ``SWETask`` with no forbidden fields.

    Parameters
    ----------
    instance_id:
        The SWE-bench instance identifier (e.g. ``django__django-14752``).
    data_path:
        Override the default path to ``swe_verified_local.json``.
        Intended for tests only.

    Returns
    -------
    SWETask
        Public-safe task object.

    Raises
    ------
    FileNotFoundError
        If the data file does not exist.
    KeyError
        If *instance_id* is not found in the file.
    """
    path = data_path or _DEFAULT_DATA_PATH
    index = _build_index(path)
    if instance_id not in index:
        raise KeyError(f"instance_id {instance_id!r} not found in {path}")
    raw = _RawTaskRecord._from_raw(index[instance_id])
    return raw.to_public()


# ---------------------------------------------------------------------------
# Evaluator-only loader (private — never called from prompt/logging code)
# ---------------------------------------------------------------------------

def _load_raw(
    instance_id: str,
    *,
    data_path: pathlib.Path | None = None,
) -> _RawTaskRecord:
    """Load the full record including evaluator-only fields.

    PRIVATE.  Must never be called from prompt rendering or logging code.
    """
    path = data_path or _DEFAULT_DATA_PATH
    index = _build_index(path)
    if instance_id not in index:
        raise KeyError(f"instance_id {instance_id!r} not found in {path}")
    return _RawTaskRecord._from_raw(index[instance_id])
