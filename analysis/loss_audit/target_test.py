"""Replay every live trade on real 15m candles at different fixed R targets (stop checked first)."""
import sqlite3, pickle, sys, statistics as st, datetime as dt
from collections import defaultdict
sys.path.insert(0, '.')
D = sys.argv[1] if len(sys.argv) > 1 else "."   # directory holding bars.pkl
data = pickle.load(open(D + "/bars.pkl", "rb"))
HORIZON = 96 * 4  # 96 1h-bars expressed in 15m bars

c = sqlite3.connect('trading.sqlite3')
rows = list(c.execute('''select symbol,direction,entry_price,stop_loss,realized_pnl,risk_amount,opened_at,source
 from positions where status="closed" and realized_pnl is not null and opened_at>="2026-07-08"
 and stop_loss is not null and risk_amount>0'''))

def bars_after(sym, ts):
    rs = data["bars"].get((sym, "15m"), [])
    return [r for r in rs if r[0] >= ts][:HORIZON]

res = defaultdict(list)
sess_res = defaultdict(lambda: defaultdict(list))
matched = 0
for sym, d, e, sl, pnl, ra, oa, src in rows:
    t = dt.datetime.fromisoformat(oa).replace(tzinfo=dt.timezone.utc)
    bs = bars_after(sym, t.timestamp())
    if len(bs) < 20 or not e or not sl:
        continue
    risk = abs(e - sl)
    if risk <= 0:
        continue
    matched += 1
    is_long = d == "long"
    h = 'overlap' if 12 <= t.hour < 16 else 'asia' if (t.hour >= 22 or t.hour < 6) else 'other'
    for tgt in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0):
        tp = e + tgt*risk if is_long else e - tgt*risk
        out = None
        for b in bs:
            hi, lo = b[2], b[3]
            hit_s = (lo <= sl) if is_long else (hi >= sl)
            hit_t = (hi >= tp) if is_long else (lo <= tp)
            if hit_s and hit_t:
                out = -1.0; break          # same bar: assume stop first (conservative)
            if hit_s:
                out = -1.0; break
            if hit_t:
                out = tgt; break
        if out is None:                     # time-stop at the horizon: mark to last close
            last = bs[-1][4]
            out = ((last - e) if is_long else (e - last)) / risk
        res[tgt].append(out)
        sess_res[h][tgt].append(out)

print(f"replayed {matched} live trades on real 15m candles (stop checked first, 96h horizon)\n")
print(f"{'target':>8}{'n':>6}{'expectancy':>13}{'win%':>8}{'total R':>10}")
for tgt in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0):
    v = res[tgt]
    print(f"{tgt:>7.1f}R{len(v):>6}{st.mean(v):>+12.3f}R{100*sum(1 for x in v if x > 0)/len(v):>7.0f}%{sum(v):>+9.1f}")

print("\nsame, split by ENTRY SESSION:")
for h in ('overlap', 'other', 'asia'):
    print(f"  --- {h} (n={len(sess_res[h][3.0])}) ---")
    for tgt in (1.5, 2.0, 3.0, 4.0):
        v = sess_res[h][tgt]
        if v:
            print(f"    {tgt:.1f}R  exp={st.mean(v):+.3f}R  win={100*sum(1 for x in v if x>0)/len(v):3.0f}%  tot={sum(v):+7.1f}")
