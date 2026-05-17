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
from planner import geocode_nus, get_walking_directions
from stops import STOPS, find_stop, nearby_stops

load_dotenv()

PLAN_ORIGIN, PLAN_DEST = range(2)

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


async def nearby_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "where are you? 📍",
        reply_markup=_location_keyboard(),
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loc = update.message.location
    stops = nearby_stops(loc.latitude, loc.longitude, radius_m=500)
    if not stops:
        await update.message.reply_text("no stops nearby... you might be off campus 💀")
        return
    buttons = [
        [InlineKeyboardButton(
            f"🚏 {s['name']} — {s['caption']} ({s['dist']} m)",
            callback_data=f"stop:{s['name']}",
        )]
        for s in stops[:5]
    ]
    await update.message.reply_text(
        f"found {len(stops)} stop(s) nearby 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


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


async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "where are you? 📍",
        reply_markup=_location_keyboard(),
    )
    return PLAN_ORIGIN


async def plan_got_origin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    stops = nearby_stops(loc.latitude, loc.longitude, radius_m=800)
    if not stops:
        await update.message.reply_text(
            "you don't seem to be on NUS campus 💀\ntry /plan again from within NUS",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    origin = stops[0]
    context.user_data["plan_origin"] = origin
    context.user_data["plan_origin_loc"] = (loc.latitude, loc.longitude)

    await update.message.reply_text(
        f"📍 nearest stop: *{origin['caption']}*\n\nwhere are you going? 🏫\n_type a place or stop name_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PLAN_DEST


async def plan_got_dest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    origin = context.user_data.get("plan_origin")
    origin_loc = context.user_data.get("plan_origin_loc")

    if not origin:
        await update.message.reply_text("something went wrong, try /plan again")
        return ConversationHandler.END

    dest_stop = None
    dest_lat = dest_lng = None
    dest_label = None

    if update.message.location:
        loc = update.message.location
        dest_lat, dest_lng = loc.latitude, loc.longitude
        stops = nearby_stops(dest_lat, dest_lng, radius_m=800)
        if stops:
            dest_stop = stops[0]
            dest_label = dest_stop["caption"]
    else:
        query = update.message.text.strip()
        dest_stop = find_stop(query)
        if dest_stop:
            dest_lat, dest_lng = dest_stop["lat"], dest_stop["lng"]
            dest_label = dest_stop["caption"]
        else:
            coords = await geocode_nus(query)
            if coords:
                dest_lat, dest_lng = coords
                dest_label = query
                stops = nearby_stops(dest_lat, dest_lng, radius_m=800)
                if stops:
                    dest_stop = stops[0]

    if not dest_stop or dest_lat is None:
        await update.message.reply_text(
            "couldn't find that place 😭\ntry a different name or share your destination 📍"
        )
        return PLAN_DEST

    if dest_stop["name"] == origin["name"]:
        await update.message.reply_text(
            "that's where you already are lol 💀\nwhere do you actually wanna go?"
        )
        return PLAN_DEST

    msg = await update.message.reply_text("planning your route one sec 👀")

    try:
        origin_arrivals, dest_arrivals, walking = await asyncio.gather(
            get_arrivals_async(origin["name"]),
            get_arrivals_async(dest_stop["name"]),
            get_walking_directions(
                origin_loc[0], origin_loc[1],
                dest_lat, dest_lng,
            ),
            return_exceptions=True,
        )

        lines = [f"🗺 *{origin['caption']} → {dest_label}*\n"]

        if not isinstance(origin_arrivals, Exception) and not isinstance(dest_arrivals, Exception):
            origin_names = {t.name for t in origin_arrivals.timings if not t.name.strip().isdigit()}
            dest_names   = {t.name for t in dest_arrivals.timings   if not t.name.strip().isdigit()}
            common = origin_names & dest_names
            if common:
                lines.append("🚌 *buses serving both stops:*")
                for t in origin_arrivals.timings:
                    if t.name in common:
                        lines.append(
                            f"  *{t.name}*: {_fmt_time(t.arrival_time)}"
                            f" | Next: {_fmt_time(t.next_arrival_time)}"
                        )
                lines.append("")
            else:
                lines.append("no direct bus — might need a transfer or just walk 🚶\n")

        if not isinstance(walking, Exception) and walking:
            if walking.get("duration"):
                lines.append(f"🚶 *walking*: {walking['distance']} · {walking['duration']}")
            for i, step in enumerate(walking.get("steps", []), 1):
                lines.append(f"{i}. {step['instruction']} _({step['distance']})_")
            if walking.get("steps"):
                lines.append("")
            lines.append(f"[open in Google Maps]({walking['maps_url']})")

        await msg.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Plan conversation failed")
        await msg.edit_text("something broke 💀 try again")

    context.user_data.pop("plan_origin", None)
    context.user_data.pop("plan_origin_loc", None)
    return ConversationHandler.END


async def plan_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("plan_origin", None)
    context.user_data.pop("plan_origin_loc", None)
    await update.message.reply_text("plan cancelled 👍", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",    "What is this app"),
        BotCommand("all",      "All bus arrivals"),
        BotCommand("arrivals", "Select stop to get arrival time"),
        BotCommand("plan",     "Plan a route to anywhere on campus"),
        BotCommand("nearby",   "Find stops near you 📍"),
        BotCommand("fav",      "Your favourite stops"),
        BotCommand("help",     "Show this message"),
    ])


def main() -> None:
    init_db()
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = Application.builder().token(token).post_init(post_init).build()

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
    )

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("all",      all_command))
    app.add_handler(CommandHandler("stops",    stops_command))
    app.add_handler(CommandHandler("arrivals", arrivals_command))
    app.add_handler(plan_handler)
    app.add_handler(CommandHandler("nearby",   nearby_command))
    app.add_handler(CommandHandler("fav",      fav_command))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Starting bot (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
