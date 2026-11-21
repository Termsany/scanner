"""Telegram switches: storage format and how the bot's notes are sorted into kinds."""
from app.tg_prefs import DEFAULTS, dump, live_kind, merge


def test_nothing_saved_means_all_on_with_signals_from_settings():
    assert merge(None) == DEFAULTS
    assert merge(None, signals_default=False)["signals"] is False


def test_saved_switches_round_trip_and_fit_the_column():
    prefs = dict(DEFAULTS, live_skips=False, signals=False)
    raw = dump(prefs)
    assert merge(raw) == prefs and merge(raw, signals_default=True)["signals"] is False
    assert len(dump({k: False for k in DEFAULTS})) <= 160          # tracker_cursors.value is String(160)


def test_bot_notes_are_sorted_by_kind():
    tag = "🟢 LIVE"
    assert live_kind(f"{tag} ✅ Bought $X for 0.1 SOL ($10)") == "live_trades"
    assert live_kind(f"{tag} 💰 Sold 50% of $X for 0.2 SOL") == "live_trades"
    assert live_kind(f"{tag} ⏭ Skipped $X: price already +40%") == "live_skips"
    assert live_kind(f"{tag} 🛡 Skipped $X: scanner says rug") == "live_skips"
    assert live_kind(f"{tag} ⚠️ Sell $X failed, retrying") == "live_errors"
    assert live_kind(f"{tag} ❌ Buy $X failed") == "live_errors"
    assert live_kind(f"{tag} ⏸ W sold 20% of $X; half was already sold at 2x") == "live_info"
    assert live_kind(f"{tag} 🧾 Found sales you made by hand") == "live_info"
