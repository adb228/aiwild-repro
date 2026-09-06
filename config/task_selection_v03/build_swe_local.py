"""
Build swe_verified_local.json (the 500 SWE-bench Verified rows + difficulty labels)
from two public GitHub mirrors, for environments without Hugging Face access.

    git clone --depth 1 https://github.com/OpenAgentsInc/swe-bench-verified
    git clone --depth 1 https://github.com/SWE-bench/swe-bench-tasks
    python build_swe_local.py

Rows come from OpenAgentsInc/swe-bench-verified/swe_bench_data.jsonl (a copy of the
HF dataset minus the `difficulty` column). Difficulty labels come from the official
SWE-bench/swe-bench-tasks repo, tasks/<instance_id>/task.yaml. The resulting label
distribution (194 / 261 / 42 / 3) matches OpenAI's published Verified breakdown.
"""
import json, os, yaml, collections

rows = [json.loads(l) for l in open("swe-bench-verified/swe_bench_data.jsonl")]
diff = {}
for t in os.listdir("swe-bench-tasks/tasks"):
    p = f"swe-bench-tasks/tasks/{t}/task.yaml"
    if os.path.exists(p):
        y = yaml.safe_load(open(p))
        if "SWE-bench/SWE-bench_Verified" in (y.get("datasets") or []):
            diff[y["instance_id"]] = y.get("difficulty")
for r in rows:
    r["difficulty"] = diff[r["instance_id"]]
assert len(rows) == 500 and all(r["difficulty"] for r in rows)
json.dump(rows, open("swe_verified_local.json", "w"))
print(len(rows), collections.Counter(r["difficulty"] for r in rows))
