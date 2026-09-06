# StockBench-derived environment — run in five commands

```bash
git clone --depth 1 https://github.com/ChenYXxxx/stockbench
pip install pandas pyarrow anthropic
export STOCKBENCH_REPO=stockbench ANTHROPIC_API_KEY  PAPER_A_MODEL=<frozen model string>
python stockbench_snapshot.py --repo $STOCKBENCH_REPO --selftest --dates stock_date_selection.json   # must print "selftest: all passed"
python run_stock_episode.py --repo $STOCKBENCH_REPO --date 2025-02-26 --cell P1 --out logs/pilot     # one pilot episode; read the log
```

Then all cells on the pilot date, then the main matrix:
```bash
for c in A0 A1 P0 P1 SOLO; do python run_stock_episode.py --repo $STOCKBENCH_REPO --date 2025-02-26 --cell $c --out logs/pilot; done
for d in $(python -c "import json;print(' '.join(json.load(open('stock_date_selection.json'))['main']))"); do
  for c in A0 A1 P0 P1; do python run_stock_episode.py --repo $STOCKBENCH_REPO --date $d --cell $c --out logs/main & done
done; wait
python analyze_stock.py logs/main
```

**Before any episode:** confirm PAPER_A_MODEL's training cutoff is before 2025-03-26 (Appendix C.2). If not, stop.

Files must sit in one directory: `stockbench_snapshot.py`, `backtest_trailing.py`, `check_memo_compliance.py`,
`prompts_stock.json`, `llm_client.py`, `run_stock_episode.py`, `analyze_stock.py`, `stock_date_selection.json`.
Budget knobs: `PAPER_A_MAX_CALLS`, `PAPER_A_MAX_TOKENS`, `PAPER_A_TEMPERATURE` (env) — set from pilot, then freeze.
