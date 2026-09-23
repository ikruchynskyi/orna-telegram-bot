"""
usage_stats.py
================
Lightweight usage counters: how many times each slash command was
invoked, and how many LLM calls were made per model. Persisted to
usage_stats.json (gitignored) so counts survive this bot's frequent
launchctl reloads - same reasoning as telegram_remind.py's
reminders.json.

Only slash commands are counted as "questions to the bot" today, not
the free-text conversation entry points in telegram_assess.py/
telegram_resources.py - those cover most real usage already, and adding
the rest is a straightforward follow-up if the totals here turn out to
undercount noticeably.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_STORE_PATH = Path(__file__).parent / "usage_stats.json"

_commands: Counter = Counter()
_llm_calls: Counter = Counter()
_since: str = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> None:
    global _since
    if not _STORE_PATH.exists():
        return
    try:
        data = json.loads(_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("usage_stats: couldn't read %s, starting fresh", _STORE_PATH, exc_info=True)
        return
    _commands.update(data.get("commands", {}))
    _llm_calls.update(data.get("llm_calls", {}))
    _since = data.get("since", _since)


def _save() -> None:
    try:
        _STORE_PATH.write_text(json.dumps(
            {"since": _since, "commands": dict(_commands), "llm_calls": dict(_llm_calls)}
        ))
    except OSError:
        logger.warning("usage_stats: couldn't write %s", _STORE_PATH, exc_info=True)


_load()


def record_command(name: str) -> None:
    """Call once at the top of a slash command's handler."""
    _commands[name] += 1
    _save()


def record_llm_call(model: str) -> None:
    """Call once per LLM API call actually made, tagged by model name."""
    _llm_calls[model] += 1
    _save()


def snapshot() -> Dict[str, Dict[str, int]]:
    return {"since": _since, "commands": dict(_commands), "llm_calls": dict(_llm_calls)}
