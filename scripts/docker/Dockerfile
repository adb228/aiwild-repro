# spreadsheet-exec:protocol-v01
# Minimal, non-root sandbox for SpreadsheetBench solution execution.
#
# Run convention (enforced by execute.py):
#   python /solution/solution.py \
#       --input  /input/input.xlsx \
#       --output /output/output.xlsx
#
# Security contract:
#   --network none  --read-only  --pids-limit 128
#   --memory 2g     --cpus 2     --security-opt no-new-privileges
#   --user nobody   (or explicit uid:gid on macOS – see execute.py)
#   --tmpfs /tmp:rw,nosuid,nodev,mode=1777
#   -v <input_xlsx>:/input/input.xlsx:ro    (read-only, per-task)
#   -v <output_dir>:/output                 (writable, per-task-local)
#   -v <script>:/solution/solution.py:ro    (read-only)
#
# Never mount raw benchmark data or hidden evaluator files.

FROM python:3.11-slim

# Install only what solution scripts need
RUN pip install --no-cache-dir \
        openpyxl==3.1.5 \
        pandas==2.2.3 \
        numpy==2.1.3

# Prepare mount-point stubs with world-accessible permissions so that
# the nobody user (uid 65534) can traverse into them even before Docker
# bind-mounts overlay them at runtime.
RUN mkdir -p /input /output /solution \
    && chmod 755 /input /output /solution

# Drop to nobody for the default run; execute.py retries with the host
# uid:gid if macOS bind-mount permissions deny access.
USER nobody

# No default CMD: the caller always passes the full command.
# E.g.: docker run ... spreadsheet-exec:protocol-v01 \
#           python /solution/solution.py \
#           --input /input/input.xlsx --output /output/output.xlsx
