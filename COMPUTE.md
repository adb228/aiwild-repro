# Compute

Hardware: Apple-silicon laptop, no GPU. SWE evaluation uses official x86_64 images under emulation. Spreadsheet execution uses the `spreadsheet-exec:protocol-v01` image (`sha256:8c861ae2b9511d19225f4f2433f4366311a32a55ad13dc29735a76c6fd165ffb`), 2 CPUs / 2g / pids 128, network none.

Costs use `config/study/pricing.yaml`. Sonnet: `(input*3.00 + output*15.00 + cache_read*0.30 + cache_creation*3.75) / 1e6`. Luna: `(uncached_input*0.20 + cached_input*0.02 + output*1.20) / 1e6`. Different model; not pooled.

**Role-wall** is the sum of recorded per-role `*_wall_seconds` on analysis rows (not calendar batch wall). **Calendar-wall** is `status.json` `updated_at − created_at` (or the sum of those intervals when an arm used more than one batch). Batch `status.json` was read from the live checkouts and is not copied into this archive. Sonnet and Luna are never pooled.

## Reported arms

| Arm | n | Cost (USD) | Role-wall (s) | Calendar-wall (s) |
|---|---:|---:|---:|---:|
| Spreadsheet v0.4.1 main, Sonnet | 100 | 27.21598020 | 16338.136 | 3001.110 |
| Spreadsheet v0.4.1 Solo, Sonnet | 25 | 3.77577690 | 1902.331 | 414.258 |
| SWE-0.3.5 main, Sonnet | 32 | 104.59005420 | 24616.94 | MISSING |
| Spreadsheet v0.4.1 main, Luna | 100 | 1.89720902 | 6509.863 | 1208.703 |
| Spreadsheet v0.4.1 Solo, Luna | 25 | 0.36121552 | 905.774 | 201.495 |
| SWE-0.3.5 main, Luna | 32 | 14.91523740 | 16007.90 | 8946.130 |
| Fixed-artifact parent replay, Sonnet (S2; never pooled with factorial estimands) | 100 | 11.06456310 | 5437.910 | 951.319 |
| **Reported total** | **414** | **163.82003634** | **71718.854** | — |

Calendar-wall notes:

- Spreadsheet v0.4.1 main Sonnet: batch `dab91361-…` (`main_v04`).
- Spreadsheet v0.4.1 Solo Sonnet: batch `4c894fa9-…` (`solo_v04`).
- SWE-0.3.5 Sonnet: batch `swe-main-v035-20260905T031032Z-f43ee6b7` has `created_at` and no `updated_at` / `finalized_at`; episode rows in that status file have no `run_id`. End is **MISSING** (not inferred).
- Luna Spreadsheet main: three `main_v04_m2` batches (80 + 20 + 16-cell relaunch). Column is the sum of the three status intervals (1208.703 s). Campaign span first-start→last-end = 1315.310 s.
- Luna Solo / Luna SWE: one batch each (`solo_v04_m2`, `swe_main_m2`).
- Fixed-artifact: confirmatory n=100 batch `77b11f88-…` only (the n=4 smoke batch is not in this row).

## Everything in `analysis/` (includes companion / closed)

| Arm | n | Cost (USD) | Role-wall (s) | Calendar-wall (s) | Notes |
|---|---:|---:|---:|---:|---|
| Spreadsheet v0.3 main, Sonnet | 100 | 22.63268235 | 14712.192 | 3369.352 | Live `episode.json` usage only; freeze CSV had empty token/wall columns |
| Spreadsheet v0.3 Solo, Sonnet | 25 | 4.33777035 | 2232.031 | 939.792 | Same source; Solo usage is parent-only |
| Spreadsheet v0.3 hard probe, Sonnet | 25 | 5.85389340 | 3261.133 | 632.564 | Supplement only; never pooled |
| SWE A-2 / 0.3.3–0.3.4 closed matrix, Sonnet | 32 | 90.08560050 | 18226.05 | MISSING | Closed; not the SWE-0.3.5 confirmatory matrix. Status files have no usable end |

v0.3 / hard token, cost, and role-wall were filled by reading only `usage.worker` / `usage.parent` token and `wall_seconds` fields from live confirmatory `data/runs/*/episode.json` (read-only; not copied into this archive) and applying the Sonnet list formula. Never gold/hidden.

v0.3 main calendar-wall is the sum of five `task_set=main` status intervals that contain those run IDs. Campaign span is 32120.261 s because Wave 3 ran hours after Waves 1–2. Wave-3 batch `4340cc18-…` also launched 5 Solo cells, so 538.368 s of that interval is shared with the Solo row.

## Batch status intervals (read-only; not in the ZIP)

| Arm | Batch | created_at | updated_at | Interval (s) |
|---|---|---|---|---:|
| SS v0.4.1 main Sonnet | `dab91361-0497-4f0c-a92b-426e99adaa9f` | 2026-09-03T19:26:56Z | 2026-09-03T20:16:57Z | 3001.110 |
| SS v0.4.1 Solo Sonnet | `4c894fa9-c79d-4279-9d5a-e848d5f41e89` | 2026-09-03T20:30:05Z | 2026-09-03T20:37:00Z | 414.258 |
| SWE-0.3.5 Sonnet | `swe-main-v035-20260905T031032Z-f43ee6b7` | 2026-09-05T03:10:32Z | — | MISSING |
| SS v0.4.1 main Luna | `7ee0737d-7ba4-42a1-ad95-9121c7a27b2e` | 2026-09-05T08:08:39Z | 2026-09-05T08:24:40Z | 961.548 |
| SS v0.4.1 main Luna | `1f5458c3-bac0-4271-9bf0-c2e03708c310` | 2026-09-05T08:24:57Z | 2026-09-05T08:25:56Z | 59.443 |
| SS v0.4.1 main Luna | `20bbc9b8-4bb2-4cd6-89b3-ca5885ee4543` | 2026-09-05T08:27:26Z | 2026-09-05T08:30:34Z | 187.712 |
| SS v0.4.1 Solo Luna | `1c1bdfb7-ca6e-4c88-9fa7-971baffca5f0` | 2026-09-05T08:31:33Z | 2026-09-05T08:34:54Z | 201.495 |
| SWE-0.3.5 Luna | `df5ed272-7664-4a86-bd8b-7534f1511144` | 2026-09-05T09:50:54Z | 2026-09-05T12:20:01Z | 8946.130 |
| Fixed-artifact Sonnet | `77b11f88-a45c-45f4-8ac0-81cacae8206c` | 2026-09-05T06:09:34Z | 2026-09-05T06:25:25Z | 951.319 |
| SS v0.3 Wave 1a | `10a5d96e-5d83-4804-a511-8254425ef568` | 2026-09-03T07:15:45Z | 2026-09-03T07:31:56Z | 971.859 |
| SS v0.3 Wave 1b | `291224e1-ab92-472b-b08a-a46e9a31644b` | 2026-09-03T07:48:05Z | 2026-09-03T07:51:47Z | 221.622 |
| SS v0.3 Wave 2a | `65c99285-94c2-40d3-931e-e6daeeff9fac` | 2026-09-03T08:09:19Z | 2026-09-03T08:16:21Z | 422.591 |
| SS v0.3 Wave 2b | `cbf907bf-4673-49bc-8420-63412471babe` | 2026-09-03T08:16:23Z | 2026-09-03T08:36:37Z | 1214.913 |
| SS v0.3 Solo-20 | `df9564f9-e7ee-4dcb-b124-b1f33f6a6e52` | 2026-09-03T15:55:24Z | 2026-09-03T16:02:05Z | 401.424 |
| SS v0.3 Wave 3 (5 tasks + 5 Solo) | `4340cc18-6efa-46a0-be26-13f44a50bb71` | 2026-09-03T16:02:06Z | 2026-09-03T16:11:05Z | 538.368 |
| SS v0.3 hard | `c5654558-eae5-44d7-9efa-d6d41fb35084` | 2026-09-03T16:27:25Z | 2026-09-03T16:37:57Z | 632.564 |
| SWE A-2 main | `swe-main-20260904T085320Z-3e6d34c3` | — | — | MISSING |
| SWE A-2 rerun | `swe-main-034-20260904T111326Z-c95d3893` | 2026-09-04T11:13:26Z | — | MISSING |
