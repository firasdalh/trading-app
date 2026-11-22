"""The journal book shared by the Journal's cards for a few seconds (2026-11-22): built once, each caller gets its own
copies of the trades, a journal reset drops it."""
from __future__ import annotations

from datetime import datetime, timezone

import app.api.journal_routes as jr
from app.models.schemas import PositionView


def _trade(i):
    return PositionView(id=i, symbol="EURUSDm", asset_class="forex", direction="long", qty=0.1, entry_price=1.1,
                        status="closed", realized_pnl=float(i), closed_at=datetime(2026, 11, 20, tzinfo=timezone.utc))


def test_built_once_for_the_cards_that_ask_together(db_session, monkeypatch):
    built = []
    monkeypatch.setattr(jr, "_build_book", lambda session: built.append(1) or [(_trade(1), None), (_trade(2), None)])
    a = jr._journal_book(db_session)
    b = jr._journal_book(db_session)
    assert len(built) == 1 and [v.id for v, _ in b] == [1, 2]
    a[0][0].source = "changed by one card"           # each caller has its own copies
    assert jr._journal_book(db_session)[0][0].source is None


def test_it_is_rebuilt_after_the_ttl_and_after_a_reset(db_session, monkeypatch):
    built = []
    monkeypatch.setattr(jr, "_build_book", lambda session: built.append(1) or [(_trade(1), None)])
    jr._journal_book(db_session)
    jr._BOOK["at"] -= jr._BOOK_TTL_S + 1
    jr._journal_book(db_session)
    assert len(built) == 2
    jr._forget_book()
    jr._journal_book(db_session)
    assert len(built) == 3
