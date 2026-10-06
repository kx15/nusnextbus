"""Tests for the NUS NextBus arrival-time resolution / extrapolation."""
from datetime import datetime, timedelta, timezone

from api import _resolve_eta

_SGT = timezone(timedelta(hours=8))


def _ts(mins_from_now: int) -> str:
    return (datetime.now(_SGT) + timedelta(minutes=mins_from_now)).strftime("%Y-%m-%d %H:%M:%S")


def _shuttle(etas, arr="-", nxt="-"):
    return {"arrivalTime": arr, "nextArrivalTime": nxt, "_etas": etas}


def test_direct_field_value_used_when_present():
    assert _resolve_eta(_shuttle([], arr="7"), "arrivalTime", 0) == "7"


def test_no_etas_returns_dash():
    assert _resolve_eta(_shuttle([]), "arrivalTime", 0) == "-"


def test_future_etas_used_directly():
    fut = [{"eta": i, "ts": _ts(2 + i * 5)} for i in range(5)]
    assert _resolve_eta(_shuttle(fut), "arrivalTime", 0) == "2"
    assert _resolve_eta(_shuttle(fut), "nextArrivalTime", 1) == "7"


def test_all_past_etas_extrapolate_to_future():
    # headway 5 min, all 5 entries are 5..30 min in the past.
    past = [{"eta": 0, "ts": _ts(-30 + i * 5)} for i in range(5)]
    first = _resolve_eta(_shuttle(past), "arrivalTime", 0)
    # Must produce a near-future minute count or "Arr", never a stale past value.
    assert first == "Arr" or (first.isdigit() and int(first) <= 6), first


def test_grace_window_recent_trip_clamped_to_arr():
    # a trip 2 min ago (within grace window) should show as imminent, not skipped
    etas = [{"eta": 0, "ts": _ts(-2 + i * 10)} for i in range(5)]
    val = _resolve_eta(_shuttle(etas), "arrivalTime", 0)
    assert val == "Arr" or val.isdigit()


async def test_fetch_public_parses_lta(monkeypatch):
    from datetime import datetime, timedelta

    import httpx

    import api

    now = datetime.now(api._SGT)
    iso = lambda m: (now + timedelta(minutes=m, seconds=30)).isoformat(timespec="seconds")  # noqa: E731
    payload = {"Services": [
        {"ServiceNo": "151", "NextBus": {"EstimatedArrival": iso(4)}, "NextBus2": {"EstimatedArrival": ""}},
        {"ServiceNo": "95", "NextBus": {"EstimatedArrival": iso(0)}, "NextBus2": {"EstimatedArrival": iso(9)}},
    ]}

    def handler(req):
        assert req.headers["AccountKey"] == "k"
        assert req.url.params["BusStopCode"] == "16181"
        return httpx.Response(200, json=payload)

    monkeypatch.setenv("LTA_ACCOUNT_KEY", "k")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        got = await api._fetch_public(c, "CLB")
        unmapped = await api._fetch_public(c, "UTOWN")
    assert [(t.name, t.arrival_time, t.next_arrival_time) for t in got] == [("95", "Arr", "9"), ("151", "4", "-")]
    assert unmapped == []


async def test_fetch_public_without_key_skips(monkeypatch):
    import api

    monkeypatch.delenv("LTA_ACCOUNT_KEY", raising=False)
    assert await api._fetch_public(None, "CLB") == []


async def test_lta_diagnostics_missing_key(monkeypatch):
    import api

    monkeypatch.delenv("LTA_ACCOUNT_KEY", raising=False)
    monkeypatch.setenv("LTA_API_KEY", "x")
    out = "\n".join(await api.lta_diagnostics())
    assert "LTA_ACCOUNT_KEY: NOT SET" in out
    assert "LTA_API_KEY" in out


async def test_lta_diagnostics_strips_quotes_and_reports_rejection(monkeypatch):
    import httpx

    import api

    seen = {}

    def handler(req):
        seen["key"] = req.headers["AccountKey"]
        return httpx.Response(401, text="Unauthorized")

    monkeypatch.setenv("LTA_ACCOUNT_KEY", ' "secretkey" ')
    monkeypatch.setattr(api, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = "\n".join(await api.lta_diagnostics())
    assert seen["key"] == "secretkey"
    assert "had quotes/spaces" in out and "HTTP 401" in out
    assert "secretkey" not in out


async def test_isb_diagnostics_marks_live_vs_estimated(monkeypatch):
    import httpx

    import api

    fut = [{"eta": i, "ts": _ts(4 + i * 10)} for i in range(5)]

    def handler(req):
        stop = req.url.params["busstopname"]
        if stop == "UTOWN":
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(200, json={"ShuttleServiceResult": {
            "TimeStamp": "2026-10-06 21:00:00",
            "shuttles": [
                {"name": "A1", "arrivalTime": "3", "nextArrivalTime": "12", "arrivalTime_veh_plate": "PD123A"},
                {"name": "D2", "arrivalTime": "-", "nextArrivalTime": "-", "_etas": fut},
                {"name": "95", "arrivalTime": "2", "nextArrivalTime": "9"},
            ],
        }})

    monkeypatch.setenv("NEXTBUS_API_URL", "https://example.test")
    monkeypatch.setenv("NEXTBUS_BASIC_AUTH", "x")
    monkeypatch.setattr(api, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = "\n".join(await api.isb_diagnostics(("CLB", "UTOWN")))
    assert "A1: LIVE 3 (PD123A), next 12" in out
    assert "D2: no live time, bot shows estimate 4" in out
    assert "raw arr=- next=- _etas[5]: 0m@" in out
    assert "_etas fields: eta, ts" not in out  # first shuttle (A1) has no _etas
    assert "fields: arrivalTime, arrivalTime_veh_plate, name, nextArrivalTime" in out
    assert "95" not in out
    assert "UTOWN: HTTP 401" in out


async def test_isb_diagnostics_reports_missing_config(monkeypatch):
    import api

    monkeypatch.delenv("NEXTBUS_BASIC_AUTH", raising=False)
    monkeypatch.setenv("NEXTBUS_API_URL", "https://example.test")
    assert "NEXTBUS_BASIC_AUTH: NOT SET" in await api.isb_diagnostics()
