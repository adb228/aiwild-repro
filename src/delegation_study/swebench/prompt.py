"""SWE worker prompt renderer.

Only accepts ``SWETask`` — the public-safe type from ``task_loader``.
This prevents evaluator-only fields from leaking into agent-visible text.

The rendered prompt includes:
  - The repository identifier (repo, version)
  - The problem statement verbatim

It explicitly excludes (and never references):
  - hints_text
  - gold patch
  - test_patch
  - FAIL_TO_PASS
  - PASS_TO_PASS
"""
from __future__ import annotations

from delegation_study.swebench.task_loader import SWETask, FORBIDDEN_FIELDS


# ---------------------------------------------------------------------------
# Worker prompt template
# ---------------------------------------------------------------------------

_WORKER_TEMPLATE = """\
You are an expert software engineer.

Repository: {repo} (version {version})
Task: {instance_id}

## Problem Statement

{problem_statement}

## Instructions

Investigate the problem described above in the repository at base commit \
{base_commit}.

- Read the repository source code and any relevant tests.
- Produce a patch (`candidate.patch`) as a unified diff against base commit \
{base_commit} that fixes the described problem.
- Your patch must modify only non-test source files.
- Do not include the gold solution, hints, or test harness artefacts in your \
reasoning or patch.
""".strip()


# ---------------------------------------------------------------------------
# Public renderer — type signature enforces SWETask
# ---------------------------------------------------------------------------

def build_worker_prompt(task: SWETask) -> str:
    """Render the agent-visible worker prompt for *task*.

    Parameters
    ----------
    task:
        A ``SWETask`` (public type).  Passing a ``_RawTaskRecord`` or raw
        dict is a type error — the type annotation is the enforcement.

    Returns
    -------
    str
        Fully rendered prompt text.  Contains no forbidden fields.
    """
    if not isinstance(task, SWETask):
        raise TypeError(
            f"build_worker_prompt requires a SWETask; got {type(task).__name__}. "
            f"Never pass _RawTaskRecord or raw dicts to the prompt renderer."
        )
    return _WORKER_TEMPLATE.format(
        repo=task.repo,
        version=task.version,
        instance_id=task.instance_id,
        problem_statement=task.problem_statement,
        base_commit=task.base_commit,
    )


# ---------------------------------------------------------------------------
# Confidential scan utility
# ---------------------------------------------------------------------------

def check_prompt_excludes_forbidden(
    prompt: str,
    forbidden_values: dict[str, str | list[str]],
) -> dict[str, str]:
    """Scan *prompt* for any literal value from *forbidden_values*.

    Emits only field-level PASS/FAIL — never echoes failing content.

    Parameters
    ----------
    prompt:
        The rendered prompt string to scan.
    forbidden_values:
        Mapping of field_name → value (str) or values (list[str]).
        Values are checked for exact substring membership.

    Returns
    -------
    dict[str, str]
        ``{"field_name": "PASS"}`` or ``{"field_name": "FAIL"}`` for each
        field.  "PASS" means the value is absent from the prompt.
    """
    results: dict[str, str] = {}
    for field_name, value in forbidden_values.items():
        if isinstance(value, str):
            to_check = [value]
        else:
            to_check = list(value)

        found = False
        for v in to_check:
            # Only check non-trivial values (skip empty strings that could
            # trivially match or not match meaninglessly).
            if len(v) >= 4 and v in prompt:
                found = True
                break
        results[field_name] = "FAIL" if found else "PASS"
    return results
