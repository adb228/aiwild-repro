"""Model-2 (gpt-5.6-luna) analysis + provider-client wiring.

Task sets are model-suffixed and never pooled with Sonnet. analyze.py
refuses mixed model strings inside one estimate. Cost uses the Luna list
rates. Frozen episode_schema.json does not list the *_m2 names; that enum
lag is documented (same class as main_v035) and is not a contracts edit.
"""
from __future__ import annotations

import copy
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from ..validate import ValidationIssue, validate_episode

# Alias is pinned in the OpenAI client. Importing it here would pull
# ``openai`` into every analyze.py load; keep the string in sync instead.
MODEL2_ALIAS = "gpt-5.6-luna"

# ── Task sets ────────────────────────────────────────────────────────────────

MODEL2_SPREADSHEET_TASK_SETS = frozenset({
    "pilot_m2",
    "main_v04_m2",
    "solo_v04_m2",
})
MODEL2_SWE_TASK_SETS = frozenset({
    "swe_pilot_m2",
    "swe_main_m2",
})
MODEL2_TASK_SETS = MODEL2_SPREADSHEET_TASK_SETS | MODEL2_SWE_TASK_SETS
MODEL2_OBSERVATION_TASK_SETS = frozenset({"pilot_m2", "swe_pilot_m2"})
MODEL2_ENV = "DELEGATION_MODEL2"

SONNET_PRIMARY_RUNS = Path("<REPO>")
SONNET_MODEL_MARKERS = ("claude", "sonnet")

# Observation caps for smoke (via env) and pilot_m2. Binding main caps
# come from the by-role rule after the pilot and live in manifest.model2.caps.
OBSERVATION_MAX_TURNS = 80
OBSERVATION_MAX_TOOL_CALLS = 160

# Luna list (USD / million tokens). Cached input is 10% of input.
LUNA_INPUT_PER_MILLION = Decimal("0.20")
LUNA_OUTPUT_PER_MILLION = Decimal("1.20")
LUNA_CACHED_INPUT_PER_MILLION = LUNA_INPUT_PER_MILLION * Decimal("0.10")
SPEND_CEILING_USD = Decimal("350")

MODEL2_SELECTION_MAP = {
    "pilot_m2": "pilot",
    "main_v04_m2": "main",
    "solo_v04_m2": "main",
}


class Model2AnalysisError(ValueError):
    """Raised when a model-2 estimate would mix models or read Sonnet runs."""


def is_model2_task_set(task_set: str) -> bool:
    return task_set in MODEL2_TASK_SETS


def model2_provider_active(task_set: str) -> bool:
    """True when this episode must use the OpenAI / Luna client.

    *_m2 task sets always use Luna. Burned development smoke uses Luna
    only when the launcher sets DELEGATION_MODEL2=1, so existing
    development unit tests keep the Anthropic client.
    """
    if is_model2_task_set(task_set):
        return True
    if task_set in {"development", "dev"} and os.environ.get(MODEL2_ENV) == "1":
        return True
    return False


def uses_observation_caps(task_set: str) -> bool:
    if task_set in MODEL2_OBSERVATION_TASK_SETS:
        return True
    return (
        task_set in {"development", "dev"}
        and os.environ.get(MODEL2_ENV) == "1"
    )


def model2_models(manifest: dict[str, Any]) -> tuple[str, str, float]:
    """Worker/parent alias and a schema-valid recorded temperature.

    The API omits temperature (Phase 0 deviation). The episode field is
    0.0 so frozen reproducibility.temperature (number 0–1) stays valid.
    """
    block = manifest.get("model2") or {}
    alias = str(block.get("alias") or block.get("worker") or MODEL2_ALIAS)
    worker = str(block.get("worker") or alias)
    parent = str(block.get("parent") or alias)
    if worker != parent:
        raise ValueError(
            f"model2.worker ({worker!r}) != model2.parent ({parent!r})"
        )
    return worker, parent, 0.0


def apply_model2_runtime_budgets(
    task_set: str,
    worker_budget: dict[str, Any],
    parent_budget: dict[str, Any],
    manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy budgets; apply observation 80/160 or frozen model2.caps."""
    worker = copy.deepcopy(worker_budget)
    parent = copy.deepcopy(parent_budget)
    block = (manifest or {}).get("model2") or {}
    ss_caps = block.get("caps") or {}
    swe_caps = block.get("swe_caps") or {}
    if task_set in {"main_v04_m2", "solo_v04_m2"} and ss_caps:
        for role, dest in (("worker", worker), ("parent", parent)):
            role_caps = ss_caps.get(role) or {}
            for key in (
                "max_turns",
                "max_tool_calls",
                "max_wall_seconds",
                "max_output_tokens_total",
            ):
                if key in role_caps:
                    dest[key] = int(role_caps[key])
        return worker, parent
    if task_set == "swe_main_m2" and swe_caps:
        for role, dest in (("worker", worker), ("parent", parent)):
            role_caps = swe_caps.get(role) or {}
            for key in (
                "max_turns",
                "max_tool_calls",
                "max_wall_seconds",
                "max_output_tokens_total",
            ):
                if key in role_caps:
                    dest[key] = int(role_caps[key])
        return worker, parent
    if uses_observation_caps(task_set):
        for dest in (worker, parent):
            dest["max_turns"] = OBSERVATION_MAX_TURNS
            dest["max_tool_calls"] = OBSERVATION_MAX_TOOL_CALLS
    return worker, parent


def client_for_task_set(task_set: str, config_path: Path | str | None = None):
    """Return OpenAIModelClient for model-2 launches; else Anthropic."""
    if model2_provider_active(task_set):
        from ..model_client_openai import OpenAIModelClient
        return OpenAIModelClient(config_path=config_path)
    from ..model_client import ModelClient
    return ModelClient(config_path=config_path) if config_path else ModelClient()


def selection_list_for_task_set(task_set: str) -> str:
    """Which frozen selection list (pilot vs main) a model-2 set uses."""
    if task_set in MODEL2_SELECTION_MAP:
        return MODEL2_SELECTION_MAP[task_set]
    if task_set == "pilot":
        return "pilot"
    return "main"


# ── Mix-model / Sonnet isolation ─────────────────────────────────────────────

def record_model_strings(record: dict[str, Any]) -> set[str]:
    repro = record.get("reproducibility") or {}
    out: set[str] = set()
    for key in ("worker_model", "parent_model"):
        value = repro.get(key)
        if isinstance(value, str) and value.strip():
            out.add(value.strip())
    return out


def looks_like_sonnet_model(model: str) -> bool:
    lower = model.lower()
    return any(marker in lower for marker in SONNET_MODEL_MARKERS)


def assert_single_model(records: Iterable[dict[str, Any]]) -> str:
    """Refuse mixed model strings inside one estimate. Return the sole model."""
    models: set[str] = set()
    for record in records:
        models |= record_model_strings(record)
    if not models:
        raise Model2AnalysisError(
            "estimate has no worker_model/parent_model strings"
        )
    if len(models) > 1:
        raise Model2AnalysisError(
            "refusing to mix model strings within one estimate: "
            + ", ".join(sorted(models))
        )
    return next(iter(models))


def assert_model2_scope_models(records: Iterable[dict[str, Any]]) -> str:
    """Single model, and it must be Luna (alias or snapshot prefix)."""
    model = assert_single_model(records)
    if looks_like_sonnet_model(model):
        raise Model2AnalysisError(
            f"refusing Sonnet model {model!r} in a model-2 estimate; "
            "different model; not pooled"
        )
    if model != MODEL2_ALIAS and not model.startswith(MODEL2_ALIAS):
        raise Model2AnalysisError(
            f"model-2 estimate requires {MODEL2_ALIAS!r} (or snapshot); "
            f"got {model!r}"
        )
    return model


def assert_not_sonnet_runs_dir(runs_dir: Path) -> None:
    try:
        resolved = runs_dir.resolve()
    except OSError as exc:
        raise Model2AnalysisError(f"cannot resolve runs dir {runs_dir}") from exc
    if resolved == SONNET_PRIMARY_RUNS.resolve():
        raise Model2AnalysisError(
            "refusing Sonnet primary data/runs; different model; not pooled"
        )


# ── Cost ledger ──────────────────────────────────────────────────────────────

def luna_episode_cost_usd(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> Decimal:
    """USD for one episode. Output rate includes reasoning tokens.

    ``output_tokens`` is the provider total (visible + reasoning when the
    API folds them). Extra ``reasoning_tokens`` are added only when they
    are not already counted in ``output_tokens``.
    """
    cached = max(0, int(cached_input_tokens))
    billed_input = max(0, int(input_tokens) - cached)
    out = max(0, int(output_tokens))
    reasoning = max(0, int(reasoning_tokens))
    if reasoning > out:
        out = reasoning
    return (
        Decimal(billed_input) * LUNA_INPUT_PER_MILLION
        + Decimal(cached) * LUNA_CACHED_INPUT_PER_MILLION
        + Decimal(out) * LUNA_OUTPUT_PER_MILLION
    ) / Decimal(1_000_000)


def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        raw = usage.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def episode_token_totals(record: dict[str, Any]) -> dict[str, int]:
    usage = record.get("usage") or {}
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    for role in ("worker", "parent"):
        bucket = usage.get(role) or {}
        totals["input_tokens"] += _usage_int(bucket, "input_tokens")
        totals["output_tokens"] += _usage_int(bucket, "output_tokens")
        totals["cached_input_tokens"] += _usage_int(
            bucket,
            "cached_input_tokens",
            "cache_read_input_tokens",
        )
        totals["reasoning_tokens"] += _usage_int(bucket, "reasoning_tokens")
    return totals


def episode_cost_usd(record: dict[str, Any]) -> Decimal:
    return luna_episode_cost_usd(**episode_token_totals(record))


def spend_from_records(records: Iterable[dict[str, Any]]) -> Decimal:
    return sum((episode_cost_usd(rec) for rec in records), Decimal(0))


def spend_exceeds_ceiling(spend: Decimal) -> bool:
    return spend >= SPEND_CEILING_USD


# ── Schema lag (frozen enum; contracts not opened) ───────────────────────────

def model2_schema_lag_issue(issue: Any) -> bool:
    """Frozen episode_schema.json enum predates recorded *_m2 task_set."""
    path = getattr(issue, "path", None)
    message = getattr(issue, "message", "") or ""
    if path != "task_set":
        return False
    return any(name in message for name in MODEL2_TASK_SETS)


def validate_model2_episode(
    record: dict[str, Any],
    record_path: Path,
) -> list[ValidationIssue]:
    """validate_episode minus documented task_set enum lag."""
    issues = validate_episode(record, record_path)
    return [issue for issue in issues if not model2_schema_lag_issue(issue)]
