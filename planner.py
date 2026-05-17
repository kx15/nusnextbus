import html as _html
import math
import os
import re
from typing import Optional

import httpx


def _strip_html(text: str) -> str:
    text = _html.unescape(text)
    text = re.sub(r"<div[^>]*>", " — ", text)  # <div> used for sub-instructions
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


_GMAPS_DIRECTIONS = "https://maps.googleapis.com/maps/api/directions/json"
_GMAPS_GEOCODE       = "https://maps.googleapis.com/maps/api/geocode/json"
_GMAPS_PLACES_SEARCH = "https://maps.googleapis.com/maps/api/place/textsearch/json"

# Singapore bounding box for geocoding bias
_SG_BOUNDS = "1.15,103.60|1.48,104.00"

# Above this straight-line distance, switch from walking to transit mode
_TRANSIT_THRESHOLD_M = 2000


def _maps_link(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float, mode: str = "walking") -> str:
    return (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_lat},{origin_lng}"
        f"&destination={dest_lat},{dest_lng}"
        f"&travelmode={mode}"
    )


async def get_directions(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
) -> dict:
    """
    Returns a directions dict. Uses walking for short trips (< 2 km),
    transit for longer ones. maps_url and mode are always set.
    """
    dist_m = haversine_m(origin_lat, origin_lng, dest_lat, dest_lng)
    mode = "transit" if dist_m > _TRANSIT_THRESHOLD_M else "walking"
    maps_url = _maps_link(origin_lat, origin_lng, dest_lat, dest_lng, mode)

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return {"maps_url": maps_url, "mode": mode, "duration": None, "distance": None, "steps": []}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _GMAPS_DIRECTIONS,
                params={
                    "origin": f"{origin_lat},{origin_lng}",
                    "destination": f"{dest_lat},{dest_lng}",
                    "mode": mode,
                    "key": api_key,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "OK" or not data.get("routes"):
            return {"maps_url": maps_url, "mode": mode, "duration": None, "distance": None, "steps": []}

        leg = data["routes"][0]["legs"][0]

        if mode == "walking":
            steps = []
            for s in leg.get("steps", []):
                try:
                    steps.append({
                        "instruction": _strip_html(s.get("html_instructions", "")),
                        "distance": s.get("distance", {}).get("text", ""),
                    })
                except Exception:
                    pass
        else:
            # Transit: one entry per leg (walk segment or transit vehicle)
            steps = []
            for s in leg.get("steps", []):
                try:
                    travel = s.get("travel_mode", "")
                    dur    = s.get("duration", {}).get("text", "")
                    dist   = s.get("distance", {}).get("text", "")
                    if travel == "WALKING":
                        steps.append({
                            "instruction": f"🚶 Walk {dist}" if dist else "🚶 Walk",
                            "distance": dur,
                        })
                    elif travel == "TRANSIT":
                        td        = s.get("transit_details", {})
                        line      = td.get("line", {})
                        name      = line.get("short_name") or line.get("name", "bus/MRT")
                        dep       = td.get("departure_stop", {}).get("name", "")
                        arr       = td.get("arrival_stop", {}).get("name", "")
                        num_stops = td.get("num_stops", "")
                        vehicle   = line.get("vehicle", {}).get("type", "")
                        icon      = "🚇" if vehicle in ("SUBWAY", "RAIL", "HEAVY_RAIL", "TRAM") else "🚌"
                        route     = f" {dep} → {arr}" if dep and arr else ""
                        stops_txt = f" ({num_stops} stops)" if num_stops else ""
                        steps.append({
                            "instruction": f"{icon} Take {name}{route}{stops_txt}",
                            "distance": dur,
                        })
                except Exception:
                    pass

        return {
            "maps_url": maps_url,
            "mode": mode,
            "duration": leg["duration"]["text"],
            "distance": leg["distance"]["text"],
            "steps": steps,
        }
    except Exception:
        return {"maps_url": maps_url, "mode": mode, "duration": None, "distance": None, "steps": []}


async def _geocode_query(address: str, bounds: str, api_key: str) -> Optional[tuple[float, float]]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _GMAPS_GEOCODE,
                params={"address": address, "bounds": bounds, "key": api_key},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            return None
        loc = data["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    except Exception:
        return None


async def _places_search(query: str, api_key: str) -> Optional[tuple[float, float]]:
    """Places Text Search — better than Geocoding for named POIs like LT28, COM1."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _GMAPS_PLACES_SEARCH,
                params={"query": query, "key": api_key},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            return None
        loc = data["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    except Exception:
        return None


async def geocode_sg(query: str) -> Optional[tuple[float, float]]:
    """
    Resolve a free-text location to (lat, lng).
    1. Geocoding API — Singapore-wide (works for addresses, MRT stations, etc.)
    2. Geocoding API — with 'NUS' appended + campus bounds bias
    3. Places Text Search — best for named campus POIs like LT28, COM1, E1A
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return None

    result = await _geocode_query(f"{query}, Singapore", _SG_BOUNDS, api_key)
    if result:
        return result

    result = await _geocode_query(f"{query} NUS, Singapore", _NUS_BOUNDS, api_key)
    if result:
        return result

    # Last resort: Places Text Search handles abbreviations and POI names
    return await _places_search(f"{query} NUS Singapore", api_key)
