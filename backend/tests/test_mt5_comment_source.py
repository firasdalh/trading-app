"""The source written into an MT5 order comment reads back as the same source from deal history."""
from app.brokers.mt5_adapter import app_comment, source_from_comment


def test_round_trip():
    for src in ("hybrid", "manual", "armed_hybrid", "armed_manual", "auto_trade"):
        assert source_from_comment(app_comment(src)) == src


def test_comment_fits_mt5_cap():
    assert len(app_comment("manual_from_armed_hybrid")) <= 31


def test_manual_from_cut_by_the_cap_reads_manual():
    assert source_from_comment(app_comment("manual_from_armed_hybrid")) == "manual"


def test_legacy_app_comment_is_app():
    # Placed by the app before the comment carried the source: who opened it is not recoverable.
    assert source_from_comment("ai-trading-app") == "app"
    assert app_comment(None) == "ai-trading-app"


def test_untagged_is_manual():
    # The app tags every order it sends, so an untagged trade was placed in the terminal / phone.
    assert source_from_comment("") == "manual"
    assert source_from_comment(None) == "manual"
