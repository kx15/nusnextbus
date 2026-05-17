import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from api import BusStopArrivals, get_all_arrivals, get_arrivals_async
from favourites import get_favourites, init_db, is_favourite, toggle_favourite
from planner import geocode_sg, geocode_with_candidates, get_directions, get_transit_to_stop
from stops import STOPS, find_stop, nearby_stops

load_dotenv()

PLAN_ORIGIN, PLAN_DEST = range(2)
NEARBY_LOCATION = 2

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PAGE_SIZE = 10


def _fmt_time(mins: str) -> str:
    if not mins or mins == "-":
        return "–"
    if mins.lower() == "arr":
        return "🚨 RUN"
    return f"{mins} min"


def format_arrivals(arrivals: BusStopArrivals) -> str:
    lines = [
        f"*{arrivals.stop_name} — {arrivals.stop_caption}*",
        f"⏱ {datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M')}",
        "",
    ]
    shuttles = [t for t in arrivals.timings if not t.name.strip().isdigit()]
    if not shuttles:
        lines.append("no buses rn... start walking bestie 💀")
    else:
        for t in shuttles:
            lines.append(
                f"\U0001f68c *{t.name}*: {_fmt_time(t.arrival_time)}"
                f" | Next: {_fmt_time(t.next_arrival_time)}"
            )
    return "\n".join(lines)


def _fav_button(user_id: int, stop_name: str) -> InlineKeyboardButton:
    label = "★ Remove Favourite" if is_favourite(user_id, stop_name) else "⭐ Add Favourite"
    return InlineKeyboardButton(label, callback_data=f"fav:{stop_name}")


def stops_keyboard(page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    page_stops = STOPS[start : start + PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                f"{s['name']} — {s['caption']}",
                callback_data=f"stop:{s['name']}",
            )
        ]
        for s in page_stops
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"page:{page - 1}"))
    if start + PAGE_SIZE < len(STOPS):
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"page:{page + 1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def format_all(results: list[Optional[BusStopArrivals]]) -> list[str]:
    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
    header = f"🚌 *all buses rn* ⏱ {timestamp}\n\n"
    lines = []
    for arrivals in results:
        if arrivals is None:
            continue
        active = [t for t in arrivals.timings if not t.name.strip().isdigit() and t.arrival_time not in ("-", "")]
        if not active:
            continue
        buses = "  ".join(
            f"*{t.name}*: {_fmt_time(t.arrival_time)}" for t in active
        )
        lines.append(f"`{arrivals.stop_name}` — {arrivals.stop_caption}\n{buses}")
    pages: list[str] = []
    current = header
    for line in lines:
        block = line + "\n\n"
        if len(current) + len(block) > 4000:
            pages.append(current.rstrip())
            current = block
        else:
            current += block
    if current.strip():
        pages.append(current.rstrip())
    return pages or ["literally no buses anywhere rn 💀 skill issue"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🚌 *NUS NextBus*\n\n"
        "no more standing at the stop praying fr\n\n"
        "• /all — every bus on campus rn\n"
        "• /arrivals `<stop>` — check a stop (e.g. `/arrivals CLB`)\n"
        "• /plan — route planner (share location → type destination)\n"
        "• /direction `<from> to <dest>` — quick plan e.g. `/direction CLB to UTOWN`\n"
        "• /bus `<service>` — NUS bus route e.g. `/bus A1`\n"
        "• /nearby — stops close to you 📍\n"
        "• /fav — your usual stops ⭐\n"
        "• /help — what is this app",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


def _location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 share my location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def nearby_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "where are you on campus? 📍",
        reply_markup=_location_keyboard(),
    )
    return NEARBY_LOCATION


async def nearby_got_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    stops = nearby_stops(loc.latitude, loc.longitude, radius_m=500)
    if not stops:
        await update.message.reply_text(
            "no NUS bus stops within 500 m — are you on campus? 💀",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(
            f"🚏 {s['name']} — {s['caption']} ({s['dist']} m)",
            callback_data=f"stop:{s['name']}",
        )]
        for s in stops[:5]
    ]
    await update.message.reply_text(
        f"NUS stops near you 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


async def nearby_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("cancelled 👍", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def stops_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Select a bus stop:",
        reply_markup=stops_keyboard(0),
    )


async def fav_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    fav_stops = get_favourites(user_id)
    if not fav_stops:
        await update.message.reply_text(
            "no usuals yet 😭\nuse /stops to add your go-to stops ⭐"
        )
        return
    buttons = [
        [InlineKeyboardButton(
            f"⭐ {s['name']} — {s['caption']}",
            callback_data=f"stop:{s['name']}",
        )]
        for name in fav_stops
        if (s := find_stop(name))
    ]
    await update.message.reply_text(
        "⭐ *your usuals*\n\nwhich one?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("checking all stops one sec 👀")
    try:
        stop_names = [s["name"] for s in STOPS]
        results = await get_all_arrivals(stop_names)
        pages = format_all(results)
        await msg.edit_text(pages[0], parse_mode="Markdown")
        for page in pages[1:]:
            await update.message.reply_text(page, parse_mode="Markdown")
    except Exception:
        logger.exception("Failed to fetch all arrivals")
        await msg.edit_text("app said nah 💀 try again")


async def arrivals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Select a bus stop:",
            reply_markup=stops_keyboard(0),
        )
        return
    query = " ".join(context.args)
    stop = find_stop(query)
    if not stop:
        await update.message.reply_text(
            f"'{query}' doesn't exist bestie. try /stops to browse 👇"
        )
        return
    try:
        arrivals = await get_arrivals_async(stop["name"])
        user_id = update.effective_user.id
        await update.message.reply_text(
            format_arrivals(arrivals),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{stop['name']}"),
                _fav_button(user_id, stop["name"]),
            ]]),
        )
    except Exception:
        logger.exception("Failed to fetch arrivals for %s", stop["name"])
        await update.message.reply_text("couldn't load that stop rn 😭 try again")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    if data.startswith("page:"):
        await query.answer()
        page = int(data.split(":", 1)[1])
        await query.edit_message_text(
            "Select a bus stop:",
            reply_markup=stops_keyboard(page),
        )
    elif data.startswith("stop:"):
        await query.answer()
        stop_name = data.split(":", 1)[1]
        stop = find_stop(stop_name)
        if not stop:
            await query.edit_message_text("that stop ghosted us 👻")
            return
        try:
            arrivals = await get_arrivals_async(stop["name"])
            user_id = query.from_user.id
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{stop_name}"),
                    _fav_button(user_id, stop_name),
                ],
                [InlineKeyboardButton("⬅ Back to stops", callback_data="page:0")],
            ])
            await query.edit_message_text(
                format_arrivals(arrivals),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to fetch arrivals for %s", stop_name)
            await query.edit_message_text("app said nah 💀 tap refresh and try again")
    elif data.startswith("refresh:"):
        stop_name = data.split(":", 1)[1]
        stop = find_stop(stop_name)
        await query.answer()
        if not stop:
            return
        try:
            arrivals = await get_arrivals_async(stop["name"])
            user_id = query.from_user.id
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{stop_name}"),
                    _fav_button(user_id, stop_name),
                ],
                [InlineKeyboardButton("⬅ Back to stops", callback_data="page:0")],
            ])
            await query.edit_message_text(
                format_arrivals(arrivals),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to refresh arrivals for %s", stop_name)
    elif data.startswith("fav:"):
        stop_name = data.split(":", 1)[1]
        user_id = query.from_user.id
        added = toggle_favourite(user_id, stop_name)
        await query.answer("Added to favourites! ⭐" if added else "Removed from favourites.")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{stop_name}"),
                _fav_button(user_id, stop_name),
            ],
            [InlineKeyboardButton("⬅ Back to stops", callback_data="page:0")],
        ])
        await query.edit_message_reply_markup(reply_markup=keyboard)
    elif data.startswith("plan_dest_candidates:") or data.startswith("dir_dest_candidates:"):
        await query.answer()
        key, _, idx_str = data.partition(":")
        idx = int(idx_str)
        candidates = context.user_data.pop(key, [])
        if not candidates or idx >= len(candidates):
            await query.edit_message_text("something went wrong, try again")
            return
        c = candidates[idx]
        d_lat, d_lng, d_label = c["lat"], c["lng"], c["label"]
        nearby = nearby_stops(d_lat, d_lng, radius_m=800)
        d_stop = nearby[0] if nearby else None

        if key == "plan_dest_candidates":
            pending = context.user_data.pop("plan_pending_origin", {})
            origin = pending.get("stop")
            o_lat  = pending.get("lat")
            o_lng  = pending.get("lng")
            o_label = pending.get("label", "your location")
        else:
            pending = context.user_data.pop("dir_pending_origin", {})
            origin = pending.get("stop")
            o_lat  = pending.get("lat")
            o_lng  = pending.get("lng")
            o_label = pending.get("label", "your location")

        if o_lat is None:
            await query.edit_message_text("session expired, please try again")
            return

        await query.edit_message_text(f"📍 got it — routing to *{d_label}*", parse_mode="Markdown")
        await _run_plan(query.message, origin, o_lat, o_lng, o_label,
                        d_stop, d_lat, d_lng, d_label, False)

    elif data.startswith("bus:"):
        service = data.split(":", 1)[1]
        await query.answer()
        route = _NUS_ROUTES.get(service)
        if not route:
            await query.edit_message_text(f"Route not found for {service}")
            return
        lines = [f"🚌 *Bus {service} — Route*\n"]
        for i, stop_name in enumerate(route, 1):
            stop = find_stop(stop_name)
            caption = stop["caption"] if stop else stop_name
            lines.append(f"{i}. {caption}")
        all_services = sorted(_NUS_ROUTES.keys())
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"bus:{name}")]
            for name in all_services
        ]
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def _resolve_location(query: str) -> tuple:
    """Return (nus_stop | None, lat, lng, label, is_exact_stop)."""
    stop = find_stop(query)
    if stop:
        return stop, stop["lat"], stop["lng"], stop["caption"], True
    coords = await geocode_sg(query)
    if coords:
        lat, lng = coords
        nearby = nearby_stops(lat, lng, radius_m=800)
        return (nearby[0] if nearby else None), lat, lng, query, False
    return None, None, None, query, False


async def _resolve_with_candidates(query: str) -> tuple:
    """
    Like _resolve_location but also returns a candidates list when the query is
    ambiguous (on-campus vs off-campus result >300 m apart).
    Returns (nus_stop | None, lat, lng, label, is_exact_stop, candidates).
    candidates = [] when unambiguous.
    """
    stop = find_stop(query)
    if stop:
        return stop, stop["lat"], stop["lng"], stop["caption"], True, []

    coords, candidates = await geocode_with_candidates(query)
    if coords:
        lat, lng = coords
        nearby = nearby_stops(lat, lng, radius_m=800)
        return (nearby[0] if nearby else None), lat, lng, query, False, candidates
    return None, None, None, query, False, []


async def _ask_which_location(message, context, candidates: list, pending_key: str) -> None:
    """Show inline buttons so user can pick among ambiguous location candidates."""
    context.user_data[pending_key] = candidates
    buttons = [
        [InlineKeyboardButton(c["label"], callback_data=f"{pending_key}:{i}")]
        for i, c in enumerate(candidates)
    ]
    await message.reply_text(
        "📍 Which location did you mean?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _run_plan(message, o_stop, o_lat, o_lng, o_label, d_stop, d_lat, d_lng, d_label, d_is_exact) -> None:
    """Send the route plan. `message` is a telegram.Message (reply target)."""
    import os as _os
    has_key = bool(_os.environ.get("GOOGLE_MAPS_API_KEY", ""))
    logger.info(
        "_run_plan: o_stop=%s d_stop=%s o=(%.4f,%.4f) d=(%.4f,%.4f) gmaps_key=%s",
        o_stop["name"] if o_stop else None,
        d_stop["name"] if d_stop else None,
        o_lat, o_lng, d_lat, d_lng,
        "SET" if has_key else "MISSING",
    )

    if o_stop and d_stop and d_stop["name"] == o_stop["name"]:
        await message.reply_text("that's the same place lol 💀")
        return

    if not has_key:
        await message.reply_text(
            "⚠️ Google Maps API key not configured — directions unavailable.\n"
            "Set GOOGLE\\_MAPS\\_API\\_KEY in Railway Variables.",
            parse_mode="Markdown",
        )
        return

    msg = await message.reply_text("planning your route one sec 👀")
    try:
        origin_loc = (o_lat, o_lng)
        lines = [f"🗺 *{o_label} → {d_label}*\n"]

        if o_stop and d_stop:
            logger.info("routing: on-campus %s → %s", o_stop["name"], d_stop["name"])
            await _route_on_campus(lines, o_stop, origin_loc, d_stop, d_lat, d_lng, d_is_exact)
        elif d_stop:
            logger.info("routing: off-campus → %s", d_stop["name"])
            await _route_offcampus_to_campus(lines, origin_loc, d_stop, d_lat, d_lng, d_is_exact)
        else:
            logger.info("routing: generic transit/walk")
            directions = await get_directions(o_lat, o_lng, d_lat, d_lng)
            _append_directions_block(lines, directions)

        logger.info("message lines: %d  preview: %s", len(lines), lines[1] if len(lines) > 1 else "")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        logger.exception("Plan failed")
        await msg.edit_text("something broke 💀 try again")


async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "where are you? 📍",
        reply_markup=_location_keyboard(),
    )
    return PLAN_ORIGIN


async def direction_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip() if context.args else ""
    if " to " not in text.lower():
        await update.message.reply_text(
            "Usage: `/direction <from> to <destination>`\ne.g. `/direction CLB to UTOWN`",
            parse_mode="Markdown",
        )
        return

    # Split on the first " to " (case-insensitive)
    idx = text.lower().index(" to ")
    o_query = text[:idx].strip()
    d_query = text[idx + 4:].strip()

    if not o_query or not d_query:
        await update.message.reply_text(
            "Usage: `/direction <from> to <destination>`\ne.g. `/direction CLB to UTOWN`",
            parse_mode="Markdown",
        )
        return

    o_stop, o_lat, o_lng, o_label, _, o_candidates = await _resolve_with_candidates(o_query)
    d_stop, d_lat, d_lng, d_label, d_is_exact, d_candidates = await _resolve_with_candidates(d_query)

    if o_lat is None:
        await update.message.reply_text(f"couldn't find origin: *{o_query}* 😭", parse_mode="Markdown")
        return
    if d_lat is None:
        await update.message.reply_text(f"couldn't find destination: *{d_query}* 😭", parse_mode="Markdown")
        return

    # If destination is ambiguous, ask user to pick before routing
    if d_candidates:
        context.user_data["dir_pending_origin"] = {
            "stop": o_stop, "lat": o_lat, "lng": o_lng, "label": o_label,
        }
        await _ask_which_location(update.message, context, d_candidates, "dir_dest_candidates")
        return

    await _run_plan(update.message, o_stop, o_lat, o_lng, o_label, d_stop, d_lat, d_lng, d_label, d_is_exact)


async def plan_got_origin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    stops = nearby_stops(loc.latitude, loc.longitude, radius_m=800)
    origin = stops[0] if stops else None

    context.user_data["plan_origin"] = origin
    context.user_data["plan_origin_loc"] = (loc.latitude, loc.longitude)

    await update.message.reply_text(
        "📍 got your location\n\nwhere are you going? 🏫\n_type a place or stop name_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PLAN_DEST


# NUS campus entry points: (stop_name, MRT station address for Directions API)
_GATEWAYS = [
    ("KR-MRT", "Kent Ridge MRT Station, Singapore"),
    ("BG-MRT", "Botanic Gardens MRT Station, Singapore"),
]

# Ordered stop sequences for each NUS ISB route (both directions where applicable)
_NUS_ROUTES: dict[str, list[str]] = {
    "A1":  ["KR-MRT", "LT13", "AS5", "BIZ2", "CLB", "LT13-OPP", "IT", "COM3", "BIZ2",
            "PGP", "PGPR", "KRB", "LT27", "S17", "YIH", "CLB", "LT13", "AS5",
            "MUSEUM", "UTOWN", "RAFFLES", "CG", "MUSEUM", "KV", "BG-MRT"],
    "A2":  ["BG-MRT", "KV", "MUSEUM", "CG", "RAFFLES", "UTOWN", "MUSEUM",
            "YIH", "S17", "LT27", "KRB", "PGPR", "PGP", "COM3", "IT",
            "LT13-OPP", "CLB", "BIZ2", "AS5", "LT13", "KR-MRT"],
    "D1":  ["COM3", "BIZ2", "CLB", "LT13-OPP", "AS5", "YIH", "MUSEUM",
            "UTOWN", "MUSEUM", "YIH", "AS5", "LT13-OPP", "CLB", "BIZ2", "COM3"],
    "D2":  ["COM3", "IT", "S17", "LT27", "KRB", "PGPR", "PGP",
            "MUSEUM", "UTOWN", "KR-MRT", "UTOWN", "MUSEUM",
            "PGP", "PGPR", "KRB", "LT27", "S17", "IT", "COM3"],
    "K":   ["KR-MRT", "LT13", "AS5", "YIH", "CLB", "LT13-OPP",
            "PGP", "PGPR", "KRB", "LT27", "S17", "MUSEUM",
            "UTOWN", "MUSEUM", "KV", "BG-MRT"],
    "P":   ["KR-MRT", "UTOWN", "CG", "UTOWN", "BG-MRT", "KV",
            "MUSEUM", "UTOWN", "KR-MRT"],
    "R1":  ["CLB", "LT13-OPP", "BIZ2", "AS5", "YIH", "MUSEUM",
            "UTOWN", "MUSEUM", "YIH", "AS5", "BIZ2", "LT13-OPP", "CLB"],
    "R2":  ["PGP", "PGPR", "IT", "UTOWN", "RAFFLES", "UTOWN",
            "IT", "PGPR", "PGP"],
}


def _nus_stops_between(bus: str, board: str, alight: str) -> Optional[int]:
    """Return number of stops between board and alight for a given NUS bus, or None."""
    route = _NUS_ROUTES.get(bus, [])
    try:
        i = route.index(board)
        j = route.index(alight, i + 1)
        return j - i
    except ValueError:
        return None


def _fmt_nus_shuttle(bus_name: str, board_stop: dict, alight_stop: dict,
                     arrival: str, next_arrival: str) -> str:
    stops = _nus_stops_between(bus_name, board_stop["name"], alight_stop["name"])
    stops_txt = f" · {stops} stop{'s' if stops != 1 else ''}" if stops else ""
    return (
        f"🚌 *{bus_name}*{stops_txt}  "
        f"{_fmt_time(arrival)} | Next: {_fmt_time(next_arrival)}"
    )


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


async def _route_on_campus(
    lines: list,
    origin: dict,
    origin_loc: tuple,
    dest_stop: dict,
    dest_lat: float,
    dest_lng: float,
    dest_is_exact_stop: bool,
) -> None:
    """On-campus → on-campus: NUS shuttle + last-mile walk."""
    tasks: list = [
        get_arrivals_async(origin["name"]),
        get_arrivals_async(dest_stop["name"]),
    ]
    if not dest_is_exact_stop:
        tasks.append(get_directions(dest_stop["lat"], dest_stop["lng"], dest_lat, dest_lng))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    origin_arrivals, dest_arrivals = results[0], results[1]
    walk = results[2] if not dest_is_exact_stop else None

    if not isinstance(origin_arrivals, Exception) and not isinstance(dest_arrivals, Exception):
        origin_names = {t.name for t in origin_arrivals.timings if not t.name.strip().isdigit()}
        dest_names   = {t.name for t in dest_arrivals.timings   if not t.name.strip().isdigit()}
        common = origin_names & dest_names
        if common:
            lines.append(
                f"🚌 *NUS shuttle: {origin['caption']} → {dest_stop['caption']}*"
            )
            for t in origin_arrivals.timings:
                if t.name in common:
                    lines.append(
                        "  " + _fmt_nus_shuttle(t.name, origin, dest_stop,
                                                t.arrival_time, t.next_arrival_time)
                    )
            lines.append("")
        else:
            lines.append("no direct NUS bus — might need a transfer or just walk 🚶\n")

    if walk and not isinstance(walk, Exception) and walk.get("duration"):
        lines.append(f"*Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
        _fmt_steps(lines, walk.get("steps", []))
        lines.append("")

    maps_url = (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_loc[0]},{origin_loc[1]}"
        f"&destination={dest_lat},{dest_lng}&travelmode=walking"
    )
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
    gateways = [(find_stop(name), addr) for name, addr in _GATEWAYS if find_stop(name)]
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

    for i, (tr, (gw, addr)) in enumerate(zip(transit_results, gateways)):
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
        common = gw_names & dest_names
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
    lines.append(f"*② NUS shuttle: {gw['caption']} → {dest_stop['caption']}*")
    if best["common"] and not isinstance(best["arrivals"], Exception):
        for t in best["arrivals"].timings:
            if t.name in best["common"]:
                lines.append(
                    "  " + _fmt_nus_shuttle(t.name, gw, dest_stop,
                                            t.arrival_time, t.next_arrival_time)
                )
    else:
        lines.append("no direct NUS bus — check /arrivals for options")
    lines.append("")

    # ③ Walk to final destination
    if walk and not isinstance(walk, Exception) and walk.get("duration"):
        lines.append(f"*③ Walk to destination* — 🚶 {walk['distance']} · {walk['duration']}")
        _fmt_steps(lines, walk.get("steps", []))
        lines.append("")

    lines.append(f"[open in Google Maps]({maps_url})")


async def bus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        # List all services
        names = sorted(_NUS_ROUTES.keys())
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"bus:{name}")]
            for name in names
        ]
        await update.message.reply_text(
            "🚌 *NUS Bus Services*\nTap a service to see its route:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    service = context.args[0].upper()
    route = _NUS_ROUTES.get(service)
    if not route:
        available = ", ".join(sorted(_NUS_ROUTES.keys()))
        await update.message.reply_text(
            f"Unknown service *{service}*.\nAvailable: {available}",
            parse_mode="Markdown",
        )
        return

    lines = [f"🚌 *Bus {service} — Route*\n"]
    for i, stop_name in enumerate(route, 1):
        stop = find_stop(stop_name)
        caption = stop["caption"] if stop else stop_name
        lines.append(f"{i}. {caption}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def plan_got_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    origin     = context.user_data.get("plan_origin")
    origin_loc = context.user_data.get("plan_origin_loc")

    if not origin_loc:
        await update.message.reply_text("something went wrong, try /plan again")
        return ConversationHandler.END

    dest_stop        = None
    dest_lat         = dest_lng = None
    dest_label       = None
    dest_is_exact_stop = False

    if update.message.location:
        loc = update.message.location
        dest_lat, dest_lng = loc.latitude, loc.longitude
        stops = nearby_stops(dest_lat, dest_lng, radius_m=800)
        if stops:
            dest_stop  = stops[0]
            dest_label = dest_stop["caption"]
    else:
        query = update.message.text.strip()
        dest_stop, dest_lat, dest_lng, dest_label, dest_is_exact_stop, candidates = \
            await _resolve_with_candidates(query)

        if candidates and dest_lat is not None:
            # Ambiguous — ask user to pick, store origin so callback can complete the route
            context.user_data["plan_pending_origin"] = {
                "stop": origin, "lat": o_lat, "lng": o_lng,
                "label": origin["caption"] if origin else "your location",
            }
            await _ask_which_location(update.message, context, candidates, "plan_dest_candidates")
            return PLAN_DEST

    if dest_lat is None:
        await update.message.reply_text(
            "couldn't find that place 😭\ntry a different name or share your destination 📍"
        )
        return PLAN_DEST

    origin_label = origin["caption"] if origin else "your location"
    o_lat, o_lng = origin_loc

    context.user_data.pop("plan_origin", None)
    context.user_data.pop("plan_origin_loc", None)

    await _run_plan(update.message, origin, o_lat, o_lng, origin_label, dest_stop, dest_lat, dest_lng, dest_label, dest_is_exact_stop)
    return ConversationHandler.END


async def plan_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("plan_origin", None)
    context.user_data.pop("plan_origin_loc", None)
    await update.message.reply_text("plan cancelled 👍", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def debugplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden debug: calls transit API from a fixed off-campus point to KR-MRT and dumps result."""
    import os as _os
    key = _os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not key:
        await update.message.reply_text("GOOGLE_MAPS_API_KEY not set")
        return

    await update.message.reply_text("calling Directions API…")
    result = await get_transit_to_stop(
        1.3521, 103.8198,  # Singapore city centre
        "Kent Ridge MRT Station, Singapore",
        1.2975, 103.7847,
    )
    lines = [
        f"key: SET ({key[:8]}…)",
        f"mode: {result.get('mode')}",
        f"duration: {result.get('duration')}",
        f"distance: {result.get('distance')}",
        f"steps: {len(result.get('steps', []))}",
    ]
    for i, s in enumerate(result.get("steps", []), 1):
        lines.append(f"{i}. {s['instruction']} ({s['distance']})")
    await update.message.reply_text("\n".join(lines) or "empty result")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmd = update.message.text.split()[0] if update.message.text else "that"
    await update.message.reply_text(
        f"bro what is {cmd} 💀 that's not a thing\n\ntype /start to see what i can actually do 👇",
    )


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",    "What is this app"),
        BotCommand("all",      "All bus arrivals"),
        BotCommand("arrivals", "Select stop to get arrival time"),
        BotCommand("plan",      "Route planner via location sharing"),
        BotCommand("direction", "Quick route e.g. /direction CLB to UTOWN"),
        BotCommand("bus",       "NUS bus route e.g. /bus A1"),
        BotCommand("nearby",    "Find stops near you 📍"),
        BotCommand("fav",       "Your favourite stops"),
        BotCommand("help",      "Show this message"),
    ])


def main() -> None:
    init_db()
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = Application.builder().token(token).post_init(post_init).build()

    nearby_handler = ConversationHandler(
        entry_points=[CommandHandler("nearby", nearby_start)],
        states={
            NEARBY_LOCATION: [MessageHandler(filters.LOCATION, nearby_got_location)],
        },
        fallbacks=[CommandHandler("cancel", nearby_cancel)],
        allow_reentry=True,
    )

    plan_handler = ConversationHandler(
        entry_points=[CommandHandler("plan", plan_start)],
        states={
            PLAN_ORIGIN: [MessageHandler(filters.LOCATION, plan_got_origin)],
            PLAN_DEST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_got_dest),
                MessageHandler(filters.LOCATION, plan_got_dest),
            ],
        },
        fallbacks=[CommandHandler("cancel", plan_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("all",      all_command))
    app.add_handler(CommandHandler("stops",    stops_command))
    app.add_handler(CommandHandler("arrivals",   arrivals_command))
    app.add_handler(CommandHandler(["direction", "destination"],  direction_command))
    app.add_handler(CommandHandler("bus",        bus_command))
    app.add_handler(CommandHandler("debugplan",  debugplan_command))
    app.add_handler(nearby_handler)
    app.add_handler(plan_handler)
    app.add_handler(CommandHandler("fav",      fav_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Starting bot (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
