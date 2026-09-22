"""
telegram_nlp.py
================
Natural-language helpers backed by a local Ollama instance (gpt-oss:20b by
default), so the bot can understand free-text resource requests instead of
only slash commands.

Uses Ollama's REST API directly over httpx (no extra pip dependency) with
format="json" to force well-formed structured output.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, List

import httpx

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
OLLAMA_TIMEOUT = 60.0

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


class OllamaError(RuntimeError):
    """Raised when the local Ollama server can't be reached or replies unusably."""


async def _chat_json(system: str, user: str, retries: int = 1) -> dict:
    try:
        return await _chat_json_once(system, user)
    except OllamaError:
        if retries <= 0:
            raise
        return await _chat_json(system, user, retries - 1)


async def _chat_json_once(system: str, user: str) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "think": False,
    }
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_HOST}/api/chat", json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        raise OllamaError(f"Ollama request failed: {e}") from e

    content = data.get("message", {}).get("content", "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # format="json" should guarantee valid JSON, but be tolerant of a
        # stray code fence or preamble anyway.
        m = _JSON_OBJECT_RE.search(content)
        if not m:
            raise OllamaError(f"Ollama returned non-JSON content: {content[:200]!r}")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise OllamaError(f"Ollama returned malformed JSON: {content[:200]!r}") from e


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
    data = await _chat_json(system, user)
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
    data = await _chat_json(system, user)
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
