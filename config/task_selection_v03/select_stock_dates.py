"""
StockBench-derived decision-date selection — Paper A (Production vs Advisory delegation).

Turns StockBench's continuous trading loop into independent snapshot tasks: each
task is one decision date t; the agent sees data through t only and outputs one
target portfolio; the portfolio is scored over the next H trading days against an
equal-weight benchmark. Dates are chosen by a pre-registered operational filter
and a deterministic spacing rule. No date is hand-picked.

Usage:
    git clone --depth 1 https://github.com/ChenYXxxx/stockbench
    python select_stock_dates.py --repo stockbench

Outputs:
    stock_pilot.json          -> 3 pilot dates
    stock_date_selection.json -> full record (filter, calendar, pilot, main, reserve, hash)

Data facts (checked on the bundled offline cache, 2026-09-02):
  * 20 DJIA symbols; daily bars 2024-12-13 .. 2025-07-31 (148 trading days). SPY has
    only 14 days and is NOT usable as a benchmark -> equal-weight basket instead.
  * news_by_day/<SYM>/<date>.json holds items with published_utc; no item is dated
    after its file date (0 of 1,069 checked) -> day-level cutoff is leak-free.
  * quarterly financials carry filing_date -> point-in-time cutoff by filing_date.
  * stock_indicators/<SYM>_<date>.json are dated -> use file date <= t.
  * news coverage is thin before Feb 2025; full (median 20/20 symbols) Mar..Jul.
"""

import argparse
import datetime as dt
import glob
import hashlib
import json
import os

H = 10             # evaluation horizon in trading days after t
LOOKBACK = 60      # trading days of history required before t (main)
LOOKBACK_PILOT = 40
MIN_NEWS_SYMS = 15 # symbols with >=1 news file in calendar window [t-2, t]
UNIVERSE_SIZE = 20


def trading_days(repo):
    return sorted({os.path.basename(f)[:-8] for f in glob.glob(f"{repo}/storage/parquet/AAPL/day/*.parquet")})


def symbols(repo):
    return sorted(s for s in os.listdir(f"{repo}/storage/parquet") if s != "SPY")


def news_syms(repo, syms, d):
    D = dt.date.fromisoformat(d)
    return sum(any(os.path.exists(f"{repo}/storage/cache/news_by_day/{s}/{(D - dt.timedelta(k)).isoformat()}.json")
                   for k in range(3)) for s in syms)


def eligible(repo, syms, days, i, lookback):
    d = days[i]
    checks = {
        "R1 history": i >= lookback,
        "R2 future": i + H < len(days),
        "R3 prices_all": all(os.path.exists(f"{repo}/storage/parquet/{s}/day/{d}.parquet") for s in syms),
        "R4 news_cover": news_syms(repo, syms, d) >= MIN_NEWS_SYMS,
    }
    fails = [k for k, v in checks.items() if not v]
    return (not fails), fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", default="stock_date_selection.json")
    ap.add_argument("--pilot-out", default="stock_pilot.json")
    args = ap.parse_args()

    days = trading_days(args.repo)
    syms = symbols(args.repo)
    assert len(syms) == UNIVERSE_SIZE, syms

    # ---- main: every eligible date, then take non-overlapping H-day windows greedily from the earliest
    elig, rej = [], {}
    for i, d in enumerate(days):
        ok, fails = eligible(args.repo, syms, days, i, LOOKBACK)
        (elig.append(d) if ok else rej.__setitem__(d, fails))
    main_dates, last = [], -10**9
    for d in elig:
        i = days.index(d)
        if i - last >= H:
            main_dates.append(d); last = i

    # ---- reserve: eligible dates at the midpoint of each main window (overlap by design; flagged)
    reserve = []
    for a, b in zip(main_dates, main_dates[1:]):
        ia, ib = days.index(a), days.index(b)
        mid = days[(ia + ib) // 2]
        if mid in elig and mid not in main_dates:
            reserve.append(mid)

    # ---- pilot: 3 dates eligible under the relaxed lookback whose windows end before the first main
    #      window; spaced 3 days apart (pilot windows may overlap each other: pilot is never analysed)
    first_main_i = days.index(main_dates[0])
    pilot, last = [], -10**9
    for i, d in enumerate(days):
        if i + H >= first_main_i:
            break
        ok, _ = eligible(args.repo, syms, days, i, LOOKBACK_PILOT)
        if ok and i - last >= 3:
            pilot.append(d); last = i
        if len(pilot) == 3:
            break

    filt = {"horizon_trading_days": H, "lookback_main": LOOKBACK, "lookback_pilot": LOOKBACK_PILOT,
            "min_news_symbols_t-2..t": MIN_NEWS_SYMS, "universe": syms, "benchmark": "equal-weight basket, rebalanced at t",
            "spacing": "non-overlapping H-day windows, greedy from earliest eligible date",
            "reserve": "midpoints of main windows; OVERLAP main windows -> block bootstrap if used"}
    out = {"dataset": "StockBench offline cache (ChenYXxxx/stockbench)", "calendar": {"first": days[0], "last": days[-1], "n": len(days)},
           "design": "A0/A1/P0/P1 on every date; one replicate; parent submits final allocation",
           "filter": filt, "eligible_dates": len(elig), "eligible_range": [elig[0], elig[-1]],
           "pilot": pilot, "main": main_dates, "reserve_in_order": reserve}
    out["selection_sha256"] = hashlib.sha256(json.dumps({k: out[k] for k in ("filter", "pilot", "main", "reserve_in_order")},
                                                        sort_keys=True).encode()).hexdigest()
    json.dump({"pilot": pilot}, open(args.pilot_out, "w"), indent=2)
    json.dump(out, open(args.out, "w"), indent=2)

    print(f"Trading days: {len(days)} ({days[0]} .. {days[-1]})   Eligible: {len(elig)} ({elig[0]} .. {elig[-1]})")
    print("PILOT  :", pilot)
    print("MAIN   :", main_dates, f"({len(main_dates)} non-overlapping {H}-day windows)")
    print("RESERVE:", reserve, "(overlapping; use only on infra failure, in order)")
    print("sha256 :", out["selection_sha256"])


if __name__ == "__main__":
    main()
