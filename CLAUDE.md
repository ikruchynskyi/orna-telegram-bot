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
  repo's development involves. `schedule_reminder` is a separate public,
  UNGATED entry point onto the same scheduling/persistence machinery -
  `telegram_resources.py`'s "remind me when this resource lands" buttons
  use it directly, without going through the gated command.
- `usage_stats.py` — usage counters (questions per command, LLM calls per
  model), persisted to `usage_stats.json` for the same reload-survival
  reason as `reminders.json`. Viewed via the hidden `/stats` command
  (`telegram_bot.py`, same allowlist as `/go`). See the note below.
- `orna_assess.py` — pure math: upgrade-projection from OCR'd stats.
- `orna_proofs.py` — pure math: guild-proof pricing, ported from
  OrnaCodex's `ProofView.vue`. See the docstring for the formula.
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

**Model-authored replies go through `_markdown_to_html` + Telegram's HTML
parse mode - nothing tells the model to write Markdown, but it does
anyway often enough that plain `reply_text` (no `parse_mode`, the
original state of every call site in this file) was showing `**bold**`/
`# Heading`/bullet syntax completely literally instead of rendering it.**
Converts to HTML rather than MarkdownV2 for the same reason every other
HTML-rendering reply in this codebase (`telegram_orna.py`,
`telegram_resources.py`) already does: Telegram's HTML mode only needs
`<`/`>`/`&` escaped, versus MarkdownV2's much wider (and easy to get
subtly wrong) escape set. Telegram's HTML mode has no heading or list
tags, so a heading becomes its own bold line and a bullet becomes a
plain "•" - the closest real equivalent each has. Applied only to the
two call sites that actually carry model-generated prose (`_send_finish`'s
final answer, the "ask" action's clarifying question) - button labels
never get this treatment, since Telegram buttons are always plain text
regardless of parse_mode, and injecting HTML tags into one would show the
literal tags. `_reply_markdown` wraps the send in a try/except that falls
back to the original unconverted plain text if Telegram ever rejects the
generated HTML as malformed (caught `TelegramError`, not assumed
impossible) - degrading back to the original literal-asterisks bug beats
the reply failing to send at all. Verified the converter directly against
headings/bold/italic/inline-code/fenced-code-blocks/links/bullets/literal-
`<`-and-`&` samples, and the fallback path via a forced-malformed-HTML
test, before deploying.

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

**`kind: "attr"` reaches every real flat field, not a hand-picked
subset - cross-checked directly against aussiescodex.com's own advanced
item-filter sidebar (user shared a screenshot: 6 "Basic Filters" groups
+ 12 "Unique Stats" groups totaling 130 stat filters) to find what was
still missing.** `orna_aussies._all_attr_fields()` scans every record in
every category and builds the field vocabulary from what's actually
there, the same "discover, don't hardcode" approach as the stat-field
fix above, with `_resolve_attr_field` doing the same exact-then-fuzzy
lookup as `_resolve_stat_field`. `_EXCLUDED_ATTR_FIELDS` deliberately
drops cross-link fields (`drops`, `skills`, `abilities`,
`upgrade_materials`, `learned_by`, `used_by`, ...) - those are already
browsable via codex-bootstrap's own `sections`, not meaningful as a
filter value. Comparing against the screenshot surfaced four concrete
gaps, all fixed:
- **`cures`** was missing from the effect kind entirely (only
  `immunities`/`causes`/`gives` existed) - items and spells both have a
  real `cures` list (e.g. Antidote cures `poisoned`). Added as a fourth
  `_EFFECT_LIST_FIELDS` entry.
- **Boolean flag fields** (`exotic`, `new`, `hidden`) are **presence-only**
  in the source data - the key exists and is `True` on a match, and is
  simply **absent** otherwise (verified directly: 0 records anywhere have
  `"exotic": false` explicitly, out of 1388 that have the key at all out
  of 2764 items). A naive `raw is False` check for the "false" case would
  therefore match nothing - had to treat "missing key" as a match for a
  false/no query too. Verified the fix produces `1388 + 1376 = 2764`,
  i.e. every item now falls into exactly one bucket.
- **List-valued fields** (`events`, `tags`) needed membership matching,
  not the old scalar substring compare.
- **`stats.element`** is a genuinely strange one: aussiescodex encodes it
  as a list of individual characters (`"arcane"` → `['a','r','c','a',
  'n','e']`), apparently an upstream `list(str)` bug on their end. The
  attr branch detects "list of single-char strings" and rejoins before
  comparing, rather than trying to match characters one at a time. Also
  needed a stats-dict fallback in the attr branch generally, since
  `element` isn't a top-level field the way `tier`/`rarity` are.
- **`type` vs `item_type`** are two distinct real fields on items - `type`
  is the weapon/armor *subtype* (`daggers`, `curved_swords`,
  `axes_&_hammers`), `item_type` is the equipment *slot*
  (`armor`/`weapon`/`off-hand`/`field`). The prompt spells out the
  difference explicitly since the names alone don't make it obvious.

`_parse_number` also picked up comma-stripping (`"2,500_orns"` → `2500.0`)
while fixing this, since `classes.price` is comma-formatted and the old
regex fallback stopped at the first comma. All of the above verified
directly against live data first, then with 3 repeated real-model calls
per phrasing before deploying, same as every other prompt change.

**`place` (not `type`/`item_type`) is the body-slot field - live bug:
"what goes on legs" was parsed as `field:"type"`, which only ever holds a
weapon subtype and never matches "legs" at all.** Real `place` values:
`head`/`torso`/`legs`/`weapon`/`off-hand`/`accessory`/`material`/
`armor_(for_adornments)`/`augment_(for_celestial_weapons)`. The prompt's
attr field guidance now spells out all three fields side by side with an
explicit "use `place` for body-slot asks, never `type`" instruction,
since the names alone don't disambiguate them.

**Buff/debuff tier shorthand (`"T Mag ++"`, `"Def ↓↓"`, `"T Mag 3"`) has
to survive two separate steps intact, and both needed fixing.** Live bug:
"/orna what gives t.mag ++" returned 50 results including an item that
only gives tier-1 T. Mag ↑, not tier 2. Root causes, both fixed:
1. `orna_aussies._parse_buff_query` never recognized `+`/`-` run notation
   or counted repeated arrows as magnitude - it only understood a literal
   digit or roman numeral, so `"++"` silently fell back to magnitude 1.
   Now `↑↑`/`↓↓` (arrow count = magnitude) and `+`/`-` runs are parsed the
   same way, with word/digit as the remaining fallback. Fixing this also
   surfaced a real regression risk: loosening `_TEAM_RE`'s trailing
   `\s+` to `\s*` (needed so `"t.mag"`, no space, is recognized as a team
   prefix) broke `"team ..."` inputs, because the alternation
   `(?:t\.?|team)` tried the single-letter `"t"` branch first and
   matched just that, leaving a mangled `"eam attack..."` behind.
   Reordering to `(?:team|t\.?)` (longest/most-specific alternative
   first) fixed both without reintroducing the old requirement for a
   space after `t.`.
2. Even with (1) fixed, `parse_conditions` itself was only reliably
   preserving `"++"` into its `value` output about 5/8 of the time -
   the rest either invented a wrong tier number, silently dropped the
   tier, or (once) fabricated a nonexistent `"t_mag"` stat field. Added
   explicit prompt guidance: tier shorthand is always `kind:"effect"`,
   never `"stat"`, and must be copied into `value` character-for-character,
   not re-notated or guessed. Verified 8/8 after the prompt change.

**A trailing stray number on an otherwise-valid codex name falls back to
the name with the number stripped** (`_run_codex_search`, alongside the
existing "rainsong" space-collapse and description-substring fallbacks).
Live bug: "/orna solarite 12345" found nothing even though "Solarite" by
itself has 2 results - `route_query` passes the number through verbatim
since it has no way to know it's noise rather than part of the name.

**`/orna` (plus `res_today`/`res_next`/`remind`) are now registered via
`set_my_commands` in a `post_init` hook (`telegram_bot.py`), in EVERY
scope Telegram actually consults for a real chat, not just `default`.**
The first version only set `default` scope and the user still only saw
2 of the 4 commands - `get_my_commands` revealed why: `all_private_chats`/
`all_group_chats`/`all_chat_administrators` each already carried their
own older, narrower (`res_today`/`res_next` only, different Ukrainian
wording) command list from outside this repo, presumably set via
BotFather itself at some point - those more specific scopes silently
shadow `default` in Telegram's own scope-precedence rules, so it doesn't
matter what `default` has. `_post_init` now writes the same full list to
`BotCommandScopeDefault`, `AllPrivateChats`, `AllGroupChats`, and
`AllChatAdministrators` explicitly. `/go` is deliberately left out of
this list, unlike everything else.

**A live "Extra data" `JSONDecodeError` (`telegram_go.py`, both the cloud
and local-fallback paths) traced to two compounding issues, both fixed.**
1. `_chat_json`'s payload never set `"think": True` - the exact fix
   `telegram_nlp.py` already carries (see above) for reliable JSON-only
   output, just never applied to `/go`'s own separate copy of this
   helper. Without it, reasoning tokens could leak into `content`
   alongside the JSON.
2. Even with that fixed, the old fallback (`_JSON_OBJECT_RE.search`, a
   greedy `\{.*\}` regex) couldn't have recovered from this failure mode
   anyway: given valid JSON followed by trailing garbage, greedy `.*`
   matches from the first `{` all the way to the LAST `}` in the whole
   string - spanning right across the garbage instead of stopping at the
   end of the first real object, so `json.loads` on the "recovered"
   text failed with the exact same error at the exact same offset as the
   raw content (visible in the log: both tracebacks show identical
   `char 442`). Replaced with `json.JSONDecoder().raw_decode(content,
   start)`, which parses exactly one complete object starting at the
   first `{` and simply stops there. `telegram_nlp.py._chat_json_once`
   had the identical latent bug in its own fallback (hadn't manifested
   yet, but same call pattern) - hardened the same way. Verified against
   three synthetic cases (clean JSON, JSON+trailing duplicate object,
   preamble text+JSON) before deploying.

**`parse_conditions` was replaced by `plan_queries`, which can return
several independent query blocks and/or one button-only clarifying
question - deliberately NOT a full ReAct loop, after weighing it against
one for "search multiple items"/"more complex questions".** The
considered alternative was giving `/orna` the same multi-step
search/open/ask loop `telegram_go.py` already has; rejected because that
loop's reliability is carried by a stronger/cloud-fallback model and
still needs real engineering (step limits, session TTLs) to stay
sane - looping that same machinery over local Ollama, which already
needed `think: True` and multiple verification passes just for reliable
*single-shot* structured output, would multiply the flakiness across
steps and add real latency to what's usually a simple one-item lookup.
Instead `plan_queries` stays a single call (a second one only on the
rare clarify round-trip) that can fan out into multiple blocks:
- **Multi-query**: `"queries"` is normally one block, but the prompt
  allows more when the ask genuinely names several separate lookups that
  don't collapse into one AND/OR filter (e.g. "best mag item for thieves
  and for mages" → two blocks, one per class, each with its own
  `useable_by` condition + `sort_by: "magic"`). `telegram_orna._execute_queries`
  runs each block through the same `query_records`/`_result_list_keyboard`
  path as before and sends one results message per block, labeled from
  the block's own `"label"`.
- **Clarification**: modeled directly on `/go`'s `"ask"` action and the
  same reasoning - button-only, never free text, because a local model's
  own clarifying questions are exactly as unreliable as everything else
  it produces, so a free-text follow-up would just compound that
  uncertainty rather than resolve it. The prompt is deliberately
  conservative about *when* to ask (only when a missing detail would
  materially change the results and no reasonable default exists -
  "good gear for my class" asks, "legendary items" or "mag > 250" don't)
  since over-asking is its own UX cost. `clarified=True` on the
  follow-up call (after a button tap) forbids asking a second time,
  mirroring `/go`'s "don't ask more than once" rule - this is what keeps
  it a single bounded round-trip instead of needing loop/session state
  the way `/go` does.
- A real bug surfaced during verification, fixed alongside this: the
  natural class nickname a player types ("mage") often isn't a literal
  substring of the stored `useable_by` value ("magic_users" contains
  "magi", not "mage") - `_USEABLE_BY_ALIASES` in `orna_aussies.py` maps
  common nicknames (mage/mages, warrior(s), thief/thieves/rogue(s),
  summoner(s)) onto a substring that's actually present, applied only to
  the `useable_by` field specifically.

**"This item grants a spell/skill when equipped" needed a whole new
`kind:"ability"` condition - it has THREE different real encodings in
the data, none of them an "effect" (buff/debuff code).** Live report:
"/orna мені треба магу щось на голову щоб ще давало додатковий spell"
("something for a mage's head that also gives a bonus spell") returned
nothing, and the user separately confirmed they expected "Hyades Wreath"
(which grants Rainsong) to show up. Investigation in order:
1. First attempt only checked items' top-level `"ability"` field (a
   `["spells", id]` cross-link, e.g. `["spells", "focused-guard"]` on a
   weapon's own signature move) - 213 items have this. Still missed
   Hyades Wreath.
2. Direct data inspection (`json.dumps(record).lower()`, searching every
   item for "rainsong" - the same brute-force technique that cracked the
   `follower_stats` bug earlier) found the real encoding: `stats["+spell"]
   == "Rainsong"`, a plain string VALUE, not a cross-link at all. A whole
   family of similar `"+"`-prefixed stats keys exists for the same
   concept - `+spell`/`+skill` (a name the WIELDER gets), `+follower_
   summon_spell`/`+follower_summon_skill` (a name their SUMMON gets -
   deliberately excluded from `kind:"ability"`'s default scope, since
   that benefits a different beneficiary than what a plain "gives me a
   spell" ask means), and boolean-flag ones (`+weapon_proficiency`,
   `+instrument_proficiency`, `+arch-alchemy`) with no name to compare.
   `_eval_condition`'s `"ability"` branch now checks both the top-level
   cross-link AND `stats["+spell"]`/`stats["+skill"]` as one unified
   concept.
3. Fixing that still didn't surface Hyades Wreath under a class-specific
   query (`useable_by: "mage"`), because it's actually `"all_classes"` -
   a query for one specific class must also match `"all_classes"` items
   (that class genuinely can use them), which `_eval_condition`'s
   `useable_by` handling didn't do before this. Also made a record with
   no `useable_by` at all default to `"all_classes"` too (defensive - no
   real item is currently missing the field, verified directly: 0/2764).
4. The prompt's `"ability"` vs `"effect"` disambiguation also needed
   sharpening - "what weapon grants Crush" (Crush being a real spell)
   was initially misread as `kind:"effect"` (Crush isn't a status/buff
   name, so `resolve_codes` correctly found nothing - a safe empty
   result, but still the wrong path). Verified 3/3 correct after adding
   an explicit example distinguishing a spell/skill's own name from an
   obvious stat-buff word (Up/Down/a tier number/a status ailment).

**`/update_codex` (hidden, `telegram_orna.py`) force-refetches
aussiescodex's `codex.json`/`translations.en.json` right now, ignoring
the 1-week TTL** - `orna_aussies.refetch_now()` calls the existing
`refresh_cache()` (clears in-memory caches + deletes the on-disk cache
files) then immediately re-fetches both, returning per-category record
counts and vocabulary sizes for a confirmation reply. Gated by the same
`GO_ALLOWED_USER_IDS` allowlist `/go` uses (imported straight from
`telegram_go`, not duplicated) and left out of `set_my_commands` like
`/go` - a maintenance command for whoever runs the bot, not something a
guild member needs, and not something to leave open to hammering
aussiescodex's API on demand.

**A name/set fragment combined with a filter ("Last Martyr items for
mage") is `"query"` intent, not `"codex"` - was being misrouted.** Live
report: `/orna last martyr речі на мага` found nothing (translated and
searched literally as a codex NAME, which obviously doesn't exist),
while `/orna last martyr` alone correctly found the 16-item set via
`codex_search`. The gap: `route_query`'s prompt described `"query"` only
in terms of stat/effect/attribute asks, never mentioning that a name
fragment PLUS a restriction is really two conditions ANDed together
(`kind:"text"` on name + an attr condition) - exactly what
`orna_aussies.query_records` already handled fine once routed there
correctly (verified directly: `"last martyr"` as a name-text condition
+ `useable_by="mage"` correctly narrows 16 results down to 4). Added an
explicit rule + example; verified 9/9 across 3 phrasings that a
fragment+filter request now goes to `"query"` while a bare fragment
(`"last martyr"` alone) still correctly stays `"codex"`.

**`kind:"attr"` conditions support `"cmp":"!="` for exclusion language
("not X", "except X", "excluding X") - previously silently ignored.**
Live ask: "helmets and armor for mages" implicitly excluding weapons,
generalized to explicit NOT support. `_CMP_OPS` already had `"!="` as a
key, but only the NUMERIC comparison branch in `_eval_condition`'s attr
kind ever consulted `cmp` at all - the text/list/bool/`useable_by`
branches always did a hardcoded equality-style match no matter what
`cmp` said, so `{"field":"item_type","cmp":"!=","value":"weapon"}`
silently behaved exactly like `cmp:"="` (verified the bug directly
before fixing: a "mage, not weapon" query returned weapons). Restructured
those branches to each set a local `matched` bool, then negate once at
the end when `cmp` is `"!="`/`"<>"` - a single negation point instead of
threading it through every branch separately. Verified: 0 weapons in the
negated result set afterward, and the positive (`"="`) path unchanged.

**Usage counters (`usage_stats.py`)** — `record_command(name)` at the top
of every slash-command handler, `record_llm_call(model)` at the two
actual LLM call sites (`telegram_nlp._chat_json_once`,
`telegram_go._chat_json`), both persisted to `usage_stats.json`
(gitignored, same reload-survival reasoning as `reminders.json`). `/stats`
(hidden, `telegram_bot.py`, same `GO_ALLOWED_USER_IDS` gate as `/go`)
reports both counters back. Deliberately scoped to slash commands only -
the free-text conversation entry points in `telegram_assess.py`/
`telegram_resources.py` aren't instrumented yet, so "questions to the
bot" undercounts by however much traffic comes in that way rather than
via a command. `record_llm_call` is called once per actual HTTP attempt
(including retries `telegram_nlp._chat_json`'s wrapper makes), not once
per logical "ask" - a retried call counts twice, which is the more
useful number for understanding real load on Ollama.

**`route_query` has a fifth, "self-aware" intent - `"other"` - for when
the message isn't actually about the codex/resources at all, so `/orna`
can suggest something instead of dead-ending on a failed name search.**
Two triggers: a meta "what can you do"/"help"/"допоможи" ask with no
Orna subject, or a message shaped like a reminder request ("нагадай
мені...", "remind me to...") - that's `/remind`'s job, a separate
command `route_query` previously had no concept of at all. `"query"` is
always empty for `"other"` - the reply (`telegram_orna._capabilities_text`)
is fixed, deterministic text, not model-generated prose, same
structured-over-freeform reasoning as everywhere else in this module.
Deliberately only mentions genuinely public commands (`/orna`,
`/res_today`, `/res_next`, `/remind`) - `/go` and its hidden siblings
stay unlisted here same as everywhere else. Verified with 30 repeated
real-model calls before deploying: 12/12 correct on four "other"-shaped
phrasings (English/Ukrainian, help-ask and reminder-ask), and - the more
important check - 18/18 legitimate Orna questions (name lookups, a stat
query, a name+filter query, "next", "today") stayed correctly classified
with zero false positives into `"other"`, since over-triggering here
would break real functionality, not just add a redundant reply.

**`route_query` has a sixth intent, `"need"`, for a quantity-bearing
resource request ("треба 1000 балоріту") - routes to the same proof-cost
report + reminder buttons the free-text `/need` flow gives, instead of
`"next"`'s bare date lookup with no proof math.** Live report: `/orna
треба 1000 балоріту` showed guild/date rows for both "Balorite" and
"Lesser Balorite" (a plain substring match, `"next"`'s whole mechanism)
with no proof-cost breakdown - the richer report already existed
(`telegram_resources.build_report`, via the free-text/`/need`
`ConversationHandler`), `/orna` just had no path to it.
`telegram_orna._run_need_report` reuses `telegram_nlp.extract_resources`/
`extract_quantities` directly (both already handle Ukrainian, so
`"need"`'s `query` is deliberately the UNTRANSLATED original text -
translating first would only risk mangling a material name before the
exact-match step that needs it) rather than opening the stateful
"which quantity did you mean" follow-up the conversation flow can -
a bare `CommandHandler` has no conversation state to return into, so a
material extracted without a resolvable quantity falls back to
`_next_text` (still useful, just without proof math) instead of trying
to ask a second message. Verified against live data: extraction on the
exact reported phrase correctly resolves `Balorite` (not `Lesser
Balorite` too) with quantity `1000`; verified 9/9 across 3 phrasings that
`"need"` triggers correctly, and 10/10 that ordinary `"next"`/`"codex"`/
`"query"` requests (including ones that also contain a number, like a
stat threshold) don't get misclassified into it.

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
single oversized block.

**Resource reports offer "🔔 remind me" buttons instead of Google Calendar
links - deliberately in one separate follow-up message, not per-row.**
`build_report` returns `(blocks, bundles)`: the text blocks as before, plus
one `ReminderBundle` per (guild, date) - materials landing at the same
guild on the same day share one bundle/button/reminder, since that's one
shop visit. `send_report_blocks(message, blocks, bundles)` sends the text
first, then - if there are any bundles - one more message with a button
per bundle; tapping one calls `telegram_remind.schedule_reminder` (a
public, UNGATED entry point separate from the gated `/remind` command,
since this needs to work for every guild member) to fire a message at
00:05 server-local on the occurrence date. Buttons live in their own
message rather than inline per report row because report blocks get
packed several-per-message to fit Telegram's length cap - a button
"belonging" to one row of a multi-block message has no single
well-defined message to attach to, so one follow-up panel sidesteps that
entirely. State (which bundle a tap refers to) lives in an in-memory
`_REMINDER_STATE` dict keyed by a short id in `callback_data`, same
pattern as `telegram_orna._STATE`/`telegram_go._SESSIONS`; a `"scheduled"`
set per state entry guards against a double-tap creating two identical
reminders. Replaced the earlier `orna_calendar.py` (deleted) design
outright, per explicit ask - a reminder the bot actually delivers beats a
link out to a separate app the user has to remember to check, and sidesteps
the same-day-timezone ambiguity that design's own comment already flagged
(the new fire time inherits that same ambiguity, documented on
`_bundle_fire_at`, rather than pretending it's precise).

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
