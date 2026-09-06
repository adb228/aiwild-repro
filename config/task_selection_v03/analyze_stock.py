"""
Paired analysis of StockBench-derived episode logs.
    python analyze_stock.py logs/main
Prints per-date excess by cell, paired differences, delta, bootstrap CIs, and secondaries.
"""
import glob, json, os, random, statistics, sys

d = sys.argv[1] if len(sys.argv) > 1 else "logs/main"
logs = [json.load(open(f)) for f in glob.glob(os.path.join(d, "*.json"))]
by = {}
for L in logs:
    fs = (L.get("parent") or {}).get("final_score")
    if fs:
        by.setdefault(L["date"], {})[L["cell"]] = L
dates = sorted(by)
cells = ["A0", "A1", "P0", "P1", "SOLO"]

def ex(date, cell):
    L = by[date].get(cell); return L["parent"]["final_score"]["excess_return"] if L else None

print(f"{'date':<12}" + "".join(f"{c:>9}" for c in cells) + f"{'P1-A1':>9}{'P0-A0':>9}{'delta':>9}")
rows = []
for t in dates:
    e = {c: ex(t, c) for c in cells}
    p1a1 = e["P1"] - e["A1"] if None not in (e["P1"], e["A1"]) else None
    p0a0 = e["P0"] - e["A0"] if None not in (e["P0"], e["A0"]) else None
    delta = p1a1 - p0a0 if None not in (p1a1, p0a0) else None
    rows.append((p1a1, p0a0, delta))
    fmt = lambda v: f"{v:>+9.2%}" if v is not None else f"{'-':>9}"
    print(f"{t:<12}" + "".join(fmt(e[c]) for c in cells) + fmt(p1a1) + fmt(p0a0) + fmt(delta))

def boot(vals, n=5000, seed=0):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2: return None
    rng = random.Random(seed); ms = []
    for _ in range(n):
        s = [rng.choice(vals) for _ in vals]; ms.append(statistics.mean(s))
    ms.sort(); return statistics.mean(vals), statistics.median(vals), ms[int(0.025 * n)], ms[int(0.975 * n)]

print()
for name, i in (("P1-A1 (production effect, +F)", 0), ("P0-A0 (production effect, -F)", 1), ("delta = (P1-A1)-(P0-A0)", 2)):
    b = boot([r[i] for r in rows])
    if b: print(f"{name:<34} mean {b[0]:+.2%}  median {b[1]:+.2%}  95% paired-bootstrap CI [{b[2]:+.2%}, {b[3]:+.2%}]  n={sum(r[i] is not None for r in rows)}")

print("\nSecondaries (P cells): L1 worker->final, accepted unchanged, delta_parent, parent backtests, memo/candidate compliance")
for t in dates:
    for c in ("P0", "P1"):
        L = by[t].get(c)
        if L:
            P = L["parent"]; C = L["compliance"]
            print(f"  {t} {c}: L1={P.get('l1_worker_to_final', float('nan')):.3f} unchanged={P.get('accepted_unchanged')} "
                  f"dParent={P.get('delta_parent_excess', float('nan')):+.2%} backtests={P.get('trailing_backtests')} candValid={C.get('candidate_valid')}")
    for c in ("A0", "A1"):
        L = by[t].get(c)
        if L: print(f"  {t} {c}: memo_ok={L['compliance'].get('memo_ok')} {L['compliance'].get('memo_reasons')}")
