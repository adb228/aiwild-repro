"""
Episode runner skeleton for the StockBench-derived environment (Paper A).

    python run_stock_episode.py --repo <stockbench> --date 2025-04-09 --cell P1 --replicate 0 --out logs/

Cells: A0 A1 P0 P1 SOLO.  The two functions marked TODO are the only LLM-facing pieces;
everything else (bundle, tools, validation, scoring, logging) is fixed here so all cells share it.
Budget enforcement (max calls / tokens / tool steps) belongs inside call_worker / call_parent  [CHECK #2].
"""
import argparse, json, os, sys, time, hashlib, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stockbench_snapshot as h
import check_memo_compliance as cmc

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = json.load(open(os.path.join(HERE, "prompts_stock.json")))
try:
    from llm_client import call_worker, call_parent          # provided: Anthropic SDK implementation
except ImportError:
    call_worker = call_parent = None

def sha(s): return hashlib.sha256(s.encode()).hexdigest()

def _call_worker_stub(cell, bundle_text, sandbox_dir):
    """Contract for call_worker (implemented in llm_client.py).
    cell in {A0,A1,P0,P1}. Prompt = PROMPTS['worker_common'] + PROMPTS['worker_'+cell[0]] + PROMPTS['feedback_'+cell[1]].
    +F cells (A1,P1): agent may run `python backtest_trailing.py --repo .. --date $DECISION_DATE --alloc <file>` inside sandbox_dir,
                       with env DECISION_DATE set; no other tools.  -F cells: no tool calls at all.
    Return dict: {"memo": str} for A cells, {"allocation": dict, "notes": str} for P cells, plus {"tokens_in","tokens_out","calls","tool_calls"}.
    """
    raise NotImplementedError

def _call_parent_stub(bundle_text, handoff, sandbox_dir):
    """Contract for call_parent (implemented in llm_client.py).
    Prompt = PROMPTS['parent'] + handoff (memo text, or 'candidate allocation' JSON + notes, or nothing for SOLO).
    Always has backtest_trailing.py in the sandbox. Never sees the cell name or worker trace.
    Return {"allocation": dict, "tokens_in","tokens_out","calls","tool_calls","trailing_backtests"}.
    """
    raise NotImplementedError

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True); ap.add_argument("--date", required=True)
    ap.add_argument("--cell", required=True, choices=["A0","A1","P0","P1","SOLO"])
    ap.add_argument("--replicate", type=int, default=0); ap.add_argument("--out", default="logs")
    a = ap.parse_args()
    if call_worker is None:
        sys.exit("llm_client.py not found next to this script (see llm_client.py in the kit)")
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    bundle = h.build_bundle(a.repo, a.date)
    text = h.bundle_to_text(bundle)
    sandbox = os.path.join(a.out, f"sandbox_{a.date}_{a.cell}_{a.replicate}"); os.makedirs(sandbox, exist_ok=True)
    os.environ["DECISION_DATE"] = a.date
    log = {"env": "stockbench_derived", "date": a.date, "cell": a.cell, "replicate": a.replicate,
           "bundle_sha256": bundle["bundle_sha256"], "prompt_sha256": {k: sha(v) for k, v in PROMPTS.items()},
           "model": os.environ.get("PAPER_A_MODEL", "[CHECK #1]"), "compliance": {}, "worker": None, "parent": None}

    handoff, raw = None, None
    if a.cell != "SOLO":
        w = call_worker(a.cell, text, sandbox)
        log["worker"] = {k: w.get(k) for k in ("tokens_in","tokens_out","calls","tool_calls")}
        if a.cell[0] == "A":
            ok, why = cmc.check(w["memo"]); log["compliance"]["memo_ok"] = ok; log["compliance"]["memo_reasons"] = why
            handoff = {"type": "memo", "text": w["memo"]}
        else:
            ok, msg = h.validate_allocation(w["allocation"]); log["compliance"]["candidate_valid"] = ok; log["compliance"]["candidate_msg"] = msg
            raw = h.score(a.repo, a.date, w["allocation"]) if ok else None
            handoff = {"type": "candidate", "allocation": w["allocation"], "notes": w.get("notes",""), "validator": msg}
            log["worker"]["allocation"] = w["allocation"]; log["worker"]["raw_score"] = raw
        if a.cell[1] == "0":
            log["compliance"]["worker_no_execution"] = (w.get("tool_calls", 0) == 0)

    p = call_parent(text, handoff, sandbox)
    ok, msg = h.validate_allocation(p["allocation"]); log["compliance"]["final_valid"] = ok; log["compliance"]["final_msg"] = msg
    final = h.score(a.repo, a.date, p["allocation"]) if ok else None
    log["parent"] = {**{k: p.get(k) for k in ("tokens_in","tokens_out","calls","tool_calls","trailing_backtests")},
                     "allocation": p["allocation"], "final_score": final}
    if raw and final:
        wf, ww = p["allocation"], handoff["allocation"]
        keys = set(wf) | set(ww)
        log["parent"]["l1_worker_to_final"] = sum(abs(float(wf.get(k,0)) - float(ww.get(k,0))) for k in keys)
        log["parent"]["accepted_unchanged"] = log["parent"]["l1_worker_to_final"] < 1e-6
        log["parent"]["delta_parent_excess"] = final["excess_return"] - raw["excess_return"]
    log["wall_clock_s"] = time.time() - t0
    fn = os.path.join(a.out, f"{a.date}_{a.cell}_{a.replicate}.json")
    json.dump(log, open(fn, "w"), indent=1, default=str); print("wrote", fn)

if __name__ == "__main__":
    main()
