"""Campus route-planning engine: NUS shuttle routing, transfers, companion-stop
crossings, and Bukit Timah gateway logic.

Pure of Telegram concerns — every function either returns data or appends
formatted strings to a ``lines`` list that the caller (bot.py) sends. Network
calls go through ``api``/``planner`` so they can be monkeypatched in tests.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from api import get_arrivals_async
from planner import get_directions, get_transit_to_stop, haversine_m
from routes import (
    _BUKIT_TIMAH_STOPS,
    _BUS_P_HUBS_ARRIVAL,
    _BUS_P_HUBS_DEPARTURE,
    _COMPANION_STOPS,
    _GATEWAYS,
    _NUS_ROUTES,
)
from stops import STOPS, find_stop

logger = logging.getLogger(__name__)


def _fmt_time(mins: str) -> str:
    if not mins or mins == "-":
        return "–"
    if mins.lower() == "arr":
        return "🏃‍♂️RUN"
    try:
        m = int(mins)
        if m > 30:
            eta = datetime.now(timezone(timedelta(hours=8))) + timedelta(minutes=m)
            return f"~{eta.strftime('%H:%M')}"
        return f"{m} min"
    except ValueError:
        return f"{mins} min"


def _fmt_steps(lines: list, steps: list, indent: str = "") -> None:
    for i, step in enumerate(steps, 1):
        lines.append(f"{indent}{i}. {step['instruction']} _({step['distance']})_")


def _append_directions_block(lines: list, directions) -> None:
    if isinstance(directions, Exception) or not directions:
        return
    mode = directions.get("mode", "walking")
    icon = "🚇" if mode == "transit" else "🚶"
    if directions.get("duration"):
        lines.append(f"{icon} *{mode}*: {directions['distance']} · {directions['duration']}")
    _fmt_steps(lines, directions.get("steps", []))
    if directions.get("steps"):
        lines.append("")
    lines.append(f"[open in Google Maps]({directions['maps_url']})")


def _fmt_nus_shuttle(bus_name: str, board_stop: dict, alight_stop: dict,
                     arrival: str, next_arrival: str) -> str:
    stops = _nus_stops_between(bus_name, board_stop["name"], alight_stop["name"])
    stops_txt = f" · {stops} stop{'s' if stops != 1 else ''}" if stops else ""
    return (
        f"🚌 *{bus_name}*{stops_txt}  "
        f"{_fmt_time(arrival)} | Next: {_fmt_time(next_arrival)}"
    )


def _nus_stops_between(bus: str, board: str, alight: str) -> int | None:
    """Return number of stops between board and alight for a given NUS bus, or None."""
    route = _NUS_ROUTES.get(bus, [])
    best: int | None = None
    start = 0
    while True:
        try:
            i = route.index(board, start)
        except ValueError:
            break
        try:
            j = route.index(alight, i + 1)
            gap = j - i
            if best is None or gap < best:
                best = gap
        except ValueError:
            pass
        start = i + 1
    return best


def _best_dest_stop(o_stop_name: str, candidates: list) -> dict | None:
    """
    Given multiple nearby destination stop candidates, return the one reachable
    in the fewest bus stops from o_stop_name.  Falls back to nearest if no route
    data exists for any candidate.
    """
    best_stop  = candidates[0]
    best_count = 10_000
    for c in candidates:
        for bus in _NUS_ROUTES:
            n = _nus_stops_between(bus, o_stop_name, c["name"])
            if n is not None and n < best_count:
                best_count = n
                best_stop  = c
    return best_stop


def _find_transfers(origin_name: str, dest_name: str) -> list[tuple[str, str, str, int]]:
    """Find 1-transfer journeys (bus1, transfer_stop, bus2, total_stops), fewest stops first."""
    seen: dict[tuple[str, str, str], int] = {}
    for stop in STOPS:
        mid = stop["name"]
        if mid in (origin_name, dest_name):
            continue
        for bus1 in _NUS_ROUTES:
            n1 = _nus_stops_between(bus1, origin_name, mid)
            if n1 is None:
                continue
            for bus2 in _NUS_ROUTES:
                n2 = _nus_stops_between(bus2, mid, dest_name)
                if n2 is None:
                    continue
                key = (bus1, mid, bus2)
                total = n1 + n2
                if key not in seen or total < seen[key]:
                    seen[key] = total
    return sorted(
        [(b1, mid, b2, sc) for (b1, mid, b2), sc in seen.items()],
        key=lambda x: x[3],
    )


async def _route_on_campus(
    lines: list,
    origin: dict,
    origin_loc: tuple,
    dest_stop: dict,
    dest_lat: float,
    dest_lng: float,
    dest_is_exact_stop: bool,
    dest_label: str = "",
) -> None:
    """On-campus → on-campus: NUS shuttle + walk."""
    is_bt_dest   = dest_stop["name"] in _BUKIT_TIMAH_STOPS
    is_bt_origin = origin["name"] in _BUKIT_TIMAH_STOPS
    is_bt = is_bt_dest or is_bt_origin

    # Choose hub order based on direction of travel
    _bt_hubs = (_BUS_P_HUBS_DEPARTURE if is_bt_origin else _BUS_P_HUBS_ARRIVAL) if is_bt else []
    # Deduplicate while preserving order (in case lists overlap)
    seen: set = set()
    all_hubs: list = []
    for h in _bt_hubs:
        if h not in seen:
            seen.add(h)
            all_hubs.append(h)

    # Include companion stops (opposite side of road) so they can be scored too
    companion_names = [
        _COMPANION_STOPS[h] for h in all_hubs
        if h in _COMPANION_STOPS and _COMPANION_STOPS[h] not in all_hubs
    ]
    all_fetch_hubs = all_hubs + companion_names

    # Fetch arrival data — hub stops + their companions
    fetch_names = [origin["name"], dest_stop["name"]] + all_fetch_hubs
    results = await asyncio.gather(
        *[get_arrivals_async(n) for n in fetch_names],
        return_exceptions=True,
    )
    origin_arrivals = results[0]
    dest_arrivals   = results[1]
    hub_arrivals    = {name: results[2 + i] for i, name in enumerate(all_fetch_hubs)}

    origin_names: set = set()
    if not isinstance(origin_arrivals, Exception):
        origin_names = {t.name for t in origin_arrivals.timings if not t.name.strip().isdigit()}

    dest_names: set = set()
    if not isinstance(dest_arrivals, Exception):
        dest_names = {t.name for t in dest_arrivals.timings if not t.name.strip().isdigit()}

    # Build live timing map from origin arrivals
    live_timing: dict = {}
    if not isinstance(origin_arrivals, Exception):
        for t in origin_arrivals.timings:
            if not t.name.strip().isdigit():
                live_timing[t.name] = t

    # All buses that serve origin→dest in correct direction, from route data.
    # Using route data (not just live API) ensures we list every option even
    # when a bus isn't actively arriving at the exact moment of the query.
    route_buses = sorted(
        bus for bus in _NUS_ROUTES
        if _nus_stops_between(bus, origin["name"], dest_stop["name"]) is not None
    )

    # Fallback: if no route data covers this pair, use live common buses
    if not route_buses:
        route_buses = sorted(
            bus for bus in (origin_names & dest_names)
            if _nus_stops_between(bus, origin["name"], dest_stop["name"]) is not None
        )

    common = set(route_buses)

    origin_addr = quote(f"{origin['caption']} NUS Singapore")
    maps_url = (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_addr}"
        f"&destination={dest_lat},{dest_lng}"
        f"&travelmode={'transit' if is_bt else 'walking'}"
    )

    transit_url = (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_loc[0]},{origin_loc[1]}"
        f"&destination={dest_lat},{dest_lng}&travelmode=transit"
    )

    if common:
        _direct_min = min(
            _nus_stops_between(bus, origin["name"], dest_stop["name"]) for bus in route_buses
        )
        # Option A: cross origin's companion, then direct to dest
        _co = _COMPANION_STOPS.get(origin["name"])
        _co_stop = find_stop(_co) if _co else None
        _co_direct: list[str] = sorted(
            bus for bus in _NUS_ROUTES
            if _co and _nus_stops_between(bus, _co, dest_stop["name"]) is not None
        ) if _co else []
        _co_min = min(
            _nus_stops_between(bus, _co, dest_stop["name"]) for bus in _co_direct
        ) if _co_direct else 999
        # Option B: direct to dest's companion, then cross road
        _dest_co = _COMPANION_STOPS.get(dest_stop["name"])
        _dest_co_stop = find_stop(_dest_co) if _dest_co else None
        _dest_co_buses: list[str] = sorted(
            bus for bus in _NUS_ROUTES
            if _dest_co and _nus_stops_between(bus, origin["name"], _dest_co) is not None
        ) if _dest_co else []
        _dest_co_min = min(
            _nus_stops_between(bus, origin["name"], _dest_co)
            for bus in _dest_co_buses
        ) if _dest_co_buses else 999

        if _co_direct and _co_min < _direct_min and _co_min <= _dest_co_min:
            # Cross origin's companion → direct to dest
            _live_co: dict = {}
            try:
                _co_arr = await get_arrivals_async(_co)
                _live_co = {t.name: t for t in _co_arr.timings if not t.name.strip().isdigit()}
            except Exception:
                pass
            lines.append(f"_cross the road to {_co_stop['caption']}_")
            lines.append("")
            lines.append(f"🚌 *NUS shuttle: {_co_stop['caption']} → {dest_stop['caption']}*")
            for bus_name in _co_direct:
                t = _live_co.get(bus_name)
                lines.append("  " + _fmt_nus_shuttle(bus_name, _co_stop, dest_stop,
                                                      t.arrival_time if t else "-",
                                                      t.next_arrival_time if t else "-"))
            lines.append("")
            if not dest_is_exact_stop:
                walk = await get_directions(dest_stop["lat"], dest_stop["lng"], dest_lat, dest_lng)
                if not isinstance(walk, Exception) and walk.get("duration"):
                    lines.append(f"*Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
                    _fmt_steps(lines, walk.get("steps", []))
                    lines.append("")
            lines.append(f"[open in Google Maps]({maps_url})")

        elif _dest_co_buses and _dest_co_min < _direct_min:
            # Direct to dest's companion → cross road to dest
            lines.append(f"🚌 *NUS shuttle: {origin['caption']} → {_dest_co_stop['caption']}*")
            for bus_name in _dest_co_buses:
                t = live_timing.get(bus_name)
                lines.append("  " + _fmt_nus_shuttle(bus_name, origin, _dest_co_stop,
                                                      t.arrival_time if t else "-",
                                                      t.next_arrival_time if t else "-"))
            lines.append(f"  _cross the road to {dest_stop['caption']}_")
            lines.append("")
            if not dest_is_exact_stop:
                walk = await get_directions(dest_stop["lat"], dest_stop["lng"], dest_lat, dest_lng)
                if not isinstance(walk, Exception) and walk.get("duration"):
                    lines.append(f"*Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
                    _fmt_steps(lines, walk.get("steps", []))
                    lines.append("")
            lines.append(f"[open in Google Maps]({maps_url})")

        else:
            # Show all valid buses; use live timing where available
            lines.append(f"🚌 *NUS shuttle: {origin['caption']} → {dest_stop['caption']}*")
            for bus_name in route_buses:
                t = live_timing.get(bus_name)
                arr  = t.arrival_time      if t else "-"
                nxt  = t.next_arrival_time if t else "-"
                lines.append("  " + _fmt_nus_shuttle(bus_name, origin, dest_stop, arr, nxt))
            lines.append("")
            if not dest_is_exact_stop:
                walk = await get_directions(dest_stop["lat"], dest_stop["lng"], dest_lat, dest_lng)
                if not isinstance(walk, Exception) and walk.get("duration"):
                    lines.append(f"*Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
                    _fmt_steps(lines, walk.get("steps", []))
                    lines.append("")
            lines.append(f"[open in Google Maps]({maps_url})")

    elif is_bt_origin and not is_bt_dest:
        # Departing from Bukit Timah campus: Bus P to best hub, then shuttle to dest.
        # Score each hub by: P stops to hub + fewest connecting bus stops to dest.
        _dest_comp_name = _COMPANION_STOPS.get(dest_stop["name"])
        best: dict | None = None
        best_score = 10_000

        for hub_name in all_hubs:
            hub_stop = find_stop(hub_name)
            hub_arr  = hub_arrivals.get(hub_name)
            if not hub_stop or isinstance(hub_arr, Exception):
                continue
            hub_names = {t.name for t in hub_arr.timings if not t.name.strip().isdigit()}
            if "P" not in origin_names or "P" not in hub_names:
                continue

            p_to_hub = _nus_stops_between("P", origin["name"], hub_name) or 999

            # Score direct connections from hub
            to_dest_direct = hub_names & dest_names
            min_direct = min(
                (_nus_stops_between(bus, hub_name, dest_stop["name"]) or 999)
                for bus in to_dest_direct
            ) if to_dest_direct else 999

            # Score via companion stop (cross the road — same physical location)
            comp_name = _COMPANION_STOPS.get(hub_name)
            comp_arr  = hub_arrivals.get(comp_name) if comp_name else None

            # Destination IS the companion of this hub — P to hub then cross road, no step 2 bus
            if comp_name == dest_stop["name"]:
                score = p_to_hub
                if score < best_score:
                    best_score = score
                    best = {
                        "hub": hub_stop, "hub_arr": hub_arr, "hub_name": hub_name,
                        "to_dest": set(), "use_companion": True,
                        "comp_name": comp_name, "comp_arr": comp_arr,
                        "direct_to_dest": True,
                    }
                continue

            to_dest_comp = set()
            min_comp = 999
            if comp_name and comp_arr and not isinstance(comp_arr, Exception):
                comp_bus_names = {t.name for t in comp_arr.timings if not t.name.strip().isdigit()}
                # Use route reachability (not just live dest arrivals) so buses that
                # serve only the companion of dest (e.g. A2 → TCOMS not TCOMS-OPP) are included.
                to_dest_comp = {
                    bus for bus in comp_bus_names
                    if (_nus_stops_between(bus, comp_name, dest_stop["name"]) is not None
                        or (_dest_comp_name
                            and _nus_stops_between(bus, comp_name, _dest_comp_name) is not None))
                }
                def _comp_n(bus: str, comp_name=comp_name, _dest_comp_name=_dest_comp_name) -> int:
                    n = _nus_stops_between(bus, comp_name, dest_stop["name"])
                    if n is None and _dest_comp_name:
                        n = _nus_stops_between(bus, comp_name, _dest_comp_name)
                    return n or 999
                min_comp = min(_comp_n(bus) for bus in to_dest_comp) if to_dest_comp else 999

            use_companion = (min_comp < min_direct) and to_dest_comp
            min_conn      = min_comp if use_companion else min_direct
            to_dest       = to_dest_comp if use_companion else to_dest_direct

            if not to_dest:
                continue

            score = p_to_hub + min_conn
            if score < best_score:
                best_score = score
                best = {
                    "hub": hub_stop, "hub_arr": hub_arr, "hub_name": hub_name,
                    "to_dest": to_dest, "use_companion": use_companion,
                    "comp_name": comp_name, "comp_arr": comp_arr,
                }

        transfer_shown = bool(best)
        if best:
            hub_stop       = best["hub"]
            hub_arr        = best["hub_arr"]
            hub_name       = best["hub_name"]
            to_dest        = best["to_dest"]
            use_companion  = best["use_companion"]
            direct_to_dest = best.get("direct_to_dest", False)
            step2_name     = best["comp_name"] if use_companion else hub_name
            step2_stop     = find_stop(step2_name) or hub_stop
            step2_arr      = best["comp_arr"]   if use_companion else hub_arr

            # Step 1: Bus P from origin to primary hub
            # Collect all P entries — API sometimes returns one per vehicle.
            # Use T1 of first, and T1 of second vehicle as Next when next_arrival is blank.
            p_timings = [t for t in origin_arrivals.timings if t.name == "P"]
            if p_timings:
                t1  = p_timings[0]
                arr = t1.arrival_time
                nxt = (t1.next_arrival_time
                       if t1.next_arrival_time and t1.next_arrival_time != "-"
                       else (p_timings[1].arrival_time if len(p_timings) > 1 else "-"))
                lines.append(f"*① {origin['caption']} → {hub_stop['caption']} (Bus P)*")
                lines.append("  " + _fmt_nus_shuttle("P", origin, hub_stop, arr, nxt))

            if direct_to_dest:
                # Destination is the companion of the hub — just cross the road
                lines.append(f"  _cross the road to {dest_stop['caption']}_")
                lines.append("")
                lines.append(f"[open in Google Maps]({maps_url})")
            else:
                if use_companion:
                    lines.append(f"  _cross the road to {step2_stop['caption']}_")
                lines.append("")

                # Step 2: All valid connecting buses (sorted by fewest stops)
                live_step2: dict = {}
                if step2_arr and not isinstance(step2_arr, Exception):
                    for t in step2_arr.timings:
                        if not t.name.strip().isdigit():
                            live_step2[t.name] = t
                def _eff_stops(b: str) -> int:
                    n = _nus_stops_between(b, step2_name, dest_stop["name"])
                    if n is None and _dest_comp_name:
                        n = _nus_stops_between(b, step2_name, _dest_comp_name)
                    return n or 999

                conn_buses = sorted(to_dest, key=_eff_stops)

                # Compute effective alight stop per bus (companion of dest when bus doesn't serve dest directly)
                _dest_comp_stop = find_stop(_dest_comp_name) if _dest_comp_name else None
                bus_entries: list[tuple[str, dict]] = []
                for bus_name in conn_buses:
                    if (_dest_comp_stop
                            and _nus_stops_between(bus_name, step2_name, dest_stop["name"]) is None
                            and _nus_stops_between(bus_name, step2_name, _dest_comp_name) is not None):
                        bus_entries.append((bus_name, _dest_comp_stop))
                    else:
                        bus_entries.append((bus_name, dest_stop))

                # Header uses the alight stop of the first bus
                _header_alight = bus_entries[0][1] if bus_entries else dest_stop
                lines.append(f"*② {step2_stop['caption']} → {_header_alight['caption']}*")
                for bus_name, eff_alight in bus_entries:
                    td  = live_step2.get(bus_name)
                    arr = td.arrival_time      if td else "-"
                    nxt = td.next_arrival_time if td else "-"
                    lines.append("  " + _fmt_nus_shuttle(bus_name, step2_stop, eff_alight, arr, nxt))
                lines.append("")
                lines.append(f"[open in Google Maps]({maps_url})")

        if not transfer_shown:
            lines.append("Bus P not available right now 💀\n")
            lines.append("🚇 *take public transport instead:*")
            lines.append(f"[MRT/bus options in Google Maps]({transit_url})")

    elif is_bt_dest:
        # Arriving at Bukit Timah campus
        transfer_shown = False

        # Direct Bus P from origin — only if P travels origin→dest in forward direction
        if ("P" in origin_names and "P" in dest_names
                and _nus_stops_between("P", origin["name"], dest_stop["name"]) is not None):
            p_timings = [t for t in origin_arrivals.timings if t.name == "P"]
            if p_timings:
                t1  = p_timings[0]
                arr = t1.arrival_time
                nxt = (t1.next_arrival_time
                       if t1.next_arrival_time and t1.next_arrival_time != "-"
                       else (p_timings[1].arrival_time if len(p_timings) > 1 else "-"))
                lines.append(f"🚌 *NUS shuttle: {origin['caption']} → {dest_stop['caption']} (Bus P)*")
                lines.append("  " + _fmt_nus_shuttle("P", origin, dest_stop, arr, nxt))
                lines.append("")
                lines.append(f"[open in Google Maps]({maps_url})")
                transfer_shown = True

        # No direct P — try via a hub stop (skip if origin == hub)
        if not transfer_shown:
            for hub_name in all_hubs:
                hub_stop = find_stop(hub_name)
                hub_arr  = hub_arrivals.get(hub_name)
                if not hub_stop or isinstance(hub_arr, Exception):
                    continue
                if hub_name == origin["name"]:   # origin IS the hub — already handled above
                    continue
                hub_names = {t.name for t in hub_arr.timings if not t.name.strip().isdigit()}
                to_hub    = origin_names & hub_names
                # Verify P travels hub→dest in forward direction
                if (not to_hub or "P" not in hub_names
                        or _nus_stops_between("P", hub_name, dest_stop["name"]) is None):
                    continue
                # Get P timing at hub (may be "–" if between runs)
                p_hub_timing = next((t for t in hub_arr.timings if t.name == "P"), None)
                if not p_hub_timing:
                    continue

                step1 = sorted(to_hub)[0]
                step1_timing = next((t for t in origin_arrivals.timings if t.name == step1), None)
                if step1_timing:
                    lines.append(f"*① {origin['caption']} → {hub_stop['caption']}*")
                    lines.append("  " + _fmt_nus_shuttle(step1, origin, hub_stop,
                                                          step1_timing.arrival_time,
                                                          step1_timing.next_arrival_time))
                lines.append("")

                lines.append(f"*② {hub_stop['caption']} → {dest_stop['caption']} (Bus P)*")
                lines.append("  " + _fmt_nus_shuttle("P", hub_stop, dest_stop,
                                                      p_hub_timing.arrival_time,
                                                      p_hub_timing.next_arrival_time))
                lines.append("")
                lines.append(f"[open in Google Maps]({maps_url})")
                transfer_shown = True
                break

        if not transfer_shown:
            if is_bt_origin:
                # Both stops are Bukit Timah campus — Bus P goes the wrong way; just walk
                walk_bt = await get_directions(origin_loc[0], origin_loc[1], dest_lat, dest_lng)
                if walk_bt and not isinstance(walk_bt, Exception) and walk_bt.get("duration"):
                    lines.append(f"🚶 *walk*: {walk_bt['distance']} · {walk_bt['duration']}")
                    _fmt_steps(lines, walk_bt.get("steps", []))
                    lines.append("")
                lines.append(f"[open in Google Maps]({maps_url})")
            else:
                lines.append("Bus P not available right now 💀\n")
                lines.append("🚇 *take public transport instead:*")
                lines.append(f"[MRT/bus options in Google Maps]({transit_url})")

    else:
        # If the stops are within walking distance, don't bother with buses
        if haversine_m(origin_loc[0], origin_loc[1], dest_lat, dest_lng) < 500:
            walk = await get_directions(origin_loc[0], origin_loc[1], dest_lat, dest_lng)
            if walk and not isinstance(walk, Exception) and walk.get("duration"):
                lines.append(f"🚶 *walk*: {walk['distance']} · {walk['duration']}")
                _fmt_steps(lines, walk.get("steps", []))
                lines.append("")
            lines.append(f"[open in Google Maps]({maps_url})")
            return

        # Check if crossing to the origin's companion stop gives a direct bus (fewer stops than any transfer)
        _orig_comp_name = _COMPANION_STOPS.get(origin["name"])
        _orig_comp_stop = find_stop(_orig_comp_name) if _orig_comp_name else None
        _comp_direct: list[str] = []
        _comp_min: int = 999
        if _orig_comp_name:
            _comp_direct = sorted(
                bus for bus in _NUS_ROUTES
                if _nus_stops_between(bus, _orig_comp_name, dest_stop["name"]) is not None
            )
            if _comp_direct:
                _comp_min = min(
                    _nus_stops_between(bus, _orig_comp_name, dest_stop["name"])
                    for bus in _comp_direct
                )

        # No direct NUS bus (non-BT): try transfers from origin and from companion
        transfers = _find_transfers(origin["name"], dest_stop["name"])
        transfer_min = transfers[0][3] if transfers else 999
        comp_transfers = _find_transfers(_orig_comp_name, dest_stop["name"]) if _orig_comp_name else []
        comp_transfer_min = comp_transfers[0][3] if comp_transfers else 999

        if _comp_direct and _comp_min <= min(transfer_min, comp_transfer_min):
            # Crossing road gives a direct or better route
            _comp_arr = await get_arrivals_async(_orig_comp_name)
            _live_comp: dict = {}
            if not isinstance(_comp_arr, Exception):
                _live_comp = {t.name: t for t in _comp_arr.timings if not t.name.strip().isdigit()}
            lines.append(f"_cross the road to {_orig_comp_stop['caption']}_")
            lines.append("")
            lines.append(f"🚌 *NUS shuttle: {_orig_comp_stop['caption']} → {dest_stop['caption']}*")
            for _bus in _comp_direct:
                _t = _live_comp.get(_bus)
                lines.append("  " + _fmt_nus_shuttle(
                    _bus, _orig_comp_stop, dest_stop,
                    _t.arrival_time if _t else "-",
                    _t.next_arrival_time if _t else "-",
                ))
            lines.append("")
            if not dest_is_exact_stop:
                walk = await get_directions(dest_stop["lat"], dest_stop["lng"], dest_lat, dest_lng)
                if not isinstance(walk, Exception) and walk.get("duration"):
                    lines.append(f"*Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
                    _fmt_steps(lines, walk.get("steps", []))
                    lines.append("")
            lines.append(f"[open in Google Maps]({maps_url})")

        elif comp_transfers and comp_transfer_min < transfer_min:
            # Crossing to companion gives a better 1-transfer route
            _comp_arr = await get_arrivals_async(_orig_comp_name)
            _live_comp_timing: dict = {}
            if not isinstance(_comp_arr, Exception):
                for t in _comp_arr.timings:
                    if not t.name.strip().isdigit():
                        _live_comp_timing[t.name] = t

            def _earliest_comp_b1(item: tuple) -> float:
                b1 = item[0]
                t = _live_comp_timing.get(b1)
                if not t or not t.arrival_time or t.arrival_time == "-":
                    return float("inf")
                if t.arrival_time.lower() == "arr":
                    return 0.0
                try:
                    return float(t.arrival_time)
                except ValueError:
                    return float("inf")

            comp_tied = [t for t in comp_transfers if t[3] == comp_transfer_min]
            comp_options = sorted(comp_tied, key=_earliest_comp_b1)[:2]
            unique_comp_mids = list({mid for _, mid, _, _ in comp_options})
            comp_mid_results = await asyncio.gather(
                *[get_arrivals_async(m) for m in unique_comp_mids],
                return_exceptions=True,
            )
            live_comp_mid_map: dict[str, dict] = {}
            for mid_name, result in zip(unique_comp_mids, comp_mid_results):
                timing_map: dict = {}
                if not isinstance(result, Exception):
                    for t in result.timings:
                        if not t.name.strip().isdigit():
                            timing_map[t.name] = t
                live_comp_mid_map[mid_name] = timing_map

            lines.append(f"_cross the road to {_orig_comp_stop['caption']}_")
            lines.append("")
            comp_labels = ["*Option A*", "*Option B*"] if len(comp_options) > 1 else [""]
            for label, (bus1, mid_name, bus2, _) in zip(comp_labels, comp_options):
                mid_stop = find_stop(mid_name)
                live_mid = live_comp_mid_map.get(mid_name, {})
                t1 = _live_comp_timing.get(bus1)
                t2 = live_mid.get(bus2)
                if label:
                    lines.append(label)
                lines.append(f"① {_orig_comp_stop['caption']} → {mid_stop['caption']}")
                lines.append("  " + _fmt_nus_shuttle(bus1, _orig_comp_stop, mid_stop,
                                                      t1.arrival_time if t1 else "-",
                                                      t1.next_arrival_time if t1 else "-"))
                lines.append(f"② {mid_stop['caption']} → {dest_stop['caption']}")
                lines.append("  " + _fmt_nus_shuttle(bus2, mid_stop, dest_stop,
                                                      t2.arrival_time if t2 else "-",
                                                      t2.next_arrival_time if t2 else "-"))
                lines.append("")
            if not dest_is_exact_stop:
                walk = await get_directions(dest_stop["lat"], dest_stop["lng"], dest_lat, dest_lng)
                if not isinstance(walk, Exception) and walk.get("duration"):
                    lines.append(f"*Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
                    _fmt_steps(lines, walk.get("steps", []))
                    lines.append("")
            lines.append(f"[open in Google Maps]({maps_url})")

        elif transfers:
            min_stops = transfers[0][3]
            tied = [t for t in transfers if t[3] == min_stops]

            def _earliest_bus1(item: tuple) -> float:
                b1 = item[0]
                t = live_timing.get(b1)
                if not t or not t.arrival_time or t.arrival_time == "-":
                    return float("inf")
                if t.arrival_time.lower() == "arr":
                    return 0.0
                try:
                    return float(t.arrival_time)
                except ValueError:
                    return float("inf")

            options = sorted(tied, key=_earliest_bus1)[:2]

            # Fetch all unique transfer stops in parallel
            unique_mids = list({mid for _, mid, _, _ in options})
            mid_results = await asyncio.gather(
                *[get_arrivals_async(m) for m in unique_mids],
                return_exceptions=True,
            )
            live_mid_map: dict[str, dict] = {}
            for mid_name, result in zip(unique_mids, mid_results):
                timing_map: dict = {}
                if not isinstance(result, Exception):
                    for t in result.timings:
                        if not t.name.strip().isdigit():
                            timing_map[t.name] = t
                live_mid_map[mid_name] = timing_map

            labels = ["*Option A*", "*Option B*"] if len(options) > 1 else [""]
            for label, (bus1, mid_name, bus2, _) in zip(labels, options):
                mid_stop = find_stop(mid_name)
                live_mid = live_mid_map.get(mid_name, {})
                t1 = live_timing.get(bus1)
                t2 = live_mid.get(bus2)
                if label:
                    lines.append(label)
                lines.append(f"① {origin['caption']} → {mid_stop['caption']}")
                lines.append("  " + _fmt_nus_shuttle(bus1, origin, mid_stop,
                                                      t1.arrival_time if t1 else "-",
                                                      t1.next_arrival_time if t1 else "-"))
                lines.append(f"② {mid_stop['caption']} → {dest_stop['caption']}")
                lines.append("  " + _fmt_nus_shuttle(bus2, mid_stop, dest_stop,
                                                      t2.arrival_time if t2 else "-",
                                                      t2.next_arrival_time if t2 else "-"))
                lines.append("")

            if not dest_is_exact_stop:
                walk = await get_directions(dest_stop["lat"], dest_stop["lng"], dest_lat, dest_lng)
                if not isinstance(walk, Exception) and walk.get("duration"):
                    lines.append(f"*Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
                    _fmt_steps(lines, walk.get("steps", []))
                    lines.append("")
            lines.append(f"[open in Google Maps]({maps_url})")
        else:
            # Truly no NUS bus option: walk or public transit
            walk = await get_directions(origin_loc[0], origin_loc[1], dest_lat, dest_lng)
            walk_m = walk.get("distance_m", 0) if (walk and not isinstance(walk, Exception)) else 0
            if walk_m > 1000:
                lines.append("no direct NUS bus and it's quite far to walk 💀\n")
                lines.append("🚌 *take a public bus instead:*")
                lines.append(f"[public transport options in Google Maps]({transit_url})")
            else:
                lines.append("no direct NUS bus — walking instead 🚶\n")
                if walk and not isinstance(walk, Exception) and walk.get("duration"):
                    lines.append(f"🚶 *walk*: {walk['distance']} · {walk['duration']}")
                    _fmt_steps(lines, walk.get("steps", []))
                    lines.append("")
            lines.append(f"[open in Google Maps]({maps_url})")


async def _route_offcampus_to_campus(
    lines: list,
    origin_loc: tuple,
    dest_stop: dict,
    dest_lat: float,
    dest_lng: float,
    dest_is_exact_stop: bool,
) -> None:
    """Off-campus → on-campus: public transit to gateway + NUS shuttle + walk."""
    # For Bukit Timah campus destinations, prefer BG-MRT gateway — it's adjacent
    # to BT campus and avoids the long detour south to Kent Ridge MRT.
    if dest_stop["name"] in _BUKIT_TIMAH_STOPS:
        ordered = sorted(_GATEWAYS, key=lambda x: 0 if x[0] == "BG-MRT" else 1)
    else:
        ordered = _GATEWAYS
    gateways = [(find_stop(name), addr) for name, addr in ordered if find_stop(name)]
    logger.info("off-campus routing: origin=%s dest_stop=%s gateways=%d",
                origin_loc, dest_stop["name"], len(gateways))

    maps_url = (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_loc[0]},{origin_loc[1]}"
        f"&destination={dest_lat},{dest_lng}&travelmode=transit"
    )

    # Fetch all data in parallel
    tasks = (
        [get_transit_to_stop(origin_loc[0], origin_loc[1], addr, g["lat"], g["lng"])
         for g, addr in gateways]
        + [get_arrivals_async(g["name"]) for g, _ in gateways]
        + [get_arrivals_async(dest_stop["name"])]
    )
    if not dest_is_exact_stop:
        tasks.append(get_directions(dest_stop["lat"], dest_stop["lng"], dest_lat, dest_lng))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    n = len(gateways)
    transit_results  = results[:n]
    gateway_arrivals = results[n:2 * n]
    dest_arrivals    = results[2 * n]
    walk             = results[2 * n + 1] if not dest_is_exact_stop else None

    for tr, (_gw, addr) in zip(transit_results, gateways):
        if isinstance(tr, Exception):
            logger.error("transit to %s failed: %s", addr, tr)
        else:
            logger.info("transit to %s: duration=%s steps=%d",
                        addr, tr.get("duration"), len(tr.get("steps", [])))

    dest_names: set = set()
    if not isinstance(dest_arrivals, Exception):
        dest_names = {t.name for t in dest_arrivals.timings if not t.name.strip().isdigit()}

    # Pick gateway: prefer one with common NUS buses to destination
    best: dict | None = None
    for (gateway, _), transit, arrivals in zip(gateways, transit_results, gateway_arrivals):
        if isinstance(transit, Exception) or not transit.get("duration"):
            continue
        gw_names: set = set()
        if not isinstance(arrivals, Exception):
            gw_names = {t.name for t in arrivals.timings if not t.name.strip().isdigit()}
        common = {bus for bus in (gw_names & dest_names)
                  if _nus_stops_between(bus, gateway["name"], dest_stop["name"]) is not None}
        if best is None or (common and not best["common"]):
            best = {"gateway": gateway, "transit": transit, "arrivals": arrivals, "common": common}

    logger.info("best gateway: %s", best["gateway"]["name"] if best else "none")

    if not best:
        logger.warning("no gateway found, falling back to direct directions")
        directions = await get_directions(origin_loc[0], origin_loc[1], dest_lat, dest_lng)
        _append_directions_block(lines, directions)
        return

    transit = best["transit"]
    transit_steps = transit.get("steps", [])
    logger.info("transit steps count: %d", len(transit_steps))

    # ① Public transport to gateway
    lines.append(f"*① Public transport → {best['gateway']['caption']}*")
    if transit.get("duration"):
        icon = "🚇" if transit.get("mode") == "transit" else "🚶"
        lines.append(f"{icon} {transit['distance']} · {transit['duration']}")
    if transit_steps:
        _fmt_steps(lines, transit_steps)
    else:
        lines.append("_(tap Google Maps below for step-by-step directions)_")
    lines.append("")

    # ② NUS shuttle to destination stop
    gw = best["gateway"]
    if best["common"] and not isinstance(best["arrivals"], Exception):
        lines.append(f"*② NUS shuttle: {gw['caption']} → {dest_stop['caption']}*")
        for t in best["arrivals"].timings:
            if t.name in best["common"]:
                lines.append(
                    "  " + _fmt_nus_shuttle(t.name, gw, dest_stop,
                                            t.arrival_time, t.next_arrival_time)
                )
        lines.append("")

        # ③ Walk to final destination
        if walk and not isinstance(walk, Exception) and walk.get("duration"):
            lines.append(f"*③ Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
            _fmt_steps(lines, walk.get("steps", []))
            lines.append("")
    else:
        # No valid NUS shuttle in the right direction — walk from gateway to final destination
        walk_to_dest = await get_directions(gw["lat"], gw["lng"], dest_lat, dest_lng)
        if walk_to_dest and not isinstance(walk_to_dest, Exception) and walk_to_dest.get("duration"):
            lines.append(f"*② Walk to destination* — 🚶 {walk_to_dest['distance']} · {walk_to_dest['duration']}")
            _fmt_steps(lines, walk_to_dest.get("steps", []))
            lines.append("")
        else:
            lines.append("no direct NUS bus from here — check /arrivals for options")
            lines.append("")

    lines.append(f"[open in Google Maps]({maps_url})")
