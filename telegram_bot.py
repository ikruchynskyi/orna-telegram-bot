import html
import os
import logging
import re

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
                             build_tz_edit_callback_handler, build_tz_input_handler,
                             reschedule_pending)
from telegram_orna import (build_ask_text_handler, build_chosen_inline_result_handler,
                          build_inline_query_handler, build_orna_callback_handler, build_orna_handler,
                          build_update_codex_handler)
from telegram_orna import _next_text, _today_text
import usage_stats

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


# Secrets that libraries put in URLs, which httpx then logs in full at INFO.
# Live incident 2026-09-24: the bot token was stolen and a third party polled
# getUpdates with it, answering this bot's users with a "join our channel"
# ad. python-telegram-bot puts the token in the PATH of every API call
# ("api.telegram.org/bot<token>/getUpdates"), so telegrambot_error.log held
# thousands of plaintext copies of it; orna_sheets does the same with
# ?key=<google api key>. The log is gitignored, so it never reached GitHub -
# but it is a 4MB plaintext credential file that anything reading the log
# (a paste, a screen share, a support request) hands over completely.
_SECRET_PATTERNS = [
    re.compile(r"(bot)\d{5,}:[A-Za-z0-9_-]{20,}"),                      # telegram bot token
    re.compile(r"((?:[?&])(?:key|api_key|apikey|access_token|token|auth)=)[^&\s\"']+", re.IGNORECASE),
]


class _RedactSecrets(logging.Filter):
    """Strip credentials out of every record before a handler writes it.

    Attached to the HANDLER rather than a logger, so it applies to records
    from every library (httpx is the one that matters) instead of only this
    module's. Rendering the message here and clearing args is deliberate:
    httpx logs "HTTP Request: %s %s" with the URL in `args`, so redacting
    `record.msg` alone would miss it entirely."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            return True
        redacted = text
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(lambda m: m.group(1) + "<redacted>", redacted)
        if redacted != text:
            record.msg, record.args = redacted, ()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactSecrets())

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")


# Telegram sends /start when someone opens the bot for the first time (the
# big "Start" button). Until 2026-09-24 nothing handled it, so a new guild
# member's very first interaction was SILENCE - the worst possible intro to a
# bot whose main feature is "just ask in your own words".
#
# Deliberately fixed text, not model-generated: it must be identical and
# correct for every newcomer, and this is the one message where being wrong
# about what the bot does costs most. It mentions only what a REGULAR member
# can actually use - /remind is in the command menu but gated to
# GO_ALLOWED_USER_IDS, so reminders are described via the buttons, which are
# genuinely open to everyone.
_WELCOME = (
    "\U0001F44B <b>Вітаю!</b> Я бот-помічник по грі Orna для нашої гільдії.\n\n"
    "<b>Головне: команди вчити не треба.</b> Напишіть <code>/orna</code> і своє питання "
    "звичайною мовою — українською або англійською.\n\n"
    "<b>Наприклад:</b>\n"
    "• <code>/orna balor sword</code> — знайти предмет у кодексі\n"
    "• <code>/orna що сьогодні</code> — які ресурси в гільдіях сьогодні\n"
    "• <code>/orna коли буде адамантин</code> — коли з'явиться ресурс\n"
    "• <code>/orna як вбити Лицаря Сіріуса</code> — тактика, імунітети, слабкості\n"
    "• <code>/orna шоломи для мага з магією понад 250</code> — пошук за характеристиками\n"
    "• <code>/orna порівняй X і Y</code> — що з двох краще\n"
    "• <code>/orna білд для heretic</code> — гайди спільноти по класах\n\n"
    "\U0001F4F8 <b>Можна просто надіслати скриншот:</b>\n"
    "• екран характеристик предмета — порахую, як він прокачається\n"
    "• екран «NEEDED OFFERINGS» з вівтаря — покажу, чого не вистачає\n\n"
    "⚡ <b>Швидкі команди:</b>\n"
    "• /res_today — ресурси на сьогодні\n"
    "• /res_next — коли з'явиться потрібний ресурс\n\n"
    "\U0001F514 У відповідях про ресурси будуть кнопки «нагадати» — натисніть, і я нагадаю "
    "в потрібний день.\n\n"
    "\U0001F4A1 <b>Що варто знати:</b>\n"
    "• Складне питання може оброблятись до хвилини — я показую, що саме зараз роблю.\n"
    "• Під відповіддю буває кнопка «\U0001F4DA Джерела» — там видно, звідки я взяв інформацію.\n"
    "• Якщо я перепитаю — можна натиснути кнопку або написати свою відповідь словами.\n\n"
    "🐞 Якщо щось не працює або відповідь неправильна — напишіть "
    "<code>/report опис проблеми</code>. Це дуже допомагає.\n\n"
    "❓ <b>Питайте що завгодно — не соромтесь.</b> Немає «неправильних» питань і не "
    "треба особливого формату. Якщо я чогось не знаю або не впевнений — так і скажу."
)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start - the first thing a new member ever sees."""
    message = update.effective_message
    if not message:
        return
    usage_stats.record_command_for(update, "start", "")
    await message.reply_text(_WELCOME, parse_mode="HTML", disable_web_page_preview=True)


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


# Telegram rejects a sendMessage over 4096 chars with BadRequest("Message is
# too long"). Live 2026-09-24: `/stats user <id>` dumps up to
# MAX_LOG_PER_USER questions WITH the text each user typed, and a single
# /orna question can run several hundred characters, so an active user's
# report blew the cap and the whole command failed with "Виникла
# непередбачена помилка" instead of showing anything.
TELEGRAM_MAX_CHARS = 4096


async def _send_lines(message, lines: list) -> None:
    """Send `lines` as as few messages as fit, splitting on line boundaries.

    Same idea as telegram_resources.send_report_blocks, kept separate because
    that one packs pre-built HTML report blocks and this is plain text. A
    single line longer than the cap is hard-split rather than dropped - the
    alternative is losing data silently, which is how this bug presented."""
    chunks, current = [], ""
    for line in lines:
        while len(line) > TELEGRAM_MAX_CHARS - 1:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[: TELEGRAM_MAX_CHARS - 1])
            line = line[TELEGRAM_MAX_CHARS - 1 :]
        if len(current) + len(line) + 1 > TELEGRAM_MAX_CHARS - 1:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    for chunk in chunks:
        await message.reply_text(chunk)


# One logged question printed in full can be hundreds of characters; the
# point of the log is WHAT was asked, not the whole essay.
_STATS_TEXT_PREVIEW = 200


async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report <description> - a guild member tells us something is broken.

    Deliberately UNGATED, unlike /stats and /update_codex: the people who hit
    bugs are exactly the ones who cannot use admin commands. Stored per user
    (newest 10 kept) AND pushed to the allowlist immediately, because a report
    nobody is told about is just a log line - every bug fixed in this bot so
    far arrived as a message, not as a stored record."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    text = " ".join(context.args or []).strip()
    usage_stats.record_command_for(update, "report", text)
    if not text:
        await message.reply_text(
            "Опишіть проблему одним повідомленням, наприклад:\n"
            "/report кнопка «нагадати» не спрацювала для адамантину\n\n"
            "Що допомагає: що ви робили, що очікували і що сталося насправді."
        )
        return

    entry = usage_stats.record_report(
        user.id if user else None,
        getattr(user, "username", None),
        getattr(user, "first_name", None),
        text,
    )
    await message.reply_text("✅ Дякую! Звіт збережено — розробник побачить його.")

    # Best-effort per recipient: one blocked chat must not swallow the report
    # for the others, and the user has already been told it was saved.
    note = (f"🐞 <b>Новий звіт про помилку</b>\n"
            f"від {html.escape(entry['display_name'])} (id {entry['user_id']})\n\n"
            f"{html.escape(entry['text'])}")
    for admin_id in GO_ALLOWED_USER_IDS:
        if user and admin_id == user.id:
            continue                      # don't notify the reporter about themselves
        try:
            await context.bot.send_message(admin_id, note, parse_mode="HTML")
        except Exception:
            logger.warning("report: could not notify admin %s", admin_id, exc_info=True)


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

    if args and args[0].lower() == "reports":
        reports = usage_stats.all_reports()
        if not reports:
            await message.reply_text("Звітів про помилки ще немає.")
            return
        lines = [f"🐞 Звіти про помилки ({len(reports)}):", ""]
        for r in reports:
            lines.append(f"[{r['ts'].replace('T', ' ')[:16]}] {r['display_name']} (id {r['user_id']})")
            lines.append(f"  {r['text']}")
        await _send_lines(message, lines)
        return

    if args and args[0].lower() == "users":
        rows = usage_stats.user_summary()
        if not rows:
            await message.reply_text("Ще немає даних по користувачам.")
            return
        lines = ["📊 Користувачі (за активністю):", ""]
        lines += [f"  {name}  (id {uid}) — {count}" for uid, name, count in rows]
        lines.append("")
        lines.append("/stats user <id|@username> — деталі й останні питання")
        await _send_lines(message, lines)
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
            raw = entry["text"] or ""
            if len(raw) > _STATS_TEXT_PREVIEW:
                raw = raw[:_STATS_TEXT_PREVIEW] + "…"
            text = f" {raw}" if raw else ""
            lines.append(f"  [{ts}] /{entry['command']}{text}")
        if not detail["recent"]:
            lines.append("  (ще немає даних)")
        await _send_lines(message, lines)
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
    lines.append("/stats users — список користувачів, /stats user <id> — деталі, "
                 "/stats reports — звіти про помилки")
    await _send_lines(message, lines)


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
        BotCommand("start", "Що вміє бот і як питати"),
        BotCommand("orna", "Запит про Orna (природною мовою)"),
        BotCommand("res_today", "Ресурси, доступні сьогодні"),
        BotCommand("res_next", "Коли з'явиться ресурс"),
        BotCommand("report", "Повідомити про помилку"),
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
    # Inline mode: "@<bot> <query>" typed in ANY chat, including groups the bot
    # was never added to, routes to the SAME /orna loop; the answer is edited
    # into the single inline message the user's chosen result posts. Needs
    # BotFather setup: /setinline (enable inline) AND /setinlinefeedback -> 100%
    # (so the chosen-result update carrying inline_message_id is delivered).
    app.add_handler(build_inline_query_handler())
    app.add_handler(build_chosen_inline_result_handler())
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
    # /start and /help both land on the welcome text - a newcomer tries
    # whichever occurs to them, and Telegram itself sends /start on open.
    app.add_handler(CommandHandler("report", handle_report))
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_start))
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
    # The typed answer to the timezone ask. Registered BEFORE the assess and
    # resources conversations, like /go's Continue handler: its filter only
    # matches a chat with a live timezone ask, so for every other chat it is a
    # guaranteed no-op that falls through to them untouched.
    app.add_handler(build_tz_input_handler())
    # The typed answer to /orna's clarifying question ("Своя відповідь", or an
    # "Інше"-style option the model offered). Same narrow-filter-before-the-
    # conversations rule as the two handlers above.
    app.add_handler(build_ask_text_handler())
    app.add_handler(build_assess_conversation())
    # Registered last: only claims free text that assess's own conversation
    # (screenshot -> AWAITING_NAME) isn't currently handling for that chat.
    app.add_handler(build_resource_conversation())
    reschedule_pending(app)
    logger.info("🤖 Bot is running...")
    # Explicit allowed_updates so inline_query/chosen_inline_result are always
    # polled (the default set includes them, but a previously-set restrictive
    # value persists server-side otherwise). Lists exactly the update types the
    # bot handles - no chat_member/reaction noise.
    app.run_polling(allowed_updates=[
        "message", "edited_message", "callback_query", "inline_query", "chosen_inline_result",
    ])


if __name__ == "__main__":
    main()
