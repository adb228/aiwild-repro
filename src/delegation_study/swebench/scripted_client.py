"""Scripted SWE model client — no Anthropic API, no .env, no gold patches.

P workers (and A parents) append a marker line to an existing tracked file
via ``write_file`` so ``git diff`` seals a nonempty patch. Gold / hints /
test_patch / FAIL_TO_PASS never enter this module.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from ..model_client import CallEvent, CallResult

_SCRIPTED_MODEL = "scripted-swe-0.3.3"
CERT_TARGET_PATH = "/testbed/README.rst"
CERT_MARKER_LINE = "# swe-031-cert-marker"
_PYTEST_PROBE = "pytest /testbed/NONEXISTENT_SWE_CERT_PROBE.py -q"


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


def _user_and_system_text(system: Any, messages: list[dict[str, Any]] | None) -> str:
    chunks: list[str] = []
    if isinstance(system, str):
        chunks.append(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
    for message in messages or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
    return "\n".join(chunks)


class ScriptedSWEClient:
    """Deterministic worker/parent stand-in. Never constructs ModelClient."""

    scripted = True
    model_id = _SCRIPTED_MODEL
    workspace_header_used = False

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self._role = "worker"
        self._condition = "P1"
        self._probe_execution = False
        self._step = 0
        self._plan: list[Any] = []
        self._events: list[CallEvent] = []
        self._tool_seq = 0
        self.write_path = CERT_TARGET_PATH
        self.write_content = ""

    def __repr__(self) -> str:
        return f"ScriptedSWEClient(instance_id={self.instance_id!r})"

    @property
    def events(self) -> list[CallEvent]:
        return list(self._events)

    def attach_workspace(
        self,
        *,
        role: str,
        condition: str,
        workspace: Any,
        probe_execution: bool = False,
    ) -> None:
        self._role = role
        self._condition = condition
        self._probe_execution = probe_execution
        self._step = 0
        will_write = (
            (role == "worker" and condition in {"P0", "P1"})
            or (role == "parent" and condition in {"A0", "A1"})
        )
        if will_write and workspace is not None:
            original = workspace.read_file(self.write_path)
            if not original.endswith("\n") and original:
                original += "\n"
            self.write_content = original + CERT_MARKER_LINE + "\n"
        self._plan = self._build_plan(will_write)

    def _build_plan(self, will_write: bool) -> list[Any]:
        steps: list[Any] = []
        if will_write:
            steps.append(("write_file", {
                "path": self.write_path,
                "content": self.write_content,
            }))
        else:
            steps.append(("read_file", {"path": self.write_path}))
        if self._probe_execution:
            steps.append(("exec_shell", {"command": _PYTEST_PROBE}))
        steps.append((
            "end_turn",
            f"Scripted {self._role} seal for {self.instance_id} under {self._condition}.",
        ))
        return steps

    def call(
        self,
        *,
        role: str,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> CallResult:
        del tools
        _user_and_system_text(system, messages)
        if self._step >= len(self._plan):
            raise RuntimeError(f"ScriptedSWEClient: no more steps for role={role}")
        action = self._plan[self._step]
        self._step += 1

        if action[0] == "end_turn":
            content: list[Any] = [_TextBlock(text=action[1])]
            stop_reason = "end_turn"
        else:
            self._tool_seq += 1
            content = [_ToolUseBlock(
                id=f"scripted-{role}-{self._tool_seq}",
                name=action[0],
                input=action[1],
            )]
            stop_reason = "tool_use"

        event = CallEvent(
            role=role,
            model_id=_SCRIPTED_MODEL,
            call_index=len(self._events),
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        self._events.append(event)
        return CallResult(
            content=content,
            stop_reason=stop_reason,
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            event=event,
        )
