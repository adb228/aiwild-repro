"""Scientific quality-control flags that retain valid episode records."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class QCFlag:
    code: str
    message: str


def memo_body_is_empty(
    record: dict[str, Any],
    *,
    record_path: Path | None = None,
) -> bool:
    """Return True iff a readable memo exists and ``task_understanding`` is empty.

    A memo file that cannot be read (missing path, missing file, or invalid JSON)
    returns False so path-only fixtures keep the existing ``memo_path`` check.
    Empty body means ``task_understanding`` is absent, not a string, or
    whitespace-only — the protocol body field. Gate/QC asserts this exact
    ``advisory_missing_memo`` code, not a separate empty-memo code.
    """
    handoff = record.get("handoff") or {}
    raw = handoff.get("memo_path")
    candidates: list[Path] = []
    if raw:
        path = Path(raw)
        if path.is_absolute():
            candidates.append(path)
        elif record_path is not None:
            candidates.append(record_path.parent / path)
        else:
            candidates.append(path)
    if record_path is not None:
        candidates.append(record_path.parent / "handoff" / "memo.json")
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            continue
        try:
            memo = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(memo, dict):
            return True
        text = memo.get("task_understanding")
        return not (isinstance(text, str) and bool(text.strip()))
    return False


def _event_derived_feedback_count(events_path: Path) -> int | None:
    """Derive executed-only feedback call count from events.jsonl.

    Returns the count of exec_shell tool_result events where the container
    actually executed the command (``executed: true`` in new format or
    ``wall_seconds > 0.001`` as proxy for old format) AND the command was a
    feedback-capable type (``feedback_command: true`` in new format).

    Returns ``None`` if the events file does not exist or cannot be parsed.
    If no event has a ``feedback_command`` key (pre-accounting-fix runs), returns
    ``None`` to signal that accurate event derivation is not possible and the
    caller should fall back to the stored compliance value.
    """
    if not events_path.exists():
        return None

    raw_events: list[dict[str, Any]] = []
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw_events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # Isolate worker exec_shell tool_result events.
    worker_exec_results: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ev in raw_events:
        if ev.get("event_type") != "tool_result":
            continue
        if ev.get("role") != "worker":
            continue
        payload = ev.get("payload", {})
        if payload.get("tool_name") != "exec_shell":
            continue
        summary_str = payload.get("summary", "{}")
        if isinstance(summary_str, str) and summary_str.endswith("…"):
            summary_str = summary_str[:-1]  # trim truncation marker
        try:
            summary: dict[str, Any] = json.loads(summary_str)
        except (json.JSONDecodeError, TypeError):
            summary = {}
        worker_exec_results.append((ev, summary))

    # If there are no worker exec_shell events, the count is definitively 0 —
    # we don't need the feedback_command marker to make that determination.
    if not worker_exec_results:
        return 0

    # Check whether events carry the new-format feedback_command marker.
    # Events produced before "Count only executed commands as feedback" lack it,
    # meaning we cannot reliably distinguish python/pytest from other commands.
    has_feedback_marker = any(
        "feedback_command" in summary for _, summary in worker_exec_results
    )
    if not has_feedback_marker:
        # Old event format: signal fallback to stored compliance value.
        return None

    # Count executed feedback-capable commands.
    count = 0
    for _, summary in worker_exec_results:
        # Determine whether Docker actually ran the command.
        # New format: "executed" key is authoritative.
        # Old format proxy: wall_seconds == 0.0 means rejection (no Docker call).
        if "executed" in summary:
            actually_executed = bool(summary["executed"])
        else:
            actually_executed = summary.get("wall_seconds", 0.0) > 0.001

        if not actually_executed:
            continue

        # Determine whether it was a feedback-capable command type.
        # New format: "feedback_command" key is authoritative.
        # Old format: key absent → conservatively skip (can't determine).
        if not summary.get("feedback_command", False):
            continue

        count += 1

    return count


def qc_episode(
    record: dict[str, Any],
    *,
    record_path: Path | None = None,
) -> list[QCFlag]:
    """Return QC flags for ``record``.

    Parameters
    ----------
    record:
        Parsed episode JSON record.
    record_path:
        Optional path to the episode JSON file.  When provided, QC will attempt
        to derive the executed-only feedback count from the adjacent
        ``events.jsonl`` file instead of the stored ``compliance`` value.
        This ensures that rejected commands (policy/allowlist/metacharacter) are
        never counted as feedback calls, even for runs produced before the
        executed-only accounting fix was committed.
    """
    flags: list[QCFlag] = []
    condition = record["condition"]
    handoff = record["handoff"]
    compliance = record["compliance"]

    # All treatment-protocol checks run regardless of `status`.
    # Per protocol: treatment violations are flagged and retained; only
    # malformed records are rejected by the validator.  `infrastructure_failure`
    # captures whether the episode completed — it is orthogonal to whether the
    # agents followed the protocol.

    if condition.startswith("P") and not handoff.get("candidate_script_path"):
        flags.append(QCFlag("production_missing_artifact", "Production transferred no script"))
    if condition.startswith("P") and not handoff.get("memo_path"):
        flags.append(QCFlag("production_missing_memo", "Production transferred no memo"))
    if condition.startswith("A") and not handoff.get("memo_path"):
        flags.append(QCFlag("advisory_missing_memo", "Advisory transferred no memo"))
    elif condition.startswith("A") and memo_body_is_empty(record, record_path=record_path):
        flags.append(QCFlag("advisory_missing_memo", "Advisory transferred an empty memo"))
    if condition.startswith("A") and handoff.get("candidate_script_path"):
        flags.append(QCFlag("advisory_artifact_leak", "Advisory transferred a script"))

    # Executed-only feedback count for the withheld_feedback_call check.
    # Prefer event-derived count when record_path is supplied and events.jsonl
    # has the feedback_command marker. Falls back to the stored compliance
    # value for pre-fix records or when events are unavailable.
    if condition.endswith("0"):
        fb_count: int
        if record_path is not None:
            events_path = record_path.parent / "events.jsonl"
            derived = _event_derived_feedback_count(events_path)
            if derived is not None:
                fb_count = derived
            else:
                fb_count = compliance.get("worker_feedback_calls", 0)
        else:
            fb_count = compliance.get("worker_feedback_calls", 0)

        if fb_count > 0:
            flags.append(
                QCFlag("withheld_feedback_call", "Worker received executable feedback in a 0 cell")
            )

    if condition.endswith("0") and (
        compliance.get("worker_observed_output_calls", 0) > 0
        or compliance.get("worker_auto_observations", 0) > 0
    ):
        flags.append(
            QCFlag("withheld_observation_call", "Worker used read_output_range in a 0 cell (treatment violation)")
        )
    if not compliance.get("parent_finalized", False):
        flags.append(QCFlag("parent_not_finalized", "Parent did not seal a final script"))
    if condition == "A1" and not compliance.get("worker_scratch_destroyed", False):
        flags.append(QCFlag("advisory_scratch_retained", "A1 scratch survived handoff"))

    for failure in record.get("failures", []):
        if failure.get("source") == "agent" and failure.get("code") == "worker_timeout":
            flags.append(QCFlag("worker_timeout", failure.get("message", "Worker timed out")))

    worker_outcome = record.get("worker_outcome")
    if handoff.get("candidate_script_path") and (
        worker_outcome is None or worker_outcome.get("evaluator_status") == "evaluator_failure"
    ):
        flags.append(QCFlag("invalid_candidate", "Candidate could not be evaluated"))

    # v0.4.1 auto-observation compliance flags
    # Flag A1/P1 worker run events missing auto_observation payload
    if condition in ("A1", "P1") and compliance.get("worker_runs_without_auto_observation", 0) > 0:
        flags.append(QCFlag(
            "missing_worker_auto_observation",
            f"A1/P1 worker had {compliance['worker_runs_without_auto_observation']} run event(s) "
            "missing auto_observation payload (treatment violation)",
        ))
    # Flag any parent/SOLO run events missing auto_observation payload
    if compliance.get("parent_runs_without_auto_observation", 0) > 0:
        flags.append(QCFlag(
            "missing_parent_auto_observation",
            f"Parent/SOLO had {compliance['parent_runs_without_auto_observation']} run event(s) "
            "missing auto_observation payload (treatment violation)",
        ))

    return flags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    record = json.loads(args.episode.read_text())
    # Pass record_path so QC can use event-derived feedback count when the
    # events.jsonl (with feedback_command markers) is present alongside the record.
    result = {
        "run_id": record.get("run_id"),
        "passed": not (flags := qc_episode(record, record_path=args.episode)),
        "flags": [asdict(flag) for flag in flags],
    }
    rendered = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
