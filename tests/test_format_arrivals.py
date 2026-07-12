"""Tests for the /arrivals message formatting (multi-vehicle merge)."""
import bot
from api import BusStopArrivals, ShuttleTiming


def _arrivals(timings):
    return BusStopArrivals("CLB", "Central Library", "2026-07-12 12:00:00", timings)


def test_merges_second_vehicle_as_next_when_next_is_blank():
    # API returns one entry per vehicle; second vehicle's T1 is the next bus
    out = bot.format_arrivals(_arrivals([
        ShuttleTiming("P", "4", "-"),
        ShuttleTiming("P", "15", "-"),
    ]))
    assert "🚌 *P*: 4 min | Next: 15 min" in out


def test_first_entry_next_wins_when_present():
    out = bot.format_arrivals(_arrivals([
        ShuttleTiming("A1", "3", "10"),
        ShuttleTiming("A1", "20", "-"),
    ]))
    assert "🚌 *A1*: 3 min | Next: 10 min" in out


def test_public_bus_numbers_filtered_out():
    out = bot.format_arrivals(_arrivals([ShuttleTiming("95", "2", "8")]))
    assert "95" not in out.splitlines()[-1]
    assert "no buses" in out
