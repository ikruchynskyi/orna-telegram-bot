"""
telegram_offerings.py
=======================
OCR pipeline for the guild "offerings" screen — the list of progress bars
showing <have> / <need> of each material pledged toward a guild goal — as
opposed to the single-item stat screenshots handled by telegram_assess.py.

Recognised by its header line:
    English:    "NEEDED OFFERINGS:"
    Ukrainian:  "НЕОБХІДНІ ПОЖЕРТВИ:" (also tolerates the common OCR
                misread "НЕОБЗІДНІ", Х <-> З look similar in some fonts)

Each row below the header reads like:
    6,386 / 9,300 Solarite
    <have>  <need>  <material name>
(thousands-separator commas included — stripped before parsing as int.)

The shortfall (need - have) per material is computed, OCR'd names are
resolved to the sheet's canonical spelling, and the result is handed
straight to telegram_resources.build_report for the familiar
guild-availability / proof-cost breakdown + "remind me" reminder bundles.
"""
from __future__ import annotations

import difflib
import html
import logging
import re
from typing import Dict, List, Optional, Tuple

from orna_material_names_uk import UK_TO_EN
from orna_sheets import fetch_sheet_data
from telegram_nlp import OllamaError, extract_resources
from telegram_resources import ReminderBundle, build_report, send_report_blocks

logger = logging.getLogger(__name__)

OFFERINGS_HEADER_RE = re.compile(
    r"(needed\s+offerings|необхідні\s+пожертви|необзідні\s+пожертви)",
    re.IGNORECASE,
)

# A thousands-grouped or plain number. The English client groups with
# commas ("6,366"); the Ukrainian client groups with spaces ("6 366") —
# and OCR adds its own noise on top: a stray period where a space/comma
# should be ("21. 938"), or a group boundary vanishing entirely so a
# 4-digit number comes through with no separator at all ("4155").
#   - grouped form: 1-3 digits, then one-or-more (separator-chars + exactly
#     3 digits) — the separator class is "one or more of , . tab space"
#     rather than a single char, to swallow a stray extra character like
#     the "." in "21. 938" (a comma/space OCR'd as a period, with the
#     normal space still following it).
#   - plain form: a bare run of digits, for when the grouping separator
#     got lost entirely.
# Horizontal whitespace only (no \n) so this can never eat into the next
# OCR line.
_NUM = r"(?:\d{1,3}(?:[.,\t   ]+\d{3})+|\d+)"

# "<have>/<need> <name>" — deliberately NOT anchored to line-start: each
# row's material icon reliably OCRs into stray leading junk (a letter, a
# symbol, or even a bare number — e.g. "Ж 6,366 / 9,394 Solarite" or
# "448 26,518 / 5,869 Greater Soul"), so we just look for the
# "<num> / <num> <name>" pattern anywhere on the line and let it skip over
# whatever junk precedes it. Anchored to line-end so the name capture
# doesn't run past its own line.
_OFFERING_LINE_RE = re.compile(
    rf"({_NUM})\s*/\s*({_NUM})\s+([^\n]+?)\s*$",
    re.MULTILINE,
)

_NON_DIGIT_RE = re.compile(r"\D")


def looks_like_offerings_screen(ocr_text: str) -> bool:
    return bool(OFFERINGS_HEADER_RE.search(ocr_text))


def _to_int(raw: str) -> Optional[int]:
    digits = _NON_DIGIT_RE.sub("", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def parse_offering_lines(ocr_text: str) -> List[Tuple[str, int, int]]:
    """Returns [(raw_name, have, need), ...] for every '<have> / <need> <name>' row found."""
    out: List[Tuple[str, int, int]] = []
    for m in _OFFERING_LINE_RE.finditer(ocr_text):
        have = _to_int(m.group(1))
        need = _to_int(m.group(2))
        name = m.group(3).strip()
        if have is None or need is None or not name:
            continue
        out.append((name, have, need))
    return out


async def resolve_material_name(raw: str, known: List[str]) -> Optional[str]:
    """Map a (possibly OCR-mangled / translated) name to the sheet's canonical spelling.

    Tries, in order: exact English match, exact match against the static
    Ukrainian name table, typo-tolerant fuzzy match against each, and only
    then the LLM. The LLM is last resort, not first choice — gpt-oss:20b is
    not reliable enough to trust on its own (verified: identical input
    returns the right translation on some calls and an empty match on
    others), so anything the static table can answer deterministically
    skips it entirely.
    """
    raw_norm = raw.strip()
    raw_lower = raw_norm.lower()

    for k in known:
        if k.lower() == raw_lower:
            return k

    uk_hit = UK_TO_EN.get(raw_lower)
    if uk_hit and uk_hit in known:
        return uk_hit

    # Cheap typo tolerance for same-alphabet OCR noise, tried against both
    # the English list and the Ukrainian one (each alphabet only matches
    # noise in its own alphabet — that's fine, we try both). Match case-
    # INSENSITIVELY (like the Ukrainian branch below): difflib is case-
    # sensitive, so OCR that upper-cases a name AND misreads a char (e.g.
    # "ADARMANTITE") scored far below cutoff against canonical "Adamantite"
    # and silently missed - the exact combined case this fuzzy step is for.
    known_lower = {}
    for k in known:
        known_lower.setdefault(k.lower(), k)
    close = difflib.get_close_matches(raw_lower, list(known_lower), n=1, cutoff=0.7)
    if close:
        return known_lower[close[0]]
    close_uk = difflib.get_close_matches(raw_lower, list(UK_TO_EN), n=1, cutoff=0.7)
    if close_uk:
        canon = UK_TO_EN[close_uk[0]]
        if canon in known:
            return canon

    # Heavier mangling / an unmapped translation -> ask the model as a last resort.
    try:
        found = await extract_resources(raw_norm, known)
    except OllamaError:
        return None
    return found[0] if found else None


async def build_offerings_report(ocr_text: str) -> Optional[Tuple[List[str], List[ReminderBundle]]]:
    """
    Parse an offerings screenshot's OCR text and return the report as a list
    of HTML blocks plus reminder bundles (see telegram_resources.build_report
    / send_report_blocks), or None if no offering rows were found in the OCR
    text at all.
    """
    rows = parse_offering_lines(ocr_text)
    if not rows:
        return None

    sheet_values = await fetch_sheet_data()
    known = [r[0] for r in sheet_values if r]

    shortfalls: Dict[str, int] = {}
    already_met: List[str] = []
    unresolved: List[str] = []
    for raw_name, have, need in rows:
        shortfall = need - have
        if shortfall <= 0:
            already_met.append(raw_name)
            continue
        canon = await resolve_material_name(raw_name, known)
        if canon is None:
            unresolved.append(raw_name)
            continue
        shortfalls[canon] = shortfalls.get(canon, 0) + shortfall

    notes: List[str] = []
    if already_met:
        notes.append("Вже вистачає: " + ", ".join(html.escape(n) for n in already_met))
    if unresolved:
        notes.append(
            "Не вдалося розпізнати: " + ", ".join(html.escape(n) for n in unresolved)
        )

    if not shortfalls:
        return [("\n".join(notes) if notes else "Бракує ресурсів немає — все зібрано.")], []

    blocks, bundles = await build_report(shortfalls, sheet_values)
    if notes:
        blocks = ["\n\n".join(notes), *blocks]
    return blocks, bundles
