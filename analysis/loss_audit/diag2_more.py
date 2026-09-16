import sys, runpy, io, contextlib, json, pickle, statistics as st
S, F = sys.argv[1], sys.argv[2]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    g = runpy.run_path(sys.argv[0].replace("diag2_more.py", "diag2_analyze.py"), run_name="lib")
tr, summ, replay, cost = g["tr"], g["summ"], g["replay"], g["cost"]
def flat_band(t, n=12, band=0.5, target=None):
    """The advisor's existing rule: at bar n, close only if |R| < band."""
    tgt = t["planned_rr"] if target is None else target
    for j,(fav,adv,close) in enumerate(t["fwd"][:96], start=1):
        if -adv <= -1.0: return -1.0 - t["cost"]
        if fav >= tgt: return tgt - t["cost"]
        if j == n and abs(close) < band: return close - t["cost"]
    return (t["fwd"][-1][2] if t["fwd"] else 0) - t["cost"]
print("advisor flat-band rule |R|<0.5 at bar 12, plan target:", summ([(flat_band(t), t['entry_time']) for t in tr]))
print("new rule R<+0.25 at bar 12, plan target:           ", summ([(replay(t, time_n=12, time_min=0.25), t['entry_time']) for t in tr]))
print("max(plan,3R) + R<+0.25 at bar 12:                  ", summ([(replay(t, target=max(t['planned_rr'],3.0), time_n=12, time_min=0.25), t['entry_time']) for t in tr]))
print("max(plan,3R) alone:                                ", summ([(replay(t, target=max(t['planned_rr'],3.0)), t['entry_time']) for t in tr]))
# below-gate trades with a fresh 4h leg
allt = json.load(open(F))
spec = pickle.load(open(S + "/bars.pkl", "rb"))["spec"]
def age4(t):
    h=t['h4']; L=t['direction']=='long'
    if not h.get('supertrend_dir') or (h['supertrend_dir']>0)!=L: return None
    return h.get('supertrend_bars_since_flip')
below=[t for t in allt if t['strategy']=='trend' and t['conf']<=0.67]
for t in below: t['net']=t['r']-cost(t)
fresh=[t for t in below if (age4(t) is not None and age4(t)<=6)]
print("below-gate trend trades:", summ([(t['net'],t['entry_time']) for t in below]))
print("  ...with fresh 4h leg<=6:", summ([(t['net'],t['entry_time']) for t in fresh]))
print("  ...conf 0.55-0.67 & fresh:", summ([(t['net'],t['entry_time']) for t in fresh if t['conf']>=0.55]))
gated=[t for t in tr]
for lim in (4,6,8,12):
    f=[t for t in gated if age4(t) is not None and age4(t)<=lim]
    print(f"  gated with 4h leg <= {lim:2}:", summ([(t['net'],t['entry_time']) for t in f]))
