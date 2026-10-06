import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

import univus
from stops import LTA_STOP_CODES, STOPS

logger = logging.getLogger(__name__)

_CAPTIONS = {s["name"]: s["caption"] for s in STOPS}

LTA_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"

_SGT = timezone(timedelta(hours=8))

# One shared client so concurrent per-stop fetches reuse connections
# instead of paying a TCP+TLS handshake per call.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


@dataclass
class ShuttleTiming:
    name: str
    arrival_time: str
    next_arrival_time: str
    arrival_veh_plate: str | None = None
    next_arrival_veh_plate: str | None = None


@dataclass
class BusStopArrivals:
    stop_name: str
    stop_caption: str
    last_updated: str
    timings: list[ShuttleTiming] = field(default_factory=list)
    public: list[ShuttleTiming] = field(default_factory=list)
    source: str = "nextbus"


def _lta_minutes(bus: dict | None) -> str:
    eta = (bus or {}).get("EstimatedArrival")
    if not eta:
        return "-"
    mins = int((datetime.fromisoformat(eta) - datetime.now(_SGT)).total_seconds() // 60)
    return "Arr" if mins <= 0 else str(mins)


def _lta_key() -> str:
    # Tolerate quotes/whitespace pasted into the hosting dashboard along with the key
    return os.environ.get("LTA_ACCOUNT_KEY", "").strip().strip("\"'").strip()


async def lta_diagnostics(stop_name: str = "CLB") -> list[str]:
    """Report LTA config and a live call result, without revealing the key."""
    raw = os.environ.get("LTA_ACCOUNT_KEY")
    key = _lta_key()
    similar = sorted(k for k in os.environ if "LTA" in k.upper() or "ACCOUNT" in k.upper())
    lines = [
        f"LTA_ACCOUNT_KEY: {'SET' if raw is not None else 'NOT SET'}"
        + (f" ({len(key)} chars{', had quotes/spaces' if raw != key else ''})" if raw is not None else ""),
        f"similar env vars: {', '.join(similar) or 'none'}",
    ]
    code = LTA_STOP_CODES.get(stop_name)
    if not key or not code:
        return lines
    try:
        resp = await _get_client().get(
            LTA_URL,
            params={"BusStopCode": code},
            headers={"AccountKey": key, "accept": "application/json"},
        )
        lines.append(f"LTA {stop_name} ({code}): HTTP {resp.status_code}")
        if resp.is_success:
            services = resp.json().get("Services", [])
            lines.append(f"services: {', '.join(s['ServiceNo'] for s in services) or 'none right now'}")
        else:
            lines.append(f"body: {resp.text[:200]}")
    except Exception as exc:
        lines.append(f"LTA call error: {type(exc).__name__}: {exc}")
    return lines


def _describe_shuttles(result: dict) -> list[str]:
    shuttles = [s for s in result.get("shuttles", []) if not str(s.get("name", "")).strip().isdigit()]
    if not shuttles:
        return ["  no ISB services returned"]
    lines = [f"  fields: {', '.join(sorted(shuttles[0]))}"]
    for s in shuttles:
        live = s.get("arrivalTime", "-")
        if live not in ("-", ""):
            plate = s.get("arrivalTime_veh_plate") or "no plate"
            lines.append(f"  {s.get('name')}: LIVE {live} ({plate}), next {s.get('nextArrivalTime', '-')}")
        else:
            est = _resolve_eta(s, "arrivalTime", 0)
            lines.append(f"  {s.get('name')}: no live time, " + (f"bot estimates {est}" if est not in ("-", "") else "bot shows –"))
    return lines


async def isb_diagnostics(stop_names: tuple[str, ...] = ("KR-MRT", "CLB")) -> list[str]:
    """Compare uNivUS (the official app's feed) with the legacy NextBus API, per stop and service."""
    api_url = os.environ.get("NEXTBUS_API_URL", "").rstrip("/")
    auth = os.environ.get("NEXTBUS_BASIC_AUTH", "")
    lines = [f"checked {datetime.now(_SGT).strftime('%H:%M:%S')}"]
    for stop in stop_names:
        lines += ["", f"== {stop}"]
        try:
            result = await univus.fetch_shuttle_service(stop, use_breaker=False)
            lines.append(f"uNivUS: OK, timestamp {result.get('TimeStamp', '?')}")
            lines += _describe_shuttles(result)
        except Exception as exc:
            lines.append(f"uNivUS: FAILED {type(exc).__name__}: {str(exc)[:150]}")
        if not api_url or not auth:
            lines.append("old NextBus: NEXTBUS_API_URL / NEXTBUS_BASIC_AUTH not set")
            continue
        try:
            result = await _fetch_legacy(_get_client(), stop, {"Authorization": f"Basic {auth}"}, api_url)
            lines.append(f"old NextBus: OK, timestamp {result.get('TimeStamp', '?')}")
            lines += _describe_shuttles(result)
        except Exception as exc:
            lines.append(f"old NextBus: FAILED {type(exc).__name__}: {str(exc)[:150]}")
    return lines


async def _fetch_public(client: httpx.AsyncClient, stop_name: str) -> list[ShuttleTiming]:
    code = LTA_STOP_CODES.get(stop_name)
    key = _lta_key()
    if not code or not key:
        return []
    try:
        resp = await client.get(
            LTA_URL,
            params={"BusStopCode": code},
            headers={"AccountKey": key, "accept": "application/json"},
        )
        resp.raise_for_status()
        services = resp.json().get("Services", [])
    except Exception as exc:
        logger.warning("LTA fetch failed for %s (%s): %s", stop_name, code, exc)
        return []
    return [
        ShuttleTiming(
            name=s["ServiceNo"],
            arrival_time=_lta_minutes(s.get("NextBus")),
            next_arrival_time=_lta_minutes(s.get("NextBus2")),
        )
        for s in sorted(services, key=lambda s: (len(s["ServiceNo"]), s["ServiceNo"]))
    ]


def _resolve_eta(shuttle: dict, field: str, etas_idx: int) -> str:
    """Return arrival time string (minutes), falling back to _etas when field is '-'.

    _etas only contains the first 5 scheduled trips of the day; after those pass we
    extrapolate using the headway inferred from the interval between those entries.
    """
    val = shuttle.get(field, "-")
    if val not in ("-", ""):
        return val
    etas = shuttle.get("_etas") or []
    if not etas:
        return val

    now = datetime.now(_SGT)

    # Parse ts (absolute SGT scheduled times) from every _etas entry
    scheduled = []
    for entry in etas:
        ts = entry.get("ts")
        if ts:
            try:
                scheduled.append(datetime.fromisoformat(ts).replace(tzinfo=_SGT))
            except Exception:
                pass

    if not scheduled:
        # No ts — fall back to eta (precomputed, potentially stale)
        if etas_idx < len(etas):
            eta = etas[etas_idx].get("eta")
            return str(eta) if eta is not None else val
        return val

    scheduled.sort()

    # Estimate headway from the gaps between consecutive scheduled entries
    if len(scheduled) >= 2:
        gaps = [(scheduled[i + 1] - scheduled[i]).total_seconds() for i in range(len(scheduled) - 1)]
        headway = timedelta(seconds=round(sum(gaps) / len(gaps)))
    else:
        headway = timedelta(0)

    # Grace window: include a trip that passed up to this many minutes ago —
    # it may be running late and still en route.
    hw_mins = headway.total_seconds() / 60
    tolerance_mins = max(5, round(hw_mins / 4)) if hw_mins > 0 else 5

    arrivals: list[int] = []
    elapsed_to_last = (now - scheduled[-1]).total_seconds()

    if elapsed_to_last < 0:
        # Some scheduled entries are still in the future — use the list directly
        for t in scheduled:
            mins = round((t - now).total_seconds() / 60)
            if mins >= -tolerance_mins:
                arrivals.append(max(mins, 0))
    elif headway.total_seconds() > 0:
        # All known entries are past; extrapolate forward.
        # Start from the current cycle (may be running late within the grace window).
        total_cycles = int(elapsed_to_last / headway.total_seconds())
        for offset in range(total_cycles, total_cycles + etas_idx + 3):
            t = scheduled[-1] + headway * offset
            mins = round((t - now).total_seconds() / 60)
            if mins >= -tolerance_mins:
                arrivals.append(max(mins, 0))
    else:
        return val

    arrivals.sort()

    if etas_idx < len(arrivals):
        m = arrivals[etas_idx]
        return "Arr" if m == 0 else str(m)
    return val


def _parse_shuttles(shuttles: list) -> list[ShuttleTiming]:
    return [
        ShuttleTiming(
            name=s["name"],
            arrival_time=_resolve_eta(s, "arrivalTime", 0),
            next_arrival_time=_resolve_eta(s, "nextArrivalTime", 1),
            arrival_veh_plate=s.get("arrivalTime_veh_plate"),
            next_arrival_veh_plate=s.get("nextArrivalTime_veh_plate"),
        )
        for s in shuttles
    ]


async def _fetch_legacy(client: httpx.AsyncClient, stop_name: str, headers: dict, api_url: str) -> dict:
    resp = await client.get(f"{api_url}/ShuttleService", params={"busstopname": stop_name}, headers=headers, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["ShuttleServiceResult"]


async def _fetch_shuttles(client: httpx.AsyncClient, stop_name: str, headers: dict, api_url: str) -> tuple[dict, str]:
    """uNivUS (what the official app shows) first; the legacy NextBus API if it fails."""
    try:
        return await univus.fetch_shuttle_service(stop_name), "univus"
    except Exception as exc:
        logger.warning("uNivUS fetch failed for %s, falling back to NextBus: %s", stop_name, exc)
    return await _fetch_legacy(client, stop_name, headers, api_url), "nextbus"


async def _fetch_stop(
    client: httpx.AsyncClient,
    stop_name: str,
    headers: dict,
    api_url: str,
) -> BusStopArrivals | None:
    try:
        (result, source), public = await asyncio.gather(
            _fetch_shuttles(client, stop_name, headers, api_url),
            _fetch_public(client, stop_name),
        )
        return BusStopArrivals(
            stop_name=result.get("name") or stop_name,
            stop_caption=result.get("caption") or _CAPTIONS.get(stop_name, stop_name),
            last_updated=str(result.get("TimeStamp", "")),
            timings=_parse_shuttles(result.get("shuttles", [])),
            public=public,
            source=source,
        )
    except Exception as exc:
        # An expired NEXTBUS_BASIC_AUTH would otherwise look identical to "no buses"
        logger.warning("NextBus fetch failed for %s: %s", stop_name, exc)
        return None


async def get_arrivals_async(stop_name: str) -> BusStopArrivals:
    api_url = os.environ["NEXTBUS_API_URL"].rstrip("/")
    headers = {"Authorization": f"Basic {os.environ['NEXTBUS_BASIC_AUTH']}"}
    result = await _fetch_stop(_get_client(), stop_name, headers, api_url)
    if result is None:
        raise RuntimeError(f"Failed to fetch arrivals for {stop_name}")
    return result


async def get_all_arrivals(stop_names: list[str]) -> list[BusStopArrivals | None]:
    api_url = os.environ["NEXTBUS_API_URL"].rstrip("/")
    headers = {"Authorization": f"Basic {os.environ['NEXTBUS_BASIC_AUTH']}"}
    client = _get_client()
    return list(
        await asyncio.gather(*[_fetch_stop(client, name, headers, api_url) for name in stop_names])
    )
