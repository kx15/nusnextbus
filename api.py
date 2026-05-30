import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

_SGT = timezone(timedelta(hours=8))


@dataclass
class ShuttleTiming:
    name: str
    arrival_time: str
    next_arrival_time: str
    arrival_veh_plate: Optional[str] = None
    next_arrival_veh_plate: Optional[str] = None


@dataclass
class BusStopArrivals:
    stop_name: str
    stop_caption: str
    last_updated: str
    timings: list[ShuttleTiming] = field(default_factory=list)


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

    # If the last known trip is still in the future, just use the scheduled list
    if scheduled[-1] >= now:
        future = [t for t in scheduled if t >= now]
    elif headway.total_seconds() > 0:
        # Extrapolate forward from the last known trip using the inferred headway
        elapsed = (now - scheduled[-1]).total_seconds()
        cycles = int(elapsed / headway.total_seconds())
        future = [scheduled[-1] + headway * (cycles + 1 + i) for i in range(etas_idx + 2)]
    else:
        return val

    if etas_idx >= len(future):
        return val

    mins = round((future[etas_idx] - now).total_seconds() / 60)
    return "Arr" if mins <= 0 else str(mins)


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


def get_arrivals(stop_name: str) -> BusStopArrivals:
    api_url = os.environ["NEXTBUS_API_URL"].rstrip("/")
    auth = os.environ["NEXTBUS_BASIC_AUTH"]
    url = f"{api_url}/ShuttleService?busstopname={stop_name}"
    headers = {"Authorization": f"Basic {auth}"}
    with httpx.Client() as client:
        resp = client.get(url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    result = data["ShuttleServiceResult"]
    return BusStopArrivals(
        stop_name=result["name"],
        stop_caption=result["caption"],
        last_updated=result["TimeStamp"],
        timings=_parse_shuttles(result.get("shuttles", [])),
    )


async def _fetch_stop(
    client: httpx.AsyncClient,
    stop_name: str,
    headers: dict,
    api_url: str,
) -> Optional[BusStopArrivals]:
    url = f"{api_url}/ShuttleService?busstopname={stop_name}"
    try:
        resp = await client.get(url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        result = resp.json()["ShuttleServiceResult"]
        return BusStopArrivals(
            stop_name=result["name"],
            stop_caption=result["caption"],
            last_updated=result["TimeStamp"],
            timings=_parse_shuttles(result.get("shuttles", [])),
        )
    except Exception:
        return None


async def get_arrivals_async(stop_name: str) -> BusStopArrivals:
    api_url = os.environ["NEXTBUS_API_URL"].rstrip("/")
    headers = {"Authorization": f"Basic {os.environ['NEXTBUS_BASIC_AUTH']}"}
    async with httpx.AsyncClient() as client:
        result = await _fetch_stop(client, stop_name, headers, api_url)
    if result is None:
        raise RuntimeError(f"Failed to fetch arrivals for {stop_name}")
    return result


async def get_all_arrivals(stop_names: list[str]) -> list[Optional[BusStopArrivals]]:
    api_url = os.environ["NEXTBUS_API_URL"].rstrip("/")
    headers = {"Authorization": f"Basic {os.environ['NEXTBUS_BASIC_AUTH']}"}
    async with httpx.AsyncClient() as client:
        return list(
            await asyncio.gather(*[_fetch_stop(client, name, headers, api_url) for name in stop_names])
        )
