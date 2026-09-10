"""
orna_calendar.py
=================
Google Calendar "add event" links for guild shop rotations, so a user can
save a reminder instead of relying on memory / re-checking the bot.

One all-day event per (guild, date) — deliberately all-day rather than a
timed slot, since neither the exact daily shop-reset time nor the user's
timezone is known; an all-day event needs no timezone at all and still
lands on the right day in Google Calendar.

If several requested materials appear at the same guild on the same day,
they're bundled into a single event (matching how you'd actually spend
that guild's proofs in one visit) rather than one event per material.
"""
from __future__ import annotations

import datetime
from typing import List, NamedTuple
from urllib.parse import quote_plus

CALENDAR_BASE = "https://calendar.google.com/calendar/u/0/r/eventedit"

# Characters left unescaped so the query string stays readable (matches the
# style Google's own "quick add" links use) — purely cosmetic, the link
# works either way since browsers decode percent-escapes regardless.
_SAFE = ":-,!"


class MaterialNeed(NamedTuple):
    name: str
    qty: int
    proofs: int


def _q(s: str) -> str:
    return quote_plus(s, safe=_SAFE)


def build_calendar_link(
    guild: str,
    currency: str,
    date: datetime.date,
    materials: List[MaterialNeed],
) -> str:
    """Build an all-day 'add to calendar' link for one guild's rotation day."""
    total_proofs = sum(m.proofs for m in materials)
    start = date.strftime("%Y%m%d")
    end = (date + datetime.timedelta(days=1)).strftime("%Y%m%d")

    details_lines = [
        f"Guild: {guild}",
        f"Proof currency: {currency}",
        f"Total needed: {total_proofs} {currency}",
        "",
        "Materials:",
    ]
    details_lines += [f"{m.qty}x {m.name} - {m.proofs} {currency}" for m in materials]
    details = "\n".join(details_lines)

    query = "&".join(
        f"{key}={_q(value)}"
        for key, value in (
            ("text", f"AL Planner: {guild}"),
            ("dates", f"{start}/{end}"),
            ("details", details),
        )
    )
    return f"{CALENDAR_BASE}?{query}"
