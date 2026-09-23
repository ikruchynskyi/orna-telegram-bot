"""
telegram_nlp.py
================
Local-Ollama structured-extraction helpers for the free-text resource flow
(telegram_resources.py's ConversationHandler and /orna's `need` tool):
which materials a message refers to, and what quantity of each. Both are
local-only (gpt-oss:20b by default) - no cloud fallback, since a wrong
guess here is cheap to re-ask about and these two calls have never needed
Ollama Cloud's extra reliability the way /orna's ReAct loop does.

/orna's own routing/condition-parsing (formerly route_query/plan_queries,
a single-shot classify-then-parse pipeline) was retired when /orna became
a real ReAct loop (see telegram_orna.py) - the loop's own first turn IS
the routing decision now, so a separate pre-classification call is
redundant. All of that prompt's hard-won lessons (condition kinds, tier
shorthand, useable_by aliases, ability vs effect vs bond disambiguation,
...) live on as telegram_orna.py's `query` tool documentation instead.

The actual HTTP/JSON-parsing mechanics (think:True, tolerant JSON
recovery, ...) live in ollama_client.py, shared with telegram_go.py and
telegram_orna.py - see that module's docstring for why "think": True and
dict-validation both matter.
"""
from __future__ import annotations

import os
from typing import Dict, List

from ollama_client import OllamaError, chat_json  # re-exported: existing `from telegram_nlp import OllamaError` call sites stay valid

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")

__all__ = ["OllamaError", "extract_resources", "extract_quantities"]


async def _local_chat_json(system: str, user: str, retries: int = 1) -> dict:
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        return await chat_json(OLLAMA_HOST, OLLAMA_MODEL, messages, headers)
    except OllamaError:
        if retries <= 0:
            raise
        return await _local_chat_json(system, user, retries - 1)


async def extract_resources(text: str, known: List[str]) -> List[str]:
    """
    Ask the model which materials from `known` the free-text `text` refers to.

    Returns canonical names (exact spelling from `known`), de-duplicated, in
    the order the model listed them. Empty list if none matched.
    Raises OllamaError if the model couldn't be reached at all.
    """
    system = (
        "You identify which Orna RPG crafting materials a player is asking "
        "about. You are given a fixed list of valid material names and a "
        "free-text message (English or Ukrainian, possibly with typos or "
        "abbreviations). Reply with strict JSON of the form "
        '{"resources": ["Exact Name From List", ...]}. '
        "Only include names that are in the list, spelled exactly as given "
        "there. Never invent a name that isn't in the list. If the message "
        'doesn\'t clearly ask about materials from the list, reply '
        '{"resources": []}.'
    )
    user = "Material list:\n" + ", ".join(known) + f"\n\nMessage:\n{text}"
    data = await _local_chat_json(system, user)
    raw = data.get("resources")
    if not isinstance(raw, list):
        return []
    lookup = {k.lower(): k for k in known}
    out: List[str] = []
    seen = set()
    for item in raw:
        canon = lookup.get(str(item).strip().lower())
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


async def extract_quantities(text: str, resources: List[str]) -> Dict[str, int]:
    """
    Ask the model how many of each of `resources` the user's reply specifies.

    Returns {name: qty} only for names the user actually gave a positive
    number for; names not mentioned are omitted (caller should re-ask about
    those). Raises OllamaError if the model couldn't be reached.
    """
    system = (
        "You extract requested quantities of Orna RPG materials from a "
        "player's reply. You are given the list of material names being "
        "asked about and the player's free-text reply (English or "
        "Ukrainian). Reply with strict JSON of the form "
        '{"quantities": {"Exact Name From List": <positive integer>, ...}}. '
        "Only include a material if the reply gives a clear number for it; "
        "omit materials the reply doesn't mention. Never invent names not "
        "in the list."
    )
    user = "Materials:\n" + ", ".join(resources) + f"\n\nReply:\n{text}"
    data = await _local_chat_json(system, user)
    raw = data.get("quantities")
    if not isinstance(raw, dict):
        return {}
    lookup = {k.lower(): k for k in resources}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        canon = lookup.get(str(k).strip().lower())
        if not canon:
            continue
        try:
            qty = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if qty > 0:
            out[canon] = qty
    return out
