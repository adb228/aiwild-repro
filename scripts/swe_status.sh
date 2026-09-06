#!/usr/bin/env bash
# Read-only SWE main-wave status pointer. Does not start or stop episodes.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
HELPER="${SCRIPT_DIR}/swe_main_batch.py"
SELECTION_REL="config/study/swe_selection_v2/swe_task_selection_v2.json"
SELECTION_JSON="${REPO_ROOT}/${SELECTION_REL}"
REQUIRED_SELECTION_SHA256="087855dc153547ea3543fc293c8491a5a5789d926cedff6ca468eab962f0c225"

usage() {
  cat <<'EOF'
Usage:
  scripts/swe_status.sh
  scripts/swe_status.sh --batch-id BATCH_ID
  scripts/swe_status.sh --status /path/to/status.json

Read-only. Defaults to the newest data/batches/swe-main-*/status.json.
Requires scripts/swe_main_batch.py (not shipped with this pointer).
Does not start tmux, episodes, or containers.
EOF
}

verify_selection_pointer() {
  if [[ ! -f "${SELECTION_JSON}" ]]; then
    echo "swe_status.sh: refusing — selection file missing: ${SELECTION_JSON}" >&2
    exit 1
  fi
  python3 - "${SELECTION_JSON}" "${REQUIRED_SELECTION_SHA256}" <<'PY'
import json
import sys

path, expected = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    print(f"swe_status.sh: refusing — cannot read selection file: {exc}", file=sys.stderr)
    sys.exit(1)
got = data.get("selection_sha256")
if got != expected:
    print(
        f"swe_status.sh: refusing — selection_sha256 mismatch: got {got} expected {expected}",
        file=sys.stderr,
    )
    sys.exit(1)
PY
}

require_helper() {
  if [[ ! -f "${HELPER}" ]]; then
    echo "swe_status.sh: refusing — ${HELPER} is not present; no docker/tmux status queried." >&2
    exit 1
  fi
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  "")
    verify_selection_pointer
    require_helper
    exec python3 "${HELPER}" status --repo-root "${REPO_ROOT}"
    ;;
  --batch-id)
    [[ $# -eq 2 ]] || { usage >&2; exit 2; }
    verify_selection_pointer
    require_helper
    exec python3 "${HELPER}" status --repo-root "${REPO_ROOT}" --batch-id "$2"
    ;;
  --status)
    [[ $# -eq 2 ]] || { usage >&2; exit 2; }
    verify_selection_pointer
    require_helper
    exec python3 "${HELPER}" status --repo-root "${REPO_ROOT}" --status "$2"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
