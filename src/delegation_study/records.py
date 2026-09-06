"""Episode event logger and usage aggregation.

EventLogger appends one JSON event per line to ``events.jsonl``, validating
each event against ``contracts/event_schema.json`` before writing.  If
validation fails the event is NOT written and a ``ValueError`` is raised.

Usage
-----
    from delegation_study.records import EventLogger

    logger = EventLogger(run_dir / "events.jsonl", run_id="abc123")
    logger.log("system", "phase_started", {"phase": "worker"})
    logger.log("worker", "model_call", {"call_index": 0, "input_tokens": 500, ...})
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from .validate import validate_event


class EventLogger:
    """Append-only, schema-validated JSONL event log.

    Thread safety: not thread-safe (single-agent use only).

    Parameters
    ----------
    path:
        Path to the events.jsonl file.  Created (and truncated) on construction.
    run_id:
        Included in every event record for cross-reference with episode.json.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._seq: int = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate / create fresh
        path.write_text("", encoding="utf-8")

    @property
    def sequence(self) -> int:
        """Current sequence counter (next event will have this sequence number)."""
        return self._seq

    def log(
        self,
        role: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Append one validated event to the log.

        Parameters
        ----------
        role:
            One of ``"system"``, ``"worker"``, ``"parent"``, ``"evaluator"``.
        event_type:
            One of the enum values in ``contracts/event_schema.json``.
        payload:
            Arbitrary dict (validated by schema as ``type: object``).

        Returns
        -------
        The event dict that was written (for callers that need the timestamp).

        Raises
        ------
        ValueError
            If the event fails JSON-Schema validation.  The event is NOT written.
        """
        event: dict[str, Any] = {
            "run_id": self._run_id,
            "sequence": self._seq,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "role": role,
            "event_type": event_type,
            "payload": payload,
        }
        issues = validate_event(event)
        if issues:
            raise ValueError(
                f"Event failed schema validation: {[str(i) for i in issues]}"
            )
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        self._seq += 1
        return event
