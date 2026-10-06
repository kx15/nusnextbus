"""NUS Enterprise: route to Opp HSSML (stairs up) rather than the nearest-looking stop."""
import pytest

import bot
import routing
from api import BusStopArrivals, ShuttleTiming
from stops import dest_override, find_stop


@pytest.mark.parametrize("label", ["NUS Enterprise", "nus enterprise @ i-cube", "21 Heng Mui Keng Terrace"])
def test_matches_by_name(label):
    assert dest_override(label, None, None)["stop"] == "HSSML-OPP"


def test_matches_by_location_near_building():
    assert dest_override("dropped pin", 1.29245, 103.77550)["stop"] == "HSSML-OPP"


def test_other_places_unaffected():
    tcoms = find_stop("TCOMS")
    assert dest_override("TCOMS", tcoms["lat"], tcoms["lng"]) is None
    assert dest_override("Block 71", None, None) is None


class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append(text)
        return self

    async def edit_text(self, text, **kw):
        self.sent.append(text)


async def test_run_plan_pins_opp_hssml_and_adds_tip(monkeypatch):
    seen = {}

    async def fake_route(lines, o, loc, d_stop, d_lat, d_lng, d_is_exact, d_label):
        seen.update(stop=d_stop["name"], lat=d_lat, exact=d_is_exact)
        lines.append("route")

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "x")
    monkeypatch.setattr(bot, "_route_on_campus", fake_route)
    kr, tcoms = find_stop("KR-MRT"), find_stop("TCOMS")
    msg = _Msg()
    # Geocoder put "NUS Enterprise" nearest TCOMS: the override must still win
    await bot._run_plan(msg, kr, kr["lat"], kr["lng"], "Kent Ridge MRT",
                        tcoms, tcoms["lat"], tcoms["lng"], "NUS Enterprise", False)
    assert seen == {"stop": "HSSML-OPP", "lat": 1.292430, "exact": False}
    assert "stairs up to NUS Enterprise" in msg.sent[-1]


async def test_kent_ridge_mrt_prefers_direct_a2_over_transfer(monkeypatch):
    timings = [ShuttleTiming(n, "3", "10") for n in ("A1", "A2", "D1", "D2", "K", "P", "R1", "R2")]

    async def fake_arrivals(name):
        return BusStopArrivals(name, name, "", timings)

    async def fake_directions(*a):
        return {"maps_url": "m", "duration": "2 mins", "distance": "80 m", "steps": []}

    monkeypatch.setattr(routing, "get_arrivals_async", fake_arrivals)
    monkeypatch.setattr(routing, "get_directions", fake_directions)
    kr, hssml = find_stop("KR-MRT"), find_stop("HSSML-OPP")
    lines = []
    await routing._route_on_campus(lines, kr, (kr["lat"], kr["lng"]), hssml, 1.29243, 103.77552, False, "NUS Enterprise")
    out = "\n".join(lines)
    assert "_cross the road to Opp Kent Ridge MRT_" in out
    assert "*A2* · 3 stops" in out
    assert "Option" not in out
