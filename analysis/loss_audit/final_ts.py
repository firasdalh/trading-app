import sys, runpy, io, contextlib
S = sys.argv[1]
def load(f):
    sys.argv = [S + "/diag2_analyze.py", S, f]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return runpy.run_path(S + "/diag2_analyze.py", run_name="lib")
for name, f in (("d2 (late-trend fix only)", S + "/d2.json"), ("d3 (+ 3R target floor)", S + "/d3.json")):
    g = load(f)
    tr, summ, replay = g["tr"], g["summ"], g["replay"]
    print(name)
    print("   as engine plans            ", summ([(replay(t), t["entry_time"]) for t in tr]))
    for n, m in ((10, 0.25), (12, 0.25), (14, 0.25), (12, 0.0)):
        print(f"   + time stop {n}h < {m}R      ", summ([(replay(t, time_n=n, time_min=m), t["entry_time"]) for t in tr]))
