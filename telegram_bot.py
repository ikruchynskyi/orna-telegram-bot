import os
import logging

from dotenv import load_dotenv
load_dotenv()  # must run before importing modules that read env vars at import time (orna_sheets)

from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    Update,
)
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters, MessageHandler
from telegram_assess import build_assess_conversation
from telegram_resources import build_reminder_callback_handler, build_resource_conversation
from telegram_go import GO_ALLOWED_USER_IDS, build_go_callback_handler, build_go_continue_handler, build_go_handler
from telegram_remind import (build_remind_handler, build_tz_callback_handler,
                             build_tz_edit_callback_handler, reschedule_pending)
from telegram_orna import build_orna_callback_handler, build_orna_handler, build_update_codex_handler
from telegram_orna import _next_text, _today_text
import usage_stats

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")


async def today_resources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Thin wrapper: /res_today and /orna's own today() tool used to
    independently reimplement the exact same sheet-walk - now both call
    telegram_orna._today_text, one implementation to maintain (and this
    picks up that function's proper HTML-escaping for free)."""
    usage_stats.record_command_for(update, "res_today")
    message = update.effective_message
    if not message:
        return
    await message.reply_text(await _today_text(), parse_mode="HTML")


async def resource_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Thin wrapper over telegram_orna._next_text - see today_resources."""
    message = update.effective_message
    if not message:
        logger.warning("Handler called without an effective message.")
        return

    text = " ".join(context.args).strip()
    usage_stats.record_command_for(update, "res_next", text)
    if not text:
        await message.reply_text(
            "Вкажіть назву ресурсу. Приклад: /res_next Adamantine"
        )
        return

    result = await _next_text(text)
    if result is None:
        await message.reply_text(
            "Такий ресурс не знайдено. Приклад використання: /res_next Adamantine"
        )
        return
    await message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden admin command: report usage_stats' counters. Gated by the
    same GO_ALLOWED_USER_IDS allowlist /go and /update_codex use.

    /stats                  - overall totals (commands, LLM calls per
                              model+backend, known user count)
    /stats users            - every known user, sorted by activity
    /stats user <id|@name>  - one user's per-command counts + their last
                              40 questions (command + the text they typed)
    """
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    if GO_ALLOWED_USER_IDS and (not user or user.id not in GO_ALLOWED_USER_IDS):
        logger.warning("stats: rejected user_id=%s", user.id if user else None)
        return

    args = context.args or []

    if args and args[0].lower() == "users":
        rows = usage_stats.user_summary()
        if not rows:
            await message.reply_text("Ще немає даних по користувачам.")
            return
        lines = ["📊 Користувачі (за активністю):", ""]
        lines += [f"  {name}  (id {uid}) — {count}" for uid, name, count in rows]
        lines.append("")
        lines.append("/stats user <id|@username> — деталі й останні питання")
        await message.reply_text("\n".join(lines))
        return

    if args and args[0].lower() == "user":
        if len(args) < 2:
            await message.reply_text("Використання: /stats user <id або @username>")
            return
        uid = usage_stats.find_user(args[1])
        detail = usage_stats.user_detail(uid) if uid else None
        if detail is None:
            await message.reply_text(f"Не знайдено користувача {args[1]!r}.")
            return
        lines = [f"📊 {detail['display_name']} (id {detail['user_id']})", "", "Команди:"]
        for name, count in sorted(detail["commands"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  /{name}: {count}")
        lines.append("")
        lines.append(f"Останні питання (до {usage_stats.MAX_LOG_PER_USER}):")
        for entry in reversed(detail["recent"]):
            ts = entry["ts"].replace("T", " ")[:16]
            text = f" {entry['text']}" if entry["text"] else ""
            lines.append(f"  [{ts}] /{entry['command']}{text}")
        if not detail["recent"]:
            lines.append("  (ще немає даних)")
        await message.reply_text("\n".join(lines))
        return

    data = usage_stats.snapshot()
    lines = [f"📊 Статистика з {data['since']}", f"Користувачів: {data['user_count']}", "", "Команди:"]
    commands = data["commands"]
    if commands:
        for name, count in sorted(commands.items(), key=lambda kv: -kv[1]):
            lines.append(f"  /{name}: {count}")
    else:
        lines.append("  (ще немає даних)")
    lines.append("")
    lines.append("LLM виклики за моделлю:")
    llm_calls = data["llm_calls"]
    if llm_calls:
        for model, count in sorted(llm_calls.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {model}: {count}")
    else:
        lines.append("  (ще немає даних)")
    lines.append("")
    lines.append("Дії /orna (ReAct loop):")
    orna_tools = data["orna_tools"]
    if orna_tools:
        for action, count in sorted(orna_tools.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {action}: {count}")
    else:
        lines.append("  (ще немає даних)")
    lines.append("")
    lines.append("/stats users — список користувачів, /stats user <id> — деталі")
    await message.reply_text("\n".join(lines))


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global safety net: without this, PTB's default handling for an
    unhandled exception in any handler is to log it and reply with
    NOTHING - the user just sees silence. Reproduced live via a crash
    vector in the old telegram_nlp/telegram_go Ollama-JSON parsing (see
    ollama_client.py's docstring) before this existed; that specific bug
    is fixed at the source now, but this net stays regardless since any
    other future handler bug has the exact same silent-failure shape."""
    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Виникла непередбачена помилка. Спробуйте ще раз.")
        except Exception:
            logger.warning("Failed to notify user about the error", exc_info=True)


async def _post_init(app):
    # /go is the one command that must stay off this list, everything else
    # genuinely is meant to be user-visible autocomplete. Set in every
    # scope Telegram actually consults for a real chat, not just the
    # "default" one - get_my_commands showed all_private_chats/
    # all_group_chats/all_chat_administrators already had their own
    # (older, narrower - just res_today/res_next) list from outside this
    # repo, likely set via BotFather at some point, and those more
    # specific scopes silently shadow "default" so only the old 2 were
    # ever showing regardless of what "default" had.
    commands = [
        BotCommand("orna", "Запит про Orna (природною мовою)"),
        BotCommand("res_today", "Ресурси, доступні сьогодні"),
        BotCommand("res_next", "Коли з'явиться ресурс"),
        BotCommand("remind", "Поставити нагадування"),
    ]
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
    ):
        await app.bot.set_my_commands(commands, scope=scope)


def main():
    # concurrent_updates defaults to False (PTB processes every update one
    # at a time, globally, regardless of chat) - live incident: a single
    # complex /orna ReAct-loop request (which can legitimately run for
    # minutes - MAX_STEPS=16, LOOP_TIMEOUT_SECONDS=300) blocked EVERY
    # other command from EVERY user, including a trivial /res_today sent
    # right after it, until it finished. PTB's own docs warn concurrent
    # processing risks a race in stateful ConversationHandler flows (the
    # assess/resources conversations) if the SAME chat sends two messages
    # close together mid-flow - a real but narrow risk, far outweighed by
    # "the whole bot hangs for minutes" being the default otherwise. A
    # modest bound (not PTB's max-256 default for True) keeps most
    # concurrent activity naturally isolated to different chats/users.
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).concurrent_updates(32).build()
    app.add_error_handler(_on_error)
    app.add_handler(CommandHandler("res_today", today_resources))
    app.add_handler(CommandHandler("res_next", resource_next))
    # New unified entry point (routes "today"/"next"/codex-search intent via
    # a small local-Ollama call) - introduced alongside the above rather
    # than replacing them, so nothing existing breaks while this is proven
    # out. See telegram_orna.py.
    app.add_handler(build_orna_handler())
    app.add_handler(build_orna_callback_handler())
    # Not exposed via setMyCommands anywhere in this repo, so it stays out
    # of the Telegram command menu / autocomplete for regular Orna users.
    app.add_handler(build_go_handler())
    app.add_handler(build_go_callback_handler())
    # Also hidden, also GO_ALLOWED_USER_IDS-gated - a maintenance command
    # (force-refetch the aussiescodex cache now instead of waiting out its
    # 1-week TTL), not something a regular guild member needs.
    app.add_handler(build_update_codex_handler())
    # Same hidden/gated treatment - reports usage_stats' counters.
    app.add_handler(CommandHandler("stats", handle_stats))
    # Registered before the Orna conversations: its filter only matches a
    # chat that just tapped /go's "Continue" button, so it's a no-op (falls
    # through to assess/resources below) for every other chat/message.
    app.add_handler(build_go_continue_handler())
    app.add_handler(build_remind_handler())
    # "🔔 remind me" buttons on a resource report - public, not gated like
    # /remind itself (see telegram_remind.schedule_reminder's docstring).
    app.add_handler(build_reminder_callback_handler())
    # UTC-offset picker buttons - shared by /remind's own HH:MM form and
    # the reminder buttons above, both ask via telegram_remind.request_utc_offset.
    app.add_handler(build_tz_callback_handler())
    # "🔄 Змінити часовий пояс" on the confirmation - a mis-tapped offset is
    # saved forever and reused silently, so it has to be correctable, and by
    # the guild members who use the ungated reminder buttons, not just the
    # /remind allowlist.
    app.add_handler(build_tz_edit_callback_handler())
    app.add_handler(build_assess_conversation())
    # Registered last: only claims free text that assess's own conversation
    # (screenshot -> AWAITING_NAME) isn't currently handling for that chat.
    app.add_handler(build_resource_conversation())
    reschedule_pending(app)
    logger.info("🤖 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
