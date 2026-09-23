"""Does odds.py's edge_r predict live outcomes? Measured with ONLY pre-trade candles."""
import sqlite3, pickle, sys, statistics as st, datetime as dt
from collections import defaultdict
sys.path.insert(0, '.')
from app.models.schemas import Candle
from app.agents.odds import barrier_odds

D = sys.argv[1] if len(sys.argv) > 1 else "."   # directory holding bars.pkl
data = pickle.load(open(D + "/bars.pkl", "rb"))

def hist(sym, tf, before_ts):
    rows = data["bars"].get((sym, tf), [])
    out = [r for r in rows if r[0] < before_ts]
    return [Candle(ts=dt.datetime.fromtimestamp(r[0], tz=dt.timezone.utc), open=r[1], high=r[2],
                   low=r[3], close=r[4], volume=r[5]) for r in out[-2000:]]

c = sqlite3.connect('trading.sqlite3')
rows = list(c.execute('''select symbol,direction,entry_price,stop_loss,take_profit,realized_pnl,
 risk_amount,opened_at,source,confidence from positions where status="closed" and realized_pnl is not null
 and opened_at>="2026-07-08" and stop_loss is not null and take_profit is not null and risk_amount>0'''))

buckets = defaultdict(list)
pairs = []
skipped = 0
for sym, d, e, sl, tp, pnl, ra, oa, src, conf in rows:
    ts = dt.datetime.fromisoformat(oa).replace(tzinfo=dt.timezone.utc).timestamp()
    cd = hist(sym, "1h", ts)
    if len(cd) < 300:
        skipped += 1; continue
    o = barrier_odds(cd, direction=d, entry=e, stop=sl, target=tp)
    if o is None:
        skipped += 1; continue
    R = pnl / ra
    pairs.append((o["edge_r"], R, conf, src))
    ed = o["edge_r"]
    k = '<-0.20' if ed < -0.20 else '-0.20..-0.05' if ed < -0.05 else '-0.05..+0.10' if ed < 0.10 else '+0.10..+0.30' if ed < 0.30 else '>=+0.30'
    buckets[k].append(R)

print(f"matched {len(pairs)} live trades (skipped {skipped})\n")
print("LIVE outcome bucketed by odds.py edge_r (computed on PRE-TRADE candles only):")
for k in ['<-0.20', '-0.20..-0.05', '-0.05..+0.10', '+0.10..+0.30', '>=+0.30']:
    if k in buckets:
        v = buckets[k]
        print(f"  edge_r {k:14} n={len(v):4} avgR={st.mean(v):+.3f} totR={sum(v):+7.1f} win={100*sum(1 for x in v if x>0)/len(v):4.0f}%")

import math
xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
n = len(xs); mx, my = st.mean(xs), st.mean(ys)
cov = sum((x-mx)*(y-my) for x, y in zip(xs, ys))/n
corr = cov/(st.pstdev(xs)*st.pstdev(ys))
t = corr*math.sqrt((n-2)/(1-corr**2))
print(f"\ncorr(edge_r, realised R) = {corr:+.4f}  n={n}  t={t:+.2f}")
cs = [p[2] for p in pairs if p[2]]
print(f"corr(confidence, realised R) on the same sample = ", end="")
mx2 = st.mean(cs); ys2 = [p[1] for p in pairs if p[2]]
cov2 = sum((x-mx2)*(y-st.mean(ys2)) for x, y in zip(cs, ys2))/len(cs)
c2 = cov2/(st.pstdev(cs)*st.pstdev(ys2))
print(f"{c2:+.4f}  t={c2*math.sqrt((len(cs)-2)/(1-c2**2)):+.2f}")

# what if we'd gated on edge_r >= 0
for thr in (-0.1, 0.0, 0.1, 0.2):
    keep = [y for x, y in zip(xs, ys) if x >= thr]
    drop = [y for x, y in zip(xs, ys) if x < thr]
    if keep and drop:
        print(f"  gate edge_r>={thr:+.2f}: kept n={len(keep):4} avgR={st.mean(keep):+.3f} totR={sum(keep):+7.1f} | "
              f"dropped n={len(drop):4} avgR={st.mean(drop):+.3f} totR={sum(drop):+7.1f}")
