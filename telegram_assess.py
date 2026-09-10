"""
telegram_assess.py
==================

Telegram bot flow for assessing Orna RPG item screenshots.

Pipeline:
  1. User sends a screenshot of an item
  2. Bot OCRs it (English + Ukrainian via Tesseract)
  3. Extracts item name + observed stats + upgrade level
  4. Looks the item up in the playorna.com codex (auto-detects language)
  5. Reverse-engineers the quality % and projects every stat across upgrade levels
  6. Replies with an HTML-formatted assessment table

If step 4 (codex lookup) fails — usually because OCR misread the name —
the bot enters an `AWAITING_NAME` state and asks the user to type the
correct item name. The stats parsed from the screenshot are kept in
`context.user_data` so the assessment can be completed once the name
is known. The user can also send `/cancel` to abort.

System prerequisites (the Tesseract binaries are NOT pip deps):
    Ubuntu/Debian: sudo apt install tesseract-ocr tesseract-ocr-ukr
    macOS:         brew install tesseract tesseract-lang
    Windows:       https://github.com/UB-Mannheim/tesseract/wiki

Python deps:
    pip install python-telegram-bot[ext] pytesseract Pillow requests beautifulsoup4

Register in your bot — one line:
    from telegram_assess import build_assess_conversation
    application.add_handler(build_assess_conversation())
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps
import pytesseract
from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from orna_assess import (
    AssessInput,
    AssessResult,
    CodexEntry,
    get_assess_result,
    get_quality_name,
)
from orna_codex import lookup_by_name
from telegram_offerings import build_offerings_report, looks_like_offerings_screen
from telegram_resources import send_report_blocks


logger = logging.getLogger(__name__)


# =============================================================================
# Conversation states
# =============================================================================

# State for ConversationHandler: waiting for the user to type the item's name
# after a failed codex lookup.
AWAITING_NAME = 1

# Key under which we stash the screenshot's parsed data while awaiting the
# user's manual name entry.
_PENDING_KEY = "pending_assessment"


# =============================================================================
# OCR text -> structured fields
# =============================================================================

# Each pattern: (canonical_key, regex). Matched against the whole OCR text.
# Aliases cover both English (full + abbreviated) and Ukrainian forms,
# including in-game abbreviations like "Ф. Зах" (physical defense),
# "М. Опір" (magical resistance), "Атк", "Маг", "Спр".
#
# OCR also frequently confuses look-alike Cyrillic/Latin letters in these
# short Ukrainian abbreviations:
#    Маг -> "Mar"  (М->M, а->a, г->r)
#    Спр -> "Cnp"  (С->C, п->n, р->p)
#    Атк -> "ATK"  (already correct visually but uppercase)
# We add those Latin look-alike spellings as aliases too.
#
# Note on the Ukrainian patterns: in-game stat names use abbreviated forms
# (Атк/Маг/Зах/Опір/Спр) that the codex doesn't display — the codex uses
# full forms (Атака/Магія/Захист/Опір/Спритність). We match BOTH so we can
# read screenshots and reuse the same internal canonical keys for codex
# stats.
_STAT_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # attack
    ("attack",     re.compile(r"\b(?:attack|atk|att|атака|атк)\b\.?\s*:?\s*([+\-]?[\d,]+)", re.I)),
    # magic — "Mar" is a common OCR misread of "Маг"
    ("magic",      re.compile(r"\b(?:magic|mag|магія|маг|mar)\b\.?\s*:?\s*([+\-]?[\d,]+)", re.I)),
    # defense — also matches "Ф. Зах" / "Ф.Зах" / "Ф Зах" (Physical Defense in-game)
    ("defense",    re.compile(r"(?:\b(?:ф|f)\b\.?\s*)?\b(?:defense|defence|def|захист|зах)\b\.?\s*:?\s*([+\-]?[\d,]+)", re.I)),
    # resistance — also matches "М. Опір" / "M Opir" (Magical Resistance in-game)
    ("resistance", re.compile(r"(?:\b(?:м|m)\b\.?\s*)?\b(?:resistance|res|опір|оп)\b\.?\s*:?\s*([+\-]?[\d,]+)", re.I)),
    # hp
    ("hp",         re.compile(r"\b(?:hp|health|оз|хр)\b\.?\s*:?\s*([+\-]?[\d,]+)", re.I)),
    # mana — "Мапа" is a common OCR misread of "Mana" with eng+ukr (M→М, a→а, n→п)
    ("mana",       re.compile(r"\b(?:mana|mp|мана|мр|мапа)\b\.?\s*:?\s*([+\-]?[\d,]+)", re.I)),
    # crit
    ("crit",       re.compile(r"\b(?:crit|critical|крит)\b\.?\s*:?\s*([+\-]?[\d,]+)\s*%?", re.I)),
    # dexterity — "Cnp" / "Cпp" are common OCR misreads of "Спр"
    ("dexterity",  re.compile(r"\b(?:dexterity|dex|спритність|спр|cnp|cпp)\b\.?\s*:?\s*([+\-]?[\d,]+)", re.I)),
    # ward
    ("ward",       re.compile(r"\b(?:ward|вард)\b\.?\s*:?\s*([+\-]?[\d,]+)\s*%?", re.I)),
    # foresight
    ("foresight",  re.compile(r"\b(?:foresight|fore|ініціатива|ініц)\b\.?\s*:?\s*([+\-]?[\d,]+)", re.I)),
]

# Recognises "Lv 5", "Lvl. 5", "Level 5", and Ukrainian "Рівень 5" / "Рів 5".
# Deliberately does NOT match a bare "+5" — that would clash with parenthetical
# stat-deltas like "Mana: 12 (+4)".
_LEVEL_PATTERN = re.compile(
    r"\b(?:lv|lvl|level|рівень|рів)\.?\s*[:#]?\s*(\d{1,2})\b", re.I
)

# Words that indicate a line is a stat / metadata, not the item name.
_NAME_DISQUALIFIERS = {
    # English
    "attack", "atk", "att", "magic", "mag", "defense", "defence", "def",
    "resistance", "res", "hp", "mana", "mp", "crit", "critical",
    "dexterity", "dex", "ward", "foresight", "tier", "rarity",
    "level", "lv", "lvl", "adornment", "category", "useable", "usable",
    "materials", "acquired", "inventory",
    # Ukrainian
    "атака", "атк", "магія", "маг", "захист", "зах", "опір", "оп",
    "оз", "хр", "мана", "мр", "крит", "спритність", "спр", "вард",
    "ініціатива", "ініц", "рівень", "рів", "ранг", "рідкість", "тип",
    "доступно", "категорія", "матеріали", "інвентар", "прикрас",
}

# Breadcrumb separators in titles like "Inventory · Masterforged Deathbringer"
_BREADCRUMB_RE = re.compile(r"\s+[-–·•]\s+")


# -----------------------------------------------------------------------------
# Anguish-mode alternate stats removal
# -----------------------------------------------------------------------------
# Anguish-eligible items show TWO stat blocks in the in-game UI:
#   1. Default stats (used for quality assessment)
#   2. Alternate stats shown when the item is equipped in its Anguish Level
# Below the second block, the game prints an explanation paragraph like:
#   "This item receives alternate stats when equipped in its relevant
#    Anguish Level. When equipped in lower Anguish Levels, it will use
#    downgraded versions of these stats."
# We detect that paragraph, then walk backwards over the lines that look
# like the alternate-stat block and strip them, so stat extraction only
# sees the default block.
_ANGUISH_ALT_MARKER = re.compile(
    r"alternate\s+stats"
    r"|anguish\s+level"
    r"|downgraded\s+versions?"
    # Ukrainian guesses — "альтернативн" (alternative), "Рівень анг"/"Анг рівень",
    # "знижен" (downgraded). Extend if the real localised phrasing differs.
    r"|альтернативн"
    r"|анг\.?\s*рівень|рівень\s+анг"
    r"|знижен",
    re.IGNORECASE,
)

# Lines we treat as part of the alternate-stat block when walking backward
# from the explanation marker. A line "looks like stats" if it has a
# `<word>: <number>` pattern OR matches one of the canonical stat patterns
# OR starts with a `+<word>` bonus marker like "+Summon Stats: +3%".
_STAT_HINT_RE = re.compile(
    r"[A-Za-zА-Яа-яЇїІіЄєҐґ'+]+\s*:\s*[+\-]?\d+", re.UNICODE
)
_BONUS_STAT_RE = re.compile(r"^\s*\+\s*\w[\w\s]*:", re.UNICODE)


def _line_looks_like_stats(line: str) -> bool:
    """Heuristic check used only by `_strip_anguish_alternate_block`.

    Broader than the canonical stat regexes — it also accepts bonus lines
    like "+Summon Stats: +3%" that the regular extractor doesn't parse,
    so we strip them along with the rest of the alternate block.
    """
    if _STAT_HINT_RE.search(line):
        return True
    if _BONUS_STAT_RE.match(line):
        return True
    return any(pat.search(line) for _, pat in _STAT_PATTERNS)


def _strip_anguish_alternate_block(text: str) -> str:
    """Remove the alternate-stats block from OCR text on Anguish-mode items.

    Returns the text unchanged when:
      * The explanation marker isn't present (item has a single stat block).
      * Only one stat block exists above the marker (no alternate to strip;
        stripping would erase the only stats we have).
    """
    m = _ANGUISH_ALT_MARKER.search(text)
    if not m:
        return text

    # Align to the start of the marker's line so the last entry of `lines`
    # is a complete preceding line, not a fragment of the marker sentence.
    line_start = text.rfind("\n", 0, m.start()) + 1
    pre = text[:line_start]
    rest = text[line_start:]

    lines = pre.splitlines(keepends=True)

    # Walk backward over blanks + stat-looking lines to locate the
    # alternate-stat block. `cut` is the index where removal would begin.
    cut = len(lines)
    stat_lines_seen = 0
    i = len(lines) - 1
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped:
            i -= 1
            continue
        if _line_looks_like_stats(stripped):
            cut = i
            stat_lines_seen += 1
            i -= 1
            continue
        break

    if stat_lines_seen == 0:
        return text  # nothing stat-shaped above marker

    # Require that there is ANOTHER stat block above the candidate block.
    # Without this guard, an item with only one stat block that happens to
    # mention any of the marker phrases would have its only stats erased.
    has_block_above = False
    j = cut - 1
    while j >= 0:
        stripped = lines[j].strip()
        if not stripped:
            j -= 1
            continue
        if _line_looks_like_stats(stripped):
            has_block_above = True
            break
        j -= 1
    if not has_block_above:
        return text

    return "".join(lines[:cut]) + rest


def _extract_stats(text: str) -> Dict[str, float]:
    """Pull every recognised stat → numeric value from the OCR text."""
    out: Dict[str, float] = {}
    for key, pat in _STAT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                out[key] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return out


def _extract_level(text: str) -> int:
    """Detect upgrade level (1..20) or default to 1 if not visible."""
    m = _LEVEL_PATTERN.search(text)
    if m:
        try:
            lv = int(m.group(1))
            if 1 <= lv <= 20:
                return lv
        except ValueError:
            pass
    return 1


def _detect_language(text: str) -> str:
    """Determine OCR text language from character distribution.

    Tesseract loaded with both 'eng' and 'ukr' may misread individual English
    glyphs as look-alike Cyrillic (O→О, A→А, etc.), so a single Cyrillic char
    isn't enough to flip to Ukrainian. We require Cyrillic letters to
    *dominate* the Latin ones in the OCR output.
    """
    cyrillic = 0
    latin = 0
    for c in text:
        if "\u0400" <= c <= "\u04FF":
            cyrillic += 1
        elif c.isascii() and c.isalpha():
            latin += 1
    # Need at least a few Cyrillic chars AND more Cyrillic than Latin to
    # confidently switch to the Ukrainian codex.
    return "uk" if cyrillic > latin and cyrillic >= 5 else "en"


def _extract_name(text: str) -> Optional[str]:
    """
    Pick the most plausible item-name line from OCR output.

    Orna's item detail screen shows the name in a styled middle-title block.
    Surrounding context varies:
      - Top breadcrumb: "Inventory" / "Інвентар" (often missing if user
        scrolled, or mangled by OCR)
      - Below the title in Anguish-eligible items: "Anguished N"

    Strategy order (each falls through to the next):

    1. If "Anguished N" appears, the item name is the line immediately
       above it. (Most reliable when present.)
    2. If an inventory header line is visible, the name is the first
       plausible line after it (skipping OCR junk).
    3. Otherwise: walk the first ~20 lines and pick the first candidate
       that passes quality checks; prefer middle-title (non-breadcrumb)
       over breadcrumb-extracted.
    """
    from orna_codex import _looks_like_quality  # local import to avoid cycle

    raw_lines = text.splitlines()

    # ---- Strategy 1: Anguished anchor ---------------------------------------
    # Pattern: a line consisting essentially of "Anguished N" (or "Анг. N").
    # The item name is the line right before it. Quality + enchantment may
    # still prefix the name; codex search candidate generator handles that.
    anguished_re = re.compile(
        r"^\s*(?:anguished|анг(?:уст|у|\.)?|анґуст)\s*\d+\s*$", re.I
    )
    for i, raw in enumerate(raw_lines):
        if anguished_re.match(raw) and i > 0:
            for j in range(i - 1, max(-1, i - 4), -1):
                cleaned = _clean_name_line(raw_lines[j])
                if cleaned is not None:
                    return _strip_trailing_quality(cleaned, _looks_like_quality)
            break  # found Anguished but no clean line above — give up on this strategy

    # ---- Strategy 2: inventory-header anchor --------------------------------
    inventory_idx = -1
    for i, raw in enumerate(raw_lines):
        low = raw.lower()
        # Match common forms even if the leading char was eaten:
        #   "Inventory", "інвентар", "нвентар" (І dropped), "ІНВЕНТАР"
        if "вентар" in low or "inventor" in low:
            inventory_idx = i
            break

    if inventory_idx >= 0:
        # Walk the next several lines (allow garbage in between like
        # "OOCO00GEO"). Prefer lines ending with a quality suffix (Ukrainian
        # signal); otherwise take the first plausible line.
        with_quality: List[str] = []
        any_plausible: List[str] = []
        for raw in raw_lines[inventory_idx + 1: inventory_idx + 6]:
            cleaned = _clean_name_line(raw)
            if cleaned is None:
                continue

            tokens = cleaned.split()
            had_quality = bool(tokens and _looks_like_quality(tokens[-1]))
            if had_quality:
                tokens = tokens[:-1]
            stripped = " ".join(tokens).strip()
            if len(stripped) < 3:
                continue

            if had_quality:
                with_quality.append(stripped)
            any_plausible.append(stripped)

        if with_quality:
            return with_quality[0]
        if any_plausible:
            return any_plausible[0]

    # ---- Strategy 3: legacy candidate-collection ----------------------------
    candidates: List[Tuple[str, bool]] = []
    for raw_line in raw_lines[:20]:
        cleaned, was_breadcrumb = _clean_name_line_with_breadcrumb_flag(raw_line)
        if cleaned is None:
            continue
        candidates.append((cleaned, was_breadcrumb))

    if not candidates:
        return None
    non_breadcrumb = [c for c, bc in candidates if not bc]
    if non_breadcrumb:
        return _strip_trailing_quality(non_breadcrumb[0], _looks_like_quality)
    return _strip_trailing_quality(candidates[0][0], _looks_like_quality)


def _strip_trailing_quality(name: str, looks_like_quality_fn) -> str:
    """Drop a trailing quality token (e.g. mangled '[Легендарне]') from a name."""
    tokens = name.split()
    while tokens and looks_like_quality_fn(tokens[-1]):
        tokens.pop()
    return " ".join(tokens).strip() or name


def _looks_like_junk_token(tok: str) -> bool:
    """Reject OCR garbage like 'OOCO00GEO' or 'OOOO0GEO' (all-caps + digits soup).

    A real word is letters with maybe an apostrophe or a final 'X'/digit.
    Garbage tokens have digits scattered IN THE MIDDLE between letters with
    no spaces, OR are all-caps single tokens with any digits at all (the
    "OOCO00GEO" case — looks like a random ID).
    """
    if not tok:
        return True
    has_letter = any(c.isalpha() for c in tok)
    if not has_letter:
        return True

    digits = sum(1 for c in tok if c.isdigit())
    letters = sum(1 for c in tok if c.isalpha())

    if digits > 0 and letters > 0:
        # Token mixing digits and letters: usually OCR junk. Real cases like
        # "Lv1" or version "X" are caught by surrounding regex/context, not
        # here, so be strict — any digit interleaved with multiple letters
        # makes it suspicious.
        # >20% digits, OR all-caps with any digit -> junk.
        if digits / (digits + letters) > 0.2:
            return True
        # All-caps with digits anywhere is a near-certain ID/garbage marker.
        if tok.upper() == tok and digits > 0:
            return True
    return False


def _clean_name_line(raw: str) -> Optional[str]:
    """Sanity-check + clean a single OCR line; returns None if it's not a name."""
    line = raw.strip().strip(".,;:|").strip()
    if len(line) < 3:
        return None
    if _has_disqualifying_word(line):
        return None
    letters = sum(c.isalpha() or c.isspace() or c in "'-" for c in line)
    if letters / len(line) < 0.6:
        return None
    if len(line.split()) > 8:
        return None

    tokens = line.split()
    if not tokens:
        return None
    # Reject lines whose ONLY token looks like OCR junk (e.g. "OOCO00GEO").
    if len(tokens) == 1 and _looks_like_junk_token(tokens[0]):
        return None
    # Reject lines where every token looks like junk.
    if all(_looks_like_junk_token(t) for t in tokens):
        return None
    return line


def _clean_name_line_with_breadcrumb_flag(raw: str) -> Tuple[Optional[str], bool]:
    """Like _clean_name_line, but also reports whether a breadcrumb was stripped."""
    line = raw.strip().strip(".,;:|").strip()
    if len(line) < 3:
        return None, False

    # Detect breadcrumb format: separator-split or known prefix word
    parts = _BREADCRUMB_RE.split(line)
    was_breadcrumb = False
    if len(parts) > 1:
        line = parts[-1].strip()
        was_breadcrumb = True
    else:
        stripped = _strip_breadcrumb_prefix(line)
        if stripped != line:
            line = stripped
            was_breadcrumb = True

    cleaned = _clean_name_line(line)
    return cleaned, was_breadcrumb


# Words that, on their own at the start of a line, indicate a breadcrumb
# even when the separator wasn't OCR'd cleanly.
_BREADCRUMB_HEADERS: Tuple[str, ...] = ("inventory", "інвентар")


def _strip_breadcrumb_prefix(line: str) -> str:
    """If `line` begins with a breadcrumb header word, drop it."""
    words = line.split(maxsplit=1)
    if len(words) >= 2 and words[0].lower() in _BREADCRUMB_HEADERS:
        return words[1].lstrip(" -·•–").strip()
    return line


def _has_disqualifying_word(line: str) -> bool:
    """True if any whole word in `line` is in _NAME_DISQUALIFIERS."""
    # Tokenise on non-letter chars to handle "HP:" / "Att:" / "Ward%".
    tokens = re.split(r"[^\w']+", line.lower(), flags=re.UNICODE)
    return any(t in _NAME_DISQUALIFIERS for t in tokens if t)


def _log_ocr_dump(chat_id: int, user_id: int, ocr_text: str) -> None:
    """Console-only diagnostic dump of raw OCR output.

    Wrapped in a clearly-marked block so it's easy to grep in service logs.
    NOT shown to the end user — Telegram replies stay clean.
    """
    border = "=" * 70
    line_count = ocr_text.count("\n") + 1
    char_count = len(ocr_text)
    logger.info(
        "%s\nOCR DUMP  chat=%s  user=%s  %d chars, %d lines\n%s\n%s\n%s",
        border, chat_id, user_id, char_count, line_count,
        border, ocr_text.rstrip(), border,
    )


# =============================================================================
# Image preprocessing for OCR
# =============================================================================

def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """
    Game UIs usually have light text on dark backgrounds. Tesseract is happier
    with the inverse, scaled up, in grayscale.
    """
    img = img.convert("L")
    # Upscale small images so glyphs are bigger than ~20px (rough rule of thumb)
    if img.width < 800:
        ratio = 800 / img.width
        img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                         Image.LANCZOS)
    # If the image is mostly dark, invert it (white text → black text)
    if sum(img.getdata()) / (img.width * img.height) < 100:
        img = ImageOps.invert(img)
    return img


def _ocr(img_bytes: bytes) -> str:
    """OCR with English + Ukrainian language packs.

    Requires Tesseract + the Ukrainian data file installed:
        Ubuntu/Debian: apt install tesseract-ocr tesseract-ocr-ukr
        macOS:         brew install tesseract tesseract-lang
    If the Ukrainian pack is missing, falls back to English only.
    """
    _ensure_tesseract_paths()
    with Image.open(io.BytesIO(img_bytes)) as img:
        prepped = _preprocess_for_ocr(img)
        try:
            return pytesseract.image_to_string(prepped, lang="eng+ukr", config="--psm 6")
        except pytesseract.TesseractError as e:
            # Likely "Failed loading language 'ukr'" - fall back to English.
            logger.warning("Ukrainian OCR pack unavailable, falling back to eng: %s", e)
            return pytesseract.image_to_string(prepped, lang="eng", config="--psm 6")


# Common Tesseract install locations probed when the binary is not on PATH.
# Order matters: more specific / more recent first.
_TESSERACT_FALLBACK_PATHS: Tuple[str, ...] = (
    "/opt/homebrew/bin/tesseract",   # Apple Silicon Homebrew
    "/usr/local/bin/tesseract",      # Intel Homebrew (and Linuxbrew)
    "/opt/local/bin/tesseract",      # MacPorts
    "/usr/bin/tesseract",            # Linux distro packages
)

_tesseract_paths_set = False


def _ensure_tesseract_paths() -> None:
    """Locate the Tesseract binary and tessdata directory.

    macOS LaunchAgent / launchd services don't inherit the interactive shell's
    PATH, so a bot that works under `python bot.py` from a terminal often
    fails under launchctl with a TesseractNotFoundError. Probing the canonical
    Homebrew locations and setting `pytesseract.tesseract_cmd` fixes that
    without requiring the user to edit their plist.
    """
    global _tesseract_paths_set
    if _tesseract_paths_set:
        return
    _tesseract_paths_set = True

    import os
    import shutil

    # 1. Try PATH first (works for normal shells)
    found = shutil.which("tesseract")
    if not found:
        # 2. Probe canonical install locations
        for path in _TESSERACT_FALLBACK_PATHS:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                found = path
                break

    if not found:
        logger.warning(
            "Tesseract binary not found in PATH or canonical install locations: %s",
            _TESSERACT_FALLBACK_PATHS,
        )
        return

    pytesseract.pytesseract.tesseract_cmd = found

    # Help Tesseract find language data files when launchd doesn't pass env.
    # Homebrew layout: <prefix>/bin/tesseract  ->  <prefix>/share/tessdata
    if "TESSDATA_PREFIX" not in os.environ:
        prefix = os.path.dirname(os.path.dirname(found))
        tessdata = os.path.join(prefix, "share", "tessdata")
        if os.path.isdir(tessdata):
            os.environ["TESSDATA_PREFIX"] = tessdata

    logger.info(
        "Tesseract: cmd=%s TESSDATA_PREFIX=%s",
        found, os.environ.get("TESSDATA_PREFIX", "<system default>"),
    )


# =============================================================================
# Response formatting
# =============================================================================

# Stats considered for the simple-ratio quality calculation, in priority order.
# We pick the first stat where we have both an observed value (from OCR) and
# a non-zero codex base. Crit/Ward/Foresight are excluded by user request.
_QUALITY_PRIORITY_STATS: Tuple[str, ...] = (
    "attack", "magic", "resistance", "defense", "hp", "mana", "dexterity",
)


def _calculate_quality_simple(
    observed_stats: Dict[str, float],
    codex_stats: Dict[str, Any],
) -> Tuple[Optional[int], Optional[str], Optional[float], Optional[float]]:
    """
    Pick one stat from _QUALITY_PRIORITY_STATS that's present in both the OCR
    result and the codex with non-zero values, then compute:
        quality % = observed / codex_base * 100

    Returns (quality, picked_key, observed_val, codex_base) or all-None when
    no priority stat is usable.
    """
    for key in _QUALITY_PRIORITY_STATS:
        observed = observed_stats.get(key)
        base = codex_stats.get(key)
        if (observed is not None and observed != 0
                and isinstance(base, (int, float)) and base != 0):
            quality = round(observed / base * 100)
            return quality, key, float(observed), float(base)
    return None, None, None, None


def _format_response(
    entry: CodexEntry,
    result: AssessResult,
    source_url: str,
    detected_stats: Dict[str, float],
    detected_level: int,
    quality_signal: Optional[Tuple[str, float, float]] = None,
) -> str:
    """Produce the HTML-formatted reply text.

    quality_signal: (picked_stat_key, observed_value, codex_base) — shown to
    the user so they can see exactly how the quality was derived.
    """
    name = html.escape(entry.name or "Unknown item")
    quality_label = (get_quality_name(result.quality_code) or "").replace(
        "quality.", ""
    ).title()

    lines = [f"🗡 <b>{name}</b>"]
    lines.append(f"Quality: <b>{result.quality}%</b> ({quality_label})")
    if quality_signal is not None:
        picked_key, picked_obs, picked_base = quality_signal
        display_pick = picked_key.replace("_", " ")
        lines.append(
            f"<i>From {display_pick}: {int(picked_obs)} ÷ {int(picked_base)} × 100</i>"
        )

    flags = []
    if entry.is_celestial_weapon:
        flags.append("celestial")
    if entry.is_two_handed:
        flags.append("two-handed")
    if entry.is_accessory:
        flags.append("accessory")
    if entry.is_adornment:
        flags.append("adornment")
    if flags:
        lines.append("Type: " + ", ".join(flags))
    lines.append(f"Level detected: {detected_level} · Upgrades to lv {result.levels}")
    lines.append("")

    # Show only the upgrade ceiling - that's what players are aiming for.
    # For 13-level items: L10 (last regular) + the three forged tiers.
    # For celestials (20 levels): the top 4.
    if result.levels >= 20:
        cols = [10, 15, 19, 20]
        col_labels = [f"L{c}" for c in cols]
    elif result.levels >= 13:
        cols = [10, 11, 12, 13]
        col_labels = ["L10", "MF", "DF", "GF"]
    else:
        cols = [1]
        col_labels = ["L1"]

    # Display aliases for cramped column widths
    display_name = {
        "adornment_slots": "slots",
        "dexterity": "dex",
        "resistance": "resist",
        "foresight": "foresight",
    }

    NAME_W, VAL_W = 9, 6
    header = ["Stat".ljust(NAME_W), "Obs".rjust(VAL_W)] + [
        lbl.rjust(VAL_W) for lbl in col_labels
    ]
    rows = [header]

    # Only show stats the codex actually tracks for this item. OCR-only stats
    # (HP=0, Def=0 on a sword, etc.) aren't on the item, so they don't belong
    # in the projection table. The "Obs" column carries the OCR value when we
    # have one.
    preferred_order = [
        "attack", "magic", "defense", "resistance", "hp", "mana",
        "dexterity", "crit", "ward", "foresight", "adornment_slots",
    ]
    ordered_keys = [k for k in preferred_order if k in result.stats] + [
        k for k in result.stats if k not in preferred_order
    ]

    for key in ordered_keys:
        codex_row = result.stats[key]
        if not codex_row.values:
            continue

        label = display_name.get(key, key.replace("_", " "))[:NAME_W]
        cells = [label.ljust(NAME_W)]

        # Observed column from OCR (or "-" if we didn't see it)
        observed_val = detected_stats.get(key)
        if observed_val is None:
            cells.append("-".rjust(VAL_W))
        else:
            cells.append(f"{int(round(observed_val))}".rjust(VAL_W))

        # Projection across upgrade levels at the calculated quality
        for c in cols:
            if c - 1 < len(codex_row.values):
                v = codex_row.values[c - 1]
                cells.append(f"{int(round(v))}".rjust(VAL_W))
            else:
                cells.append("-".rjust(VAL_W))
        rows.append(cells)

    table = "\n".join("  ".join(r) for r in rows)
    lines.append(f"<pre>{html.escape(table)}</pre>")
    lines.append(f'<a href="{html.escape(source_url)}">codex page</a>')
    return "\n".join(lines)


# =============================================================================
# Handlers — register the ConversationHandler from build_assess_conversation()
# =============================================================================

async def _run_assessment_and_reply(
    msg: Message,
    entry: CodexEntry,
    source_url: str,
    observed_stats: Dict[str, float],
    item_level: int,
) -> None:
    """Compute quality (simple ratio strategy), project stats, send the reply.

    Strategy: pick one priority stat (Att/Mag/Res/Def/HP/Mana/Dex) that's in
    both the OCR result and the codex with non-zero values, and compute
    quality % = observed / codex_base * 100. Then project all codex stats
    across upgrade levels at that quality.
    """
    quality, picked_key, picked_obs, picked_base = _calculate_quality_simple(
        observed_stats, entry.stats,
    )
    if quality is None:
        obs_summary = ", ".join(
            f"{k}={int(v)}" for k, v in observed_stats.items()
        ) or "(none)"
        codex_summary = ", ".join(
            f"{k}={int(v)}" for k, v in entry.stats.items()
            if isinstance(v, (int, float)) and k in _QUALITY_PRIORITY_STATS
        ) or "(no priority stats on codex page)"
        await msg.reply_text(
            f"<b>{html.escape(entry.name)}</b>: couldn't determine quality.\n"
            "None of the priority stats (Attack, Magic, Resistance, Defense, "
            "HP, Mana, Dexterity) were present in both your screenshot and "
            "the codex with non-zero values.\n\n"
            f"<i>Read from screenshot:</i> {html.escape(obs_summary)}\n"
            f"<i>Codex base:</i> {html.escape(codex_summary)}",
            parse_mode="HTML",
        )
        return

    # is_quality_calc=True tells get_assess_result to trust inp.quality and
    # just project the stat values across upgrade levels — no reverse-engineering.
    inp = AssessInput(
        entry=entry,
        level=item_level,
        boss_scaling=entry.boss_scaling,
        quality=quality,
        stats={},  # ignored when is_quality_calc=True
    )
    result = get_assess_result(inp, is_quality_calc=True)
    if result is None or result.levels == 0:
        await msg.reply_text(
            f"<b>{html.escape(entry.name)}</b>: this item type isn't assessable "
            "(it's likely a non-scaling material or useable).",
            parse_mode="HTML",
        )
        return

    reply = _format_response(
        entry, result, source_url, observed_stats, item_level,
        quality_signal=(picked_key, picked_obs, picked_base),
    )
    await msg.reply_text(reply, parse_mode="HTML", disable_web_page_preview=True)


def _stash_pending(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    stats: Dict[str, float],
    level: int,
    lang: str,
    ocr_name: Optional[str],
    ocr_text: str,
) -> None:
    """Save screenshot-derived data so a follow-up text message can finish the job."""
    context.user_data[_PENDING_KEY] = {
        "stats": dict(stats),
        "level": level,
        "lang": lang,
        "ocr_name": ocr_name,
        "ocr_text": ocr_text[:600],
    }


async def assess_item_screenshot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """
    Entry point. OCR the screenshot, look it up in the codex.

    Returns:
        ConversationHandler.END if the assessment was completed (or unrecoverable
        error), or AWAITING_NAME if we need the user to type the item name.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    logger.info("assess_item from chat_id=%s user_id=%s", chat_id, user_id)

    msg = update.effective_message
    if msg is None:
        return ConversationHandler.END

    # ---- 1. Pick the image: photo or image-document --------------------------
    file_id: Optional[str] = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif (
        msg.document
        and msg.document.mime_type
        and msg.document.mime_type.startswith("image/")
    ):
        file_id = msg.document.file_id

    if file_id is None:
        await msg.reply_text(
            "Send me a screenshot of your Orna item (as a photo or image file) "
            "and I'll tell you its quality and project upgrade stats."
        )
        return ConversationHandler.END

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # ---- 2. Download --------------------------------------------------------
    try:
        tg_file = await context.bot.get_file(file_id)
        img_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.exception("telegram download failed")
        await msg.reply_text(f"Couldn't download your image: {e}")
        return ConversationHandler.END

    # ---- 3. OCR (blocking → run in a worker thread) -------------------------
    try:
        ocr_text = await asyncio.to_thread(_ocr, img_bytes)
    except pytesseract.TesseractNotFoundError:
        logger.exception("tesseract not installed or not on PATH")
        await msg.reply_text(
            "OCR engine (tesseract) not found on this server.\n"
            "  Linux:   sudo apt install tesseract-ocr tesseract-ocr-ukr\n"
            "  macOS:   brew install tesseract tesseract-lang\n\n"
            "If it's installed but only works when running the bot from a "
            "terminal, your service environment is missing PATH. The bot "
            "tries /opt/homebrew/bin and /usr/local/bin automatically — "
            "if neither matches, set `pytesseract.pytesseract.tesseract_cmd` "
            "to the absolute path of the binary, or add its directory to "
            "PATH in your launchd plist."
        )
        return ConversationHandler.END
    except Exception as e:
        logger.exception("ocr failed")
        await msg.reply_text(f"OCR failed: {e}")
        return ConversationHandler.END

    # Console-only diagnostics. Visible in the bot's stdout / launchd logs;
    # never sent to the end user.
    _log_ocr_dump(chat_id, user_id, ocr_text)

    # ---- 3b. Guild "offerings" screen? Different screen, different pipeline. -
    # (progress bars of "<have> / <need> <material>" toward a guild goal,
    # rather than a single item's stats) — detect by its header and, if
    # found, hand off entirely instead of trying to parse it as an item.
    if looks_like_offerings_screen(ocr_text):
        try:
            blocks = await build_offerings_report(ocr_text)
        except Exception as e:
            logger.exception("offerings report failed")
            await msg.reply_text(f"Не вдалося обробити пожертви: {e}")
            return ConversationHandler.END
        if blocks is None:
            await msg.reply_text(
                "Побачив «NEEDED OFFERINGS», але не зміг розпізнати жодного рядка ресурсу."
            )
            return ConversationHandler.END
        await send_report_blocks(msg, blocks)
        return ConversationHandler.END

    # Anguish-eligible items show a second "alternate stats" block below the
    # default one. We only want the default block for quality assessment.
    stats_text = _strip_anguish_alternate_block(ocr_text)
    if stats_text != ocr_text:
        logger.info("stripped Anguish-mode alternate stat block from OCR text")

    # ---- 4. Parse OCR text into name / level / stats / language --------------
    item_name = _extract_name(ocr_text)
    observed_stats = _extract_stats(stats_text)
    item_level = _extract_level(ocr_text)
    lang = _detect_language(ocr_text)
    cy = sum(1 for c in ocr_text if "\u0400" <= c <= "\u04FF")
    la = sum(1 for c in ocr_text if c.isascii() and c.isalpha())
    logger.info(
        "PARSED: name=%r level=%s lang=%s (cyrillic=%d latin=%d) stats=%s",
        item_name, item_level, lang, cy, la, observed_stats,
    )

    # If we couldn't parse any stats, OCR probably failed badly. Asking for the
    # name won't help in that case — bail out and ask for a better screenshot.
    if not observed_stats:
        logger.warning("no stats parsed from OCR — bailing out")
        await msg.reply_text(
            (f"Found <b>{html.escape(item_name)}</b>, but " if item_name else "")
            + "couldn't parse any stats. Make sure the stat panel is visible "
            "and try again.\n\n"
            f"<i>OCR text:</i>\n<pre>{html.escape(ocr_text[:600])}</pre>",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # ---- 5. Codex lookup (blocking I/O → thread) ----------------------------
    entry: Optional[CodexEntry] = None
    source_url: Optional[str] = None
    if item_name:
        from orna_codex import _name_candidates  # for logging only
        logger.info(
            "CODEX SEARCH: name=%r lang=%s candidates=%s",
            item_name, lang, _name_candidates(item_name),
        )
        try:
            entry, source_url = await asyncio.to_thread(
                lookup_by_name, item_name, lang
            )
        except Exception as e:
            logger.exception("codex lookup failed")
            await msg.reply_text(
                f"Codex lookup failed: {html.escape(str(e))}",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        if entry is not None:
            logger.info(
                "CODEX HIT: %r url=%s flags=%s codex_stats=%s",
                entry.name, source_url,
                {
                    "celestial": entry.is_celestial_weapon,
                    "two_handed": entry.is_two_handed,
                    "accessory": entry.is_accessory,
                    "adornment": entry.is_adornment,
                    "boss_scaling": entry.boss_scaling,
                },
                entry.stats,
            )
        else:
            logger.info("CODEX MISS: no entry found for any candidate")
    else:
        logger.warning("no item name extracted — skipping codex search")

    # ---- 5a. If lookup failed, hand off to the manual-name flow -------------
    if entry is None or source_url is None:
        _stash_pending(
            context,
            stats=observed_stats, level=item_level, lang=lang,
            ocr_name=item_name, ocr_text=ocr_text,
        )
        observed_summary = ", ".join(
            f"{k}={int(v)}" for k, v in observed_stats.items() if v
        ) or "(none above 0)"
        if item_name:
            prompt = (
                f"Couldn't find <b>{html.escape(item_name)}</b> in the codex. "
                "OCR may have misread the name.\n\n"
                f"Detected stats: <i>{html.escape(observed_summary)}</i> "
                f"at level {item_level}.\n\n"
                "Reply with the correct item name (or send /cancel to abort)."
            )
        else:
            prompt = (
                "Couldn't read the item name from that screenshot, but I got "
                f"the stats: <i>{html.escape(observed_summary)}</i> "
                f"at level {item_level}.\n\n"
                "Reply with the item name (or send /cancel to abort)."
            )
        await msg.reply_text(prompt, parse_mode="HTML")
        return AWAITING_NAME

    # ---- 6. Got a codex hit — run the assessment and end --------------------
    await _run_assessment_and_reply(
        msg, entry, source_url, observed_stats, item_level
    )
    return ConversationHandler.END


async def receive_item_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """
    Follow-up handler in AWAITING_NAME state. The user typed the item's name;
    re-run the codex lookup with that name and the previously-saved stats.
    """
    msg = update.effective_message
    if msg is None or not msg.text:
        return AWAITING_NAME

    pending = context.user_data.get(_PENDING_KEY)
    if not pending:
        # Defensive: shouldn't get here without pending data
        return ConversationHandler.END

    item_name = msg.text.strip()
    if not item_name:
        await msg.reply_text("Item name can't be empty. Try again, or /cancel.")
        return AWAITING_NAME
    if len(item_name) > 100:
        await msg.reply_text("That looks too long for an item name. Try again, or /cancel.")
        return AWAITING_NAME

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # The user might have typed the name in a different language than what
    # OCR detected (or OCR misdetected). Re-detect language from the typed
    # name and override the saved value so we hit the correct codex.
    typed_lang = _detect_language(item_name)
    if typed_lang != pending["lang"]:
        logger.info(
            "language re-detected from typed name: %s -> %s",
            pending["lang"], typed_lang,
        )
        pending["lang"] = typed_lang
        # Persist the change so a retry in this same state uses it too
        context.user_data[_PENDING_KEY] = pending

    logger.info(
        "receive_item_name from chat_id=%s user_id=%s name=%r lang=%s",
        chat_id, user_id, item_name, pending["lang"],
    )

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        entry, source_url = await asyncio.to_thread(
            lookup_by_name, item_name, pending["lang"]
        )
    except Exception as e:
        logger.exception("codex lookup failed (followup)")
        await msg.reply_text(
            f"Codex lookup failed: {html.escape(str(e))}\n"
            "Try again, or /cancel.",
            parse_mode="HTML",
        )
        return AWAITING_NAME

    if entry is None or source_url is None:
        # Surface the variants we attempted so the user can see we DID
        # strip prefixes, and can correct accordingly.
        from orna_codex import _name_candidates  # local import: private API
        tried = ", ".join(html.escape(c) for c in _name_candidates(item_name))
        await msg.reply_text(
            f"Still no codex match for <b>{html.escape(item_name)}</b>.\n"
            f"<i>Tried:</i> {tried}\n\n"
            "Try a different spelling — usually the base item name without "
            "quality / enchantment prefixes works best (e.g. <code>Beguiled "
            "Axe X</code> instead of <code>Legendary Electric Beguiled Axe X</code>).\n"
            "Or send /cancel.",
            parse_mode="HTML",
        )
        return AWAITING_NAME

    # Got it — clear the saved state and finish the assessment.
    context.user_data.pop(_PENDING_KEY, None)
    await _run_assessment_and_reply(
        msg, entry, source_url, pending["stats"], pending["level"]
    )
    return ConversationHandler.END


async def cancel_pending_assessment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Discard any pending screenshot data and exit the conversation."""
    context.user_data.pop(_PENDING_KEY, None)
    msg = update.effective_message
    if msg:
        await msg.reply_text("Cancelled. Send a new screenshot anytime.")
    return ConversationHandler.END


def build_assess_conversation() -> ConversationHandler:
    """
    Assemble the ConversationHandler that drives the screenshot → assessment
    flow, including the manual-name fallback when OCR misreads the item name.

    Register with:
        application.add_handler(build_assess_conversation())

    Behaviour:
      * Photo / image-document     -> entry point, runs OCR + codex lookup.
      * Codex hit                  -> reply with assessment table, end.
      * Codex miss / no name       -> ask for the name, enter AWAITING_NAME.
      * In AWAITING_NAME:
          - text message           -> retry codex with that name as the item.
          - new photo              -> abandon previous, start over.
          - /cancel                -> drop saved data, end.
    """
    photo_filter = filters.PHOTO | filters.Document.IMAGE

    return ConversationHandler(
        entry_points=[
            MessageHandler(photo_filter, assess_item_screenshot),
        ],
        states={
            AWAITING_NAME: [
                # Plain text -> treat as the item's name
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_item_name
                ),
                # New screenshot -> restart the flow
                MessageHandler(photo_filter, assess_item_screenshot),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_pending_assessment),
        ],
        # Per-user, per-chat state isolation (default in PTB).
    )
