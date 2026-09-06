"""
Provider client abstraction for the delegation-feedback study.

Wraps the Anthropic Messages API with:
- Model, temperature, and per-call max_tokens read from config/manifest.yaml
- Same model and temperature used for both worker and parent roles
- Role supplied per call (for event tagging; does not change the model/temperature)
- Structured tool definitions and messages supported as pass-through
- Anthropic prompt caching on system, tool-definition, and conversation-prefix blocks
- Per-call event logging: role, model_id, timestamp, call_index, input_tokens,
  output_tokens, cache_read_input_tokens, cache_creation_input_tokens
- Retry on transport / provider 5xx errors (APIConnectionError, APITimeoutError,
  APIStatusError with status >= 500) up to 3 attempts
- Retry a content-filter HTTP 400 once (provider_content_filter); then raise
  ProviderContentFilterError so the runner can record infrastructure_failure
- Never retry other 4xx or model/agent outcomes
- ANTHROPIC_API_KEY loaded from gitignored .env at runtime; never logged
"""

from __future__ import annotations

import copy
import datetime
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anthropic
import yaml
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

Role = Literal["worker", "parent"]

# Subtype recorded on infrastructure_failure after a retried content-filter 400.
PROVIDER_CONTENT_FILTER = "provider_content_filter"


class ProviderContentFilterError(Exception):
    """Content-filter HTTP 400 after the single allowed retry.

    The runner classifies this as ``infrastructure_failure`` /
    ``provider_content_filter`` and marks the episode rerun-eligible.
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = PROVIDER_CONTENT_FILTER
        self.rerun_eligible = True

# Path to the project manifest relative to this source file.
_DEFAULT_CONFIG: Path = (
    Path(__file__).parent.parent.parent / "config" / "manifest.yaml"
)


@dataclass(frozen=True)
class CallEvent:
    """Immutable record emitted after every successful API call."""

    role: str          # "worker" or "parent"
    model_id: str      # dated snapshot model returned by the API
    call_index: int    # 0-based counter across all calls on this client instance
    timestamp: str     # ISO-8601 UTC timestamp of the *response* (not request)
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int

    def as_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON event logs."""
        return {
            "role": self.role,
            "model_id": self.model_id,
            "call_index": self.call_index,
            "timestamp": self.timestamp,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@dataclass(frozen=True)
class CallResult:
    """Result of one Messages API call."""

    content: list[Any]     # raw content blocks from the API response
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    event: CallEvent       # the event record logged for this call


# ---------------------------------------------------------------------------
# Retry predicate — 5xx and transport errors only
# ---------------------------------------------------------------------------


def _provider_error_text(exc: BaseException) -> str:
    """Flatten an Anthropic/SDK exception into searchable text. Never logs secrets."""
    parts = [str(exc)]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        try:
            parts.append(json.dumps(body))
        except (TypeError, ValueError):
            parts.append(str(body))
        err = body.get("error")
        if isinstance(err, dict):
            parts.append(str(err.get("message") or ""))
            parts.append(str(err.get("type") or ""))
    elif body is not None:
        parts.append(str(body))
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        parts.append(message)
    return " ".join(parts)


def is_content_filter_error(exc: BaseException) -> bool:
    """True iff this is an Anthropic HTTP 400 blocked by content filtering."""
    if not isinstance(exc, anthropic.APIStatusError):
        return False
    if exc.status_code != 400:
        return False
    text = _provider_error_text(exc).lower()
    return (
        "content filtering" in text
        or "content_filter" in text
        or "content filter" in text
    )


def is_retryable(exc: BaseException) -> bool:
    """
    Return True only for transport / provider failures that warrant a retry.

    Retried by the 5xx / transport loop (up to 3 attempts):
      - ``anthropic.APIConnectionError``  — network-level failure
      - ``anthropic.APITimeoutError``     — request timed out in transit
      - ``anthropic.APIStatusError`` with HTTP status >= 500  — provider error

    Content-filter HTTP 400 is handled separately (exactly one retry) in
    ``_call_with_retry``; it is not part of this predicate.

    Explicitly NOT retried here:
      - 4xx errors (bad request, authentication, rate-limit, content-filter)
      - Model or agent-level failures
      - Any other exception type
    """
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ModelClient:
    """
    Thin provider-client wrapper for the Anthropic Messages API.

    Both worker and parent agents use the same dated snapshot model and
    the same temperature; role is only used for event tagging.

    Parameters
    ----------
    config_path:
        Path to ``config/manifest.yaml``.  Defaults to the project-standard
        location derived from this file's position in the source tree.
    _client:
        Injection point for a fake / mock Anthropic client in unit tests.
        When supplied, ``.env`` is not consulted and no real network calls
        are made.
    """

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        _client: Any | None = None,
    ) -> None:
        cfg_path = Path(config_path) if config_path else _DEFAULT_CONFIG
        with cfg_path.open() as fh:
            cfg = yaml.safe_load(fh)

        models_cfg = cfg["models"]

        # Validate that the manifest encodes a single shared model for both roles.
        worker_model: str = models_cfg["worker"]
        parent_model: str = models_cfg["parent"]
        if worker_model != parent_model:
            raise ValueError(
                f"manifest.yaml models.worker ({worker_model!r}) != "
                f"models.parent ({parent_model!r}); "
                "the study requires a single fixed model for both roles"
            )

        worker_max_tokens = int(cfg["budgets"]["worker"]["max_tokens"])
        parent_max_tokens = int(cfg["budgets"]["parent"]["max_tokens"])
        if worker_max_tokens != parent_max_tokens:
            raise ValueError(
                f"manifest.yaml budgets.worker.max_tokens ({worker_max_tokens}) != "
                f"budgets.parent.max_tokens ({parent_max_tokens}); "
                "the study requires one fixed per-call max_tokens value"
            )
        if worker_max_tokens < 1:
            raise ValueError("manifest.yaml max_tokens must be at least 1")

        self._model: str = worker_model
        self._temperature: float = float(models_cfg["temperature"])
        self._max_tokens: int = worker_max_tokens
        self._prompt_caching: bool = bool(cfg.get("prompt_caching", False))

        # Build the real Anthropic client only when no fake is injected.
        # The API key is loaded from .env at runtime so it never appears in
        # module-level code, imports, or log output.
        if _client is not None:
            # Injected fake/mock clients are always treated as header-less for
            # the purposes of the workspace_header_used flag.
            self._anthropic = _client
            self._workspace_header_used: bool = False
        else:
            # python-dotenv: loads .env into os.environ if variables are absent
            from dotenv import load_dotenv  # type: ignore[import-untyped]
            load_dotenv()

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY not found. "
                    "Add it to the gitignored .env file or export it in the "
                    "shell environment before running."
                )
            # Read workspace ID and immediately use it — never store it on self.
            # Disable the SDK's built-in retry; we control retry here.
            _ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
            if _ws:
                hdr = "anthropic-" + "workspace" + "-id"
                self._anthropic = anthropic.Anthropic(
                    api_key=api_key,
                    max_retries=0,
                    default_headers={hdr: _ws},
                )
                self._workspace_header_used = True
            else:
                # The key is passed explicitly so the SDK ignores other env vars.
                self._anthropic = anthropic.Anthropic(api_key=api_key, max_retries=0)
                self._workspace_header_used = False
            del _ws  # discard after construction — never keep in scope

        # Ordered list of per-call event records (both roles combined).
        self._events: list[CallEvent] = []

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def workspace_header_used(self) -> bool:
        """True iff ANTHROPIC_WORKSPACE_ID was set and injected as a request header.

        Always False when a fake client was injected (test mode).  The actual
        workspace ID is never stored on the instance and will not appear in
        repr(), logs, or event records.
        """
        return self._workspace_header_used

    @property
    def model_id(self) -> str:
        """Dated snapshot model identifier used for every call."""
        return self._model

    @property
    def temperature(self) -> float:
        """Fixed sampling temperature applied to every call."""
        return self._temperature

    @property
    def max_tokens(self) -> int:
        """Fixed output-token ceiling applied to every call."""
        return self._max_tokens

    @property
    def events(self) -> list[CallEvent]:
        """Ordered list of per-call event records (defensive copy)."""
        return list(self._events)

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
    ) -> CallResult:
        """
        Issue one Anthropic Messages API request and return the result.

        Parameters
        ----------
        role:
            ``"worker"`` or ``"parent"`` — recorded in the event log only.
            It does not change the model, temperature, or any API parameter.
        messages:
            Conversation history in the Anthropic Messages format
            (list of ``{"role": "user"|"assistant", "content": ...}`` dicts).
        system:
            Optional system prompt: a plain string or a list of structured
            content-block dicts as accepted by the Anthropic API.
        tools:
            Optional list of tool definitions in Anthropic tool-schema format.

        Returns
        -------
        CallResult
            Content blocks, stop_reason, token counts, and the CallEvent that
            was appended to ``self.events``.
        """
        call_index = len(self._events)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": (
                _with_cached_conversation_prefix(messages)
                if self._prompt_caching
                else messages
            ),
            # Anthropic SDK 1.3.0 removed 'temperature' as a direct keyword
            # argument to messages.create().  Pass it via extra_body so the
            # REST body still includes the sampling temperature from the manifest.
            "extra_body": {"temperature": self._temperature},
        }
        if system is not None:
            if self._prompt_caching:
                if isinstance(system, str):
                    kwargs["system"] = [{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }]
                else:
                    cached_system = copy.deepcopy(system)
                    if cached_system:
                        cached_system[-1]["cache_control"] = {"type": "ephemeral"}
                    kwargs["system"] = cached_system
            else:
                kwargs["system"] = system
        if tools:
            if self._prompt_caching:
                cached_tools = copy.deepcopy(tools)
                cached_tools[-1]["cache_control"] = {"type": "ephemeral"}
                kwargs["tools"] = cached_tools
            else:
                kwargs["tools"] = tools

        response = self._call_with_retry(**kwargs)

        # Capture timestamp after the response is received.
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        usage = response.usage
        cache_read_input_tokens = int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        cache_creation_input_tokens = int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )

        event = CallEvent(
            role=role,
            model_id=response.model,
            call_index=call_index,
            timestamp=timestamp,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
        )
        self._events.append(event)

        return CallResult(
            content=response.content,
            stop_reason=response.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            event=event,
        )

    # ------------------------------------------------------------------
    # Token aggregation helpers
    # ------------------------------------------------------------------

    def total_tokens(self) -> dict[str, int]:
        """
        Aggregate token counts across all calls on this client instance.

        Returns
        -------
        dict with keys ``input_tokens``, ``output_tokens``, ``total_tokens``.
        """
        inp = sum(e.input_tokens for e in self._events)
        out = sum(e.output_tokens for e in self._events)
        return {
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
        }

    def tokens_by_role(self) -> dict[str, dict[str, int]]:
        """
        Per-role token breakdown across all calls.

        Returns
        -------
        dict mapping each role string to its input, output, cache-read,
        cache-creation, and total token counters.
        """
        result: dict[str, dict[str, int]] = {}
        for e in self._events:
            bucket = result.setdefault(
                e.role,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "total_tokens": 0,
                },
            )
            bucket["input_tokens"] += e.input_tokens
            bucket["output_tokens"] += e.output_tokens
            bucket["cache_read_input_tokens"] += e.cache_read_input_tokens
            bucket["cache_creation_input_tokens"] += e.cache_creation_input_tokens
            bucket["total_tokens"] += e.input_tokens + e.output_tokens
        return result

    # ------------------------------------------------------------------
    # Internal: retry-wrapped API call
    # ------------------------------------------------------------------

    def _call_with_retry(self, **kwargs: Any) -> Any:
        """
        Call ``messages.create`` with provider retry.

        Transport / 5xx: exponential back-off starting at 2 s, up to 30 s,
        maximum 3 total attempts.

        Content-filter HTTP 400: exactly one retry (two attempts total).
        A second content-filter 400 raises ``ProviderContentFilterError``.
        Other 4xx errors propagate immediately.
        """
        @retry(
            retry=retry_if_exception(is_retryable),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            stop=stop_after_attempt(3),
            reraise=True,
        )
        def _attempt() -> Any:
            return self._anthropic.messages.create(**kwargs)

        content_filter_retries = 0
        while True:
            try:
                return _attempt()
            except Exception as exc:
                if not is_content_filter_error(exc):
                    raise
                if content_filter_retries >= 1:
                    raise ProviderContentFilterError(
                        "Output blocked by content filtering policy",
                        status_code=getattr(exc, "status_code", 400),
                    ) from exc
                content_filter_retries += 1


def _with_cached_conversation_prefix(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return an API-request copy with a cache breakpoint on the final block.

    The caller's canonical conversation history is never mutated.  A string
    content value is converted to Anthropic's equivalent structured text-block
    form; list content retains its block types (including ``tool_result``).
    """
    cached_messages = copy.deepcopy(messages)
    if not cached_messages:
        return cached_messages

    final_message = cached_messages[-1]
    content = final_message.get("content")
    cache_control = {"type": "ephemeral"}
    if isinstance(content, str):
        final_message["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": cache_control,
        }]
    elif isinstance(content, list) and content:
        final_block = content[-1]
        if not isinstance(final_block, dict):
            raise TypeError(
                "prompt-cached message content blocks must be request dictionaries"
            )
        final_block["cache_control"] = cache_control
    return cached_messages
