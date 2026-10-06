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
