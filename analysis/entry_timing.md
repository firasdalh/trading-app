# Task 3 — Entry timing relative to the trend

Sample: **170 entries** — all fire inside an ADX>=25 run by construction (trend_only mode only trades a confirmed trend). Percentiles are the fraction of the completed trend run already elapsed at the moment of entry. The run is defined from the ADX cross of 25 to its drop back below, so the 'price move' below is measured from the CONFIRMATION bar, not the true swing origin (which usually starts earlier, before ADX confirms).

## By TIME (bars elapsed / total run length)

- median **16%** of the trend already elapsed at entry
- p10 0% · p25 0% · p50 16% · p75 47% · p90 82%
- mean 28%

Distribution:
     0-20%:   89 ####################
    20-40%:   27 ######
    40-60%:   19 ####
    60-80%:   16 ###
   80-101%:   19 ####

## By PRICE (move captured before entry / total run move)

- median **7%** of the trend's price move already happened before entry
- p10 0% · p25 0% · p50 7% · p75 75% · p90 100%

## Conclusion

Entries are typically **EARLY** in the ADX-confirmed run: median **~16%** of the run's duration elapsed at entry, and a median of only ~7% of the run's price move happened before entry. So once ADX confirms the trend, the funnel enters promptly rather than chasing an exhausted move.
- BUT the distribution is right-skewed: **~21%** of entries still fire in the back half (>=60%) of the run — a 'chase' tail. This is consistent with, and the reason for, the confidence formula's anti-chase penalty (entries far from EMA20 value are down-weighted).
- Because the run is measured from the ADX cross (a lagging gate), the TRUE swing usually began before confirmation — so relative to the whole price swing, entries are later than the 7% figure suggests. The honest read: **early within the *confirmed* trend, mid-way within the *whole* move.**

_Caveat: 'total run length/move' is known only in hindsight (the run had to finish to be measured); at entry time the system cannot know how much trend remains. This measures historical positioning, not a usable real-time signal._