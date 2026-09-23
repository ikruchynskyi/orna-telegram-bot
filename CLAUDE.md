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
  Three calls: extract resource names from free text, extract quantities
  from a reply, and route/translate a free-text `/orna` request. All three
  set `"think": True` — see the note below on why that isn't optional.
- `orna_sheets.py` — Google Sheets access for the Material Forecast tab.
- `orna_codex.py` — two layers over playorna.com's codex: the original
  item-specific scraper (stats for assess, material tier/rarity for proof
  pricing), and `fetch_codex_json`/`codex_search`, a general-purpose reader
  for *any* codex page — see the `/orna` section below.
- `orna_aussies.py` — a *different* Orna data source: aussiescodex.com's
  own bulk `codex.json`/`translations.en.json` dump (committed to the
  repo, see the `/orna` section for why), which (unlike playorna's
  per-page JSON) exposes every item/monster/etc.'s buffs, debuffs,
  immunities, and description as short internal codes/plain text plus a
  flat code→human-name table. `query_records` is a generic multi-attribute
  evaluator (numeric stat thresholds, description/name substrings, effect
  codes, flat attributes, combined with AND/OR) — the only way to answer
  "mag > 250 and crit > 3%" or "what gives immunity to X" as a real search
  instead of guessing. Also cached to disk (`.aussies_cache/`), 24h TTL.
  See the `/orna` section.
- `telegram_orna.py` — `/orna <text>`, a unified natural-language entry
  point that routes into today's-resources / when's-it-available / codex
  browsing / effect search. See its own section below.
- `telegram_remind.py` — hidden `/remind` command (same allowlist as
  `/go`): schedules a one-off reminder via PTB's `JobQueue`, persisted to
  `reminders.json` so it survives the frequent `launchctl` reloads this
  repo's development involves.
- `orna_assess.py` — pure math: upgrade-projection from OCR'd stats.
- `orna_proofs.py` — pure math: guild-proof pricing, ported from
  OrnaCodex's `ProofView.vue`. See the docstring for the formula.
- `orna_calendar.py` — builds Google Calendar "add event" prefill links.
  All-day events by design — the exact daily shop-reset time and the
  user's timezone are both unknown, so a timed event would just be wrong.
- `orna_material_names_uk.py` / `.json` / `orna_scrape_material_names.py` —
  static EN↔UK material name table + the script that generates it.
- `telegram_go.py` — hidden `/go` command, deliberately unrelated to Orna.
  See its own section below; it's the most complex module in the repo and
  most of its design is a direct response to bugs found the hard way.

## The hidden `/go` command (`telegram_go.py`)

Personal-use feature, not for the Orna users this bot otherwise serves:
`/go [nsfw] <request>` runs a small ReAct loop (search / youtube / open /
ask / finish) against an LLM, built around the "only Telegram works, wifi
is slow and metered" case (an actual plane-wifi use case, not hypothetical).
It's deliberately never registered via `setMyCommands`, so it doesn't
appear in any command menu, and `GO_ALLOWED_USER_IDS` gates it to specific
Telegram user ids — anyone else's `/go` is silently ignored (no "not
authorized" reply, since that reply would itself confirm the command
exists).

**Two model backends, picked per-request, never mixed mid-conversation.**
Normal mode: Ollama Cloud (`GO_MODEL`, default `gemma4:31b` — confirmed via
`/api/show` to genuinely support vision, tools, and thinking), falling back
to the same local Ollama the Orna NLP features use (`telegram_nlp.
OLLAMA_MODEL`, `gpt-oss:20b` — text-only, no vision) if the cloud call
fails for any reason. `/go nsfw ...` never touches the cloud at all: it
always uses a local model (`NSFW_MODEL`, an uncensored GGUF) and DuckDuckGo
instead of Tavily, since a hosted service would likely refuse this content
outright. Before assuming any given Ollama Cloud model's capabilities,
check for real via `POST https://ollama.com/api/show {"model": "..."}`
(with the same bearer token) rather than guessing from the name — it
returns a `capabilities` list (`vision`, `tools`, `thinking`, ...) plus
param count, and has already caught one wrong assumption during
development (see the multimodal note below).

**The video pipeline re-encodes unconditionally; it does not trust
yt-dlp's own merge.** `_download_source` grabs whatever yt-dlp can get
(any codec, any container) up to a generous size backstop, and
`_fit_to_size` always re-encodes to H.264/AAC via a direct `ffmpeg` call
with a bitrate computed from the actual duration to hit `MAX_VIDEO_MB`,
stepping down a fixed resolution ladder (360p→240p→144p) only as far as
needed. This replaced three earlier designs that each seemed reasonable
and each broke in a different way in production:
  1. Filtering yt-dlp's own format selector to `height<=N` and trusting
     whatever it merged — broke when a video's only sub-360p option was
     VP9, which plays audio-only on most phone players once muxed into an
     `.mp4` (VP9-in-MP4 is technically valid but poorly supported).
  2. Using `--max-filesize` as the real size cap — it's checked per
     *fragment* (the video-only and audio-only streams independently)
     before merging, not on the merged result. On a longer video, whichever
     fragment is bigger can quietly get dropped while yt-dlp still exits 0,
     silently leaving a merge that's actually just one lone track. This is
     why the real cap lives entirely in `_fit_to_size`'s post-encode size
     check now, and `--max-filesize` in `_download_source` is just a
     generous backstop (`FRAGMENT_SAFETY_MB`) against pathological cases.
  3. The subtlest one: **yt-dlp needs its own `ffmpeg` to do the bv+ba
     merge, found via `PATH` unless `--ffmpeg-location` is passed
     explicitly.** Under launchd (see the deployment note below), `PATH`
     is minimal and doesn't include `/opt/homebrew/bin` — so without
     `--ffmpeg-location`, yt-dlp silently left the two fragments unmerged
     (still exit code 0) and `_download_source`'s file-selection logic
     would pick whichever fragment happened to be bigger as "the"
     download. This produced several rounds of "video but no audio" /
     "audio but no video" bug reports that looked unrelated until the
     actual yt-dlp/ffmpeg output was logged (see below) and the pattern
     became obvious. Every yt-dlp invocation in this file passes
     `--ffmpeg-location` now; don't drop it.
  Because of #3, `_download_source` also refuses to pick a file whose name
  still carries yt-dlp's per-fragment suffix (`<id>.f<format>.<ext>`) — a
  real merge always produces a clean `<id>.<ext>` — rather than trusting
  "biggest file in the directory" alone.

**Log the actual subprocess output, not just the final exception
message.** Early rounds of the video pipeline only surfaced failures as a
brief Telegram message, never server-side — every real bug above was
eventually found by adding `logger.info`/`logger.warning` with the full
yt-dlp/ffmpeg stdout+stderr tail at each step (`_download_source`,
`_fit_to_size`, `_has_audio`, `_probe_duration`), then reproducing the
exact failing video directly via a throwaway `asyncio.run(...)` script.
Guessing from the user-facing error text alone repeatedly pointed at the
wrong cause; the log almost always didn't.

**Deployment: the launchd plist runs from this repo, not a separate
copy.** `~/Library/LaunchAgents/com.username.telegrambot.plist` points at
`orna-telegram-bot/telegram_bot.py` with this directory as
`WorkingDirectory`, loading `orna-telegram-bot/.env` (gitignored, not the
one in `~`). This used to not be true — an earlier, stale flat copy in
`~/telegram_bot.py` was what launchd actually ran, silently missing every
`/go`-related change until that was noticed and fixed. Reload after any
change with `launchctl unload/load` on that plist, and note launchd's
`PATH` is minimal (see the `--ffmpeg-location` point above) — never assume
a Homebrew binary is reachable by bare name from code that runs as this
service; use the absolute path (`FFMPEG_PATH`/`FFPROBE_PATH`/`YTDLP_PATH`
env vars, defaulted to absolute paths already).

**The "ask" ReAct action only offers tappable options, never free text —
on purpose.** Routing a model's clarifying question through free-text
reply would need a general-purpose text `MessageHandler`, and this repo's
other conversations (`telegram_assess`, `telegram_resources`) already
depend on fragile registration-order-based text handling (see the handler
ordering note above). Buttons avoid that risk entirely. The one place
`/go` *does* accept free text/photo again is the "Continue" button
(`_PENDING_CONTINUE`), and that was made safe the same way: a custom
`filters.MessageFilter` that only matches a chat with an active,
unexpired pending-continue flag, registered before the Orna conversation
handlers — for every chat that hasn't just tapped Continue, it's a
guaranteed no-op and falls straight through, so it can't interfere with
the assess/resources flows no matter what.

**Multimodal continuation degrades gracefully, because not every
configured model supports it.** `/go`'s "Continue" button lets a photo be
attached to a follow-up message; verified directly (see the `/api/show`
note above) that `GO_MODEL` supports vision for real, but the two local
fallbacks (`gpt-oss:20b`, `NSFW_MODEL`) don't. Ollama returns a clean
`400 "does not support multimodal requests"` for those rather than
crashing — `_call_model` detects that specific error, strips the image
from the message in place (so `session.messages` doesn't keep re-sending
a doomed image on every later turn), notes it in that message's text, and
retries once. No per-model capability table to maintain; it just asks
Ollama and reacts to what comes back.

## The `/orna` command (`telegram_orna.py`)

Unlike `/go`, this is a real, visible feature for the Orna users the bot
otherwise serves — introduced *alongside* the existing `/res_today`,
`/res_next`, `/need`, and the free-text auto-detect flow rather than
replacing them (a deliberate choice made with the user: zero risk to what
guild members already rely on while `/orna` is proven out; the old
commands can be retired later).

**One LLM call routes and translates in a single round trip, then the LLM
is out of the picture entirely.** `telegram_nlp.route_query` classifies a
free-text message (English or Ukrainian) into `"today"` / `"next"` /
`"codex"` and translates the relevant part to English, all in one prompt —
this only became reliable after the `"think": True` fix above; the
combined classify-and-translate ask was actually the *first* thing that
surfaced the flakiness during development, and got no more reliable from
simplifying the prompt or splitting it into separate calls until the real
cause (`think: False`) was found and fixed. `"today"` and `"next"` reuse
the exact same Google Sheet data `/res_today`/`/res_next` already serve;
`"next"` falls through to codex search if the named thing isn't a known
Material Forecast material (a boss, a non-shop item — codex search's own
"no results" is a better dead end than a hard failure).

**Every codex page - regardless of category - is one universal JSON
schema, no per-category parsing needed.** Confirmed by fetching real pages
across every category (items, classes, monsters, bosses, followers, raids,
spells, buildings, dungeons): each embeds a `<script id="codex-bootstrap"
type="application/json">` blob with `detail: {name, description, sprite,
facts: [{label, value}], effects: [str], tags: [str], sections: [{title,
entries: [{category, name, url, tier, rarity, ...}]}]}` for a single
entry, or `results: [...]` in the same per-entry shape for a search/
listing page. `orna_codex.fetch_codex_json`/`codex_search` just extract
and return this as-is — `telegram_orna.py` is purely a rendering/
navigation layer over already-structured data, exactly the split the user
asked for (LLM only for routing, Telegram as the UI for the actual codex).
`sections[].entries[].url` is the site's own cross-link graph (an item's
"Dropped by" monster, its "Upgrade materials", a monster's "Skills", ...)
and can point at a different category than the current page — that's
what makes drilling from an item into the monster that drops it, then
into that monster's own skills, "just work" with the same two functions
recursively.

**Navigation sends new messages, never edits in place — except paging
through one result list.** Same pattern as `telegram_go.py`: tapping a
button to view an entry or drill into a section replies with a *new*
message rather than editing the current one, so Telegram's own scrollback
becomes the browsing history for free — no back-button, no navigation
stack to maintain. Only "next/prev page" of a single search-results list
edits the existing message's keyboard in place, since that's genuinely
the same list, not a new one.

**Generic multi-attribute query ("mag > 250 and crit > 3%", "what gives
immunity to stunned", "items with 'dragon' in the description") is a
two-step LLM pipeline into one generic evaluator, not per-query-shape
code.** `telegram_nlp.route_query` first classifies + translates (as
before); when it returns intent `"query"`, a *second*, separately-focused
call (`parse_conditions`) turns the already-English text into a flat list
of typed conditions (`kind`: `"stat"` for a numeric threshold, `"effect"`
for immunity/causes/gives, `"text"` for a description/name substring,
`"attr"` for a flat field like rarity/tier), plus an `"and"`/`"or"`
combinator and an optional category. Splitting classification from
condition-extraction into two focused prompts was a deliberate choice —
one mega-prompt doing both proved less reliable during development, the
same lesson as the `"think"` fix above but about prompt *scope* rather
than a request parameter. `orna_aussies.query_records` then evaluates
every condition against every record with `_eval_condition` and combines
with `all`/`any` — a single generic evaluator, not bespoke code per query
shape, so a new `kind` is the only thing a new query type needs.
`resolve_codes` (used by `kind: "effect"` conditions) first tries a small
rule-based parser for the team/stat/direction/magnitude pattern
(`_parse_buff_query` — e.g. "T Mag 3" → `t__mag_uuu`) before falling back
to fuzzy string matching against `translations.en.json`'s ~220 simple
status names ("stunned", "paralyzed", ...). **Team and non-team tiers are
genuinely asymmetric in the real game data** — e.g. non-team "Att Down"
only goes to tier 1, but "T. Att Down" goes to tier 3 — so the
valid-tiers cache (`_build_stem_directions`) keys on `(team, stat)`, not
just `stat`; an earlier version merged them into one set per stat and
silently offered a non-team tier that doesn't exist. Verified directly
against the live data before and after that fix, not just by reading the
code. Results are capped at 50 (`query_records`'s `limit`) since a single
loose condition like "mag > 250" alone can match hundreds of records.

**Query results use the exact same rich rendering as a name search -
stats/facts/sections in chat, not just a link out.** First version made
query results plain `url=` link buttons straight to aussiescodex.com,
skipping the fetch+render entirely; reverted on the same day, per
explicit feedback, once it was clear having the stats actually visible in
the chat (not just a link to tap through to) was the valuable part.
`_run_query_search` now builds playorna-shaped entries and reuses
`_result_list_keyboard`/`_send_entry` exactly like a "codex" name search
does. aussiescodex only earns a place as a single **"📊 Assess"** link
button on the entry view (`orna_aussies.has_aussies_page` gates it - only
4 of 9 categories have a page there, its URL segment for spells is
`orna-skills` not `orna-spells`, both verified by checking a real page's
own outbound links, not guessed) - playorna's own codex has no upgrade/
assess calculator, aussiescodex does, so that's the one thing worth
sending the user there for.

**aussiescodex.com's `codex.json`/`translations.en.json` are fetched
dynamically and cached to disk, gitignored (`.aussies_cache/`), with a
1-week TTL.** Briefly committed these to the repo instead in an earlier
round of this work, per an explicit ask - reverted back to gitignored
dynamic fetch on a follow-up ask in the same conversation, since a
week-long TTL keeps them close enough to current without needing to
re-commit ~3MB of upstream data on every game patch. If this changes
again, update both `CACHE_TTL_SECONDS` in `orna_aussies.py` and this
note together.

**A `kind: "stat"` condition's `field` is NOT restricted to a fixed
enum - `translations.en.json`'s `stats` dict (~155 keys) is the real
vocabulary, and it's much wider than the obvious `hp`/`attack`/`magic`/
etc.: things like `follower_stats`, `summon_stats`, `crit_damage`
(distinct from `crit`/`crit_chance`), `view_distance`, `multi-target_damage`
all live there too.** An earlier version of `parse_conditions`'s prompt
hardcoded a 10-field enum, which silently broke any query for a stat
outside that list (reported live: "follower stats > 10%" → "nothing
found", even though 19 items actually have it). The fix has two halves
that both matter: the prompt now tells the model to infer any reasonable
snake_case field name instead of picking from a closed list, **and**
`orna_aussies._eval_condition`'s `"stat"` branch fuzzy-resolves whatever
field name comes back (`_resolve_stat_field`, exact-normalized match
first, then `difflib.get_close_matches` against `translations['stats']`
keys) - so small drift like singular/plural or an extra space still
lands on the right key. Verified against live data (`follower_stats`,
`crit_damage` vs `crit`, negative thresholds like `defense < -50` - all
real in the data) and against 3 repeated real-model calls per phrasing
before deploying, same as every other prompt change this session.

**"What lowers X" is ambiguous between an effect (a debuff that reduces
a stat) and a stat condition (an item whose own stat is negative) - the
prompt disambiguates by whether the ask targets an enemy/buff-by-name
vs the item's own numbers.** "What lowers enemy defense" → `kind:
"effect"`, `field: "causes"`, value `"Def Down"` (routes through the
existing effect-code resolver, which already has `def_d`/`def_dd` etc.
in `translations['status']`). "Items with negative defense" → `kind:
"stat"`, `field: "defense"`, `cmp: "<"`, `value: 0` - `_parse_number`
already handled negative values fine (`"-130"` → `-130.0`), the gap was
purely that the prompt never told the model negative stat values were a
legitimate thing to ask for.

**Ranking queries ("the item with the biggest mag", "weakest defense
follower") are a `sort_by`/`sort_dir` pair on `query_records`, not a new
condition kind.** `parse_conditions` now returns `sort_by`/`sort_dir`
alongside `conditions` - conditions can be empty when the ask is pure
ranking with no other filter. `query_records` resolves `sort_by`
through the same fuzzy field resolver as a stat condition, drops records
missing that stat entirely (nothing to rank them by), sorts, then slices
by `offset`/`limit`. `EffectMatch.sort_value` carries the formatted
number through to the results list UI so the ranked value is visible
without opening each entry (`(410)` next to the item name, replacing the
tier star for that result set). Verified: top-5 magic items, bottom-5
defense items, and offset-by-1 all checked directly against live data,
plus repeated real-model calls confirming `sort_by`/`sort_dir` come back
correctly for several phrasings including one that combines a filter
condition with a sort.

**A codex name search that turns up nothing falls back to an
aussiescodex description-substring search before giving up** (`
_run_codex_search`, after the existing "rainsong" space-collapse retry).
Covers requests like "strange sword" that only match an item's
*description* ("Bladeless"'s), not its name - `route_query` naturally
sends these down the name-lookup ("codex") path since there's no
stat/effect/attribute language to trigger "query" intent, so the name
search has to be the one that recovers rather than trying to get the
classifier prompt to somehow guess this belongs to the other path.

## Things that aren't obvious from reading one file at a time

**Handler registration order is load-bearing.** `telegram_bot.py` registers
`build_assess_conversation()` before `build_resource_conversation()`. Both
are `ConversationHandler`s and PTB only runs the first one in a group whose
`check_update` matches. The assess conversation's `AWAITING_NAME` state
photo/text handlers only match when *that* conversation is actually active
for the chat; when it isn't, it correctly falls through to the resources
conversation. Don't reorder these without re-checking that interaction.

**`"think": False` was the actual cause of `gpt-oss:20b`'s flakiness, not
the model itself.** This section used to warn that the same input to
`telegram_nlp.extract_resources` could return the right answer on one call
and an empty list on the next. Root-caused during the `/orna` work
(2026-09-22): with `"think": False` in the `/api/chat` payload, this
model/quantization returns empty or truncated-mid-reasoning content
instead of the requested JSON *close to 100% of the time* under repeated
testing - not occasional flakiness, a near-total failure rate. Switching to
`"think": True` (now the default in `telegram_nlp._chat_json_once`) was
100% reliable across the same repeated tests, including free-text
Ukrainian input: Ollama separates the reasoning out on its own and
`content` comes back as clean JSON. The tradeoff is a bit more latency per
call (the model actually thinks now), which has been an acceptable trade
so far. `orna_material_names_uk.json`'s static EN↔UK table is used by
`telegram_offerings.py` specifically (OCR'd offerings-screen rows), not by
`extract_resources` — don't assume it's a universal fallback underneath
every LLM call in this repo. The general principle still holds even with
the fix: prefer a deterministic lookup wherever one is feasible, and keep
the LLM for genuinely fuzzy natural-language parsing (`telegram_orna.py`'s
routing is a good example of leaning on it appropriately once it was
actually reliable).

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

`telegram_go.py` changes follow the same no-mocks philosophy, one level
further: call its internal functions directly (`_download_youtube`,
`_run_search`, `_call_model`, ...) against real YouTube/DDG/Ollama in a
throwaway script before ever touching the live bot, especially for the
video pipeline — every real bug in it turned out to be video-specific
(a particular ID's format ladder, a particular fragment getting dropped),
never reproducible in the abstract. After confirming a fix works standalone,
reload the live service (`launchctl unload` then `load` on
`~/Library/LaunchAgents/com.username.telegrambot.plist`) and check
`orna-telegram-bot/telegrambot_error.log` for the `go:`-prefixed lines
`telegram_go.py` logs at each pipeline step — they carry the actual
yt-dlp/ffmpeg output, not just the final error message the user saw.
