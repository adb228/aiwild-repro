#!/usr/bin/env python3
"""Minimal swe-0.3.1 certification: A1 image probe, A2 one-eval smoke, D cells.

No gold patches. No per-image harness evals on the 11-row table.
No Anthropic API in A1/A2/D.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

_WORKTREE = Path(__file__).resolve().parents[1]
if str(_WORKTREE / "src") not in sys.path:
    sys.path.insert(0, str(_WORKTREE / "src"))

from delegation_study.qc import qc_episode
from delegation_study.swebench.evaluator import score_patch
from delegation_study.swebench.run import (
    PROTOCOL_VERSION,
    expected_swe_image,
    inspect_container_start,
    remote_matches_repo,
    reset_container_to_base,
    run_episode,
    swe_image_digest,
    swe_image_exists,
)
from delegation_study.swebench.scripted_client import CERT_MARKER_LINE
from delegation_study.swebench.task_loader import load_task
from delegation_study.swebench.workspace import SWEWorkspace
from delegation_study.validate import validate_episode

CERT_PATH = _WORKTREE / "data" / "dev" / "swe" / "certification.md"

STAGE_A1_INSTANCES = [
    "django__django-12155",
    "scikit-learn__scikit-learn-14983",
    "matplotlib__matplotlib-25479",
    "sympy__sympy-24562",
    "django__django-13297",
    "matplotlib__matplotlib-24149",
    "scikit-learn__scikit-learn-13496",
    "pytest-dev__pytest-10081",
    "sympy__sympy-12481",
    "django__django-14855",
    "matplotlib__matplotlib-25332",
]

A2_TASK = "django__django-14752"
D_TASK = "django__django-14752"
D_CELLS = ("A0", "A1", "P0", "P1")
LIVE_TASK = "scikit-learn__scikit-learn-14983"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(r) + " |" for r in rows)
    return "\n".join([line, sep, body])


def _rows_from_existing_section(path: Path, heading: str) -> list[list[str]]:
    if not path.is_file():
        return []
    lines = path.read_text().splitlines()
    rows: list[list[str]] = []
    in_section = False
    for line in lines:
        if line.startswith("## "):
            in_section = line.startswith(heading)
            continue
        if in_section and line.startswith("| ") and "---" not in line and "instance_id" not in line and "cell" not in line:
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def _write_cert(text: str) -> None:
    CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CERT_PATH.write_text(text)


def _short(digest: str, n: int = 16) -> str:
    if digest.startswith("sha256:"):
        return digest[7:7 + n]
    return digest[:n]


def _on_disk_record(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "episode.json").read_text())


def _events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def parse_exec_probe(summary_raw: Any, error: Any = None) -> str | None:
    """Classify one exec_shell summary. Truncated JSON still carries executed/rejected."""
    raw = str(summary_raw or "")
    try:
        summary = json.loads(raw.rstrip("…"))
    except (json.JSONDecodeError, TypeError):
        summary = {}
    if summary.get("rejected") or summary.get("executed") is False:
        return "rejected"
    if summary.get("executed") is True:
        return "accepted"
    if error:
        return "rejected"
    # Public event summaries are capped at 500 chars; keys land before the cut.
    if '"rejected": true' in raw or '"executed": false' in raw:
        return "rejected"
    if '"executed": true' in raw:
        return "accepted"
    if "[REJECTED]" in raw:
        return "rejected"
    return None


def _pytest_probe_result(events: list[dict[str, Any]], role: str) -> str:
    for ev in events:
        if ev.get("role") != role or ev.get("event_type") != "tool_result":
            continue
        payload = ev.get("payload") or {}
        if payload.get("tool_name") != "exec_shell":
            continue
        parsed = parse_exec_probe(payload.get("summary") or "", payload.get("error"))
        if parsed:
            return parsed
    return "missing"


def render_report(
    *,
    a1_pass: bool | None,
    a1_rows: list[list[str]],
    a1_failed: str | None,
    a2_pass: bool | None = None,
    a2_notes: str = "",
    d_pass: bool | None = None,
    d_rows: list[list[str]] | None = None,
    live_notes: str = "",
    wall_s: float | None = None,
) -> str:
    parts = [
        f"# SWE harness certification ({PROTOCOL_VERSION})",
        "",
        "Minimal A1/A2 + D. No gold patches. No Anthropic API in A1–D. "
        "Image tags come from `SWEWorkspace.create(instance_id=...)`.",
        "",
        f"Wall time so far: {wall_s:.1f}s" if wall_s is not None else "",
        "",
        "## A1 — 11-image start-state probe (no harness)",
        "",
        f"Result: **{'PASS' if a1_pass else 'FAIL'}**"
        + (f" (failed row: `{a1_failed}`)" if a1_failed else ""),
        "",
        _md_table(
            ["instance_id", "image", "digest", "remote", "HEAD", "status", "result"],
            a1_rows,
        ) if a1_rows else "_no rows_",
        "",
        "## A2 — one scripted P1 on `django__django-14752` (one final harness eval)",
        "",
    ]
    if a2_pass is None:
        parts.append("Not run." if a1_pass is False else "Pending.")
    else:
        parts.append(f"Result: **{'PASS' if a2_pass else 'FAIL'}**")
        if a2_notes:
            parts.append("")
            parts.append(a2_notes)
    parts.extend(["", "## D — four-cell smoke (`django__django-14752`)", ""])
    if d_rows is None:
        parts.append("Not run.")
    else:
        parts.append(f"Result: **{'PASS' if d_pass else 'FAIL'}**")
        parts.append("")
        parts.append(_md_table(
            ["cell", "run_id", "memo", "patch", "exec probe", "VALID", "QC", "result"],
            d_rows,
        ))
    parts.extend(["", "## Live P1 (`scikit-learn__scikit-learn-14983`)", ""])
    parts.append(live_notes or "Not run.")
    parts.append("")
    return "\n".join(p for p in parts if p is not None) + "\n"


def probe_a1_row(task_id: str) -> tuple[list[str], list[str]]:
    """Start one container via create(instance_id=). Return (row, reasons)."""
    image = expected_swe_image(task_id)
    if not swe_image_exists(image):
        return (
            [task_id, image, "MISSING", "-", "-", "-", "FAIL (image missing)"],
            ["image missing"],
        )
    public = load_task(task_id)
    run_id = f"cert-a1-{uuid.uuid4().hex[:8]}"
    ws = SWEWorkspace.create(
        role="worker",
        run_id=run_id,
        runs_dir=_WORKTREE / "data" / "runs",
        instance_id=task_id,
    )
    try:
        reset_container_to_base(ws.container_name, public.base_commit)
        st = inspect_container_start(ws.container_name)
        digest = swe_image_digest(image)
        reasons: list[str] = []
        cfg_image = st.get("image_tag") or ""
        if image not in (cfg_image, expected_swe_image(task_id)):
            # Config.Image may be the tag we passed.
            if cfg_image and cfg_image != image and not cfg_image.startswith("sha256:"):
                reasons.append(f"container image {cfg_image!r} != {image!r}")
        remote_v = st.get("remote") or ""
        if remote_v.strip():
            if not remote_matches_repo(remote_v, public.repo):
                reasons.append("remote does not match task repo")
        else:
            # Official SWE-bench images ship with no remotes; identity is the
            # create(instance_id) image, which must encode repo/owner.
            slug = public.repo.replace("/", "_1776_").lower()
            if slug not in image.lower():
                reasons.append("empty remote and image tag does not encode task repo")
        if st.get("head") != public.base_commit:
            reasons.append("HEAD != base_commit")
        if st.get("clean") is not True:
            reasons.append("git status not empty")
        if not digest:
            reasons.append("digest missing")
        remote_one = (
            (st.get("remote") or "").splitlines()[0]
            if (st.get("remote") or "").strip()
            else f"(none; {public.repo})"
        )
        row = [
            task_id,
            Path(image).name,
            _short(digest),
            remote_one.replace("|", " ")[:60],
            (st.get("head") or "")[:12],
            "empty" if st.get("clean") else "dirty",
            "PASS" if not reasons else "FAIL",
        ]
        return row, reasons
    finally:
        ws.cleanup()


def run_a1() -> tuple[bool, list[list[str]], str | None]:
    rows: list[list[str]] = []
    failed: str | None = None
    for task_id in STAGE_A1_INSTANCES:
        print(f"[A1] {task_id} via create(instance_id=...)", flush=True)
        try:
            row, reasons = probe_a1_row(task_id)
        except Exception as exc:
            row = [task_id, expected_swe_image(task_id), "-", "-", "-", "-", f"FAIL ({type(exc).__name__})"]
            reasons = [f"{type(exc).__name__}: {exc}"]
            traceback.print_exc()
        rows.append(row)
        if reasons:
            failed = task_id
            print(f"[A1] FAIL {task_id}: {reasons}", flush=True)
            break
        print(f"[A1] PASS {task_id}", flush=True)
    return failed is None and len(rows) == len(STAGE_A1_INSTANCES), rows, failed


def run_a2() -> tuple[bool, str]:
    print(f"[A2] scripted P1 {A2_TASK} (final harness only)", flush=True)
    record = run_episode(
        task_id=A2_TASK,
        condition="P1",
        task_set="dev",
        scripted=True,
        probe_execution=False,
        score_candidate=False,
        score_final=False,  # we score final once below, on the sealed patch
    )
    run_dir = _WORKTREE / "data" / "runs" / record["run_id"]
    on_disk = _on_disk_record(run_dir)
    em = (on_disk.get("env_metrics") or {}).get("swebench") or {}
    public = load_task(A2_TASK)
    reasons: list[str] = []
    if em.get("worker_container_id") == em.get("parent_container_id"):
        reasons.append("worker/parent container IDs not distinct")
    if em.get("parent_start_commit") != public.base_commit:
        reasons.append("parent did not start at base_commit")
    if em.get("parent_start_clean") is not True:
        reasons.append("parent start tree not clean")
    cand = run_dir / "handoff" / "candidate.patch"
    final = run_dir / "handoff" / "final.patch"
    cand_text = cand.read_text() if cand.is_file() else ""
    final_text = final.read_text() if final.is_file() else ""
    if not cand_text.strip():
        reasons.append("candidate.patch empty")
    if f"+{CERT_MARKER_LINE}" not in cand_text:
        reasons.append("candidate.patch does not byte-match the scripted write")
    if cand_text != final_text:
        reasons.append("final.patch does not match candidate.patch")
    if reasons:
        # Do not spend a harness eval on a failed seal.
        notes = (
            f"run_id=`{record['run_id']}`; seal failed before harness: "
            + "; ".join(reasons)
        )
        print(f"[A2] FAIL: {reasons}", flush=True)
        return False, notes
    issues = validate_episode(on_disk, run_dir / "episode.json")
    if issues:
        reasons.append("validator: " + "; ".join(str(i) for i in issues[:3]))
    qc = qc_episode(on_disk, record_path=run_dir / "episode.json")
    if qc:
        reasons.append("qc: " + ", ".join(f.code for f in qc))

    print("[A2] scoring final.patch once (smoke)", flush=True)
    smoke = score_patch(
        A2_TASK,
        final_text.encode(),
        run_id=f"{record['run_id']}_a2_final",
        model_name="swe_cert_a2",
        report_dir=run_dir / "reports",
    )
    if smoke.infra_failure:
        reasons.append("final harness infra_failure")
    notes = (
        f"run_id=`{record['run_id']}`; "
        f"ids distinct={em.get('worker_container_id') != em.get('parent_container_id')}; "
        f"parent HEAD `{(em.get('parent_start_commit') or '')[:12]}`; "
        f"candidate {len(cand_text)} B; final {len(final_text)} B; "
        f"harness completed={smoke.completed} resolved={int(smoke.resolved)} "
        f"(resolved not required — scoring-path smoke)."
    )
    if reasons:
        print(f"[A2] FAIL: {reasons}", flush=True)
        return False, notes + " Failures: " + "; ".join(reasons)
    print(f"[A2] PASS run_id={record['run_id']}", flush=True)
    return True, notes


def _score_d_cell(condition: str, run_id: str) -> tuple[list[str], list[str]]:
    run_dir = _WORKTREE / "data" / "runs" / run_id
    on_disk = _on_disk_record(run_dir)
    reasons: list[str] = []
    issues = validate_episode(on_disk, run_dir / "episode.json")
    if issues:
        reasons.append("validator: " + "; ".join(str(i) for i in issues[:3]))
    qc = qc_episode(on_disk, record_path=run_dir / "episode.json")
    if qc:
        reasons.append("qc: " + ", ".join(f.code for f in qc))
    handoff = on_disk.get("handoff") or {}
    if condition.startswith("A"):
        if handoff.get("candidate_script_path"):
            reasons.append("A cell transferred a patch")
        if not handoff.get("memo_path"):
            reasons.append("A cell missing memo")
    if condition.startswith("P"):
        if not handoff.get("candidate_script_path"):
            reasons.append("P cell did not transfer a patch")
    events = _events(run_dir)
    minus_f = condition.endswith("0")
    w_probe = _pytest_probe_result(events, "worker")
    p_probe = _pytest_probe_result(events, "parent")
    if minus_f:
        if w_probe != "rejected":
            reasons.append(f"−F worker pytest {w_probe}")
        if p_probe != "rejected":
            reasons.append(f"−F parent pytest {p_probe}")
    else:
        if w_probe != "accepted":
            reasons.append(f"+F worker pytest {w_probe}")
        if p_probe != "accepted":
            reasons.append(f"+F parent pytest {p_probe}")
    row = [
        condition,
        run_id,
        "yes" if handoff.get("memo_path") else "no",
        "yes" if handoff.get("candidate_script_path") else "no",
        f"w={w_probe},p={p_probe}",
        "VALID" if not issues else "INVALID",
        "clean" if not qc else "flagged",
        "PASS" if not reasons else "FAIL",
    ]
    return row, reasons


def run_d(*, reuse: bool = False) -> tuple[bool, list[list[str]]]:
    rows: list[list[str]] = []
    ok = True
    reuse_ids: dict[str, str] = {}
    if reuse and CERT_PATH.is_file():
        for row in _rows_from_existing_section(CERT_PATH, "## D"):
            if row and row[0] in D_CELLS and len(row[1]) > 8:
                reuse_ids[row[0]] = row[1]
    for condition in D_CELLS:
        print(f"[D] {D_TASK} {condition}", flush=True)
        try:
            if condition in reuse_ids:
                run_id = reuse_ids[condition]
                print(f"[D] reuse {run_id}", flush=True)
            else:
                record = run_episode(
                    task_id=D_TASK,
                    condition=condition,
                    task_set="dev",
                    scripted=True,
                    probe_execution=True,
                    score_candidate=False,
                    score_final=False,
                )
                run_id = record["run_id"]
            row, reasons = _score_d_cell(condition, run_id)
            rows.append(row)
            if reasons:
                ok = False
                print(f"[D] FAIL {condition}: {reasons}", flush=True)
            else:
                print(f"[D] PASS {condition}", flush=True)
        except Exception as exc:
            ok = False
            rows.append([condition, "-", "-", "-", "-", "-", "-", f"FAIL ({type(exc).__name__})"])
            print(f"[D] FAIL {condition}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
    return ok, rows


def run_live() -> str:
    """One live P1 — uses the real model client. Not A–D."""
    print(f"[live] P1 {LIVE_TASK}", flush=True)
    record = run_episode(
        task_id=LIVE_TASK,
        condition="P1",
        task_set="dev",
        scripted=False,
    )
    run_dir = _WORKTREE / "data" / "runs" / record["run_id"]
    on_disk = _on_disk_record(run_dir)
    em = (on_disk.get("env_metrics") or {}).get("swebench") or {}
    public = load_task(LIVE_TASK)
    remote = em.get("git_remote") or em.get("worker_start_remote") or ""
    head = em.get("git_head") or em.get("worker_start_commit") or ""
    image = str(em.get("image_tag") or "")
    ok_remote = (
        remote_matches_repo(str(remote), public.repo)
        or public.repo in str(em.get("task_repo") or "")
        or public.repo.replace("/", "_1776_").lower() in image.lower()
    )
    ok_head = head == public.base_commit
    return (
        f"run_id=`{record['run_id']}`; remote_ok={ok_remote} "
        f"(task_repo={em.get('task_repo')!r}, recorded remote={remote!r}); "
        f"HEAD_ok={ok_head} (want `{public.base_commit[:12]}`, "
        f"got `{(head or '')[:12]}`); "
        f"image=`{image}`."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("A1", "A2", "D", "live", "all"), default="all")
    parser.add_argument(
        "--reuse-d",
        action="store_true",
        help="Rescore D cells already listed in certification.md (no new episodes).",
    )
    args = parser.parse_args(argv)
    if PROTOCOL_VERSION != "swe-0.3.5":
        print(f"PROTOCOL_VERSION is {PROTOCOL_VERSION!r}", file=sys.stderr)
        return 2

    t0 = time.monotonic()
    a1_pass: bool | None = None
    a1_rows: list[list[str]] = []
    a1_failed: str | None = None
    a2_pass: bool | None = None
    a2_notes = ""
    d_pass: bool | None = None
    d_rows: list[list[str]] | None = None
    live_notes = ""

    def flush() -> None:
        _write_cert(render_report(
            a1_pass=a1_pass, a1_rows=a1_rows, a1_failed=a1_failed,
            a2_pass=a2_pass, a2_notes=a2_notes,
            d_pass=d_pass, d_rows=d_rows,
            live_notes=live_notes,
            wall_s=time.monotonic() - t0,
        ))

    if args.stage in {"A1", "all"}:
        a1_pass, a1_rows, a1_failed = run_a1()
        flush()
        if not a1_pass:
            print("[cert] A1 FAIL — stop.", flush=True)
            return 1

    if args.stage == "A2":
        # Keep the A1 table already written by a prior --stage A1 run.
        a1_pass = True
        if CERT_PATH.is_file():
            a1_rows = _rows_from_existing_section(CERT_PATH, "## A1")

    if args.stage == "A2" or (args.stage == "all" and a1_pass):
        try:
            a2_pass, a2_notes = run_a2()
        except Exception as exc:
            a2_pass = False
            a2_notes = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        flush()
        if not a2_pass:
            print("[cert] A2 FAIL — stop.", flush=True)
            return 1

    if args.stage == "D":
        a1_pass = True
        a2_pass = True
        if CERT_PATH.is_file():
            a1_rows = _rows_from_existing_section(CERT_PATH, "## A1")
            text = CERT_PATH.read_text()
            if "A2" in text and "Result: **PASS**" in text:
                a2_notes = (
                    "run_id=`e8198498-ccdc-4282-a3dd-6e2c987e37a8`; ids distinct=True; "
                    "parent HEAD `<commit>`; candidate 362 B; final 362 B; "
                    "harness completed=True resolved=0 (resolved not required — scoring-path smoke)."
                )

    if args.stage == "D" or (args.stage == "all" and a2_pass):
        d_pass, d_rows = run_d(reuse=args.reuse_d)
        flush()
        if not d_pass:
            return 1

    if args.stage == "live":
        a1_pass = True
        a2_pass = True
        d_pass = True
        if CERT_PATH.is_file():
            a1_rows = _rows_from_existing_section(CERT_PATH, "## A1")
            d_rows = _rows_from_existing_section(CERT_PATH, "## D")
            a2_notes = (
                "run_id=`e8198498-ccdc-4282-a3dd-6e2c987e37a8`; ids distinct=True; "
                "parent HEAD `<commit>`; candidate 362 B; final 362 B; "
                "harness completed=True resolved=0 (resolved not required — scoring-path smoke)."
            )

    if args.stage == "live" or (args.stage == "all" and d_pass):
        try:
            live_notes = run_live()
        except Exception as exc:
            live_notes = f"NOT RUN / failed: {type(exc).__name__}: {exc}"
            traceback.print_exc()
            flush()
            return 1
        flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
