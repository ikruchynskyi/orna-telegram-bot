import os
import datetime
import logging
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()  # must run before importing modules that read env vars at import time (orna_sheets)

import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters, MessageHandler
from telegram_assess import build_assess_conversation
from telegram_resources import build_resource_conversation
from telegram_go import build_go_callback_handler, build_go_handler
from orna_sheets import GUILD_NAMES, fetch_sheet_data, get_today_month_day

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")


async def today_resources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    logger.info("res_today from chat_id=%s user_id=%s", chat_id, user_id)

    today = get_today_month_day()

    try:
        values = await fetch_sheet_data()
    except httpx.HTTPError as e:
        logger.exception("Failed to fetch sheet data")
        await update.message.reply_text(f"Не вдалося отримати дані: {e}")
        return

    tdg: dict[str, list[str]] = defaultdict(list)
    for res in values:
        res_name = res[0]
        for i, date in enumerate(res[1:]):
            if date == today and i < len(GUILD_NAMES):
                tdg[GUILD_NAMES[i]].append(res_name)

    if not tdg:
        await update.message.reply_text(f"Сьогодні ({today}) немає ресурсів.")
        return

    msg = [f"Ресурси {today}"]
    for guild, materials in tdg.items():
        msg.append(f"{guild}{' ' * 6}{', '.join(materials)}")

    await update.message.reply_text("\n".join(msg))


async def resource_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        logger.warning("Handler called without an effective message.")
        return

    text = " ".join(context.args).lower().strip()
    if not text:
        await message.reply_text(
            "Вкажіть назву ресурсу. Приклад: /res_next Adamantine"
        )
        return

    try:
        values = await fetch_sheet_data()
    except httpx.HTTPError as e:
        logger.exception("Failed to fetch sheet data")
        await message.reply_text(f"Не вдалося отримати дані: {e}")
        return

    msg = [f"Наступні гільдії і дати коли з’явиться ресурс {text}"]
    for res in values:
        res_name = res[0]
        if text not in res_name.lower():
            continue

        guild_dates = [(GUILD_NAMES[i], date) for i, date in enumerate(res[1:]) if i < len(GUILD_NAMES) and date]

        try:
            guild_dates.sort(key=lambda x: datetime.datetime.strptime(x[1], '%B %d'))
        except ValueError:
            logger.warning("Malformed date in row: %s", res)
            continue

        msg.append(f"<b>{res_name}:</b>")
        for guild, date in guild_dates:
            msg.append(f"{guild}{' ' * 6}{date}")
        msg.append(" ")

    if len(msg) == 1:
        await message.reply_text(
            "Такий ресурс не знайдено. Приклад використання: /res_next Adamantine"
        )
        return

    await message.reply_text("\n".join(msg), parse_mode="HTML")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("res_today", today_resources))
    app.add_handler(CommandHandler("res_next", resource_next))
    # Not exposed via setMyCommands anywhere in this repo, so it stays out
    # of the Telegram command menu / autocomplete for regular Orna users.
    app.add_handler(build_go_handler())
    app.add_handler(build_go_callback_handler())
    app.add_handler(build_assess_conversation())
    # Registered last: only claims free text that assess's own conversation
    # (screenshot -> AWAITING_NAME) isn't currently handling for that chat.
    app.add_handler(build_resource_conversation())
    logger.info("🤖 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
