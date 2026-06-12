import logging
import os
from datetime import datetime, timedelta, timezone

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
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from api import BusStopArrivals, get_all_arrivals, get_arrivals_async
from favourites import get_favourites, init_db, is_favourite, toggle_favourite
from planner import geocode_with_candidates, get_directions, get_transit_to_stop
from routes import _BUKIT_TIMAH_STOPS, _BUS_SCHEDULE, _NUS_ROUTES
from routing import (
    _append_directions_block,
    _best_dest_stop,
    _fmt_time,
    _route_offcampus_to_campus,
    _route_on_campus,
)
from stops import STOPS, find_stop, nearby_stops

load_dotenv()

NEARBY_LOCATION = 0
GO_FROM, GO_TO = 1, 2

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
    """All STOPS not in the quick-pick list, sorted A–Z by caption.
    'Opp X' sorts immediately after 'X' so the main stop precedes its opposite side."""
    def _key(item: tuple[str, str]) -> tuple[str, int]:
        cap = item[1]
        if cap.startswith("Opp "):
            return (cap[4:], 1)
        return (cap, 0)
    return sorted(
        [(s["name"], s["caption"]) for s in STOPS if s["name"] not in _DIRECTION_STOP_NAMES],
        key=_key,
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


def escape_md(text: str) -> str:
    """Escape Telegram legacy-Markdown specials in user-derived text.

    Free-text locations/queries are interpolated into ``parse_mode="Markdown"``
    messages; an unescaped ``*``/``_``/``[``/`` ` `` would raise BadRequest and
    the user would get no reply.
    """
    if not text:
        return text
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


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


def _arrival_keyboard(user_id: int, stop_name: str, with_back: bool = True) -> InlineKeyboardMarkup:
    """Refresh + favourite (+ optional 'back to stops') buttons under an arrivals message."""
    rows = [[
        InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{stop_name}"),
        _fav_button(user_id, stop_name),
    ]]
    if with_back:
        rows.append([InlineKeyboardButton("⬅ Back to stops", callback_data="page:0")])
    return InlineKeyboardMarkup(rows)


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


def format_all(results: list[BusStopArrivals | None]) -> list[str]:
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
        "*Check arrivals*\n"
        "• /arrivals — pick a stop and see live bus times\n"
        "• /all — every stop at a glance\n\n"
        "*Get directions*\n"
        "• /go `CLB to UTOWN` — instant route between any two stops or buildings\n"
        "• /go — tap to pick stops or share your location\n\n"
        "*Other*\n"
        "• /bus `A1` — see a bus route and schedule\n"
        "• /fav — your saved stops ⭐",
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
        "NUS stops near you 👇",
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
            reply_markup=_arrival_keyboard(user_id, stop["name"], with_back=False),
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
            await query.edit_message_text(
                format_arrivals(arrivals),
                parse_mode="Markdown",
                reply_markup=_arrival_keyboard(user_id, stop_name),
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
            await query.edit_message_text(
                format_arrivals(arrivals),
                parse_mode="Markdown",
                reply_markup=_arrival_keyboard(user_id, stop_name),
            )
        except Exception:
            logger.exception("Failed to refresh arrivals for %s", stop_name)
    elif data.startswith("fav:"):
        stop_name = data.split(":", 1)[1]
        user_id = query.from_user.id
        added = toggle_favourite(user_id, stop_name)
        await query.answer("Added to favourites! ⭐" if added else "Removed from favourites.")
        await query.edit_message_reply_markup(reply_markup=_arrival_keyboard(user_id, stop_name))
    elif data.startswith("go_dest_candidates:"):
        await query.answer()
        _, _, idx_str = data.partition(":")
        idx = int(idx_str)
        candidates = context.user_data.pop("go_dest_candidates", [])
        if not candidates or idx >= len(candidates):
            await query.edit_message_text("something went wrong, try again")
            return
        c = candidates[idx]
        d_lat, d_lng, d_label = c["lat"], c["lng"], c["label"]
        nearby = nearby_stops(d_lat, d_lng, radius_m=800)
        d_stop = nearby[0] if nearby else None
        pending = context.user_data.pop("go_pending_origin", {})
        origin  = pending.get("stop")
        o_lat   = pending.get("lat")
        o_lng   = pending.get("lng")
        o_label = pending.get("label", "your location")
        if o_lat is None:
            await query.edit_message_text("session expired, please try again")
            return
        await query.edit_message_text(f"📍 got it — routing to *{escape_md(d_label)}*", parse_mode="Markdown")
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


async def _resolve_with_candidates(query: str) -> tuple:
    """
    Resolve a query to a location, returning a candidates list when the query is
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
    has_key = bool(os.environ.get("GOOGLE_MAPS_API_KEY", ""))
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
        lines = [f"🗺 *{escape_md(o_label)} → {escape_md(d_label)}*\n"]

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


async def go_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = " ".join(context.args).strip() if context.args else ""
    if " to " in text.lower():
        idx     = text.lower().index(" to ")
        o_query = text[:idx].strip()
        d_query = text[idx + 4:].strip()
        if o_query and d_query:
            o_stop, o_lat, o_lng, o_label, _, _ = await _resolve_with_candidates(o_query)
            d_stop, d_lat, d_lng, d_label, d_is_exact, d_cands = await _resolve_with_candidates(d_query)
            if o_lat is None:
                await update.message.reply_text(f"couldn't find origin: *{escape_md(o_query)}* 😭", parse_mode="Markdown")
                return ConversationHandler.END
            if d_lat is None:
                await update.message.reply_text(f"couldn't find destination: *{escape_md(d_query)}* 😭", parse_mode="Markdown")
                return ConversationHandler.END
            if d_cands:
                context.user_data["go_pending_origin"] = {"stop": o_stop, "lat": o_lat, "lng": o_lng, "label": o_label}
                await _ask_which_location(update.message, context, d_cands, "go_dest_candidates")
            else:
                await _run_plan(update.message, o_stop, o_lat, o_lng, o_label, d_stop, d_lat, d_lng, d_label, d_is_exact)
            return ConversationHandler.END

    await update.message.reply_text(
        "🗺 *Route planner*\n\nWhere are you coming *from*?\nTap a stop or type a location 👇",
        parse_mode="Markdown",
        reply_markup=_direction_keyboard("go_from"),
    )
    await update.message.reply_text(
        "or share your location 📍",
        reply_markup=_location_keyboard(),
    )
    return GO_FROM


async def go_got_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        data = update.callback_query.data

        if "_page:" in data:
            page = int(data.split("_page:")[1])
            await update.callback_query.edit_message_reply_markup(
                reply_markup=_direction_keyboard("go_from", page)
            )
            return GO_FROM

        stop_name = data.split(":", 1)[1]
        stop = find_stop(stop_name)
        label = stop["caption"] if stop else stop_name
        context.user_data.update({
            "go_o_stop": stop,
            "go_o_lat": stop["lat"] if stop else None,
            "go_o_lng": stop["lng"] if stop else None,
            "go_o_label": label,
        })
        await update.callback_query.edit_message_text(
            f"📍 From: *{escape_md(label)}*\n\nWhere are you going *to*?\nTap a stop or type any location 👇",
            parse_mode="Markdown",
            reply_markup=_direction_keyboard("go_to"),
        )
        return GO_TO

    elif update.message.location:
        loc = update.message.location
        stops = nearby_stops(loc.latitude, loc.longitude, radius_m=800)
        origin = stops[0] if stops else None
        label = origin["caption"] if origin else "your location"
        context.user_data.update({
            "go_o_stop": origin,
            "go_o_lat": loc.latitude,
            "go_o_lng": loc.longitude,
            "go_o_label": label,
        })
        await update.message.reply_text("📍 got your location", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text(
            f"From: *{escape_md(label)}*\n\nWhere are you going *to*?\nTap a stop or type any location 👇",
            parse_mode="Markdown",
            reply_markup=_direction_keyboard("go_to"),
        )
        return GO_TO

    else:
        query = update.message.text.strip()
        o_stop, o_lat, o_lng, o_label, _, _ = await _resolve_with_candidates(query)
        if o_lat is None:
            await update.message.reply_text(
                f"couldn't find *{escape_md(query)}* 😭\ntry again or tap a stop above",
                parse_mode="Markdown",
            )
            return GO_FROM
        context.user_data.update({
            "go_o_stop": o_stop,
            "go_o_lat": o_lat,
            "go_o_lng": o_lng,
            "go_o_label": o_label,
        })
        await update.message.reply_text(
            f"📍 From: *{escape_md(o_label)}*\n\nWhere are you going *to*?\nTap a stop or type any location 👇",
            parse_mode="Markdown",
            reply_markup=_direction_keyboard("go_to"),
        )
        return GO_TO


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


def _bus_first_last(service: str) -> tuple[str | None, str | None]:
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


async def go_got_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    o_stop  = context.user_data.pop("go_o_stop",  None)
    o_lat   = context.user_data.pop("go_o_lat",   None)
    o_lng   = context.user_data.pop("go_o_lng",   None)
    o_label = context.user_data.pop("go_o_label", "your location")

    if o_lat is None:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text("session expired, try /go again")
        return ConversationHandler.END

    if update.callback_query:
        await update.callback_query.answer()
        data = update.callback_query.data

        if "_page:" in data:
            context.user_data.update({"go_o_stop": o_stop, "go_o_lat": o_lat,
                                       "go_o_lng": o_lng, "go_o_label": o_label})
            page = int(data.split("_page:")[1])
            await update.callback_query.edit_message_reply_markup(
                reply_markup=_direction_keyboard("go_to", page)
            )
            return GO_TO

        stop_name = data.split(":", 1)[1]
        d_stop = find_stop(stop_name)
        d_lat = d_stop["lat"] if d_stop else None
        d_lng = d_stop["lng"] if d_stop else None
        d_label = d_stop["caption"] if d_stop else stop_name
        await update.callback_query.edit_message_text(
            f"📍 *{escape_md(o_label)} → {escape_md(d_label)}*\nplanning route…", parse_mode="Markdown"
        )
        await _run_plan(update.callback_query.message, o_stop, o_lat, o_lng, o_label,
                        d_stop, d_lat, d_lng, d_label, True)
        return ConversationHandler.END

    elif update.message.location:
        loc = update.message.location
        d_stops = nearby_stops(loc.latitude, loc.longitude, radius_m=800)
        d_stop = d_stops[0] if d_stops else None
        d_lat, d_lng = loc.latitude, loc.longitude
        d_label = d_stop["caption"] if d_stop else "your destination"
        await update.message.reply_text(
            f"📍 *{escape_md(o_label)} → {escape_md(d_label)}*\nplanning route…",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        await _run_plan(update.message, o_stop, o_lat, o_lng, o_label,
                        d_stop, d_lat, d_lng, d_label, False)
        return ConversationHandler.END

    else:
        query = update.message.text.strip()
        d_stop, d_lat, d_lng, d_label, d_is_exact, d_cands = await _resolve_with_candidates(query)
        if d_lat is None:
            context.user_data.update({"go_o_stop": o_stop, "go_o_lat": o_lat,
                                       "go_o_lng": o_lng, "go_o_label": o_label})
            await update.message.reply_text(
                f"couldn't find *{escape_md(query)}* 😭\ntry again or tap a stop above",
                parse_mode="Markdown",
            )
            return GO_TO
        if d_cands:
            context.user_data.update({"go_o_stop": o_stop, "go_o_lat": o_lat,
                                       "go_o_lng": o_lng, "go_o_label": o_label})
            await _ask_which_location(update.message, context, d_cands, "go_dest_candidates")
            return GO_TO
        await _run_plan(update.message, o_stop, o_lat, o_lng, o_label,
                        d_stop, d_lat, d_lng, d_label, d_is_exact)
        return ConversationHandler.END


async def go_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for k in ("go_o_stop", "go_o_lat", "go_o_lng", "go_o_label", "go_pending_origin"):
        context.user_data.pop(k, None)
    await update.message.reply_text("cancelled 👍", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def _is_admin(user_id: int) -> bool:
    """True only for the configured ADMIN_USER_ID. Diagnostics are admin-only."""
    admin = os.environ.get("ADMIN_USER_ID", "")
    return admin.isdigit() and int(admin) == user_id


async def debugplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden admin diagnostic: calls transit API from a fixed off-campus point to KR-MRT."""
    if not _is_admin(update.effective_user.id):
        await unknown_command(update, context)
        return
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
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
        BotCommand("arrivals", "Live bus times at a stop"),
        BotCommand("go",       "Route planner — /go CLB to UTOWN"),
        BotCommand("all",      "All stops at a glance"),
        BotCommand("bus",      "Bus route & schedule — /bus A1"),
        BotCommand("fav",      "Your saved stops ⭐"),
        BotCommand("help",     "How to use this bot"),
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

    go_handler = ConversationHandler(
        entry_points=[CommandHandler(["go", "plan", "direction", "destination"], go_start)],
        states={
            GO_FROM: [
                CallbackQueryHandler(go_got_from, pattern=r"^go_from"),
                MessageHandler(filters.LOCATION, go_got_from),
                MessageHandler(filters.TEXT & ~filters.COMMAND, go_got_from),
            ],
            GO_TO: [
                CallbackQueryHandler(go_got_to, pattern=r"^go_to"),
                MessageHandler(filters.LOCATION, go_got_to),
                MessageHandler(filters.TEXT & ~filters.COMMAND, go_got_to),
            ],
        },
        fallbacks=[CommandHandler("cancel", go_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      help_command))
    app.add_handler(CommandHandler("all",       all_command))
    app.add_handler(CommandHandler("stops",     stops_command))
    app.add_handler(CommandHandler("arrivals",  arrivals_command))
    app.add_handler(CommandHandler("bus",       bus_command))
    app.add_handler(CommandHandler("debugplan", debugplan_command))
    app.add_handler(nearby_handler)
    app.add_handler(go_handler)
    app.add_handler(CommandHandler("fav",       fav_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Starting bot (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
