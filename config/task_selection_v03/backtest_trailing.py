"""
+F tool for the StockBench-derived environment.

Scores a candidate allocation over the TRAILING window ending at the decision date t
(buy at open of t-19, mark at close of t) using the same execution semantics as the
real scorer. This is the only "test" the environment admits before handoff: it
verifies arithmetic and recent behaviour, never the future.

Usage (inside the +F sandbox):
    python backtest_trailing.py --repo <stockbench> --date 2025-04-09 --alloc candidate.json
Prints portfolio return, EW return, excess, drawdown, worst day over the trailing 20 days.
The tool refuses any date after the decision date baked into the sandbox.
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stockbench_snapshot as h

TRAIL = 20

def trailing_score(repo, t, alloc):
    days = h.trading_days(repo)
    i = days.index(t)
    start = days[i - TRAIL]                 # decision date for the trailing window
    return h.score(repo, start, alloc, H=TRAIL)   # entry = open(start+1), exit = close(start+TRAIL) == close(t)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True); ap.add_argument("--date", required=True); ap.add_argument("--alloc", required=True)
    a = ap.parse_args()
    locked = os.environ.get("DECISION_DATE")
    if locked and a.date > locked:
        sys.exit(f"refused: {a.date} is after the decision date {locked}")
    alloc = json.load(open(a.alloc))
    ok, msg = h.validate_allocation(alloc)
    if not ok:
        sys.exit(f"invalid allocation: {msg}")
    r = trailing_score(a.repo, a.date, alloc)
    print(json.dumps({k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()}, indent=1))
