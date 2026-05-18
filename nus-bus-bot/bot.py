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
DIRECTION_FROM, DIRECTION_TO = 3, 4

# Page 0 quick-pick stops (shown first, same set)
_DIRECTION_STOPS = [
    ("CLB",    "Central Library"),
    ("KR-MRT", "Kent Ridge MRT"),
    ("UTOWN",  "UTown"),
    ("COM3",   "COM 3"),
    ("BIZ2",   "BIZ 2"),
    ("PGP",    "Prince George's Park"),
    ("YIH",    "YIH"),
    ("MUSEUM", "Museum"),
    ("LT27",   "LT 27"),
    ("KRB",    "KR Bus Terminal"),
    ("OTH",      "Oei Tiong Ham (BTC)"),
    ("NUSS-OPP", "Opp NUSS"),
]
_DIRECTION_STOPS_PER_PAGE = 12
# Quick-pick names for deduplication
_DIRECTION_STOP_NAMES = {name for name, _ in _DIRECTION_STOPS}


def _direction_extra_stops() -> list[tuple[str, str]]:
    """All STOPS not in the quick-pick list, sorted A–Z by caption."""
    return sorted(
        [(s["name"], s["caption"]) for s in STOPS if s["name"] not in _DIRECTION_STOP_NAMES],
        key=lambda x: x[1],
    )


def _direction_keyboard(prefix: str, page: int = 0) -> InlineKeyboardMarkup:
    """
    Page 0 : quick-pick stops sorted A–Z.
    Page 1+: remaining stops sorted A–Z, _DIRECTION_STOPS_PER_PAGE at a time.
    """
    if page == 0:
        page_items = sorted(_DIRECTION_STOPS, key=lambda x: x[1])
        extra = _direction_extra_stops()
        has_next = len(extra) > 0
        has_prev = False
    else:
        extra = _direction_extra_stops()
        start   = (page - 1) * _DIRECTION_STOPS_PER_PAGE
        page_items = extra[start : start + _DIRECTION_STOPS_PER_PAGE]
        has_prev = True
        has_next = start + _DIRECTION_STOPS_PER_PAGE < len(extra)

    rows, pair = [], []
    for stop_name, label in page_items:
        pair.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{stop_name}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"{prefix}_page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("More stops ➡️", callback_data=f"{prefix}_page:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)

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
        return "🏃‍♂️RUN"
    try:
        m = int(mins)
        if m > 30:
            eta = datetime.now(timezone(timedelta(hours=8))) + timedelta(minutes=m)
            return f"~{eta.strftime('%H:%M')}"
        return f"{m} min"
    except ValueError:
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
        # Merge multiple vehicle entries for the same service into one row
        merged: dict[str, tuple[str, str]] = {}
        for t in shuttles:
            if t.name not in merged:
                merged[t.name] = (t.arrival_time, t.next_arrival_time)
        for name, (first, nxt) in merged.items():
            lines.append(
                f"\U0001f68c *{name}*: {_fmt_time(first)}"
                f" | Next: {_fmt_time(nxt)}"
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
        shuttles = [t for t in arrivals.timings if not t.name.strip().isdigit()]
        if not shuttles:
            buses = "no service"
        else:
            buses = "  ".join(
                f"*{t.name}*: {_fmt_time(t.arrival_time)}" for t in shuttles
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
        "• /plan — route planner (share location → choose/type location)\n"
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
        reply_markup=_direction_keyboard("stop"),
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
            reply_markup=_direction_keyboard("stop"),
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
    if data.startswith("stop_page:"):
        await query.answer()
        page = int(data.split(":", 1)[1])
        await query.edit_message_reply_markup(
            reply_markup=_direction_keyboard("stop", page),
        )
    elif data.startswith("page:"):
        await query.answer()
        page = int(data.split(":", 1)[1])
        await query.edit_message_reply_markup(
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
            await query.answer(f"Route not found for {service}", show_alert=True)
            return
        lines = [f"🚌 *Bus {service} — Route*\n"]
        sched_lines = _bus_schedule_lines(service)
        if sched_lines:
            lines.extend(sched_lines)
            lines.append("")
        for i, stop_name in enumerate(route, 1):
            stop = find_stop(stop_name)
            lines.append(f"{i}. {stop['caption'] if stop else stop_name}")
        # Send route as a new message below — buttons stay intact above
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="\n".join(lines),
            parse_mode="Markdown",
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
            # When destination is a building (not exact stop), check nearby stops
            # and pick the one reachable in fewest bus stops from the effective origin.
            # For BT campus origins (OTH/CG/BG-MRT), the journey always transfers
            # through KR-MRT — use that as the effective origin for optimisation.
            if not d_is_exact:
                candidates = nearby_stops(d_lat, d_lng, radius_m=200)
                if len(candidates) > 1:
                    eff_origin = (
                        "KR-MRT"
                        if o_stop["name"] in _BUKIT_TIMAH_STOPS
                        else o_stop["name"]
                    )
                    better = _best_dest_stop(eff_origin, candidates)
                    if better:
                        d_stop = better
            logger.info("routing: on-campus %s → %s", o_stop["name"], d_stop["name"])
            await _route_on_campus(lines, o_stop, origin_loc, d_stop, d_lat, d_lng, d_is_exact, d_label)
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


async def direction_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Quick text mode: /direction CLB to UTOWN
    text = " ".join(context.args).strip() if context.args else ""
    if " to " in text.lower():
        idx     = text.lower().index(" to ")
        o_query = text[:idx].strip()
        d_query = text[idx + 4:].strip()
        if o_query and d_query:
            o_stop, o_lat, o_lng, o_label, _, _ = await _resolve_with_candidates(o_query)
            d_stop, d_lat, d_lng, d_label, d_is_exact, d_cands = await _resolve_with_candidates(d_query)
            if o_lat is None:
                await update.message.reply_text(f"couldn't find origin: *{o_query}* 😭", parse_mode="Markdown")
                return ConversationHandler.END
            if d_lat is None:
                await update.message.reply_text(f"couldn't find destination: *{d_query}* 😭", parse_mode="Markdown")
                return ConversationHandler.END
            if d_cands:
                context.user_data["dir_pending_origin"] = {"stop": o_stop, "lat": o_lat, "lng": o_lng, "label": o_label}
                await _ask_which_location(update.message, context, d_cands, "dir_dest_candidates")
            else:
                await _run_plan(update.message, o_stop, o_lat, o_lng, o_label, d_stop, d_lat, d_lng, d_label, d_is_exact)
            return ConversationHandler.END

    # Conversation mode: show FROM keyboard
    await update.message.reply_text(
        "🗺 *Direction planner*\n\nWhere are you coming *from*?\nTap a stop or type any location 👇",
        parse_mode="Markdown",
        reply_markup=_direction_keyboard("dir_from"),
    )
    return DIRECTION_FROM


async def _direction_resolve(query_or_stop: str, is_stop_name: bool) -> tuple:
    """Resolve a stop name or free-text query to (stop, lat, lng, label, is_exact, candidates)."""
    if is_stop_name:
        stop = find_stop(query_or_stop)
        if stop:
            return stop, stop["lat"], stop["lng"], stop["caption"], True, []
    return await _resolve_with_candidates(query_or_stop)


async def direction_got_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        data = update.callback_query.data
        if "_page:" in data:
            page = int(data.split("_page:")[1])
            await update.callback_query.edit_message_reply_markup(
                reply_markup=_direction_keyboard("dir_from", page)
            )
            return DIRECTION_FROM
        stop_name = data.split(":", 1)[1]
        stop = find_stop(stop_name)
        label = stop["caption"] if stop else stop_name
        context.user_data.update({"dir_o_stop": stop, "dir_o_lat": stop["lat"] if stop else None,
                                   "dir_o_lng": stop["lng"] if stop else None, "dir_o_label": label})
        await update.callback_query.edit_message_text(
            f"📍 From: *{label}*\n\nWhere are you going *to*?\nTap a stop or type any location 👇",
            parse_mode="Markdown",
            reply_markup=_direction_keyboard("dir_to"),
        )
    else:
        query = update.message.text.strip()
        o_stop, o_lat, o_lng, o_label, _, _ = await _resolve_with_candidates(query)
        if o_lat is None:
            await update.message.reply_text(f"couldn't find *{query}* 😭\ntry again or tap a stop above", parse_mode="Markdown")
            return DIRECTION_FROM
        context.user_data.update({"dir_o_stop": o_stop, "dir_o_lat": o_lat, "dir_o_lng": o_lng, "dir_o_label": o_label})
        await update.message.reply_text(
            f"📍 From: *{o_label}*\n\nWhere are you going *to*?\nTap a stop or type any location 👇",
            parse_mode="Markdown",
            reply_markup=_direction_keyboard("dir_to"),
        )
    return DIRECTION_TO


async def direction_got_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    o_stop  = context.user_data.pop("dir_o_stop",  None)
    o_lat   = context.user_data.pop("dir_o_lat",   None)
    o_lng   = context.user_data.pop("dir_o_lng",   None)
    o_label = context.user_data.pop("dir_o_label", "your location")

    if o_lat is None:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text("session expired, try /direction again")
        return ConversationHandler.END

    if update.callback_query:
        await update.callback_query.answer()
        data = update.callback_query.data
        if "_page:" in data:
            page = int(data.split("_page:")[1])
            # Put origin back so it's available when user picks destination
            context.user_data.update({"dir_o_stop": o_stop, "dir_o_lat": o_lat,
                                       "dir_o_lng": o_lng, "dir_o_label": o_label})
            await update.callback_query.edit_message_reply_markup(
                reply_markup=_direction_keyboard("dir_to", page)
            )
            return DIRECTION_TO
        stop_name  = data.split(":", 1)[1]
        d_stop     = find_stop(stop_name)
        d_lat      = d_stop["lat"]     if d_stop else None
        d_lng      = d_stop["lng"]     if d_stop else None
        d_label    = d_stop["caption"] if d_stop else stop_name
        d_is_exact = True
        await update.callback_query.edit_message_text(
            f"📍 *{o_label} → {d_label}*\nplanning route…", parse_mode="Markdown"
        )
        message = update.callback_query.message
    else:
        query = update.message.text.strip()
        d_stop, d_lat, d_lng, d_label, d_is_exact, d_cands = await _resolve_with_candidates(query)
        if d_lat is None:
            await update.message.reply_text(f"couldn't find *{query}* 😭\ntry again or tap a stop above", parse_mode="Markdown")
            return DIRECTION_TO
        if d_cands:
            context.user_data.update({"dir_o_stop": o_stop, "dir_o_lat": o_lat, "dir_o_lng": o_lng, "dir_o_label": o_label})
            await _ask_which_location(update.message, context, d_cands, "dir_dest_candidates")
            return DIRECTION_TO
        message = update.message

    await _run_plan(message, o_stop, o_lat, o_lng, o_label, d_stop, d_lat, d_lng, d_label, d_is_exact)
    return ConversationHandler.END


async def direction_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for k in ("dir_o_stop", "dir_o_lat", "dir_o_lng", "dir_o_label"):
        context.user_data.pop(k, None)
    await update.message.reply_text("cancelled 👍")
    return ConversationHandler.END


async def plan_got_origin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    stops = nearby_stops(loc.latitude, loc.longitude, radius_m=800)
    origin = stops[0] if stops else None

    context.user_data["plan_origin"] = origin
    context.user_data["plan_origin_loc"] = (loc.latitude, loc.longitude)

    await update.message.reply_text("📍 got your location", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(
        "where are you going? 🏫\nTap a stop or type any location 👇",
        parse_mode="Markdown",
        reply_markup=_direction_keyboard("plan_to"),
    )
    return PLAN_DEST


# NUS campus entry points: (stop_name, MRT station address for Directions API)
_GATEWAYS = [
    ("KR-MRT", "Kent Ridge MRT Station, Singapore"),
    ("BG-MRT", "Botanic Gardens MRT Station, Singapore"),
]

# Ordered stop sequences for each NUS ISB route.
# Stop membership verified against NUS NextBus API (/ShuttleService per stop).
# Direction order is best-estimate geographic; count may be ±1 for edge cases.
_NUS_ROUTES: dict[str, list[str]] = {
    # All routes rebuilt from NUS NextBus API — every stop membership verified.
    # Stop order is best-estimate geographic; counts may be ±1 for edge cases.

    # A1 API-confirmed: AS5, BIZ2, CLB, KR-MRT, KRB, LT13, LT27, PGP,
    #                   TCOMS-OPP, UHALL, UHC-OPP, YIH
    # Route order confirmed from live timing: UHALL→UHC-OPP→YIH→CLB→KRB (2 min gap)
    # KRB is the official start; bus goes south campus first, then north back to KRB.
    "A1": [
        "KRB", "LT13", "AS5", "BIZ2", "TCOMS-OPP", "PGP",
        "KR-MRT", "LT27", "UHALL", "UHC-OPP", "YIH", "CLB", "KRB",
    ],

    # A2 confirmed route (user-verified):
    "A2": [
        "KRB", "IT", "YIH-OPP", "MUSEUM", "UHC", "UHALL-OPP",
        "S17", "KR-MRT-OPP", "PGPR", "TCOMS",
        "HSSML-OPP", "NUSS-OPP", "LT13-OPP", "KRB",
    ],

    # D1 confirmed route (user-verified):
    "D1": [
        "COM3", "HSSML-OPP", "NUSS-OPP", "LT13-OPP", "IT",
        "YIH-OPP", "MUSEUM", "UTOWN",
        "YIH", "CLB", "LT13", "AS5", "BIZ2", "COM3",
    ],

    # D2 confirmed route (user-verified):
    "D2": [
        "COM3", "TCOMS-OPP", "PGP", "KR-MRT",
        "LT27", "UHALL", "UHC-OPP", "MUSEUM", "UTOWN", "UHC",
        "UHALL-OPP", "S17", "KR-MRT-OPP", "PGP", "TCOMS", "COM3",
    ],

    # K confirmed route (user-verified):
    "K": [
        "PGP", "KR-MRT", "LT27", "UHALL", "UHC-OPP",
        "YIH", "CLB", "SDE3-OPP", "JP-SCH-16151", "KV",
        "MUSEUM", "UHC", "UHALL-OPP", "S17", "KR-MRT-OPP", "PGP",
    ],

    # P confirmed route (user-verified):
    "P": [
        "KV", "CG", "OTH", "BG-MRT", "KR-MRT", "UHC-OPP", "UTOWN",
    ],

    # R1 confirmed route (user-verified):
    "R1": [
        "KV", "MUSEUM", "UTOWN", "YIH", "CLB", "LT13", "AS5", "BIZ2", "PGP",
    ],

    # R2 confirmed route (user-verified):
    "R2": [
        "PGP", "HSSML-OPP", "NUSS-OPP", "LT13-OPP", "IT",
        "YIH-OPP", "UTOWN", "RAFFLES", "KV",
    ],
}


def _nus_stops_between(bus: str, board: str, alight: str) -> Optional[int]:
    """Return number of stops between board and alight for a given NUS bus, or None."""
    route = _NUS_ROUTES.get(bus, [])
    best: Optional[int] = None
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


def _best_dest_stop(o_stop_name: str, candidates: list) -> Optional[dict]:
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


_MAIN_SERVICES = frozenset({"A1", "A2", "D1", "D2"})


def _find_transfers(origin_name: str, dest_name: str) -> list[tuple[str, str, str, int]]:
    """Find 1-transfer journeys (bus1, transfer_stop, bus2, total_stops).
    Sorted by total stops, then by preference for main services (A1/A2/D1/D2)."""
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

    def _score(item: tuple) -> tuple:
        b1, _, b2, stops = item
        non_main = (0 if b1 in _MAIN_SERVICES else 1) + (0 if b2 in _MAIN_SERVICES else 1)
        return (stops, non_main)

    return sorted(
        [(b1, mid, b2, sc) for (b1, mid, b2), sc in seen.items()],
        key=_score,
    )


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


# Stops at Bukit Timah campus — only reachable via Bus P
_BUKIT_TIMAH_STOPS = {"CG", "BG-MRT", "OTH"}
# Transfer hubs for arriving at BT campus
# KV is the natural P boarding point for OTH/CG (KV→CG→OTH on Bus P)
_BUS_P_HUBS_ARRIVAL   = ["UTOWN", "KR-MRT", "KV", "MUSEUM"]
# Transfer hubs for departing BT campus (KR-MRT first — only 2 stops from OTH on P)
_BUS_P_HUBS_DEPARTURE = ["KR-MRT", "UTOWN", "MUSEUM"]
# Companion stops — same physical location, opposite side of road.
# Used to find shorter connecting routes (e.g. D2 from KR-MRT-OPP is 3 stops to COM3
# vs 12 stops from KR-MRT itself).
_COMPANION_STOPS: dict[str, str] = {
    # Verified companion pairs (opposite sides of road, complementary bus sets)
    "KR-MRT":    "KR-MRT-OPP",  "KR-MRT-OPP": "KR-MRT",   # 22m
    "LT27":      "S17",          "S17":         "LT27",       # 27m
    "TCOMS":     "TCOMS-OPP",   "TCOMS-OPP":  "TCOMS",      # 25m
    "UHALL":     "UHALL-OPP",   "UHALL-OPP":  "UHALL",      # 22m
    "UHC":       "UHC-OPP",     "UHC-OPP":    "UHC",        # 56m
    "YIH":       "YIH-OPP",     "YIH-OPP":    "YIH",        # 29m
    "LT13":      "LT13-OPP",    "LT13-OPP":   "LT13",       # 87m
    "BIZ2":      "HSSML-OPP",   "HSSML-OPP":  "BIZ2",       # 48m
    "CLB":       "IT",           "IT":          "CLB",        # 74m
}


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

    from urllib.parse import quote as _quote
    origin_addr = _quote(f"{origin['caption']} NUS Singapore")
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
        # Show all valid buses; use live timing where available, – otherwise
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
            to_dest_comp = set()
            min_comp = 999
            if comp_name and comp_arr and not isinstance(comp_arr, Exception):
                comp_bus_names = {t.name for t in comp_arr.timings if not t.name.strip().isdigit()}
                to_dest_comp = comp_bus_names & dest_names
                min_comp = min(
                    (_nus_stops_between(bus, comp_name, dest_stop["name"]) or 999)
                    for bus in to_dest_comp
                ) if to_dest_comp else 999

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
            if use_companion:
                lines.append(f"  _cross the road to {step2_stop['caption']}_")
            lines.append("")

            # Step 2: All valid connecting buses (sorted by fewest stops)
            live_step2: dict = {}
            if step2_arr and not isinstance(step2_arr, Exception):
                for t in step2_arr.timings:
                    if not t.name.strip().isdigit():
                        live_step2[t.name] = t
            conn_buses = sorted(
                to_dest,
                key=lambda b: _nus_stops_between(b, step2_name, dest_stop["name"]) or 999,
            )
            lines.append(f"*② {step2_stop['caption']} → {dest_stop['caption']}*")
            for bus_name in conn_buses:
                td  = live_step2.get(bus_name)
                arr = td.arrival_time      if td else "-"
                nxt = td.next_arrival_time if td else "-"
                lines.append("  " + _fmt_nus_shuttle(bus_name, step2_stop, dest_stop, arr, nxt))
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
            lines.append("Bus P not available right now 💀\n")
            lines.append("🚇 *take public transport instead:*")
            lines.append(f"[MRT/bus options in Google Maps]({transit_url})")

    else:
        # No direct NUS bus (non-BT): try 1 transfer
        transfers = _find_transfers(origin["name"], dest_stop["name"])
        if transfers:
            bus1, mid_name, bus2, _ = transfers[0]
            mid_stop = find_stop(mid_name)
            try:
                mid_arrivals = await get_arrivals_async(mid_name)
            except Exception:
                mid_arrivals = None
            live_mid: dict = {}
            if mid_arrivals and not isinstance(mid_arrivals, Exception):
                for t in mid_arrivals.timings:
                    if not t.name.strip().isdigit():
                        live_mid[t.name] = t
            t1 = live_timing.get(bus1)
            t2 = live_mid.get(bus2)
            lines.append(f"*① {origin['caption']} → {mid_stop['caption']}*")
            lines.append("  " + _fmt_nus_shuttle(bus1, origin, mid_stop,
                                                  t1.arrival_time if t1 else "-",
                                                  t1.next_arrival_time if t1 else "-"))
            lines.append("")
            lines.append(f"*② {mid_stop['caption']} → {dest_stop['caption']}*")
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


# Bus operating schedules.
# Keys: "mon_fri", "saturday", "mon_sat" (Mon–Sat), "sun_ph" (Sun/PH), "weekday" (legacy Mon–Sat).
# A value of None for "sun_ph" means no service on Sunday/PH.
_BUS_SCHEDULE: dict[str, dict] = {
    "A1": {
        "mon_sat": ("07:15", "23:00"),
        "sun_ph":  ("09:07", "23:00"),
    },
    "A2": {
        "mon_sat": ("07:15", "23:00"),
        "sun_ph":  ("09:00", "23:00"),
    },
    "D1": {
        "mon_fri":  ("07:15 _(term)_ / 07:20 _(vac)_", "23:00"),
        "saturday": ("07:20", "23:00"),
        "sun_ph":   ("09:10", "23:00"),
    },
    "D2": {
        "mon_sat": ("07:15", "23:00"),
        "sun_ph":  ("09:00", "23:00"),
    },
    "K": {
        "mon_fri":  ("07:04", "23:04"),
        "saturday": ("07:04", "19:44"),
        "sun_ph":   None,  # No service 💀
    },
    "P": {
        "mon_fri":  ("08:20", "17:25"),
        "saturday": None,  # No service 💀
        "sun_ph":   None,  # No service 💀
    },
    "R1": {
        "mon_fri":  ("07:40", "19:30"),
        "saturday": None,  # No service 💀
        "sun_ph":   None,  # No service 💀
    },
    "R2": {
        "mon_fri":  ("08:20", "19:30"),
        "saturday": None,  # No service 💀
        "sun_ph":   None,  # No service 💀
    },
}


def _bus_schedule_lines(service: str) -> list[str]:
    """Return one formatted line per day-band for the full operating schedule."""
    sched = _BUS_SCHEDULE.get(service)
    if not sched:
        return []

    def _fmt(label: str, times) -> str:
        if times is None:
            return f"⏰ *{label}*  No service 💀"
        first, last = times
        return f"⏰ *{label}*  First {first} · Last {last}"

    lines: list[str] = []
    has_mon_sat  = "mon_sat"  in sched or "weekday" in sched
    has_mon_fri  = "mon_fri"  in sched
    has_saturday = "saturday" in sched
    has_sun_ph   = "sun_ph"   in sched or "weekend" in sched

    if has_mon_sat:
        key = "mon_sat" if "mon_sat" in sched else "weekday"
        lines.append(_fmt("Mon–Sat", sched[key]))
    elif has_mon_fri:
        lines.append(_fmt("Mon–Fri", sched["mon_fri"]))
        if has_saturday:
            sat_val = sched["saturday"]
            sun_val = sched.get("sun_ph") if "sun_ph" in sched else sched.get("weekend")
            # Collapse into one line when both weekend days are no service
            if sat_val is None and sun_val is None and has_sun_ph:
                lines.append(_fmt("Sat/Sun/PH", None))
                return lines
            lines.append(_fmt("Sat", sat_val))

    if has_sun_ph:
        key = "sun_ph" if "sun_ph" in sched else "weekend"
        lines.append(_fmt("Sun/PH", sched[key]))

    return lines


def _bus_first_last(service: str) -> tuple[Optional[str], Optional[str]]:
    """Return (first_bus, last_bus) for today. Returns ("no_service", None) when no service today."""
    sched = _BUS_SCHEDULE.get(service)
    if not sched:
        return None, None
    dow = datetime.now(timezone(timedelta(hours=8))).weekday()  # 0=Mon … 5=Sat, 6=Sun
    if dow == 6:  # Sunday / PH
        if "sun_ph" in sched:
            times = sched["sun_ph"]
            if times is None:
                return "no_service", None
        else:
            times = sched.get("weekend")
    elif dow == 5:  # Saturday
        if "saturday" in sched:
            times = sched["saturday"]
            if times is None:
                return "no_service", None
        else:
            times = sched.get("mon_sat") or sched.get("weekday")
    else:  # Mon–Fri
        times = (sched.get("mon_fri")
                 or sched.get("mon_sat")
                 or sched.get("weekday"))
    if times is None:
        return None, None
    return times


async def bus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        names = sorted(_NUS_ROUTES.keys())
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"bus:{name}")]
            for name in names
        ]
        await update.message.reply_text(
            "🚌 *NUS Bus Services*\n  Tap a service to see its route:",
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
    sched_lines = _bus_schedule_lines(service)
    if sched_lines:
        lines.extend(sched_lines)
        lines.append("")

    for i, stop_name in enumerate(route, 1):
        stop = find_stop(stop_name)
        lines.append(f"{i}. {stop['caption'] if stop else stop_name}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def plan_got_dest_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle stop-picker button tap (or page navigation) in the /plan destination step."""
    query = update.callback_query
    await query.answer()

    if "_page:" in query.data:
        page = int(query.data.split("_page:")[1])
        await query.edit_message_reply_markup(
            reply_markup=_direction_keyboard("plan_to", page)
        )
        return PLAN_DEST

    origin     = context.user_data.pop("plan_origin", None)
    origin_loc = context.user_data.pop("plan_origin_loc", None)

    if not origin_loc:
        await query.edit_message_text("session expired, try /plan again")
        return ConversationHandler.END

    stop_name = query.data.split(":", 1)[1]
    d_stop = find_stop(stop_name)
    if not d_stop:
        await query.answer("stop not found — type it instead", show_alert=True)
        return PLAN_DEST

    o_lat, o_lng = origin_loc
    o_label = origin["caption"] if origin else "your location"

    await query.edit_message_text(
        f"📍 *{o_label} → {d_stop['caption']}*\nplanning route…", parse_mode="Markdown"
    )
    await _run_plan(
        query.message, origin, o_lat, o_lng, o_label,
        d_stop, d_stop["lat"], d_stop["lng"], d_stop["caption"], True,
    )
    return ConversationHandler.END


async def plan_got_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    origin     = context.user_data.get("plan_origin")
    origin_loc = context.user_data.get("plan_origin_loc")

    if not origin_loc:
        await update.message.reply_text("something went wrong, try /plan again")
        return ConversationHandler.END

    o_lat, o_lng = origin_loc

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
        BotCommand("plan",      "Route planner - share location, choose/type destination"),
        BotCommand("direction", "<from> to <dest>"),
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
                CallbackQueryHandler(plan_got_dest_stop, pattern=r"^plan_to"),
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
    direction_handler = ConversationHandler(
        entry_points=[CommandHandler(["direction", "destination"], direction_start)],
        states={
            DIRECTION_FROM: [
                CallbackQueryHandler(direction_got_from, pattern=r"^dir_from"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, direction_got_from),
            ],
            DIRECTION_TO: [
                CallbackQueryHandler(direction_got_to, pattern=r"^dir_to"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, direction_got_to),
            ],
        },
        fallbacks=[CommandHandler("cancel", direction_cancel)],
        allow_reentry=True,
    )
    app.add_handler(direction_handler)
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
