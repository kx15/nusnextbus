"""Tests for the deduplicated arrivals keyboard helper."""
import bot


def test_arrival_keyboard_with_back(monkeypatch):
    monkeypatch.setattr(bot, "is_favourite", lambda u, s: False)
    rows = bot._arrival_keyboard(1, "CLB").inline_keyboard
    assert len(rows) == 2
    assert rows[0][0].text == "🔄 Refresh"
    assert rows[0][0].callback_data == "refresh:CLB"
    assert rows[0][1].text == "⭐ Add Favourite"
    assert rows[0][1].callback_data == "fav:CLB"
    assert rows[1][0].callback_data == "page:0"


def test_arrival_keyboard_without_back(monkeypatch):
    monkeypatch.setattr(bot, "is_favourite", lambda u, s: True)
    rows = bot._arrival_keyboard(1, "CLB", with_back=False).inline_keyboard
    assert len(rows) == 1
    assert rows[0][1].text == "★ Remove Favourite"
