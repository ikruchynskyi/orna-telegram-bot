"""Relay game announcements from Discord to Telegram, translated to Ukrainian.

orna_discord reads the Discord channel the official announcement channels are
FOLLOWED into; this module translates each new announcement and posts it to
every Telegram group and channel the bot is in.

REDEEM CODES ARE NEVER TRANSLATED - by construction, not by instruction. An LLM
told "do not translate the code" still sometimes re-cases it, re-spaces it, or
"helpfully" renders a word-like code in Ukrainian, and a code that is off by one
character is worse than none: it looks valid and fails. So protect() lifts every
code (and inline code span and URL) OUT of the text before translation and puts
a placeholder in its place; the model never sees a code at all. restore() puts
them back byte-for-byte, and a code whose placeholder the model lost is appended
on its own line rather than dropped. Restored codes are sent as <code>, which
Telegram renders as tap-to-copy on mobile.

WHICH TELEGRAM CHATS. The Bot API cannot list the chats a bot is in, so they are
tracked: a chat is registered when the bot is added (my_chat_member) or when the
bot sees any message or channel post there - which also catches chats it was in
before this feature existed. Private chats never get announcements. A group
admin can stop them with `/announcements off`; a chat the bot was removed from
(Forbidden) is dropped automatically, and a group upgraded to a supergroup
(ChatMigrated) follows its new id. State lives in gitignored
announce_chats.json, written atomically like every state file in this repo.

FIRST RUN STARTS FROM "NOW". With no cursor, the job records the newest message
in the channel and sends nothing - otherwise the first deploy would flood every
chat with the channel's whole history.
"""

from __future__ import annotations

import paths
import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from orna import orna_discord
from bot.telegram_go import GO_ALLOWED_USER_IDS, _markdown_to_html

logger = logging.getLogger(__name__)

STATE_PATH = paths.STATE / "announce_chats.json"
POLL_INTERVAL_SECONDS = 120
# The translation model is telegram_orna.TRANSLATION_MODEL - one choice for
# every translation the bot makes; the measurement behind it is noted there.
MAX_IMAGES = 3
# Below Telegram's 4096 with room for the header, footer and HTML tags that
# conversion adds; split on paragraph boundaries so a tag is never cut.
CHUNK_CHARS = 3500
_BROADCAST_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}

# ---------------------------------------------------------------------------
# State: which chats, and how far the Discord channel has been read
# ---------------------------------------------------------------------------

_state: Optional[dict] = None


def _load() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _state = {}
        except Exception:
            logger.warning("announce: %s unreadable - starting empty", STATE_PATH, exc_info=True)
            _state = {}
        _state.setdefault("chats", {})
        _state.setdefault("cursor", None)
    return _state


def _save() -> None:
    """Atomic: a launchctl reload mid-write must not leave a torn file. Called
    with no await between the change and the write, so two handlers running
    concurrently on the event loop cannot interleave a read-modify-write."""
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_load(), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def register(chat) -> None:
    if chat is None or chat.type not in _BROADCAST_TYPES:
        return
    chats = _load()["chats"]
    key = str(chat.id)
    if key not in chats:
        chats[key] = {"title": chat.title or "", "type": str(chat.type), "enabled": True}
        _save()
        logger.info("announce: registered %s %r", key, chat.title)
    elif chats[key].get("title") != (chat.title or ""):
        chats[key]["title"] = chat.title or ""
        _save()


def unregister(chat_id) -> None:
    if _load()["chats"].pop(str(chat_id), None) is not None:
        _save()
        logger.info("announce: dropped chat %s", chat_id)


def set_enabled(chat_id, enabled: bool) -> None:
    entry = _load()["chats"].get(str(chat_id))
    if entry is not None:
        entry["enabled"] = enabled
        _save()


def enabled_chats() -> list:
    return [int(k) for k, v in _load()["chats"].items() if v.get("enabled", True)]


# ---------------------------------------------------------------------------
# Protect codes from translation
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"https?://[^\s<>()\]]+")
_CODE_WORD_RE = re.compile(r"\b(?:redeem|promo|coupon|gift ?codes?|codes?)\b", re.I)
# Mixed letters AND digits in capitals - "WINTER2026", "3MILLION" - is a code,
# or at worst an id; either way it must not be translated. Anywhere in the text.
_MIXED_RE = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,}\b")
# Inside a sentence that MENTIONS a code: any all-caps token (an all-letters
# code like "ORNAVERSARY"), and any letters+digits token in any case.
_CAPS_RE = re.compile(r"\b[A-Z][A-Z0-9_-]{3,}\b")
_ALNUM_RE = re.compile(r"(?<!<)\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{4,}\b")
_PLACEHOLDER_RE = re.compile(r"<\s*k\s*(\d+)\s*/?\s*>", re.I)


def protect(text: str) -> tuple:
    """-> (text with placeholders, [(kind, original)]). kind is "code" for a
    redeem code (sent back as <code>), "span" for an existing `inline code` or
    fenced block, "url" for a link.

    Over-protecting costs one English word left untranslated; under-protecting
    can cost a broken code. So it leans toward protecting - but NOT every
    capitalised word: a "NEW EVENT" heading outside a code sentence is still
    translated."""
    tokens: list = []

    def hold(kind: str, value: str) -> str:
        tokens.append((kind, value))
        return f"<k{len(tokens) - 1}/>"

    text = _FENCE_RE.sub(lambda m: hold("span", m.group(0)), text)
    text = _INLINE_CODE_RE.sub(lambda m: hold("span", m.group(0)), text)
    text = _URL_RE.sub(lambda m: hold("url", m.group(0)), text)
    text = _MIXED_RE.sub(lambda m: hold("code", m.group(0)), text)

    out_lines, in_list = [], False
    for line in text.split("\n"):
        # A "Codes:" line opens a list: each following single-token line is a
        # code, until a blank line ends the list.
        if in_list and line.strip() and len(line.split()) == 1 and "<k" not in line:
            line = line.replace(line.strip(), hold("code", line.strip()))
            out_lines.append(line)
            continue
        in_list = bool(_CODE_WORD_RE.search(line) and line.rstrip().endswith(":")) or \
            (in_list and bool(line.strip()))
        pieces = re.split(r"(?<=[.!?])\s+", line)
        for i, sent in enumerate(pieces):
            if _CODE_WORD_RE.search(sent):
                sent = _CAPS_RE.sub(lambda m: hold("code", m.group(0)), sent)
                sent = _ALNUM_RE.sub(lambda m: hold("code", m.group(0)), sent)
            pieces[i] = sent
        out_lines.append(" ".join(pieces))
    return "\n".join(out_lines), tokens


def restore(text: str, tokens: list) -> str:
    """Put every protected value back exactly. A code comes back wrapped in
    backticks - Markdown inline code -> <code> in Telegram, tap-to-copy. Any
    value whose placeholder the translator dropped is appended rather than
    lost: a code missing from an announcement is the one unacceptable outcome."""
    used = set()

    def put(m: re.Match) -> str:
        i = int(m.group(1))
        if i >= len(tokens):
            return ""
        used.add(i)
        kind, value = tokens[i]
        return f"`{value}`" if kind == "code" else value

    out = _PLACEHOLDER_RE.sub(put, text)
    lost = [tokens[i] for i in range(len(tokens)) if i not in used]
    codes = [v for k, v in lost if k in ("code", "span")]
    urls = [v for k, v in lost if k == "url"]
    if codes:
        out += "\n\n🎁 " + ", ".join(f"`{v.strip('`')}`" for v in codes)
    if urls:
        out += "\n" + "\n".join(urls)
    return out


# Names the game data does not carry but announcements use constantly.
_EXTRA_NAMES = {"orna", "northern forge", "hero of aethric", "odie", "orna rpg",
                "ithra", "thor", "vulcan", "demeter"}  # the four Monuments - not codex records
_vocab: Optional[set] = None
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def _names_vocab() -> set:
    global _vocab
    if _vocab is None:
        _vocab = set(_EXTRA_NAMES)
        try:
            from orna import orna_codex_db
            con = orna_codex_db.connect()
            for (n,) in con.execute("SELECT name FROM records WHERE name IS NOT NULL UNION "
                                    "SELECT name FROM terms WHERE kind IN ('events', 'tags') AND name IS NOT NULL"):
                if len(n.split()) <= 4:
                    _vocab.add(n.lower())
        except Exception:
            logger.warning("announce: codex names unavailable - only the fixed list is pinned", exc_info=True)
        # Classes and specializations are NOT codex records - and a translated
        # one ("Duelist" -> "Дулїст") is the failure pinning was built for.
        try:
            from orna import orna_classes
            _vocab.update(n.lower() for kind in ("class", "specialization") for n in orna_classes.all_names(kind))
        except Exception:
            logger.warning("announce: class names unavailable", exc_info=True)
    return _vocab


def game_names(text: str) -> list:
    """The game names in `text` that must stay in English: an event, monster,
    item or class name that EXISTS in the game data (longest match first, a
    plural "keys" also matching "Key"), and only where it is capitalised - so a
    codex material called "Stone" does not freeze the word in "set in stone".

    This replaces pinning EVERY capitalised word, which left whole sentences in
    English ("Thank всім за гру") - see telegram_orna._translate's `pin`."""
    vocab, words = _names_vocab(), [(m.group(0), m.start()) for m in _WORD_RE.finditer(text)]
    found, i = [], 0
    while i < len(words):
        for n in (4, 3, 2, 1):
            if i + n > len(words) or not words[i][0][:1].isupper():
                continue
            span = text[words[i][1]:words[i + n - 1][1] + len(words[i + n - 1][0])]
            key = " ".join(w for w, _ in words[i:i + n]).lower()
            if key in vocab or (key.endswith("s") and key[:-1] in vocab):
                found.append(span)
                i += n
                break
        else:
            i += 1
    return list(dict.fromkeys(found))


def codes_in(tokens: list) -> list:
    return [v.strip("`").strip() for k, v in tokens if k in ("code", "span")]


async def translate_announcement(text: str,
                                 translator: Optional[Callable[[str], Awaitable[str]]] = None) -> str:
    """English announcement -> Ukrainian, codes untouched. `translator` is
    injectable for the self-check; in production it is /orna's own _translate
    (proper nouns pinned and verified, falls back to the input on failure - so
    a translation outage still relays the English, codes intact)."""
    if translator is None:
        from bot.telegram_orna import TRANSLATION_MODEL, _translate

        async def translator(t: str) -> str:
            return await _translate(t, "Ukrainian", "English", pin=game_names(t), model=TRANSLATION_MODEL)

    masked, tokens = protect(text)
    out = restore(await translator(masked), tokens)
    # The guarantee, checked rather than trusted: every code is present
    # character for character. restore() already appends a lost one, so this
    # only fires on a bug - and then the English original (codes intact) is
    # sent instead of a translation that lost one.
    missing = [c for c in codes_in(tokens) if c not in out]
    if missing:
        logger.error("announce: codes %s missing after restore - sending the original", missing)
        return text
    return out


# ---------------------------------------------------------------------------
# Formatting and sending
# ---------------------------------------------------------------------------

def _chunks(markdown: str) -> list:
    """Split on blank lines into pieces of at most CHUNK_CHARS, each converted
    to HTML on its own - so no tag ever straddles two Telegram messages."""
    out, cur = [], ""
    for para in markdown.split("\n\n"):
        while len(para) > CHUNK_CHARS:            # one giant paragraph: hard-split on a line
            cut = para.rfind("\n", 0, CHUNK_CHARS)
            cut = cut if cut > 0 else CHUNK_CHARS
            out.append(para[:cut])
            para = para[cut:].lstrip("\n")
        if cur and len(cur) + len(para) + 2 > CHUNK_CHARS:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def _blockquotes(html_text: str) -> str:
    """Discord "> quote" lines -> a real Telegram <blockquote>. The shared
    _markdown_to_html has no quote support, so a quoted "how to redeem" box
    arrived as literal "> " lines (seen in the first admin test). Done on the
    converted HTML: a line starting "&gt; " can only have come from a literal
    "> " in the source, since the converter escapes every ">"."""
    out, quote = [], []
    for line in html_text.split("\n"):
        if line.startswith("&gt; ") or line == "&gt;":
            quote.append(line[5:])
            continue
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote = []
        out.append(line)
    if quote:
        out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
    return "\n".join(out)


def render(source: str, translated: str, link: str) -> list:
    """-> HTML messages. Header names where it came from; the footer links the
    ORIGINAL message in the official server."""
    parts = [_blockquotes(_markdown_to_html(c)) for c in _chunks(translated)] or [""]
    parts[0] = f"📢 <b>{html.escape(source)}</b>\n\n{parts[0]}"
    if link:
        parts[-1] += f'\n\n<a href="{html.escape(link, quote=True)}">Оригінал у Discord</a>'
    return parts


async def _send_one(bot, chat_id: int, parts: list, images: list) -> Optional[int]:
    """Send to one chat. Returns the chat id that ended up being used (a
    migrated group gets a new one), or None if the chat should be dropped."""
    for part in parts:
        try:
            await bot.send_message(chat_id, part, parse_mode="HTML", disable_web_page_preview=True)
        except ChatMigrated as e:
            return await _send_one(bot, e.new_chat_id, parts, images)
        except Forbidden:
            return None
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                return None
            # Malformed HTML after translation: send the text rather than nothing.
            plain = re.sub(r"<[^>]+>", "", part)
            await bot.send_message(chat_id, html.unescape(plain), disable_web_page_preview=True)
    for url in images[:MAX_IMAGES]:
        try:
            await bot.send_photo(chat_id, url)
        except TelegramError:
            logger.warning("announce: image not sent to %s: %s", chat_id, url[:80])
    return chat_id


async def broadcast(bot, parts: list, images: list) -> int:
    sent = 0
    for chat_id in enabled_chats():
        try:
            used = await _send_one(bot, chat_id, parts, images)
        except TelegramError:
            logger.warning("announce: send to %s failed", chat_id, exc_info=True)
            continue
        if used is None:
            unregister(chat_id)
        else:
            if used != chat_id:            # migrated group: keep its settings, new id
                entry = _load()["chats"].pop(str(chat_id), {})
                _load()["chats"][str(used)] = entry
                _save()
            sent += 1
        await asyncio.sleep(0.05)          # well under Telegram's ~30 msg/s
    return sent


# ---------------------------------------------------------------------------
# The polling job
# ---------------------------------------------------------------------------

_lock = asyncio.Lock()


def configured() -> bool:
    return bool(os.environ.get(orna_discord.TOKEN_ENV) and os.environ.get(orna_discord.CHANNEL_ENV))


async def poll_once(bot, client=None, translator=None) -> dict:
    """One poll of the Discord channel. Advances the cursor PER MESSAGE after
    it is broadcast, so a crash mid-batch resends at most one announcement
    instead of the whole batch. Never advances past messages it could not read
    (IntentMissing), so they are relayed once the intent is fixed."""
    async with _lock:
        state = _load()
        channel = os.environ.get(orna_discord.CHANNEL_ENV, "")
        own = client is None
        client = client or orna_discord.Client(os.environ.get(orna_discord.TOKEN_ENV, ""))
        try:
            if not state["cursor"]:
                state["cursor"] = await asyncio.to_thread(orna_discord.latest_id, client, channel) or "0"
                _save()
                logger.info("announce: first run - starting after message %s", state["cursor"])
                return {"relayed": 0, "started": True}
            followed, newest = await asyncio.to_thread(orna_discord.fetch_new, client, channel, state["cursor"])
            relayed = 0
            for msg in followed:
                ann = orna_discord.as_announcement(msg)
                if ann["text"] or ann["images"]:
                    text = await translate_announcement(ann["text"], translator) if ann["text"] else ""
                    await broadcast(bot, render(ann["source"], text, ann["link"]), ann["images"])
                    relayed += 1
                state["cursor"] = ann["id"]
                _save()
            state["cursor"] = newest
            _save()
            return {"relayed": relayed}
        finally:
            if own:
                client.close()


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue entry point. Every failure is logged and swallowed - a Discord
    outage or a revoked token must never affect the rest of the bot."""
    try:
        result = await poll_once(context.bot)
        if result.get("relayed"):
            logger.info("announce: relayed %s announcement(s)", result["relayed"])
    except orna_discord.IntentMissing as e:
        logger.error("announce: %s", e)
    except Exception:
        logger.warning("announce: poll failed", exc_info=True)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if change is None:
        return
    status = change.new_chat_member.status
    if status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        register(change.chat)
    elif status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        unregister(change.chat.id)


async def _on_any(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registers a group/channel the moment the bot sees anything there -
    which is how chats it joined before this feature existed get picked up."""
    register(update.effective_chat)


async def handle_announcements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/announcements [on|off]. In a group: status, or a toggle by a chat admin.
    In a private chat, for the bot's owners: which chats will receive them."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if msg is None or chat is None:
        return
    arg = (context.args[0].lower() if context.args else "")

    if chat.type == ChatType.PRIVATE:
        if not user or (GO_ALLOWED_USER_IDS and user.id not in GO_ALLOWED_USER_IDS):
            return
        if arg in ("add", "remove") and len(context.args) > 1:
            await msg.reply_text(await _owner_add_remove(context.bot, arg, context.args[1]))
            return
        chats = _load()["chats"]
        lines = [f"Анонси Discord: {'налаштовано' if configured() else 'НЕ налаштовано'}, "
                 f"курсор {_load()['cursor'] or '—'}", f"Чатів: {len(chats)}"]
        lines += [f"{'✅' if v.get('enabled', True) else '⏸'} {v.get('title') or k} ({k})" for k, v in chats.items()]
        lines.append("\nДодати чат: /announcements add @handle (або id)\nПрибрати: /announcements remove @handle")
        await msg.reply_text("\n".join(lines))
        return

    register(chat)
    if arg in ("on", "off"):
        allowed = bool(user and GO_ALLOWED_USER_IDS and user.id in GO_ALLOWED_USER_IDS)
        if not allowed and user:
            try:
                member = await context.bot.get_chat_member(chat.id, user.id)
                allowed = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
            except TelegramError:
                allowed = False
        if not allowed:
            await msg.reply_text("Лише адміністратори чату можуть змінювати це.")
            return
        set_enabled(chat.id, arg == "on")
    on = _load()["chats"].get(str(chat.id), {}).get("enabled", True)
    await msg.reply_text("Анонси гри з Discord у цьому чаті: " + ("увімкнено ✅" if on else "вимкнено ⏸")
                         + "\n/announcements on | off - для адміністраторів")


async def _owner_add_remove(bot, action: str, target: str) -> str:
    """Register or drop a chat by @handle or id, verified against Telegram.

    The Bot API cannot list a bot's chats, so an EXISTING group was only picked
    up once somebody wrote in it after deploy - live 2026-10-05, "Orna RPG UA"
    (@ornarpgua) was missing from the list because nobody had. A channel was
    worse: it only appears on a new post. This asks Telegram directly, and
    refuses a chat the bot is not actually in, or a channel it cannot post to,
    rather than registering something that would fail on every announcement."""
    ref = target if target.startswith("@") or target.lstrip("-").isdigit() else "@" + target
    try:
        chat = await bot.get_chat(int(ref) if ref.lstrip("-").isdigit() else ref)
    except TelegramError as e:
        return f"Не знайшов чат {target}: {e}"
    if action == "remove":
        unregister(chat.id)
        return f"Прибрано: {chat.title} ({chat.id})"
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramError as e:
        return f"Не вдалося перевірити, чи бот у {chat.title}: {e}"
    if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return f"Бота немає в {chat.title} (статус: {member.status}) - спершу додайте його туди."
    if chat.type == ChatType.CHANNEL and not getattr(member, "can_post_messages", False):
        return f"{chat.title} - канал, і бот там не може публікувати: дайте йому право «Публікація повідомлень»."
    register(chat)
    return f"✅ Додано: {chat.title} ({chat.id}) - анонси надходитимуть сюди."


def build_handlers() -> list:
    """(handler, group) pairs. The recorder sits in its OWN group so it runs
    alongside every other handler and can never take an update away from the
    order-sensitive assess/resources conversations (see CLAUDE.md)."""
    return [
        (ChatMemberHandler(_on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), 0),
        (CommandHandler("announcements", handle_announcements), 0),
        (MessageHandler((filters.ChatType.GROUPS | filters.ChatType.CHANNEL), _on_any), 97),
    ]


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def _demo() -> None:
    import tempfile

    # --- protect / restore ------------------------------------------------
    def roundtrip(src, translate=lambda s: s.upper()):
        masked, toks = protect(src)
        return masked, toks, restore(translate(masked), toks)

    # a code next to the word "code" - all-letters, the hardest kind
    masked, toks, out = roundtrip("Thanks for 8 years! Use code ORNAVERSARY for a gift.")
    assert "ORNAVERSARY" not in masked and codes_in(toks) == ["ORNAVERSARY"], (masked, toks)
    assert "`ORNAVERSARY`" in out, out
    # letters+digits anywhere, even with no "code" word nearby
    assert codes_in(protect("Event bonus: WINTER2026 is live")[1]) == ["WINTER2026"]
    # lowercase code with digits after the keyword
    assert codes_in(protect("Redeem: winter2026 in options")[1]) == ["winter2026"]
    # a "Codes:" list, one per line
    m, t = protect("Codes:\nSNOWDAY\nHOLLY\n\nEnjoy the event!")
    assert codes_in(t) == ["SNOWDAY", "HOLLY"] and "Enjoy the event!" in m, (m, t)
    # inline code and URLs are protected too
    m, t = protect("Type `ABC` at https://playorna.com/redeem now")
    assert [k for k, _ in t] == ["span", "url"] and "playorna" not in m, (m, t)
    # NOT over-protected: a capitalised heading outside a code sentence translates
    m, t = protect("NEW EVENT: Ragnarok returns this weekend!")
    assert t == [] and m == "NEW EVENT: Ragnarok returns this weekend!", (m, t)

    # a translator that mangles spacing of the placeholder still restores
    _, toks, out = roundtrip("Use code ORNA2026 now", lambda s: s.replace("<k0/>", "< k0 />"))
    assert "`ORNA2026`" in out, out
    # a translator that DROPS the placeholder: the code is appended, never lost
    _, toks, out = roundtrip("Use code ORNA2026 now", lambda s: "Використайте код зараз")
    assert "Використайте код зараз" in out and "`ORNA2026`" in out, out

    # --- the whole translate path, with a hostile translator --------------
    async def hostile(t):     # translates everything it can see, codes included
        return t.replace("Use", "Використайте").replace("code", "код").replace("SNOW", "СНІГ")

    got = asyncio.run(translate_announcement("Use code SNOWDAY today", hostile))
    assert "`SNOWDAY`" in got and "СНІГ" not in got, got

    # --- which words stay English: real game names only ---------------------
    global _vocab
    saved_vocab = _vocab
    _vocab = {"wyrmhunt", "fenrir", "arisen", "skeleton key", "stone", "orna", "northern forge"}
    got = game_names("Thank you! Wyrmhunt is back. Defeat Fenrir for Arisen gear and 50 Skeleton keys. "
                     "Happy holidays from Northern Forge, set in stone.")
    assert got == ["Wyrmhunt", "Fenrir", "Arisen", "Skeleton keys", "Northern Forge"], got
    _vocab = saved_vocab

    # --- rendering ---------------------------------------------------------
    parts = render("Orna RPG #announcements", "**Нова подія**\n\nКод: `ORNA2026`", "https://discord.com/x")
    assert parts[0].startswith("📢 <b>Orna RPG #announcements</b>") and "<code>ORNA2026</code>" in parts[0]
    assert parts[-1].endswith('<a href="https://discord.com/x">Оригінал у Discord</a>')
    q = render("S", "Before\n> line one\n> a > b\nAfter", "")[0]
    assert "<blockquote>line one\na &gt; b</blockquote>" in q and "Before" in q and "After" in q, q
    long = "\n\n".join(f"para {i} " + "x" * 300 for i in range(40))
    assert all(len(c) <= CHUNK_CHARS for c in _chunks(long)) and len(_chunks(long)) > 1

    # --- registry + the job, against fake Discord and fake Telegram -------
    global STATE_PATH, _state
    saved_path, saved_state = STATE_PATH, _state
    import httpx
    with tempfile.TemporaryDirectory() as tmp:
        STATE_PATH, _state = Path(tmp) / "s.json", None

        class Chat:
            def __init__(self, cid, ctype, title="g"):
                self.id, self.type, self.title = cid, ctype, title

        register(Chat(1, ChatType.PRIVATE))
        assert enabled_chats() == [], "private chats never get announcements"
        for cid in (-100, -200, -300, -400):
            register(Chat(cid, ChatType.SUPERGROUP))
        set_enabled(-400, False)

        class FakeBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, chat_id, text, **kw):
                if chat_id == -200:
                    raise Forbidden("bot was kicked")
                if chat_id == -300:
                    raise ChatMigrated(-301)
                self.sent.append((chat_id, text))

            async def send_photo(self, chat_id, url, **kw):
                self.sent.append((chat_id, "PHOTO " + url))

        base = 1_300_000_000_000_000_000
        msgs = [{"id": str(base + 1), "type": 0, "flags": 0, "content": "old chat", "author": {}},
                {"id": str(base + 2), "type": 0, "flags": 2, "author": {"username": "Orna RPG #news"},
                 "content": "Event starts <t:1767225600:F>! Use code ORNA2026.",
                 "attachments": [{"url": "https://cdn/b.png", "content_type": "image/png"}], "embeds": [],
                 "message_reference": {"guild_id": "1", "channel_id": "2", "message_id": "3"}},
                {"id": str(base + 3), "type": 0, "flags": 0, "content": "someone chatting", "author": {}}]
        visible = {"n": 1}

        def handler(request):
            q = dict(request.url.params)
            after = int(q.get("after", 0))
            pool = msgs[:visible["n"]]
            if "after" not in q:
                return httpx.Response(200, json=list(reversed(pool))[:int(q.get("limit", 100))])
            return httpx.Response(200, json=list(reversed([m for m in pool if int(m["id"]) > after])))

        client = orna_discord.Client("T", transport=httpx.MockTransport(handler), sleep=lambda s: None)
        os.environ[orna_discord.CHANNEL_ENV] = "2"
        bot = FakeBot()

        async def fake_translate(t):
            return t.replace("Event starts", "Подія починається").replace("Use code", "Використайте код")

        # first run: cursor set to the newest message, NOTHING sent
        r = asyncio.run(poll_once(bot, client, fake_translate))
        assert r.get("started") and bot.sent == [] and _load()["cursor"] == str(base + 1), (r, bot.sent)
        # a followed announcement and a chat message arrive
        visible["n"] = 3
        r = asyncio.run(poll_once(bot, client, fake_translate))
        assert r["relayed"] == 1, r
        texts = {cid: t for cid, t in bot.sent if not t.startswith("PHOTO")}
        assert set(texts) == {-100, -301}, f"enabled + migrated only, got {set(texts)}"
        body = texts[-100]
        assert "Подія починається 2026-01-01 00:00 UTC" in body, body
        assert "<code>ORNA2026</code>" in body, body
        assert "someone chatting" not in body and "old chat" not in body
        assert (-100, "PHOTO https://cdn/b.png") in bot.sent, "the announcement's image is sent too"
        assert -200 not in enabled_chats(), "a chat that kicked the bot is dropped"
        assert -301 in enabled_chats() and -300 not in enabled_chats(), "a migrated group follows its new id"
        assert -400 not in [c for c, _ in bot.sent], "a chat turned off gets nothing"
        assert _load()["cursor"] == str(base + 3), "cursor moved past the ordinary chat message too"
        # nothing new -> nothing sent
        n = len(bot.sent)
        asyncio.run(poll_once(bot, client, fake_translate))
        assert len(bot.sent) == n
        client.close()
    STATE_PATH, _state = saved_path, saved_state
    os.environ.pop(orna_discord.CHANNEL_ENV, None)
    print("telegram_announce: all checks passed")


if __name__ == "__main__":
    _demo()
