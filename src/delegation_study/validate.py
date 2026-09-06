"""Structural validation for episode and event records."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_checks(record: dict[str, Any], record_path: Path) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    base = record_path.parent
    is_complete = record.get("status") == "complete"

    def _resolve(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else base / p

    def _escape_check(field: str, raw: str) -> None:
        """Emit an issue if path escapes the run directory (for real complete runs)."""
        if not is_complete:
            return
        try:
            resolved = _resolve(raw).resolve()
            run_dir = base.resolve()
            resolved.relative_to(run_dir)
        except ValueError:
            issues.append(ValidationIssue(field, f"path escapes run directory: {raw}"))

    pairs = [
        ("handoff.memo_path", record.get("handoff", {}).get("memo_path"),
         "handoff.memo_sha256", record.get("handoff", {}).get("memo_sha256")),
        (
            "handoff.candidate_script_path",
            record.get("handoff", {}).get("candidate_script_path"),
            "handoff.candidate_script_sha256",
            record.get("handoff", {}).get("candidate_script_sha256"),
        ),
        (
            "final_script_path",
            record.get("final_script_path"),
            "final_script_sha256",
            record.get("final_script_sha256"),
        ),
    ]
    for path_key, raw_path, hash_key, expected_hash in pairs:
        if expected_hash is not None and not raw_path:
            issues.append(ValidationIssue(hash_key, f"hash supplied without {path_key}"))
            continue
        if raw_path and expected_hash is None:
            issues.append(ValidationIssue(path_key, f"path supplied without {hash_key}"))
            continue
        if not raw_path:
            continue
        _escape_check(path_key, raw_path)
        path = Path(raw_path)
        if not path.is_absolute():
            path = base / path
        if is_complete and not path.is_file():
            issues.append(ValidationIssue(path_key, f"completed artifact does not exist: {path}"))
        elif path.is_file() and sha256_file(path) != expected_hash:
            issues.append(ValidationIssue(hash_key, f"hash does not match {path_key}"))

    if is_complete:
        for outcome_key in ("worker_outcome", "final_outcome"):
            outcome = record.get(outcome_key)
            if not outcome:
                continue
            for artifact_key in ("output_path", "evaluator_path"):
                raw_path = outcome.get(artifact_key)
                if artifact_key == "output_path" and outcome.get("evaluator_status") != "ok":
                    continue
                if not raw_path:
                    issues.append(
                        ValidationIssue(
                            f"{outcome_key}.{artifact_key}",
                            "completed evaluation is missing its artifact path",
                        )
                    )
                    continue
                _escape_check(f"{outcome_key}.{artifact_key}", raw_path)
                path = Path(raw_path)
                if not path.is_absolute():
                    path = base / path
                if not path.is_file():
                    issues.append(
                        ValidationIssue(
                            f"{outcome_key}.{artifact_key}",
                            f"completed artifact does not exist: {path}",
                        )
                    )

            raw_evaluator = outcome.get("evaluator_path")
            if raw_evaluator:
                evaluator_path = Path(raw_evaluator)
                if not evaluator_path.is_absolute():
                    evaluator_path = base / evaluator_path
                if evaluator_path.is_file():
                    try:
                        evaluator = json.loads(evaluator_path.read_text())
                    except (OSError, json.JSONDecodeError) as exc:
                        issues.append(
                            ValidationIssue(
                                f"{outcome_key}.evaluator_path",
                                f"cannot read evaluator artifact: {exc}",
                            )
                        )
                    else:
                        for field in (
                            "evaluator_status",
                            "full_task_success",
                            "target_unit_pass_rate",
                            "n_units",
                            "output_sha256",
                        ):
                            if evaluator.get(field) != outcome.get(field):
                                issues.append(
                                    ValidationIssue(
                                        f"{outcome_key}.{field}",
                                        f"does not match evaluator artifact {evaluator_path}",
                                    )
                                )

            # Cross-check output_sha256 vs actual file on disk
            raw_output = outcome.get("output_path")
            claimed_sha = outcome.get("output_sha256")
            if claimed_sha and raw_output:
                output_path = Path(raw_output)
                if not output_path.is_absolute():
                    output_path = base / output_path
                if output_path.is_file():
                    actual_sha = sha256_file(output_path)
                    if actual_sha != claimed_sha:
                        issues.append(
                            ValidationIssue(
                                f"{outcome_key}.output_sha256",
                                f"actual file SHA256 {actual_sha[:16]}… does not match "
                                f"claimed {claimed_sha[:16]}…",
                            )
                        )
    return issues


def _outcome_checks(record: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for key in ("worker_outcome", "final_outcome"):
        outcome = record.get(key)
        if outcome is None:
            continue
        status = outcome.get("evaluator_status")
        if status == "ok":
            if outcome.get("n_units", 0) == 0:
                issues.append(ValidationIssue(f"{key}.n_units", "ok evaluation has zero units"))
            if outcome.get("full_task_success") is None:
                issues.append(ValidationIssue(key, "ok evaluation must have scores"))
            if outcome.get("target_unit_pass_rate") is None:
                issues.append(ValidationIssue(key, "ok evaluation must have scores"))
        elif status == "not_finalized":
            if key != "final_outcome":
                issues.append(ValidationIssue(key, "not_finalized is valid only for final_outcome"))
            if outcome.get("full_task_success") != 0:
                issues.append(ValidationIssue(key, "not_finalized full_task_success must equal 0"))
            if outcome.get("target_unit_pass_rate") != 0.0:
                issues.append(
                    ValidationIssue(key, "not_finalized target_unit_pass_rate must equal 0.0")
                )
            if outcome.get("n_units") != 0:
                issues.append(ValidationIssue(key, "not_finalized n_units must equal 0"))
        else:
            if outcome.get("full_task_success") is not None:
                issues.append(ValidationIssue(key, "failed evaluation must have null scores"))
            if outcome.get("target_unit_pass_rate") is not None:
                issues.append(ValidationIssue(key, "failed evaluation must have null scores"))
    return issues


def _limit_exhaustion_checks(record: dict[str, Any]) -> list[ValidationIssue]:
    """Cross-validate scientific-budget and safety-stop attribution per role."""
    issues: list[ValidationIssue] = []
    usage = record.get("usage", {})
    for role in ("worker", "parent"):
        role_usage = usage.get(role, {})
        exhausted = role_usage.get("budget_exhausted")
        exhausted_on = role_usage.get("budget_exhausted_on", _SENTINEL)
        if exhausted is None or exhausted_on is _SENTINEL:
            pass  # schema validation will catch missing fields
        elif exhausted is False and exhausted_on is not None:
            issues.append(ValidationIssue(
                f"usage.{role}.budget_exhausted_on",
                "must be null when budget_exhausted is false",
            ))
        elif exhausted is True and exhausted_on is None:
            issues.append(ValidationIssue(
                f"usage.{role}.budget_exhausted_on",
                "must name the scientific budget when budget_exhausted is true",
            ))

        safety_exhausted = role_usage.get("safety_exhausted")
        safety_exhausted_on = role_usage.get(
            "safety_exhausted_on", _SENTINEL
        )
        if safety_exhausted is None or safety_exhausted_on is _SENTINEL:
            continue  # schema validation will catch missing fields
        if safety_exhausted is False and safety_exhausted_on is not None:
            issues.append(ValidationIssue(
                f"usage.{role}.safety_exhausted_on",
                "must be null when safety_exhausted is false",
            ))
        if safety_exhausted is True and safety_exhausted_on is None:
            issues.append(ValidationIssue(
                f"usage.{role}.safety_exhausted_on",
                "must name max_input_tokens_safety when safety_exhausted is true",
            ))
        if safety_exhausted is True and exhausted is True:
            issues.append(ValidationIssue(
                f"usage.{role}",
                "cannot attribute one phase stop to both safety and budget exhaustion",
            ))
    return issues


# Sentinel for missing-key detection (not equal to None)
_SENTINEL = object()


def _analysis_inclusion_checks(record: dict[str, Any]) -> list[ValidationIssue]:
    """Cross-validate analysis_included vs analysis_inclusion_reason."""
    issues: list[ValidationIssue] = []
    included = record.get("analysis_included", _SENTINEL)
    reason = record.get("analysis_inclusion_reason", _SENTINEL)
    if included is _SENTINEL or reason is _SENTINEL:
        return issues  # schema validation catches missing required fields
    # Both must be present and reason must be non-empty (schema enforces minLength 1).
    # Additional semantic: false requires a specific exclusion reason.
    if included is False and (not isinstance(reason, str) or not reason.strip()):
        issues.append(ValidationIssue(
            "analysis_inclusion_reason",
            "must contain a specific exclusion reason when analysis_included is false",
        ))
    return issues


def validate_episode(record: dict[str, Any], record_path: Path) -> list[ValidationIssue]:
    validator = Draft202012Validator(
        _load_schema("episode_schema.json"), format_checker=FormatChecker()
    )
    issues = [
        ValidationIssue(
            ".".join(str(part) for part in error.absolute_path) or "$",
            error.message,
        )
        for error in validator.iter_errors(record)
    ]
    issues.extend(_artifact_checks(record, record_path))
    issues.extend(_outcome_checks(record))
    issues.extend(_limit_exhaustion_checks(record))
    issues.extend(_analysis_inclusion_checks(record))
    return sorted(issues, key=lambda issue: (issue.path, issue.message))


def validate_event(event: dict[str, Any]) -> list[ValidationIssue]:
    validator = Draft202012Validator(
        _load_schema("event_schema.json"), format_checker=FormatChecker()
    )
    return [
        ValidationIssue(
            ".".join(str(part) for part in error.absolute_path) or "$",
            error.message,
        )
        for error in validator.iter_errors(event)
    ]


def validate_event_log(
    path: Path,
    episode: dict[str, Any],
) -> list[ValidationIssue]:
    """Validate every JSONL event and its cross-record invariants."""
    issues: list[ValidationIssue] = []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        return [ValidationIssue("events.jsonl", str(exc))]

    if not lines:
        return [ValidationIssue("events.jsonl", "event log is empty")]

    events: list[dict[str, Any]] = []
    previous_timestamp: datetime.datetime | None = None
    for line_number, line in enumerate(lines, start=1):
        prefix = f"events.jsonl:{line_number}"
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(ValidationIssue(prefix, f"invalid JSON: {exc}"))
            continue
        if not isinstance(event, dict):
            issues.append(ValidationIssue(prefix, "event must be a JSON object"))
            continue
        events.append(event)
        for issue in validate_event(event):
            suffix = "" if issue.path == "$" else f".{issue.path}"
            issues.append(ValidationIssue(f"{prefix}{suffix}", issue.message))

        if event.get("run_id") != episode.get("run_id"):
            issues.append(
                ValidationIssue(
                    f"{prefix}.run_id",
                    "does not match episode run_id",
                )
            )
        if event.get("sequence") != line_number - 1:
            issues.append(
                ValidationIssue(
                    f"{prefix}.sequence",
                    f"expected contiguous sequence {line_number - 1}",
                )
            )

        try:
            timestamp = datetime.datetime.fromisoformat(
                str(event.get("timestamp", "")).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if previous_timestamp is not None and timestamp < previous_timestamp:
            issues.append(
                ValidationIssue(
                    f"{prefix}.timestamp",
                    "event timestamps are not monotonic",
                )
            )
        previous_timestamp = timestamp

    final_seals = [
        event
        for event in events
        if event.get("event_type") == "artifact_sealed"
        and event.get("payload", {}).get("artifact") == "final_solution"
    ]
    final_evaluations = [
        event
        for event in events
        if event.get("event_type") == "evaluation"
        and event.get("payload", {}).get("phase") == "final"
    ]
    expected_final_seals = (
        1 if episode.get("compliance", {}).get("parent_finalized") else 0
    )
    if len(final_seals) != expected_final_seals:
        issues.append(
            ValidationIssue(
                "events.jsonl",
                f"expected exactly {expected_final_seals} final_solution seals, "
                f"found {len(final_seals)}",
            )
        )
    if len(final_evaluations) != 1:
        issues.append(
            ValidationIssue(
                "events.jsonl",
                f"expected exactly one final evaluation, found {len(final_evaluations)}",
            )
        )
    if expected_final_seals == 1 and len(final_seals) == 1 and len(final_evaluations) == 1:
        if final_evaluations[0].get("sequence", -1) <= final_seals[0].get("sequence", -1):
            issues.append(
                ValidationIssue(
                    "events.jsonl",
                    "final hidden evaluation must occur after final_solution seal",
                )
            )
    if len(final_evaluations) == 1:
        outcome = episode.get("final_outcome", {})
        payload = final_evaluations[0].get("payload", {})
        for field in ("evaluator_status", "n_units"):
            if payload.get(field) != outcome.get(field):
                issues.append(
                    ValidationIssue(
                        f"events.jsonl.final_evaluation.{field}",
                        "does not match episode final_outcome",
                    )
                )

    # ── Usage / event count cross-checks ──────────────────────────────────
    # Count model_call and tool_call events per role, then compare to usage record.
    usage = episode.get("usage", {})
    for role in ("worker", "parent"):
        role_usage = usage.get(role, {})
        expected_model_calls = role_usage.get("model_calls")
        expected_tool_calls = role_usage.get("tool_calls")

        actual_model_calls = sum(
            1 for e in events
            if e.get("event_type") == "model_call" and e.get("role") == role
        )
        actual_tool_calls = sum(
            1 for e in events
            if e.get("event_type") == "tool_call" and e.get("role") == role
        )

        if expected_model_calls is not None and actual_model_calls != expected_model_calls:
            issues.append(
                ValidationIssue(
                    f"usage.{role}.model_calls",
                    f"event count {actual_model_calls} != usage record {expected_model_calls}",
                )
            )
        if expected_tool_calls is not None and actual_tool_calls != expected_tool_calls:
            issues.append(
                ValidationIssue(
                    f"usage.{role}.tool_calls",
                    f"event count {actual_tool_calls} != usage record {expected_tool_calls}",
                )
            )
        for field in (
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            expected_tokens = role_usage.get(field)
            actual_tokens = sum(
                int(e.get("payload", {}).get(field, 0) or 0)
                for e in events
                if e.get("event_type") == "model_call" and e.get("role") == role
            )
            if expected_tokens is not None and actual_tokens != expected_tokens:
                issues.append(
                    ValidationIssue(
                        f"usage.{role}.{field}",
                        f"event total {actual_tokens} != usage record {expected_tokens}",
                    )
                )

    return sorted(issues, key=lambda issue: (issue.path, issue.message))


def validate_files(paths: Iterable[Path]) -> dict[Path, list[ValidationIssue]]:
    results: dict[Path, list[ValidationIssue]] = {}
    run_ids: dict[str, Path] = {}
    for path in paths:
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            results[path] = [ValidationIssue("$", str(exc))]
            continue
        issues = validate_episode(record, path)
        events_path = path.parent / "events.jsonl"
        if events_path.is_file():
            issues.extend(validate_event_log(events_path, record))
        elif path.name == "episode.json" and record.get("status") == "complete":
            issues.append(
                ValidationIssue("events.jsonl", f"completed event log does not exist: {events_path}")
            )
        run_id = record.get("run_id")
        if run_id in run_ids:
            issues.append(
                ValidationIssue("run_id", f"duplicate run_id also found in {run_ids[run_id]}")
            )
        elif isinstance(run_id, str):
            run_ids[run_id] = path
        results[path] = issues
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episodes", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = False
    for path, issues in validate_files(args.episodes).items():
        if issues:
            failed = True
            print(f"INVALID {path}")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print(f"VALID {path}")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
