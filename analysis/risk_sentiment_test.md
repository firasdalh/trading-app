# Risk-sentiment (risk-on/off) filter test

Risk gauge: **US500m** daily trend (EMA20 vs EMA50). Each trade classified as a risk-on or risk-off bet via the exposure-factor model, then checked against the regime at its date.

## Aligned with the risk regime vs fighting it

| group | stats |
|---|---|
| ALIGNED (bet agrees with regime) | n= 99  win=32.3%  exp=+0.071R  total=+7.1R |
| FIGHTING (bet against regime) | n= 43  win=41.9%  exp=+0.423R  total=+18.2R |
| neutral (no risk tilt / flat regime) | n= 28  win=28.6%  exp=+0.174R  total=+4.9R |

## Walk-forward (last 30% out-of-sample)

| window | ALIGNED | FIGHTING |
|---|---|---|
| in-sample | n= 77  win=32.5%  exp=+0.075R  total=+5.8R | n= 20  win=35.0%  exp=+0.250R  total=+5.0R |
| OUT-OF-SAMPLE | n= 22  win=31.8%  exp=+0.058R  total=+1.3R | n= 23  win=47.8%  exp=+0.573R  total=+13.2R |

## Verdict

- Aligned expectancy **+0.071R** vs fighting **+0.423R** (gap -0.352R).
- The edge does NOT hold in both windows -> not a reliable filter on this sample; do NOT wire it. (Small samples once split by regime + time.)

_Coarse gauge (one index, EMA trend); USD trades excluded from the tilt; small per-cell samples — read directionally._