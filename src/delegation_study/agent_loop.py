"""Agent conversation loop for the delegation study.

Implements one agent phase (worker or parent) as a multi-turn tool-use loop
against the Anthropic Messages API.

Design invariants
-----------------
* Boundary is enforced through separate workspaces and separate
  ToolDispatcher instances — not through prompt prose.  The parent
  ToolDispatcher operates on a fresh workspace created from clean public
  input; it has no reference to any worker workspace path.
* Worker private trace (detailed tool inputs/outputs) is written only to
  the private log passed in as ``private_logger``.  The public events.jsonl
  only receives high-level model_call / tool_call / tool_result events with
  safe-to-expose fields.
* Worker execution feedback (run_solution output, errors) may include error
  text and disposable output inspection, but the hidden golden / evaluator
  is never accessible inside the worker sandbox.
* Budget enforcement: max_turns (API calls), max_tool_calls, max_wall_seconds,
  and cumulative max_output_tokens_total.
* Cumulative input tokens are measured but are not a scientific budget.
  max_input_tokens_safety is a separate emergency safety ceiling.

Public API
----------
    result = run_agent_phase(
        role="worker",
        condition="P1",
        client=model_client,
        dispatcher=worker_dispatcher,
        system_prompt=...,
        initial_user_message=...,
        budget=manifest["budgets"]["worker"],
        event_logger=event_logger,
        private_logger=private_logger,   # worker only; None for parent
    )
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model_client import ModelClient, Role
from .records import EventLogger
from .spreadsheetbench.tools import ToolDispatcher


# ---------------------------------------------------------------------------
# Tool schema definitions (Anthropic tool format)
# ---------------------------------------------------------------------------

# Shared by all conditions that include the tool.
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_sheets": {
        "name": "list_sheets",
        "description": (
            "Return the names of all worksheets in the workbook. "
            "No parameters required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "workbook_metadata": {
        "name": "workbook_metadata",
        "description": (
            "Return structural metadata for all sheets: name, dimensions, "
            "max_row, max_col, and named ranges."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "read_range": {
        "name": "read_range",
        "description": (
            "Read cell values AND formula text from a rectangular range. "
            "Returns 'rows' (cached values, data_only=True) and a parallel "
            "'formulas' matrix (formula text like '=SUM(A1:A10)' where present, "
            "null for non-formula cells). "
            "range_ref accepts 'Sheet1!A1:B4', 'A1:B4', 'B6' (single cell), "
            "or 'A:G' (full-column range). Sheet names with spaces: use quotes "
            "e.g. \"'My Sheet'!A1:B5\". "
            "sheet is optional; overridden by an inline sheet prefix."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "range_ref": {
                    "type": "string",
                    "description": (
                        "Cell range reference, e.g. 'Sheet1!A1:D10', 'A1:B4', "
                        "'B6', 'A:G', or \"'Sheet With Spaces'!A1:C5\"."
                    ),
                },
                "sheet": {
                    "type": "string",
                    "description": "Sheet name (optional if included in range_ref).",
                },
            },
            "required": ["range_ref"],
        },
    },
    "search_workbook": {
        "name": "search_workbook",
        "description": (
            "Substring search across ALL cells in ALL sheets, matching against "
            "both cached cell values and formula text. max_results caps match "
            "output but does NOT limit inspection scope — every cell is examined. "
            "Each match includes 'value' (cached) and 'formula' (formula text or null)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to search for.",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "If true, match case exactly (default false).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matches to return (default 100).",
                },
            },
            "required": ["query"],
        },
    },
    "write_solution": {
        "name": "write_solution",
        "description": (
            "Write or overwrite solution.py in the workspace. "
            "The script must accept --input and --output CLI arguments and write "
            "the result to /output/output.xlsx when run as: "
            "python solution.py --input /input/input.xlsx --output /output/output.xlsx"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Complete Python source code for the solution script.",
                },
            },
            "required": ["code"],
        },
    },
    "run_solution": {
        "name": "run_solution",
        "description": (
            "Execute the current solution.py in the secure sandbox against the "
            "clean task input. Returns exit_code, stdout, stderr, and whether "
            "output.xlsx was produced. Call write_solution first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "submit_memo": {
        "name": "submit_memo",
        "description": (
            "Submit the handoff memo. Requires exactly these 7 fields: "
            "task_understanding (string), target_locations (array of strings), "
            "proposed_approach (array of strings), assumptions (array of strings), "
            "observed_feedback (array of strings), known_risks (array of strings), "
            "validation_plan (array of strings)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "memo": {
                    "type": "object",
                    "description": "The memo object with all 7 required fields.",
                    "properties": {
                        "task_understanding": {"type": "string"},
                        "target_locations": {
                            "type": "array", "items": {"type": "string"}
                        },
                        "proposed_approach": {
                            "type": "array", "items": {"type": "string"}
                        },
                        "assumptions": {
                            "type": "array", "items": {"type": "string"}
                        },
                        "observed_feedback": {
                            "type": "array", "items": {"type": "string"}
                        },
                        "known_risks": {
                            "type": "array", "items": {"type": "string"}
                        },
                        "validation_plan": {
                            "type": "array", "items": {"type": "string"}
                        },
                    },
                    "required": [
                        "task_understanding", "target_locations", "proposed_approach",
                        "assumptions", "observed_feedback", "known_risks",
                        "validation_plan",
                    ],
                },
            },
            "required": ["memo"],
        },
    },
    "submit_solution": {
        "name": "submit_solution",
        "description": (
            "Seal the current solution.py as the final/candidate script. "
            "Must have called write_solution first. "
            "For the worker: call this together with submit_memo to complete the handoff. "
            "For the parent: calling this seals the final script and completes the episode."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "run_ephemeral_python": {
        "name": "run_ephemeral_python",
        "description": (
            "A1 only. Run arbitrary Python code in the sandbox against a scratch copy "
            "of the input. The script accesses /input/input.xlsx directly. "
            "Output files and the scratch directory are discarded after this call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Has access to /input/input.xlsx.",
                },
            },
            "required": ["code"],
        },
    },
    # ── SWE-bench tools (P1 worker and parent) ───────────────────────────────
    "exec_shell": {
        "name": "exec_shell",
        "description": (
            "Run a shell command inside /testbed (network-isolated container). "
            "Read-only commands (ls, cat, head, tail, grep, find, git log/show/diff, sed -n) "
            "are always allowed. Write-capable Python and pytest are allowed in +F cells. "
            "Shell metacharacters (;, &&, ||, |, >, <, backticks, $()) are rejected. "
            "Use read_file/write_file for file I/O."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run in /testbed. No metacharacters.",
                },
            },
            "required": ["command"],
        },
    },
    "read_file": {
        "name": "read_file",
        "description": (
            "Read the full text content of a file inside /testbed. "
            "Path must be within /testbed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path inside /testbed (e.g. /testbed/setup.py).",
                },
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "name": "write_file",
        "description": (
            "Write text content to a file inside /testbed. "
            "Creates parent directories as needed. "
            "Path must be within /testbed and must not escape via '..'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path inside /testbed to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
}


def _build_tool_list(allowed_tools: list[str]) -> list[dict[str, Any]]:
    """Build the Anthropic-format tool list for the given allowed tool names."""
    return [
        _TOOL_SCHEMAS[name]
        for name in allowed_tools
        if name in _TOOL_SCHEMAS
    ]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class AgentPhaseResult:
    """Result of one completed agent phase (worker or parent)."""

    # Usage counters
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    validation_cycles: int = 0   # run_solution / run_ephemeral_python calls
    wall_seconds: float = 0.0

    # Terminal condition
    terminated_normally: bool = False   # True iff the phase used its terminal tool

    # Failure info (None if terminated_normally)
    failure_code: str | None = None
    failure_message: str | None = None

    # Budget exhaustion tracking
    # budget_exhausted=True when the phase stopped due to any budget cap
    # budget_exhausted_on identifies which cap fired (null when budget_exhausted=False)
    budget_exhausted: bool = False
    budget_exhausted_on: str | None = None

    # Safety exhaustion is intentionally separate from scientific budgets.
    safety_exhausted: bool = False
    safety_exhausted_on: str | None = None

    # swe-0.2: reprompt and end_reason tracking (None when feature not used)
    reprompted_on_end_turn: int = 0   # count of prose-only reprompts injected (swe-0.2 only)
    end_reason: str | None = None     # canonical termination reason string

    # swe-0.3: state-sealed completion fields
    terminal_assistant_text: str | None = None  # last assistant text at seal time
    seal_source: str | None = None  # "natural_end_turn_sealed" | "explicit_submit" | "cap_sealed" | "max_tokens_sealed" | "unexpected_stop_sealed" | None

    # swe-0.3.2: max_tokens continuation and unexpected-stop recording
    continued_on_max_tokens: int = 0
    unexpected_stop_reason: str | None = None

    def to_usage_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "validation_cycles": self.validation_cycles,
            "wall_seconds": round(self.wall_seconds, 3),
            "budget_exhausted": self.budget_exhausted,
            "budget_exhausted_on": self.budget_exhausted_on,
            "safety_exhausted": self.safety_exhausted,
            "safety_exhausted_on": self.safety_exhausted_on,
        }


# ---------------------------------------------------------------------------
# Agent phase runner
# ---------------------------------------------------------------------------

_ASSISTANT_TEXT_INLINE_LIMIT = 4096  # bytes (inclusive)
# Separator inserted between consecutive text blocks in a single response.
# Uses a plain newline so no content is invented; tests assert this value.
_TEXT_BLOCK_SEPARATOR = "\n"
# Never written into events. Named so tests can assert we do not emit it.
# sklearn-14983 A0 omitted empty assistant_text; QC then reported MISSING.
_ASSISTANT_TEXT_MISSING_SENTINEL = "MISSING"

# swe-0.3.2: continue a role after a per-call max_tokens cut-off.
_MAX_TOKENS_CONTINUATION_LIMIT = 3
_MAX_TOKENS_CONTINUATION_TEXT = "Your previous response was cut off; continue."


def _seal_terminal_stop(result: AgentPhaseResult, *, text: str, end_reason: str, seal_source: str) -> None:
    """Apply swe-0.3.1 state-seal: this terminal stop ends the role (no re-prompt-for-submit)."""
    result.terminal_assistant_text = text
    result.terminated_normally = True
    result.end_reason = end_reason
    result.seal_source = seal_source


def _coerce_assistant_text(value: Any) -> str:
    """Return a str suitable for assistant_text persistence.

    None and non-str values become ``""`` so empty / tool-only responses stay
    recordable (``assistant_text: ""``) instead of crashing ``str.join`` or
    omitting the field.
    """
    return value if isinstance(value, str) else ""


def _extract_assistant_text(content: list[Any]) -> str:
    """Extract and join all text blocks from an API response content list.

    Design choices
    --------------
    * Text blocks only: ``{"type": "text", "text": "..."}`` (Anthropic SDK).
      Tool-use blocks, image blocks, etc. are silently ignored.
    * Blocks are joined in their original order with ``_TEXT_BLOCK_SEPARATOR``
      (a single newline) so no content is invented.
    * A response with no text blocks (tool-only or empty) returns ``""``.
      This satisfies the every-event invariant: ``assistant_text: ""`` is a
      valid empty string, not absent and not the sentinel ``MISSING``.
    * The returned string is always a ``str`` (None text blocks coerce to ``""``).
    """
    parts: list[str] = []
    for block in content:
        # Support both object-style (Anthropic SDK) and dict-style blocks.
        if hasattr(block, "type"):
            if block.type == "text":
                parts.append(_coerce_assistant_text(getattr(block, "text", "")))
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(_coerce_assistant_text(block.get("text")))
    return _TEXT_BLOCK_SEPARATOR.join(parts)


def _persist_assistant_text(
    text: str,
    run_dir: Path,
    role: str,
    call_index: int,
) -> dict[str, str]:
    """Persist assistant text and return the payload fragment for model_call event.

    Returns exactly one of:
    - ``{"assistant_text": text}``   — when UTF-8 encoding is <=4096 bytes (inclusive)
    - ``{"raw_payload_path": rel}``  — when >4096 bytes; file written atomically
                                       under ``run_dir/logs/model_calls/``
                                       before the event references it.

    Empty string is a first-class recorded value: ``{"assistant_text": ""}``.
    The field is never omitted for empty/tool-only/whitespace-only responses
    and is never the sentinel ``MISSING`` (sklearn-14983 A0 worker bug).

    The path is run-relative (``logs/model_calls/<role>_<call_index:05d>.txt``)
    and contains no ``..`` components.  The raw bytes round-trip exactly:
    ``Path(run_dir / path).read_bytes() == text.encode("utf-8")``.
    """
    text = _coerce_assistant_text(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= _ASSISTANT_TEXT_INLINE_LIMIT:
        return {"assistant_text": text}

    # >4096 bytes: write to file first, then reference it.
    log_dir = run_dir / "logs" / "model_calls"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Unique, no-traversal filename: role + zero-padded call index.
    fname = f"{role}_{call_index:05d}.txt"
    dest = log_dir / fname
    # Atomic write: write to temp file in same directory, then rename.
    tmp_path = dest.with_suffix(".tmp")
    try:
        tmp_path.write_bytes(encoded)
        tmp_path.rename(dest)
    except Exception:
        # Clean up temp if rename failed; let caller handle the exception.
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    rel_path = f"logs/model_calls/{fname}"
    return {"raw_payload_path": rel_path}


def _model_call_assistant_text_fields(
    content: list[Any],
    model_call_text_dir: "Path | None",
    role: str,
    call_index: int,
) -> dict[str, str]:
    """Attach assistant text to a successful model_call payload.

    Spreadsheet / default (``model_call_text_dir is None``): returns ``{}`` so
    existing Spreadsheet events stay unchanged.

    SWE path (dir set): every successful model_call — including empty,
    whitespace-only, and tool-only responses — yields exactly one of
    ``assistant_text`` (inline, ≤4096 UTF-8 bytes, including ``""``) or
    ``raw_payload_path`` (>4096). Empty text is recorded as ``""``, never
    omitted, never ``MISSING``.
    """
    if model_call_text_dir is None:
        return {}
    return _persist_assistant_text(
        _extract_assistant_text(content),
        model_call_text_dir,
        role,
        call_index,
    )


# ---------------------------------------------------------------------------
# Bounded tool output (swe-0.3)
# ---------------------------------------------------------------------------

_TOOL_OUTPUT_HEAD_LINES = 60
_TOOL_OUTPUT_TAIL_LINES = 60
_TOOL_OUTPUT_MAX_BYTES = 8192  # total bytes for the bounded tool_content_str
_TOOL_OUTPUT_REPLACEMENT_CHAR = "\ufffd"  # Unicode replacement character


def _truncate_text_lines(text: str, head: int = _TOOL_OUTPUT_HEAD_LINES,
                         tail: int = _TOOL_OUTPUT_TAIL_LINES) -> tuple[str, bool, int]:
    """Truncate text to head+tail lines. Returns (text, was_truncated, original_lines).

    Avoids overlap: if head+tail >= total lines, returns text unchanged.
    The truncation marker is appended between head and tail sections.
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)
    if n <= head + tail:
        return text, False, n
    omitted = n - head - tail
    marker = f"[... {omitted} lines omitted by bounded output ...]\n"
    return "".join(lines[:head]) + marker + "".join(lines[n - tail:]), True, n


def _encode_safe_utf8(text: str, max_bytes: int) -> bytes:
    """Encode text to UTF-8, truncating at a valid codepoint boundary.

    Deterministic: never splits a multi-byte character. Appends a
    truncation marker when bytes were cut. If ``max_bytes`` is smaller
    than the marker, returns a prefix of the marker.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded
    marker = b"[BYTE-TRUNCATED]"
    if max_bytes <= len(marker):
        return marker[:max_bytes]
    cutoff = max_bytes - len(marker)
    cut = encoded[:cutoff]
    # Strip a trailing incomplete UTF-8 sequence: first any continuation
    # bytes (10xxxxxx), then a leftover leading byte (11xxxxxx). Leaving the
    # lead byte would decode as U+FFFD and is not a valid codepoint boundary.
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    if cut and (cut[-1] & 0xC0) == 0xC0:
        cut = cut[:-1]
    return cut + marker


def _bound_and_log_tool_output(
    tool_output: dict,
    log_dir: "Path",
    role: str,
    tool_call_index: int,
) -> dict:
    """Apply deterministic bounded output to a tool result containing stdout/stderr.

    Applies to any tool result dict that has ``output`` or ``stderr`` keys
    (primarily exec_shell). Other keys (returncode, wall_seconds, etc.) are
    preserved unchanged.

    Algorithm:
    1. Write full stdout+stderr to disk atomically before truncation.
    2. Apply 60-line head + 60-line tail truncation to each stream separately,
       avoiding overlap.
    3. If the total JSON payload exceeds 8192 bytes, apply secondary byte
       truncation to ``output`` then ``stderr`` to fit.
    4. Attach full-log SHA-256, run-relative path, original line/byte counts,
       and truncated flag to the returned dict.

    This function is a no-op for tool outputs lacking ``output``/``stderr`` keys
    (e.g. read_file, write_file, submit_memo, submit_solution).

    Streams are kept separate (not combined) in the on-disk log.
    """
    import hashlib as _hashlib

    if "output" not in tool_output and "stderr" not in tool_output:
        return tool_output

    full_stdout = str(tool_output.get("output") or "")
    full_stderr = str(tool_output.get("stderr") or "")

    # ── 1. Write full output to disk BEFORE any truncation ──────────────
    log_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{role}_{tool_call_index:05d}_tool_output.json"
    log_path = log_dir / fname
    rel_path = f"logs/tool_outputs/{fname}"

    import json as _json
    full_payload = _json.dumps(
        {"stdout": full_stdout, "stderr": full_stderr},
        ensure_ascii=False,
    )
    full_bytes_raw = full_payload.encode("utf-8", errors="replace")
    full_sha256 = _hashlib.sha256(full_bytes_raw).hexdigest()
    original_stdout_lines = len(full_stdout.splitlines())
    original_stderr_lines = len(full_stderr.splitlines())
    original_bytes = len(full_stdout.encode("utf-8", errors="replace")) + len(
        full_stderr.encode("utf-8", errors="replace")
    )

    # Atomic write: tmp then rename
    tmp_path = log_path.with_suffix(".tmp")
    try:
        tmp_path.write_bytes(full_bytes_raw)
        tmp_path.rename(log_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        # Non-fatal: continue without disk log if write fails

    # ── 2. Apply line truncation ─────────────────────────────────────────
    bounded_stdout, stdout_trunc, _ = _truncate_text_lines(full_stdout)
    bounded_stderr, stderr_trunc, _ = _truncate_text_lines(full_stderr)
    truncated = stdout_trunc or stderr_trunc

    result = dict(tool_output)
    result["output"] = bounded_stdout
    result["stderr"] = bounded_stderr

    # ── 3. Secondary byte limit: trim to fit within _TOOL_OUTPUT_MAX_BYTES ─
    # Always attach full-log metadata so every bounded result is auditable.
    trunc_meta = {
        "_output_truncated": truncated,
        "_full_output_sha256": full_sha256,
        "_full_output_path": rel_path,
        "_original_stdout_lines": original_stdout_lines,
        "_original_stderr_lines": original_stderr_lines,
        "_original_output_bytes": original_bytes,
    }
    result_with_meta = {**result, **trunc_meta}

    try:
        total_encoded = _json.dumps(result_with_meta, ensure_ascii=False).encode("utf-8", errors="replace")
        if len(total_encoded) > _TOOL_OUTPUT_MAX_BYTES:
            # Trim output first, then stderr to fit.
            # Reserve budget for metadata + other fields (all non-output/stderr).
            other_fields = {k: v for k, v in result_with_meta.items()
                            if k not in ("output", "stderr")}
            other_bytes = len(_json.dumps(other_fields, ensure_ascii=False).encode("utf-8", errors="replace"))
            # Leave 20 bytes overhead for JSON structure
            content_budget = max(0, _TOOL_OUTPUT_MAX_BYTES - other_bytes - 20)
            # Split budget: 60% stdout, 40% stderr
            stdout_budget = int(content_budget * 0.6)
            stderr_budget = content_budget - stdout_budget
            result_with_meta["output"] = _encode_safe_utf8(
                bounded_stdout, stdout_budget
            ).decode("utf-8", errors="replace")
            result_with_meta["stderr"] = _encode_safe_utf8(
                bounded_stderr, stderr_budget
            ).decode("utf-8", errors="replace")
            result_with_meta["_output_truncated"] = True
    except Exception:
        pass  # Non-fatal: if byte-limit trimming fails, return line-truncated version

    return result_with_meta


def run_agent_phase(
    *,
    role: Role,
    condition: str,
    client: ModelClient,
    dispatcher: ToolDispatcher,
    system_prompt: str,
    initial_user_message: str,
    budget: dict[str, Any],
    event_logger: EventLogger,
    private_logger: EventLogger | None = None,
    # swe-0.2: optional reprompt support (None = disabled, legacy Spreadsheet behavior)
    required_tools: set[str] | None = None,
    reprompt_builder: "((set[str]) -> str) | None" = None,
    # swe-0.2: assistant text persistence (None = disabled, legacy Spreadsheet behavior)
    # Pass the run directory Path to enable; model_call events will carry either
    # ``assistant_text`` (<=4096 UTF-8 bytes) or ``raw_payload_path`` (>4096).
    model_call_text_dir: "Path | None" = None,
    # swe-0.3: state-sealed completion mode
    # When True, the first natural end_turn response seals the role immediately;
    # no reprompt is injected. Submit tools are optional logged actions only.
    swe_03_mode: bool = False,
    # swe-0.3: bounded tool output log directory
    # When set, stdout/stderr tool outputs are bounded (60+60 lines, <=8192 bytes)
    # and full outputs are written to disk under this directory.
    tool_output_log_dir: "Path | None" = None,
) -> AgentPhaseResult:
    """Run one agent phase (worker or parent) to completion or budget exhaustion.

    Parameters
    ----------
    role:
        ``"worker"`` or ``"parent"`` — used for ModelClient tagging and event logs.
    condition:
        Experiment condition (``"P1"`` etc.) — used to select tool allowlist.
    client:
        Shared ModelClient instance (same model for both phases).
    dispatcher:
        ToolDispatcher for this phase's workspace.  Worker and parent each get
        their own dispatcher so their workspaces are strictly separated.
    system_prompt:
        System prompt for this phase.
    initial_user_message:
        The first user turn (task description + context).
    budget:
        Role limits including turns, tool calls, wall time, cumulative output
        budget, and the distinct cumulative input safety ceiling.
    event_logger:
        Public events.jsonl logger (model_call events are logged here).
    private_logger:
        Worker-private trace logger (tool inputs/outputs with execution detail).
        Pass None for the parent (its tool events go only to event_logger).
    model_call_text_dir:
        When set (SWE-only), the run directory Path used for assistant text
        persistence. Each successful model_call event gains exactly one of:
        ``assistant_text`` (str, <=4096 UTF-8 bytes, inclusive, including
        ``""`` for empty / tool-only / whitespace-only) or
        ``raw_payload_path`` (run-relative path to a file containing the raw
        UTF-8 bytes). Empty responses record ``assistant_text: ""`` — the
        field is never omitted and is never the sentinel ``MISSING``.
        When None (default, Spreadsheet path), no persistence is performed and
        existing Spreadsheet event behavior is unchanged.
    swe_03_mode:
        When True (swe-0.3), the first natural ``end_turn`` response seals the
        role immediately without any reprompt. Submit tools (submit_memo,
        submit_solution) become optional logged actions; their calls are
        processed normally but the loop only terminates on end_turn. The
        terminal assistant text is captured and stored in
        ``AgentPhaseResult.terminal_assistant_text``.
        When False (default, swe-0.2 and Spreadsheet), existing behavior
        applies (reprompt injection, required_tools, etc.).
    tool_output_log_dir:
        When set (swe-0.3), stdout/stderr from tool executions are bounded
        (first 60 lines + last 60 lines, max 8192 total bytes) before being
        returned to the model. The full original output is written to disk
        under ``tool_output_log_dir/logs/tool_outputs/`` with SHA-256
        metadata attached to the bounded result. When None (Spreadsheet path),
        tool output is passed through unchanged.

    Returns
    -------
    AgentPhaseResult
        Usage counters, terminal condition flag, and failure info if any.
    """
    result = AgentPhaseResult()
    t_start = time.monotonic()

    max_turns: int = budget["max_turns"]
    max_tool_calls: int = budget["max_tool_calls"]
    max_wall: int = budget["max_wall_seconds"]
    # Input is a non-budget safety ceiling; output remains a scientific budget.
    max_input_safety: int | None = budget.get("max_input_tokens_safety")
    max_output_total: int | None = budget.get("max_output_tokens_total")

    # ── Condition-aware terminal tool ─────────────────────────────────────────
    # A0/A1 workers terminate normally on submit_memo (no solution is produced).
    # P0/P1 workers and the parent always terminate on submit_solution.
    if role == "parent":
        terminal_tool = "submit_solution"
    elif condition in ("A0", "A1"):
        terminal_tool = "submit_memo"
    else:
        terminal_tool = "submit_solution"

    # swe-0.2: track which required tools have been validly submitted.
    # When required_tools is None (Spreadsheet path), this feature is disabled.
    _required_remaining: set[str] = set(required_tools) if required_tools else set()

    # Pre-flight limit sanity.  Safety attribution remains separate from budget
    # attribution even when the value is invalid before the first API call.
    if max_input_safety is not None and max_input_safety <= 0:
        result.failure_code = "safety_input_tokens"
        result.failure_message = (
            f"{role} max_input_tokens_safety ceiling is nonsensical: "
            f"{max_input_safety}"
        )
        result.safety_exhausted = True
        result.safety_exhausted_on = "max_input_tokens_safety"
        result.wall_seconds = time.monotonic() - t_start
        return result
    if max_output_total is not None and max_output_total <= 0:
        result.failure_code = "budget_output_tokens_total"
        result.failure_message = (
            f"{role} max_output_tokens_total cap is nonsensical: {max_output_total}"
        )
        result.budget_exhausted = True
        result.budget_exhausted_on = "max_output_tokens_total"
        result.wall_seconds = time.monotonic() - t_start
        return result

    allowed_tools = dispatcher.allowed_tools()
    tool_list = _build_tool_list(allowed_tools)

    # Conversation history in Anthropic Messages format
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_message}
    ]

    # Terminal signal: set True when the condition-specific terminal tool is called
    submitted_terminal = False

    for turn in range(max_turns):
        # Wall-clock budget check
        elapsed = time.monotonic() - t_start
        if elapsed >= max_wall:
            result.failure_code = "budget_wall_seconds"
            result.failure_message = (
                f"{role} wall-clock budget exceeded: {elapsed:.1f}s >= {max_wall}s"
            )
            result.budget_exhausted = True
            result.budget_exhausted_on = "max_wall_seconds"
            result.end_reason = "wall_time"
            result.seal_source = "cap_sealed"
            break

        # Tool-call budget check
        if result.tool_calls >= max_tool_calls:
            result.failure_code = "budget_tool_calls"
            result.failure_message = (
                f"{role} tool-call budget exceeded: {result.tool_calls} >= {max_tool_calls}"
            )
            result.budget_exhausted = True
            result.budget_exhausted_on = "max_tool_calls"
            result.end_reason = "max_tool_calls"
            result.seal_source = "cap_sealed"
            break

        # ── API call ─────────────────────────────────────────────────────
        api_result = client.call(
            role=role,
            messages=messages,
            system=system_prompt,
            tools=tool_list if tool_list else None,
        )
        result.input_tokens += api_result.input_tokens
        result.output_tokens += api_result.output_tokens
        cache_read_input_tokens = getattr(api_result, "cache_read_input_tokens", 0)
        cache_creation_input_tokens = getattr(
            api_result, "cache_creation_input_tokens", 0
        )
        result.cache_read_input_tokens += cache_read_input_tokens
        result.cache_creation_input_tokens += cache_creation_input_tokens
        result.model_calls += 1

        # ── Cumulative token limit checks (after recording this call's tokens) ──
        # Deterministic precedence: the emergency input safety ceiling is checked
        # before the output budget.  When either is met or exceeded, no further
        # model call is made.
        # Sealing of any artifact already emitted is handled by the dispatcher
        # state (memo/solution written before the cap fired are preserved).
        if (
            max_input_safety is not None
            and result.input_tokens >= max_input_safety
        ):
            result.failure_code = "safety_input_tokens"
            result.failure_message = (
                f"{role} cumulative input token safety ceiling reached: "
                f"{result.input_tokens} >= {max_input_safety}"
            )
            result.safety_exhausted = True
            result.safety_exhausted_on = "max_input_tokens_safety"
            result.end_reason = "safety_cap"
            result.seal_source = "cap_sealed"
            # Log public event before stopping.
            # Safety-cap: API call succeeded; persist text if enabled.
            _text_frag: dict[str, str] = _model_call_assistant_text_fields(
                api_result.content,
                model_call_text_dir,
                role,
                api_result.event.call_index,
            )
            event_logger.log(
                role=role,
                event_type="model_call",
                payload={
                    "call_index": api_result.event.call_index,
                    "model_id": api_result.event.model_id,
                    "timestamp": api_result.event.timestamp,
                    "input_tokens": api_result.input_tokens,
                    "output_tokens": api_result.output_tokens,
                    "cache_read_input_tokens": cache_read_input_tokens,
                    "cache_creation_input_tokens": cache_creation_input_tokens,
                    "stop_reason": api_result.stop_reason,
                    **_text_frag,
                },
            )
            break
        if max_output_total is not None and result.output_tokens >= max_output_total:
            result.failure_code = "budget_output_tokens_total"
            result.failure_message = (
                f"{role} cumulative output token budget exhausted: "
                f"{result.output_tokens} >= {max_output_total}"
            )
            result.budget_exhausted = True
            result.budget_exhausted_on = "max_output_tokens_total"
            result.end_reason = "output_token_cap"
            result.seal_source = "cap_sealed"
            # Log public event before stopping.
            # Output-cap: API call succeeded; persist text if enabled.
            _text_frag = _model_call_assistant_text_fields(
                api_result.content,
                model_call_text_dir,
                role,
                api_result.event.call_index,
            )
            event_logger.log(
                role=role,
                event_type="model_call",
                payload={
                    "call_index": api_result.event.call_index,
                    "model_id": api_result.event.model_id,
                    "timestamp": api_result.event.timestamp,
                    "input_tokens": api_result.input_tokens,
                    "output_tokens": api_result.output_tokens,
                    "cache_read_input_tokens": cache_read_input_tokens,
                    "cache_creation_input_tokens": cache_creation_input_tokens,
                    "stop_reason": api_result.stop_reason,
                    **_text_frag,
                },
            )
            break

        # Log public model_call event (no tool payload content, just metadata).
        # SWE: persist assistant text on every successful call, including empty.
        _text_frag = _model_call_assistant_text_fields(
            api_result.content,
            model_call_text_dir,
            role,
            api_result.event.call_index,
        )
        event_logger.log(
            role=role,
            event_type="model_call",
            payload={
                "call_index": api_result.event.call_index,
                "model_id": api_result.event.model_id,
                "timestamp": api_result.event.timestamp,
                "input_tokens": api_result.input_tokens,
                "output_tokens": api_result.output_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "stop_reason": api_result.stop_reason,
                **_text_frag,
            },
        )

        # ── Parse response content ────────────────────────────────────────
        response_content = api_result.content

        # Collect text blocks for conversation history
        assistant_turn: dict[str, Any] = {
            "role": "assistant",
            "content": response_content,
        }
        messages.append(assistant_turn)

        # Check if this is the terminal turn (no tool use → end of phase)
        if api_result.stop_reason == "end_turn":
            # Capture terminal assistant text for this response regardless of mode.
            _terminal_text = _extract_assistant_text(api_result.content)

            # ── swe-0.3: state-sealed completion ─────────────────────────
            if swe_03_mode:
                # First natural end_turn seals immediately. No reprompt.
                # Submits are optional prior actions; the seal condition is
                # purely the absence of tool_use in this response (guaranteed
                # by stop_reason == "end_turn").
                result.terminal_assistant_text = _terminal_text
                result.terminated_normally = True
                result.end_reason = "natural_end_turn_sealed"
                result.seal_source = "natural_end_turn_sealed"
                break

            # ── swe-0.2: if required_tools enabled and any still missing, inject reprompt
            if _required_remaining and reprompt_builder is not None:
                # Prose-only response with outstanding required submissions.
                # Inject a targeted re-prompt and continue; do NOT break.
                result.reprompted_on_end_turn += 1
                reprompt_text = reprompt_builder(_required_remaining)
                event_logger.log(
                    role=role,
                    event_type="reprompted_on_end_turn",
                    payload={
                        "reprompt_index": result.reprompted_on_end_turn,
                        "missing_tools": sorted(_required_remaining),
                        "turn": turn,
                    },
                )
                messages.append({
                    "role": "user",
                    "content": reprompt_text,
                })
                # Do NOT break — continue to next turn in the for loop.
                continue

            # Natural end without tool use — phase ends (swe-0.2 / Spreadsheet)
            if submitted_terminal:
                result.terminated_normally = True
                result.end_reason = "successful_required_submissions"
                result.seal_source = "explicit_submit"
            else:
                result.failure_code = "agent_no_submission"
                result.failure_message = (
                    f"{role} reached end_turn without calling {terminal_tool}"
                )
                result.end_reason = "agent_no_submission"
            break

        if api_result.stop_reason == "max_tokens":
            # swe-0.3.2: do not end the role on a per-call cut-off. Continue
            # up to _MAX_TOKENS_CONTINUATION_LIMIT times, then seal.
            _cut_text = _extract_assistant_text(api_result.content)
            if result.continued_on_max_tokens < _MAX_TOKENS_CONTINUATION_LIMIT:
                result.continued_on_max_tokens += 1
                event_logger.log(
                    role=role,
                    event_type="continued_on_max_tokens",
                    payload={
                        "continuation_index": result.continued_on_max_tokens,
                        "stop_reason": "max_tokens",
                        "turn": turn,
                    },
                )
                messages.append({
                    "role": "user",
                    "content": _MAX_TOKENS_CONTINUATION_TEXT,
                })
                continue
            if swe_03_mode:
                _seal_terminal_stop(
                    result,
                    text=_cut_text,
                    end_reason="max_tokens_sealed",
                    seal_source="max_tokens_sealed",
                )
            else:
                result.failure_code = "unexpected_stop_reason"
                result.failure_message = (
                    f"{role} unexpected stop_reason: 'max_tokens' "
                    f"after {result.continued_on_max_tokens} continuations"
                )
                result.end_reason = "unexpected_stop_reason"
                result.unexpected_stop_reason = "max_tokens"
            break

        if api_result.stop_reason != "tool_use":
            # Any other unexpected stop (including content-filter stop_reason):
            # seal immediately and record the exact reason.
            result.unexpected_stop_reason = api_result.stop_reason
            event_logger.log(
                role=role,
                event_type="failure",
                payload={
                    "code": "unexpected_stop_reason",
                    "stop_reason": api_result.stop_reason,
                    "sealed": True,
                },
            )
            if swe_03_mode:
                _seal_terminal_stop(
                    result,
                    text=_extract_assistant_text(api_result.content),
                    end_reason="unexpected_stop_sealed",
                    seal_source="unexpected_stop_sealed",
                )
            else:
                result.failure_code = "unexpected_stop_reason"
                result.failure_message = (
                    f"{role} unexpected stop_reason: {api_result.stop_reason!r}"
                )
                result.end_reason = "unexpected_stop_reason"
            break

        # ── Execute tool calls ────────────────────────────────────────────
        tool_use_blocks = [
            b for b in response_content
            if hasattr(b, "type") and b.type == "tool_use"
        ]
        if not tool_use_blocks:
            # stop_reason=tool_use but no tool_use blocks — shouldn't happen
            result.failure_code = "protocol_error"
            result.failure_message = (
                f"{role} stop_reason=tool_use but no tool_use blocks in response"
            )
            break

        # Build tool_results list for the next user turn
        tool_results: list[dict[str, Any]] = []

        for block in tool_use_blocks:
            tool_name = block.name
            tool_input = block.input if hasattr(block, "input") else {}
            tool_use_id = block.id

            result.tool_calls += 1

            # Log tool_call event (public; no detailed output)
            event_logger.log(
                role=role,
                event_type="tool_call",
                payload={
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "tool_call_index": result.tool_calls - 1,
                },
            )

            # Execute via dispatcher (enforces allowlist)
            try:
                tool_output = dispatcher.call(tool_name, **tool_input)
                tool_error = None
            except Exception as exc:
                tool_output = None
                tool_error = str(exc)

            # swe-0.3: apply bounded output to stdout/stderr-producing tools
            if tool_error is None and tool_output is not None and tool_output_log_dir is not None:
                try:
                    tool_output = _bound_and_log_tool_output(
                        tool_output,
                        tool_output_log_dir / "logs" / "tool_outputs",
                        role,
                        result.tool_calls - 1,
                    )
                except Exception:
                    pass  # Non-fatal: if bounding fails, use original output

            # Track validation cycles (run_solution / run_ephemeral_python)
            if tool_name in ("run_solution", "run_ephemeral_python"):
                result.validation_cycles += 1

            # Track terminal submission (condition-aware)
            if tool_name == terminal_tool and tool_error is None:
                submitted_terminal = True

            # swe-0.2: track required tool completions (valid submissions only)
            if tool_error is None and tool_name in _required_remaining:
                _required_remaining.discard(tool_name)

            # Serialise tool output for the API
            if tool_error is not None:
                tool_content_str = json.dumps({"error": tool_error})
            else:
                try:
                    tool_content_str = json.dumps(tool_output)
                except (TypeError, ValueError) as exc:
                    tool_content_str = json.dumps({"error": f"Serialisation failed: {exc}"})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": tool_content_str,
            })

            # Log tool_result event (public; strip large payloads for inspection)
            safe_result_summary = _safe_summary(tool_name, tool_output, tool_error)
            tool_result_payload: dict[str, Any] = {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "error": tool_error,
                "summary": safe_result_summary,
            }
            # v0.4.1: if the run tool attached an auto_observation, persist it verbatim
            # in the public event so compliance can be verified from events.jsonl.
            if isinstance(tool_output, dict) and "auto_observation" in tool_output:
                tool_result_payload["auto_observation"] = tool_output["auto_observation"]
            event_logger.log(
                role=role,
                event_type="tool_result",
                payload=tool_result_payload,
            )

            # Log FULL detail to private trace (worker only)
            if private_logger is not None:
                private_logger.log(
                    role=role,
                    event_type="tool_result",
                    payload={
                        "tool_name": tool_name,
                        "tool_use_id": tool_use_id,
                        "tool_input": tool_input,
                        "tool_output": tool_output,
                        "error": tool_error,
                    },
                )

            # If submit_solution succeeded and for worker also check memo,
            # we can end the turn loop after processing all tool calls in this batch.

        # Append tool results as the next user turn
        messages.append({
            "role": "user",
            "content": tool_results,
        })

        # If the terminal tool was called in this batch AND all required tools
        # have been submitted, end the loop.
        # In swe-0.3 mode, submit tools are optional actions; the loop only
        # terminates on natural end_turn (handled above).
        if not swe_03_mode and submitted_terminal and not _required_remaining:
            result.terminated_normally = True
            result.end_reason = "successful_required_submissions"
            result.seal_source = "explicit_submit"
            break

    else:
        # Exhausted all turns without break.
        # swe-0.3: submit tools never terminate the loop, so a cap seal can
        # fire even after optional submits. swe-0.2 / Spreadsheet: only
        # seal-as-exhausted when the terminal tool was never called.
        if swe_03_mode or not submitted_terminal:
            result.failure_code = "budget_turns"
            result.failure_message = (
                f"{role} turn budget exhausted ({max_turns} turns)"
            )
            result.budget_exhausted = True
            result.budget_exhausted_on = "max_turns"
            result.end_reason = "max_turns"
            result.seal_source = "cap_sealed"

    result.wall_seconds = time.monotonic() - t_start
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_summary(
    tool_name: str,
    output: Any,
    error: str | None,
) -> str:
    """Return a brief, safe summary of tool output for the public event log.

    Execution feedback (sandbox stdout/stderr) is included because it helps
    with compliance tracking, but hidden evaluator data is never accessible
    inside the sandbox so it cannot appear here.
    """
    if error is not None:
        return f"error: {error[:200]}"
    if output is None:
        return "null"
    if isinstance(output, dict):
        # For run_solution: include exit_code and succeeded flag only
        if tool_name in ("run_solution",):
            return json.dumps({
                k: output[k]
                for k in ("exit_code", "succeeded", "timed_out")
                if k in output
            })
        # For write_solution / submit_*: include path and sha256
        if tool_name in ("write_solution", "submit_solution", "submit_memo"):
            return json.dumps({
                k: output[k]
                for k in ("path", "sha256", "bytes")
                if k in output
            })
        # For inspection tools: truncate large outputs
        as_str = json.dumps(output)
        return as_str[:500] + ("…" if len(as_str) > 500 else "")
    return str(output)[:200]
