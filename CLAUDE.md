# CLAUDE.md

Guidance for Claude Code (or any future contributor) working in this repo.
See `README.md` for user-facing setup/usage — this file is about the code
itself: conventions, gotchas, and why things are structured the way they are.

## Stack

Plain Python 3, `python-telegram-bot` (async, `ConversationHandler`-based),
no framework, no test suite, no build step. Run directly:
`python telegram_bot.py`. There is no CI — changes are verified by running
small `asyncio.run(...)` snippets against the live Google Sheet / codex /
Ollama (see "Verifying changes" below), since most of the logic is thin
glue around three live, unmocked external services.

## Module map

- `telegram_bot.py` — entry point, registers handlers, `app.run_polling()`.
- `telegram_assess.py` — screenshot OCR pipeline for single-item stat
  screens (quality/upgrade projection). Its photo handler is also where
  offerings-screen screenshots get detected and handed off.
- `telegram_offerings.py` — parses the altar's "NEEDED OFFERINGS" screen
  (`<have> / <need> <material>` rows), computes shortfalls, and reuses
  `telegram_resources.build_report`.
- `telegram_resources.py` — the natural-language "what do I need"
  `ConversationHandler`, and the shared report builder (`build_report`,
  `send_report_blocks`) both this and `telegram_offerings.py` use.
- `telegram_nlp.py` — Ollama REST client (`/api/chat`, `format="json"`).
  Two calls: extract resource names from free text, extract quantities from
  a reply.
- `orna_sheets.py` — Google Sheets access for the Material Forecast tab.
- `orna_codex.py` — scrapes playorna.com codex pages: item stats (for
  assess) and material tier/rarity (for proof pricing).
- `orna_assess.py` — pure math: upgrade-projection from OCR'd stats.
- `orna_proofs.py` — pure math: guild-proof pricing, ported from
  OrnaCodex's `ProofView.vue`. See the docstring for the formula.
- `orna_calendar.py` — builds Google Calendar "add event" prefill links.
  All-day events by design — the exact daily shop-reset time and the
  user's timezone are both unknown, so a timed event would just be wrong.
- `orna_material_names_uk.py` / `.json` / `orna_scrape_material_names.py` —
  static EN↔UK material name table + the script that generates it.

## Things that aren't obvious from reading one file at a time

**Handler registration order is load-bearing.** `telegram_bot.py` registers
`build_assess_conversation()` before `build_resource_conversation()`. Both
are `ConversationHandler`s and PTB only runs the first one in a group whose
`check_update` matches. The assess conversation's `AWAITING_NAME` state
photo/text handlers only match when *that* conversation is actually active
for the chat; when it isn't, it correctly falls through to the resources
conversation. Don't reorder these without re-checking that interaction.

**Never call the LLM as a single source of truth.** `gpt-oss:20b` (a small
local model) is measurably flaky: the same input to
`telegram_nlp.extract_resources` can return the right answer on one call
and an empty list on the next (verified by repeated identical calls during
development). This is why `orna_material_names_uk.json` exists at all —
Ukrainian material names are resolved via that static table first, with the
LLM only as a last-resort fallback for names the table doesn't cover. If
you're tempted to route more logic through the LLM, prefer a deterministic
lookup wherever one is feasible, and keep the LLM for genuinely fuzzy
natural-language parsing only.

**OCR text needs defensive parsing, not clean regexes.** Real OCR output
puts junk in front of every offerings row (a misread icon — a stray letter,
symbol, or even a bare digit) and mangles number formatting inconsistently
even within one screenshot: comma-grouped, space-grouped, a stray period
where a separator should be, or the separator vanishing entirely for a
4-digit number. See `telegram_offerings._NUM` and `_OFFERING_LINE_RE` for
the current tolerant pattern, and don't re-anchor that regex to line-start —
it's deliberately not anchored so it can skip leading junk.

**Guild order must stay in sync across two files.** `orna_sheets.GUILD_NAMES`
(column order N–W in the spreadsheet) and `orna_proofs.GUILD_PROOFS` (dict
whose insertion order pairs each guild with its proof currency + scaling
factor) both encode the same 10-guild ordering. They're independent
constants, not derived from each other — if you ever reorder one, check the
other.

**Report messages are chunked, not truncated.** `telegram_resources.
send_report_blocks` packs per-material report blocks into as few Telegram
messages as fit under ~3500 chars, splitting on block boundaries so an
`<a>`/`<b>` tag never gets cut mid-message, with a line-level fallback for a
single oversized block. This replaced an earlier design that sent a second
message with all calendar links concatenated — that could exceed Telegram's
4096-char cap and silently fail to send, which is why calendar links are
now embedded as per-row `<a href>`s in the main report instead of a
separate message.

## Verifying changes

There's no test suite. The working pattern used throughout development:

```bash
cd orna-telegram-bot
set -a && source .env && set +a
AI/venv/bin/python3 -c "
import asyncio
from telegram_resources import build_report
from orna_sheets import fetch_sheet_data

async def main():
    sheet = await fetch_sheet_data()
    blocks = await build_report({'Adamantine': 500}, sheet)
    print(blocks[0])

asyncio.run(main())
"
```

This hits the real Google Sheet, the real codex, and (for
`telegram_nlp`/`telegram_offerings` changes) the real local Ollama — there's
no mocking layer. When debugging an OCR-related report, the actual OCR
output for a real screenshot is logged (never sent to the user) via
`telegram_assess._log_ocr_dump` — check the bot's log for `OCR DUMP` blocks
rather than guessing at OCR formatting.
