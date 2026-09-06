"""Repair Anthropic conversation history for swe-0.3.3 / swe-0.3.4.

swe-0.3.2 continues a role after ``stop_reason=max_tokens`` by appending a
prose user turn. If that truncated assistant message still contains
``tool_use`` blocks, the next Messages API call is a 400:

    tool_use ids were found without tool_result blocks immediately after

swe-0.3.3 inserts synthetic ``tool_result`` blocks so the conversation stays
API-valid. It does not execute the truncated tools and does not change
prompts or caps.

swe-0.3.4 additionally compact older ``tool_result`` payloads when the
provider rejects the request as ``prompt is too long``, then retries. If
compaction cannot get under the window, ``PromptTooLongError`` is raised
so the runner can write an ``infrastructure_failure`` episode record.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

PROVIDER_PROMPT_TOO_LONG = "provider_prompt_too_long"
CONTEXT_WINDOW_TOKENS = 200_000
COMPACT_TRIGGER_TOKENS = 180_000
COMPACTED_STUB = (
    "[compacted: prior tool_result omitted to stay under the provider "
    "context window; the workspace still has the side effects of that call]"
)


class PromptTooLongError(Exception):
    """Provider context-window 400 after swebench-local compaction failed.

    The runner classifies this as ``infrastructure_failure`` /
    ``provider_prompt_too_long`` and marks the episode rerun-eligible.
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = PROVIDER_PROMPT_TOO_LONG
        self.rerun_eligible = True

SYNTHETIC_TOOL_RESULT_TEXT = (
    "Not executed: the preceding assistant message included tool_use "
    "block(s) but stop_reason was not tool_use (typically max_tokens "
    "truncation). Synthetic tool_result inserted so the conversation "
    "remains API-valid."
)


def _iter_blocks(content: Any) -> list[Any]:
    if content is None or isinstance(content, str):
        return []
    if isinstance(content, dict):
        return [content]
    try:
        return list(content)
    except TypeError:
        return []


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _tool_use_id(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("id") or block.get("tool_use_id")
    return getattr(block, "id", None) or getattr(block, "tool_use_id", None)


def tool_use_ids(content: Any) -> list[str]:
    ids: list[str] = []
    for block in _iter_blocks(content):
        if _block_type(block) == "tool_use":
            tid = _tool_use_id(block)
            if tid:
                ids.append(str(tid))
    return ids


def tool_result_ids(content: Any) -> set[str]:
    ids: set[str] = set()
    for block in _iter_blocks(content):
        if _block_type(block) == "tool_result":
            tid = _tool_use_id(block)
            if tid:
                ids.add(str(tid))
    return ids


def dangling_tool_use_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Return tool_use ids that are not followed by matching tool_results."""
    dangling: list[str] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        ids = tool_use_ids(msg.get("content"))
        if not ids:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        have: set[str] = set()
        if nxt is not None and nxt.get("role") == "user":
            have = tool_result_ids(nxt.get("content"))
        dangling.extend(tid for tid in ids if tid not in have)
    return dangling


def _synthetic_result(tool_use_id: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": True,
        "content": json.dumps({
            "error": "tool_use_truncated_not_executed",
            "message": SYNTHETIC_TOOL_RESULT_TEXT,
        }),
    }


def _as_user_blocks(content: Any) -> list[Any]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(_iter_blocks(content))


def repair_dangling_tool_use(messages: list[dict[str, Any]]) -> list[str]:
    """Mutate ``messages`` in place so every tool_use has a following tool_result.

    Returns the tool_use ids that received a synthetic result.
    """
    repaired: list[str] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant":
            ids = tool_use_ids(msg.get("content"))
            if ids:
                nxt = messages[i + 1] if i + 1 < len(messages) else None
                have: set[str] = set()
                if nxt is not None and nxt.get("role") == "user":
                    have = tool_result_ids(nxt.get("content"))
                missing = [tid for tid in ids if tid not in have]
                if missing:
                    synthetic = [_synthetic_result(tid) for tid in missing]
                    if nxt is not None and nxt.get("role") == "user":
                        nxt["content"] = synthetic + _as_user_blocks(nxt.get("content"))
                    else:
                        messages.insert(i + 1, {"role": "user", "content": synthetic})
                    repaired.extend(missing)
        i += 1
    return repaired


def is_prompt_too_long(exc: BaseException) -> bool:
    """True iff this looks like Anthropic's context-window 400."""
    status = getattr(exc, "status_code", None)
    if status not in (None, 400):
        return False
    text = str(exc).lower()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        try:
            text = f"{text} {json.dumps(body).lower()}"
        except (TypeError, ValueError):
            text = f"{text} {str(body).lower()}"
    return "prompt is too long" in text or "prompt_too_long" in text


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap char/4 estimate. Used only to decide when to compact."""
    try:
        raw = json.dumps(messages, default=str)
    except (TypeError, ValueError):
        raw = str(messages)
    return max(1, len(raw) // 4)


def _tool_result_text(block: Any) -> str:
    content = block.get("content") if isinstance(block, dict) else getattr(block, "content", "")
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, default=str)
    except (TypeError, ValueError):
        return str(content)


def compact_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = 6,
    min_chars: int = 400,
) -> int:
    """Replace older large tool_result payloads with stubs. Returns count compacted."""
    compacted = 0
    cutoff = max(0, len(messages) - keep_recent)
    for i, msg in enumerate(messages):
        if i >= cutoff:
            break
        if msg.get("role") != "user":
            continue
        blocks = _iter_blocks(msg.get("content"))
        if not blocks:
            continue
        new_blocks: list[Any] = []
        changed = False
        for block in blocks:
            if _block_type(block) != "tool_result":
                new_blocks.append(block)
                continue
            text = _tool_result_text(block)
            if text.startswith("[compacted:") or len(text) < min_chars:
                new_blocks.append(block)
                continue
            tid = _tool_use_id(block)
            stub: dict[str, Any] = {
                "type": "tool_result",
                "content": COMPACTED_STUB,
            }
            if tid:
                stub["tool_use_id"] = tid
            new_blocks.append(stub)
            changed = True
            compacted += 1
        if changed:
            msg["content"] = new_blocks
    return compacted


def compact_old_assistant_text(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = 2,
) -> int:
    """Last-resort: stub older assistant prose. Keeps tool_use blocks intact."""
    compacted = 0
    cutoff = max(0, len(messages) - keep_recent)
    for i, msg in enumerate(messages):
        if i >= cutoff or msg.get("role") != "assistant":
            continue
        blocks = _iter_blocks(msg.get("content"))
        if not blocks:
            continue
        new_blocks: list[Any] = []
        changed = False
        for block in blocks:
            if _block_type(block) == "text":
                text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
                if isinstance(text, str) and text.startswith("[compacted:"):
                    new_blocks.append(block)
                    continue
                new_blocks.append({
                    "type": "text",
                    "text": "[compacted: prior assistant prose omitted to stay under the context window]",
                })
                changed = True
                compacted += 1
            else:
                new_blocks.append(block)
        if changed:
            msg["content"] = new_blocks
    return compacted


class ConversationGuard:
    """Wrap a model client: repair dangling tool_use, then compact on overflow."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def call(self, **kwargs: Any) -> Any:
        messages = kwargs.get("messages")
        if messages:
            repaired = repair_dangling_tool_use(messages)
            if repaired:
                log.warning(
                    "swe-0.3.3 conversation guard: inserted synthetic tool_result "
                    "for dangling tool_use ids %s",
                    repaired,
                )
            if estimate_message_tokens(messages) >= COMPACT_TRIGGER_TOKENS:
                n = compact_tool_results(messages)
                if n:
                    log.warning(
                        "swe-0.3.4 conversation guard: preemptive compact of %d "
                        "tool_result(s); est_tokens=%d",
                        n,
                        estimate_message_tokens(messages),
                    )
        try:
            return self._inner.call(**kwargs)
        except Exception as exc:
            if not messages or not is_prompt_too_long(exc):
                raise
            last: BaseException = exc
            for keep, min_chars in ((6, 400), (2, 80), (0, 1)):
                n = compact_tool_results(
                    messages, keep_recent=keep, min_chars=min_chars
                )
                if not n:
                    continue
                log.warning(
                    "swe-0.3.4 conversation guard: compacted %d tool_result(s) "
                    "after prompt-too-long (keep_recent=%d)",
                    n,
                    keep,
                )
                try:
                    return self._inner.call(**kwargs)
                except Exception as retry_exc:
                    if not is_prompt_too_long(retry_exc):
                        raise
                    last = retry_exc
            n_text = compact_old_assistant_text(messages, keep_recent=2)
            if n_text:
                log.warning(
                    "swe-0.3.4 conversation guard: compacted %d assistant text "
                    "block(s) after tool_result compact still overflowed",
                    n_text,
                )
                try:
                    return self._inner.call(**kwargs)
                except Exception as retry_exc:
                    if not is_prompt_too_long(retry_exc):
                        raise
                    last = retry_exc
            raise PromptTooLongError(str(last)) from last
