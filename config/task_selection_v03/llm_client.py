"""
LLM client for the StockBench-derived runner (Anthropic SDK). Implements the two functions
run_stock_episode.py needs. Same loop for worker and parent; only the prompt and the tool
allow-list differ, exactly as the cell manifest in Appendix C.12 prescribes.

Env:
    ANTHROPIC_API_KEY   required
    PAPER_A_MODEL       model string, frozen for the whole study            [CHECK #1]
    PAPER_A_MAX_CALLS   max model calls per role per episode (default 8)    [CHECK #2]
    PAPER_A_MAX_TOKENS  max output tokens per call (default 4000)           [CHECK #2]
    STOCKBENCH_REPO     path to the stockbench checkout (for the tool)
    DECISION_DATE       set by the runner; the tool refuses later dates

pip install anthropic
"""
import json, os, re, subprocess, sys
import anthropic

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = json.load(open(os.path.join(HERE, "prompts_stock.json")))
MODEL = os.environ.get("PAPER_A_MODEL", "")
MAX_CALLS = int(os.environ.get("PAPER_A_MAX_CALLS", "8"))
MAX_TOKENS = int(os.environ.get("PAPER_A_MAX_TOKENS", "4000"))
TEMPERATURE = float(os.environ.get("PAPER_A_TEMPERATURE", "0"))
REPO = os.environ.get("STOCKBENCH_REPO", "stockbench")

TOOL = {
    "name": "backtest_trailing",
    "description": "Score a candidate allocation over the 20 trading days ending on the decision date "
                   "(buy at open of day 1, mark at close of the decision date). Checks arithmetic and recent "
                   "behaviour only; says nothing about the future.",
    "input_schema": {"type": "object",
                     "properties": {"allocation": {"type": "object", "description": "ticker -> weight; may include CASH; weights sum to 1"}},
                     "required": ["allocation"]},
}


def _run_tool(allocation, sandbox_dir):
    f = os.path.join(sandbox_dir, "candidate_for_backtest.json")
    json.dump(allocation, open(f, "w"))
    r = subprocess.run([sys.executable, os.path.join(HERE, "backtest_trailing.py"), "--repo", REPO,
                        "--date", os.environ["DECISION_DATE"], "--alloc", f],
                       capture_output=True, text=True, env=os.environ.copy())
    return (r.stdout or "") + (r.stderr or "")


def _last_json_object(text):
    objs = re.findall(r"\{[^{}]*\}", text, re.S)
    for o in reversed(objs):
        try:
            d = json.loads(o)
            if isinstance(d, dict):
                return d
        except Exception:
            continue
    return None


def _loop(system, user_text, tools_allowed, sandbox_dir):
    """One agent loop. Returns (final_text, stats)."""
    assert MODEL, "PAPER_A_MODEL not set"
    client = anthropic.Anthropic()
    msgs = [{"role": "user", "content": user_text}]
    stats = {"calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0}
    text_out = ""
    for _ in range(MAX_CALLS):
        kw = dict(model=MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE, system=system, messages=msgs)
        if tools_allowed:
            kw["tools"] = [TOOL]
        resp = client.messages.create(**kw)
        stats["calls"] += 1
        stats["tokens_in"] += resp.usage.input_tokens
        stats["tokens_out"] += resp.usage.output_tokens
        text_out = "".join(b.text for b in resp.content if b.type == "text")
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses or not tools_allowed:
            break
        msgs.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            stats["tool_calls"] += 1
            out = _run_tool(tu.input.get("allocation", {}), sandbox_dir)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out[:4000]})
        msgs.append({"role": "user", "content": results})
    return text_out, stats


def call_worker(cell, bundle_text, sandbox_dir):
    role, fb = cell[0], cell[1]
    system = PROMPTS["worker_common"] + PROMPTS["worker_" + role] + PROMPTS["feedback_" + fb]
    text, stats = _loop(system, bundle_text, tools_allowed=(fb == "1"), sandbox_dir=sandbox_dir)
    if role == "A":
        return {"memo": text, **stats}
    alloc = _last_json_object(text) or {}
    notes = text[: text.rfind("{")] if "{" in text else text
    return {"allocation": alloc, "notes": notes.strip()[-2000:], **stats}


def call_parent(bundle_text, handoff, sandbox_dir):
    user = bundle_text
    if handoff is None:
        user += "\n\n=== HANDOFF ===\nNone. You are working alone.\n"
    elif handoff["type"] == "memo":
        user += "\n\n=== HANDOFF: ASSISTANT MEMO ===\n" + handoff["text"] + "\n"
    else:
        user += ("\n\n=== HANDOFF: CANDIDATE PORTFOLIO ===\n" + json.dumps(handoff["allocation"], indent=1)
                 + "\nValidator: " + handoff.get("validator", "") + "\nAssistant notes: " + handoff.get("notes", "") + "\n")
    text, stats = _loop(PROMPTS["parent"], user, tools_allowed=True, sandbox_dir=sandbox_dir)
    stats["trailing_backtests"] = stats["tool_calls"]
    return {"allocation": _last_json_object(text) or {}, **stats}
