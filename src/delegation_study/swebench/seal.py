"""swe-0.3.5 empty-diff reprompt-once completion for SWE roles.

``agent_loop.run_agent_phase`` still seals the first natural ``end_turn``
under ``swe_03_mode``. This module is the SWE-owned loop/seal path: at a
bare end-of-turn, an empty ``git diff`` and no successful submit earn
exactly one reprompt. A second consecutive bare end-turn, or a cap after
that reprompt, seals a genuine non-submission.

Advisory (A0/A1) workers keep the swe-0.3 immediate seal (memo from
terminal text). Production workers and every parent use this rule.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from ..agent_loop import (
    AgentPhaseResult,
    _MAX_TOKENS_CONTINUATION_LIMIT,
    _MAX_TOKENS_CONTINUATION_TEXT,
    _bound_and_log_tool_output,
    _build_tool_list,
    _extract_assistant_text,
    _model_call_assistant_text_fields,
    _safe_summary,
    _seal_terminal_stop,
    run_agent_phase,
)

EMPTY_DIFF_REPROMPT = (
    "You have not changed any files. Make your edit, then call submit_solution."
)

SEALED_AFTER_REPROMPT = "sealed_after_reprompt"
SEAL_NOW = "seal_now"
REPROMPT_ONCE = "reprompt_once"
SEAL_NON_SUBMISSION = "seal_non_submission"


def diff_is_empty(diff: str | None) -> bool:
    return not (diff and str(diff).strip())


def read_workspace_diff(extract_diff: Callable[[], str] | None) -> str:
    """Return the current git diff, or ``""`` if unavailable."""
    if extract_diff is None:
        return ""
    try:
        return extract_diff() or ""
    except Exception:
        return ""


def decide_empty_diff_end_turn(
    *,
    submitted: bool,
    diff: str | None,
    already_reprompted: bool,
    apply_rule: bool,
) -> str:
    """Decide what a natural end-of-turn should do.

    Returns one of ``seal_now``, ``reprompt_once``, ``seal_non_submission``.
    """
    if not apply_rule or submitted or not diff_is_empty(diff):
        return SEAL_NOW
    if already_reprompted:
        return SEAL_NON_SUBMISSION
    return REPROMPT_ONCE


def _mark_sealed_after_reprompt(result: AgentPhaseResult, text: str) -> None:
    result.terminal_assistant_text = text
    result.terminated_normally = False
    result.end_reason = SEALED_AFTER_REPROMPT
    result.seal_source = SEALED_AFTER_REPROMPT
    result.sealed_after_reprompt = True  # type: ignore[attr-defined]


def _mark_natural_seal(result: AgentPhaseResult, text: str) -> None:
    result.terminal_assistant_text = text
    result.terminated_normally = True
    result.end_reason = "natural_end_turn_sealed"
    result.seal_source = "natural_end_turn_sealed"
    result.sealed_after_reprompt = False  # type: ignore[attr-defined]


def _log_model_call(
    *,
    event_logger: Any,
    role: str,
    api_result: Any,
    model_call_text_dir: Any,
) -> None:
    cache_read = getattr(api_result, "cache_read_input_tokens", 0)
    cache_create = getattr(api_result, "cache_creation_input_tokens", 0)
    text_frag = _model_call_assistant_text_fields(
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
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
            "stop_reason": api_result.stop_reason,
            **text_frag,
        },
    )


def _record_usage(result: AgentPhaseResult, api_result: Any) -> tuple[int, int]:
    cache_read = getattr(api_result, "cache_read_input_tokens", 0)
    cache_create = getattr(api_result, "cache_creation_input_tokens", 0)
    result.input_tokens += api_result.input_tokens
    result.output_tokens += api_result.output_tokens
    result.cache_read_input_tokens += cache_read
    result.cache_creation_input_tokens += cache_create
    result.model_calls += 1
    return cache_read, cache_create


def _hit_token_cap(
    result: AgentPhaseResult,
    *,
    role: str,
    max_input_safety: int | None,
    max_output_total: int | None,
) -> bool:
    if max_input_safety is not None and result.input_tokens >= max_input_safety:
        result.failure_code = "safety_input_tokens"
        result.failure_message = (
            f"{role} cumulative input token safety ceiling reached: "
            f"{result.input_tokens} >= {max_input_safety}"
        )
        result.safety_exhausted = True
        result.safety_exhausted_on = "max_input_tokens_safety"
        result.end_reason = "safety_cap"
        result.seal_source = "cap_sealed"
        return True
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
        return True
    return False


def _maybe_cap_to_non_submission(
    result: AgentPhaseResult,
    *,
    apply_rule: bool,
    submitted: bool,
    already_reprompted: bool,
    extract_diff: Callable[[], str] | None,
    event_logger: Any,
    role: str,
    text: str = "",
) -> None:
    """After a budget/safety cap, seal genuine non-submission if the one reprompt fired."""
    if not apply_rule or not already_reprompted or submitted:
        return
    if not diff_is_empty(read_workspace_diff(extract_diff)):
        return
    _mark_sealed_after_reprompt(result, text)
    event_logger.log(
        role=role,
        event_type="failure",
        payload={
            "code": SEALED_AFTER_REPROMPT,
            "reprompted_on_end_turn": result.reprompted_on_end_turn,
            "seal_source": SEALED_AFTER_REPROMPT,
            "empty_diff": True,
            "submitted": False,
            "cap_exhausted": True,
        },
    )


def run_swe_agent_phase(
    *,
    role: str,
    condition: str,
    client: Any,
    dispatcher: Any,
    system_prompt: str,
    initial_user_message: str,
    budget: dict[str, Any],
    event_logger: Any,
    private_logger: Any | None = None,
    required_tools: set[str] | None = None,
    reprompt_builder: Any = None,
    model_call_text_dir: Any = None,
    swe_03_mode: bool = True,
    tool_output_log_dir: Any = None,
    extract_diff: Callable[[], str] | None = None,
    apply_empty_diff_rule: bool = True,
) -> AgentPhaseResult:
    """Run one SWE worker or parent phase with swe-0.3.5 empty-diff sealing.

    When ``apply_empty_diff_rule`` is false the shared swe-0.3 loop is used
    unchanged (A-cell workers). ``required_tools`` / ``reprompt_builder`` are
    accepted for call-site compatibility and ignored on the rule path, as in
    swe-0.3.
    """
    if not apply_empty_diff_rule:
        result = run_agent_phase(
            role=role,
            condition=condition,
            client=client,
            dispatcher=dispatcher,
            system_prompt=system_prompt,
            initial_user_message=initial_user_message,
            budget=budget,
            event_logger=event_logger,
            private_logger=private_logger,
            required_tools=required_tools,
            reprompt_builder=reprompt_builder,
            model_call_text_dir=model_call_text_dir,
            swe_03_mode=swe_03_mode,
            tool_output_log_dir=tool_output_log_dir,
        )
        result.sealed_after_reprompt = False  # type: ignore[attr-defined]
        return result

    return _run_empty_diff_phase(
        role=role,
        condition=condition,
        client=client,
        dispatcher=dispatcher,
        system_prompt=system_prompt,
        initial_user_message=initial_user_message,
        budget=budget,
        event_logger=event_logger,
        private_logger=private_logger,
        model_call_text_dir=model_call_text_dir,
        tool_output_log_dir=tool_output_log_dir,
        extract_diff=extract_diff,
    )


def _run_empty_diff_phase(
    *,
    role: str,
    condition: str,
    client: Any,
    dispatcher: Any,
    system_prompt: str,
    initial_user_message: str,
    budget: dict[str, Any],
    event_logger: Any,
    private_logger: Any | None,
    model_call_text_dir: Any,
    tool_output_log_dir: Any,
    extract_diff: Callable[[], str] | None,
) -> AgentPhaseResult:
    """swe-0.3 state-seal plus one empty-diff reprompt."""
    result = AgentPhaseResult()
    result.sealed_after_reprompt = False  # type: ignore[attr-defined]
    t_start = time.monotonic()

    max_turns: int = budget["max_turns"]
    max_tool_calls: int = budget["max_tool_calls"]
    max_wall: int = budget["max_wall_seconds"]
    max_input_safety: int | None = budget.get("max_input_tokens_safety")
    max_output_total: int | None = budget.get("max_output_tokens_total")

    if role == "parent":
        terminal_tool = "submit_solution"
    elif condition in ("A0", "A1"):
        terminal_tool = "submit_memo"
    else:
        terminal_tool = "submit_solution"

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
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_message}
    ]
    submitted_terminal = False
    already_reprompted = False

    for turn in range(max_turns):
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
            _maybe_cap_to_non_submission(
                result,
                apply_rule=True,
                submitted=submitted_terminal,
                already_reprompted=already_reprompted,
                extract_diff=extract_diff,
                event_logger=event_logger,
                role=role,
            )
            break

        if result.tool_calls >= max_tool_calls:
            result.failure_code = "budget_tool_calls"
            result.failure_message = (
                f"{role} tool-call budget exceeded: {result.tool_calls} >= {max_tool_calls}"
            )
            result.budget_exhausted = True
            result.budget_exhausted_on = "max_tool_calls"
            result.end_reason = "max_tool_calls"
            result.seal_source = "cap_sealed"
            _maybe_cap_to_non_submission(
                result,
                apply_rule=True,
                submitted=submitted_terminal,
                already_reprompted=already_reprompted,
                extract_diff=extract_diff,
                event_logger=event_logger,
                role=role,
            )
            break

        api_result = client.call(
            role=role,
            messages=messages,
            system=system_prompt,
            tools=tool_list if tool_list else None,
        )
        _record_usage(result, api_result)

        if _hit_token_cap(
            result,
            role=role,
            max_input_safety=max_input_safety,
            max_output_total=max_output_total,
        ):
            _log_model_call(
                event_logger=event_logger,
                role=role,
                api_result=api_result,
                model_call_text_dir=model_call_text_dir,
            )
            _maybe_cap_to_non_submission(
                result,
                apply_rule=True,
                submitted=submitted_terminal,
                already_reprompted=already_reprompted,
                extract_diff=extract_diff,
                event_logger=event_logger,
                role=role,
                text=_extract_assistant_text(api_result.content),
            )
            break

        _log_model_call(
            event_logger=event_logger,
            role=role,
            api_result=api_result,
            model_call_text_dir=model_call_text_dir,
        )

        response_content = api_result.content
        messages.append({"role": "assistant", "content": response_content})

        if api_result.stop_reason == "end_turn":
            terminal_text = _extract_assistant_text(api_result.content)
            action = decide_empty_diff_end_turn(
                submitted=submitted_terminal,
                diff=read_workspace_diff(extract_diff),
                already_reprompted=already_reprompted,
                apply_rule=True,
            )
            if action == REPROMPT_ONCE:
                already_reprompted = True
                result.reprompted_on_end_turn += 1
                event_logger.log(
                    role=role,
                    event_type="reprompted_on_end_turn",
                    payload={
                        "reprompt_index": result.reprompted_on_end_turn,
                        "turn": turn,
                        "reason": "empty_diff",
                    },
                )
                messages.append({
                    "role": "user",
                    "content": EMPTY_DIFF_REPROMPT,
                })
                continue
            if action == SEAL_NON_SUBMISSION:
                _mark_sealed_after_reprompt(result, terminal_text)
                event_logger.log(
                    role=role,
                    event_type="failure",
                    payload={
                        "code": SEALED_AFTER_REPROMPT,
                        "reprompted_on_end_turn": result.reprompted_on_end_turn,
                        "seal_source": SEALED_AFTER_REPROMPT,
                        "empty_diff": True,
                        "submitted": False,
                        "turn": turn,
                    },
                )
                break
            _mark_natural_seal(result, terminal_text)
            break

        if api_result.stop_reason == "max_tokens":
            cut_text = _extract_assistant_text(api_result.content)
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
            _seal_terminal_stop(
                result,
                text=cut_text,
                end_reason="max_tokens_sealed",
                seal_source="max_tokens_sealed",
            )
            _maybe_cap_to_non_submission(
                result,
                apply_rule=True,
                submitted=submitted_terminal,
                already_reprompted=already_reprompted,
                extract_diff=extract_diff,
                event_logger=event_logger,
                role=role,
                text=cut_text,
            )
            break

        if api_result.stop_reason != "tool_use":
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
            _seal_terminal_stop(
                result,
                text=_extract_assistant_text(api_result.content),
                end_reason="unexpected_stop_sealed",
                seal_source="unexpected_stop_sealed",
            )
            break

        tool_use_blocks = [
            b for b in response_content
            if hasattr(b, "type") and b.type == "tool_use"
        ]
        if not tool_use_blocks:
            result.failure_code = "protocol_error"
            result.failure_message = (
                f"{role} stop_reason=tool_use but no tool_use blocks in response"
            )
            break

        tool_results: list[dict[str, Any]] = []
        for block in tool_use_blocks:
            tool_name = block.name
            tool_input = block.input if hasattr(block, "input") else {}
            tool_use_id = block.id
            result.tool_calls += 1
            event_logger.log(
                role=role,
                event_type="tool_call",
                payload={
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "tool_call_index": result.tool_calls - 1,
                },
            )
            try:
                tool_output = dispatcher.call(tool_name, **tool_input)
                tool_error = None
            except Exception as exc:
                tool_output = None
                tool_error = str(exc)

            if (
                tool_error is None
                and tool_output is not None
                and tool_output_log_dir is not None
            ):
                try:
                    tool_output = _bound_and_log_tool_output(
                        tool_output,
                        tool_output_log_dir / "logs" / "tool_outputs",
                        role,
                        result.tool_calls - 1,
                    )
                except Exception:
                    pass

            if tool_name in ("run_solution", "run_ephemeral_python"):
                result.validation_cycles += 1
            if tool_name == terminal_tool and tool_error is None:
                submitted_terminal = True

            if tool_error is not None:
                tool_content_str = json.dumps({"error": tool_error})
            else:
                try:
                    tool_content_str = json.dumps(tool_output)
                except (TypeError, ValueError) as exc:
                    tool_content_str = json.dumps(
                        {"error": f"Serialisation failed: {exc}"}
                    )

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": tool_content_str,
            })
            event_logger.log(
                role=role,
                event_type="tool_result",
                payload={
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "error": tool_error,
                    "summary": _safe_summary(tool_name, tool_output, tool_error),
                },
            )
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

        messages.append({"role": "user", "content": tool_results})
    else:
        result.failure_code = "budget_turns"
        result.failure_message = (
            f"{role} turn budget exhausted ({max_turns} turns)"
        )
        result.budget_exhausted = True
        result.budget_exhausted_on = "max_turns"
        result.end_reason = "max_turns"
        result.seal_source = "cap_sealed"
        _maybe_cap_to_non_submission(
            result,
            apply_rule=True,
            submitted=submitted_terminal,
            already_reprompted=already_reprompted,
            extract_diff=extract_diff,
            event_logger=event_logger,
            role=role,
            text=result.terminal_assistant_text or "",
        )

    result.wall_seconds = time.monotonic() - t_start
    return result
