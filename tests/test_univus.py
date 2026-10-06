"""Tests for the uNivUS guest-session shuttle client and the NextBus fallback."""
import json

import httpx
import pytest

import api
import univus

DATA = {"TimeStamp": "2026-10-06 21:33:00", "name": "KR-MRT",
        "shuttles": [{"name": "A1", "arrivalTime": "6", "nextArrivalTime": "22"}]}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(univus, "_session", None)
    monkeypatch.setattr(univus, "_down_until", 0.0)


def _install(monkeypatch, esb_responses):
    """Fake uNivUS: login sets cookies via a 302; ESB replies come from the given list in order."""
    calls = {"login": 0, "esb": []}
    replies = iter(esb_responses)

    def handler(req):
        if req.url.path.endswith("/loginPublic"):
            calls["login"] += 1
            return httpx.Response(302, headers=[
                ("Location", "/univus/web/"),
                ("Set-Cookie", f"UNIVUS_WEB_XSRF_TOKEN=tok%2B{calls['login']}; Path=/"),
                ("Set-Cookie", "UNIVUS_WEB_API_DATA=abc; Path=/univus/web/api/login"),
            ])
        calls["esb"].append(req)
        return next(replies)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(univus, "_new_client", lambda: httpx.AsyncClient(transport=transport, follow_redirects=False))
    return calls


async def test_guest_session_and_double_encoded_payload(monkeypatch):
    calls = _install(monkeypatch, [httpx.Response(200, json=json.dumps({"code": "00000", "data": DATA}))])
    assert await univus.fetch_shuttle_service("KR-MRT") == DATA
    req = calls["esb"][0]
    assert req.headers["X-XSRF-TOKEN"] == "tok+1"
    assert "UNIVUS_WEB_API_DATA=abc" in req.headers["Cookie"]
    assert json.loads(req.content) == {"methodpath": univus.SHUTTLE_METHOD, "busstopname": "KR-MRT"}


async def test_expired_session_relogs_in_once(monkeypatch):
    calls = _install(monkeypatch, [
        httpx.Response(200, json={"code": "10007", "msg": "expired"}),
        httpx.Response(200, json={"code": "00000", "data": {"ShuttleServiceResult": DATA}}),
    ])
    assert await univus.fetch_shuttle_service("KR-MRT") == DATA
    assert calls["login"] == 2
    assert calls["esb"][1].headers["X-XSRF-TOKEN"] == "tok+2"


async def test_error_code_trips_breaker(monkeypatch):
    calls = _install(monkeypatch, [httpx.Response(200, json={"code": "10009", "msg": "new release"})])
    with pytest.raises(univus.UnivusError, match="10009"):
        await univus.fetch_shuttle_service("KR-MRT")
    with pytest.raises(univus.UnivusError, match="skipped"):
        await univus.fetch_shuttle_service("CLB")
    assert len(calls["esb"]) == 1


async def test_rejected_login_raises(monkeypatch):
    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    monkeypatch.setattr(univus, "_new_client", lambda: httpx.AsyncClient(transport=transport))
    with pytest.raises(univus.UnivusError, match="HTTP 500"):
        await univus.fetch_shuttle_service("KR-MRT")


async def test_fetch_shuttles_prefers_univus(monkeypatch):
    async def ok(stop, use_breaker=True):
        return DATA

    monkeypatch.setattr(univus, "fetch_shuttle_service", ok)
    result, source = await api._fetch_shuttles(None, "KR-MRT", {}, "https://legacy.test")
    assert (result, source) == (DATA, "univus")


async def test_fetch_shuttles_falls_back_to_nextbus(monkeypatch):
    async def down(stop, use_breaker=True):
        raise univus.UnivusError("down")

    legacy = {"name": "KR-MRT", "caption": "Kent Ridge MRT", "TimeStamp": "t", "shuttles": []}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ShuttleServiceResult": legacy}))
    monkeypatch.setattr(univus, "fetch_shuttle_service", down)
    async with httpx.AsyncClient(transport=transport) as c:
        result, source = await api._fetch_shuttles(c, "KR-MRT", {}, "https://legacy.test")
    assert (result, source) == (legacy, "nextbus")


async def test_stop_without_caption_uses_known_caption(monkeypatch):
    async def ok(stop, use_breaker=True):
        return DATA

    async def no_public(client, stop):
        return []

    monkeypatch.setattr(univus, "fetch_shuttle_service", ok)
    monkeypatch.setattr(api, "_fetch_public", no_public)
    a = await api._fetch_stop(None, "KR-MRT", {}, "https://legacy.test")
    assert a.stop_caption == "Kent Ridge MRT" and a.source == "univus"
    assert [(t.name, t.arrival_time) for t in a.timings] == [("A1", "6")]


async def test_stop_level_error_does_not_trip_breaker(monkeypatch):
    calls = _install(monkeypatch, [
        httpx.Response(200, json={"code": "20001", "msg": "unknown stop"}),
        httpx.Response(200, json={"code": "00000", "data": DATA}),
    ])
    with pytest.raises(univus.StopError):
        await univus.fetch_shuttle_service("NOPE")
    assert await univus.fetch_shuttle_service("KR-MRT") == DATA
    assert len(calls["esb"]) == 2


async def test_concurrent_rejections_log_in_once(monkeypatch):
    import asyncio

    logins = []

    def handler(req):
        if req.url.path.endswith("/loginPublic"):
            logins.append(1)
            return httpx.Response(302, headers=[
                ("Location", "/univus/web/"),
                ("Set-Cookie", f"UNIVUS_WEB_XSRF_TOKEN=s{len(logins)}; Path=/"),
            ])
        # The first session has expired; any later one is accepted
        if req.headers["X-XSRF-TOKEN"] == "s1":
            return httpx.Response(200, json={"code": "10007", "msg": "expired"})
        return httpx.Response(200, json={"code": "00000", "data": DATA})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(univus, "_new_client", lambda: httpx.AsyncClient(transport=transport, follow_redirects=False))
    results = await asyncio.gather(*[univus.fetch_shuttle_service(s) for s in ("A", "B", "C")])
    assert results == [DATA] * 3
    assert len(logins) == 2


def test_negative_minutes_shown_as_no_time():
    import bot

    assert bot._fmt_time("-3") == "–"
    assert bot._fmt_time("0") == "0 min"
