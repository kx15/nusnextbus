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
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from api import BusStopArrivals, get_all_arrivals, get_arrivals_async
from favourites import get_favourites, init_db, is_favourite, toggle_favourite
from planner import get_walking_directions
from stops import STOPS, find_stop, nearby_stops

load_dotenv()

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
        "• /plan `<from> <to>` — route planner (e.g. `/plan CLB UTOWN`)\n"
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


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /plan <from> <to>\ne.g. `/plan CLB UTOWN`",
            parse_mode="Markdown",
        )
        return

    args = context.args
    origin_stop = dest_stop = None

    # Try every split point so multi-word captions work (e.g. "college green")
    for i in range(1, len(args)):
        o = find_stop(" ".join(args[:i]))
        d = find_stop(" ".join(args[i:]))
        if o and d:
            origin_stop, dest_stop = o, d
            break

    if not origin_stop or not dest_stop:
        await update.message.reply_text(
            "couldn't find those stops bestie 😭\nuse /stops to browse all stops"
        )
        return

    if origin_stop["name"] == dest_stop["name"]:
        await update.message.reply_text("you're already there lol 💀")
        return

    msg = await update.message.reply_text("planning your route one sec 👀")

    try:
        origin_arrivals, dest_arrivals, walking = await asyncio.gather(
            get_arrivals_async(origin_stop["name"]),
            get_arrivals_async(dest_stop["name"]),
            get_walking_directions(
                origin_stop["lat"], origin_stop["lng"],
                dest_stop["lat"], dest_stop["lng"],
            ),
            return_exceptions=True,
        )

        lines = [f"🗺 *{origin_stop['caption']} → {dest_stop['caption']}*\n"]

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
                lines.append("no direct bus found — might need a transfer or just walk 🚶\n")

        if not isinstance(walking, Exception) and walking:
            if walking.get("duration"):
                lines.append(f"🚶 *walking*: {walking['distance']} · {walking['duration']}")
            lines.append(f"[open in Google Maps]({walking['maps_url']})")

        await msg.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Plan command failed")
        await msg.edit_text("something broke 💀 try again")


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",    "What is this app"),
        BotCommand("all",      "All bus arrivals"),
        BotCommand("arrivals", "Select stop to get arrival time"),
        BotCommand("plan",     "Plan a route between two stops"),
        BotCommand("nearby",   "Find stops near you 📍"),
        BotCommand("fav",      "Your favourite stops"),
        BotCommand("help",     "Show this message"),
    ])


def main() -> None:
    init_db()
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("all",      all_command))
    app.add_handler(CommandHandler("stops",    stops_command))
    app.add_handler(CommandHandler("arrivals", arrivals_command))
    app.add_handler(CommandHandler("plan",     plan_command))
    app.add_handler(CommandHandler("nearby",   nearby_command))
    app.add_handler(CommandHandler("fav",      fav_command))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Starting bot (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
