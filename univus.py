"""NUS shuttle arrivals from uNivUS, the feed the official app has used since Sep 2026.

Uses the uNivUS web app's public guest session: no NUSNET login or API key.
The ShuttleService payload has the same shape as the legacy NextBus API.
"""
import asyncio
import json
import time
from urllib.parse import unquote

import httpx

ORIGIN = "https://inetapps.nus.edu.sg"
WEB_BASE = f"{ORIGIN}/univus/web/"
LOGIN_URL = f"{WEB_BASE}api/login/loginPublic"
ESB_URL = f"{ORIGIN}/univus/web/api/esb"
SHUTTLE_METHOD = "/univus/api/bus-proxy/shuttle-service"
XSRF_COOKIE = "UNIVUS_WEB_XSRF_TOKEN"

_RENEW_AFTER_S = 23 * 3600
# Codes uNivUS returns (at HTTP 200) when the guest session is no longer valid
_AUTH_CODES = {"10007", "19000"}
# Codes that mean the whole feed is refusing us (bad API key, outdated app version)
_FEED_CODES = {"10000", "10009"}
# After a failure, skip uNivUS for this long so users aren't kept waiting on it
_BREAKER_S = 60


class UnivusError(Exception):
    pass


class StopError(UnivusError):
    """A problem with one stop's answer, not with the feed; doesn't trip the breaker."""


_session: httpx.AsyncClient | None = None
_session_at = 0.0
_down_until = 0.0
_lock = asyncio.Lock()


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=6.0, follow_redirects=False)


async def _login() -> httpx.AsyncClient:
    client = _new_client()
    try:
        # The login redirect itself carries the session cookies; don't follow it
        resp = await client.get(LOGIN_URL, headers={"Accept": "text/html", "Referer": WEB_BASE})
        if resp.status_code != 302 or XSRF_COOKIE not in {c.name for c in client.cookies.jar}:
            raise UnivusError(f"guest login failed: HTTP {resp.status_code}")
    except Exception:
        await client.aclose()
        raise
    return client


async def _get_session(rejected: httpx.AsyncClient | None = None) -> httpx.AsyncClient:
    """The current guest session; renewed if expired or if it is the one just rejected.

    Many stops are fetched at once, so only the first caller to see a rejected
    session logs in again; the rest pick up the new one.
    """
    global _session, _session_at
    async with _lock:
        stale = _session is None or time.monotonic() - _session_at > _RENEW_AFTER_S
        if stale or (rejected is not None and _session is rejected):
            if _session is not None:
                await _session.aclose()
                _session = None
            _session = await _login()
            _session_at = time.monotonic()
        return _session


def _headers(client: httpx.AsyncClient) -> dict:
    cookies = {c.name: c.value for c in client.cookies.jar}
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-XSRF-TOKEN": unquote(cookies.get(XSRF_COOKIE, "")),
        # Sent explicitly: the jar would drop cookies whose path doesn't cover /api/esb
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "Origin": ORIGIN,
        "Referer": WEB_BASE,
    }


async def _query(stop_name: str) -> dict:
    client = None
    for attempt in (0, 1):
        client = await _get_session(rejected=client)
        resp = await client.post(
            ESB_URL,
            headers=_headers(client),
            json={"methodpath": SHUTTLE_METHOD, "busstopname": stop_name},
        )
        if resp.status_code in (401, 403) and attempt == 0:
            continue
        resp.raise_for_status()
        payload = resp.json()
        # The web proxy sometimes JSON-encodes the upstream JSON text once more
        if isinstance(payload, str):
            payload = json.loads(payload)
        code = str(payload.get("code")) if isinstance(payload, dict) else None
        if code in _AUTH_CODES and attempt == 0:
            continue
        if code != "00000":
            msg = payload.get("msg") if isinstance(payload, dict) else str(payload)[:100]
            # Unrecognised app version / API key or a dead session affect every stop
            error = UnivusError if code in _FEED_CODES | _AUTH_CODES or code is None else StopError
            raise error(f"code {code}: {msg}")
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("ShuttleServiceResult"), dict):
            data = data["ShuttleServiceResult"]
        if not isinstance(data, dict) or not isinstance(data.get("shuttles"), list):
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise StopError(f"unexpected response, data keys: {keys}")
        return data
    raise UnivusError("guest session rejected after renewal")


async def fetch_shuttle_service(stop_name: str, use_breaker: bool = True) -> dict:
    """Return the ShuttleServiceResult-shaped dict for a stop, or raise."""
    global _down_until
    if use_breaker and time.monotonic() < _down_until:
        raise UnivusError("skipped: failed recently")
    try:
        return await _query(stop_name)
    except StopError:
        raise
    except Exception:
        _down_until = time.monotonic() + _BREAKER_S
        raise
