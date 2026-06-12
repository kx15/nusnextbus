"""Characterization tests for the pure routing/schedule functions.

These pin the *current* behaviour so the routing engine can be refactored
(extracted into modules, decomposed) without silent regressions.
"""
import bot
import routing
from routes import _BUS_SCHEDULE


class TestNusStopsBetween:
    def test_known_pairs(self):
        cases = {
            ("A1", "KRB", "CLB"): 11,
            ("A1", "CLB", "KRB"): 1,
            ("A1", "KR-MRT", "UHALL"): 2,
            ("A2", "KRB", "S17"): 6,
            ("D1", "COM3", "UTOWN"): 7,
            ("D1", "UTOWN", "COM3"): 6,
            ("D2", "COM3", "UTOWN"): 8,
            ("K", "PGP", "CLB"): 6,
            ("P", "KV", "OTH"): 2,
            ("P", "OTH", "KR-MRT"): 2,
            ("R1", "KV", "PGP"): 8,
            ("R2", "PGP", "KV"): 8,
            ("A1", "KRB", "KRB"): 12,  # circular route: wraps a full loop
        }
        for (bus, a, b), expected in cases.items():
            assert routing._nus_stops_between(bus, a, b) == expected, (bus, a, b)

    def test_unreachable_returns_none(self):
        assert routing._nus_stops_between("A1", "CLB", "NONEXIST") is None
        assert routing._nus_stops_between("Z9", "KRB", "CLB") is None


class TestFindTransfers:
    def test_top_result_and_count(self):
        cases = {
            ("OTH", "COM3"): (4, ("P", "UTOWN", "D1", 10)),
            ("KV", "PGP"): (32, ("K", "KR-MRT-OPP", "D2", 6)),
            ("CLB", "PGPR"): (6, ("K", "KR-MRT-OPP", "A2", 9)),
            ("UTOWN", "AS5"): (17, ("D1", "CLB", "D1", 4)),
        }
        for (o, d), (count, top) in cases.items():
            res = routing._find_transfers(o, d)
            assert len(res) == count, (o, d, len(res))
            assert res[0] == top, (o, d, res[0])

    def test_results_sorted_by_total_stops(self):
        res = routing._find_transfers("KV", "PGP")
        totals = [r[3] for r in res]
        assert totals == sorted(totals)


class TestBestDestStop:
    def test_picks_fewest_stops_from_origin(self):
        cands = bot.nearby_stops(1.296544, 103.772569, radius_m=300)  # near CLB
        assert {c["name"] for c in cands} == {"CLB", "IT", "LT13-OPP"}
        assert routing._best_dest_stop("KRB", cands)["name"] == "IT"


class TestSchedule:
    def test_first_last_today_independent_values(self):
        # _bus_first_last depends on weekday; assert the static schedule table instead.
        assert _BUS_SCHEDULE["K"]["sun_ph"] is None
        assert _BUS_SCHEDULE["P"]["mon_fri"] == ("08:20", "17:25")
        assert _BUS_SCHEDULE["A1"]["mon_sat"] == ("07:15", "23:00")

    def test_schedule_lines(self):
        assert bot._bus_schedule_lines("A1") == [
            "⏰ *Mon–Sat*  First 07:15 · Last 23:00",
            "⏰ *Sun/PH*  First 09:07 · Last 23:00",
        ]
        assert bot._bus_schedule_lines("P") == [
            "⏰ *Mon–Fri*  First 08:20 · Last 17:25",
            "⏰ *Sat/Sun/PH*  No service 💀",
        ]
        assert bot._bus_schedule_lines("D1")[0].startswith("⏰ *Mon–Fri*")
        assert bot._bus_schedule_lines("UNKNOWN") == []


class TestFmtTime:
    def test_deterministic_cases(self):
        assert bot._fmt_time("-") == "–"
        assert bot._fmt_time("") == "–"
        assert bot._fmt_time("Arr") == "🏃‍♂️RUN"
        assert bot._fmt_time("arr") == "🏃‍♂️RUN"
        assert bot._fmt_time("0") == "0 min"
        assert bot._fmt_time("5") == "5 min"
        assert bot._fmt_time("abc") == "abc min"

    def test_over_30_minutes_shows_clock_time(self):
        # exact value depends on now(); assert the shape
        assert bot._fmt_time("31").startswith("~")
