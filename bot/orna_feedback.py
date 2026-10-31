"""Players vote on /orna answers with reactions; admins turn the votes into knowledge.

Learning from feedback without retraining a model (the models are hosted; their weights can't be changed):
- Every /orna answer the bot sends is logged (data/state/orna_answers.jsonl, the newest MAX_ANSWERS).
- A reaction on one is a vote: 👍 ❤ 🔥 👏 💯 🎉 🤩 count +1, 👎 💩 🤡 🤬 🤮 count -1; changing or removing it
  updates the vote (data/state/orna_votes.json). No buttons - the guild chat has ~180 people.
  Telegram sends reactions to a bot in a private chat always, and in a group only when the bot is an admin there.
- /feedback [bad|good] (admins): the answers with votes, worst or best first, each with an id.
- /verify <id>: the answer goes into the "verified" knowledge source - a question asked again finds it.
- /correct <id> <what is right>: a correction goes into the "corrections" source, which outranks every guide.
Both are indexed at once (Pinecone + the keyword index), so the next answer already uses them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageReactionHandler

import paths
from bot import usage_stats
from bot.telegram_announce import is_admin_user
from knowledge import orna_curated, orna_pinecone

logger = logging.getLogger(__name__)
ANSWERS = paths.STATE / "orna_answers.jsonl"
VOTES = paths.STATE / "orna_votes.json"
MAX_ANSWERS = 3000
UP = {"👍", "❤", "❤️", "🔥", "👏", "💯", "🎉", "🤩", "😍", "🥰", "⚡"}
DOWN = {"👎", "💩", "🤡", "🤬", "🤮", "😡"}


def _key(chat_id, message_id) -> str:
    return f"{chat_id}:{message_id}"


def record_answer(sent, question: str, answer: str) -> None:
    """Log one sent /orna answer (sent = the Telegram Message returned by the send)."""
    if getattr(sent, "message_id", None) is None or getattr(sent, "chat_id", None) is None:
        return
    row = {"key": _key(sent.chat_id, sent.message_id), "ts": int(time.time()), "q": question[:800], "a": answer[:4000]}
    try:
        with open(ANSWERS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if ANSWERS.stat().st_size > 6_000_000:   # keep the newest MAX_ANSWERS
            lines = ANSWERS.read_text(encoding="utf-8").splitlines()[-MAX_ANSWERS:]
            tmp = ANSWERS.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, ANSWERS)
    except OSError:
        logger.warning("feedback: could not log an answer", exc_info=True)


def _answers() -> dict:
    try:
        return {r["key"]: r for r in (json.loads(l) for l in ANSWERS.read_text(encoding="utf-8").splitlines() if l.strip())}
    except (OSError, ValueError):
        return {}


def _votes() -> dict:
    try:
        return json.loads(VOTES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_votes(v: dict) -> None:
    tmp = VOTES.with_suffix(".tmp")
    tmp.write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, VOTES)


def vote_of(reactions) -> int:
    """+1, -1 or 0 from a user's current reactions (several: the first that counts)."""
    for r in reactions or []:
        e = getattr(r, "emoji", None)
        if e in UP:
            return 1
        if e in DOWN:
            return -1
    return 0


async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    r = update.message_reaction
    if r is None or r.user is None:          # an anonymous admin's reaction carries no user
        return
    key = _key(r.chat.id, r.message_id)
    if key not in _answers():
        return                                # a reaction on something that isn't an /orna answer
    votes = _votes()
    v = vote_of(r.new_reaction)
    if v:
        votes.setdefault(key, {})[str(r.user.id)] = v
    else:
        votes.get(key, {}).pop(str(r.user.id), None)
    _save_votes(votes)
    logger.info("feedback: %s vote %+d on %s", r.user.id, v, key)


def _short(key: str) -> str:
    return key.replace("-100", "").replace(":", "-")


def _find(short: str):
    for key, row in _answers().items():
        if _short(key) == short:
            return key, row
    return None, None


async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/feedback [bad|good]: voted answers, worst (or best) first."""
    msg = update.effective_message
    if not is_admin_user(update.effective_user):
        return
    usage_stats.record_command_for(update, "feedback", " ".join(context.args or []))
    answers, votes = _answers(), _votes()
    rows = [(sum(v.values()), sum(1 for x in v.values() if x > 0), sum(1 for x in v.values() if x < 0), k)
            for k, v in votes.items() if v and k in answers]
    good = (context.args or [""])[0].lower() == "good"
    rows.sort(key=lambda t: (-t[0] if good else t[0], -answers[t[3]]["ts"]))
    if not rows:
        await msg.reply_text("Ще немає голосів. Гравці голосують реакціями (👍/👎) на відповіді /orna.")
        return
    lines = []
    for net, up, down, k in rows[:10]:
        a = answers[k]
        q = a["q"].split("\n")[0][:120]
        lines.append(f"<b>{_short(k)}</b> 👍{up} 👎{down} — {q}\n<i>{a['a'][:200]}</i>")
    await msg.reply_text("\n\n".join(lines) + "\n\n/verify &lt;id&gt; — у перевірені відповіді; "
                         "/correct &lt;id&gt; &lt;що правильно&gt; — у виправлення", parse_mode="HTML")


async def _reindex(ns: str) -> None:
    if orna_pinecone.enabled():
        try:
            await asyncio.to_thread(orna_pinecone.index_corpus, ns)
        except Exception:
            logger.warning("feedback: re-indexing %s failed", ns, exc_info=True)


async def handle_verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/verify <id>: the answer becomes a verified answer to its question."""
    msg = update.effective_message
    if not is_admin_user(update.effective_user):
        return
    key, row = _find((context.args or [""])[0])
    if row is None:
        await msg.reply_text("Немає відповіді з таким id — див. /feedback.")
        return
    title = row["q"].split("\n")[0][:90]
    orna_curated.add("verified", title, f"Question: {row['q']}\nVerified answer: {row['a']}")
    await _reindex("verified")
    await msg.reply_text(f"✅ Додано до перевірених відповідей: «{title}».")


async def handle_correct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/correct <id> <what is right>: a correction for that question, outranking every guide."""
    msg = update.effective_message
    if not is_admin_user(update.effective_user):
        return
    args = context.args or []
    key, row = _find(args[0] if args else "")
    fix = " ".join(args[1:]).strip()
    if row is None or not fix:
        await msg.reply_text("Використання: /correct <id> <що правильно> (id — з /feedback).")
        return
    title = row["q"].split("\n")[0][:90]
    orna_curated.add("corrections", f"{title} (admin correction)",
                     f"Question: {row['q']}\nThe bot answered (WRONG): {row['a'][:800]}\nCorrect, per an admin: {fix}")
    await _reindex("corrections")
    await msg.reply_text(f"✅ Виправлення збережено для «{title}». Наступна відповідь його врахує.")


def handlers() -> list:
    return [MessageReactionHandler(handle_reaction), CommandHandler("feedback", handle_feedback),
            CommandHandler("verify", handle_verify), CommandHandler("correct", handle_correct)]


def _demo() -> None:
    class R:
        def __init__(self, e):
            self.emoji = e
    assert vote_of([R("👍")]) == 1 and vote_of([R("💩")]) == -1 and vote_of([R("🤔")]) == 0 and vote_of([]) == 0
    assert _short("-1001185210862:345") == "1185210862-345" and _short("388352701:12") == "388352701-12"
    print("orna_feedback: all checks passed")


if __name__ == "__main__":
    _demo()
