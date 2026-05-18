import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx


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
    """Return arrival time string, falling back to _etas[idx].eta when field is '-'."""
    val = shuttle.get(field, "-")
    if val in ("-", "") and shuttle.get("_etas"):
        etas = shuttle["_etas"]
        if len(etas) > etas_idx:
            eta = etas[etas_idx].get("eta")
            if eta is not None:
                return str(eta)
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
