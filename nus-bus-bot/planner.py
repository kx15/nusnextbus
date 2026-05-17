import os
from typing import Optional

import httpx

_GMAPS_DIRECTIONS = "https://maps.googleapis.com/maps/api/directions/json"
_GMAPS_GEOCODE    = "https://maps.googleapis.com/maps/api/geocode/json"

# NUS campus bounding box used to bias geocoding results
_NUS_BOUNDS = "1.285,103.765|1.310,103.795"


def _maps_link(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float) -> str:
    return (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_lat},{origin_lng}"
        f"&destination={dest_lat},{dest_lng}"
        f"&travelmode=walking"
    )


async def get_walking_directions(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
) -> dict:
    """
    Returns a dict with maps_url always set, plus duration/distance strings
    if the Directions API key is configured and the call succeeds.
    """
    maps_url = _maps_link(origin_lat, origin_lng, dest_lat, dest_lng)
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return {"maps_url": maps_url, "duration": None, "distance": None}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _GMAPS_DIRECTIONS,
                params={
                    "origin": f"{origin_lat},{origin_lng}",
                    "destination": f"{dest_lat},{dest_lng}",
                    "mode": "walking",
                    "key": api_key,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "OK" or not data.get("routes"):
            return {"maps_url": maps_url, "duration": None, "distance": None}

        leg = data["routes"][0]["legs"][0]
        return {
            "maps_url": maps_url,
            "duration": leg["duration"]["text"],
            "distance": leg["distance"]["text"],
        }
    except Exception:
        return {"maps_url": maps_url, "duration": None, "distance": None}


async def geocode_nus(query: str) -> Optional[tuple[float, float]]:
    """Resolve a free-text NUS location to (lat, lng), biased to campus."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _GMAPS_GEOCODE,
                params={
                    "address": f"{query} NUS Singapore",
                    "bounds": _NUS_BOUNDS,
                    "key": api_key,
                },
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
