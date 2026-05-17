import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from api import BusStopArrivals, get_all_arrivals, get_arrivals_async
from favourites import get_favourites, init_db, is_favourite, toggle_favourite
from stops import STOPS, find_stop

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
        "• /fav — your usual stops ⭐\n"
        "• /help — what is this app",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


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


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start",    "What is this app"),
        BotCommand("all",      "All bus arrivals"),
        BotCommand("arrivals", "Select stop to get arrival time"),
        BotCommand("fav",      "Your favourite stops"),
        BotCommand("help",     "Show this message"),
    ])


def main() -> None:
    init_db()
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("all", all_command))
    app.add_handler(CommandHandler("stops", stops_command))
    app.add_handler(CommandHandler("arrivals", arrivals_command))
    app.add_handler(CommandHandler("fav", fav_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Starting bot (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
