# CLAUDE.md

Guidance for Claude Code (or any future contributor) working in this repo.
See `README.md` for user-facing setup/usage — this file is about the code
itself: conventions, gotchas, and why things are structured the way they are.
Deep dives and incident histories that would bloat this file live in `docs/`
(e.g. `docs/orna-loop-internals.md` for the full `/orna` loop internals +
post-mortems) — this file summarizes and links to them; add new deep rationale
there, not here.

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
  offerings-screen screenshots get detected and handed off. **In a group
  only the amity and offerings screens get a reply** - any other image is
  ignored silently (no typing indicator, no error), because busy guild chats
  post random screenshots and the bot was answering each with an OCR dump.
  Item assessment still works in a private chat, but only when stats were
  actually parsed - no stats means no reply at all. OCR text is never sent
  to the chat (it goes to the log only, via `_log_ocr_dump`).
  OCR can turn the styled title area into a plausible garbage line above the
  real name, and the codex search ALWAYS returns something - so the flow
  tries several title-line candidates (`_extract_name_candidates`) and
  accepts a hit only if it resembles the searched line (`_name_matches`);
  otherwise it falls through to asking the user for the name.
- `telegram_offerings.py` — parses the altar's "NEEDED OFFERINGS" screen
  (`<have> / <need> <material>` rows), computes shortfalls, and reuses
  `telegram_resources.build_report`.
- `telegram_resources.py` — the natural-language "what do I need"
  `ConversationHandler`, and the shared report builder (`build_report`,
  `send_report_blocks`) both this and `telegram_offerings.py` use.
- `ollama_client.py` — shared low-level Ollama `/api/chat` client
  (`think: True` + `format="json"` + tolerant JSON recovery + cloud→local
  fallback), extracted from what used to be near-duplicate copies in
  `telegram_nlp.py` and `telegram_go.py`. `chat_json` is one HTTP attempt;
  `chat_json_with_fallback` tries Ollama Cloud first, falls back to local
  on failure (mid-turn image-drop retry included). Raises `OllamaError` if
  the parsed JSON isn't a dict — see the note below on why that check
  matters, not just format compliance.
- `telegram_nlp.py` — the two remaining local-only structured-extraction
  calls: which materials a free-text message refers to, and what quantity
  of each (used by `telegram_resources.py`'s conversation and `/orna`'s
  `need` tool). Both call `ollama_client.chat_json` directly, local host
  only, with a retry-once wrapper. `/orna`'s own routing/condition-parsing
  used to live here (`route_query`/`plan_queries`) but was retired when
  `/orna` became a real ReAct loop — see its section below.
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
- `orna_codex_db.py` / `codex.sqlite3` (gitignored) — the codex as a real
  queryable DB, built in 0.29s from the same dump `orna_aussies` caches.
  Exists because `query_records` is a FILTER, not a query language: it cannot
  COUNT, GROUP BY, join a record to its drops' stats, or full-text search.
  SQLite, not MySQL/MongoDB — see `README-db.md` for the measured comparison.
  Backs `/orna`'s `sql` tool; rebuilt by `/update_codex`. Read-only connection
  (`mode=ro`) is the write guarantee, not a prompt rule.
- `telegram_orna.py` — `/orna <text>`, a real ReAct loop (today/next/need/
  search_codex/query/events/open_entry/research/calculate/assess/compare/
  build_optimize/towers/class_guide/knowledge_search/web_search/ask/finish
  tools) over Orna's data. See its own section below — this is now the
  second most complex module in the repo after `telegram_go.py`, and
  deliberately mirrors that file's loop design.
- `orna_calendar.py` — scrapes `playorna.com/calendar/`'s live event list
  (no `codex-bootstrap` JSON there, unlike every other codex page — plain
  server-rendered `article.event-card` HTML). Filters to live/upcoming
  events by real parsed datetimes, not left to the model — see the `/orna`
  section for the live bug this fixes. Cached to disk like
  `orna_aussies.py` but with a 6h TTL, not 1 week.
- `orna_towers.py` — deterministic estimate of Orna's 5 "Wild Towers of
  Olympia"'s current floor, ported line-for-line from OrnaCodex's own
  `tower.ts` (pinned commit) and cross-checked against that original
  TypeScript's actual output under Node before deploying — see the `/orna`
  section. Pure time-based math, no external data source at all.
- `orna_classes.py` / `orna_classes.json` / `orna_scrape_classes.py` — the
  per-class and per-specialization stat modifiers, bonus stats and passive
  effects behind aussiescodex's stats estimator, plus the estimator math
  (Ascension Level, PVP). Committed, NOT crawlable — see the `/orna`
  section.
- `orna_bonuses.py` — Amities and Crucibles scraped from aussiescodex's
  two HTML pages, disk-cached a week like `orna_releases.py`. See the
  `/orna` section for why these needed a source of their own.
- `orna_reddit.py` / `orna_reddit.txt` / `orna_scrape_reddit.py` — what
  Orna's own developers (u/OrnaOdie, u/Widogeist) have written on Reddit:
  hidden mechanics, exact formulas, "why it works like that" answers. Static
  and committed, NOT re-crawled — see the `/orna` section.
- `orna_releases.py` — playorna.com/releases/, the official patch notes,
  parsed from plain server-rendered HTML (`article.release-note`, no
  `codex-bootstrap` JSON — same as `orna_calendar.py`) and cached to disk
  (`.releases_cache/`, gitignored) with a 1-week TTL like
  `orna_aussies.py`. See the `/orna` section for why a changelog earns its
  own source alongside the codex.
- `orna_knowledge.py` / `orna_knowledge.txt` / `orna_scrape_knowledge.py` —
  a curated community-knowledge reference (flattened text, fuzzy-searched)
  for what playorna's codex genuinely doesn't track at all — most notably
  per-boss elemental damage resistances/immunities. Static generated file
  + the script that (re)generates it, same pattern as
  `orna_material_names_uk.json`/`orna_scrape_material_names.py`. See the
  `/orna` section for sources and why this is flattened text rather than
  typed tables.
- `orna_echo.py` / `orna_echo.txt` / `orna_scrape_echo.py` — playerecho.com's
  37 Orna guides, the only source in the repo that states FORMULAS and
  mechanics outright (Ward capacity, Ascension altar costs, dungeon
  cooldowns/godforging, anguish proofs, per-event tier gates). Committed and
  re-crawled by hand like `orna_reddit.txt`; searched at SECTION level and
  surfaced through `knowledge_search`. See the `/orna` section.
- `orna_discord.py` / `telegram_announce.py` — official Orna announcements,
  FOLLOWED into a Discord channel the user owns, relayed to every Telegram
  group/channel the bot is in, translated to Ukrainian with redeem codes left
  untouched. See "Discord announcements" below.
- `orna_monuments.py` — this week's Monument rewards (floorchart.top): which of
  Ithra/Thor/Vulcan/Demeter gives what on which floor. Backs `/orna`'s
  `monuments` tool. Disk-cached 6h + refetched at the ISO-week rollover
  (`.monuments_cache/`, gitignored). See "Monuments" below.
- `orna_ornabook.txt` / `orna_scrape_ornabook.py` — Ornabook (book.cadelabs.ovh),
  a community mechanics book. No reader of its own: same `=== Title (url) ===`
  / `## Heading` format as `orna_echo.txt`, read by `orna_echo.search(...,
  path=...)`. Surfaced as a `knowledge_search` block. See "Ornabook" below.
- `orna_guides.py` / `orna_guide_<topic>.txt` (×8) / `orna_scrape_guides.py`
  — long-form WRITTEN community class/build guides (Summoner, Realmshifter/
  Thief, Deity, Gilgamesh, Beowulf, Swash, Heretic, Towers of Olympia
  mechanics), one static file per topic, deliberately NOT merged into
  `orna_knowledge.txt`'s single fuzzy-searched corpus — see the `/orna`
  section for why a "select by class name" reader shape fits these better
  than a "grep across everything" one.
- `telegram_remind.py` — hidden `/remind` command (same allowlist as
  `/go`): schedules a one-off reminder via PTB's `JobQueue`, persisted to
  `reminders.json` so it survives the frequent `launchctl` reloads this
  repo's development involves. `schedule_reminder` is a separate public,
  UNGATED entry point onto the same scheduling/persistence machinery -
  `telegram_resources.py`'s "remind me when this resource lands" buttons
  use it directly, without going through the gated command.
- `telegram_amity.py` — memory-hunt coordination. `telegram_assess`'s photo
  flow hands a "MEMORY COMPLETED"/"Спомин завершено" screenshot here (fuzzy
  header match), and also an INVENTORY/equipped amity ("Inventory"/"Інвентар",
  no header, no "When equipped...") - recognised by the fixed description
  "Spectral essence of a place in time... bonuses and maluses.", whose effects
  end at TIER/РАНГ. Its screen carries no hour, so the uploader must give
  colour + option + UTC hour ("Red 4 14", "червона 4 14:00";
  `parse_choice_hour`), and one ACQUIRED before this week's Monday (a day of
  slack for the player's local date) is refused as already reset. Effects are split (lowercase/number-only line = wrap; first
  half bonuses, second half maluses - the counts are always equal, an odd
  count is refused) and fuzzy-matched against `orna_bonuses`' structured
  `amity_cards` (Ukrainian lines go through `telegram_orna._translate` first).
  Witch colour/option comes from the caption ("red 4", "фіолетовий 3",
  "4yellow"); if missing, the uploader is pinged and their NEXT message is read
  once (pending-filter pattern, invalid = cancelled). Hour/week come from the
  message's UTC date; week = ISO week, which resets Monday 00:00 UTC like the
  game. `/amity` lists this week's finds globally (sharer shown as a t.me link,
  not a mention, so listing doesn't ping everyone); `/amity delete [hours]`
  removes the caller's own. Stored in gitignored `amities.json`; dedupe is by
  slot + effects, so a party member re-posting the leader's find is rejected.
  `/iam <nick>` stores an in-game nickname per Telegram user ID (not
  username, which can change) in gitignored `nicknames.json`; `/amity` shows
  it next to the sharer. Unlike amities, it never resets.
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
development (see the multimodal note below). `_call_model` now delegates
the actual cloud→local fallback mechanics to `ollama_client.
chat_json_with_fallback` (shared with `/orna`'s loop — see its section) —
`/go` still passes its own `GO_MODEL`/timeout defaults, so this delegation
changed nothing about `/go`'s own behavior, just removed a duplicate
implementation. `/orna`'s loop deliberately uses a SHORTER model-call
timeout than `/go`'s (unchanged 90s read) — see the `/orna` section's
reliability notes for why a step-budgeted loop and a single-shot-per-turn
design want different timeout tradeoffs.

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
guild members already rely on while `/orna` is proven out; `/res_today`/
`/res_next` now delegate to `/orna`'s own `_today_text`/`_next_text`
rather than keeping a second copy — see below — so at this point only the
free-text `ConversationHandler` flow in `telegram_resources.py` is a
genuinely separate implementation).

### Architecture: a real ReAct loop, not a routing pipeline

`/orna` is a real ReAct loop (it replaced the old `route_query`/`plan_queries`
two-call pipeline), mirroring `/go`'s `_advance`/session/step-budget design: one
action per turn via a JSON schema `{"thought","action","action_input","args"}`
(native tool-calling also declared, as a fallback), over the tools in `_ACTIONS`.

**Loop knobs** (`OrnaSession`/`_advance`/`_call_step_model`, current values):
- `MAX_STEPS = 35`, `LOOP_TIMEOUT_SECONDS = 600` — the hard wall-clock ceiling
  (`asyncio.wait_for`) is the real "always replies" guarantee; running out of
  either SUMMARISES from what was gathered (`_close_out`), never fails blank.
- **Every step tries Ollama Cloud first, falls back to local mid-turn.**
  `MAX_CLOUD_CALLS = 20` is a runaway guard; `FORCE_LOCAL` (harness) forces
  local. Cloud read timeout 45s (give up fast, a fallback waits), local 120s
  (it IS the fallback).
- **Cloud circuit breaker** (`ollama_client.CLOUD_COOLDOWN_SECONDS = 300`,
  `cloud_is_parked()`): one cloud failure parks the cloud leg 300s, so an outage
  can't cost 45s per step. Only `OllamaUnavailable` (timeout/HTTP/connection)
  parks it — a reply that arrived but was unusable does not.
- `_call_step_model` retries once. `ask` is bounded by `MAX_ASKS_PER_REQUEST =
  2`; a typed follow-up re-arms for `_FOLLOWUP_TTL_SECONDS = 180s` and resumes
  with `_RESUME_STEPS = 8`.

**Standing invariants — the rules the detailed docs keep re-deriving:**
1. Observation honesty: the model reads only what a tool RETURNS, never what it
   posts to Telegram; any truncation is marked PARTIAL, never silent.
2. Tool guard > prompt rule: when a tool can produce a valid-but-useless answer,
   close it in the tool, not only in the prompt.
3. Blame the tool before the model: check what the tool returned for the exact
   args before concluding "the model is flaky."
4. Never block the event loop: wrap disk/HTTP/CPU work in `asyncio.to_thread`.
5. Cache pattern (aussies/releases/bonuses/knowledge): disk cache + TTL + atomic
   write + an empty/partial parse is NEVER cached (see each module + `/update_codex`).

**Context format (2026-09-29).** The transcript is TAGGED: `[USER QUESTION]` /
`[USER FOLLOW-UP]` / `[USER ANSWER to your question]`, numbered
`[TOOL RESULT #n · call]` blocks, and `[SYSTEM NOTE]`. `finish()`/`ask()` are
recorded as assistant turns, so a `/clarify` can see the answer it follows (before
this they were never stored at all). Every step call also gets a transient
`_working_state` message, which is never stored. It indexes the user turns, the
model's own earlier answers and every result by number, and asks the thought to
quote the values it will use before picking a tool. This fixed a live report:
"+20%" on a 200% godforged item re-ran codex lookups and scaled the BASE stat.
Two things caused it: that brief was missing, and `assess` returned no numbers
(it now returns the projected per-level stats via `_projection_observation`).
The log has the whole chain, keyed by sid:
`orna: turn` / `step` (with thought) / `result` / `finish`. httpx's `getUpdates`
polling lines are dropped by `telegram_bot._RedactSecrets`, since they were ~95% of the log.

**Codex shape:** every playorna codex page is one universal JSON schema
(`codex-bootstrap`: `detail{name,facts,effects,tags,sections}`, or `results`);
`orna_codex.fetch_codex_json`/`codex_search` return it as-is, so the loop is a
render/navigate layer. Navigation sends NEW messages (Telegram scrollback IS the
history); only result-list paging edits in place.

**Generic query:** the `query` tool builds `{conditions,combinator,category,
sort_by,sort_dir}`; `orna_aussies.query_records`/`_eval_condition` evaluate one
condition vocabulary (kinds: `stat`/`text`/`effect`/`attr`/`ability`/`bond_bonus`).

**Full internals — the complete condition vocabulary, every tool's design, and
the incident history behind each rule — are in `docs/orna-loop-internals.md`.
Read it before changing the loop; add new incidents THERE, not here.**


### `estimate_stats` - a whole character's projected stats

`estimate_stats(args={items:[{name,quality,level}], specialization, class,
ascension_level, pvp, amities})` posts a stat table. It computes BASE stats
and ITEM stats SEPARATELY (design ask 2026-09-25) and shows each as its own
block plus a combined total when both are present - so a player can ask for
just their base stats (class + spec + AL, no gear) OR a full loadout. The
order of operations is deliberately split across two modules so each half is
pinned by its own self-check:
  1. gear: every worn item assessed at ITS OWN quality and level (the same
     `orna_assess.get_assess_result` path `/orna assess` uses) and summed -
     gear stats are ADDITIVE;
  2. base: the tier-10 specialization's absolute base stats;
  3. `orna_classes.scale` applies the class's percent modifiers, Ascension
     Level (+1%/level) and PVP (HP ×2) to EACH block. `scale` is linear per
     stat, so `base_scaled + items_scaled` equals scaling the combined block -
     the total is exact, just broken out, which is what lets the two be shown
     (and computed) independently.

**`orna_classes.scale` is that third step, and it exists because there were
TWO copies of it.** `orna_classes.estimate` and this tool each had their own
identical loop over the stat block, so the AL/PVP rules `orna_classes._demo`
pins were not necessarily the rules the bot ran - the tool's own docstring
already claimed it delegated, and didn't. Both call `scale` now.

**What's REQUIRED depends on the mode** (revised 2026-09-25 when the tool
learned to do base stats without gear): `ascension_level`, AND at least one of
(a real `specialization` -> base stats, or `items` -> gear stats). `items`,
`pvp` and `class` are OPTIONAL and must NOT be demanded: omit `items` for a
base-only estimate; `pvp` defaults to PVE (a base-stats ask is "class, spec,
AL" and shouldn't drag the user through a PVP prompt - the reply states the
assumption); `class` modifiers apply only when a class is given, so `spec + AL`
alone still yields base stats. The tool still refuses a call it can't compute
ANYTHING from and returns an observation listing exactly what's missing, so
the model's next move is an `ask` for precisely those. Notes:
- `specialization: "none"` is a valid ANSWER (not every player has a tier-10
  spec - aussiescodex's own estimator ships a "None" entry for this); with
  `"none"` AND no items there's nothing to compute, so that's the one case it
  asks for "a spec or items". `ascension_level` is read with an explicit `is
  None` check, not `or`, so a real AL of 0 isn't collapsed back into "not
  given".
- The refusal observation is prefixed `NEEDS_INPUT:` - see the finish note
  below, which keys off it.
- The values may come from an earlier TOOL result as well as from the user:
  the tool description tells the model that a named BUILD ("the omniflask
  raid build") means calling `class_guide`/`knowledge_search` first and
  passing the item names it lists.
- A name that isn't in the real pool is a MISSING input, not a warning. Live
  2026-09-25: `specialization="Гільгармос"` resolved to nothing and was
  SILENTLY dropped, so Gilgamesh's whole 12,509-hp base never entered the sum
  and the table still looked complete.

**The tool splits the phrase the user actually types; the model kept asking
instead.** Live 2026-09-25, a request that already contained everything -
"Im heretic ara, sequencer, 102 AL, PVP. Items: Celestial Staff 20lvl,
Godforged Heretics Robe 200%, ..." - was answered by asking for the upgrade
levels, then asking for the quality percentages, then spending eight steps
on `search_codex`, then finishing with prose that described what the answer
would be and no table at all. Three causes, all fixed:
- `_split_item_phrase` reads quality and level out of the NAME
  ("Godforged Fallen Sky Shoes 195%" -> 195%, level 13; "Celestial Staff
  20lvl" -> level 20), so the phrase can be handed straight through and
  there is nothing left to ask about. `_level_in` accepts the marker before
  or after the number, since people write both.
- The tool description now says to pass names VERBATIM and never
  `search_codex` them first - the tool resolves them itself and reports what
  it can't.
- The `MAX_ASKS_PER_REQUEST` observation said "...and finish", so the model
  finished - without ever calling the tool it had just spent two asks
  collecting inputs for. It now says to re-read what the user already wrote,
  CALL the tool, and only then finish: "do not describe what the tool would
  have computed: run it."
Verified 2/2 end-to-end on that exact request: one `estimate_stats` call, no
searches, no asks, full table.

**`_name_candidates` also tries the POSSESSIVE forms** - "Cupid Locket" is
"Cupid's Locket" and "Heretics Robe" is "Heretic's Robe". Dropping the
trailing word (the existing ladder) doesn't save these: bare "Cupid" matches
the monster first. Both spellings are tried before the lossier drops, so
`assess`/`compare`/`build_optimize` gained it too.

**Quality and LEVEL are two independent axes, and treating them as one
understated every upgraded item.** Orna has 13 levels: 1-10, then
Masterforged 11 / Demonforged 12 / Godforged 13. `_parse_quality_spec` used
to return level 1 for every percentage, so "my 185% Lost Helmet" was
projected UNUPGRADED (318 defense instead of 726 at lv10) and the only way to
reach a high level was to name a forge tier, which also forced quality to
100%. It now reads an explicit level out of the same free text (`lv10`,
`+10`, `рівень 10`, `185% lv10`), and `estimate_stats` also accepts a
separate `level` key per item. An explicit level beats one implied by a forge
name. Defaults, per explicit ask: **quality 100%, level 1** - the old default
was 200%/level 13, i.e. every unstated piece came back silently forged.
Fixing this in the shared parser means `assess`, `compare` and
`build_optimize` all gained it too, since all four route through it.
**Read the projection, not `entry.stats`** - `AssessResult.stats` is
`{stat: StatRow}` where `StatRow.values` holds one value per upgrade level.
A first version summed `entry.stats`, which is the item's UNUPGRADED base,
so a godforged Lost Helmet contributed 172 defense instead of 472 - the
totals still looked plausible, which is exactly why this is called out.

**Budget raised to `MAX_STEPS = 35` and `LOOP_TIMEOUT_SECONDS = 600`** on
ask, because a multi-item estimate legitimately needs many tool calls.
Running out of either still SUMMARISES rather than failing - `_close_out`
already covered both endings, and there is now a stub check that 35 steps
followed by exhaustion produces an answer built from what was gathered.

**Four things went wrong the first time this ran live, all fixed, and
three of them produced a confident WRONG answer rather than an error:**
- **A name must be resolved in its OWN pool.** "Heretic Ara Sequencer" was
  passed as `specialization="Heretic Ara"`, `class="Heretic"`, and
  `find_class("Heretic")` returned the tier-10 SPECIALIZATION (searched
  first by default) whose `stat_modifiers` are empty - so Sequencer's real
  -5/+15/-5 were silently dropped. `find_class(name, kind=...)` now forces
  the pool, and the tool says so when a `class` value isn't a class.
- **The model invented a loadout.** Given only a class it produced a total
  built from three items the user never mentioned. The prompt now forbids
  putting anything in `items` the user didn't name, and calling the tool
  with no items makes it print "спорядження не вказано" outright - the tool
  cannot otherwise distinguish "no gear" from "the model forgot the gear",
  and a silent omission reads as a complete answer.
- **`ask` offered invented pairings** ("Маг(Gilgamesh)", "Ловець(deity)" -
  not real class/spec pairs), and added its own "Своя відповідь" on top of
  the one the loop always appends, so the user saw two near-identical
  buttons. Options matching `_OTHER_OPTION_RE` are now filtered out, and
  the prompt requires real concrete choices.
- **Unknowns were silently defaulted** (AL 0). The prompt now requires
  every still-unknown input to be stated as an assumption in `finish()`.

**An impossible LOADOUT was totalled up as if it were a character, and the
fix was data the repo already had.** Live 2026-09-25: "find all best magic
items for head, torso, hands, legs, accessories ... heretic ara sequencer with
102 AL" produced a full stat table for a Celestial Archistaff (TWO-HANDED)
worn together with an Arisen North Star (off-hand). Three separate defects:
- **`is_two_handed` was hardcoded `False`**, on a note in this very file saying
  aussies "doesn't expose as a flat field at all". It does - as a **tag**,
  `"two_handed"`, on **106 items**. So the flag is now derived from the record,
  which also fixes the adornment-slot count `orna_assess` keys off it (the
  narrow consequence the old `ponytail:` note predicted) and was the real cause
  of the impossible loadout. **The weapon SUBTYPE is not a substitute:
  archistaffs are 20 two-handed and 67 one-handed**, so "it's an archistaff"
  says nothing. `CodexEntry` gained a `place` field for the same reason - the
  slot is needed to validate a loadout at all.
- **`_check_loadout`** (pure, unit-tested) validates the real slot capacities -
  one head/torso/legs, **two** accessories, and two HANDS: either one
  two-hander alone, or two one-handed weapons, never a two-hander plus an
  off-hand. `estimate_stats` REFUSES an illegal loadout, posts nothing, and
  returns the conflict so the loop re-picks. Deliberately NOT the
  `NEEDS_INPUT:` prefix - that arms the wait-for-a-typed-answer path, and this
  needs the MODEL to choose legal gear, not the user to supply anything.
  Verified: the exact reported loadout is refused with 0 messages posted, and
  the loop then re-queries for one-handed weapons and finishes with a legal
  weapon + off-hand.
- **Dual wielding two one-handed weapons counts 65% of their COMBINED stats**
  (`_DUAL_WIELD_FACTOR`) - the guild's statement of game behaviour, not
  derivable from any source in the repo (the guides describe dual-wielding but
  never the factor), so it is implemented as given and pinned in `_demo`,
  exactly like `orna_classes`' AL/PVP rules. **It genuinely beats the
  two-hander**, measured on this very request: two one-handed staves total
  13,467 magic against the Celestial Archistaff's 12,445, so "the two-hander
  is obviously better" is wrong and the prompt says so.
- **Only ONE CELESTIAL weapon can be equipped**, one-handed or two-handed
  (game rule, stated 2026-09-25) - it matters precisely because celestials top
  most stat rankings, so a naive "best in every slot" search reaches for two of
  them. This also invalidated this file's own first dual-wield measurement,
  which used two celestial staves: the legal pairing (Celestial Staff + Arisen
  Fey Macha Pillar) totals **13,743** magic, not 13,467, against the
  two-hander's 12,445 - so the dual-wield advantage is LARGER than first
  reported.
- **Class/spec abilities are DISCOVERED from the codex, not hand-written per
  specialization** (`orna_aussies.class_abilities`, added 2026-09-25 on the ask
  "ideally bot should find out this data for all specializations on its own").
  All 82 classes - every tier-10 spec and celestial variant included - carry a
  structured `abilities` list in `codex.json`, and `translations.en.json`
  describes all 134 of them in plain English ("Resurgence: You become more
  powerful as your HP decreases in battle"). So `estimate_stats` can list what a
  Gilgamesh/Deity/Heretic Ara actually does with no rule written per class.
  Two things to know: aussies names the gendered pairs as ONE entry
  ("Beowulf / Bestla", "Heretic Ara / Hera Ara"), so each side of the slash is
  registered as its own alias or a lookup for "Beowulf" finds nothing; and the
  two sources are COMPLEMENTARY, not redundant - `orna_classes.json` carries
  `passiveEffects` for only 13 classes and NONE of the tier-10 specs, but it is
  the only place naming the Dual Staffs / Dual Wield conditions (Sequencer,
  Duelist), which aussies has no class record for at all. Show both.
- **Class/spec PASSIVES are conditional and the stat table cannot express
  them** - `orna_classes.json` has carried `"Sequencer Doublecast (Dual
  Staffs)"` / `"Sequencer Weapon Power (Dual Staffs)"` all along and nothing
  surfaced them, so an estimate silently ignored the nuance that decides
  whether the loadout is any good. They are now listed in the reply AND in the
  observation (the model cannot read what was only sent to Telegram), with an
  explicit line saying whether a "dual" condition is met.

**`_REASONING_RULE` - reason twice, once before the tools and once before
finish.** Added 2026-09-25 on ask, after the above. The `"thought"` field must
first state the goal and enumerate EVERY explicit constraint the user gave
(slots, quality, level, class, spec, AL, PVE/PVP, quantities, game mode,
language), then plan the tools; and before `finish()` it must walk that list
again and confirm each constraint is satisfied by an OBSERVATION rather than an
assumption, that every number came back from a tool, and that nothing a tool
warned about (a refusal, a PARTIAL list, a conditional passive, an assumption)
was dropped. It also carries the GAME-RULE SANITY paragraph above, since a stat
table can be arithmetically perfect and still describe a character nobody can
build. Prompt-only, so it is a reduction and not a guarantee - the guarantees
for this class are `estimate_stats`' refusal and the derived `is_two_handed`.

**A prompt rule could not make the model ask - the TOOL had to refuse.**
Live: "/orna calculate my stats" called `estimate_stats` with empty args and
posted a header, "спорядження не вказано" and an EMPTY stat table: a
confident-looking answer containing nothing. Two causes. The tool
description itself said "if they gave no gear, either ask, **or call it with
no items at all**" - an explicit licence to skip the clarification, which no
amount of CLARIFICATION wording further down a 33KB prompt was going to
outweigh. And the tool cheerfully rendered the empty case. Both fixed: the
licence is gone, and with no items AND no specialization AND no class the
tool now posts NOTHING and returns an observation telling the model to
`ask()` for the specific missing inputs. Verified 3/3 that the same request
now asks. **The pattern to copy: when a tool can produce a
technically-valid-but-useless answer, close it in the tool, not in the
prompt** - the prompt is advice, the tool is the guarantee.

**Clarification vs inline, the two halves of "ask when something is
missing":**
- In a CHAT, a request missing something that would change the answer (for
  a stat estimate: the items, their qualities, the spec/class, AL, PVP)
  makes the model call `ask`. Verified live - "порахуй мої стати" asks for
  exactly those, rather than inventing a loadout.
- INLINE there is no reply channel at all, so `OrnaSession.allow_ask` is
  False there: the prompt says so up front, and `_advance_inner` refuses an
  `ask` with an observation telling the model to answer from what it has and
  state its assumptions. Verified both ways with stubs - chat pauses on the
  question, inline answers anyway.
- **Follow-ups and typed answers go through `/clarify <text>`** (2026-09-28,
  replacing a 180s "this user's next free-text message resumes the loop"
  window that kept catching group chatter). One conversation per user
  (`_USER_SESSIONS`: user_id -> sid): a new `/orna` replaces it, and it expires
  `SESSION_TTL_SECONDS` (15 min) after the bot's last reply in it - `created` is
  refreshed on each reply, so it measures idleness. `OrnaSession.awaiting` says
  whether the last reply was a question (ask(), or a finish that really was
  one), which frames the `/clarify` as an ANSWER (`_RESUME_STEPS`) or a
  FOLLOW-UP (`MAX_STEPS`). A `/clarify` while the loop is still running is
  refused (`session.running`), a bare "/clarify thanks" is acknowledged without
  resuming (`_is_pleasantry`), and every finish/ask carries a `_clarify_hint`
  line (not inline, which has no reply channel). Pinned in `_demo`.
- **`MAX_ASKS_PER_REQUEST = 2`, enforced in code.** The loop asked three
  times in a row: the user tapped "I'll provide details", then
  "Specialization/Class" - options naming WHAT to supply rather than
  answering anything - so each tap resumed the loop with no new information
  and it asked again. The prompt's "don't ask more than once" did not hold.
  Past the cap the model gets an observation telling it to work with what it
  has and finish.
- **Every option must be a possible ANSWER, not a category of answer.**
  "Mage"/"Godforged"/"PVP" are answers; "I'll provide details",
  "Specialization/Class", "Equipment and quality" are not - tapping one
  carries no information, which is what produced the three-ask loop. The
  prompt says this with both the good and the bad examples, and tells the
  model to say outright that the user may just type the whole list.
- **A finish() that is really a question keeps listening.** The model often
  states what it still needs as an ANSWER instead of calling `ask`, which ENDS
  the request - and the user's typed reply then falls through to the assess/
  resources handlers and vanishes ("Bot ignored my answer", live 2026-09-25;
  measured 2 of 3 runs of "порахуй мої стати" finished in prose without ever
  calling a tool). Two narrow signals that a finish is a question, both
  checked in the finish branch: a tool refused for want of a user input (the
  `NEEDS_INPUT:` prefix), or the loop made NO tool call at all, which for
  `/orna` means it produced no data and can only have been asking. Either arms
  the same one-shot typed-answer wait an `ask` would have, counts as an ask so
  `MAX_ASKS_PER_REQUEST` bounds it, and is skipped entirely inline. The
  inferred case gets a shorter fuse (`_FOLLOWUP_TTL_SECONDS`, 180s) than a
  real ask (600s), because it is a guess. `handle_ask_text` clears the flag
  and tops `steps_left` up to `_RESUME_STEPS` - the answer can arrive after
  the budget was already spent, and resuming into an immediate close-out would
  waste it.
- **Class and specialization names are a CLOSED SET, so the prompt carries
  the whole set rather than three examples.** The `estimate_stats` tool
  description interpolates `orna_classes.all_names("class")` and
  `all_names("specialization")` - 40 + 19 real names, built from the data so
  they cannot drift. Live 2026-09-25 the model invented Ukrainian class
  buttons ("Маг", "Дудар", "Зник", "Орdinator", "Гільгармос"), none of which
  `find_class` resolves; with the real list in the prompt, 7 of 7 runs offered
  real names. Note this is DATA, not another rule - the guarantee is the
  tool's refusal above, which is what stops a bad name becoming a wrong
  number. Also fixed: the prompt's own "good option" example was `"Mage"`,
  which is not an Orna class (`find_class` resolves it to *Time Mage*), so the
  prompt was teaching the exact invention it warns about.
- **`ask` options get normalised** (`_normalize_options`): the model
  sometimes packs the whole list into ONE string
  (`["['Клас та одяг', 'Тільки класс', 'Інше']"]`), which rendered as a
  single button labelled with a Python list repr. A bare string is unpacked
  too, since iterating it would otherwise make one button per CHARACTER.
  Same wrong-shape drift as `action_input` arriving inside `args`.

### Class / specialization stats (`orna_classes.py`) - the player stats estimator

**`orna_classes.json`'s two pool names are aussiescodex's and they are
INVERTED from the game's own words - this was the bot calling Ranger a class
for a year.** The game nests three things: a **CLASS LINE** (exactly six: Mage,
Thief, Warrior, Valhallan, Summoner, Demigod - each holding a class at EVERY
tier 1-10, so a line is not one class and its tier-10 class is only its last
rung), a **CLASS** (one tier 1-10 step inside a line - tier 1 Mage/Thief/Warrior
up to the 18 tier-10 ones: Heretic/Hera, Gilgamesh/Gallia, Beowulf/Bestla, Grand
Summoner, Deity, Realmshifter, each with two Celestial variants), and one
**SPECIALIZATION** on top (Ranger, Berserker, Sequencer, Duelist, ...). The dataset's `spec_stats` pool (19) is really the
tier-10 CLASS and its `classes` pool (40) is really the SPECIALIZATION.
Measured, not argued: **0 of the 40 appear in the codex's own `classes`
category, and all 19 of the others do** (`Diety` being aussies' misspelling).
- **Live failure 2026-09-27** that surfaced it: an answer offered "Ranger",
  "Summoner" and "Dexterity-based classes (Ranger, Assassin, Tamer)" side by
  side as classes. Only Summoner is one. It came straight from the
  `estimate_stats` tool description, which interpolated the 40-name pool as
  the valid `class` values and the 19-name pool as the valid `specialization`
  values - so the prompt taught the inversion, and the stat table printed it
  too ("клас: Sequencer · спеціалізація: Heretic Ara", exactly backwards).
- **The pool names are LEFT as they are**; renaming them would desync this
  module from its own scraper for no user-visible gain. The inversion stops at
  the module edge instead: the `estimate_stats` tool's KEYS are now the game's
  (`class`="Heretic Ara", `specialization`="Sequencer"), the reply and the
  observation label them that way, and `_TAXONOMY_RULE` in the system prompt
  states all three levels.
- **The tolerance is `telegram_orna._reassign_class_pools`, and it works
  because the two pools are fully DISJOINT** (0 overlapping names, asserted in
  `_demo`) - so the NAME alone says which pool it belongs to and a crossed call
  still lands right. Verified all four shapes produce identical output:
  game-correct, fully inverted, class alone, and a specialization sent in the
  `class` field. A name in NEITHER pool stays in the field the model filled, so
  the existing "that is not a real name" refusal still points at the right one.
- **A class's LINE is in no structured source, so nothing here claims to know
  it.** A codex class record has `tier` and no line (checked across all 82), and
  the official description's "can wield equipment of the thief" is cross-line
  EQUIPMENT ACCESS rather than membership - `Heretic Corvus` is a Mage-line class
  whose own description says "thief". The chains exist only as prose, in
  `orna_echo`'s tier-by-tier progression guides, so `_TAXONOMY_RULE` tells the
  model to get a line from `knowledge_search` or leave it out. **A
  line -> tier-10-class table was written and then removed** (2026-09-27, same
  session): it made "the Mage line" read as a synonym for Heretic, which is the
  confusion it was supposed to fix. `orna_classes` keeps only a shape assert
  (18 tier-10 names = 6 lines x 3).
- **The fix needed THREE layers, because our own prose outvoted the rule.**
  With the code and prompt fixed, 1 of 3 live runs still answered "Heretic is a
  tier-10 specialization" - and the source was `orna_mechanics.txt`, a corpus we
  maintain, which read "The six tier-10 specializations are Gilgamesh,
  Heretic, ...". The loop retrieved it via `knowledge_search` and repeated it.
  Same shape as the tower-reset case below, so the same three-layer answer:
  * the corpus now states the three levels and says outright that these are
    CLASSES (plus that a line holds a class at every tier);
  * `_TAXONOMY_RULE` ends with **THIS RULE OUTRANKS A SOURCE'S WORDING** -
    `orna_echo.txt`, `orna_reddit.txt` and `orna_qa.txt` use the loose wording
    (41/101/51 hits of "specialization") and are scraped from other people, so
    they are not ours to police; the rule names the exact sentence shape and
    says to translate it, not repeat it. Same pattern as `towers` outranking
    guide prose and `releases` outranking `knowledge_search`;
  * tier-0 `corpora-vs-taxonomy` pins the corpus we DO own, scoped to it for
    exactly the reason `corpora-vs-towers` is, and it excludes the corpus's own
    correcting sentence before searching for the claim.
- **Gear restrictions key on the LINE, not the class or the specialization** -
  that is what an item's `useable_by` is (`all_classes`/`magic_users`/
  `warrior_classes`/`thief_classes`/`valhallan_summoner_classes`/
  `melee_classes`, the only six values in the live data). So a "which classes
  is this for" answer reads that field; it never groups by specialization.
  Verified after: the reported request answers with each drop's slot and its
  real `useable_by`, where before it invented per-class recommendations.

The data behind aussiescodex's own stats estimator, extracted from the
Next.js chunk that feeds their UI. **Committed, and deliberately not on a
TTL like every other scraped source**, because there is no stable address
to poll: the chunk's filename carries a content hash
(`218-cb72350c16e02252.js`) that changes on every one of their deploys, so
yesterday's URL 404s. `orna_scrape_classes.py` takes the URL as an
argument and prints where to find the current one. Same
committed-and-manual treatment as `orna_reddit.txt`, for a different
reason — that one is append-only history, this one has no fetchable URL.

- No JS engine needed: the payload is three `JSON.parse('{...}')` literals,
  pulled out as text. The extractor scans for the closing quote rather than
  regexing, because the payload contains escaped quotes a non-greedy regex
  would stop on.
- **The blobs are classified by SHAPE, not position.** A rebuild could
  reorder them, and mislabelling the absolute stat table as percent
  modifiers would poison every estimate while still looking plausible.
- **Two kinds of entry, and confusing them gives wrong-but-believable
  numbers:** 19 tier-10 SPECIALIZATIONS carry ABSOLUTE stats (Gilgamesh =
  hp 12509, attack 1304); 40 CLASSES carry PERCENT `statModifiers`
  (Brawler = hp +5%). A class therefore returns modifiers only and an
  empty stat block unless the caller supplies a base — it must never
  invent one.
- Estimator rules, which are the guild's statement of game behaviour and
  are NOT derivable from the data, so they are pinned in `_demo()`:
  **Ascension Level is +1% per level on every stat (AL 100 doubles), PVP
  doubles HP only.** Both compose multiplicatively with the class modifier.
- Two name-matching traps, both found by the self-check: aussiescodex
  spells it **"Diety"** while players type "deity" (fuzzy match handles
  it), and their **"None"** placeholder entry is all zeros — leaving it in
  the pool made any string containing "none" (e.g. "nonexistent") resolve
  to a class of zeros, so it is filtered out. Substring matching is also
  one-directional on purpose: the typed name may be part of a real name
  ("summoner" → "Grand Summoner"), never the reverse, or any sentence
  mentioning a short class name resolves to it.
- Surfaced through `knowledge_search` like the other non-codex sources.
  Verified end to end: "що дає клас Duelist і скільки HP у Gilgamesh на
  AL 100 в PVP?" returned the modifiers, the passive, and the scaled HP.

### Amities and Crucibles (`orna_bonuses.py`)

Gear-bonus affixes: their tiers, roll ranges, and which equipment slots
each can appear on. **Checked before building anything that these were
genuinely missing** (asked for explicitly, and worth repeating for any
future "add source X" request): aussiescodex's own `codex.json` - which
`orna_aussies.py` already downloads in full - has nine categories and
neither of these is one of them; the community sheets mention "amity"
four times and "crucible" once, in passing; the reddit corpus discusses
them constantly but as prose, never as the numbers. So "what range does
the Defending amity roll?" or "which slots take an Avidity crucible?" had
no answer anywhere in the bot.

- Live counts: **184 amity blocks** (one per bonus per tier) and **45
  crucible rows**. Both pages are server-rendered, so plain HTTP + BS4
  works - no browser needed, unlike the Reddit crawl.
- **The crucible table's Bonus cell is a rowspan**, present only on a
  group's FIRST row. Without carrying it forward every continuation row
  loses the name of the bonus it describes, which is most of the table.
- **Some amities are legitimately rangeless** (Arch-Alchemy, The Hybrid
  are boolean effects), so parsing must not treat a missing range as a
  failure - an early version counted 145 of 184 as "incomplete" for
  exactly that reason.
- Flattened to `" | "` text rather than typed records, same reasoning as
  `orna_knowledge.txt`: irregular scraped data that a fuzzy search plus a
  reading model handles better than per-field parsing, and a page tweak
  then degrades to messier text instead of a crash.
- Caches per the shared convention (empty parse never cached - pinning "there
  are no crucibles" for a week would be worse than retrying); re-crawls on the
  1-week TTL, and `/update_codex` forces it alongside the codex/notes/sheets.
- Surfaced through `knowledge_search`, not a 19th tool - the prompt is
  ~30KB and this is another *provenance* of answer, not another question
  to ask. Verified end to end: "які слоти можуть мати crucible на avidity
  і який максимальний відсоток?" → all five slots, max 10%, cited.

### Prose corpora must not contradict the ported math - the tower-reset case

Two prose corpora landed on 2026-09-25 (`orna_mechanics.txt`, `orna_echo.txt`),
and auditing them against the repo's own pinned math (the `orna-game-mechanics`
skill's golden rule: live/verified code wins, and SAY SO rather than silently
correcting) found exactly the conflict that skill warns about.

**`orna_mechanics.txt` said the wild towers reset "weekly on a different day per
Titan". They do not.** `orna_towers` is a line-for-line port of OrnaCodex's
`tower.ts`, cross-checked under Node, and it pins `CYCLE_DAYS = 35`. Measured to
settle it: advancing the clock 35 days reproduces the current floors EXACTLY
(selene 19, eos 50, oceanus 44, themis 39, prometheus 34), advancing 7 days does
not. Live consequence before the fix: "when do the wild towers reset?" answered
with a fabricated weekday table ("Eos: Monday 00:00 UTC, Oceanus: Tuesday, ...")
2/2. Note such a table refutes itself - 7-day offsets would put every tower on
the SAME weekday, not consecutive ones.

**The first correction was itself wrong, which is the more useful lesson.**
Writing that the towers are "staggered five floors - seven days of phase -
apart" came from dividing 35/5, which is not how the offset works: the towers
are offset by `_BASE_FLOORS = [35, 30, 25, 20, 15]`, five floors, and at ~6
floors/day that is about **20 HOURS** apart, not a week. Measured from the code:
consecutive resets fall 20h/19h/20h apart, drifting across weekdays
(Thu/Sat/Sun/Sun/Mon between 01:04 and 20:04 UTC in one real cycle). The model
then amplified the stray "seven days" into the weekday table again. **Do not
paraphrase a formula into prose without computing the paraphrase** - derive the
number from the code and check it.

Three fixes, one per layer:
- The corpus states the 35-day cycle, the ~20-hour offset, the 2023-12-07 UTC
  anchor, and explicitly that there is NO weekday schedule.
- The **`towers` tool description** now says it is AUTHORITATIVE for any tower
  floor, cycle or reset timing and outranks guide prose - the same "a verified
  source beats a fan-maintained one" shape as the `releases`-overrides-
  `knowledge_search` rule. This is the durable half, because `orna_echo.txt` is
  scraped from someone else's site and its wording is not ours to police.
- Suite tier-0 `corpora-vs-towers` pins it, scoped to the `=== Wild Towers of
  Olympia ===` section of the corpus we maintain. A first version scanned both
  corpora by period-delimited chunk and false-positived on a `## Weekly
  checklist` bullet list where "weekly reset" belongs to the Astraltree and
  "Towers" sits in a different bullet - the wrong unit for a checklist.

Measured after: 3/3 runs correct, one of them calling `towers` for live floors
and computing time-to-reset from them.

**Also refined, in the same audit:** the skill's "there is NO Tier 11" gotcha is
right about CLASSES (verified: nothing in the codex carries a tier above 10, in
any of the nine categories) but was wrong as a flat answer. **★11 is a real
CONTENT tier**: ★11 dungeons/towers place Arisen Superbosses on floor 16 and
floor 25 (★10 only on the final floor), and a character at ★11 level 250 gets
DOUBLE the Godforging chances per run. None of that is in any structured source,
which is precisely why the prose corpora earn their place.

### The playerecho guide corpus - the only source that states a FORMULA

`orna_echo.py` / `orna_echo.txt` / `orna_scrape_echo.py`, added 2026-09-25 on
ask. Every other source describes RESULTS: the codex gives an entry's own
numbers and never a formula, the community sheets tabulate outcomes, the reddit
corpus has devs explaining things in passing. playerecho.com/orna's 37 guides
write the mechanics down - and the gaps they fill were real: before this the bot
had no source at all for "how is Ward capacity calculated" (only a dev Reddit
quote that Ward absorbs magic damage first), for Ascension altar costs, for
dungeon modes/cooldowns/godforging, or for per-EVENT tier gates and rewards -
`orna_calendar` knows only WHICH events are live, never their content.

- **The formulas live in `<pre><code>`, and a parser that reads only `<p>`/`<li>`
  silently drops every one of them.** First version captured "Base Ward is
  calculated from your stats:" and then jumped to the worked example, losing
  `Ward_Base = (HP + MP) / 2` entirely - the single most valuable line on the
  site. `<pre>` is collected, and its line breaks are PRESERVED (prose is
  collapsed) because a multi-line formula squeezed onto one line is unreadable.
- **The `<h1>` is outside `<article>`**, in the page's hero `<section>`, so
  `article.find("h1")` titled every block with the URL slug.
- **The site serves `Content-Type: text/html` with NO charset, so `requests`
  decoded UTF-8 as ISO-8859-1** and `×`/`→`/`★` arrived as `Ã`/`â`. That
  corrupted exactly the characters the formulas are made of. `_fetch_text` sets
  `resp.encoding = resp.apparent_encoding`, and `build_text` REFUSES to write a
  corpus containing mojibake sequences - a silently mis-decoded corpus is worse
  than a failed crawl. Both pinned in the scraper's `_demo()`.
- Enumerated from the site's own **sitemap**, not the four paginated index
  pages, so a pagination change cannot silently drop an article. `robots.txt` is
  `Allow: /` (checked 2026-09-25); the crawler identifies itself honestly and
  sleeps 1s between pages.
- **Searched at SECTION level** (`## Heading` blocks, 639 of them across 37
  guides), not line level - that is why it is a separate module from
  `orna_knowledge` rather than a 17th section in it. That corpus is tabular, so
  one row IS the answer; here the answer is a paragraph plus the formula it
  introduces, and returning just the line containing "Ward" would strip the
  formula two lines below. Same argument `orna_reddit`/`orna_guides` make.
  Heading and title hits are weighted above body hits (3× / 2×): a heading is
  what a section is ABOUT, a body word may be an aside.
- Surfaced through `knowledge_search` as a labelled `GUIDE MECHANICS /
  FORMULAS` block, **not a 19th tool** - the model already picks between 18
  actions and this is another provenance, not another question. Citations are
  per-ARTICLE URLs, not one vague site link.
- Cross-checked against the repo's own ported math, per the mechanics skill's
  golden rule: the guides' `Total multiplier = (1 + bonuses) × (1 + Ascension
  Level / 100)` AGREES with `orna_classes.scale`'s +1%/level. The Ward formula
  has no counterpart in the repo, so it is new knowledge rather than a conflict.
- Verified end to end: "how is ward capacity calculated in orna?" answers
  `(HP + MP) / 2` 2/2 via one `knowledge_search` call, and the suite's
  `ward-formula` case (tier 2) is 3/3 with the expected formula READ OUT OF THE
  CORPUS at run time rather than hardcoded.
- Overlap is deliberate and harmless: 8 of the 37 are class guides that
  `orna_guide_*.txt` also covers, from a different author. Two (`hoa-map`,
  `orna-vs-hero-of-aethric`) are about Hero of Aethric, the same studio's other
  game - kept for the same reason the reddit filter keeps "aethric", since the
  mechanics discussions cross over.

### Ornabook - a guide whose key tables are drawn in icons (2026-10-05)

`orna_scrape_ornabook.py` crawls the 10 chapter pages of book.cadelabs.ovh into
`orna_ornabook.txt` (60 articles). robots.txt is Cloudflare's content-signal
preamble with NO signal values and no Disallow - by its own rule, unset neither
grants nor restricts. Things a plain crawl gets wrong here, all pinned in
`_demo`:
- **The status-effect table is written in ICONS.** Header cells are
  `td_tmp.png`/`su_tmp.png`..., row labels `def.png`/`atk.png`, so the text alone
  is "-100% | -80% | +25%" - numbers with no meaning that a model would quote
  confidently. `_icon_label` decodes `[stat_]{t|d|s}{u|d}[_tmp]` into the game's
  own notation (`atk_tu_tmp` -> "T. Att ↑↑↑"), matching aussies' status names.
- **Empty cells are positional** (229 rows). `orna_scrape_echo._table_rows`
  drops them, which shifts every later number one column left; this scraper
  keeps them as "—". (The echo version still drops them - its tables have not
  been checked for this.)
- **Crawled per chapter page, NOT via mdBook's one-page `print.html`**: there
  the `<h1>`s are sections and repeated ids get "-1" suffixes that do not exist
  on the real pages, so every citation would point at a missing anchor.
- Formulas are MathJax LaTeX source; `_tex` turns them into plain math. A
  sub-heading carries its parent ("Dual-wielding > Example"), because the reader
  weights heading words 3x and "Example" alone says nothing.
- `orna_echo`'s reader was generalised to take a `path` (one parse cache per
  file) rather than copied - orna_echo's own output is byte-identical.

**The audit, and a lesson about auditing.** A first regex pass over the other
corpora labelled two already-covered facts as "new" (the hybrid "mean of Def and
Res" is in `orna_mechanics.txt` worded differently) - a pattern over exact
wording is a lossy stand-in for "is this fact covered", same lesson as set-name
substrings. Re-checked by READING. Result:
- **genuinely new:** the dual-wield BONUS formula `(1 + 0.65*B)^2` - the repo
  had only the REJECTED "50% for world bonuses" claim for that side, and this is
  an independent source agreeing with the guild's 0.65; raid "sanding";
  per-stat buff rows; Drakeblight's 500 cap (consistent with the dev's "small
  DoT" comment).
- **corroborated** by an independent source: buff tiers +25/+50/+100% and
  debuffs -20/-80/-100%, multiplicative stacking (1.25 x 1.5 = 1.875).
- **one unresolved CONFLICT:** T. Crit ↑↑↑ is +60% in Ornabook, +80% in the
  community sheet, and no dev comment settles it. Recorded in
  `orna_mechanics.txt` (the corpus we own) with an instruction to give both.
  Verified 2/2: the bot answers with both figures and says they disagree.

### Discord announcements -> Telegram, in Ukrainian (2026-10-05)

The user followed the official server's `#game-announcements` and
`#redeem-codes` (Discord's "Follow" on an announcement channel) into `#main` of
their own server, and invited their own bot there. No admin of the official
server is involved, which is why this exists instead of the earlier
`discord_search` idea (a bot cannot read a server whose admins did not invite
it; that tool was removed without ever being committed).
- **Only followed messages are relayed** - the `IS_CROSSPOST` flag (1 << 1).
  Ordinary chat in `#main`, and Discord's own "X followed this channel" notices
  (type 12 - the two messages that were there on setup), are never sent.
- **Redeem codes are never translated, by construction.** `protect()` lifts
  codes, inline code and URLs out BEFORE translation and leaves placeholders;
  the model never sees a code. `restore()` puts them back byte-for-byte as
  `<code>` (tap-to-copy), and appends any whose placeholder the model dropped -
  then a final check sends the English original rather than a translation that
  lost a code. Tested by mutation: disabling either half fails `_demo`.
- **The shared `_translate` pinned EVERY capitalised word**, which is right for
  /orna answers and wrong for prose: "Thank всім за гру", "Технічне
  обслуговування Server", "Happy свята". It now takes `pin=`; announcements pin
  only names that exist in the game data (`game_names`: Wyrmhunt, Fenrir,
  Arisen, Skeleton Key...) and only where capitalised. It also forbids Russian
  letters for Ukrainian and retries once - live it produced "экіпіровку". That
  letter check cannot catch Russian WORDS in shared letters ("время",
  "начали", even "potom" in Latin), which is a model-quality problem.
- **Which Telegram chats.** The Bot API cannot list a bot's chats, so they are
  tracked in gitignored `announce_chats.json`: on `my_chat_member` (added /
  removed), on any group message, and on any channel post. Privacy mode is OFF
  for this bot (`can_read_all_group_messages`), so a group registers the first
  time anyone writes in it. A channel is known only when the bot is added to it
  or a post appears there - and a bot must be a channel ADMIN with Post
  Messages to post at all. `my_chat_member` and `channel_post` had to be added
  to `allowed_updates`, or channels would never be seen. `/announcements on|off`
  for a group's admins; in a private chat it lists the registry for the owners.
  A kicked bot's chat is dropped (Forbidden); a group upgraded to a supergroup
  follows its new id (ChatMigrated).
- **First run starts from "now"** (cursor = newest message, nothing sent), the
  cursor advances per message after broadcast, and never past messages it
  could not read (IntentMissing) - those relay once the intent is fixed.
- **Message Content intent** blanks content/embeds/attachments together, and
  followed messages are authored by a WEBHOOK, so a check that skips bot
  authors is blind exactly here. `check_intent` looks at the followed messages
  themselves. Verified ON for real: the bot reads the follow notices' text.

### `monuments` - this week's Monument rewards (2026-10-05)

floorchart.top (by Truffles) is a GitHub Pages shell; the data is ONE public
Google Apps Script endpoint returning `{week, ithra, thor, vulcan, demeter}`,
each a grid: column 0 = floor, slots `Reward 1/2/3` (CATEGORIES: "Proofs",
"Materials"...), `Material` and `Potion` (the SPECIFIC item: "Adamantine",
"Nostrum"). Contributors enter it by hand weekly; `week` is the ISO week.
- **Abbreviations are expanded from the codex, never by rule.** The site's
  "P Runestone" is Perfect Runestone but "P Darkstone" is Pure Darkstone, and
  "C Ortanite" is Cursed Ortanite - a "P = Pure" rule is wrong for one of them.
  `_expand` finds the ONE codex item with that initial and base word; an
  ambiguous one is left as written (findable) rather than guessed (not).
- **Staleness is reported, not hidden.** A chart whose week is not the current
  ISO week is refetched even inside the TTL; if the site still serves last
  week's, the observation says STALE and tells the model to say so.
- **The "Reward 1/2/3" slot labels were read as quantities.** Live: "Reward 3:
  Proofs" became "each floor gives 3 proofs" and "Ithra gives up to 2" - from a
  chart with no quantities at all. Category cells are now shown without their
  slot label, the observation states there are no amounts, and a category
  query gets the only real count (floors per monument). Pinned in `_demo`.
- **A list of numbers is POSTED by the tool, not re-typed by the model.** With
  the exact floor list in its observation, the model still mis-copied a floor
  1 run in 3 (Ithra 11 for 9). Category queries now post a deterministic card
  (like `towers`) and the model answers in one line. Specific-name lookups
  ("where do I get adamantine") post nothing - 1-2 floors, correct every run.
  Measured after: 3/3 correct; the guarantee holds when the model asks by
  category (2/3 runs) - asking for the whole chart instead takes the
  observation-only path.
- Routing: 4/4 runs picked `monuments` first from the description alone.

### Rubenir's stat calculator - a cross-check of `estimate_stats` (2026-10-05)

"Orna Character Stat Calculator v0.7" (Google Sheets, built to reproduce the
in-game status screen). Its two DATA tabs (Classes: base stats + per-level
growth for 82 classes; Specializations: % modifiers + secondary bonuses) are in
`orna_scrape_knowledge._TABLES`, so they refresh with the other sheets. Its
Calculator tab is NOT crawled - its CSV is one default configuration's computed
numbers. Its value is the FORMULA chain, read via the Sheets API with
`valueRenderOption=FORMULA` and written up as prose in `orna_mechanics.txt`
("Character stat calculation").

What the cross-check found against the repo's own math:
- **Agrees:** PvP doubles HP only; Ascension +1%/level and it multiplies gear
  too; **28 of 32** shared specializations have identical % modifiers - an
  independent corroboration of `orna_classes.json`.
- **CONFLICT, open, and it matters:** the sheet applies a specialization's %
  to the class BASE only, before gear. `estimate_stats` applies it to gear too
  (`orna_classes.scale(gear_raw, class_mods, ...)`) - and that is an unpinned
  assumption: `scale()` being linear is a convenience, not evidence. ~4,160
  magic on a Sequencer loadout at AL 102. NOT changed - settle it on the status
  screen (equip one item, see whether the stat rises by exactly its value).
  The corpus tells the model to say the estimate may overstate gear.
- **CONFLICT, 4 specs:** Berserker, Brawler, Chronomancer, Time Mage differ.
  The repo's data is the NEWER source (the sheet lacks 8 later specs, e.g.
  Titanfelled), so the bot keeps it; the corpus says to give both.
- **New, no repo counterpart:** per-level growth for every class, world bonuses
  (Origin Town x1.1, Dukedom x1.05, Party x1.1) with Ascension ADDED to their
  product rather than multiplied, hybrid-CLASS Magic->Attack conversion
  (Beowulf 25%, Bahamut 20%), and the Swashbuckler formula - whose
  "bonus shrinks with Defense gained" matches the player Q&A's "keep defence
  below ~380".
- `orna_scrape_knowledge._clean_rows` drops empty cells, the same column-shift
  hazard as `orna_scrape_echo`. Harmless for these two tabs (the sheet writes
  "-" for an empty modifier; only the "None" row's blank tier shifts), but
  check it before adding a sheet with truly empty cells.

### The reddit developer corpus - searched by `knowledge_search`, not its own tool

Orna's devs answer mechanics questions on Reddit in detail that exists in no
codex page, community sheet or patch note. `orna_scrape_reddit.py` pulls
u/OrnaOdie's submissions + comments and u/Widogeist's comments into
`orna_reddit.txt`; `orna_reddit.py` reads it. Added 2026-09-24 on ask.
- **Reddit allows no anonymous access.** Verified 2026-09-24:
  `/user/<name>/submitted.json` returns **403** for any User-Agent (browser
  strings included), `old.reddit.com` **302s to a login page**, and
  `api.reddit.com` 403s too. Two routes, and the parser doesn't care which:
  read-only application-only OAuth (`grant_type=client_credentials` from a
  *script* app), or **`--from-dir`**, which builds from listing JSON already
  saved by a logged-in browser. `--from-dir` exists because app registration
  turns out to be gated behind Reddit's API-terms sign-up for some accounts,
  and it is how the committed corpus was actually built (a browser session's
  cookies paging `?limit=100&after=...`).
- **The widely-repeated ~1000-item listing cap does NOT apply to these user
  listings** - measured, both comment listings were still returning a fresh
  `after` cursor at 1200 and ran to 1330 / 1968. `MAX_PAGES` is a runaway
  guard, not a target; paging stops when the cursor goes null.
- **Real corpus shape** (2026-09-24): 3,416 items fetched -> **2,514 entries
  kept**, 1.5MB, spanning 2018-2026 (the bulk 2022-2024), median entry 254
  characters. Parses in ~11ms and searches in ~4ms, so no caching is needed
  beyond the module-level list.
- Two filters, both tuned against the real data rather than guessed:
  `_MIN_BODY_CHARS = 80` (was 120 - that discarded "Ward absorbs magic
  damage before HP does... That is intentional." at 105 chars, exactly the
  kind of statement this corpus exists for; losing signal beats keeping
  noise, since search ranks by word overlap and an acknowledgement never
  outranks an explanation), and `_SUBREDDIT_RE` (these devs also post in
  r/buildinpublic, r/SipsTea etc. - only ~1% of entries, but pure noise
  here; "aethric" is kept deliberately, as Hero of Aethric is the same
  studio and the mechanics discussions cross over).
- **Committed and re-run BY HAND, unlike the weekly sheets.** Reddit history
  is append-only and years old; re-crawling it weekly would spend a
  rate-limited budget re-fetching thousands of unchanged comments to learn
  nothing. The sheets are live documents, which is why they get a TTL and
  this does not.
- **Searched at ENTRY level, not line level** - that is the whole reason it
  isn't another section inside `orna_knowledge.txt`. That corpus is tabular,
  so one row IS the answer and returning the matching line is right. A dev
  explaining why orn bonus multiplies is a paragraph, and handing back only
  the line containing "multiplicative" strips the reasoning around it - the
  same argument `orna_guides.py` makes for keeping long-form guides whole.
- **`knowledge_search` searches both corpora; no 19th tool was added.** The
  model already picks between 18 actions and the prompt is ~30KB, which is
  the one thing this file has repeatedly seen it lose instructions to.
  "Community sheet" vs "what a dev said" is a distinction about the ANSWER's
  provenance, not about which question to ask - so it comes back as a
  labelled `DEVELOPER COMMENTS` block that the prompt says outranks the
  sheets, with an explicit caveat that a years-old comment may predate a
  patch and `releases()` should be checked before quoting a figure.
- A missing `orna_reddit.txt` disables the corpus cleanly (logged, empty
  results) rather than breaking `knowledge_search` for everyone - so a
  checkout made before the first scrape still works.

### `releases` - the only source that says what CHANGED

The codex and the community sheets both describe what IS. Neither ever
mentions what CHANGED, and `orna_knowledge.txt`'s sheets are
hand-maintained, so they can lag a balance patch by weeks - an answer built
from them can be confidently stale with nothing in the data hinting at it.
playorna's own patch notes are the one source that does hint it (e.g.
"Added 5% Ward Power bonus to each piece of the Judge Trifecta warrior
gear"), so the loop can read them and qualify an answer it would otherwise
state flatly. Added 2026-09-24 on explicit ask.

- `orna_releases.py` follows the shared cache convention (see "Things that
  aren't obvious"): gitignored dir, 1-week TTL, atomic write, unreadable =
  miss, and an empty parse is never cached (it would pin a silent "no patch
  notes exist" for a week if playorna's markup changed, so it raises instead).
- The page carries ~15 notes with no pagination, about three months at the
  observed cadence. That is the window where "did a patch change this?" is
  a live question, so there is nothing to page through - but it also means
  **finding nothing is not proof nothing changed**, which both the tool's
  miss message and its prompt description say explicitly.
- `search()` matches the whole query first, then falls back to any single
  significant word, because a patch bullet names things exactly ("Judge
  Trifecta Falx") while a question says "judge falx".
- Same no-`reply_text` shape as `knowledge_search`/`web_search`: raw
  changelog lines aren't something to show a user verbatim, the model reads
  them and writes the caveat itself. `asyncio.to_thread` for the cache-miss
  fetch, like every other data access in that file.
- `/update_codex` refreshes the notes too - the reason to run it at all is
  "a patch just landed", and refreshing one source but not the other is
  exactly the stale mix it exists to prevent.
- The prompt tells the model a note here OVERRIDES the fan-maintained
  knowledge base, while the codex itself is official and already current -
  so this mainly qualifies `knowledge_search`/`class_guide` answers rather
  than codex stats. Verified live: "чи варто брати Judge Trifecta для
  воїна? чи були зміни?" called `releases` and cited the real 1.334
  +5% Ward Power change in its answer.

### The chat shows the ANSWER, not the loop's browsing (2026-09-26)

Reported after the Judge Trifecta run: correct behaviour, unusable output. The
loop opened twelve codex entries and posted a full card for each, so the answer
arrived at the bottom of a wall of reasoning artefacts the user had to scroll
past. The ask was explicit - data stays in the model's context, the user sees the
answer clearly and opens codex entries only if they want to.

- **The browse tools record instead of posting.** `open_entry` fetches with
  `post=False` (fetch + digest, render nothing), and `search_codex`/`query`
  record their result entries rather than posting a results card.
  `_remember_entries` collects them on the session, deduped by url and capped at
  `_MAX_VIEWED_ENTRIES = 40` so a 50-row query cannot build an unusable
  keyboard. **Every observation is byte-identical**, so the model's context and
  reasoning are untouched - this is purely what lands in the chat.
- **A SMALL lookup posts its card again (2026-09-30).** Deferring every card
  also took the rich entry view away from the most common request there is -
  "what is this item" - which came back as prose plus a link ("now the codex
  tool returns only the final description and a button"). `finish()` now posts
  the full card - description, the `pre_table` of stats, effects, and the
  `Dropped by` / `Immunities` / `Upgrade materials` cross-link sections plus the
  Aussie Codex link (all plain text since 2026-10-01, see below) - for up to
  `_AUTO_CARD_MAX_ENTRIES` (2) entries, above the
  answer so the answer still lands at the bottom. Entries the loop actually
  READ win over ones a search merely LISTED (`open_entry` marks its entry
  `opened`), so the one-item lookup shows that item and not every near-name
  hit. A browse that touched a dozen entries is unchanged - still the quiet
  button. Inline mode is excluded (one text message, no chat to post into), and
  a card that fails to send is logged and skipped: it must never cost the
  answer it introduces. The button lists only what was NOT carded.
- **Cross-link sections and the Aussie Codex link became plain text, not
  buttons (2026-10-01).** The bot is in a ~180-person guild chat, and every
  tappable button is a dead click some member will eventually make - the
  per-section button (`Dropped by (3)`, `Used in (12)`, ...) posted a WHOLE
  NEW message listing the names once tapped, and with 180 potential tappers
  that's exactly the "bloating the chat" the ask named. `_format_entry` now
  inlines every section's entry names directly under the stats table
  (`<b>Dropped by:</b> name, name, ...`, capped at 100 per section like
  `_run_open_entry_tool`'s own digest, "(+N more)" rather than silent
  truncation) and the Aussie Codex link renders as a plain `<a href>` instead
  of a `url=` button - Telegram opens a plain link directly with no bot
  reply involved, so it costs nothing to show or tap. `_send_entry` no longer
  builds a `reply_markup` for an entry card at all (`_section_buttons`
  itself is untouched and still used by the unrelated `events()` roster
  buttons, which weren't part of this ask). Observations are unchanged -
  `_run_open_entry_tool`'s digest already carried every section's names as
  text for the model; only what posts to the chat changed.
- **`finish()` carries one "📄 Записи кодексу (N)" button.** Tapping it posts the
  paged list, and tapping an entry there goes through the EXISTING `open`
  callback and renders the identical card - no second rendering path to keep in
  step. A button TAP still posts immediately, because there the card IS what was
  asked for.
- Measured on the reported request: **17 steps, 13 entries read, ONE user-visible
  message** (the answer; the ephemeral status line deletes itself). Before: a
  dozen cards plus the answer. A simple lookup is likewise one message.
- **This partly reverses an earlier explicit preference, flagged rather than
  silently overwritten.** `query` results were once link-only buttons and were
  changed to render richly in chat "once it was clear having the stats actually
  visible in the chat (not just a link to tap through to) was the valuable part".
  The stats are still visible in chat - tapping an entry posts the same full card
  - they are one tap away instead of automatic. If that trade turns out wrong for
  browse-shaped asks ("show me the Last Martyr set"), the fix is to post the
  results card when the request IS a browse and keep deferring it when the loop
  is doing internal lookups, not to revert wholesale.
- **What still posts:** computed deliverables, because they ARE the answer - the
  `assess` stat table, `estimate_stats`, `today`/`next` reports, `towers`,
  `build_optimize`, `need`. The line is browse artefacts vs computed results.
  Pinned in `_demo`: a spy message stub asserts `open_entry` and `search_codex`
  send NOTHING, that a repeat read does not duplicate the button entry, and that
  the recorder is capped.

### `finish()` must not restate a card it just posted (2026-10-02)

Live report: `/orna yelmogus great staff` auto-posted the codex entry card (full
stats, per the section above) and then `finish()`'s own text restated the same
numbers in prose below it - the user saw the answer twice. The card-posting
mechanism existed, but **nothing in the prompt ever told the model it happens**
- from the model's point of view it was simply answering a question about an
item's stats, which looks exactly like a case needing numbers restated.

- **`_TOOLS_TEXT`'s `finish()` description** and **`_REASONING_RULE`'s
  "before finish()" checklist** both gained an explicit line: a 1-2-item codex
  lookup auto-posts its full stat card right above the answer, so `finish()`
  must not re-list those stats/effects/tags - just answer the actual question
  in one short sentence.
- **That was directly CONTRADICTED by an existing rule** - the CONTEXT FORMAT
  section said "a number you rely on must be restated [in finish()], since tool
  results are internal and the user never sees them." True for every other
  tool, false for a card that just posted to the SAME chat. Fixed with an
  explicit EXCEPT clause naming codex item stat cards.
- **Even after fixing the contradiction, a prompt-only rule did not reach
  100%** - re-verified over several `orna_loop_harness.py` runs of the exact
  report: restating was clearly REDUCED (shorter, partial repeats) but not
  eliminated on every run. Consistent with this file's own repeated finding
  elsewhere (`_close_out`, `MAX_ASKS_PER_REQUEST`, the earlier "reason twice"
  rule) that a prompt instruction squeezed into the same call that is also
  picking an action is a reduction, not a guarantee - which is what motivated
  actually building a real REVIEW step instead of a third prompt patch (next
  section).

### PLAN-WORK-REVIEW-RELEASE - genuinely separate PLAN and REVIEW model calls (2026-10-02)

Design ask: whether a formal PLAN/WORK/REVIEW/RELEASE framework would improve
on the bare ReAct loop, prompted directly by the stats-duplication bug above -
a prompt rule telling the model to "reason twice" (plan before the first tool
call, re-check before `finish()`) already existed (`_REASONING_RULE`), and it
was demonstrably unreliable: outvoted by a contradicting rule in the very same
prompt, and still inconsistent even after the contradiction was fixed. Both
PLAN and REVIEW are squeezed into the SAME JSON call that is also picking an
action for that turn - so "reasoning" and "the thing being reasoned about"
share one call, with no second opinion. Answers confirmed on ask: PLAN is
**one upfront extra model call** producing a plan before any tool runs
(not another "thought" field); REVIEW is **a genuinely separate call** after a
draft `finish()`, which can send the draft back for more WORK; the extra
latency is an accepted tradeoff; and both are scoped to **complex requests
only** - a short single-item lookup should stay exactly as fast as before.

- **`_looks_complex_request(text)`** is a cheap heuristic, deliberately NOT an
  LLM classification call (that would spend the very latency this gate exists
  to protect simple lookups from): a long request (>=20 words), >=2 quality/
  level markers (a multi-item loadout each carrying its own "185%"/"lv10"),
  >=2 comma/"and"/"та"/"і"-joined clauses (several named things at once), or a
  keyword match for the genuinely multi-step tool shapes (estimate/build/
  compare/loadout/optimi[sz]e/"порахуй мої стати"/"мій білд"). Thresholds lean
  generous on purpose - a false positive just costs one extra PLAN+REVIEW call
  on a request that turns out simple, never a wrong answer.
- **PLAN (`_call_plan_model`)** runs once, right after a fresh session is
  created (`_maybe_plan`, wired into `handle_orna` only - NOT the ask()/
  `/clarify` resume path, since that is a continuation of work already
  planned, not a new request; and NOT inline mode, which is already
  latency/complexity-constrained and has no reply channel to show a slow
  answer in anyway). It is handed the request and the tool list, asked to
  name every explicit constraint and a short ordered plan, and the result is
  injected as one `[SYSTEM NOTE]` into `session.messages` before the loop's
  first step - so it sits in context like any other note, not as a special
  code path the main loop has to know about.
- **REVIEW (`_call_review_model`)** runs when `action == "finish"`, BEFORE
  anything is posted - no card, no reply, nothing user-visible yet - so a
  "redo" verdict costs nothing beyond the model call itself. It is given the
  original request, a condensed transcript of the evidence gathered so far
  (reusing `_working_state`, the same transcript the main loop's own step
  calls already see - no second extraction to maintain), the draft answer,
  and whether a codex card is about to auto-post over it; it returns one of:
  - `"approve"` - draft posts unchanged;
  - `"revise"` - REVIEW rewrites the answer itself (used for wording-only
    problems, e.g. the draft repeats numbers a card already shows, or is
    needlessly long) - no new claims beyond what the evidence already
    supports, so this cannot introduce new invented facts;
  - `"redo"` - REVIEW sends the draft back with concrete feedback naming what
    is missing/wrong and which tool would fix it; the feedback is appended as
    a `[SYSTEM NOTE]` and the loop `continue`s to make more tool calls, same
    as any other mid-loop correction.
- **Deliberately NOT given the main loop's own system prompt or
  `session.messages` as its context** - that would hand REVIEW the very
  instructions that let the draft go wrong in the first place. It gets a
  short, independent brief instead, which is the whole point of a SEPARATE
  call: a fresh read with nothing else to do but check.
- **Bounded to `MAX_REVIEW_ROUNDS = 1`** - same "always replies, never hangs"
  guarantee `_close_out`/`MAX_ASKS_PER_REQUEST` already give the rest of this
  file. A second `finish()` attempt skips REVIEW entirely and posts, so one
  redo round is the absolute worst case added to a request's latency.
- **Fails open everywhere, by design**: a failed PLAN call (timeout, bad
  JSON) just means no plan note was injected - the loop proceeds exactly as
  it would have before this existed. A failed REVIEW call is treated as
  `"approve"` of the model's own original draft. Neither can make the loop
  reply LESS reliably than it did before.
- **Verified live** via `orna_loop_harness.py` (now wired with the same
  `_maybe_plan` call `handle_orna` makes, so it exercises the real path):
  `"balor sword"` -> `use_plan_review=False`, unchanged plain ReAct, 3 steps.
  A genuinely complex multi-constraint `estimate_stats` request (class + spec
  + AL + PVE + two named, quality/level-qualified items) -> `use_plan_review=
  True`, one REVIEW round spent, two `finish` attempts in the action trace (a
  redo), and the final posted answer was a corrected, complete stat summary -
  a real example of REVIEW catching and fixing a draft the plain loop would
  have posted as-is.
- Tier 0 (`orna_test_suite.py`) re-run clean after this change - 100%, no
  regressions.

### `sql` — the codex as a database, and why SQLite (2026-10-04)

Design ask: the codex tool read `codex.json` and built search criteria against
it, so it could not answer an aggregation ("how many items are in the codex").
MySQL vs MongoDB was the question; the answer is **SQLite**, because on the
ask's own two criteria it wins both — reads dominate (an in-process query beats
a localhost round-trip) and there is nothing to be consistent about (2.4MB,
5,080 records, ZERO writers, a derived cache of someone else's dump refreshed
weekly). It also does the third requirement better than either: MongoDB allows
**one text index per collection**, which is a hard ceiling on "full-text search
by any field", while FTS5 gives per-column matching, prefixes and BM25.
Measured: parse 14ms, in-memory filter scan 5.4ms, DB build 0.29s, GROUP BY
0.34ms. Full comparison and the "if you still want MySQL" path: `README-db.md`.

- **The bug under the ask was NOT storage, and it was reporting a cap as a
  total.** `query_records` returns `results[offset:offset+limit]`, and
  `_run_query_tool` reported `len(matches)` — so "how many items can mages
  use" answered **50** (the cap) instead of **1598**. That is the
  observation-honesty rule's exact failure, in the one place it was never
  applied. Fixed in two layers: `orna_aussies.count_records` (true total +
  optional `group_by`, sharing ONE scan with `query_records` via
  `_matching_records`), surfaced as `query`'s `count`/`group_by` args; and the
  listing path now reports the real total with an explicit `PARTIAL` marker,
  paying for the second scan only when the cap was actually hit.
- **Schema shape is forced by the data**: 37 top-level fields (most absent on
  most records) and **157 distinct stat keys**, so `stats(rid, field, value,
  raw)` is long/narrow — any stat filterable/sortable/aggregatable with no
  schema change when the game adds one — plus the whole record in a `json`
  column for `json_extract`. `links` carries every `[category, id]` edge WITH
  the target's name joined in, which is what makes "the stats of what this raid
  drops" one query instead of N lookups.
- **`terms` is the table that did not exist anywhere before.** Statuses,
  buffs/debuffs and class abilities are NOT codex records — they live only in
  `translations.en.json` and appear on records as bare codes. So "how many
  statuses exist" / "what does Life Siphon do" had no answer in any source.
  846 rows: 312 statuses, 134 abilities WITH descriptions, 156 stat names, and
  every enum. Materials are records (`item_type='material'`), pets are
  `category='followers'` — both already covered, and `_demo` now pins that
  every KIND of codex thing is both filterable (indexed WHERE) and searchable
  (findable by its own name through FTS), because those three are the ones
  easy to miss.
- **Read-only is enforced by the ENGINE, not the prompt** — the connection is
  `file:...?mode=ro`, so no model-written statement can write whatever the
  prompt says. Plus: one statement only, SELECT/WITH only, a 5s
  progress-handler abort (a cartesian join over 5,080 records would otherwise
  hang the thread), and a row cap whose truncation is reported as `PARTIAL`.
  All pinned in `_demo`, including that the connection itself refuses a CREATE.
- **A tool description was NOT enough to get it called, exactly as this file
  predicted.** Live, 3/3: "how many items are in the codex?" finished with a
  confident **"2773"** and an EMPTY action trace. The number was RIGHT, which
  is the dangerous part — it came from the model's memory of a public site, not
  the database, and a stale one would have looked identical. Same shape as
  `class_guide` being 0/3 before `_CLASS_GUIDE_RULE`. The fix is CODE, per the
  standing "close it in the tool, not the prompt" rule: `_forced_evidence_note`
  sends a HOW-MANY/total/average question back ONCE when it reached `finish`
  with zero tool calls. Deliberately narrow (`_AGGREGATION_RE`, EN+UK) so it
  cannot fire on a pleasantry or a capability question, where no tool call is
  correct; one-shot and fails open, so it can never cost a reply. It sits
  BEFORE the REVIEW block because REVIEW is gated to complex requests and this
  question is 7 words. Measured after: 3/3 call a tool first — and the model
  picks `query count` for the simple count and `sql` (schema first, then a
  correct GROUP BY and a JOIN+AVG) for what `query` cannot express.
- **`/update_codex` rebuilds the DB** in the same command that refetches the
  dump it is derived from — refreshing one and not the other is the stale mix
  that command exists to prevent. Atomic (temp + rename), and a build
  producing under 1000 records REFUSES to replace a good DB, so a failure
  leaves the previous one serving.
- **`query_records`/`count_records` now RUN on SQL** (they delegate to
  `orna_codex_db.query`/`.count`; the in-memory scan stays as the fallback if
  the DB file is missing or unreadable). This reverses an earlier decision in
  this very section not to migrate `_eval_condition` - the stated reason was
  that every branch in it exists because of a documented live bug, so a hand
  port would re-litigate all of them. **That reasoning was wrong about which
  way the oracle cuts**: the in-memory evaluator being present is exactly what
  makes the port verifiable, by running both over all 5,080 records and
  requiring identical results.
- **The port splits in two, and only one half is new code.** VOCABULARY
  resolution is reused verbatim (`_resolve_stat_field`, `_resolve_attr_field`,
  `resolve_codes`/`_parse_buff_query`'s "T Mag 3" -> `t__mag_uuu` shorthand,
  `_USEABLE_BY_ALIASES`) - that is where most of the subtlety lives. Only the
  COMPARISON semantics became SQL, which is what the differential covers.
- **The differential found FOUR real bugs in the port**, 156 mismatching
  conditions out of 4,220 comparisons on the first run, and every one would
  have shipped looking plausible:
  * a top-level `hp` MIRRORED into the `stats` table "so ORDER BY hp works
    across categories" - which silently redefined `{stat hp > 100}`, since the
    in-memory kind reads ONLY `record["stats"]` and a boss's 3,000,000 must not
    match. Top-level values get their own `hp_num`/`price_num` columns instead;
  * the attr -> `stats[original_field]` fallback dropped: `{attr hp = 10}`
    matched 20 items in memory and 0 in SQL;
  * the label branch taken BEFORE that fallback: `{attr element = fire}` was
    91 vs 0. `_resolve_attr_field("element")` fuzzy-resolves to **"events"** - a
    wrong resolution - and the in-memory answer is only correct BECAUSE the
    fallback rescues it, so the port had to reproduce that order exactly;
  * the character-array shape: `element` is `['f','i','r','e']` on **288
    items** (and `["fire"]` on 354 spells). That branch had been dismissed here
    as defensive and unreachable. It is neither - 91 vs 63. Normalised at build
    time in `_stat_raw` so no query path needs to know.
- **ONE deliberate deviation, asserted so it stays deliberate**: a condition on
  a field that resolves to NOTHING matches EVERY record in memory (the bool-flag
  branch reading absent-as-False, so "items where bogus_field = false" -> all
  5,080). The SQL layer fails closed. Unreachable either way -
  `unresolvable_condition_fields` refuses such a query upstream.
- **A trimmed differential is PINNED in `orna_codex_db._demo`** - one case per
  condition kind plus the four regressions above, each stating whether it
  expects hits (a case matching nothing in BOTH paths agrees vacuously and
  would keep "passing" after the data shifted under it). The full ~4,200
  comparison matrix is a scratch script, not committed.
- **Behaviour CHANGE, not a parity bug**: grouping a count by a list field
  (`tags`/`events`) now returns per-value counts (`found_in_chests: 382`) where
  the in-memory version returned one key per list COMBINATION
  (`"['found_in_shops', 'found_in_chests']"`). The SQL shape is the useful one,
  but it is a change in output, not a fix.

### Running it anywhere — `install.sh`, `Dockerfile`, `docker-compose.yml`

- **The image exists for the three SYSTEM binaries `pip` cannot provide**:
  tesseract **with Ukrainian traineddata** (the OCR runs `lang="eng+ukr"` and
  degrades silently to English without it, which reads as "the bot ignored my
  screenshot"), ffmpeg, and yt-dlp — all at fixed absolute paths passed in by
  env var, since this repo's launchd history is several outages' worth of "a
  binary was not on a minimal PATH". The build FAILS if any of them, or the
  `ukr` language data, or FTS5, is missing — loudly at build time rather than
  on a user's first screenshot.
- **No database service in compose, and that is the point** — a MySQL/Mongo
  service would mean a second container, a volume, credentials, a healthcheck
  and startup-order retries for a 2.4MB read-only dataset read by one process.
- **The repo is bind-mounted (`.:/app`) rather than state being volume-mounted
  per file.** The state files (`reminders.json`, `usage_stats.json`,
  `amities.json`, `nicknames.json`) are written with temp-file + `os.replace`,
  which REPLACES the target — so a single-file bind mount or a symlink gets
  silently eaten on the first write, and persistence would look fine until the
  first reload. A whole-directory mount is the one shape that works without
  refactoring every module's paths. That bind mount also means
  `codex.sqlite3` DOES persist on the host (it is written to `/app`), which an
  earlier version of this note wrongly denied - harmless either way, since it
  is gitignored and rebuilds in ~0.3s.
- **Secrets only ever at runtime** (`env_file`), never an image layer, and
  `.dockerignore` excludes `.env*` — this repo has already leaked a committed
  `.env` once (see `.gitignore`'s own note).
- **Ollama is its own container with the model BAKED IN** (`Dockerfile.ollama`),
  not a profile and not the host's. The point is migration: a new host needs
  Docker, the repo and `.env`, with no Ollama install and no model pull, so the
  first request cannot race a 13GB download. Cost is an image as large as the
  model. Three things to know:
  * **`.env`'s `OLLAMA_MODEL` is an MLX build** (`nemotron-3.5-lightning:30b-mlx`)
    and MLX runs ONLY on Apple Silicon - it cannot run in a Linux container at
    all. So compose pins `OLLAMA_MODEL` to the baked `gpt-oss:20b` (the repo's
    own code default, portable GGUF, and the model every harmony/tool-calling
    measurement in this file was taken against) rather than inheriting it.
  * `OLLAMA_HOST` is set in compose to `http://ollama:11434`, overriding
    `.env`'s localhost. An earlier version left this at
    `host.docker.internal` while also shipping the ollama service, so the
    profile started a container the bot then ignored.
  * the baked models live in the image; the `ollama-models` volume keeps
    anything pulled LATER. Docker seeds a NEW named volume from the image, but
    an existing one SHADOWS it - so remove the volume after changing which
    models are baked.
  * `pkill -f "ollama serve"` ends the build-time server. The pull's exit
    status fails the build, so a bad model name is caught at build rather than
    at runtime.
- `install.sh` is the native path (macOS/brew or Debian/apt), idempotent,
  `--no-deps` to skip system packages. It never overwrites an existing `.env`
  (that would lose a working bot token) and ends with a real verification pass:
  imports, tesseract languages, the video binaries, and a record count out of
  the codex DB.
- **NOT VERIFIED: neither image has ever been built** — the Docker daemon was
  not running on this machine, and the Ollama image was prepared for a future
  host migration rather than tested. `docker compose config` validates and
  resolves both services, and `install.sh` parses; `docker build` has not run
  once. Build both before relying on either.

### The ephemeral status message (shared by `/orna` AND `/go`)

A `/orna` request can legitimately run for minutes (`MAX_STEPS = 16`, plus
the `LOOP_TIMEOUT_SECONDS` ceiling), and the chat was previously silent for
all of it except whatever tools happened to post - no way to tell a working
request from a stuck one. `_Status` sends ONE message on the first update
("🤔 Думаю…"), EDITS it in place for each step ("🔎 Шукаю в кодексі…",
"📚 Читаю гайд…", from `_ACTION_LABELS`), and DELETES it when the request
ends, so a finished conversation reads exactly as it did before this
existed. Added 2026-09-24 on ask. Each step's line also shows the tool's key
ARGUMENT (`_status_detail`): "🔎 Шукаю в кодексі… «Fallen King Centaurus»",
"🔎 Підбираю за характеристиками… «followers, orn_bonus»" - so the user (and an
admin reading over their shoulder) sees WHAT is being looked up, not just that
something is. `research`/`estimate_stats` got their own labels here too (they
fell to the generic "⏳ Працюю…" before).
- **Edit one message, never send per step.** A line per step is precisely
  the scrollback spam that dead-end tool messages already had to be removed
  for (see the loop's session notes above).
- **`_advance` owns its whole lifetime**, creating it and clearing it in a
  `finally`. That is what stops it being orphaned by the timeout path
  (which cancels `_advance_inner` mid-step), by an `ask` that returns to
  wait on a button, or by an unexpected exception. Verified for both the
  normal and the timeout path.
- **Every Telegram call in it swallows its own errors** and `update()`
  no-ops when the text is unchanged - a status line must never cost the
  answer, or an extra API call per step for the same string.
- An action with no label falls back to a generic "⏳ Працюю…" rather than
  leaking the internal action name.
- **`_Status` lives in `telegram_go.py`, not where it was written.** `/go` got
  the same line on ask 2026-09-26, and `/orna` imports from `/go` while `/go`
  imports nothing from `/orna` - so that is the acyclic home for the shared
  class, and duplicating it was the wrong answer. `/go`'s labels are its own
  (`_ACTION_LABELS` keyed on its `_ACTIONS`, English, since `/go` is not the
  guild-facing command) and its steps are the SLOWEST in the bot - a `youtube`
  action downloads and re-encodes a video - on the one feature built for slow
  plane wifi, where silence is indistinguishable from a hang. Same
  own-its-whole-lifetime rule: `/go`'s `ask` returns from inside the loop to wait
  on a button, so the status is created in `_advance` and cleared in a `finally`
  around the extracted `_advance_steps`.

### `finish()` carries a "📚 Джерела" button - what the answer was actually built from

**Update (2026-10-01): the button itself is gone.** With the bot added to a
~180-person guild chat, every tappable button is a dead click some member
will make, and each tap posts a brand-new message - "it's bloating the chat"
(explicit ask). `session.sources`/`_add_source` keep running exactly as
before (every tool below still cites what it read), but `finish()` no longer
turns that list into a button, and nothing writes into `_SOURCES`/responds to
the `orna|src|...` callback anymore - the citation bookkeeping lives on
in-session only, nothing user-facing reads it right now. The rest of this
section (what gets cited, and why not everything) is the still-accurate
history of why the mechanism exists, kept for whenever a quieter way to
surface it (e.g. folding into the answer text) replaces the button.

Every tool that reads something with a URL appends `(label, url)` to
`OrnaSession.sources` (`_add_source`, deduped by URL, capped), and `finish()`
attaches one button when that list is non-empty; tapping it posts the
citations as `url=` buttons. Modelled on `/go`'s own source links, per
explicit ask 2026-09-24. What gets cited, and why not everything:
- **`knowledge_search`** cites the Google Sheet **and tab** each matched
  section came from, not one vague "the knowledge base" link.
  `orna_knowledge.source_url` builds that map from
  `orna_scrape_knowledge._TABLES` (where the ids already live, so there is no
  second copy to drift); the key is the section title exactly as the
  generated file writes it, `"<title> (<source note>)"`, which is what
  `search()` already prefixes each block with. Importing that scraper is
  side-effect free - its work is behind `if __name__ == "__main__"` - and a
  failed import degrades to "no link" rather than breaking the tool.
- **`web_search`** cites Tavily's own `sources` list, which
  `telegram_go._tavily_search` already returns as `{title, url}`.
- **`open_entry`** cites the codex page it READ, and **`search_codex`**
  cites the pages it surfaced - but only the top `_MAX_CITED_PER_SEARCH`
  (3) of them (`_cite_entries`), because a result LIST is weaker evidence
  than a page actually read and one loose search can return 50 rows, which
  would crowd out the sheet/web citations that actually answered the
  question. Dedupe by URL means the usual search→open_entry pair cites the
  opened page once, not twice.
- `_SOURCES` is keyed by sid but kept OUT of `_ORNA_SESSIONS`, which is
  pruned after `SESSION_TTL_SECONDS` (15 min) while a posted answer stays in
  the chat forever - tapping the button an hour later should still work
  (**now dead code since the 2026-10-01 removal above** - kept rather than
  torn out, since restoring the button only needs the two lines back).


### `research` - one-call supergraph, `codex.json`-first

The loop used to gather a subgraph one entity per tool call: to answer "what
does Fallen King Centaurus drop and which class benefits from those items?" it
`open_entry`'d the raid (1 network call), read the six drop names, then
`open_entry`'d each of the six items (six more network calls) for their stats -
~7 network round-trips and ~7 LLM steps, which blew the step/wall-clock budget
so the bot **failed to answer a question whose data it already had on disk**.
`research` (`_run_research_tool` → `orna_aussies.build_supergraph` +
`_render_supergraph`) returns the whole subgraph in ONE call so the model
reasons over everything on the first iteration - fewer paid LLM calls, and no
"answer assembled datum-by-datum" degradation (the design ask, 2026-09-27; see
`docs/superpowers/specs|plans/2026-09-27-orna-research-supergraph*`).

- **It is built entirely from the local `codex.json` + `translations.en.json`,
  zero network.** The dump holds every entity BY ID (including event raids like
  Fallen King Centaurus), and every cross-link edge (`raids.drops`,
  `monsters.skills`, `items.dropped_by`/`upgrade_materials`, `spells.used_by`/
  `learned_by`) is a list of `[category, id]` pairs. `build_supergraph`
  resolves a name → `(category, id)`, walks that category's default edge set
  (`_DEFAULT_EXPAND`) one level, and joins each target to a compact leaf
  (`_leaf`: name, `useable_by`, place/item_type, tier/rarity, stats, effects).
- **Records carry NO `name` in `codex.json` - names live in
  `translations.en.json`** (the same reason a name-grep of the dump "misses"
  an entry that is really there). `resolve_entity` therefore builds a reverse
  `name→[(cat,id)]` index over `display_name`, falls back to `fuzzy_codex_name`
  for a typo/transliteration, and for a name that spans categories (200 do,
  e.g. "aaru cobra" is a monster AND a follower) returns the priority-ordered
  pick (`_CATEGORY_PRIORITY`, fightable-thing first) with the rest in
  `alternatives` - surfaced in the observation, never a silent mispick and
  never a second round-trip.
- **Effects are humanized via `translations['status']`** (`t__crit_u` → "T.
  Crit ↑", `blind` → "Blind"), falling back to the raw code so an unknown one
  degrades to text rather than crashing.
- **Generous caps, and truncation is NEVER silent.** Every leaf shows ALL its
  stats/effects (real items have up to 18 stats / 21 effects; a silent per-leaf
  cap quietly made "which class benefits" answers wrong). `per_relation_cap`
  defaults to 80 - larger than the biggest real relation (a 76-drop raid) - so
  it effectively never truncates; when a ceiling IS hit it is marked `PARTIAL`
  with the true total (`_names_observation`'s rule) and the whole-observation
  cut (`_RESEARCH_CODEX_MAX`, 40000) happens only at a LINE boundary
  (`_truncate_lines`), never mid-value - a "244" clipped to "24" is a WRONG
  number, worse than a marked-short list. A dangling edge target degrades to an
  empty leaf, no crash. **The caps are ceilings for the 256k-context model, not
  tight budgets: populating the context with what the model needs to reason is
  the goal - do NOT re-introduce a stingy cap.** The same principle was applied
  across the corpora feeding `knowledge_search`/`web_search`
  (`_KNOWLEDGE_OBS_MAX` 40000, per-corpus content caps raised ~4-6x, web_search
  snippets 200→1200) - see the observation-honesty note below.
- **The knowledge half reuses `_gather_knowledge`** - the exact 7-corpus
  aggregation `knowledge_search` uses, extracted so there is no second copy -
  driven by the resolved subject, so a "how do I beat X" `research` call
  carries the Monster-Data elemental immunities and **satisfies
  `_STRATEGY_RULE` in one call** (the prompt says to prefer it there over
  `knowledge_search` + N `open_entry`).
- **Posts no per-entity cards** (same `post=False` spirit as `open_entry`
  today); the touched entities are recorded on `session.viewed_entries` so
  `finish()` offers buttons to open any of them. Multi-entity requests bundle
  every subject in one observation (`args.entities`, or a comma/"and"/"та"
  split of `action_input` - but the WHOLE string is resolved first and split
  only if that fails, so a real name that itself contains a comma or "and"
  ("Arisen Thor, the Storm God", "Sword and Shield") is not shredded into a
  wrong item plus an unresolved half).
- **Added alongside** `open_entry`/`search_codex`/`query`, not replacing them -
  they still serve browsing and single lookups; `research` is the prompt's
  DEFAULT for analytical/comparative/"how to beat" questions.
- **`# ponytail:` phase-2 gaps** (deliberately not built): `bestial_bond`
  (follower spell/bond grants, a nested list-of-tiers not `[cat,id]` pairs) is
  not expanded; depth is fixed at 1; there is no playorna fallback for an
  entity genuinely absent from the dump (it is reported unresolved instead).
  Pinned by `orna_aussies._demo` (the centaurus raid: ≥6 drops each with stats
  + useable_by, ≥6 skills, the cap/PARTIAL and ambiguity cases) and Tier-0
  `research-supergraph` in `orna_test_suite.py`.

### `knowledge_search` and `web_search` - the codex genuinely doesn't know everything

Both tools exist for the same gap: playorna's codex + aussiescodex's
`codex.json` are complete for an entry's own facts/stats/effects, but
**a boss's elemental damage immunities/resistances are not tracked
anywhere in either data source at all** - verified directly (not just
assumed) by inspecting a real boss record end to end and finding no such
field, empty or otherwise. Live report that surfaced this: `/orna як
вбити Лицар Сіріус?` ("how do I kill Knight Sirus") - this boss is immune
to nearly every element except one, information no codex fact captures,
but exactly the kind of thing a wiki or forum documents. `_STRATEGY_RULE`
in the loop's system prompt makes calling `knowledge_search` (and
`web_search` if that doesn't help) **mandatory**, not optional, for any
"how do I beat/kill X" or "what's X weak to" question - specifically
because a codex page that *looks* complete (facts, stats, sections, all
present) is exactly the case where a model would otherwise reasonably
assume it already has enough to answer, and a live-verified failure
confirmed that: skipping straight to `finish` with only codex facts
produced a confidently wrong "no known weaknesses, just hit it hard"
instead of the real answer. `knowledge_search` is tried first (free,
instant, pre-vetted community data, no external API), `web_search` is the
fallback when it doesn't have the answer either.

- **`knowledge_search`** (`orna_knowledge.py`, reading
  `orna_knowledge.txt`) is a static, generated reference built from 4
  user-provided Google Sheets (`orna_scrape_knowledge.py`'s `_TABLES`
  list): 2 tabs of a "Gear XP/Orn/Gold Boosts + Combat Mechanics Notes"
  spreadsheet, 12 tabs of the community "Ornapedia" spreadsheet (Badges,
  Boost Items, Buildings, End of Gauntlet Items, Monster Data, Pets/
  Followers, Proofs for Materials, Raid Rewards, Skills/Spells, Titles,
  View Distance, XP/Leveling), and 2 tabs of an orn-bonus-calculator
  spreadsheet (Main, GearInf) whose numbers/formulas back the
  `_AGGREGATE_RULE` quality-scaling math (see below). **Deliberately
  flattened into plain `" | "`-joined text lines under `=== Title ===`
  section headers, not typed per-sheet schemas** - these are
  human-maintained community wiki sheets with inconsistent columns
  sheet-to-sheet, not clean data, so a generic substring/fuzzy search
  (`orna_knowledge.search`) that a model reads and interprets itself is
  the right fit, not bespoke parsing code per sheet - same
  "tool retrieves, model interprets" split `web_search` and
  `orna_calendar.fetch_events` already use. **The bot refreshes this itself on a 1-week TTL**
  (2026-09-24, on ask): `_corpus_text()` rebuilds from the live sheets via
  `orna_scrape_knowledge.build_text()` - the SAME builder the script uses,
  extracted for exactly this so the corpus format can't drift from the
  parser reading it - and caches into `.knowledge_cache/` (gitignored),
  matching `orna_aussies`/`orna_releases`. Three things to know:
  - **The committed `orna_knowledge.txt` is now a FALLBACK, not the live
    source.** Order: fresh cache -> rebuild from sheets -> stale cache ->
    committed file. Every fetch failure is a warning, never an exception, so
    a Google outage degrades to the last good copy rather than taking the
    knowledge base down. Verified by simulating one: still 16 sections,
    search still answers.
  - Still worth re-running `python3 orna_scrape_knowledge.py` and committing
    occasionally, precisely BECAUSE that file is the safety net - left alone
    for a year it becomes a very old one.
  - **A rebuild is validated PER SECTION against the committed copy before
    it is cached** (`_sane_rebuild`), because the realistic failure is
    Google returning HTTP 200 with one tab truncated or empty while the
    other 15 are fine - and a rejected rebuild that got cached would answer
    from a gutted corpus for a WEEK. A whole-corpus line ratio is far too
    coarse for that: emptying the LARGEST tab costs only ~9% of the total
    lines, which a 90% check waves straight through (measured, which is why
    the first version of this guard was replaced). So: every committed
    section must still exist, and one holding more than
    `_SMALL_SECTION_LINES` may not come back with under
    `_MIN_SECTION_RATIO` of its lines. A rejected rebuild is never cached,
    so the bot keeps serving the last good data and the failure shows up in
    the log. If the sheets ever legitimately shrink past this, rebuilds keep
    being rejected until someone re-runs the scraper and commits - the
    fallback then becomes the new baseline. That is deliberate: a human
    confirming a big shrink beats the bot silently accepting one.
  - A rebuild is ~16 HTTP requests (one per tab). Every caller reaches
    `search()` through `asyncio.to_thread`
    (`telegram_orna._run_knowledge_tool`), which is what keeps that off the
    event loop - don't add a caller that skips it.
  The TTL exists because these sheets are hand-maintained and drift
  independently of any game patch - NOT because drift was measured here.
  Checked 2026-09-24: a fresh `build_text()` was byte-identical to the
  committed file, so the corpus was exactly in sync at that point.
  `/update_codex` refreshes this too (its slow leg, so it runs last).
  **Correction worth keeping, because it nearly became a false "the sheets
  are drifting" conclusion in this file:** `len(text)` counts CHARACTERS
  while `os.path.getsize()`/`ls` count BYTES, and this corpus holds ~1,162
  multi-byte characters (★, emoji, Cyrillic) - so the same content reads as
  305,263 one way and 306,425 the other. Compare corpora with a real diff
  (or compare like with like), never a length against a file size.
  `search(query, section="", limit=20)` tries an exact case-insensitive
  substring match first, then retries once with each query word
  fuzzy-corrected against the corpus's own ~3500-word vocabulary
  (`difflib.get_close_matches`, cutoff 0.75) - the same fix for
  transliteration drift ("Sirius" vs the game's actual "Sirus") that
  `search_codex`'s own retry chain and `orna_aussies._resolve_stat_field`
  already use elsewhere in this codebase.
- **`web_search`** reuses `telegram_go._tavily_search`/`TAVILY_API_KEY`
  directly - same API key, same call shape, no second Tavily client.
  Unlike every other tool, it never posts its own `reply_text`: raw
  search results aren't trustworthy/structured enough to show a user
  verbatim the way a codex result is, so the model reads the returned
  text as an observation and writes the real answer itself in `finish()`
  - same pattern `/go`'s own "search" action already uses.

### `assess` - projecting an item's stats from a name + quality instead of a screenshot

`/orna assess <item name> <quality>` (e.g. `/orna assess arisen aaru robe
Legendary` or `...185%`) runs the exact same projection math the
screenshot-upload flow uses (`orna_assess.get_assess_result` +
`telegram_assess._format_response`) but starting from a typed quality
instead of OCR'd observed stats - added after a live ask asked for both a
name-based version of `/assess` AND confirmation that quality names
("ornate") and raw percentages ("195%") both actually work.

**Building this surfaced a real, previously-unknown production bug that
affects the screenshot flow too, not just this new tool.** The first
version pulled stats from `orna_codex.lookup_by_name`'s playorna-HTML-
scraped `CodexEntry` (the same path the screenshot flow already used) and
got back an **empty stats dict** for "Arisen Aaru Robe" even though the
item genuinely has stats - per explicit correction mid-session
("instead of looking into codes, assess tool better check codex.json"),
investigated instead of assumed away: **playorna migrated every page fact
(not just Tier/Rarity/Place, which `_harvest_meta` already handled) from
`<div class="codex-stat">` markup to `<dl class="entry-facts"><dt>Label
</dt><dd>Value</dd></dl>`, and `orna_codex.parse_codex_html`'s stat
extraction was never updated for it** - silently returning `stats={}` for
every item's combat stats since that migration, on the screenshot flow
too. Fixed at the source in `orna_codex.py` (kept the old `div.codex-stat`
path as a first attempt - verified it now matches nothing live - added a
`dl.entry-facts` fallback via `dt.find_next_sibling("dd")`; verified
against a live "Arisen Aaru Robe" page: previously empty, now correctly
`{'defense': 151.0, 'resistance': 191.0, 'mana': 80.0, 'ward': 4.0,
'adornment_slots': 4}`).

Even with that scraper fixed, `_run_assess_tool` pulls stats from
**aussiescodex's `codex.json`** (`_aussies_record_to_codex_entry`,
`orna_aussies._codex()`), not from `orna_codex.CodexEntry` - a second,
structural reason beyond the scraper bug: **some stats (e.g. an item's
own Orn Bonus) render on playorna's page as a free-text "effect" bullet,
never as a structured fact/dt-dd pair at all**, so no amount of HTML-
scraper fixing can reach them - verified directly (Dark Mage Hood's Orn
Bonus never reaches a scraped `CodexEntry.stats`, confirmed present in
aussies' `stats` dict). `codex_search` (playorna's own ranked name
search, already proven reliable everywhere else in this module) is still
used for name resolution only, since aussies' own name matching is a
plain, ambiguity-prone substring - the category+id from that search then
indexes straight into `codex["main"][category][id]`.
`_aussies_record_to_codex_entry` derives the `is_adornment`/
`is_accessory`/`is_celestial_weapon`/`is_upgradable`/`has_scaling_slots`/
`boss_scaling` flags `orna_assess` needs from aussies' clean `place`/
`item_type`/`rarity` enum-like fields instead of regex-matching scraped
page text - more reliable for everything except `is_two_handed`, which
aussies doesn't expose as a flat field at all and is hardcoded `False`
(documented `# ponytail:` gap in the function - only affects a celestial
TWO-HANDED weapon's adornment-slot count, narrow enough not to chase
until it's actually reported).

`_parse_quality_spec` accepts either a raw percentage (`"185"`, `"185%"`)
or a named tier, via two lookup tables:
- `_QUALITY_NAME_TO_PERCENT` (broken=50, poor=90, regular/normal=100,
  superior=101, famed=120, legendary=140, ornate=171) - each tier's own
  LOWER bound as a representative %, since there's no single canonical
  "the" percentage for a bare tier name; `_run_assess_tool` notes this
  assumption in the reply rather than leaving it silent.
- `_FORGED_LEVELS` (masterforged=11, demonforged=12, godforged=13) -
  Masterforged/Demonforged/Godforged are really upgrade **levels** past
  10 in Orna's own mechanics, not a quality percentage (`get_quality_code`
  derives the quality bucket from level once past 10) - quality defaults
  to 100% for these, an item assumed pushed to the tier's floor rather
  than some arbitrary higher %.

A **pre-existing quirk in the ported `orna_assess.py`** (not introduced
by this work, not modified - assumed intentional port fidelity):
`get_assess_result`'s displayed "(Quality Name)" label always derives via
`get_quality_code(quality, 1)` - level hardcoded to `1` regardless of the
actual requested level - so a Masterforged/Demonforged/Godforged request
(level 11-13) always displays as "(Regular)" even though the underlying
stat projection is correct. `_run_assess_tool` resolves this
transparently rather than leaving it misleading: when the input was a
named tier, it inserts a "Запитана якість: X" line ahead of the
auto-derived (and known-wrong-for-this-case) label.

Bonus-type stats (orn/exp/gold/luck bonus, ...) aren't part of the core
10-stat upgrade table `_format_response` renders, but scale with quality
via the same official formula (`scaled = ((100 + base) * (100 +
scaling) - 10000) / 100` - the identical formula `_AGGREGATE_RULE`
documents for the loop's own multi-slot build-optimization reasoning, see
above) - `_run_assess_tool` appends a "Бонус-статистики" section computed
via `get_quality_bonus()` for any `QUALITY_CODE_BONUS_KEYS` the item
actually has, since that's exactly the number a "best build" bonus
question needs and the core table alone wouldn't surface it.

### `towers`, `compare`, `build_optimize`, `class_guide` - four tools added after surveying OrnaCodex's own source

All four came out of one research pass through `github.com/67au/OrnaCodex`
(a different, more feature-complete Orna codex web app) explicitly looking
for ideas worth adopting, plus a direct ask to port its wild-tower math.

**`towers` (`orna_towers.py`) is a line-for-line port of OrnaCodex's
`src/utils/tower.ts` (pinned at commit `4201d034`), not a reimplementation
from a description - and it was cross-checked against the ORIGINAL
TypeScript's actual output, not just read and trusted.** Orna's 5 "Wild
Towers of Olympia" (Selene/Eos/Oceanus/Themis/Prometheus) each climb over
a fixed 35-day UTC cycle, gaining floor(s) at 6 fixed checkpoints per day
(01:00/05:00/10:00/15:00/15:36/20:00 UTC) plus a +6 jump at every day
boundary, wrapping/capping near the top into floor 50 - a sentinel meaning
"cleared, waiting for the next cycle," not a literal value from the raw
`(... % 35) + 15` formula. This needs **no external data at all** - not
the live game, not a spreadsheet - it's pure deterministic math from the
current UTC time, which is exactly why it's a good target for a from-
scratch port: the checkpoint times, the +6-per-day term, and the
floor-50 wraparound rule are all non-obvious game-specific constants a
plausible-looking guess could easily get subtly wrong. Verification: the
*actual* `tower.ts` (types stripped only, otherwise unmodified) was run
under Node against 9 fixed timestamps (including a cycle-epoch instant, a
day-boundary jump, and a floor-50 wraparound) plus a 2-day
`getTowerFloorsInNextDays` projection - `orna_towers._demo()` pins the
Python port's output against those exact real outputs, so a future edit
that silently diverges from upstream fails loudly instead of just looking
plausible. `_run_towers_tool` needs no LLM args at all (posts all 5
towers' current floor + the next floor-change checkpoint) - cheap enough
to always report everything and let the model read what was actually
asked about.

**`compare` is modeled directly on OrnaCodex's own Compare feature
(`src/stores/compare.ts`) - assess-then-diff, not a raw side-by-side stat
dump.** Reading that file surfaced the actual design worth copying: it
doesn't compare items' raw base stats (barely meaningful pre-upgrade for
gear) - it runs each item through the SAME quality/level projection an
assess does (defaulting to quality 200%/level 13, i.e. "fully forged" -
copied directly, since a comparison is normally about a build's ceiling,
not one arbitrary quality), then diffs every later item against the
FIRST one in the list across the union of both items' stat keys.
`_run_compare_tool` reuses `_resolve_aussies_entry` (factored out of
`_run_assess_tool` for this, see below) + `orna_assess.get_assess_result`
- the exact same pipeline `assess()` already has, just run once per item
(2-6 items) instead of once. A non-scaling item (a material, a flat-stat
accessory `get_assess_result` returns `levels=0` for) falls back to its
raw `entry.stats` rather than an empty row, so it's still comparable on
whatever it does have.

**`build_optimize` replaces `_AGGREGATE_RULE`'s old LLM-orchestrated
per-slot recipe with deterministic Python, for exactly the question
shape that recipe was built for** ("max orn bonus across every slot") -
before this, answering that reliably needed a dedicated `calculate` tool
PLUS a long worked-example prompt section, and still cost several loop
steps and real risk of the model mis-tracking a number across turns.
`_run_build_optimize_tool` does the per-slot `query_records` lookup, the
official quality-scaling formula, and the multiplicative stack across
slots natively in one call - reusing `orna_assess.get_quality_bonus`
(the SAME formula `_run_assess_tool`'s own bonus-stats section already
uses, not a second implementation of it) and defaulting to the standard
7-slot loadout (head/weapon/off-hand/torso/legs/accessory×2 - Orna has
two accessory slots) when the model doesn't specify one. Only meaningful
for `QUALITY_CODE_BONUS_KEYS` stats (orn/exp/gold/luck bonus and similar
%-stacking stats) - a raw combat stat like magic/attack doesn't stack
across slots the same way, that's `query`'s `sort_by` or `compare`'s
job. `_AGGREGATE_RULE` keeps the old manual per-slot-query+calculate
recipe as an explicit fallback for a case `build_optimize`'s fixed shape
doesn't cover, rather than deleting it outright. Verified: a live rerun
of the exact "max orn bonus across every slot" question this session's
`_AGGREGATE_RULE` was originally built to answer independently arrived
at the same ~160% total this file's earlier manual-verification pass
already confirmed by hand - a real cross-check, not just "it ran without
an exception."

**`class_guide` reads long-form written community guides
(`orna_guides.py` / `orna_guide_<topic>.txt` ×8 / `orna_scrape_guides.py`)
for a SPECIFIC named class/build** - Summoner, Realmshifter/Thief, Deity,
Gilgamesh, Beowulf, the Swash build (usable by any class, not a class
itself), Heretic, plus a Towers of Olympia mechanics/rewards guide
(unrelated to the `towers` tool's live floor-height math - same name,
two different things, called out explicitly in both the prompt and
`orna_guides.py`'s own docstring to head off exactly the confusion the
old `orna_calendar.py` filename collision caused elsewhere in this
file). **Deliberately a separate per-topic-file system, not more
sections in `orna_knowledge.txt`**: that corpus is short community-wiki
FACTS (a tier/HP/resistance row IS the complete answer, hence one
flattened blob fuzzy-searched as a whole), where these guides are
long-form REASONING (why a build works, tradeoffs between two setups) -
grepping a fragment out would lose the argument around it, so the right
unit to hand the model is "the whole guide for the class this question
is about," selected by name via `orna_guides.resolve_guide` (exact/alias
match, then a difflib fallback for typos), not searched piecemeal.
Guides run from ~10KB to ~180KB (Summoner) of prose - `_run_class_guide_tool`
returns a `query`-focused excerpt (matching lines + surrounding context)
when given one, or just the guide's opening otherwise, capped at 6000
chars either way; unlike a fact lookup this has no `reply_text` of its
own, same as `knowledge_search`/`web_search` - it's source material the
model reads and writes the real answer from in `finish()`.

**Live-verified prompt gap, fixed before this shipped**: the first
version of `class_guide`'s tool description alone was NOT enough - a
direct loop run for "дай пораду по білду для класу thief" skipped the
new tool entirely, instead running a plain `query`-based gear search
plus the model's own general training knowledge, and finished with a
generic "glass cannon, max attack" answer that never touched the actual
curated guide. Same failure shape `_STRATEGY_RULE` already exists to
prevent for boss immunities (a plausible-looking answer from general
knowledge, when a specific tool exists precisely because general
knowledge isn't reliable here) - fixed the same way, with a new
`_CLASS_GUIDE_RULE` making `class_guide` MANDATORY at least once before
`finish` for any class/build strategy question naming one of the 8
topics. Verified 3/3 across different phrasings (English and Ukrainian)
after the fix, where the first attempt had already shown it doesn't
happen reliably on its own.

**`class_guide`'s excerpt is TAB-AWARE, and the model must keep the
game-mode word - a same-named build in a different tab was a live bug.**
Live report: `/orna show codex items for raid heretic using omniflask
build` returned the Early-T10 "Omniflask Raiding" build's gear instead of
the Raids-tab "Omniflask Weakness" the user meant. Two compounding causes,
both fixed: (1) the excerpt picker - extracted out of
`_run_class_guide_tool` into the stdlib-only, unit-tested
`orna_guides.guide_excerpt` - scored `=== Build ===` headers by plain
substring and returned only the single top scorer with its `--- Tab ---`
header stripped, so the query word "raid" matched "Raid**ing**" and beat
the Raids-tab build. It's now hierarchy-aware: a query word naming a
`--- Tab ---` ("raid" -> the "Raids" tab) RESTRICTS the search to that
tab, so it can't collide with a same-named build in another tab, and ties
(a bare "omniflask") return every top match each prefixed with its tab
header for the model to pick from. (2) the model kept reducing
"raid ... omniflask" down to just "omniflask", dropping the disambiguating
word - the `class_guide` tool description now tells it to keep the
game-mode/section word (raid/dungeon/tower/early/...) IN its query.
Verified 5/5 end-to-end (real cloud model) after both, 0/3 before.

**Build/`class_guide` answers must be in the USER's language - they were
always coming back Ukrainian, even for an English question.** Live report,
reproduced 3/3: an English "give me build advice for the heretic omniflask
raid build" answered in Ukrainian. Cause is model-layer: the general
"reply in the same language the user wrote in" rule was outweighed, for
this path specifically, by `_CLASS_GUIDE_RULE`'s leading Ukrainian example
(`"дай пораду по білду для класу thief"`), the prompt's other Ukrainian
`finish` examples, and the long ENGLISH guide excerpt the model reads
right before finishing. Softening the prompt (a stronger top-level LANGUAGE
rule noting the examples are illustrative, plus a LANGUAGE line in
`_CLASS_GUIDE_RULE`) HELPED but was NOT reliable - re-tested, the exact
heretic-omniflask-raid question still came back Ukrainian about half the
time, because counter-instructing against examples the model is also
imitating is a weak lever. The robust fix is DETERMINISTIC: `_orna_system_
prompt(user_text)` now detects the script the user actually wrote in
(`_CYRILLIC_RE` -> "Ukrainian", else "English" - this guild writes only
those two) and prepends a per-request "CRITICAL LANGUAGE LOCK: ... MUST be
in <lang>" line, so the required language is stated as fact rather than
inferred from examples. `handle_orna` passes the request text in (the loop
harness does too); the prompt-guidance changes stay as reinforcement.

**The lock has to cover the prompt's own EXAMPLES, or it loses to them -
again.** Live 2026-09-25: `/orna calculate my stats` (English) came back in
Ukrainian, with every class name mistranslated into words that resolve to
nothing. The CLARIFICATION rule's example ask text was a hardcoded Ukrainian
string sitting exactly where the model composes its question - the same fight
`_CLASS_GUIDE_RULE` already lost. `_orna_system_prompt` now builds that
example in the language the lock just demanded, so imitation helps instead of
hurting. The lock also states that PROPER NAMES ARE NOT TRANSLATED: items,
classes, specializations and spells keep their English spelling inside a reply
in any language, because they are identifiers and a translated one matches
nothing in the data. Verified 3/3 English answers English and 3/3 Ukrainian
answers Ukrainian with the class names left in English.
Verified: English build questions answer in English, Ukrainian ones stay
Ukrainian.

### English-first: translate at the edges, reason in one language

Design ask 2026-09-26 - "a lot of text is in Ukrainian and I think it can make
the bot understand context worse". The loop used to reason in whatever language
the user wrote, enforced by a per-request CRITICAL LANGUAGE LOCK. Every knowledge
source it has is ENGLISH (codex, aussies, the sheets, the guides, the dev corpus,
the player Q&A) and so is every rule in the prompt, so a Ukrainian request made
the model reason across a language boundary on every step - reading English
evidence to write Ukrainian thoughts. Now there are two gates and one interior
language:

- **Input gate** (`build_loop_messages`): a non-English request is translated to
  English for the loop, and **the original is kept alongside it verbatim** -
  "prefer THIS spelling for any item/material/class name you pass to a tool".
  That matters because `need`'s extraction and every codex name lookup run on
  those tokens, and a translator is exactly the wrong thing to have touched them.
- **The loop is English, always** (`_LOOP_LANGUAGE`). The lock inverted: every
  thought, tool argument, ask and finish is English. This also DELETED a whole
  failure mode - the prompt's own examples used to have to be rebuilt in the
  user's language, because a Ukrainian example sitting next to an
  answer-in-English instruction kept winning (CLAUDE.md has two separate
  incidents of that). With one language there is nothing to imitate wrongly.
- **Output gate**: the finished answer (and an `ask`) is translated back, and
  ONLY when it is not already in the target language - the fixed
  capability/reminder replies the prompt tells the model to copy verbatim are
  Ukrainian, and re-translating them would both cost a call and paraphrase text
  that is deliberately deterministic. `ask` OPTIONS are never translated: they
  are matched back by exact text when tapped, and several are proper nouns.
- **Proper nouns are pinned by ENUMERATION and then CHECKED, because the rule
  alone does not hold.** The first live Ukrainian answer rendered the class
  `Duelist` as "Дулїст" - unresolvable in the data, the same failure the old lock
  existed for. `_proper_nouns` extracts the capitalised identifiers from the
  source, the prompt lists them as "must appear EXACTLY as written", and the
  result is verified; a dropped name triggers ONE retry naming it. Measured
  after: 4/4, 2/2 and 1/1 identifiers kept across three real answers.
  Subtlety worth keeping: an extracted name must be VERBATIM in the source or
  "keep this exactly" is unsatisfiable - stripping common words from the middle
  turned "Altar of Ascension" into "Altar Ascension", which appears nowhere and
  would have burned a retry on every single translation. Edges only.
- **Both gates degrade to the untranslated text on any failure** rather than
  raising: a slightly-wrong-language answer beats no answer. Cost is two extra
  model calls on a non-English request, one per gate.
- **Both harnesses go through the input gate now** (`orna_test_suite.
  run_case_once`, the skill's `orna_loop_harness`). Building the session by hand
  would test a path production no longer takes, and `ukrainian-lock` would fail
  for the wrong reason. Verified after: `ukrainian-lock` 2/2 (Ukrainian in,
  Cyrillic out, class names left English) and `heretic-build` 2/2 (English in,
  English out).

### Inline mode (`@<bot> <query>` in any chat, incl. groups the bot isn't in)

`handle_inline_query` + `handle_chosen_inline_result` route an inline query
to the SAME loop as `/orna`, so the bot can answer in groups it was never
added to. The design is shaped by two hard Telegram constraints:

- **Inline queries fire on every keystroke**, so `handle_inline_query` does
  NO work - it just returns one cheap placeholder `InlineQueryResultArticle`
  ("🔎 <query> ⏳ обробляю…"). The real loop runs ONCE, when the user picks
  that result, in `handle_chosen_inline_result`.
- **An inline answer is ONE editable text message** (the bot can't stream
  several messages, or send photos, into a chat it isn't a member of). So
  the loop runs against `_InlineSink` - a stand-in "message" that COLLECTS
  every `reply_text` (dropping photos/keyboards, `__getattr__` no-ops the
  rest) instead of posting - and the joined text is `edit_message_text`'d
  into the inline message via its `inline_message_id`. `_advance(...,
  with_status=False)` skips the ephemeral `_Status` message (there's no live
  chat to put it in). Long answers are truncated to 4096 chars; malformed-
  after-truncation HTML falls back to a tag-stripped plain edit.

Two BotFather settings are REQUIRED and can't be done from code: `/setinline`
(enable inline at all) and `/setinlinefeedback` -> 100% (so the
chosen-result update, which carries the `inline_message_id` we edit, is
delivered - it's only present because the placeholder result has an inline
keyboard). `telegram_bot.main()` also passes an explicit `allowed_updates`
including `inline_query`/`chosen_inline_result` so they're always polled. The
typed-clarification (`ask`) follow-up can't work inline (no chat to wait in);
if the model asks, the question itself becomes the answer and the user
re-invokes with more detail.

## Things that aren't obvious from reading one file at a time

**Handler registration order is load-bearing.** `telegram_bot.py` registers
`build_assess_conversation()` before `build_resource_conversation()`. Both
are `ConversationHandler`s and PTB only runs the first one in a group whose
`check_update` matches. The assess conversation's `AWAITING_NAME` state
photo/text handlers only match when *that* conversation is actually active
for the chat; when it isn't, it correctly falls through to the resources
conversation. Don't reorder these without re-checking that interaction.

**PTB's `concurrent_updates` defaults to `False` - every update from every
chat is processed one at a time, globally, regardless of which chat it's
from - and this bit the whole bot in production, not just `/orna`.** Live
incident (2026-09-23): a single complex `/orna` request (which can
legitimately run for minutes - `MAX_STEPS = 16`, `LOOP_TIMEOUT_SECONDS =
300`) blocked EVERY other command from EVERY user - including a trivial
`/res_today` sent by a different user right after it - until the loop
finished or timed out. Before the ReAct rewrite this was never really
exposed, since no handler in the bot ran anywhere near that long; once
`/orna` could legitimately take a few minutes, PTB's sequential-by-default
processing turned one slow user's request into a full outage for everyone
else. Fixed in `telegram_bot.main()` via
`.concurrent_updates(32)` on the `ApplicationBuilder` - a modest bound
(not PTB's max-256 default for plain `True`) that keeps most concurrent
activity naturally isolated to different chats/users without going
unbounded. The tradeoff, and why 32 rather than `True`/unbounded: PTB's
own docs warn concurrent processing risks a race in stateful
`ConversationHandler` flows (the assess/resources conversations) if the
*same* chat sends two messages close together mid-flow - a real but
narrow risk, far outweighed by "the whole bot hangs for minutes" being
the default otherwise.

**Enabling `concurrent_updates` is necessary but not sufficient - a
genuinely blocking synchronous call inside a handler coroutine still
stalls the entire single-threaded asyncio event loop, for every chat,
concurrency setting or not.** `concurrent_updates` only helps PTB dispatch
multiple *update handlers* without waiting on each other's `await` points
- it does nothing for a handler that calls something synchronous and
blocking directly (a network fetch, a disk read, a CPU-bound scan) without
wrapping it in `asyncio.to_thread`. Found and fixed two of these live in
`telegram_orna.py` during the same incident's investigation, both already
noted in their own docstrings but worth having in one place: `_run_assess_tool`'s
`_aussies_codex()` call (can trigger a synchronous network fetch on an
aussiescodex cache miss) and `_run_knowledge_tool`'s
`orna_knowledge.search()` call (a synchronous disk read on first call, plus
a `difflib`-based fuzzy-correction pass on a ~3500-word vocabulary on a
miss) - both now wrapped in `asyncio.to_thread(...)`. When adding a new
`/orna` tool (or any handler) that touches the filesystem, an external
HTTP call not already wrapped by an async client, or any CPU-heavy loop,
wrap it in `asyncio.to_thread` - this class of bug won't show up in quick
manual testing (a single request looks fine), it only surfaces as
"everything hangs" once two real users' requests overlap in production.

**The shared cache convention (aussies / releases / bonuses / knowledge) -
one rule, four copies.** Every scraped/fetched source: keeps a gitignored disk
cache on a TTL (1 week, except `orna_calendar`'s 6h), writes it atomically
(temp then rename, so a `launchctl` reload mid-write can't leave a torn file),
treats an unreadable cache as a miss, and - the load-bearing part - **NEVER
caches an empty or partial parse** (that would pin "there is nothing here" for
the whole TTL). `orna_aussies` additionally sanity-checks the dump before
committing, and `/update_codex` is transactional (see its section). When adding
a fetched source, follow this; each module's own section below notes only what
is specific to it (its TTL, its sanity check).

**`"think": False` was the actual cause of `gpt-oss:20b`'s flakiness, not
the model itself.** This section used to warn that the same input to
`telegram_nlp.extract_resources` could return the right answer on one call
and an empty list on the next. Root-caused during the `/orna` work
(2026-09-22): with `"think": False` in the `/api/chat` payload, this
model/quantization returns empty or truncated-mid-reasoning content
instead of the requested JSON *close to 100% of the time* under repeated
testing - not occasional flakiness, a near-total failure rate. Switching to
`"think": True` was 100% reliable across the same repeated tests,
including free-text Ukrainian input: Ollama separates the reasoning out on
its own and `content` comes back as clean JSON. The tradeoff is a bit more
latency per call (the model actually thinks now), which has been an
acceptable trade so far. This is now enforced in exactly one place,
`ollama_client.chat_json` (unconditionally sets `"think": True` - every
caller across `telegram_nlp.py`, `telegram_go.py`, and `telegram_orna.py`'s
loop goes through this one function, so there's no longer a second copy to
forget to fix). `orna_material_names_uk.json`'s static EN↔UK table is used
by `telegram_offerings.py` specifically (OCR'd offerings-screen rows), not
by `extract_resources` — don't assume it's a universal fallback underneath
every LLM call in this repo. The general principle still holds even with
the fix: prefer a deterministic lookup wherever one is feasible, and keep
the LLM for genuinely fuzzy natural-language parsing (`telegram_orna.py`'s
ReAct loop is a good example of leaning on it appropriately once it was
actually reliable).

**`gpt-oss:20b` answers a "pick one named action" prompt with a NATIVE
tool call about half the time, even though this repo never declares any
tools - and that arrives as an EMPTY `content`, not an error.** Measured
live 2026-09-24 against the real `/orna` system prompt: 11/20 local calls
came back with `message.content == ""` and the real choice sitting in
`message.tool_calls` (e.g. `{"name": "knowledge_search", "arguments":
{"action_input": "Knight Sirus"}}`). This is gpt-oss's Harmony format
asserting itself: the loop's prompt *is* a tool-choice prompt, so the model
expresses it the way it was trained to, and Ollama then logs `harmony
parser: no reverse mapping found for function name` (there is no mapping -
no tools were sent) and, less often, returns a bare **500** instead. This
was the real cause behind both the recurring `Ollama returned non-JSON
content: ''` warnings and the user-visible "Ollama request failed: Server
error '500'" replies. Two halves, both needed:
- **The model's decision is correct in these replies, just in the wrong
  field**, so `ollama_client._from_tool_calls` translates a tool call back
  into the action dict rather than discarding it. Fixed in `chat_json`, the
  one function every caller routes through, so `/go` and `telegram_nlp`
  get it too. `arguments` IS the intended object; when the action name
  lives in the call's `name` instead, leftover keys are that action's own
  args and get nested under `"args"` (a `query`'s conditions/category/
  sort_by arrive flat). Pinned by asserts in `ollama_client._demo()`
  against the three real shapes - run `python3 ollama_client.py`.
- **Prompt wording alone cannot close this** (measured: 9/20 → 14/20 with
  an explicit "you have no callable functions" rule, 17/20 also
  de-function-ifying the tool bullets - never 20/20), so the rule was in
  `_orna_system_prompt` as a cheap reduction, not as the fix. After both:
  **39/40** usable actions across two batches, vs 9/20 before. The residual
  was Ollama's own 500 - see the next paragraph, which removes it.

**The bare 500 above is PREVENTED by declaring the action names as
`tools`, not recovered - and the fix is the opposite of what the symptom
suggests (it is not a bad model, and swapping models fixes nothing).**
Live report 2026-09-24: a long multi-item `/orna` request died at step 11
with `Ollama request failed: Server error '500'`, throwing away ten steps
of gathered data. `~/.ollama/logs/server.log` named the cause exactly -
every 500 is preceded by `harmony parser: no reverse mapping found for
function name` with the action the model picked
(`harmonyFunctionName=search_codex`). gpt-oss:20b DOES support tool
calling (`/api/show` → `capabilities: ['completion','tools','thinking']`);
the bug was that the loop declared NO tools while prompting the model to
pick one of 17 named actions, so when Harmony emitted the call it wanted,
Ollama had nothing to map the name back to. Measured over one day's
server log: 91 such warnings → 27 bare 500s (~30% of them; the other 70%
come back as an ordinary `tool_calls` reply `_from_tool_calls` handles).
The fix is `telegram_orna._STEP_TOOLS` - the 17 action names declared in
`/api/chat`'s `tools` array, passed through the new `tools=` parameter on
`ollama_client.chat_json`/`chat_json_with_fallback`. Verified live: 8
probe calls on the failing prompt with nothing declared → warnings and
tool-call replies; the same 8 with the names declared → **0 warnings, 0
500s**, and a full 16-step local-only run of the exact failing request →
0 warnings, 0 500s. Notes:
  * `_ACTIONS` is now the single list the prompt's action enum AND
    `_STEP_TOOLS` are both built from - they cannot drift apart.
  * The tools' `parameters` schema mirrors the JSON object the prompt
    asks for (`thought`/`action_input`/`args`/`options`) so a native
    call's arguments land under the keys `_run_tool` actually reads. An
    un-schema'd declaration produced `{"name": "godforged lost helmet"}`
    where the loop wanted `action_input`.
  * `_orna_system_prompt`'s old "you have NO callable functions, NEVER
    emit a tool call" paragraph is now FALSE and contradicted by the tool
    list Ollama's own template injects - replaced with a plain statement
    that JSON content is preferred and a native call is understood too.
  * `_from_tool_calls` still matters after this: a correctly-MAPPED call
    comes back as a valid `tool_calls` reply with empty `content`, which
    is exactly what it translates. Its `_demo()` pins one more real shape
    seen only once tools were declared (the action's args nested under
    `"args"` by the model itself).
  * `/go` and `telegram_nlp` pass no `tools` and their payloads are
    byte-identical to before - `tools` is only added to the payload when
    truthy. They are lower-risk anyway: their prompts ask for an
    extraction, not a choice among named actions.
  * A 500 whose log line shows a duration of exactly `45.00Xs` is NOT
    this bug - that is `STEP_MODEL_TIMEOUT`'s own read timeout
    disconnecting mid-generation, which Ollama then logs as a 500. Check
    for the harmony warning immediately above the line before chasing it.

**Two live follow-on blockers, both found only because the 500 stopped
masking them - a request can now run its full budget and still show the
user nothing.** Same request as above:
- **The model repeats a tool call with the identical input, despite the
  prompt telling it not to.** Measured on the real loop: 4 of 16 steps
  were the SAME `search_codex 'godforged lost helmet'`, which both
  exhausted the step budget and posted the same result card to the chat 4
  times. `OrnaSession.seen_calls` (signature → its observation) replays
  the cached observation plus a "stop repeating it" nudge instead of
  re-running the tool - no duplicate Telegram card, no wasted step. After
  this, the same request spent all 16 steps on distinct work and reached
  `assess`/`calculate`.
- **Both ways a request can end without `finish()` threw away everything
  gathered — `_close_out` is the shared fix.** Out of steps: the loop had
  looked up all four items' Orn Bonus and run the arithmetic, then spent
  its last steps on `web_search` and replied only "не вдалося сформувати
  відповідь". Out of wall-clock (`LOOP_TIMEOUT_SECONDS`): runs had
  assessed five of six items, with every number sitting in context, and
  showed the user "запит триває надто довго" and nothing else. Both
  endings now append a "no further tool calls are possible, answer NOW
  with what you have" observation and take ONE more model call, falling
  back to the old fixed message only if that call fails. It cannot loop,
  and is bounded by `_call_step_model`'s retry-once at ~90s past
  whichever limit was hit — so the wall-clock guarantee is now
  `LOOP_TIMEOUT_SECONDS` + one bounded call, not a hard 300s. That is a
  deliberate trade: a late real answer beats a punctual useless one.
  NOTE the second ending got MORE common, not less, after the assess
  fixes above — `assess` per named item is slower than the
  `search_codex`/`open_entry` path it replaced, so a six-item request now
  does more real work per step and more often runs out of time rather
  than steps. If these requests keep timing out, `LOOP_TIMEOUT_SECONDS`
  is the knob, and the dominant per-step cost is a cloud call that eats
  its full `STEP_MODEL_TIMEOUT` before falling back to local.

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

**Reports and codex entries render as aligned `<pre>` monospace tables via
the one shared `telegram_resources.pre_table`.** Live report: `/orna
balorite 100` (the `need` tool -> `build_report`) and the codex entry view
(a button tap -> `_format_entry`) printed space-padded rows WITHOUT a
`<pre>` wrapper, so Telegram's proportional font collapsed the padding and
the columns read as ragged text - unlike `/res_today`, whose `<pre>` tables
line up. `pre_table` (the single definition of that aligned-table style, so
they can't drift apart) pads each column to its widest RAW cell then
HTML-escapes (so `<`/`>`/`&` stay safe inside `<pre>`); `build_report`,
`_format_entry`, `_today_text`, and `_next_text` all go through it.

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
reminders (still checked even though the tapped button - see next
paragraph - normally isn't there to tap again; a client showing a
momentarily-stale cached keyboard, or a very fast double-tap racing the
edit below, both still hit this).

**Tapping a reminder button removes it from the keyboard - that's the
confirmation, there's no separate message.** `handle_reminder_button`
rebuilds `reply_markup` from `bundles`, excluding every index already in
`state["scheduled"]`, and edits the message in place; once every button
in a panel has been tapped, `reply_markup` goes to `None` (Telegram
just drops the keyboard) rather than an empty `InlineKeyboardMarkup`.
The `query.answer()` toast is still sent too, but as a non-blocking
toast (no `show_alert=True`) rather than a modal - now that the button's
disappearance is itself a persistent, visible confirmation, a popup the
user has to dismiss would be redundant weight, not the primary signal.
Replaced an earlier Google-Calendar-link design outright, per explicit
ask - a reminder the bot actually delivers beats a link out to a separate
app the user has to remember to check, and sidesteps the same-day-timezone
ambiguity that design's own comment already flagged (the new fire time
inherits that same ambiguity, documented on `_bundle_fire_at`, rather than
pretending it's precise). **Filename collision, worth knowing if you ever
run `git log`/`blame` on it:** that old design's module was *also* called
`orna_calendar.py`, deleted in the same commit that added these reminder
buttons (2026-09-23) - the `orna_calendar.py` that exists today (see the
module map / the `/orna` events tool) is a completely unrelated file
created later the same day, scraping `playorna.com/calendar/`'s live
event list rather than generating Google Calendar links. Same filename,
two unrelated histories - `git log --follow` on it will jump between them.

**The timezone ask is FREE TEXT now - an offset ("+3") or a place
("Київ", "New York") - not a grid of buttons.** It was 24-27 offset
buttons, and Telegram CLIPS an inline-button label that doesn't fit with no
ellipsis, so at 6 per row `UTC-10`/`UTC-11`/`UTC-12` all rendered as
`UTC-1` on a phone: the user read that as the picker repeating itself, and
tapping any of them silently saved a timezone hours off. That was worse
than cosmetic, because `usage_stats.set_user_tz` persists the pick and
every later reminder reuses it without asking again. **When adding any
inline keyboard, budget the label against the row width** - the
`_bundle_label` 64-char cap guards Telegram's API limit, a different thing
that does nothing about on-screen clipping.
The replacement (`request_utc_offset`/`handle_tz_input`/`handle_tz_confirm`):
- `_parse_offset_text` handles a typed offset deterministically, including
  fractional ones (`+5:30`, `+5.5`) that the old whole-hour grid couldn't
  express at all.
- Anything else goes to the model, which proposes only an IANA NAME; then
  `zoneinfo` decides whether that name is real and what its offset is. A
  hallucinated zone therefore can't become a plausible wrong offset - it
  raises and we say we couldn't work it out. Same "LLM for the fuzzy part,
  deterministic lookup for the answer" split as the rest of the repo.
- The resolved zone is shown for **confirm/decline**, and either button
  removes the keyboard (`_drop_keyboard`) so nothing stays tappable.
- **A place also fixes DST**, which no stored number can: `usage_stats`
  keeps `_user_zone` alongside `_user_tz` and `get_user_tz` recomputes from
  the zone on every read, so a user in a DST region stops drifting an hour
  twice a year. A bare typed offset still stores just the number - that is
  the user's choice, and it still freezes.
- `/remind tz` re-asks, because an already-saved timezone was otherwise
  unreachable: the "change" button only lives on the confirmation message,
  which scrolls away.

**Free text is NOT free in this bot - reuse the pending-filter pattern.**
`_PENDING_TZ_INPUT` + `_PendingTzInputFilter` + `build_tz_input_handler`
copy `/go`'s Continue handler exactly: a `filters.MessageFilter` that
matches only a chat with a live pending ask, registered in
`telegram_bot.py` BEFORE the assess and resources conversations. For every
chat without a pending ask it is a guaranteed no-op that falls straight
through, so it cannot steal text from those registration-order-sensitive
flows. Any future free-text prompt in this repo should be built the same
way rather than with a bare `MessageHandler`.

**Button labels include the material name(s), not just the guild -
`_bundle_label`.** Live report: asking about 2 materials produced 19
buttons, but the label was just guild + day-count, so most guild names
appeared twice (once per material, at different dates) with no way to
tell which button was for which material without tapping it. `_bundle_label`
joins every material name in the bundle (comma-separated - a bundle can
have more than one when two requested materials land at the same guild
on the same day, e.g. both landing at "Towers" the same day become one
button naming both) into the button text, truncated to 64 chars as a
safety cap for an unusually long combination.

**A full-codebase review (2026-09-24) fixed a cluster of "fuzzy-edge" bugs -
the recurring PATTERNS are catalogued in the skill (see below), since this
bot is glue over fuzzy inputs (LLM tool args, OCR, transliterated names,
live caches) and the "obvious" code was wrong at the fuzzy edge.** Each was
reproduced deterministically before the fix: `orna_assess.get_quality_code`
returned Broken for a quality of exactly 170 (`in_range` is `[lo, hi)`; 170
is the top of Legendary) - and 223 lines of genuinely-dead code were then
removed from that module (`get_full_result`/`make_input`/`CodexEntry.
from_dict`, plus `get_assess_result`'s unreachable observed-stat branch and
its helpers). `orna_aussies._eval_condition`'s attr kind mishandled
`{tier "=" 0/1}` (a bool-flag branch hijacked value 0/1, and
`str(raw or "")` collapsed a real 0) and matched every record on an empty
`useable_by`. `orna_knowledge`/`telegram_offerings` fuzzy matches were
case-sensitive. `orna_codex`'s English regex fallback could clobber
correctly-parsed facts, and its `lru_cache` (no TTL, dead `clear_cache`) is
now cleared by `/update_codex` so codex stats don't stay stale after a game
patch until restart. `telegram_orna` treated a `place:"material"` record as
upgradable gear (assess/compare posted a nonsense upgrade table), iterated
a bare-string `items`/`slots` arg character-by-character, returned a
header-only false "success" from `_next_text`, and had no idempotency guard
on the ask-callback (a double-tap double-spent the shared session).
`telegram_go._calculate` ran unbounded `pow` on the event loop (capped),
`_markdown_to_html` garbled `*`-bullets and intraword underscores and
double-wrapped bold headings, and `ollama_client.chat_json_with_fallback`
skipped the local fallback when a post-image-drop cloud retry failed.
`telegram_resources._next_occurrence` silently dropped a "February 29"
forecast date (year-1900 non-leap parse). See
`.claude/skills/verifying-orna-changes/references/common-pitfalls.md` for
the ten patterns these cluster into.

### Reviewing an outside research document (2026-09-26)

A Gemini Deep Research report on Orna's mechanics was reviewed and its genuinely
new material merged into `orna_mechanics.txt` - NOT added as a seventh
`knowledge_search` block, because that tool already composes six and one call had
to be capped at 8KB. Filling a gap in an existing corpus beats another source.
The raw document is committed as `gemini_research_2026-09-26.txt` for provenance.

**The headline gain: the universal damage formula**, which existed nowhere in the
repo. Guild-confirmed:
`Damage = ((Stat_off × M1) − (Stat_def × Buff_def) / 2) × M2 × N_strikes × Buff_off × Faction`
It is a THRESHOLD system - if the bracket resolves to <= 0 the damage is exactly
zero regardless of M2, offensive buffs or elemental weakness - which is why
offensive buffs on a non-penetrating skill do nothing, and it finally explains
"why does my skill do zero damage" as a mechanic rather than a mystery. Also new:
DoT bypassing Second Chance, Demonworking Tools, Tower Shard -> Sky Shard
economics, and the multi-strike "debuff rolls once" rule.

**How to treat a document like this, learned from its errors:**
- **It had at least one wrong cited number.** It asserted dual wielding reduces
  main stats to 45% (and world bonuses to 50%), with a citation. The guild
  reconfirmed **0.65 of the combined stats**, which is what the code implements.
  So its prose is CLAIMS TO CHECK, not data - recorded in the corpus itself so a
  future reader does not re-import the 45%.
- **Two of its numbers independently CONFIRMED existing sources**, which is the
  useful signal: `Ward = (HP + Mana) / 2` matches `orna_echo`, and hybrid skills
  using 65% ATT + 65% MAG matches `orna_echo`'s own table. Agreement between
  sources that did not copy each other is worth more than either alone.
- **Its quality bands side with the community guides against our port** (Superior
  110-119, Famed 120-130 vs `get_quality_code`'s 101-119 / 120-139). That is now
  two independent sources against the port; the repo stays authoritative for code
  and the prescribed check is a live playorna assess page.
- **Equations do not survive a Google Docs text export.** Every formula was an
  image/LaTeX, so the export reads "the formula is defined as follows:" followed
  by nothing - including the central damage equation, which had to be supplied
  separately. Ask for equations as text or images when requesting such a doc.
- Its 62 sources named **`blog.ornarpg.com`**, the official dev blog, which no
  corpus here uses yet - a real candidate if another source is ever wanted.

### The player Q&A corpus - the only source indexed by the QUESTION

`orna_qa.py` / `orna_qa.txt` / `orna_scrape_qa.py`, added 2026-09-26 on ask.
r/OrnaRPG questions paired with their best-voted answers. Every other corpus is
keyed on an ANSWER's wording (`orna_knowledge` tabular, `orna_reddit` dev prose,
`orna_echo` guide sections, `orna_guides` whole guides); an incoming question
resembles a past player's question far more than any answer, and that is exactly
the live retrieval failure that motivated it - the Vritra Charm answer WAS in
`orna_reddit.txt` and three `knowledge_search` calls missed it because the model
searched the ITEM while the text is indexed by the MECHANIC. It also carries the
one thing no other source has: a **corrected premise** ("those are summons, not
followers"), which the tool description tells the model to look for.

- **Reddit rate-limits hard, so the crawl is RESUMABLE - and it took three
  runs.** 825 unique QUESTION-flaired posts listed, 800 with >=2 comments. At 1s
  between requests Reddit 429'd after ~88 of them; a second run was throttled on
  the LISTING itself and produced zero posts. Both the listing and each post's
  comments are now cached under `.qa_cache/` (gitignored), a 429 backs off
  (30/60/120/240s, honouring `Retry-After`) and then STOPS rather than grinding,
  and the corpus rebuilds from the whole cache on every run. **At 5s per request
  the third run completed 800/800 with zero failures and four handled 429 pauses
  -> 751 threads / 1,806 answers, 809KB.** Re-run
  `REDDIT_COOKIE=... QA_HELDOUT_IDS=... python3 orna_scrape_qa.py` to extend it;
  it skips everything cached.
- **Scoring is IDF-weighted, and the three wrong designs before it are the
  lesson.** (1) Binary keep/drop of "common" words by document frequency
  DISCARDED "summoner" - >45 of 751 threads mention it - so "how do I get the
  summoner class" could not be answered at all. **On a single-topic corpus the
  domain terms are frequent BY NATURE and are also the discriminating ones**, so
  they must be down-weighted, never dropped. (2) Requiring TWO question-side hits
  was tuned on the rate-limited 87-thread corpus and rejected real questions at
  scale: "how does ward work" reduces to {work, ward}, and a thread titled "94k
  ward how??" matches one of them - so all 59 ward threads were unreachable. At
  751 threads the RANKING does the precision work, so the floor only has to
  reject a zero-signal match. (3) Giving an UNKNOWN word the maximum weight
  ("rare by definition") made "hello" a super-discriminator that matched "Hello
  I'm new and looking for tips" for any greeting; a word no question contains
  scores ZERO, because it cannot help FIND a question.
  The floor is on a score NORMALISED by log(N), so it does not depend on corpus
  size - an absolute floor rejected everything in the unit-test fixture, which is
  how that bug announced itself. `_MIN_SCORE = 2.5` by measurement: every real
  test question reachable, NONE of six deliberately-generic ones matched. The
  fixture is padded to a realistic shape for the same reason - IDF is meaningless
  over two threads.
- **KNOWN LIMIT, stated rather than tuned around:** IDF cannot separate a word
  that is rare AND meaningless from one that is rare and meaningful. Swept the
  floor 1.2->9 before normalising and every value both answered all the real
  questions and matched the junk ones, i.e. the knob does not separate them. That
  is acceptable because of where the result goes - the tool hands the model
  "possibly related threads" to judge, not an answer, and the confidence gate
  stops a weak match becoming a confident reply.
- Vote counts stay IN the text (`A [22up]`, `A [6up DEV]`) so the reading model
  can weigh a 22-upvote answer against a 2-upvote one, dev answers are flagged,
  and every block carries its DATE - the tool description says `releases()`
  outranks an old answer on numbers. Upvotes are a crowd signal, not truth.
- The six blind-evaluation threads are crawled into `orna_qa_heldout.txt`
  instead of the corpus (`QA_HELDOUT_IDS`), so the bot cannot answer the
  questions it is judged on. Hygiene only, NOT a guarantee - `web_search`
  reaches the live threads anyway (see the benchmarking section below).
- The cookie is read from `REDDIT_COOKIE` and never committed, same rule as
  `orna_scrape_reddit`'s two routes.
- **`knowledge_search`'s observation is capped in TOTAL, not just per block.**
  It now composes up to SIX source blocks (sheets, player Q&A, guide formulas,
  mechanics, class data, dev comments) and one call measured **14,909
  characters** - a large slice of a step's context spent on sources that may all
  be marginal, which is the opposite of helping the model reason.
  `_KNOWLEDGE_OBS_MAX = 8000` drops whole blocks from the END and TELLS the model
  how many were omitted, so a truncation never looks complete - the same rule as
  `_names_observation`. Measured after: ~7.3-7.9KB per call.

### The confidence gate - finish() must say when it does not know

Explicit ask 2026-09-26, after three of six answers in the blind Reddit review
invented a cause rather than admitting ignorance. Two halves, and the second is
the one that makes it real:

- **Prompt** (`_CONFIDENCE_RULE`): every `finish()` carries a `confidence` 0-100
  scored on the EVIDENCE (90-100 every claim from an observation; 75-89 a stated
  assumption; below 75 you are guessing), and below the floor it must say so in
  the answer, name the unverified part and what would settle it.
- **Code** (`_confidence_gate`, `_evidence_ceiling`, `_CONFIDENCE_FLOOR = 75`):
  a self-reported number is weakly calibrated, so it is **CLAMPED by what the
  loop actually verified**. No tool called at all -> ceiling 40 ("nothing was
  verified"); every observation a dead end -> ceiling 60. Below 75 the reply
  LEADS with an explicit "I don't know this reliably" banner in the user's own
  language, with the partial answer kept beneath it rather than discarded - the
  label has to come first so it cannot be skim-read past, but a labelled partial
  beats a blank refusal.
- `_DEAD_END_MARKERS` fails OPEN (evidence assumed good) if a tool starts
  refusing with new wording: a false cap on every answer would be worse than a
  missed one.
- The whole decision is ONE function so it is testable - inline in the finish
  branch it was not, because there is no way to inject a step into
  `_advance_inner`. Pinned in `_demo` across six cases: 95% claimed with no tool
  calls is gated, 95% with only dead ends is gated, 95% with a real observation
  passes through untouched, the model's own 50% is honoured, no number plus real
  evidence is NOT penalised, and the admission follows the user's language.

## Benchmarking against public answers does not work - the bot can read them

A blind test was run 2026-09-26: six top r/OrnaRPG question posts of the year,
title+body only, answered by the loop, then compared against the top-voted
comments. It is a good exercise and the failure PATTERNS below are worth having.
But as a repeatable benchmark it is structurally broken, in two ways that both
bit:
- **`orna_reddit.txt` already contains the answer.** "Where to spend orns?"
  looked like a clean win - the bot's figures (50m Altar of Ascension, 15m
  specs, 16m Grand Summoner, 20m Deity) matched u/Widogeist's top comment
  exactly. They matched because that comment is verbatim in the corpus (`grep
  -c "Altar of Ascension is 50m"` -> 1). It retrieved the thing it was graded
  against.
- **`web_search` reaches the thread itself.** On "Anguished Ornate questions"
  the cloud model made **13 web_search calls** and cited "the Orna Reddit thread
  (r/OrnaRPG - Anguished Ornate questions)", reproducing the top comment. So
  even holding a post out of the corpus does not make it blind - the ground
  truth is on the open web, which is a tool the bot has.
**So do not use public Q&A as a scored benchmark.** `orna_test_suite`'s cases
are derived from the game DATA and graded against it, which is why they stay
honest. Use Reddit for finding failure shapes, never for a pass rate.

What the exercise did surface, on questions where nothing leaked:
- **It will not challenge a question's premise.** "How to get multiple
  followers?" - the community's top answer (22up) was "you're fighting a
  summoner, those are summons, not followers", which is also why they have their
  own HP bar. Both models answered the question as asked and explained follower
  capacity instead.
- **It fabricates a cause rather than saying it does not know.** For the Vritra
  Charm it invented auto-dismantle settings and a differently-named debuff; the
  real answer (18up) is that status immunity never blocks effects caused by YOU
  or YOUR FOLLOWER. That exact statement is in `orna_reddit.txt` ("...will not
  prevent debuffs that are caused by your followers spells/abilities") and three
  `knowledge_search` calls missed it, because the model searched the ITEM name
  while the answer is indexed by the MECHANIC - searching "immunity debuffs
  caused by follower" returns it immediately.
- **It hedges where the question is binary.** "Do the stats stack, 39% or 24%?"
  got "likely refers to" rather than the top comment's clean "it doesn't stack".

**`open_entry` now refuses a url it was not given by a tool**
(`_codex_path_problem`). Two live cases in one session: `/codex/items/vritra
charm/` - a path the model built from the item NAME, space and all - and
`https://playerecho.com/orna/circle-of-anguish`, a CITATION url from a
`knowledge_search` block. Both raised inside `fetch_codex_json`, burned a step,
and the Vritra request then answered from invention. It now returns an
observation naming the problem (wrong host / not a codex path / came from a
citation) and posts nothing, so the loop can recover. Pinned in `_demo`.

## Comparing local models (`orna_model_compare.py`)

Runs the SAME requests through two or more local models and reports wall-clock,
step count, the exact tool sequence each chose, and pass/fail. Reuses
`orna_test_suite`'s graded cases rather than new prompts, so "was it right" is
decided by the suite's live-data expectations, not by eye. Always local-only
(`MAX_CLOUD_CALLS = 0`) - through a cloud-first loop most steps would measure
the cloud. Results append to a gitignored JSON so a long comparison can be run
one model per invocation (each fits inside a sane timeout) and still print one
combined table (`REPORT=1`).

**A run that CRASHED is not a run that answered wrongly, and conflating them
makes the comparison actively misleading.** When a step's model call dies, the
loop posts its Ukrainian failure text - and the grader then scored that as
"answer must be in English but contains Cyrillic" and as missing facts, i.e. it
blamed the model's judgement for a backend fault. Runs whose answer carries a
loop-level failure signature are now marked **ERRORED** and excluded from the
pass denominator. Rows recorded before this lack the flag (muse-glimmer's), so
count them from the stored answer text instead.

Measured 2026-09-25, the same 6 cases (2 simple, 2 medium, 2 hard), N=1, local:

| model | passed | crashed | median | first tool right? |
|---|---|---|---|---|
| `nemotron-3.5-lightning:30b-mlx` (current) | **6/6** | 0 | **33s** | yes |
| `qwen3.8:27b-mlx` | **6/6** | 0 | 138s | yes |
| `lfm2.5:8b` | 2/4 graded | 2/6 | 60s | mostly |
| `muse-glimmer:30b-mlx` | 1/3 graded | 4/7 | 184s | no |
| `magistral:latest` | 4/5 graded | 1/6 | 146s | yes |
| `laguna-xs-2.1:nvfp4` | 0/0 graded | **6/6** | 24s | **yes** |

Read past the pass column, because the failures have three different causes:
- **`laguna-xs-2.1` answered CORRECTLY and still scored zero.** Every run died
  on `Ollama returned non-JSON content` whose content was the right answer in
  prose ("Judge Trifecta Falx is a **Tier 10** weapon with **Famed**..."). It
  routed perfectly - the correct first tool on all six - and then wrote the
  FINISH step as markdown instead of the required JSON envelope, which the loop
  cannot use. That is a format-compliance gap, not a quality one, and it would
  likely pass if the finish step tolerated prose the way
  `ollama_client._from_tool_calls` tolerates a native tool call.
- **`muse-glimmer` fails in the backend, and it is NOT the known harmony bug.**
  Its 500s ran 19-36s (not the `45.00Xs` read-timeout signature) and the server
  log shows renderer/parser `glimmer` with `harmony=null` and no "no reverse
  mapping" line anywhere - so this is the glimmer parser failing on this prompt
  shape, a different cause from the gpt-oss case documented above. Where it did
  answer, it looped: 64 `query` calls across the set and one case hitting the
  full 35-step ceiling without ever reading an entry.
- **`lfm2.5:8b` is the only one with genuine ANSWER failures** (2), naming the
  wrong classes for a set - plus 2 crashes.
- **`magistral:latest` is the best of the alternatives on correctness** (4 of 5
  graded, the miss being the 13-item set answered without "warrior") but is 4x
  slower than the current model, and its one crash is the same shape as
  laguna's: it emitted its REASONING into `content` ("I see that the class_guide
  tool requires a specific topic to be set in the args...") instead of the JSON
  envelope. Unlike the other locals it is selected by `go_template` rather than
  a renderer/parser, which is the likely reason `"think": True` did not keep the
  reasoning out of `content` for it. Two of six models therefore fail ONLY on
  the JSON envelope, which makes tolerating a prose/reasoning finish the single
  highest-value robustness change available here - it would recover laguna
  entirely and magistral's one loss.

So there is no reason to switch: the current model is right as often as the best
alternative and 4x faster, and wall-clock is the scarce resource in this loop.

qwen matched nemotron answer-for-answer and picked the same tool first on every
case; its only routing difference was one extra `query` on the 13-item set
question (15 steps vs 12), which bought nothing. Caveats: N=1 per case, so a small accuracy difference
would not show - re-run with N>=3 before concluding anything about quality; the
first request per model pays a cold load (flagged in the output); and these are
wall-clock figures for the whole ReAct loop including real codex/sheets calls,
not tokens/sec.

## The test suite (`orna_test_suite.py`)

Three tiers, run before and after a major update and compared. No framework,
no mocks - same reasoning as everything else here: mocking the codex/sheets/
Ollama would test nothing that actually breaks. `python3 orna_test_suite.py`
is tier 0 alone (~1s); `TIER=0,1,2,3 N=3` is the real gate (~20 min, so record
a baseline one tier at a time - `SAVE_BASELINE=1` MERGES rather than replaces
for exactly that reason).

- **Tier 0 (9 checks, no LLM) must be 100%.** It runs each module's own
  `_demo()` rather than restating their asserts, plus cross-module ground-truth
  invariants (the `useable_by` absent-field rule, the bogus-field report, the
  observation-honesty helper, the quality boundaries, the possessive name
  ladder, the raid's own class split).
- **Tiers 1-3 score a PASS RATE over N runs**, graded on FACTS (a substring
  that must appear, one that must not, which tools the trace contains) with
  every expectation DERIVED FROM LIVE DATA at run time - a suite of pinned
  numbers would fail on the next game patch instead of on a regression.
- Determinism levers: `ollama_client` now takes `ORNA_LLM_TEMPERATURE`/
  `ORNA_LLM_SEED` (env-gated - **unset in production, and then the payload is
  byte-identical to before**, same rule as `tools`). A/B measured on a real
  step prompt: 5/5 usable JSON with and without them, so pinning sampling
  costs nothing.
- **`finish()` is NEVER in the action trace** - `_advance_inner`'s finish
  branch returns before the assistant message is appended. So a case asserting
  "research, then finish" as two tool calls can never pass. Cost two false
  failures before it was spotted.
- **Baseline of 2026-09-25**: 11 of 12 cases 3/3, `trifecta-classes` 2/3.
  A single drop is weak evidence - `stacking-total` measured 1/3 then 3/3 on
  consecutive batches with no code change, `ukrainian-lock` 1/2 then 3/3.
  Re-run with `CASE=<id> N=5` before believing a regression.
- **Check the EXPECTATION before "fixing" the bot.** Twice while building this
  the grader was wrong and the loop was right. The sharper one: the case
  asserted "no Judge Trifecta item is useable by mages", and a live run
  correctly answered that **`Scroll of the Judges Trifecta` IS `all_classes`**
  - the substring `"judge trifecta"` does not match `"judgeS trifecta"`, the
  same plural/possessive lossiness as the `_name_candidates` bug. **A set-name
  substring is a lossy stand-in for set membership; the raid's own `drops`
  list is the fact** (Judge Trifecta Maximus drops exactly 12 items, 4 warrior
  / 4 thief / 4 valhallan_summoner, none mage-useable - and the scroll is NOT
  one of them). Note this also makes the original bug report's question
  genuinely ambiguous, which is why that case now asks about the raid's drops.

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

`/orna`'s ReAct loop is verified the same no-mocks way, plus one extra
harness: a throwaway `FakeMessage` (a stand-in with a `reply_text` that
just prints/collects instead of hitting Telegram) passed straight into
`_advance`/the loop's tool functions, run against the real local/cloud
Ollama and real codex/aussiescodex data end-to-end for a given request
string — lets you watch every `reply_text` a multi-step request would
actually send (including which tool got dead-ended, retried, or produced
an empty observation) without needing a live chat to test against. Same
`orna-telegram-bot/telegrambot_error.log` reload-and-check step afterward,
watching for the `orna:`-prefixed step logs mirroring `/go`'s own
`go:`-prefixed ones.

**This whole no-mocks method is packaged as the `verifying-orna-changes`
skill** (`.claude/skills/verifying-orna-changes/`), so it doesn't have to be
re-derived each time. It carries: an ordered procedure (establish
ground-truth from the source sheet/codex -> deterministic pure-function
repro -> real-model end-to-end repro -> multi-run verification, because the
model is non-deterministic so one green run proves nothing); a ready-to-run
`scripts/orna_loop_harness.py` that drives the real `/orna` loop for a given
request via a `FakeMessage` and prints the ACTION trace + every reply
(`Q="balor sword" N=5 python3
.claude/skills/verifying-orna-changes/scripts/orna_loop_harness.py`,
`FORCE_LOCAL=1` for local-only, secrets read only from the environment);
`references/common-pitfalls.md`, the ten recurring bug shapes the
full-codebase review above clustered into (case-insensitive matching,
falsy-value collapse, unvalidated LLM args, boundary math, event-loop
blocking, lossy-query retrieval, cache staleness, double-delivered
callbacks, regex ordering, fixing the shared function not the symptom); and
`evals/trigger-evals.json` for re-tuning the skill's own triggering. Reach
for it - and its harness - when writing or debugging any bot change.

### When the closing line is sent, and who writes the Ukrainian (2026-10-05)

- **The model decides whether a posted card is the whole answer; tools never
  read the question.** `monuments`, `today`, `next` and `towers` post their own
  card (through `_PostRecorder`/`_run_listing`), and their observation then says
  so (`_POSTED_NOTE`): if the card fully answers, `finish` with an EMPTY
  action_input and nothing more is sent (the `/clarify` hint is edited onto the
  card); otherwise send only what the card does not show. A first version decided
  this in code from the question's words (`_JUDGMENT_RE`) and dropped a real
  answer ("які матеріали" is not a "judgment"); a tool that guessed its category
  from the question was also tried and removed. Rule kept: tools have one
  question-agnostic API, the model forms the params and finalises.
  `posted_note`/`last_post` reset every turn - a `/clarify` once inherited the
  previous card's flag and its real answer was dropped.
- **Cards are rendered in the user's language by CODE**, not by a model: reward
  categories, labels and dates from small tables, material names from
  `orna_material_names_uk.json` (the game's own names). Potions, guilds, towers and
  monuments stay as proper nouns. The model still reads English data - `today`/`next`
  now return the posted list in their observation (they used to return "sent it",
  and the model re-called `today` blind). The towers card carries the hours-to-50
  column, or the model re-lists it.
- **Material names in a Ukrainian question are glossed from that table** at the
  input gate (`_uk_material_names`, stem match so inflections hit): the
  translation turned "червоний драконіт" into "Red Dragonite". `search_codex` also
  tries a near-exact fuzzy correction (`_NEAR_EXACT_CUTOFF`, case-insensitive)
  BEFORE its word-dropping ladder, which had turned "Red Dragonite" into plain
  Dragonite.
- **No REVIEW once a tool posted the deliverable.** REVIEW never sees the posted
  card, and in all three measured cases it made the closing line worse: a
  tautology ("Materials are those listed in the monument table"), a sentence cut
  to "Demeter", and a correct "Demeter - 8 floors" revised into "impossible to
  determine which monument gives the most".
- **TRANSLATION_MODEL (gemma4:31b) writes ALL the user-facing Ukrainian.** The
  loop model (deepseek-v4.1-flash via /model) often answers in Ukrainian itself,
  and the old "translate only if not already in the target language" rule then
  let its Ukrainian through untouched ("зелья", English leftovers). A draft
  already in the target language is now REWRITTEN (`_translate` with
  source == target is a rewrite prompt, not "translate Ukrainian into
  Ukrainian"); only the fixed verbatim replies are exempt. Names are pinned with
  `telegram_announce.game_names` - codex names plus classes/specializations and
  the four Monuments - instead of every capitalised word.
