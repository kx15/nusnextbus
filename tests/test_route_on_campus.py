"""Golden-output integration tests for the on-campus routing engine.

The NUS NextBus and Google Maps calls are replaced with deterministic fakes so
the full branching of `_route_on_campus` (companion crossings, Bukit Timah
gateway transfers, direct routes) is pinned. This is the safety net that lets
the ~600-line function be decomposed without behavioural drift.
"""
import pytest

import routing
from api import BusStopArrivals, ShuttleTiming
from stops import find_stop

_FIXED_TIMINGS = [
    ("A1", "3", "10"), ("D1", "5", "12"), ("D2", "7", "15"), ("K", "2", "9"),
    ("P", "4", "11"), ("A2", "6", "13"), ("R1", "8", "16"), ("R2", "1", "8"),
]


def _fake_arrivals(name):
    stop = find_stop(name)
    return BusStopArrivals(
        name, stop["caption"] if stop else name, "2026-06-12 12:00:00",
        [ShuttleTiming(n, a, x) for n, a, x in _FIXED_TIMINGS],
    )


@pytest.fixture(autouse=True)
def _patch_apis(monkeypatch):
    async def fake_get_arrivals_async(name):
        return _fake_arrivals(name)

    async def fake_get_directions(a, b, c, d):
        return {"maps_url": "http://m", "mode": "walking", "duration": "3 mins",
                "distance": "200 m", "distance_m": 200,
                "steps": [{"instruction": "Walk", "distance": "200 m"}]}

    monkeypatch.setattr(routing, "get_arrivals_async", fake_get_arrivals_async)
    monkeypatch.setattr(routing, "get_directions", fake_get_directions)


async def _route(o_name, d_name, d_is_exact=True):
    lines = []
    o = find_stop(o_name)
    d = find_stop(d_name)
    await routing._route_on_campus(
        lines, o, (o["lat"], o["lng"]), d, d["lat"], d["lng"], d_is_exact, d["caption"]
    )
    return lines


GOLDEN = {
    ("CLB", "UTOWN"): [
        "_cross the road to Information Technology_",
        "",
        "🚌 *NUS shuttle: Information Technology → University Town*",
        "  🚌 *D1* · 3 stops  5 min | Next: 12 min",
        "  🚌 *R2* · 2 stops  1 min | Next: 8 min",
        "",
        "[open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=Central%20Library%20NUS%20Singapore&destination=1.303876,103.774621&travelmode=walking)",
    ],
    ("KRB", "CLB"): [
        "🚌 *NUS shuttle: Kent Ridge Bus Terminal → Information Technology*",
        "  🚌 *A2* · 1 stop  6 min | Next: 13 min",
        "  _cross the road to Central Library_",
        "",
        "[open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=Kent%20Ridge%20Bus%20Terminal%20NUS%20Singapore&destination=1.296544,103.772569&travelmode=walking)",
    ],
    ("OTH", "COM3"): [
        "*① Oei Tiong Ham Building → Kent Ridge MRT (Bus P)*",
        "  🚌 *P* · 2 stops  4 min | Next: 11 min",
        "  _cross the road to Opp Kent Ridge MRT_",
        "",
        "*② Opp Kent Ridge MRT → COM 3*",
        "  🚌 *D2* · 3 stops  7 min | Next: 15 min",
        "",
        "[open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=Oei%20Tiong%20Ham%20Building%20NUS%20Singapore&destination=1.294431,103.775217&travelmode=transit)",
    ],
    ("PGP", "UTOWN"): [
        "🚌 *NUS shuttle: Prince George's Park → University Town*",
        "  🚌 *D2* · 6 stops  7 min | Next: 15 min",
        "  🚌 *R2* · 6 stops  1 min | Next: 8 min",
        "",
        "[open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=Prince%20George%27s%20Park%20NUS%20Singapore&destination=1.303876,103.774621&travelmode=walking)",
    ],
    ("KR-MRT", "OTH"): [
        "*① Kent Ridge MRT → Kent Vale*",
        "  🚌 *A1*  3 min | Next: 10 min",
        "",
        "*② Kent Vale → Oei Tiong Ham Building (Bus P)*",
        "  🚌 *P* · 2 stops  4 min | Next: 11 min",
        "",
        "[open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=Kent%20Ridge%20MRT%20NUS%20Singapore&destination=1.319796,103.817774&travelmode=transit)",
    ],
    ("AS5", "YIH"): [
        "🚌 *NUS shuttle: AS 5 → Yusof Ishak House*",
        "  🚌 *A1* · 8 stops  3 min | Next: 10 min",
        "",
        "[open in Google Maps](https://www.google.com/maps/dir/?api=1&origin=AS%205%20NUS%20Singapore&destination=1.298885,103.774377&travelmode=walking)",
    ],
}


@pytest.mark.parametrize("pair,expected", list(GOLDEN.items()), ids=[f"{o}->{d}" for o, d in GOLDEN])
async def test_route_on_campus_golden(pair, expected):
    assert await _route(*pair) == expected
