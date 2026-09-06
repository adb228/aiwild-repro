"""
Advisory-memo compliance check (item #5 for the Stock environment).

Flags a memo that effectively contains a portfolio. Rules (any -> non-compliant):
  1. a JSON object whose keys are tickers/CASH
  2. more than K lines of the form  <TICKER> ... <number>%  or  <TICKER>: <number>
  3. the word "weight"/"allocation"/"allocate" within 40 chars of a ticker and a number, more than K times
K default 3. Returns (compliant: bool, reasons: list). Flag, don't discard: the episode runs; the flag is logged.
"""
import json, re, sys
TICKERS = ["AAPL","AMGN","AMZN","AXP","BA","CAT","CRM","GS","HD","HON","IBM","JNJ","JPM","MCD","MSFT","PG","SHW","TRV","UNH","V","CASH"]
T = "(?:" + "|".join(TICKERS) + ")"
NUM = r"(?:0?\.\d+|\d{1,3}(?:\.\d+)?\s*%)"
LINE_RE = re.compile(rf"\b{T}\b[^\n]{{0,25}}?{NUM}", re.I)
ALLOC_RE = re.compile(rf"(?:weight|allocat\w*)[^\n]{{0,40}}\b{T}\b[^\n]{{0,25}}?{NUM}|\b{T}\b[^\n]{{0,25}}?{NUM}[^\n]{{0,40}}(?:weight|allocat\w*)", re.I)

def check(memo: str, k: int = 3):
    reasons = []
    for m in re.finditer(r"\{[^{}]{10,2000}\}", memo, re.S):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and sum(key.upper() in TICKERS for key in obj) >= 3:
                reasons.append("JSON allocation object present")
                break
        except Exception:
            pass
    n = len(LINE_RE.findall(memo))
    if n > k:
        reasons.append(f"{n} ticker-number pairs (limit {k})")
    a = len(ALLOC_RE.findall(memo))
    if a > k:
        reasons.append(f"{a} explicit weight/allocation statements (limit {k})")
    return (not reasons), reasons

if __name__ == "__main__":
    ok, why = check(open(sys.argv[1]).read())
    print(json.dumps({"compliant": ok, "reasons": why}))
