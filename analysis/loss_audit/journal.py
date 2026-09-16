import sqlite3, statistics as st, json, datetime as dt, sys
from collections import defaultdict, Counter
c=sqlite3.connect('file:trading.sqlite3?mode=ro',uri=True)
def t(s): return dt.datetime.fromisoformat(s[:26])
def load(since='2026-07-08', until='2099-12-31'):
    pos=c.execute('''select id,opened_at,closed_at,symbol,direction,entry_price,stop_loss,take_profit,realized_pnl,risk_amount,source,qty,last_price,confidence from positions where status='closed' and realized_pnl is not null and closed_at>=? and closed_at<? ''',(since,until)).fetchall()
    orders=c.execute('select id,created_at,symbol,proposal_id,avg_fill_price from orders where status="filled"').fetchall()
    bys=defaultdict(list)
    for o in orders: bys[o[2]].append(o)
    props={r[0]:r for r in c.execute('select id,entry,stop_loss,take_profit,confidence,timeframe,source,rationale,created_at,reasoning from trade_proposals')}
    out=[]
    for p in pos:
        cands=[o for o in bys[p[3]] if o[3] and abs((t(o[1])-t(p[1])).total_seconds())<120]
        if not cands: continue
        o=min(cands,key=lambda o:abs((t(o[1])-t(p[1])).total_seconds()))
        pr=props[o[3]]
        entry=p[5]; sl0=pr[2]; tp0=pr[3]; ex=p[12]
        sgn=1 if p[4]=='long' else -1
        risk=abs(entry-sl0) if sl0 else 0
        d=dict(id=p[0],opened=p[1],closed=p[2],sym=p[3],dir=p[4],entry=entry,sl=sl0,tp=tp0,exit=ex,pnl=p[8],risk_amt=p[9],src=p[10],conf=p[13],tf=pr[5],rationale=pr[7],reasoning=pr[9],prop_entry=pr[1])
        d['R']=p[8]/p[9] if p[9] else None
        d['priceR']=sgn*(ex-entry)/risk if risk and ex else None
        d['plannedR']=abs(tp0-entry)/risk if risk and tp0 else None
        d['hold_h']=(t(p[2])-t(p[1])).total_seconds()/3600
        out.append(d)
    return out
if __name__=="__main__":
    since=sys.argv[1] if len(sys.argv)>1 else '2026-07-08'
    until=sys.argv[2] if len(sys.argv)>2 else '2099-12-31'
    out=load(since,until)
    print(len(out),'trades',since,'->',until)
    w=[d for d in out if d['pnl']>0]; l=[d for d in out if d['pnl']<=0]
    print('win%',round(100*len(w)/len(out)),'net$',round(sum(d['pnl'] for d in out)),'avgWinR',round(st.mean(d['R'] for d in w),2),'avgLossR',round(st.mean(d['R'] for d in l),2),'expR',round(st.mean(d['R'] for d in out),3))
    b=Counter()
    for d in w:
        if d['priceR'] is None or not d['plannedR']: b['?']+=1; continue
        f=d['priceR']/d['plannedR']
        b['TP' if f>=0.9 else 'mid' if f>=0.5 else 'small' if f>=0.15 else 'BE']+=1
    print('winners exit:',b)
    lb=Counter('full' if (d['priceR'] or -1)<=-0.9 else 'partial' for d in l); print('losers exit:',lb)
    for key in ['src','sym','dir','tf']:
        g=defaultdict(list)
        for d in out: g[d[key]].append(d)
        print('--- by',key)
        for k,v in sorted(g.items(), key=lambda kv: sum(x['pnl'] for x in kv[1])):
            print(f"  {str(k):16} n={len(v):3} win%={round(100*sum(x['pnl']>0 for x in v)/len(v)):3} net$={round(sum(x['pnl'] for x in v)):6} expR={round(st.mean(x['R'] for x in v),2):6}")
