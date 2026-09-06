#!/usr/bin/env bash
# SWE main-wave launcher pointer. Pins Amendment A-2 selection.
# Does not embed runner/orchestration code. Launch requires scripts/swe_main_batch.py
# (not shipped on this branch). Caps are not hardcoded; do not invent swebench: caps.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
HELPER="${SCRIPT_DIR}/swe_main_batch.py"

SELECTION_REL="config/study/swe_selection_v2/swe_task_selection_v2.json"
SELECTION_JSON="${REPO_ROOT}/${SELECTION_REL}"
REQUIRED_SELECTION_SHA256="087855dc153547ea3543fc293c8491a5a5789d926cedff6ca468eab962f0c225"

usage() {
  cat <<EOF
Usage: scripts/launch_swe_main.sh [--dry-run|--verify|--preflight]

Pins and requires:
  ${SELECTION_REL}
  selection_sha256 ${REQUIRED_SELECTION_SHA256}

The launcher refuses before any tmux session, batch/run directory, or
container is created if the selection file is missing or its
selection_sha256 does not match (field and canonical recompute).

--dry-run / --verify
  Read-only pin check. Does not start tmux, episodes, or containers.

--preflight
  Pin check, then the optional helper's read-only preflight if present.

With no arguments, pin-checks and then execs the helper launch path.
The helper is not part of this pointer; if it is absent, launch is refused
and nothing is started.

This script does not add or change swebench: caps in config/manifest.yaml.
EOF
}

verify_selection() {
  if [[ ! -f "${SELECTION_JSON}" ]]; then
    echo "launch_swe_main.sh: refusing — selection file missing: ${SELECTION_JSON}" >&2
    exit 1
  fi
  python3 - "${SELECTION_JSON}" "${REQUIRED_SELECTION_SHA256}" <<'PY'
import hashlib
import json
import sys

path, expected = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    print(f"launch_swe_main.sh: refusing — cannot read selection file: {exc}", file=sys.stderr)
    sys.exit(1)

got = data.get("selection_sha256")
if not isinstance(got, str) or not got.strip():
    print("launch_swe_main.sh: refusing — selection_sha256 missing from selection file", file=sys.stderr)
    sys.exit(1)
if got != expected:
    print(
        f"launch_swe_main.sh: refusing — selection_sha256 mismatch: got {got} expected {expected}",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    payload = {key: data[key] for key in ("seed", "filter", "pilot", "main", "reserve_in_order")}
except KeyError as exc:
    print(f"launch_swe_main.sh: refusing — selection payload missing {exc}", file=sys.stderr)
    sys.exit(1)

recomputed = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
if recomputed != expected:
    print(
        f"launch_swe_main.sh: refusing — recomputed selection hash mismatch: {recomputed} expected {expected}",
        file=sys.stderr,
    )
    sys.exit(1)

main_ids = [item.get("instance_id") for item in data.get("main", [])]
print(f"selection_ok path={path}")
print(f"selection_sha256={got}")
print("main_ids=" + ",".join(id_ for id_ in main_ids if isinstance(id_, str)))
PY
}

require_helper() {
  if [[ ! -f "${HELPER}" ]]; then
    echo "launch_swe_main.sh: refusing — ${HELPER} is not present; no tmux/episodes started." >&2
    exit 1
  fi
}

export SWE_SELECTION_JSON="${SELECTION_JSON}"
export SWE_SELECTION_SHA256="${REQUIRED_SELECTION_SHA256}"

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  --dry-run|--verify)
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    verify_selection
    echo "dry-run: pin verified; no tmux, episodes, or containers started."
    exit 0
    ;;
  --preflight)
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    verify_selection
    require_helper
    exec python3 "${HELPER}" preflight --repo-root "${REPO_ROOT}"
    ;;
  "")
    verify_selection
    require_helper
    exec python3 "${HELPER}" launch --repo-root "${REPO_ROOT}"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
