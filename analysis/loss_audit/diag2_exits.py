import sys, runpy
sys.argv = [sys.argv[0], sys.argv[1], sys.argv[2]]
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    g = runpy.run_path(sys.argv[0].replace("diag2_exits.py", "diag2_analyze.py"), run_name="lib")
tr, summ, replay = g["tr"], g["summ"], g["replay"]
def ex(title, pool=None, **kw):
    pool = tr if pool is None else pool
    print(f"   {title:52} {summ([(replay(t, **kw), t['entry_time']) for t in pool])}")
print("BASE plan"); ex("plan")
print("-- target plateau")
for tg in (2.5, 3.0, 3.5, 4.0): ex(f"fixed {tg}R", target=tg)
for fl in (2.5, 3.0):
    print(f"   max(plan,{fl}R)", end=""); 
    vals=[]
    for t in tr:
        vals.append((replay(t, target=max(t['planned_rr'], fl)), t['entry_time']))
    print(" "*36, summ(vals))
print("-- time-stop plateau (bar N close < m R -> exit)")
for n in (8, 10, 12, 14, 16, 20):
    for m in (0.0, 0.2, 0.3, 0.5):
        ex(f"bar {n} < {m}", time_n=n, time_min=m)
print("-- 4h ADX exhaustion filter plateau")
for lim in (35, 40, 45, 50):
    keep=[t for t in tr if (t['h4'].get('adx') or 0) < lim]; drop=[t for t in tr if (t['h4'].get('adx') or 0) >= lim]
    ex(f"keep 4h ADX < {lim}", pool=keep); ex(f"   dropped (>= {lim})", pool=drop)
print("-- combos")
keep=[t for t in tr if (t['h4'].get('adx') or 0) < 40]
ex("3R target + time stop bar12<0.3", target=3.0, time_n=12, time_min=0.3)
ex("plan + time stop bar12<0.3 + 4hADX<40", pool=keep, time_n=12, time_min=0.3)
ex("3R + time stop bar12<0.3 + 4hADX<40", pool=keep, target=3.0, time_n=12, time_min=0.3)
ex("3R + 4hADX<40", pool=keep, target=3.0)
