"""
OpenAI Responses API client for the model-2 (gpt-5.6-luna) replication.

Implements the same call() surface as ``model_client.ModelClient`` so the
existing agent loop can consume Anthropic-shaped content blocks and stop
reasons.  This module does not replace the Sonnet client.

Phase 0 pins:
  - alias gpt-5.6-luna; persist ``response.model`` as the resolved snapshot
  - reasoning.effort = "high" on every request
  - temperature omitted (reasoning models reject sampling temperature)
  - max_output_tokens = 128000
  - prompt_cache_options mode=explicit, ttl=30m (not Anthropic cache_control)
  - reasoning items via previous_response_id chaining
"""

from __future__ import annotations

import ast
import copy
import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

import openai
import yaml
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..model_client import (
    CallEvent,
    CallResult,
    ProviderContentFilterError,
    Role,
)
from ..swebench.conversation import PromptTooLongError

# ---------------------------------------------------------------------------
# Pins (also recorded in config/study/decisions.md after live smoke)
# ---------------------------------------------------------------------------

MODEL2_ALIAS = "gpt-5.6-luna"
REASONING_EFFORT: Literal["high"] = "high"
MAX_OUTPUT_TOKENS = 128_000
PROMPT_CACHE_MODE: Literal["explicit"] = "explicit"
PROMPT_CACHE_TTL: Literal["30m"] = "30m"
REASONING_ITEM_STRATEGY = "previous_response_id"
SAMPLING_DEVIATION = (
    "temperature omitted on every request; gpt-5.6 reasoning models reject "
    "sampling temperature when reasoning.effort is set"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "manifest.yaml"
_PROMPTS_DIR = _REPO_ROOT / "data" / "dev" / "method_source_pack" / "prompts"
_SS_RUN = _REPO_ROOT / "src" / "delegation_study" / "spreadsheetbench" / "run.py"
_SWE_RUN = _REPO_ROOT / "src" / "delegation_study" / "swebench" / "run.py"

# Frozen v0.4.1 hashes from method_source_pack/prompts/index.md (study-final-2).
# Frozen swe-0.3.5 hashes from committed swebench/run.py at caps 115/100
# (recorded in config/study/decisions.md Phase 0). Never edit prompts to match.
FROZEN_SWE_035_HASHES: dict[str, str] = {
    "A0": "d6b3ba17baa8c1cd9130ec9bf43e98c497ebee8efc005198483b60e3b8deb1c0",
    "A1": "d6b3ba17baa8c1cd9130ec9bf43e98c497ebee8efc005198483b60e3b8deb1c0",
    "P0": "637cb71ea906a23418e8f0b2afb635601a8486bbfcc1ee116f4a95aab87191ee",
    "P1": "637cb71ea906a23418e8f0b2afb635601a8486bbfcc1ee116f4a95aab87191ee",
    "parent": "c4e904ff9dfe33b94d85ef9f3c47afc9b74c883da0d05813bcc72da6a8784787",
}

FROZEN_SS_V041_HASHES: dict[str, str] = {
    "A0": "ba7d362fc133a74f52aa2565a48cc4fb6f371136a822d13c49183b28108a57eb",
    "A1": "aea29069ad0140999b8f39473d6a50022b07e73633d7d4c9b56f5123d2bed474",
    "P0": "76fb104317fe5c72473ac423e9689f92162447c92983cf70bca552f87328d55b",
    "P1": "321328a27007aa0a352d5973c56fa92797fa42a331b678ffa56c7ca2d06fc794",
    "parent": "0193f4a73fd2c1c837363dd75e890b03e1fba21f221e043f33b5d968c76ef209",
}

# Spreadsheet tool names from config/tool_manifests.yaml (union of all cells).
SPREADSHEET_MANIFEST_TOOLS: frozenset[str] = frozenset({
    "list_sheets",
    "workbook_metadata",
    "read_range",
    "search_workbook",
    "run_ephemeral_python",
    "read_output_range",
    "write_solution",
    "run_solution",
    "submit_memo",
    "submit_solution",
})

# SWE tools from swebench/workspace.py allowlists (union of A/P worker + parent).
SWE_MANIFEST_TOOLS: frozenset[str] = frozenset({
    "exec_shell",
    "read_file",
    "write_file",
    "submit_memo",
    "submit_solution",
})

# read_output_range is in the spreadsheet manifest but absent from
# agent_loop._TOOL_SCHEMAS.  Description bytes match the v0.4.1 prompt line
# in method_source_pack/prompts/v041_a1_system.md (committed freeze text).
READ_OUTPUT_RANGE_DESCRIPTION = (
    "read recalculated values from your output workbook"
)
READ_OUTPUT_RANGE_TOOL: dict[str, Any] = {
    "name": "read_output_range",
    "description": READ_OUTPUT_RANGE_DESCRIPTION,
    "input_schema": {
        "type": "object",
        "properties": {
            "range_ref": {
                "type": "string",
                "description": (
                    "Cell range reference in the role's output workbook, "
                    "e.g. 'Sheet1!A1:D10'."
                ),
            },
        },
        "required": ["range_ref"],
    },
}


# ---------------------------------------------------------------------------
# Anthropic-shaped content blocks (agent_loop uses attribute access)
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    """Stand-in for an Anthropic text content block."""

    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    """Stand-in for an Anthropic tool_use content block."""

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    type: str = "tool_use"


@dataclass(frozen=True)
class OpenAICallEvent(CallEvent):
    """CallEvent plus Luna usage splits."""

    reasoning_tokens: int = 0
    visible_output_tokens: int = 0
    cached_input_tokens: int = 0
    resolved_snapshot: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = super().as_dict()
        d.update({
            "reasoning_tokens": self.reasoning_tokens,
            "visible_output_tokens": self.visible_output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "resolved_snapshot": self.resolved_snapshot,
        })
        return d


@dataclass(frozen=True)
class OpenAICallResult(CallResult):
    """CallResult plus Luna usage splits and the Responses id for chaining."""

    reasoning_tokens: int = 0
    visible_output_tokens: int = 0
    cached_input_tokens: int = 0
    resolved_snapshot: str = ""
    response_id: str | None = None


# ---------------------------------------------------------------------------
# Small accessors
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


# ---------------------------------------------------------------------------
# Tool schema conversion (description bytes must be unchanged)
# ---------------------------------------------------------------------------


def anthropic_tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert Anthropic tool_use schemas to Responses function tools."""
    if not tools:
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        description = tool.get("description", "")
        parameters = copy.deepcopy(tool.get("input_schema") or {
            "type": "object",
            "properties": {},
        })
        converted.append({
            "type": "function",
            "name": tool["name"],
            "description": description,
            "parameters": parameters,
            "strict": False,
        })
    return converted


def openai_tools_to_anthropic(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Inverse of ``anthropic_tools_to_openai`` (description bytes preserved)."""
    if not tools:
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "function" and "function" in tool:
            inner = tool["function"]
            description = inner.get("description", "")
            parameters = copy.deepcopy(inner.get("parameters") or {
                "type": "object",
                "properties": {},
            })
            name = inner["name"]
        else:
            description = tool.get("description", "")
            parameters = copy.deepcopy(tool.get("parameters") or {
                "type": "object",
                "properties": {},
            })
            name = tool["name"]
        converted.append({
            "name": name,
            "description": description,
            "input_schema": parameters,
        })
    return converted


def collect_manifest_anthropic_tools() -> list[dict[str, Any]]:
    """Every Spreadsheet + SWE manifest tool as an Anthropic schema.

    Descriptions come from ``agent_loop._TOOL_SCHEMAS`` (byte-identical to
    what the Sonnet runner sends) plus the committed read_output_range
    description for the one manifest tool missing from that table.
    """
    from .agent_loop import _TOOL_SCHEMAS

    names = sorted(SPREADSHEET_MANIFEST_TOOLS | SWE_MANIFEST_TOOLS)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
        if name in _TOOL_SCHEMAS:
            out.append(copy.deepcopy(_TOOL_SCHEMAS[name]))
        elif name == "read_output_range":
            out.append(copy.deepcopy(READ_OUTPUT_RANGE_TOOL))
        else:
            raise KeyError(f"manifest tool {name!r} has no Anthropic schema")
        seen.add(name)
    missing = (SPREADSHEET_MANIFEST_TOOLS | SWE_MANIFEST_TOOLS) - seen
    if missing:
        raise KeyError(f"unmapped manifest tools: {sorted(missing)}")
    return out


# ---------------------------------------------------------------------------
# Conversation conversion
# ---------------------------------------------------------------------------


def _system_to_developer_item(system: str | list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if system is None:
        return None
    if isinstance(system, str):
        text = system
    else:
        parts: list[str] = []
        for block in system:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif hasattr(block, "text"):
                parts.append(str(getattr(block, "text") or ""))
        text = "\n".join(parts)
    return {
        "type": "message",
        "role": "developer",
        "content": [{
            "type": "input_text",
            "text": text,
            "prompt_cache_breakpoint": {"mode": "explicit"},
        }],
    }


def _tool_result_output(block: Any) -> str:
    content = _get(block, "content", "")
    return _as_text(content)


def convert_message_to_input_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one Anthropic-format message into Responses input items."""
    role = message.get("role")
    content = message.get("content")
    items: list[dict[str, Any]] = []

    if isinstance(content, str):
        mapped_role = "assistant" if role == "assistant" else "user"
        items.append({
            "type": "message",
            "role": mapped_role,
            "content": content,
        })
        return items

    blocks: list[Any]
    if content is None:
        blocks = []
    elif isinstance(content, list):
        blocks = list(content)
    else:
        blocks = [content]

    text_parts: list[str] = []
    for block in blocks:
        btype = _block_type(block)
        if btype == "tool_result":
            items.append({
                "type": "function_call_output",
                "call_id": _get(block, "tool_use_id") or _get(block, "id"),
                "output": _tool_result_output(block),
            })
        elif btype == "tool_use":
            raw_input = _get(block, "input", {}) or {}
            if not isinstance(raw_input, (str, bytes)):
                arguments = json.dumps(raw_input)
            else:
                arguments = raw_input if isinstance(raw_input, str) else raw_input.decode()
            items.append({
                "type": "function_call",
                "call_id": _get(block, "id"),
                "name": _get(block, "name"),
                "arguments": arguments,
            })
        elif btype == "text":
            text_parts.append(_as_text(_get(block, "text", "")))
        elif btype is None and isinstance(block, str):
            text_parts.append(block)

    if text_parts:
        mapped_role = "assistant" if role == "assistant" else "user"
        items.append({
            "type": "message",
            "role": mapped_role,
            "content": "\n".join(text_parts),
        })
    return items


def convert_messages_to_input(
    messages: list[dict[str, Any]],
    *,
    system: str | list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Full-history conversion (fallback when chaining is not available)."""
    items: list[dict[str, Any]] = []
    developer = _system_to_developer_item(system)
    if developer is not None:
        items.append(developer)
    for message in messages:
        items.extend(convert_message_to_input_items(message))
    return items


def convert_delta_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New-turn items only: last user message (tool results or continuation)."""
    if not messages:
        return []
    last = messages[-1]
    if last.get("role") != "user":
        return []
    return convert_message_to_input_items(last)


# ---------------------------------------------------------------------------
# Stop-reason + usage mapping
# ---------------------------------------------------------------------------


def map_stop_reason(
    *,
    status: str | None = None,
    incomplete_reason: str | None = None,
    has_function_call: bool = False,
    has_refusal: bool = False,
    has_content_filter: bool = False,
) -> str:
    """Map a Responses API outcome onto the study stop_reason vocabulary.

    truncated → max_tokens
    function call → tool_use
    natural stop → end_turn
    content filter / refusal → infrastructure
    """
    reason = (incomplete_reason or "").lower()
    status_l = (status or "").lower()
    if (
        has_content_filter
        or has_refusal
        or reason in {"content_filter", "content_policy"}
        or status_l in {"content_filter"}
    ):
        return "infrastructure"
    if reason in {"max_output_tokens", "max_tokens", "length"} or status_l == "incomplete":
        if reason in {"max_output_tokens", "max_tokens", "length"} or (
            status_l == "incomplete" and reason in {"", "max_output_tokens", "max_tokens", "length"}
        ):
            if reason in {"max_output_tokens", "max_tokens", "length"}:
                return "max_tokens"
            if status_l == "incomplete" and not has_function_call:
                return "max_tokens"
    if has_function_call:
        return "tool_use"
    return "end_turn"


def extract_usage(usage: Any) -> dict[str, int]:
    """Split provider usage into input / cached / visible / reasoning."""
    input_tokens = int(_get(usage, "input_tokens", 0) or 0)
    output_tokens = int(_get(usage, "output_tokens", 0) or 0)
    details_in = _get(usage, "input_tokens_details")
    details_out = _get(usage, "output_tokens_details")
    cached = int(_get(details_in, "cached_tokens", 0) or 0)
    cache_write = int(_get(details_in, "cache_write_tokens", 0) or 0)
    reasoning = int(_get(details_out, "reasoning_tokens", 0) or 0)
    visible = max(0, output_tokens - reasoning)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": cache_write,
        "cached_input_tokens": cached,
        "reasoning_tokens": reasoning,
        "visible_output_tokens": visible,
    }


def content_from_response(response: Any) -> list[TextBlock | ToolUseBlock]:
    """Project Responses output items onto Anthropic-shaped content blocks."""
    content: list[TextBlock | ToolUseBlock] = []
    for item in _get(response, "output", None) or []:
        itype = _get(item, "type")
        if itype == "function_call":
            raw_args = _get(item, "arguments", "") or ""
            try:
                parsed = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                parsed = {"_unparsed": raw_args}
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
            content.append(ToolUseBlock(
                id=str(_get(item, "call_id") or _get(item, "id") or ""),
                name=str(_get(item, "name") or ""),
                input=parsed,
            ))
        elif itype == "message":
            for part in _get(item, "content", None) or []:
                ptype = _get(part, "type")
                if ptype in {"output_text", "text"}:
                    content.append(TextBlock(text=_as_text(_get(part, "text", ""))))
                elif ptype == "refusal":
                    content.append(TextBlock(text=_as_text(_get(part, "refusal", ""))))
        elif itype == "output_text":
            content.append(TextBlock(text=_as_text(_get(item, "text", ""))))
    return content


def response_flags(response: Any) -> dict[str, Any]:
    """Booleans / reasons used by stop-reason mapping."""
    has_function_call = False
    has_refusal = False
    has_content_filter = False
    for item in _get(response, "output", None) or []:
        itype = _get(item, "type")
        if itype == "function_call":
            has_function_call = True
        if itype in {"refusal"}:
            has_refusal = True
        for part in _get(item, "content", None) or []:
            ptype = _get(part, "type")
            if ptype == "refusal":
                has_refusal = True
    incomplete = _get(response, "incomplete_details")
    incomplete_reason = _get(incomplete, "reason")
    status = _get(response, "status")
    if incomplete_reason == "content_filter":
        has_content_filter = True
    return {
        "status": status,
        "incomplete_reason": incomplete_reason,
        "has_function_call": has_function_call,
        "has_refusal": has_refusal,
        "has_content_filter": has_content_filter,
    }


# ---------------------------------------------------------------------------
# Provider error classification
# ---------------------------------------------------------------------------


def _provider_error_text(exc: BaseException) -> str:
    parts = [str(exc)]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        try:
            parts.append(json.dumps(body))
        except (TypeError, ValueError):
            parts.append(str(body))
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        parts.append(message)
    return " ".join(parts)


def is_context_length_error(exc: BaseException) -> bool:
    text = _provider_error_text(exc).lower()
    needles = (
        "context_length",
        "context length",
        "maximum context",
        "prompt is too long",
        "input is too long",
        "tokens exceed",
        "too many tokens",
        "context window",
    )
    return any(n in text for n in needles)


def is_temperature_rejected(exc: BaseException) -> bool:
    text = _provider_error_text(exc).lower()
    if "temperature" not in text:
        return False
    return any(n in text for n in (
        "unsupported",
        "not supported",
        "unknown parameter",
        "invalid",
        "cannot be used",
        "not allowed",
    ))


def is_content_filter_error(exc: BaseException) -> bool:
    if not isinstance(exc, openai.APIStatusError):
        return False
    text = _provider_error_text(exc).lower()
    return (
        "content_filter" in text
        or "content filter" in text
        or "content policy" in text
        or "refusal" in text and "safety" in text
    )


_RATE_LIMIT_LOG = _REPO_ROOT / "data" / "dev" / "model2" / "rate_limit.jsonl"
_CONCURRENCY_BACKOFF_LOG = _REPO_ROOT / "data" / "dev" / "model2" / "concurrency_backoff.log"
_CONCURRENCY_STATE = _REPO_ROOT / "data" / "dev" / "model2" / "concurrency_state.json"


def record_rate_limit(status_code: int = 429) -> None:
    """Append a 429/rate-limit mark. Never writes keys or response bodies."""
    _RATE_LIMIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": int(status_code),
    }
    with _RATE_LIMIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def consecutive_rate_limits(path: Path | None = None) -> int:
    log = path or _RATE_LIMIT_LOG
    if not log.is_file():
        return 0
    n = 0
    for line in reversed(log.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            break
        if int(row.get("status") or 0) == 429:
            n += 1
            continue
        break
    return n


def maybe_drop_concurrency(current: int, *, floor: int = 2) -> int:
    """On three consecutive 429s, drop concurrency by one (floor 2) and log."""
    current = max(floor, int(current))
    n = consecutive_rate_limits()
    if n < 3:
        return current
    state = {}
    if _CONCURRENCY_STATE.is_file():
        try:
            state = json.loads(_CONCURRENCY_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    already = int(state.get("drops_for_streak") or 0)
    # One drop per streak of 3; further 429s in the same streak do not
    # keep dropping until a non-429 breaks the streak (already resets).
    expected_drops = n // 3
    if expected_drops <= already:
        return current
    nxt = max(floor, current - 1)
    _CONCURRENCY_BACKOFF_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _CONCURRENCY_BACKOFF_LOG.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} "
            f"concurrency {current} -> {nxt}: three consecutive 429s "
            f"(streak={n})\n"
        )
    state["drops_for_streak"] = already + 1
    state["last_concurrency"] = nxt
    _CONCURRENCY_STATE.write_text(json.dumps(state) + "\n", encoding="utf-8")
    return nxt


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return True
    if isinstance(exc, openai.RateLimitError) or (
        isinstance(exc, openai.APIStatusError)
        and int(getattr(exc, "status_code", 0) or 0) == 429
    ):
        record_rate_limit(429)
        return True
    if isinstance(exc, openai.APIStatusError):
        return int(getattr(exc, "status_code", 0) or 0) >= 500
    return False


# ---------------------------------------------------------------------------
# Prompt-hash guard (read-only; never edits prompts)
# ---------------------------------------------------------------------------


def _ast_load_functions(path: Path, names: Iterable[str]) -> dict[str, Any]:
    wanted = set(names)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keep = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    module = ast.Module(body=keep, type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict[str, Any] = {}
    exec(compile(module, str(path), "exec"), ns)  # noqa: S102 — committed builders only
    return ns


def _sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_prompt_md_payload(path: Path) -> tuple[str, str]:
    """Return (recorded_hash, exact_prompt_bytes) from a method_source_pack file."""
    text = path.read_text(encoding="utf-8")
    recorded = ""
    match = re.search(
        r"SHA-256 of exact UTF-8 prompt bytes: `([a-f0-9]{64})`",
        text,
    )
    if match:
        recorded = match.group(1)
    begin = "<!-- BEGIN EXACT UTF-8 PROMPT -->"
    end = "<!-- END EXACT UTF-8 PROMPT -->"
    start = text.index(begin) + len(begin)
    if start < len(text) and text[start] == "\n":
        start += 1
    stop = text.index(end)
    payload = text[start:stop]
    if payload.endswith("\n"):
        payload = payload[:-1]
    return recorded, payload


def compute_spreadsheet_prompt_hashes() -> dict[str, str]:
    ns = _ast_load_functions(
        _SS_RUN,
        ("_worker_system_prompt", "_parent_system_prompt", "_solo_system_prompt"),
    )
    return {
        "A0": _sha256_utf8(ns["_worker_system_prompt"]("A0")),
        "A1": _sha256_utf8(ns["_worker_system_prompt"]("A1")),
        "P0": _sha256_utf8(ns["_worker_system_prompt"]("P0")),
        "P1": _sha256_utf8(ns["_worker_system_prompt"]("P1")),
        "parent": _sha256_utf8(ns["_parent_system_prompt"]()),
        "SOLO": _sha256_utf8(ns["_solo_system_prompt"]()),
    }


def compute_swe_prompt_hashes(
    *,
    worker_max_turns: int = 115,
    parent_max_turns: int = 100,
) -> dict[str, str]:
    """Hash swe-0.3.5 system prompts at the frozen main caps (115 / 100)."""
    ns = _ast_load_functions(
        _SWE_RUN,
        ("_worker_system_prompt", "_parent_system_prompt"),
    )
    return {
        "A0": _sha256_utf8(ns["_worker_system_prompt"](worker_max_turns, "A0")),
        "A1": _sha256_utf8(ns["_worker_system_prompt"](worker_max_turns, "A1")),
        "P0": _sha256_utf8(ns["_worker_system_prompt"](worker_max_turns, "P0")),
        "P1": _sha256_utf8(ns["_worker_system_prompt"](worker_max_turns, "P1")),
        "parent": _sha256_utf8(ns["_parent_system_prompt"](parent_max_turns)),
    }


def load_frozen_ss_v041_hashes_from_committed_files() -> dict[str, str]:
    """Hashes recorded in method_source_pack prompt files + index.md."""
    mapping = {
        "A0": _PROMPTS_DIR / "v041_a0_system.md",
        "A1": _PROMPTS_DIR / "v041_a1_system.md",
        "P0": _PROMPTS_DIR / "v041_p0_system.md",
        "P1": _PROMPTS_DIR / "v041_p1_system.md",
        "parent": _PROMPTS_DIR / "v041_parent_system.md",
    }
    out: dict[str, str] = {}
    for key, path in mapping.items():
        recorded, payload = _extract_prompt_md_payload(path)
        payload_hash = _sha256_utf8(payload)
        if recorded and recorded != payload_hash:
            raise AssertionError(
                f"{path.name}: recorded hash {recorded} != payload hash {payload_hash}"
            )
        if recorded and FROZEN_SS_V041_HASHES[key] != recorded:
            raise AssertionError(
                f"{path.name}: recorded hash {recorded} != index hash "
                f"{FROZEN_SS_V041_HASHES[key]}"
            )
        out[key] = recorded or payload_hash
    return out


def assert_frozen_prompt_hashes() -> dict[str, Any]:
    """Compare live builders to frozen committed hashes. Never edits prompts.

    Spreadsheet: v0.4.1 hashes from method_source_pack/prompts.
    SWE: hashes of the committed swe-0.3.5 builders at frozen caps 115/100
    (no separate SWE hash table exists in paper_outputs / method_source_pack;
    the freeze-tag source file is the recorded artifact).
    """
    mismatches: list[str] = []
    frozen_ss = load_frozen_ss_v041_hashes_from_committed_files()
    live_ss = compute_spreadsheet_prompt_hashes()
    for key, expected in frozen_ss.items():
        got = live_ss[key]
        if got != expected:
            mismatches.append(f"spreadsheet {key}: live {got} != frozen {expected}")

    live_swe = compute_swe_prompt_hashes()
    # Cross-check parent uniqueness and A-cell / P-cell pairing (swe-0.3.5).
    if live_swe["A0"] != live_swe["A1"]:
        mismatches.append("swe A0/A1 worker hashes diverged (unexpected for freeze)")
    if live_swe["P0"] != live_swe["P1"]:
        mismatches.append("swe P0/P1 worker hashes diverged (unexpected for freeze)")
    if live_swe["parent"] in {live_swe["A0"], live_swe["P0"]}:
        mismatches.append("swe parent hash collided with a worker hash")
    for key, expected in FROZEN_SWE_035_HASHES.items():
        got = live_swe[key]
        if got != expected:
            mismatches.append(f"swe {key}: live {got} != frozen {expected}")

    if mismatches:
        raise AssertionError(
            "PROMPT HASH MISMATCH — stop; do not edit prompts. "
            + "; ".join(mismatches)
        )
    return {
        "spreadsheet_v041": live_ss,
        "swe_035": live_swe,
        "swe_note": (
            "SWE hashes computed from committed swebench/run.py at study-final-2 "
            "with frozen caps worker=115 parent=100; no separate hash table in "
            "paper_outputs / method_source_pack."
        ),
    }


# ---------------------------------------------------------------------------
# Models endpoint (alias gate)
# ---------------------------------------------------------------------------


def list_model_ids(client: Any) -> list[str]:
    ids: list[str] = []
    pages = client.models.list()
    for model in pages:
        mid = _get(model, "id")
        if mid:
            ids.append(str(mid))
    return ids


def luna_alias_is_listed(client: Any) -> bool:
    return MODEL2_ALIAS in list_model_ids(client)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenAIModelClient:
    """ModelClient-compatible wrapper around the OpenAI Responses API."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        _client: Any | None = None,
    ) -> None:
        cfg_path = Path(config_path) if config_path else _DEFAULT_CONFIG
        with cfg_path.open() as fh:
            cfg = yaml.safe_load(fh) or {}

        model2 = cfg.get("model2") or {}
        alias = str(model2.get("alias") or model2.get("worker") or MODEL2_ALIAS)
        worker = str(model2.get("worker") or alias)
        parent = str(model2.get("parent") or alias)
        if worker != parent:
            raise ValueError(
                f"model2.worker ({worker!r}) != model2.parent ({parent!r}); "
                "the study requires a single fixed model for both roles"
            )
        self._model = worker
        self._reasoning_effort = str(model2.get("reasoning_effort") or REASONING_EFFORT)
        if self._reasoning_effort != REASONING_EFFORT:
            raise ValueError(
                f"model2.reasoning_effort must be {REASONING_EFFORT!r}, "
                f"got {self._reasoning_effort!r}"
            )
        self._max_tokens = int(model2.get("max_output_tokens") or MAX_OUTPUT_TOKENS)
        cache_cfg = model2.get("prompt_caching") or {}
        self._cache_mode = str(cache_cfg.get("mode") or PROMPT_CACHE_MODE)
        self._cache_ttl = str(cache_cfg.get("ttl") or PROMPT_CACHE_TTL)
        self._reasoning_item_strategy = str(
            model2.get("reasoning_item_strategy") or REASONING_ITEM_STRATEGY
        )
        # Temperature is omitted; record the pin even if YAML has a value.
        self._temperature: float | None = None
        self._temperature_omitted = True
        self._sampling_deviation = SAMPLING_DEVIATION

        if _client is not None:
            self._openai = _client
        else:
            from dotenv import load_dotenv  # type: ignore[import-untyped]
            load_dotenv()
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY not found. "
                    "Add it to the gitignored .env file or export it before running."
                )
            self._openai = openai.OpenAI(api_key=api_key, max_retries=0)

        self._events: list[OpenAICallEvent] = []
        self._previous_response_id: str | None = None
        self._last_call_role: Role | None = None
        self._last_resolved_snapshot: str | None = None
        self._workspace_header_used = False

    # ------------------------------------------------------------------
    # Read-only properties (ModelClient surface)
    # ------------------------------------------------------------------

    @property
    def workspace_header_used(self) -> bool:
        return self._workspace_header_used

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def temperature(self) -> float | None:
        return self._temperature

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def events(self) -> list[OpenAICallEvent]:
        return list(self._events)

    @property
    def previous_response_id(self) -> str | None:
        return self._previous_response_id

    @property
    def last_resolved_snapshot(self) -> str | None:
        return self._last_resolved_snapshot

    @property
    def reasoning_item_strategy(self) -> str:
        return self._reasoning_item_strategy

    @property
    def prompt_cache_mode(self) -> str:
        return self._cache_mode

    @property
    def prompt_cache_ttl(self) -> str:
        return self._cache_ttl

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def call(
        self,
        *,
        role: Role,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> OpenAICallResult:
        call_index = len(self._events)
        openai_tools = anthropic_tools_to_openai(tools)

        # One client is shared across worker and parent. The worker often
        # ends after a terminal tool_use whose function_call_output is never
        # sent. Chaining the parent's first turn onto that response yields
        # 400 "No tool output found for function call". Reset on role change.
        if self._last_call_role is not None and self._last_call_role != role:
            self._previous_response_id = None
        self._last_call_role = role

        use_chain = (
            self._reasoning_item_strategy == "previous_response_id"
            and self._previous_response_id is not None
            and messages
            and messages[-1].get("role") == "user"
        )
        if use_chain:
            input_items = convert_delta_input(messages)
        else:
            input_items = convert_messages_to_input(messages, system=system)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "max_output_tokens": self._max_tokens,
            "reasoning": {"effort": self._reasoning_effort},
            "prompt_cache_options": {
                "mode": self._cache_mode,
                "ttl": self._cache_ttl,
            },
            "store": True,
        }
        if system is not None and use_chain:
            # previous_response_id does not carry over top-level instructions.
            if isinstance(system, str):
                kwargs["instructions"] = system
            else:
                kwargs["instructions"] = "\n".join(
                    str(_get(b, "text", "")) for b in system
                )
        if openai_tools:
            kwargs["tools"] = openai_tools
        if use_chain:
            kwargs["previous_response_id"] = self._previous_response_id

        response = self._call_with_retry(**kwargs)

        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        usage = extract_usage(_get(response, "usage"))
        flags = response_flags(response)
        stop_reason = map_stop_reason(**flags)
        resolved = str(_get(response, "model") or self._model)
        self._last_resolved_snapshot = resolved
        response_id = _get(response, "id")
        if response_id:
            self._previous_response_id = str(response_id)

        event = OpenAICallEvent(
            role=role,
            model_id=resolved,
            call_index=call_index,
            timestamp=timestamp,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_input_tokens=usage["cache_read_input_tokens"],
            cache_creation_input_tokens=usage["cache_creation_input_tokens"],
            reasoning_tokens=usage["reasoning_tokens"],
            visible_output_tokens=usage["visible_output_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            resolved_snapshot=resolved,
        )
        self._events.append(event)

        return OpenAICallResult(
            content=content_from_response(response),
            stop_reason=stop_reason,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_input_tokens=usage["cache_read_input_tokens"],
            cache_creation_input_tokens=usage["cache_creation_input_tokens"],
            event=event,
            reasoning_tokens=usage["reasoning_tokens"],
            visible_output_tokens=usage["visible_output_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            resolved_snapshot=resolved,
            response_id=str(response_id) if response_id else None,
        )

    def total_tokens(self) -> dict[str, int]:
        inp = sum(e.input_tokens for e in self._events)
        out = sum(e.output_tokens for e in self._events)
        return {
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
        }

    def tokens_by_role(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for e in self._events:
            bucket = result.setdefault(
                e.role,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "visible_output_tokens": 0,
                    "total_tokens": 0,
                },
            )
            bucket["input_tokens"] += e.input_tokens
            bucket["output_tokens"] += e.output_tokens
            bucket["cache_read_input_tokens"] += e.cache_read_input_tokens
            bucket["cache_creation_input_tokens"] += e.cache_creation_input_tokens
            bucket["reasoning_tokens"] += e.reasoning_tokens
            bucket["visible_output_tokens"] += e.visible_output_tokens
            bucket["total_tokens"] += e.input_tokens + e.output_tokens
        return result

    def _call_with_retry(self, **kwargs: Any) -> Any:
        @retry(
            retry=retry_if_exception(is_retryable),
            # TPM 429s ask for a few seconds but stay hot under concurrency 2.
            # Honor the documented 429 policy: wait and retry; do not kill the episode.
            wait=wait_exponential(multiplier=2, min=4, max=90),
            stop=stop_after_attempt(8),
            reraise=True,
        )
        def _attempt() -> Any:
            return self._openai.responses.create(**kwargs)

        content_filter_retries = 0
        while True:
            try:
                return _attempt()
            except Exception as exc:
                if is_context_length_error(exc):
                    raise PromptTooLongError(
                        "Provider context window exceeded",
                        status_code=int(getattr(exc, "status_code", 400) or 400),
                    ) from exc
                if is_temperature_rejected(exc) and "temperature" in kwargs:
                    kwargs = dict(kwargs)
                    kwargs.pop("temperature", None)
                    extra = dict(kwargs.get("extra_body") or {})
                    extra.pop("temperature", None)
                    if extra:
                        kwargs["extra_body"] = extra
                    elif "extra_body" in kwargs:
                        kwargs.pop("extra_body")
                    self._temperature_omitted = True
                    continue
                if not is_content_filter_error(exc):
                    raise
                if content_filter_retries >= 1:
                    raise ProviderContentFilterError(
                        "Output blocked by content filtering policy",
                        status_code=getattr(exc, "status_code", 400),
                    ) from exc
                content_filter_retries += 1


# Drop-in alias for later runner wiring (Phase 0 does not launch episodes).
ModelClient = OpenAIModelClient
