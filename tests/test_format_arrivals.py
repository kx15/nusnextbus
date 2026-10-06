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


def test_public_buses_shown_in_own_section():
    a = _arrivals([ShuttleTiming("A1", "3", "10")])
    a.public = [ShuttleTiming("95", "Arr", "9")]
    out = bot.format_arrivals(a)
    assert "🚍 *Public Buses*" in out
    assert "*95*: " + bot._fmt_time("Arr") + " | Next: 9 min" in out
    assert "no buses" not in out


def test_public_only_stop_is_not_reported_empty():
    a = _arrivals([])
    a.public = [ShuttleTiming("151", "4", "-")]
    assert "no buses" not in bot.format_arrivals(a)


def test_tcoms_advisory():
    a = BusStopArrivals("TCOMS", "TCOMS", "", [ShuttleTiming("A1", "3", "10")])
    assert "Opp HSSML" in bot.format_arrivals(a)
