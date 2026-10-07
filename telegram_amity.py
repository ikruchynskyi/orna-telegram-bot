"""
telegram_amity.py
=================
Memory-hunt coordination: players post the "MEMORY COMPLETED" / "Спомин
завершено" screenshot of an amity a witch gave them, captioned with the witch
colour and option number ("red 4", "фіолетовий 3", "4yellow"); `/amity` lists
this week's finds so others can ask the sharer to invite them to their party.

Game rules this leans on (from the guild, not derivable from any source here):
* the amity is seeded by the PARTY LEADER, so a party member gets the same one;
* a witch's options rotate hourly and repeat at the same UTC hour every day;
* everything reshuffles at Monday 00:00 UTC - so a find is keyed by
  (ISO week, UTC hour, colour, option) and dropped when the week rolls over.

Entry point is `handle_amity_screen`, called from telegram_assess's photo flow
once OCR says the screen is an amity - no second photo handler, so a busy group
chat's other images never reach this module.
"""
from __future__ import annotations

import paths
import asyncio
import datetime
import difflib
import html
import json
import logging
import re
import time
from typing import Optional

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)

_STORE_PATH = paths.STATE / "amities.json"
_NICKS_PATH = paths.STATE / "nicknames.json"   # telegram user id -> in-game nickname (/iam)
PENDING_TTL_SECONDS = 600

COLORS = {  # colour -> (emoji, stems in EN / UK / RU, lowercase)
    "Red": ("🔴", ("red", "черв", "красн")),
    "Yellow": ("🟡", ("yellow", "жовт", "желт")),
    "Green": ("🟢", ("green", "зел")),
    "Blue": ("🔵", ("blue", "син", "блак", "голуб")),
    "Purple": ("🟣", ("purple", "violet", "pink", "фіол", "фиол", "пурп", "рож", "роз")),
}
_HEADERS = ("MEMORY COMPLETED", "СПОМИН ЗАВЕРШЕНО")
# An INVENTORY/equipped amity ("Inventory"/"Інвентар", "Одягнений") has no
# header or "When equipped..." line; its effects follow the fixed description
# "...through both bonuses and maluses." and end at TIER/РАНГ.
_EQUIP_RE = re.compile(r"when equip|споряджен|maluses\.", re.IGNORECASE)
_AMITY_DESC_RE = re.compile(r"spectral essence of a place|bonuses and maluses", re.IGNORECASE)
_END_RE = re.compile(r"^\W*(tier|rank|ранг|acquired|отримано|useable by|доступно)\b", re.IGNORECASE)
_REWARD_RE = re.compile(r"\d{1,3}(?:[,.\s]\d{3})+")   # "109,324,554 gold" ends the effects
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")

_HOUR_HINT = ("Це amity з інвентарю — на ньому немає години, тож додайте годину UTC, коли його знайшли: "
              "<b>Red 4 14</b> (колір, варіант, година 0-23).\nInventory amity has no hour - add the UTC hour "
              "it was found: <b>Red 4 14</b>.")
_CHOICE_HINT = ("Кольори / colors: 🔴 Red (червона), 🟡 Yellow (жовта), 🟢 Green (зелена), "
                "🔵 Blue (синя), 🟣 Purple (фіолетова). Номер варіанту / option: 1-5.")

# (chat_id, user_id) -> (draft entry, expires_monotonic): a screenshot waiting
# for its colour/option. One typed answer is read, valid or not.
_PENDING: dict = {}


# --------------------------------------------------------------------------- parsing

def parse_choice(text: str) -> Optional[tuple]:
    """("Red", 4) from free text in any of the guild's languages, or None if it
    names no colour, several colours, or not exactly one option number 1-5."""
    low = (text or "").lower()
    found = {c for c, (_e, stems) in COLORS.items() if any(s in low for s in stems)}
    nums = re.findall(r"\d+", low)
    if len(found) != 1 or len(nums) != 1 or not 1 <= int(nums[0]) <= 5:
        return None
    return found.pop(), int(nums[0])


_HOUR_MARK_RE = re.compile(r"(\d{1,2})\s*(?::\d{2}|utc|h\b|год)", re.IGNORECASE)
_ACQUIRED_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(20\d{2})")


def parse_choice_hour(text: str) -> Optional[tuple]:
    """("Red", 4, 14) for an INVENTORY amity, whose screen carries no hour:
    colour + option + UTC hour, e.g. "red 4 14", "червона 4 14:00", "14 utc
    red 4". A number marked as a time (":00", "utc", "h", "год") is the hour;
    otherwise the first number is the option and the second the hour."""
    low = (text or "").lower()
    found = {c for c, (_e, stems) in COLORS.items() if any(s in low for s in stems)}
    marked = _HOUR_MARK_RE.search(low)
    rest = low[:marked.start()] + " " + low[marked.end():] if marked else low
    nums = [int(n) for n in re.findall(r"\d+", rest)]
    if marked:
        nums.append(int(marked.group(1)))
    if len(found) != 1 or len(nums) != 2 or not 1 <= nums[0] <= 5 or not 0 <= nums[1] <= 23:
        return None
    return found.pop(), nums[0], nums[1]


def is_memory_screen(ocr_text: str) -> bool:
    """The memory-hunt RESULT screen (its hour is the message time); anything
    else looks_like_amity_screen accepts is an inventory/equipped amity."""
    return any(difflib.SequenceMatcher(None, line.strip().upper(), h).ratio() > 0.8
               for line in ocr_text.splitlines() for h in _HEADERS)


def acquired_date(ocr_text: str) -> Optional[datetime.date]:
    m = _ACQUIRED_RE.search(ocr_text)
    try:
        return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
    except ValueError:
        return None


def looks_like_amity_screen(ocr_text: str) -> bool:
    # Fuzzy, because the header is small caps and OCR mangles it now and then.
    return bool(_AMITY_DESC_RE.search(ocr_text)) or is_memory_screen(ocr_text)


def split_effects(ocr_text: str) -> Optional[tuple]:
    """(bonuses, maluses) as raw lines, or None if unreadable. Effects sit
    between "When equipped..." and the first reward amount; a wrapped line
    starts lowercase or carries no letters at all ("40%"); a line that STARTS
    with a number but has words ("5.0% of the damage your Ward takes...") is a
    new effect. Bonuses are listed
    first and there are always as many maluses as bonuses - an odd count means
    the split went wrong, so it is refused rather than guessed."""
    lines = ocr_text.splitlines()
    start = max((i for i, l in enumerate(lines) if _EQUIP_RE.search(l)), default=None)
    if start is None:
        return None
    effects = []
    for line in lines[start + 1:]:
        line = line.strip()
        if not line:
            continue
        if _REWARD_RE.search(line) or _END_RE.match(line):
            break
        first = next((ch for ch in line if ch.isalpha()), "")
        if not first and not effects:
            continue   # icon junk before the first effect
        if effects and (not first or line[0].islower()):
            effects[-1] += " " + line
        else:
            effects.append(line)
    n = len(effects)
    if n == 0 or n % 2 or n > 6:
        return None
    return effects[:n // 2], effects[n // 2:]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[\d.,%#+\-–]", " ", text.lower()).split())


def describe(raw: str, english: str, kind: str, cards: list) -> tuple:
    """(display text, dedupe key) for one effect. Matched against the
    aussiescodex template of the same side (bonus/malus - "Increases arcane
    damage" and "Decreases arcane damage" are otherwise one letter apart), the
    rolled value is put back into the template and the roll range appended."""
    target = _norm(english)
    best, score = None, 0.0
    for card in cards:
        if card["kind"] == kind:
            r = difflib.SequenceMatcher(None, target, _norm(card["desc"])).ratio()
            if r > score:
                best, score = card, r
    num = _NUM_RE.search(english) or _NUM_RE.search(raw)
    value = f"{float(num.group().replace(',', '.')):g}" if num else ""
    if best is None or score < 0.75:
        return english, _norm(raw) + value   # translated if it was Ukrainian
    text = best["desc"]
    if value:
        # aussies writes some templates as "increased by -%"; the sign is theirs, not the roll's
        text = re.sub(r"-?[%#]", lambda m: value + ("%" if m.group().endswith("%") else ""), text, count=1)
    if best["range"]:
        text += f" ({best['range']})"
    return text, f"{best['name']}|{best['desc']}|{value}"


async def _describe_all(bonuses: list, maluses: list) -> tuple:
    """-> (bonus texts, malus texts, dedupe key parts)."""
    raws = bonuses + maluses
    english = raws
    try:
        import orna_bonuses
        cards = (await asyncio.to_thread(orna_bonuses.all_bonuses)).get("amity_cards") or []
    except Exception as e:  # catalog down: still store the raw OCR text
        logger.warning("amity: catalog unavailable (%s)", e)
        cards = []
    if cards and any(re.search("[\u0400-\u04FF]", r) for r in raws):
        from telegram_orna import _translate  # lazy: telegram_orna imports telegram_assess
        got = (await _translate("\n".join(raws), "English", source="Ukrainian")).splitlines()
        got = [g.strip() for g in got if g.strip()]
        if len(got) == len(raws):
            english = got
    out = [describe(r, e, "bonus" if i < len(bonuses) else "malus", cards)
           for i, (r, e) in enumerate(zip(raws, english))]
    texts = [t for t, _k in out]
    return texts[:len(bonuses)], texts[len(bonuses):], [k for _t, k in out]


# --------------------------------------------------------------------------- storage

def _week(ts: datetime.datetime) -> str:
    y, w, _d = ts.isocalendar()   # ISO weeks start Monday - the game's reset
    return f"{y}-W{w:02d}"


def _load() -> list:
    try:
        entries = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    this_week = _week(datetime.datetime.now(datetime.timezone.utc))
    return [e for e in entries if e.get("week") == this_week]


def _save(entries: list) -> None:
    tmp = _STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_STORE_PATH)


def parse_hours(text: str) -> Optional[set]:
    """{8, 14} from "8, 14" / "08:00,14"; None if any number isn't an hour."""
    nums = [int(n) for n in re.findall(r"\d+", re.sub(r":00\b", "", text or ""))]
    return set(nums) if nums and all(0 <= n <= 23 for n in nums) else None


def delete_entries(user_id: int, hours: set) -> int:
    """Drop this week's finds the user shared at those UTC hours; -> count."""
    entries = _load()
    kept = [e for e in entries if not (e["user_id"] == user_id and e["hour"] in hours)]
    _save(kept)
    return len(entries) - len(kept)


def add_entry(entry: dict) -> bool:
    """False if the same amity is already shared for that slot this week."""
    entries = _load()
    if any(e["key"] == entry["key"] for e in entries):
        return False
    entries.append(entry)
    _save(entries)
    return True


# --------------------------------------------------------------------------- telegram

def _load_nicks() -> dict:
    try:
        return json.loads(_NICKS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_nick(user_id: int, nick: str) -> None:
    nicks = _load_nicks()
    nicks[str(user_id)] = nick
    tmp = _NICKS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(nicks, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_NICKS_PATH)


def _who(entry: dict, nick: str = "") -> str:
    # A t.me link, not a mention entity: /amity must not ping every sharer.
    if entry.get("username"):
        who = f'<a href="https://t.me/{entry["username"]}">@{html.escape(entry["username"])}</a>'
    else:
        who = html.escape(entry.get("name") or "?")
    return who + (f" (🎮 <code>{html.escape(nick)}</code>)" if nick else "")


def _format(entry: dict, now_hour: Optional[int] = None, nick: str = "") -> str:
    emoji = COLORS[entry["color"]][0]
    head = (f"<b>{entry['hour']:02d}:00 UTC</b> {emoji} <b>{entry['color']} {entry['option']}</b>"
            f" — {_who(entry, nick)}" + (" ⏰ <b>зараз</b>" if entry["hour"] == now_hour else ""))
    lines = [head] + [f"➕ {html.escape(b)}" for b in entry["bonuses"]] \
                   + [f"➖ {html.escape(m)}" for m in entry["maluses"]]
    return "\n".join(lines)


async def _finish(message, draft: dict, color: str, option: int, hour: Optional[int] = None) -> None:
    draft = dict(draft, color=color, option=option)
    if hour is not None:
        draft["hour"] = hour
    draft["key"] = "|".join([draft["week"], str(draft["hour"]), color, str(option)] + sorted(draft.pop("parts")))
    if not await asyncio.to_thread(add_entry, draft):
        await message.reply_text("Цей amity для цієї відьми й години вже є у списку цього тижня — див. /amity")
        return
    await message.reply_text("✅ Збережено, дякую! Список цього тижня: /amity\n\n" + _format(draft),
                             parse_mode="HTML", disable_web_page_preview=True)


async def handle_amity_screen(msg, ocr_text: str) -> None:
    split = split_effects(ocr_text)
    if split is None:
        await msg.reply_text("Бачу «Memory completed», але не зміг прочитати бонуси/малуси amity.")
        return
    bonuses, maluses, parts = await _describe_all(*split)
    ts = msg.date or datetime.datetime.now(datetime.timezone.utc)   # aware either way
    ts = ts.astimezone(datetime.timezone.utc)
    user = msg.from_user
    inventory = not is_memory_screen(ocr_text)
    if inventory:
        # Its week is when it was FOUND; a find from before this Monday has
        # already been reset in game and would list a slot that no longer
        # gives it. (The date is the player's local one - allow a day of slack.)
        found = acquired_date(ocr_text)
        monday = (ts - datetime.timedelta(days=ts.weekday())).date()
        if found and found < monday - datetime.timedelta(days=1):
            await msg.reply_text(f"Цей amity отримано {found:%d.%m} — до тижневого скидання (пн 00:00 UTC), "
                                 "тож цей варіант відьми вже дає інше. Не зберігаю.")
            return
    draft = {"week": _week(ts), "hour": None if inventory else ts.hour, "bonuses": bonuses, "maluses": maluses, "parts": parts,
             "user_id": user.id, "username": user.username, "name": user.full_name,
             "chat_id": msg.chat_id, "ts": ts.isoformat()}
    choice = (parse_choice_hour if inventory else parse_choice)(msg.caption or "")
    if choice:
        await _finish(msg, draft, *choice)
        return
    _PENDING[(msg.chat_id, user.id)] = (draft, time.monotonic() + PENDING_TTL_SECONDS)
    if inventory:
        await msg.reply_text(f"{user.mention_html()}, яка відьма, варіант і година UTC?\n\n{_HOUR_HINT}\n\n"
                             f"{_CHOICE_HINT}", parse_mode="HTML")
        return
    await msg.reply_text(
        f"{user.mention_html()}, яка відьма і який варіант? Напишіть колір і номер, наприклад "
        f"<b>Red 4</b> або <b>Червона 4</b>.\nWhich witch and option? E.g. <b>Red 4</b>.\n\n{_CHOICE_HINT}",
        parse_mode="HTML")


def cancel_pending(chat_id: int, user_id: int) -> None:
    _PENDING.pop((chat_id, user_id), None)


class _PendingFilter(filters.MessageFilter):
    """Only the uploader's next message in that chat, while an ask is live."""

    def filter(self, message) -> bool:
        pending = _PENDING.get((message.chat_id, message.from_user.id if message.from_user else 0))
        return bool(pending and time.monotonic() < pending[1])


async def handle_choice_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    pending = _PENDING.pop((msg.chat_id, msg.from_user.id), None)
    if pending is None:
        return
    if pending[0].get("delete"):
        await _delete(msg, msg.text)
        return
    inventory = pending[0].get("hour") is None
    choice = (parse_choice_hour if inventory else parse_choice)(msg.text)
    if choice is None:
        example = "Red 4 14" if inventory else "Red 4"
        await msg.reply_text(f"Не розпізнав {'колір, номер і годину' if inventory else 'колір і номер'} — amity "
                             f"не збережено. Надішліть скріншот ще раз з підписом, наприклад «{example}».\n\n"
                             + _CHOICE_HINT + (" Година / hour: 0-23 UTC." if inventory else ""))
        return
    await _finish(msg, pending[0], *choice)


async def _delete(msg, text: str) -> None:
    hours = parse_hours(text)
    if hours is None:
        await msg.reply_text("Не розпізнав години (0-23) — нічого не видалено.")
        return
    n = await asyncio.to_thread(delete_entries, msg.from_user.id, hours)
    shown = ", ".join(f"{h:02d}:00" for h in sorted(hours))
    await msg.reply_text(f"🗑 Видалено ваших amity: {n} (години UTC: {shown})." if n else
                         f"У вас немає amity цього тижня на {shown} UTC.")


async def handle_amity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram_resources import send_report_blocks   # chunked HTML sender
    msg = update.effective_message
    args = context.args or []
    if args and args[0].lower() in ("delete", "del", "видалити"):
        if len(args) > 1:                      # "/amity delete 8,14" - no need to ask
            await _delete(msg, " ".join(args[1:]))
            return
        mine = sorted({e["hour"] for e in await asyncio.to_thread(_load) if e["user_id"] == msg.from_user.id})
        if not mine:
            await msg.reply_text("У вас немає amity цього тижня.")
            return
        _PENDING[(msg.chat_id, msg.from_user.id)] = ({"delete": True}, time.monotonic() + PENDING_TTL_SECONDS)
        await msg.reply_text("Які години (UTC) видалити? Через кому, наприклад «8, 14». Ваші: "
                             + ", ".join(f"{h:02d}" for h in mine))
        return
    entries = await asyncio.to_thread(_load)
    now = datetime.datetime.now(datetime.timezone.utc)
    if not entries:
        await msg.reply_text("Цього тижня ще ніхто не поділився amity. Надішліть скріншот "
                             "«Memory completed» з підписом, наприклад «Red 4».")
        return
    order = list(COLORS)
    entries.sort(key=lambda e: (e["hour"], order.index(e["color"]), e["option"]))
    head = (f"<b>Amity цього тижня</b> ({len(entries)}) — скидання пн 00:00 UTC, зараз "
            f"{now.hour:02d}:{now.minute:02d} UTC. Щоб отримати такий самий — попросіться в пати.")
    nicks = await asyncio.to_thread(_load_nicks)
    await send_report_blocks(msg, [head] + [_format(e, now.hour, nicks.get(str(e["user_id"]), ""))
                                            for e in entries])


async def handle_iam_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    nick = " ".join(context.args or []).strip()[:32]
    if not nick:
        current = (await asyncio.to_thread(_load_nicks)).get(str(msg.from_user.id))
        await msg.reply_text((f"Ваш нік у грі: {current}\n" if current else "")
                             + "Щоб вказати нік у грі, напишіть: /iam ВашНік")
        return
    await asyncio.to_thread(set_nick, msg.from_user.id, nick)
    await msg.reply_text(f"✅ Запам'ятав: ваш нік у грі — {nick}. Він з'явиться поруч з вами в /amity.")


def build_iam_handler() -> CommandHandler:
    return CommandHandler("iam", handle_iam_command)


def build_amity_handler() -> CommandHandler:
    return CommandHandler("amity", handle_amity_command)


def build_amity_choice_handler() -> MessageHandler:
    return MessageHandler(filters.TEXT & ~filters.COMMAND & _PendingFilter(), handle_choice_text)


# --------------------------------------------------------------------------- self-check

_EN = """MEMORY COMPLETED
Here's what you found:
Favor & The Well Known
When equipped...

Critical hits will be 40% more effective
5.0% of the damage your Ward takes will be
converted to mana
The maximum damage of status effects is
increased by 150.0%

Decreases arcane damage by 30%

% 109,324,554 gold
% 1,308,405 orns
"""
_UK = """СПОМИН ЗАВЕРШЕНО
Ось що ви знайшли:
Будучи спорядженим...

Ви отримаєте на 4% більше золота
Пошкодження від ультимативних атак рейдових
босів зменшено на 5%

Подвійні заклинання будуть менш ефективні на
40%

Позбавляє майстерності володіння держаковою
зброєю
2, 78,116,658 золота
"""

# Real OCR of inventory/equipped amities (2026-09-29): no header, no
# "When equipped..." - the effects follow the fixed description.
_INV_EN = """‘Inventory ,
Siphoning & Feebleness
Spectral essence of a place in time. Amities give one balance
through both bonuses and maluses.
There is a chance that you will recover HP from 5.0% of the
damage dealt to an opponent
Your stats are decreased by 10% when defending territory
TIER * 10
ACQUIRED 9/28/2026
"""
_INV_UK = """IHBeHTap =
Opportunity & Toxins (L)
Одягнений
Spectral essence of a place in time. Amities give one balance
through both bonuses and maluses.
Critical hits will be 40% more effective
Your accessories will be 25% more effective
Your chance to miss an opponent is increased by 2%
Removes proficiency for Swords
РАНГ % 10
OTPUMAHO 9/27/2026
"""


def _demo() -> None:
    """Real OCR of the two guild screenshots. Run `python3 telegram_amity.py`."""
    for text, exp in [("red 4", ("Red", 4)), ("фіолетовий 3", ("Purple", 3)), ("розовий5", ("Purple", 5)),
                      ("4yellow", ("Yellow", 4)), ("Зелена 2", ("Green", 2)), ("синя 1", ("Blue", 1)),
                      ("red", None), ("red 7", None), ("red 4 blue", None), ("lol", None), ("red 4 5", None)]:
        assert parse_choice(text) == exp, (text, parse_choice(text))
    assert looks_like_amity_screen(_EN) and looks_like_amity_screen(_UK)
    assert looks_like_amity_screen("MEMORY C0MPLETED") and not looks_like_amity_screen("NEEDED OFFERINGS")
    assert looks_like_amity_screen(_INV_EN) and looks_like_amity_screen(_INV_UK)
    assert not is_memory_screen(_INV_EN) and not is_memory_screen(_INV_UK) and is_memory_screen(_EN)
    assert acquired_date(_INV_EN) == datetime.date(2026, 9, 28) and acquired_date(_EN) is None
    for text, exp in [("red 4 14", ("Red", 4, 14)), ("червона 4 14:00", ("Red", 4, 14)),
                      ("14 utc red 4", ("Red", 4, 14)), ("4yellow 0", ("Yellow", 4, 0)), ("синя 2 23h", ("Blue", 2, 23)),
                      ("red 4", None), ("red 4 24", None), ("red 7 14", None), ("red 4 14 5", None)]:
        assert parse_choice_hour(text) == exp, (text, parse_choice_hour(text))
    assert describe("x", "Your chance to miss an opponent is increased by 2%", "malus",
                    [{"kind": "malus", "name": "Inaccuracy", "range": "1–2%",
                      "desc": "Your chance to miss an opponent is increased by -%"}])[0] \
        == "Your chance to miss an opponent is increased by 2% (1–2%)"
    assert split_effects(_INV_EN) == (["There is a chance that you will recover HP from 5.0% of the damage dealt "
                                       "to an opponent"], ["Your stats are decreased by 10% when defending territory"])
    assert split_effects(_INV_UK) == (["Critical hits will be 40% more effective",
                                       "Your accessories will be 25% more effective"],
                                      ["Your chance to miss an opponent is increased by 2%",
                                       "Removes proficiency for Swords"])
    b, m = split_effects(_EN)
    assert b == ["Critical hits will be 40% more effective",
                 "5.0% of the damage your Ward takes will be converted to mana"], b
    assert m == ["The maximum damage of status effects is increased by 150.0%",
                 "Decreases arcane damage by 30%"], m
    b, m = split_effects(_UK)
    assert b[1] == "Пошкодження від ультимативних атак рейдових босів зменшено на 5%", b
    assert m == ["Подвійні заклинання будуть менш ефективні на 40%",
                 "Позбавляє майстерності володіння держаковою зброєю"], m
    assert split_effects("MEMORY COMPLETED\nWhen equipped...\nOne\n1,000 gold") is None   # odd count
    # an effect may START with its number; only a letterless line ("40%") wraps
    b, m = split_effects("When equipped...\n5.0% of the damage your Ward takes will be\nconverted to mana\n"
                         "Doublecasts will be less effective by\n40%\n1,000 gold")
    assert b == ["5.0% of the damage your Ward takes will be converted to mana"], b
    assert m == ["Doublecasts will be less effective by 40%"], m

    cards = [{"kind": "bonus", "name": "% Arcane Dmg", "range": "6–30%", "desc": "Increases arcane damage by %"},
             {"kind": "malus", "name": "The Arcane", "range": "9–80%", "desc": "Decreases arcane damage by %"},
             {"kind": "bonus", "name": "% Crit Dmg", "range": "5–40%", "desc": "Critical hits will be % more effective"}]
    text, key = describe("Decreases arcane damage by 30%", "Decreases arcane damage by 30%", "malus", cards)
    assert text == "Decreases arcane damage by 30% (9–80%)" and key.startswith("The Arcane|"), text
    assert describe("x", "Critical hits will be 40% more effective", "bonus", cards)[0] == \
        "Critical hits will be 40% more effective (5–40%)"
    assert describe("Weird unknown thing", "Weird unknown thing", "bonus", cards)[0] == "Weird unknown thing"

    monday = datetime.datetime(2026, 9, 28, 0, 5, tzinfo=datetime.timezone.utc)
    assert _week(monday) != _week(monday - datetime.timedelta(minutes=10))   # Monday 00:00 UTC resets
    assert _week(monday) == _week(monday + datetime.timedelta(days=6, hours=23))
    assert parse_hours("8, 14") == {8, 14} and parse_hours("08:00,23") == {8, 23}
    assert parse_hours("8, 24") is None and parse_hours("nope") is None
    assert "@bob" in _who({"username": "bob"}) and "<" not in _who({"name": "<x>"}).replace("&lt;", "")
    assert "🎮 <code>Odie&amp;Co</code>" in _who({"username": "bob"}, "Odie&Co") and "🎮" not in _who({"username": "bob"})
    print("telegram_amity: all checks passed")


if __name__ == "__main__":
    _demo()
