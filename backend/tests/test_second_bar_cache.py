"""Second bars kept between reads (2026-11-22): the same bars as building them from all the ticks every time,
while a read fetches only the ticks from the first bar not yet final."""
from __future__ import annotations

import random

from app.brokers import mt5_adapter as M
from app.brokers.mt5_adapter import Mt5BrokerAdapter, _bars_from_ticks

BASE = 1_800_000_000


class LiveFake:
    """A terminal whose clock moves: ticks (with milliseconds) show up once the clock passes them, M1 bars are
    the minutes that have a tick, as in MT5."""
    TIMEFRAME_M1 = 1
    COPY_TICKS_ALL = 1

    def __init__(self, ticks):
        self.ticks = ticks
        self.now_ms = 0
        self.tick_reads = []

    def symbol_info(self, sym):
        return object()

    def symbol_select(self, sym, enable=True):
        return True

    def _seen(self):
        return [t for t in self.ticks if t["time_msc"] <= self.now_ms]

    def copy_rates_from_pos(self, sym, tf, start, count):
        minutes = sorted({t["time"] - t["time"] % 60 for t in self._seen()})
        return [{"time": m} for m in minutes[-count:]]

    def copy_ticks_range(self, sym, date_from, date_to, flags):
        out = [t for t in self._seen() if date_from <= t["time"] <= date_to]
        self.tick_reads.append(len(out))
        return out


def _adapter(fake):
    a = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    a._mt5 = fake
    a._paper = True
    return a


def _reference(fake, seconds, limit):
    """What the uncached read returned: every tick of the window's M1 bars, grouped, the last ``limit``."""
    minutes = -(-limit * seconds // 60) + 1
    rates = fake.copy_rates_from_pos("X", 1, 0, minutes)
    if not rates:
        return []
    ticks = [t for t in fake._seen() if rates[0]["time"] <= t["time"] <= rates[-1]["time"] + 120]
    bars = _bars_from_ticks([t["time"] for t in ticks], [t["bid"] for t in ticks], seconds)[-limit:]
    return [(b[0], b[1], b[2], b[3], b[4], float(b[5])) for b in bars]


def _got(series):
    return [(int(c.ts.timestamp()), c.open, c.high, c.low, c.close, c.volume) for c in series.candles]


def _ticks(seed, hours=5, gap_after_h=3, gap_s=2 * 86400):
    """Ticks with busy and quiet stretches, several in one second, some at a bar's first second, a zero bid,
    and a weekend-long gap."""
    rnd = random.Random(seed)
    out, ms, p = [], BASE * 1000, 1.1
    end = (BASE + hours * 3600) * 1000
    while ms < end:
        ms += rnd.choice([30, 120, 400, 900, 2500, 7000, 40000])
        p = round(p + rnd.uniform(-0.0004, 0.0004), 5)
        out.append({"time": ms // 1000, "time_msc": ms, "bid": 0.0 if rnd.random() < 0.002 else p})
    if gap_after_h:
        cut = (BASE + gap_after_h * 3600) * 1000
        out = [t if t["time_msc"] < cut else {**t, "time_msc": t["time_msc"] + gap_s * 1000,
                                              "time": (t["time_msc"] + gap_s * 1000) // 1000} for t in out]
    return out


def test_kept_bars_equal_a_full_rebuild_through_time():
    for seed in (1, 2):
        M._SEC_CACHE.clear()
        fake = LiveFake(_ticks(seed, hours=3, gap_after_h=2))
        a = _adapter(fake)
        rnd = random.Random(seed + 100)
        fake.now_ms = (BASE + 1800) * 1000
        last = fake.ticks[-1]["time_msc"]
        times = [t["time_msc"] for t in fake.ticks]
        checks = 0
        while fake.now_ms < last + 5000:
            # irregular steps, now and then a long one (a read after the desk was closed for a while); across
            # the weekend gap the clock jumps to a few seconds before the next tick
            fake.now_ms += rnd.choice([350, 1000, 1700, 5000, 12000, 31000, 900_000 if rnd.random() < 0.01 else 4000])
            nxt = next((t for t in times if t > fake.now_ms), None)
            if nxt is not None and nxt - fake.now_ms > 3_600_000 and rnd.random() < 0.9:
                fake.now_ms = nxt - rnd.choice([0, 500, 3000])
            tf, sec = rnd.choice((("5s", 5), ("10s", 10), ("30s", 30)))
            limit = rnd.choice((500, 3, 400, 500))
            assert _got(a.get_ohlcv("X", tf, limit=limit)) == _reference(fake, sec, limit), (seed, tf, limit, fake.now_ms)
            checks += 1
        assert checks > 500


def test_a_read_after_the_first_fetches_only_the_new_ticks():
    M._SEC_CACHE.clear()
    fake = LiveFake(_ticks(7, gap_after_h=0))
    a = _adapter(fake)
    fake.now_ms = (BASE + 4 * 3600) * 1000
    a.get_ohlcv("X", "30s", limit=500)
    first = fake.tick_reads[-1]
    fake.now_ms += 5000
    a.get_ohlcv("X", "30s", limit=500)
    assert first > 1000 and fake.tick_reads[-1] < first / 20


def test_a_short_read_does_not_cut_what_a_long_one_needs():
    M._SEC_CACHE.clear()
    fake = LiveFake(_ticks(9, gap_after_h=0))
    a = _adapter(fake)
    fake.now_ms = (BASE + 4 * 3600) * 1000
    a.get_ohlcv("X", "5s", limit=500)
    full = fake.tick_reads[-1]
    for _ in range(5):
        fake.now_ms += 1000
        a.get_ohlcv("X", "5s", limit=3)        # the chart's refresh
    fake.now_ms += 1000
    assert _got(a.get_ohlcv("X", "5s", limit=500)) == _reference(fake, 5, 500)
    assert fake.tick_reads[-2] < full / 20       # no rebuild (the last read is the reference's own)


def test_a_failed_tick_read_returns_nothing_and_keeps_what_was_built():
    M._SEC_CACHE.clear()
    fake = LiveFake(_ticks(11, gap_after_h=0))
    a = _adapter(fake)
    fake.now_ms = (BASE + 3600) * 1000
    a.get_ohlcv("X", "10s", limit=100)
    real = fake.copy_ticks_range
    fake.copy_ticks_range = lambda *a_, **k: None
    assert a.get_ohlcv("X", "10s", limit=100).candles == []
    fake.copy_ticks_range = real
    fake.now_ms += 3000
    assert _got(a.get_ohlcv("X", "10s", limit=100)) == _reference(fake, 10, 100)


def test_the_cache_holds_a_bounded_number_of_keys():
    M._SEC_CACHE.clear()
    fake = LiveFake(_ticks(13, hours=1, gap_after_h=0))
    a = _adapter(fake)
    fake.now_ms = (BASE + 1800) * 1000
    for i in range(M._SEC_CACHE_MAX + 10):
        a._second_bars(f"S{i}", 5, 10)
    assert len(M._SEC_CACHE) == M._SEC_CACHE_MAX
