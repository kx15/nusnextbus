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

# NUS campus bounding box — covers main campus + UTown + Bukit Timah campus
# CG/OTH/BG-MRT are at ~1.319-1.323, 103.815-103.818 (Bukit Timah)
_NUS_BOUNDS = "1.285,103.765|1.330,103.820"

# Above this straight-line distance, switch from walking to transit mode
_TRANSIT_THRESHOLD_M = 2000


def _maps_link(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float, mode: str = "walking") -> str:
    return (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_lat},{origin_lng}"
        f"&destination={dest_lat},{dest_lng}"
        f"&travelmode={mode}"
    )


def _extract_walking_steps(leg: dict) -> list:
    steps = []
    for s in leg.get("steps", []):
        try:
            steps.append({
                "instruction": _strip_html(s.get("html_instructions", "")),
                "distance": s.get("distance", {}).get("text", ""),
            })
        except Exception:
            pass
    return steps


def _extract_transit_steps(leg: dict) -> list:
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
    return steps


async def _call_directions(origin: str, destination: str, mode: str, api_key: str) -> Optional[dict]:
    """Raw Directions API call. Returns parsed leg dict or None."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _GMAPS_DIRECTIONS,
                params={"origin": origin, "destination": destination, "mode": mode, "key": api_key},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "OK" or not data.get("routes"):
            return None
        return data["routes"][0]["legs"][0]
    except Exception:
        return None


async def get_directions(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
) -> dict:
    """Walking for short trips (< 2 km), transit for longer ones."""
    dist_m = haversine_m(origin_lat, origin_lng, dest_lat, dest_lng)
    mode   = "transit" if dist_m > _TRANSIT_THRESHOLD_M else "walking"
    maps_url = _maps_link(origin_lat, origin_lng, dest_lat, dest_lng, mode)

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return {"maps_url": maps_url, "mode": mode, "duration": None, "distance": None, "steps": []}

    leg = await _call_directions(f"{origin_lat},{origin_lng}", f"{dest_lat},{dest_lng}", mode, api_key)
    if not leg:
        return {"maps_url": maps_url, "mode": mode, "duration": None, "distance": None, "steps": []}

    steps = _extract_walking_steps(leg) if mode == "walking" else _extract_transit_steps(leg)
    return {
        "maps_url": maps_url,
        "mode": mode,
        "duration": leg.get("duration", {}).get("text"),
        "distance": leg.get("distance", {}).get("text"),
        "distance_m": leg.get("distance", {}).get("value", 0),  # raw metres
        "steps": steps,
    }


async def get_transit_to_stop(
    origin_lat: float,
    origin_lng: float,
    dest_address: str,
    dest_lat: float,
    dest_lng: float,
) -> dict:
    """
    Transit directions to a named stop/address (much more reliable than GPS coords
    for MRT stations). Falls back to walking if transit returns no results.
    """
    maps_url = _maps_link(origin_lat, origin_lng, dest_lat, dest_lng, "transit")
    api_key  = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return {"maps_url": maps_url, "mode": "transit", "duration": None, "distance": None, "steps": []}

    origin = f"{origin_lat},{origin_lng}"

    leg = await _call_directions(origin, dest_address, "transit", api_key)
    if not leg:
        # Fallback: try walking (user might be very close)
        leg = await _call_directions(origin, dest_address, "walking", api_key)
        if not leg:
            return {"maps_url": maps_url, "mode": "transit", "duration": None, "distance": None, "steps": []}
        steps = _extract_walking_steps(leg)
        mode  = "walking"
    else:
        steps = _extract_transit_steps(leg)
        mode  = "transit"

    return {
        "maps_url": maps_url,
        "mode": mode,
        "duration": leg.get("duration", {}).get("text"),
        "distance": leg.get("distance", {}).get("text"),
        "distance_m": leg.get("distance", {}).get("value", 0),
        "steps": steps,
    }


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


# NUS campus bounding box — results inside this are preferred over off-campus matches
_NUS_LAT = (1.285, 1.330)
_NUS_LNG = (103.765, 103.820)


def _on_campus(lat: float, lng: float) -> bool:
    return _NUS_LAT[0] <= lat <= _NUS_LAT[1] and _NUS_LNG[0] <= lng <= _NUS_LNG[1]


def _building_code_expansions(query: str) -> list[str]:
    """
    Return alternative search strings for short NUS building/room codes.
    e.g. "lt24" → ["Lecture Theatre 24 NUS Singapore", "LT 24 NUS Singapore"]
         "as6"  → ["AS 6 NUS Singapore"]
         "e3a"  → ["Engineering Block E3A NUS Singapore"]
    """
    import re as _re
    q = query.strip().upper()
    exps: list[str] = []

    m = _re.match(r'^LT(\d+[A-Z]?)$', q)
    if m:
        exps += [f"Lecture Theatre {m.group(1)} NUS Singapore",
                 f"LT {m.group(1)} NUS Singapore"]
        return exps

    m = _re.match(r'^E(\d+[A-Z]?)$', q)
    if m:
        exps += [f"NUS Engineering Block E{m.group(1)} Singapore",
                 f"E {m.group(1)} NUS Singapore"]
        return exps

    m = _re.match(r'^S(\d+[A-Z]?)$', q)
    if m:
        exps += [f"NUS Science Block S{m.group(1)} Singapore",
                 f"S {m.group(1)} NUS Singapore"]
        return exps

    # Auditorium N → UTown (numbered NUS auditoriums are in UTown)
    m = _re.match(r'^AUDITORIUM\s*(\d+[A-Z]?)$', q)
    if m:
        exps += [f"UTown Auditorium {m.group(1)} NUS Singapore",
                 f"Auditorium {m.group(1)} University Town NUS Singapore"]
        return exps

    # Generic: letters + digits (e.g. AS6, COM1)
    m = _re.match(r'^([A-Z]+)(\d+[A-Z]?)$', q)
    if m:
        exps.append(f"{m.group(1)} {m.group(2)} NUS Singapore")

    return exps


async def geocode_with_candidates(
    query: str,
) -> tuple[Optional[tuple[float, float]], list[dict]]:
    """
    Resolve a query to (best_lat_lng, candidates).
    candidates is a non-empty list of {"lat","lng","label"} dicts only when
    an on-campus and an off-campus result both exist and are >300 m apart.
    In that case the caller should ask the user to pick one.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return None, []

    sg     = await _geocode_query(f"{query}, Singapore",      _SG_BOUNDS,  api_key)
    nus    = await _geocode_query(f"{query} NUS, Singapore",  _NUS_BOUNDS, api_key)
    places = await _places_search(f"{query} NUS Singapore", api_key)

    # Try building-code expansions via geocoding API (works even when Places is blocked).
    # Run these BEFORE accepting the generic NUS result — the expanded query is more
    # specific and may correct cases like "auditorium 3 NUS" → UHALL (wrong).
    expansion_result: Optional[tuple[float, float]] = None
    for alt in _building_code_expansions(query):
        r = await _geocode_query(alt, _NUS_BOUNDS, api_key)
        if r and _on_campus(*r):
            expansion_result = r
            break
    if expansion_result:
        places = expansion_result  # prefer specific expansion over generic nus result

    import re as _re
    on_campus  = next((r for r in [sg, nus, places] if r and _on_campus(*r)), None)
    off_campus = sg if sg and not _on_campus(*sg) else None

    # Building codes (LT24, AS6, E3A, COM1 …) are unambiguously NUS — no disambiguation
    is_building_code = bool(_re.match(r'^[A-Za-z]{1,4}\d+[A-Za-z]?$', query.strip()))

    if on_campus and off_campus and not is_building_code:
        dist = haversine_m(on_campus[0], on_campus[1], off_campus[0], off_campus[1])
        if dist > 300:
            return on_campus, [
                {"lat": on_campus[0],  "lng": on_campus[1],  "label": f"{query.title()} (NUS campus)"},
                {"lat": off_campus[0], "lng": off_campus[1], "label": f"{query.title()} (outside NUS)"},
            ]

    best = on_campus or sg or nus or places
    return best, []


async def geocode_sg(query: str) -> Optional[tuple[float, float]]:
    """
    Resolve a free-text location to (lat, lng).

    Strategy:
    1. Singapore-wide geocoding — if the result is ON campus, accept immediately.
    2. NUS-biased geocoding — if stage 1 returned nothing or an off-campus result,
       try again with 'NUS' appended and campus bounds. Accept if on campus.
    3. Places Text Search — for abbreviations / POI codes (LT28, COM1, Saga College).
    4. Fall back to the off-campus stage-1 result if nothing better was found
       (so off-campus destinations like Orchard MRT still work).
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return None

    sg_result = await _geocode_query(f"{query}, Singapore", _SG_BOUNDS, api_key)
    if sg_result and _on_campus(*sg_result):
        return sg_result  # On-campus hit — done

    # Try building-code / keyword expansions FIRST — they're more specific than
    # the generic "X NUS, Singapore" query (which can return wrong campus buildings).
    # e.g. "Auditorium 3 NUS" → UHALL (wrong); "UTown Auditorium 3 NUS" → correct.
    for alt in _building_code_expansions(query):
        r = await _geocode_query(alt, _NUS_BOUNDS, api_key)
        if r and _on_campus(*r):
            return r

    # Stage-1 returned off-campus (or nothing). Try generic NUS search.
    nus_result = await _geocode_query(f"{query} NUS, Singapore", _NUS_BOUNDS, api_key)
    if nus_result and _on_campus(*nus_result):
        return nus_result

    places_result = await _places_search(f"{query} NUS Singapore", api_key)
    if places_result and _on_campus(*places_result):
        return places_result

    # Nothing on campus found — accept off-campus stage-1 result if it exists
    return sg_result or nus_result or places_result
