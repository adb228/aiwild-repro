"""
StockBench-derived snapshot harness — Paper A.

Three functions, nothing else:
    build_bundle(repo, t)            -> point-in-time information set for decision date t (dict + text)
    validate_allocation(alloc, cap)  -> (ok, message)
    score(repo, t, alloc, H=10)      -> dict with portfolio return, EW return, excess, secondaries

Execution convention (StockBench): buy at OPEN of t+1, hold, mark at CLOSE of t+H. Zero costs.
Benchmark: equal-weight over the 20-name universe, same execution.

Point-in-time guarantees enforced in build_bundle:
    bars        : date <= t (file-level)
    news        : published_utc <= t 23:59:59 (item-level), from calendar days t-2..t
    financials  : filing_date <= t (record-level)
    indicators  : latest file with date <= t (file-level)
An assertion fails if any record after t would be emitted.

Run `python stockbench_snapshot.py --repo <stockbench dir> --selftest` to verify:
    (1) EW allocation scores excess == 0 on every main date
    (2) scoring is deterministic
    (3) no bundle contains any record dated after t
    (4) validator accepts/rejects correctly
"""

import argparse
import datetime as dt
import glob
import hashlib
import json
import os

import functools

import pandas as pd

UNIVERSE = ["AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "GS", "HD", "HON",
            "IBM", "JNJ", "JPM", "MCD", "MSFT", "PG", "SHW", "TRV", "UNH", "V"]
H_DEFAULT = 10
CAP_DEFAULT = 0.25
LOOKBACK_BARS = 60
NEWS_DAYS = 2
NEWS_TOP_K = 5


# ------------------------------------------------------------------ data --

def trading_days(repo):
    return sorted({os.path.basename(f)[:-8] for f in glob.glob(f"{repo}/storage/parquet/AAPL/day/*.parquet")})


@functools.lru_cache(maxsize=None)
def bar(repo, sym, d):
    f = f"{repo}/storage/parquet/{sym}/day/{d}.parquet"
    if not os.path.exists(f):
        return None
    r = pd.read_parquet(f).iloc[0]
    return {"date": str(r["date"])[:10], "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}


def bars_through(repo, sym, days, t, n):
    i = days.index(t)
    out = [bar(repo, sym, d) for d in days[max(0, i - n + 1): i + 1]]
    return [b for b in out if b]


def news_through(repo, sym, t, k=NEWS_TOP_K):
    T = dt.date.fromisoformat(t)
    cutoff = f"{t}T23:59:59"
    items = []
    for back in range(NEWS_DAYS, -1, -1):
        f = f"{repo}/storage/cache/news_by_day/{sym}/{(T - dt.timedelta(back)).isoformat()}.json"
        if os.path.exists(f):
            for it in json.load(open(f)).get("items", []):
                pub = it.get("published_utc", "")
                assert pub <= cutoff, f"lookahead: {sym} news {pub} > {t}"
                items.append({"published_utc": pub, "title": it.get("title", ""), "source": it.get("source", "")})
    items.sort(key=lambda x: x["published_utc"], reverse=True)
    return items[:k]


def latest_financials(repo, sym, t):
    f = f"{repo}/storage/cache/financials/{sym}.quarterly.json"
    if not os.path.exists(f):
        return None
    recs = json.load(open(f))
    recs = recs if isinstance(recs, list) else recs.get("results", [])
    ok = [r for r in recs if r.get("filing_date", "9999") <= t]
    if not ok:
        return None
    r = max(ok, key=lambda r: r["filing_date"])
    fin = r.get("financials", {})
    def g(section, key):
        try:
            return fin[section][key]["value"]
        except Exception:
            return None
    return {"fiscal_period": f"{r.get('fiscal_year')} {r.get('fiscal_period')}", "filing_date": r["filing_date"],
            "revenue": g("income_statement", "revenues"), "net_income": g("income_statement", "net_income_loss"),
            "eps_diluted": g("income_statement", "diluted_earnings_per_share")}


def latest_indicators(repo, sym, t):
    fs = sorted(f for f in glob.glob(f"{repo}/storage/cache/stock_indicators/{sym}_*.json")
                if os.path.basename(f)[len(sym) + 1:-5] <= t)
    if not fs:
        return None
    d = json.load(open(fs[-1]))
    return {"as_of": os.path.basename(fs[-1])[len(sym) + 1:-5],
            **{k: d.get(k) for k in ("market_cap", "pe_ratio", "dividend_yield", "week_52_high", "week_52_low")}}


def build_bundle(repo, t, H=H_DEFAULT, cap=CAP_DEFAULT):
    days = trading_days(repo)
    assert t in days, t
    b = {"decision_date": t, "horizon_trading_days": H, "execution": "buy at open of next trading day, mark at close after H trading days",
         "constraints": {"long_only": True, "weights_sum_to_1": True, "max_weight_per_name": cap, "cash_allowed": True},
         "universe": UNIVERSE, "symbols": {}}
    for s in UNIVERSE:
        bb = bars_through(repo, s, days, t, LOOKBACK_BARS)
        assert all(x["date"] <= t for x in bb)
        b["symbols"][s] = {"bars_last_%d" % LOOKBACK_BARS: bb, "close_7d": [x["close"] for x in bb[-7:]],
                           "indicators": latest_indicators(repo, s, t), "latest_filing": latest_financials(repo, s, t),
                           "news_top_k": news_through(repo, s, t)}
    b["bundle_sha256"] = hashlib.sha256(json.dumps(b, sort_keys=True, default=str).encode()).hexdigest()
    return b


def bundle_to_text(b):
    L = [f"DECISION DATE: {b['decision_date']}   HORIZON: {b['horizon_trading_days']} trading days",
         f"EXECUTION: {b['execution']}", f"CONSTRAINTS: {json.dumps(b['constraints'])}",
         f"UNIVERSE: {', '.join(b['universe'])}", ""]
    for s, d in b["symbols"].items():
        L.append(f"=== {s} ===")
        L.append("close_7d (oldest..newest): " + ", ".join(f"{c:.2f}" for c in d["close_7d"]))
        bb = d[f"bars_last_{LOOKBACK_BARS}"]
        if bb:
            L.append(f"60d: first close {bb[0]['close']:.2f} on {bb[0]['date']}; last close {bb[-1]['close']:.2f} on {bb[-1]['date']}; "
                     f"60d high {max(x['high'] for x in bb):.2f}; 60d low {min(x['low'] for x in bb):.2f}")
        if d["indicators"]:
            L.append("indicators: " + json.dumps(d["indicators"]))
        if d["latest_filing"]:
            L.append("latest filing: " + json.dumps(d["latest_filing"]))
        for n in d["news_top_k"]:
            L.append(f"  news [{n['published_utc'][:16]}] {n['title']}")
        L.append("")
    return "\n".join(L)


# ------------------------------------------------------------- validate --

def validate_allocation(alloc, cap=CAP_DEFAULT):
    if not isinstance(alloc, dict) or not alloc:
        return False, "not a non-empty object"
    bad = [k for k in alloc if k not in UNIVERSE and k != "CASH"]
    if bad:
        return False, f"unknown tickers: {bad}"
    try:
        vals = {k: float(v) for k, v in alloc.items()}
    except Exception:
        return False, "non-numeric weight"
    neg = [k for k, v in vals.items() if v < 0]
    if neg:
        return False, f"negative weights: {neg}"
    over = [k for k, v in vals.items() if k != "CASH" and v > cap + 1e-9]
    if over:
        return False, f"weights above cap {cap}: {over}"
    s = sum(vals.values())
    if not (0.999 <= s <= 1.001):
        return False, f"weights sum to {s:.4f}, not 1"
    return True, "ok"


# ---------------------------------------------------------------- score --

def score(repo, t, alloc, H=H_DEFAULT):
    days = trading_days(repo)
    i = days.index(t)
    assert i + H < len(days), "insufficient future data"
    t1, tH = days[i + 1], days[i + H]
    rets = {}
    for s in UNIVERSE:
        o, c = bar(repo, s, t1), bar(repo, s, tH)
        assert o and c, f"missing bar {s}"
        rets[s] = c["close"] / o["open"] - 1.0
    w = {k: float(v) for k, v in alloc.items()}
    port = sum(w.get(s, 0.0) * rets[s] for s in UNIVERSE)          # CASH contributes 0
    ew = sum(rets[s] for s in UNIVERSE) / len(UNIVERSE)
    # daily path for drawdown / worst day
    path = []
    for d in days[i + 1: i + H + 1]:
        v = 0.0
        for s in UNIVERSE:
            b0, bd = bar(repo, s, t1), bar(repo, s, d)
            v += w.get(s, 0.0) * (bd["close"] / b0["open"])
        v += w.get("CASH", 0.0)
        path.append(v)
    peak, mdd = -1e9, 0.0
    for v in path:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1.0)
    worst_day = min((path[j] / path[j - 1] - 1.0) for j in range(1, len(path))) if len(path) > 1 else 0.0
    hhi = sum(v * v for k, v in w.items() if k != "CASH")
    l1_vs_ew = sum(abs(w.get(s, 0.0) - 1.0 / len(UNIVERSE)) for s in UNIVERSE) + w.get("CASH", 0.0)
    return {"t": t, "entry": t1, "exit": tH, "portfolio_return": port, "ew_return": ew, "excess_return": port - ew,
            "beat_benchmark": port > ew, "max_drawdown": mdd, "worst_day": worst_day, "hhi": hhi,
            "n_positions": sum(1 for k, v in w.items() if k != "CASH" and v > 0), "l1_vs_ew": l1_vs_ew}


# ------------------------------------------------------------- selftest --

def selftest(repo, dates):
    ew = {s: 1.0 / len(UNIVERSE) for s in UNIVERSE}
    ok, msg = validate_allocation(ew); assert ok, msg
    for bad, why in [({"AAPL": 1.2, "CASH": -0.2}, "negative"), ({"AAPL": 0.5, "MSFT": 0.5}, "cap"),
                     ({"AAPL": 0.5}, "sum"), ({"XYZ": 1.0}, "ticker")]:
        assert not validate_allocation(bad)[0], why
    print("validator: ok")
    for t in dates:
        b = build_bundle(repo, t)
        n_news = sum(len(d["news_top_k"]) for d in b["symbols"].values())
        s1, s2 = score(repo, t, ew), score(repo, t, ew)
        assert s1 == s2, "non-deterministic"
        assert abs(s1["excess_return"]) < 1e-12, "EW excess != 0"
        print(f"{t}: bundle ok ({n_news} news items, sha {b['bundle_sha256'][:8]}) | EW window return {s1['ew_return']:+.2%} "
              f"| entry {s1['entry']} exit {s1['exit']} | excess(EW)={s1['excess_return']:+.1e}")
    print("selftest: all passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dates", default=None, help="JSON file with 'main' (and 'pilot') date lists")
    ap.add_argument("--bundle", default=None, help="print the text bundle for this date")
    a = ap.parse_args()
    if a.bundle:
        print(bundle_to_text(build_bundle(a.repo, a.bundle)))
    if a.selftest:
        sel = json.load(open(a.dates)) if a.dates else {"main": [], "pilot": []}
        selftest(a.repo, sel.get("pilot", []) + sel.get("main", []))
