"""The aggressive flow in the learner (2026-11-21, the user: "add both: the flow on the pane, and let the learner watch
it"): each crypto trade's reading - the taker-buy share of the FLOW_BARS (5) finished 1m candles before its entry on Binance's
USDT perpetual, minus a half, from the trade's side - added to its fingerprint once, and the trades split by it like
any other part. Crypto only; watched only: no Apply button."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.agents.learning as L
from app.models.db import StrategyTradeFact
from tests.test_learning import _facts, _strategy

U = timezone.utc
AT = datetime(2026, 11, 18, 14, 7, 30, tzinfo=U)
AT_MS = int(AT.timestamp() * 1000)
N = L.FLOW_BARS


def _kl(start_ms, n, buy_share=0.6, vol=10.0):
    """n Binance 1m klines from start_ms, each with ``vol`` traded and ``buy_share`` of it taker buys."""
    return [[start_ms + 60_000 * i, "1", "1", "1", "1", str(vol), start_ms + 60_000 * i + 59_999, "0", 5,
             str(vol * buy_share), "0", "0"] for i in range(n)]


def test_the_symbol_maps_to_its_binance_perpetual():
    assert L.perp_of("BTCUSDm") == "BTCUSDT" and L.perp_of("ETHUSDm") == "ETHUSDT" and L.perp_of("BTCUSD") == "BTCUSDT"
    assert L.perp_of("US30m") is None and L.perp_of("") is None


def test_only_finished_candles_count_and_the_reading_is_signed_to_the_trade():
    first = (AT_MS // 60_000 - N) * 60_000                      # the N before 14:07 finished; 14:07 still forming
    rows = _kl(first - 600_000, 10, 0.0) + _kl(first, N, 0.6) + _kl(first + N * 60_000, 1, 0.0, 1000.0)
    assert L.flow_of(1, rows, AT_MS) == pytest.approx(0.1)
    assert L.flow_of(-1, rows, AT_MS) == pytest.approx(-0.1)   # buyers were the market's: against a sell
    assert L.flow_of(1, rows[10:10 + N - 1], AT_MS) is None    # fewer than N finished candles
    assert L.flow_of(1, _kl(first, N, 0.5, 0.0), AT_MS) is None    # no volume at all


def test_it_is_read_from_binance_with_the_trade_s_window():
    seen = []

    def fetch(url):
        seen.append(url)
        return _kl((AT_MS // 60_000 - N) * 60_000, N, 0.45)
    assert L.history_flow("BTCUSDm", 1, AT, fetch) == pytest.approx(-0.05)
    assert "symbol=BTCUSDT" in seen[0] and f"endTime={AT_MS}" in seen[0] and "interval=1m" in seen[0]
    assert L.history_flow("US30m", 1, AT, fetch) is None and len(seen) == 1     # not crypto: nothing read


def _fact(session, cfg, key, symbol="BTCUSDm", fp=None, opened=AT):
    row = StrategyTradeFact(key=key, strategy_id=cfg.id, symbol=symbol, side="long", opened_at=opened,
                            closed_at=opened + timedelta(minutes=20), pnl=5.0, r=0.2, entry=100.0, risk_dist=1.0,
                            fp=fp, fp_from="entry" if fp else None)
    session.add(row)
    return row


def test_each_crypto_fingerprint_gets_its_reading_once(db_session):
    cfg = _strategy(db_session)
    _fact(db_session, cfg, "a", fp={"v": 2})
    _fact(db_session, cfg, "b", fp={"v": 2, "flow": 0.03})                  # has it already
    _fact(db_session, cfg, "c", symbol="XAUUSDm", fp={"v": 2})              # gold: not crypto
    _fact(db_session, cfg, "d", fp=None)                                     # no fingerprint yet
    db_session.commit()
    calls = []

    def fetch(url):
        calls.append(url)
        return _kl((AT_MS // 60_000 - N) * 60_000, N, 0.55)
    assert L.fill_flow(db_session, cfg, AT + timedelta(hours=1), fetch=fetch) == 1
    fps = {r.key: r.fp for r in db_session.query(StrategyTradeFact).all()}
    assert fps["a"]["flow"] == pytest.approx(0.05) and fps["b"]["flow"] == 0.03
    assert "flow" not in fps["c"] and fps["d"] is None and len(calls) == 1
    assert L.fill_flow(db_session, cfg, AT + timedelta(hours=1), fetch=fetch) == 0   # read once


def test_a_failed_read_counts_a_try_and_ends_the_tick_s_reads(db_session):
    cfg = _strategy(db_session)
    for k in range(4):
        _fact(db_session, cfg, f"t{k}", fp={"v": 2}, opened=AT - timedelta(minutes=k))
    db_session.commit()
    calls = []

    def down(url):
        calls.append(url)
        raise OSError("binance down")
    assert L.fill_flow(db_session, cfg, AT, fetch=down) == 1 and len(calls) == 1
    tries = sorted((r.fp or {}).get("flow_tries", 0) for r in db_session.query(StrategyTradeFact).all())
    assert tries == [0, 0, 0, 1]
    for _ in range(4 * L.FLOW_TRIES):                                      # one read a tick while it fails
        L.fill_flow(db_session, cfg, AT, fetch=down)
    assert all((r.fp or {}).get("flow_tries") == L.FLOW_TRIES for r in db_session.query(StrategyTradeFact).all())
    assert L.fill_flow(db_session, cfg, AT, fetch=down) == 0                 # given up on all of them


def test_the_trades_split_by_it_and_it_has_nothing_to_apply():
    facts = _facts(key=lambda bad: {"flow": -0.04 if bad else 0.04})
    parts = {p["key"]: p for p in L.find_parts(facts)}
    weak = parts["flow:low"]
    assert weak["kind"] == "weak" and "AGAINST" in weak["label"] and weak["feature_name"].startswith("aggressive flow")
    assert parts["flow:high"]["kind"] == "sweet"
    assert L.action_for(weak, {"pairs": [], "sides": "both"}) is None
    mid = L.find_parts(_facts(key=lambda bad: {"flow": 0.0}))
    assert not any(p["feature"] == "flow" and p["bucket"] != "mid" for p in mid)


def test_a_trade_without_the_reading_is_left_out_of_the_part():
    facts = _facts(key=lambda bad: {"streak": 1})
    assert not any(p["feature"] == "flow" for p in L.find_parts(facts))
