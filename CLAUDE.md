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
- `telegram_orna.py` — `/orna <text>`, a real ReAct loop (today/next/need/
  search_codex/query/events/open_entry/calculate/assess/compare/
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

Through 2026-09-22, `/orna` was a bounded two-call pipeline: one LLM call
(`route_query`) classified + translated the request, a second
(`plan_queries`) turned a `"query"`-intent request into one or more
structured condition blocks — deliberately **not** a ReAct loop at the
time, the tradeoff being weighed explicitly against giving `/orna` the
same multi-step loop `/go` has: rejected because looping a local-only
model seemed likely to multiply flakiness across steps for what's usually
a one-item lookup. Rebuilt 2026-09-23 into a genuine ReAct loop — ReAct
tools: `today`/`next`/`need`/`search_codex`/`query`/`events`/`open_entry`/
`knowledge_search`/`web_search`/`calculate`/`assess`/`ask`/`finish`, one
action per turn via a JSON action schema (`{"thought","action",
"action_input","args"}`), not native Ollama tool-calling — deliberately
mirroring `/go`'s own `_advance`/session/step-budget design rather than
inventing a second loop shape. What changed the calculus from the
2026-09-22 rejection: `/orna` now also gets Ollama Cloud access (see
below), and several live bugs (a bad codex-name guess, a follower-vs-item
schema gap, a "how do I beat X" question the old pipeline had no path to
answer at all) turned out to be exactly the shape a loop self-corrects —
observe a dead end, reason, try something else — that a one-shot parse
structurally can't recover from.

**Session/step design**, in `telegram_orna.py`'s `OrnaSession`/
`_ORNA_SESSIONS`/`_advance`/`_call_step_model`:
- `MAX_STEPS = 16`. **EVERY step tries Ollama Cloud first and falls back to
  local mid-turn if that call fails** (`_call_step_model`), per explicit ask
  2026-09-24. `MAX_CLOUD_CALLS = 20` is now only a runaway guard, not a
  routing decision - `MAX_STEPS` is 16, so 20 covers every step plus the
  close-out call and never binds on a normal request. Setting it to 0 forces
  local-only (that is exactly what the verification harness's `FORCE_LOCAL`
  does). This is the third routing scheme here, and the history matters
  because each was a reasonable-sounding answer to the previous one's
  failure: "first 8 turns → cloud" spent the quota on cheap early routing
  and left the heavy final synthesis on local; "cloud only above
  `CLOUD_CONTEXT_CHARS = 1500` of accumulated context" (now deleted)
  inverted that, keeping tool-routing and codex lookups local because their
  observations are short. What killed the second one: **a cheap step is not
  cheap to get WRONG.** The local model picking the wrong tool, or dropping
  a number it was handed, costs a step out of `MAX_STEPS` and real
  wall-clock - and once `/orna` started ending on `LOOP_TIMEOUT_SECONDS`
  rather than on the step budget (see the assess notes below), wall-clock
  became the scarce resource, not cloud quota.
- **Cloud-first needs a circuit breaker, and this was learned the hard
  way within an hour of shipping it** (`ollama_client.CLOUD_COOLDOWN_
  SECONDS`, `cloud_is_parked()`). When every step tries cloud first, a
  cloud OUTAGE costs a full `STEP_MODEL_TIMEOUT` on every single step
  before the local fallback even starts — a 16-step request pays 16×45s of
  pure waiting and blows its whole wall-clock budget having done no work.
  Live 2026-09-24, reported as "the bot doesn't respond": Ollama Cloud
  started returning `ReadTimeout`, whose `str()` is EMPTY — which is why
  the log line reads `Ollama Cloud unavailable (Ollama request failed: )`
  with nothing after the colon. That empty parenthetical is the signature
  of a cloud timeout, not of a mysterious error with no message. One
  failure now parks the cloud leg for 300s so the rest of that request and
  any concurrent one go straight to local at full speed; the first call
  after the cooldown probes cloud again and any success clears the park.
  Lives in `ollama_client` rather than `telegram_orna` so `/go` gets it
  too — it has the same cloud-first shape. Diagnose a suspected outage
  with a direct `POST https://ollama.com/api/chat` and watch the wall
  clock: a real outage returns nothing for the full timeout.
- `LOOP_TIMEOUT_SECONDS = 300` — a hard wall-clock ceiling on the whole
  request (`asyncio.wait_for` around the loop), regardless of step count.
  This is the actual guarantee the loop always replies within a bounded
  time no matter what any single step does; a hung/slow chain gets cut
  off here with a "took too long, try again" message instead of the user
  waiting indefinitely with no way to tell a slow loop from a stuck one.
- `_call_step_model` retries a failed model call once before giving up
  (matches `telegram_nlp`'s retry-once convention) — live incident
  (2026-09-23): a long multi-tool-call request died on one empty-content
  response from the local model; discarding every step of reasoning
  already done over what's often a transient blip was a bad trade.
- **The two legs of a step run on DIFFERENT deadlines, because they have
  different jobs.** Cloud gets `STEP_MODEL_TIMEOUT` (45s read) - it should
  give up fast, since there is a fallback waiting and a step-budgeted loop
  treats a hanging call as pure waste. Local gets `LOCAL_MODEL_TIMEOUT`
  (120s read) - it IS the fallback, nothing comes after it, so cutting it
  off mid-generation throws the whole step away for nothing; gpt-oss:20b
  runs ~49 tok/s here and a long accumulated tool history genuinely can
  take over 45s. `chat_json_with_fallback`'s `local_timeout` parameter is
  what makes this possible (it defaults to `timeout`, which is what `/go`
  passes - one deadline for both legs). `ollama_client.DEFAULT_TIMEOUT`
  (`/go`'s, and the default) went 90s → 120s at the same time and for the
  same reason. Live incident (2026-09-23) that set the cloud side: one step
  once spent ~2.5 minutes (a full 90s cloud timeout, then a slow empty
  local response) before failing — see `concurrent_updates` below for why
  that alone was enough to lock up the *entire* bot for every user, not
  just the one slow request. Worst case per step is now 45s + 120s, so
  `LOOP_TIMEOUT_SECONDS` can be consumed by two pathological steps; that
  is the knob to raise if legitimately-long requests start getting cut.
- Logging is deliberately light (no `exc_info=True`) at every
  intermediate retry/fallback point, with the full traceback logged once
  — at the point the loop actually gives up — not at every layer.
  Verified live: one failed step used to produce 4 redundant full
  tracebacks (cloud→local fallback ×2 attempts, the retry warning, the
  final give-up) for what is really one event.
- Every non-`ask`/`finish` tool call posts its own rich Telegram message
  immediately (same as `/go`'s `youtube` action) and returns a *short*
  text observation to the model — `finish` is always just a closing
  sentence, never where the actual data lives, so the model never has to
  retype a guild/date table or a stat block from memory. Dead-end tool
  calls (0 results) deliberately do **not** post their own "nothing
  found" message anymore — live bug: several dead-end messages plus a
  step-budget-exhausted message cluttered the chat for one request that
  should have been a single clean answer; a `query`/`search_codex` dead
  end is often just one step in the model retrying with a different
  spelling or field, and only a genuinely final "nothing anywhere"
  belongs in the user's chat (that's `finish()`'s job).
- Any tool that produces a number the model will REASON WITH puts that
  number in its text observation, not only in the message it posted —
  the model cannot read what it only sent to Telegram. `query`'s
  `sort_by` carries `"[sort_by=value]"` (live bug: the model couldn't
  see button labels as text, so it `open_entry`'d items just to re-read
  a number it already had); `build_optimize` carries `"[total=N]"`; and
  `assess` carries `"[orn_bonus=57.5%, ...]"` — that last one added
  2026-09-24 after a live report where the whole request was "total the
  Orn Bonus of these six godforged items", the loop assessed every one
  of them CORRECTLY, got back only `"posted assessment for X"`, and
  answered "на жаль, не отримали точні дані про бонуси". The one number
  the request was about was computed and then dropped on the floor.
  When adding a tool, ask what number the model needs back, not just
  what the user sees.
- **A quality-scaled bonus and a codex base value are different numbers,
  and only `assess` produces the first.** An item's codex page shows its
  UNSCALED base (Lost Helmet: "Orn Bonus: +5%"); at godforged that same
  item is +57.5%. Before the observation fix above, the loop's only
  readable numbers were the base ones, so it totalled those. The prompt
  (`_AGGREGATE_RULE`'s NAMED-ITEM clause) now spells out the third
  question shape explicitly: the user naming items AND their qualities is
  neither a `build_optimize` ask (that PICKS items for you) nor a plain
  lookup — it's `assess` once per named item, then `calculate`. Verified
  live 3/3 that the loop calls `assess` per item after this, where before
  it used `search_codex`/`open_entry` base values 0/1.
- **`orna_knowledge.search` was prefixing the WRONG line as the column
  header, so every number read out of the boosts table was a guess.**
  `_looks_like_header` takes the first `" | "`-split line whose segments
  average ≤15 chars. In the "Gear XP/Orn/Gold Boosts" section that matched
  `All values are multiplicators and stack with each other | 0.1 | 1 | 1 |
  1.1 | …` — a prose sentence whose 9-word first cell is dragged under the
  average by the ten bare numbers after it — one line ABOVE the genuine
  header `Item | Tier | Type | Exp | Orns | Gold | Luck | …`. The model
  therefore saw `Temple of Wealth | - | Kingdom Research | - | 1.2 | 1.2 |
  …` with no column names and had to guess which cell was Orns. Live, two
  runs of the same request read those rows differently (1.2 vs 2 for Temple
  of Wealth, 1.2 vs 1.1 for Vulcan's Brew) and answered ×195.81 and ×299.16
  for the same question; ×195.81 is correct. The fix rejects a candidate
  whose FIRST cell is a sentence (>4 words): a header's first cell is the
  row-label column's name (`Item`, `Type`, `Members`, `Tier & Rarity`) and
  is never prose, while a prose line-in puts its sentence exactly there.
  Two stricter rules were tried and reverted — "no cell may exceed 25
  chars" and "no cell may exceed 4 words" both also rejected the Proofs
  section's genuine header, which carries emoji labels (`👺 Anguish`, long
  in `len()` terms) and a stray sheet note (`Price Formulae, for those
  interested:`) in its LAST cell. Checked against all 16 sections before
  and after: only the intended one changed. `python3 orna_knowledge.py`
  now pins this.
- **The corpus spells it `Vulcan's Brew`, the players write "Volcan's
  Brew"** — `search`'s fuzzy-correction pass does bridge that, but a
  direct `grep` for "Volcan" finds nothing, so don't conclude from a grep
  alone that something is absent from this corpus.
- **`orna_knowledge.search` needs the WHOLE query as one substring, so a
  query naming several subjects matches NOTHING — `_search_words` is the
  fallback.** Live report: `knowledge_search("Shrine of Luck, Lucky Silver
  Coin, Temple of Wealth, Volcan's Brew orn")` returned empty and the
  answer said none of them were in the data, while each name on its own
  returns its exact row. First fix split the query on
  `,`/`;`/`/`/`and`/`та` in `telegram_orna`; that held until the model
  wrote **the same list space-separated with no delimiter at all**, which
  no splitter can help with — live again, and that run invented the
  multipliers and answered ×305.80. Replaced (splitter deleted) by scoring
  in `orna_knowledge` itself: rank lines by how many DISTINCT query words
  they contain, ≥2 to count, words under 3 chars dropped so "of"/"a" can't
  match every row. It needs no delimiter, so it covers the comma form, the
  "and" form and the bare-space form with one mechanism, it runs after the
  existing fuzzy-correction pass (so "Volcan's" still reaches "Vulcan's"),
  and it fixes every caller rather than one tool. Verified: the exact
  space-separated query now returns all four subjects with the column
  header attached; single-subject queries still take the exact-substring
  path unchanged. Pinned in `python3 orna_knowledge.py`.
- **Bonus stacking is multiplicative and the product is a MULTIPLIER, not
  a percentage — both halves were got wrong live, in the same session.**
  `build_optimize` has always been the canonical implementation
  (`multiplier *= (1 + scaled / 100)`, then `total_pct = (multiplier - 1)
  * 100`); the hand-rolled path had no such anchor, and two runs of the
  same request answered "650%" (additive sum) and "21.76%" (the raw
  product labelled as a percentage; it's ×21.76, i.e. +2076%).
  `_AGGREGATE_RULE` ends with a STACKING CONVENTION paragraph stating
  both rules and requiring finish() to give both forms. The deterministic
  half: `_run_calculate_tool` appends `"[as a stacking bonus: xN total =
  +M% bonus]"` to any PURE PRODUCT whose result is >1, so the
  `(product - 1) * 100` step is never done in the model's head — it
  slipped exactly there, twice: 21.76× reported as "+1776%", and later
  195.81× reported as "+95.8%". The first version only matched the
  `(1 + b/100) * …` shape `_AGGREGATE_RULE` asks for and MISSED that
  second slip, because the model had already converted each bonus to a
  multiplier itself and wrote `21.757 * 1.25 * 2 * 2 * 1.2 * 1.2 * 1.25`
  — a perfectly good stacking expression with no `(1 +` in it. A `+` or
  `-` anywhere means it isn't a pure product, so ordinary arithmetic is
  left alone. Measured after: three separate totals in one verification
  set (+2075.7%, +800%, +2076%) all correct, where the run before it
  produced "+95.8%".
- **The model sometimes nests `action_input` INSIDE `args`** —
  `{"action":"calculate","args":{"action_input":"1.575 * 1.65 * …"}}`,
  seen live 2026-09-24. The expression was right there and usable, but
  the tool received `""` and spent a whole step replying "calculate needs
  a numeric expression". `_advance_inner` accepts either placement now.
  Same class as `ollama_client._from_tool_calls`: the model's decision is
  correct, it just arrived in the wrong field, so translate rather than
  reject.
- **`_resolve_aussies_entry` had NO name-retry ladder, and that — not the
  model — was the "it ignores its own tools" failure.** `search_codex` (the
  tool) has had mechanical retries since 2026-09-23, but the resolver that
  `assess`/`compare`/`build_optimize` all route through had none, so any
  name it couldn't match exactly dead-ended. Live: for the request "godforged
  lost helmet, godforged court jester outfit, godforged arisen terror in
  hand, …", `assess` failed on EVERY item, the loop fell back to
  `search_codex`/`open_entry`, and it answered that the items "were not
  found". Two real shapes, both fixed by `_name_candidates`:
  - the QUALITY repeated inside the name (`"godforged lost helmet"`) — the
    natural thing for the model to pass, since it is how the user wrote it,
    but quality is a separate argument and the codex name is `Lost Helmet`.
    Stripped via the same `_QUALITY_NAME_TO_PERCENT`/`_FORGED_LEVELS`
    tables `_parse_quality_spec` already uses, at the EDGES only.
  - a trailing word that isn't part of the name: the user's own qualifier
    (`arisen terror IN HAND`, i.e. which slot), or a word the codex spells
    possessively so the full phrase misses — `codex_search("court jester
    outfit")` returns 0 while `"court jester"` returns `Court Jester's
    Outfit`. Dropping trailing words covers both; capped at 3 drops and
    never down to one word, and it only runs after an exact lookup already
    came back empty, so it can only turn a dead end into a hit.
  Verified: all six names from the failing request, spelled exactly as the
  user typed them, now resolve. **The lesson is the one this file keeps
  relearning** — "the model is being flaky" was wrong twice in one session
  (this, and the boost multipliers that turned out to be the header bug
  above). Check what the tool actually returned for the exact arguments the
  model sent before concluding the model is at fault.
- **`_AGGREGATE_RULE`'s counting rule** (six items named → six assess
  observations; an item listed twice is assessed once and counted twice;
  never state a bonus from your own knowledge; never change how many of
  an item the user said they have) targets a live run that made ONE
  assess call and then answered with "4 godforged helmets, 4 godforged
  outfits" and invented per-item percentages. Prompt-only, so it is a
  reduction and not a guarantee — same caveat `_CLASS_GUIDE_RULE` carries.
  Standing measurement: 2 of 3 end-to-end runs answer correctly, against
  1 of 3 before this round. The boost multipliers themselves are now
  STABLE across runs (byte-identical `calculate` expressions), where
  before the header fix they varied 9.0 vs 14.06 vs 15.0.
- **A tool must never let the loop mistake a SAMPLE for the whole set, and
  the fix for that class is a RULE plus two invariants - not a nudge per
  question shape.** Live 2026-09-25: "does Judge Trifecta drop items useable
  by mages?" answered "warrior or thief classes only", having never seen the
  four `valhallan_summoner_classes` pieces. Three separate defects, and only
  the first is specific to that question:
  * `_run_codex_search`'s observation said `"13 results for 'Judge Trifecta'"`
    and then listed the first **5** names (`results[:5]`), with nothing marking
    the cut. The model opened exactly those 5 and generalised. All name lists
    now go through `_names_observation`, which appends
    `"(+N MORE not listed - this list is PARTIAL...)"` - the same honesty the
    `open_entry` section digest already had. This is CLAUDE.md's own
    observation rule (the model cannot read what you only sent to Telegram)
    being violated where it had been written down for a year, so it is now
    pinned STRUCTURALLY: `_demo()` asserts every list-returning tool builds its
    observation through that helper and re-introduces no bare truncating slice,
    and the assert names the offending function. Add a new list-returning tool
    to that tuple.
  * `_COMPLETENESS_RULE` in the system prompt is the general half: a claim
    about a whole group (all/none/only/a count/a superlative) requires having
    observed every member; a PARTIAL observation is not the group; prefer ONE
    filtered `query` over N `open_entry` calls; and if the group genuinely
    cannot be covered, state which subset the answer rests on instead of a
    universal. Deliberately NOT a per-shape hint inside a tool's return value -
    that was the first version and it is exactly the "thousands of small tuning
    hacks" this file should not accumulate. Measured after: 7/7 runs answer the
    question correctly, and the loop now keeps working (one run opened all 13
    entries and gave the full three-way split) rather than stopping at 5.
  * **A `0` must mean "nothing matched", never "nothing was searched".**
    `useable_by` defaulted a MISSING value to `"all_classes"` - defensive for
    items (0/2764 lack it) but wrong for every other category, so raids/
    monsters/bosses matched every class filter and the raid "Judge Trifecta
    Maximus" came back as mage-useable. An absent field is now no-match. And
    `orna_aussies.unresolvable_condition_fields`, called by `_run_query_tool`
    before it runs, turns a bogus field name into an explicit "query did NOT
    run ... this is NOT an empty result" observation with suggestions, instead
    of 0 rows: the loop filtered on `dropped_by` (excluded as a cross-link
    field), got 0, and reported "drops nothing usable by mages" - the right
    answer from no evidence, and the same 0 it would have gotten had the answer
    been yes. `dropped_by` is still excluded; the data does support it
    (`dropped_by = "judge-trifecta-maximus"` matches the real 12, by ID not
    name), so wire it up properly if a request ever needs it rather than
    un-excluding it blind.
- Codex/query dead ends get the same mechanical retries `search_codex`
  always had (trailing-number-strip, space-collapse) plus two added
  2026-09-23: collapsing consecutive duplicated letters, and dropping a
  leading word — both aimed at Ukrainian→English transliteration slips
  ("клятий ортаніт" → "Cursed Ortannite"/"Ortannite", real name
  "Ortanite"; note "Cursed X" can also be a REAL item name, e.g. "Cursed
  Ortanite" the boss-family material, so the model tries the plain name
  first but isn't told to assume a modifier is always spurious).

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
immunity to stunned", "items with 'dragon' in the description") is the
loop's `query` tool over one generic evaluator, not per-query-shape
code.** The model builds `{"conditions": [...], "combinator": "and"|"or",
"category": ..., "sort_by": ..., "sort_dir": ...}` itself as the tool's
`args` (this used to be a dedicated second LLM call, `parse_conditions`/
`plan_queries`, back when `/orna` was a fixed pipeline — the condition
*vocabulary* below is unchanged, only which call produces it changed).
`orna_aussies.query_records` evaluates every condition against every
record with `_eval_condition` and combines with `all`/`any` — a single
generic evaluator, not bespoke code per query shape, so a new `kind` is
the only thing a new query type needs. `resolve_codes` (used by
`kind: "effect"` conditions) first tries a small rule-based parser for the
temp/stat/direction/magnitude pattern (`_parse_buff_query` — e.g. "T Mag
3" → `t__mag_uuu`) before falling back to fuzzy string matching against
`translations.en.json`'s ~220 simple status names ("stunned", "paralyzed",
...). **The "T." prefix means "Temp[orary]", not "Team"** — verified
directly against playorna's own served icon filenames (`"T. Def ↑"` →
`defense_up_temp.png`, vs. plain `"Def ↑"` → `defense_up.png`, same
pairing for Res) after this was wrongly assumed to mean "Team" since
before 2026-09-23; `_TEMP_RE` (formerly `_TEAM_RE`) still accepts `"team"`
as an input synonym since that's the natural guess a player makes from
the abbreviation, it just isn't what the code internally means by it.
**Temp and non-temp tiers are genuinely asymmetric in the real game
data** — e.g. non-temp "Att Down" only goes to tier 1, but "T. Att Down"
goes to tier 3 — so the valid-tiers cache (`_build_stem_directions`) keys
on `(is_temp, stat)`, not just `stat`; an earlier version merged them
into one set per stat and silently offered a non-temp tier that doesn't
exist. Verified directly against the live data before and after that
fix, not just by reading the code. Results are capped at 50
(`query_records`'s `limit`) since a single loose condition like
"mag > 250" alone can match hundreds of records.

**Query results use the exact same rich rendering as a name search -
stats/facts/sections in chat, not just a link out.** First version made
query results plain `url=` link buttons straight to aussiescodex.com,
skipping the fetch+render entirely; reverted on the same day, per
explicit feedback, once it was clear having the stats actually visible in
the chat (not just a link to tap through to) was the valuable part.
`_run_query_tool` (the loop's `query` tool) builds playorna-shaped entries
and reuses `_result_list_keyboard`/`_send_entry` exactly like a
`search_codex` name search does. aussiescodex only earns a place as a
single **"📊 Assess"** link
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
all live there too.** An earlier version of the condition-extraction
prompt hardcoded a 10-field enum, which silently broke any query for a stat
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
condition kind.** The `query` tool's `args` carries `sort_by`/`sort_dir`
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
*description* ("Bladeless"'s), not its name - the model naturally reaches
for `search_codex` first for a bare name-shaped ask since there's no
stat/effect/attribute language to suggest `query`, so the name search has
to be the one that recovers rather than expecting the model to somehow
guess this belongs to the other tool.

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
   surfaced a real regression risk: loosening `_TEMP_RE`'s (then
   `_TEAM_RE`, see the terminology-correction note above) trailing
   `\s+` to `\s*` (needed so `"t.mag"`, no space, is recognized as a temp
   prefix) broke `"team ..."` inputs, because the alternation
   `(?:t\.?|team)` tried the single-letter `"t"` branch first and
   matched just that, leaving a mangled `"eam attack..."` behind.
   Reordering to `(?:team|t\.?)` (longest/most-specific alternative
   first) fixed both without reintroducing the old requirement for a
   space after `t.`.
2. Even with (1) fixed, the condition-extraction prompt itself was only
   reliably preserving `"++"` into its `value` output about 5/8 of the
   time - the rest either invented a wrong tier number, silently dropped
   the tier, or (once) fabricated a nonexistent `"t_mag"` stat field.
   Added explicit prompt guidance: tier shorthand is always
   `kind:"effect"`, never `"stat"`, and must be copied into `value`
   character-for-character, not re-notated or guessed. Verified 8/8
   after the prompt change.

**A trailing stray number on an otherwise-valid codex name falls back to
the name with the number stripped** (`_run_codex_search`, alongside the
existing "rainsong" space-collapse and description-substring fallbacks).
Live bug: "/orna solarite 12345" found nothing even though "Solarite" by
itself has 2 results - the model passed the number through verbatim since
it had no way to know it's noise rather than part of the name.

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

**`/go` declares its action names as `tools` too (`_GO_TOOLS`), for the
same reason `/orna` does** — its prompt is the identical "pick one of
these named actions" shape (`search`/`youtube`/`open`/`calculate`/`ask`/
`finish`), which makes a Harmony-format model emit a native tool call that
Ollama then can't map back, logging `no reverse mapping found for function
name` and 500ing about a third of the time. No model in `/go`'s config is
Harmony-format today, but `gpt-oss:20b` was the local default until
2026-09-24 and is still installed — this was one env-var away, on the one
feature used where debugging isn't an option (slow plane wifi). The prompt's
action enum is now derived from the same `_ACTIONS` tuple, and came out
byte-identical, so this added the tools array and changed nothing else.

**"The model said something unusable" is NOT "the service is down", and
conflating them takes cloud away from everyone.** `ollama_client.
OllamaUnavailable` (a subclass of `OllamaError`) marks a request that never
produced a reply — timeout, connection failure, HTTP error status — and
ONLY that subclass parks the cloud circuit breaker. A reply that arrived
but wasn't usable JSON stays a plain `OllamaError`: still worth falling
back over for that turn, never worth a 300s cloud blackout for every other
request. Live 2026-09-24, within minutes of switching `/orna` to
nemotron-3-super: it answered one step with plain prose instead of the
requested JSON, and because that raised a bare `OllamaError` the breaker
parked cloud for five minutes. Measured afterwards, that model returns
usable JSON 6/6 on the same step shape — so the bad reply was a one-off and
the breaker's over-reaction was the actual defect. This is the second bug
of exactly this shape in one session (see the no-vision 400 below): when
adding a failure path, ask whether it means *unreachable* or merely
*unhelpful*, because the breaker only ever belongs on the first.

**Ollama has TWO different wordings for "this model can't take images",
and only catching one of them is actively harmful under cloud-first
routing.** `ollama_client._is_no_vision_error` matches both: local Ollama
says `does not support multimodal requests`, Ollama Cloud says `this model
does not support image input`. The original check looked for `"multimodal"`
only, so the cloud phrasing fell through as a generic `OllamaError` — which
is indistinguishable from an outage, so it would trip the cloud circuit
breaker and park cloud for 300s **for every caller**, degrading `/orna`
because someone sent `/go` a photo. Found 2026-09-24 while trialling a
vision-less `GO_MODEL`, by actually POSTing an image and reading the 400
body rather than trusting the existing check — the graceful path (drop the
image, note it in the message, retry once) silently wasn't running. Pinned
in `ollama_client._demo()`.

**Model configuration is three separate settings, deliberately.**
`OLLAMA_MODEL` (local, shared by `/orna`, `/go` and `telegram_nlp`),
`GO_MODEL` (`/go`'s cloud) and `ORNA_CLOUD_MODEL` (`/orna`'s cloud,
defaulting to `GO_MODEL` so it changes nothing unless set). The split
exists because the two loops want different things: `/orna` is text-only
and wants the biggest reasoner available, while `/go`'s "Continue" button
attaches photos and needs VISION. Live 2026-09-24: `/orna` moved to
`nemotron-3-super` (120B, tools+thinking, no vision) while `/go` stayed on
`gemma4:31b` for exactly that reason — a single shared setting would have
silently cost `/go` its images. Local moved to
`nemotron-3.5-lightning:30b-mlx` (30B MoE, 3B active, MLX-native): measured
on the real `/orna` prompt at 1.9–2.2s per step after a 16s cold load,
against `gpt-oss:20b`'s 5–20s, and it isn't Harmony-format either.

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

**Multi-part requests ("best mag item for thieves and for mages", "legs
and head for mage, mag > 50") are the loop calling `query` more than
once, not a batched multi-block schema.** Before the 2026-09-23 loop
rewrite, this was `plan_queries`'s job - one call returning several
independent condition *blocks* in one shot, deliberately built that way
instead of a full ReAct loop specifically because looping local-only
Ollama seemed likely to multiply flakiness across steps. The loop
supersedes this entirely: the prompt's `_AGGREGATE_RULE`/multi-part
example tells the model to call `query` (or `search_codex`) once PER
distinct thing, observe each result, then `finish` once with a wrap-up -
more genuinely ReAct-shaped (the model can react to one slot's result
before deciding how to search the next) and no separate batching schema
to maintain. `ask`'s button-only, never-free-text, don't-ask-twice design
carried over unchanged from the old pipeline (which itself modeled it on
`/go`'s own "ask" action) - a local model's own clarifying questions are
exactly as unreliable as everything else it produces, so a free-text
follow-up would just compound that uncertainty rather than resolve it.
A real bug surfaced during the original `plan_queries` verification and
still applies: the natural class nickname a player types ("mage") often
isn't a literal substring of the stored `useable_by` value ("magic_users"
contains "magi", not "mage") - `_USEABLE_BY_ALIASES` in `orna_aussies.py`
maps common nicknames (mage/mages, warrior(s), thief/thieves/rogue(s),
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

A **fourth** encoding turned up later (2026-09-23, during the ReAct
rewrite, fixing the live "which follower gives earth sigil" report):
followers don't have `stats["+spell"]`/`"ability"` at all - a spell grant
lives in `record["bestial_bond"]`, a list of bond tiers each holding
`{name, type, chance?}` entries, where `type == "ABILITY"` means `name`
is a spell/skill slug (e.g. `earth-sigil-2` on both Ancient Jinn and
Anubis). `_eval_condition`'s `"ability"` branch now also scans
`bestial_bond` tiers for `ABILITY` entries; its `"effect"` branch
similarly scans `bestial_bond` tiers with `type == "BOND"` (a status-code
proc) when `field` is `""`/`"gives"`. `type == "BONUS"` entries (passive
%s like `orn_bonus`) are deliberately left unwired - no report has asked
for these yet, and they'd need a new condition shape, not a fit into
`"ability"`/`"effect"` - marked with a `# ponytail:` comment in
`orna_aussies.py` noting the gap.

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
`codex_search`. The gap (this predates the ReAct rewrite but the fix still
applies to the loop's own tool-choice prompt): the routing guidance only
described `query` in terms of stat/effect/attribute asks, never mentioning
that a name fragment PLUS a restriction is really two conditions ANDed
together (`kind:"text"` on name + an attr condition) - exactly what
`orna_aussies.query_records` already handled fine once routed there
correctly (verified directly: `"last martyr"` as a name-text condition
+ `useable_by="mage"` correctly narrows 16 results down to 4). The
system prompt's condition rules carry an explicit rule + example for this;
verified 9/9 across 3 phrasings that a fragment+filter request calls
`query` while a bare fragment (`"last martyr"` alone) still correctly
calls `search_codex`.

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

**`/report <опис>` — the one command aimed at people who CAN'T use the
admin ones.** Deliberately ungated, unlike `/stats`/`/update_codex`/`/go`:
the guild members who hit bugs are exactly the ones an allowlist shuts out.
Each report is stored per user (`usage_stats.record_report`, newest
`MAX_REPORTS_PER_USER = 10` kept, the 11th dropping the oldest so one noisy
reporter can't push everyone else out) **and pushed to every
`GO_ALLOWED_USER_IDS` chat immediately** — a report nobody is told about is
just a log line, and every bug fixed in this bot so far arrived as a
message, not as a stored record. Notification is best-effort per recipient
inside its own try/except: one admin who blocked the bot must not swallow
the report for the others, and the reporter has already been told it was
saved either way. An admin filing a report isn't notified about themselves.
Readable back via `/stats reports` (newest first, across all users), which
is what stops the store being write-only. Listed in `set_my_commands` and
in the `/start` welcome, since a bug-report command nobody can find is
worth nothing. `all_reports()` reverses each user's list BEFORE the sort:
timestamps have second resolution, so several reports in the same second
compare equal and a stable sort would otherwise leave them oldest-first
inside a newest-first list.

**`/stats reset` needs a second word, not a button.** It is destructive and
irreversible, and this chat is full of keyboards from earlier messages - a
mis-tap must not be able to wipe the counters, so it takes
`/stats reset confirm` (or `/stats reset all`) and a bare `/stats reset`
only shows what would be cleared. `usage_stats.reset()` deliberately keeps
two things that live in the same store but are NOT statistics: **saved
timezones** (clearing them would silently force every member to re-pick
their zone before their next reminder could be scheduled) and **bug
reports** unless `all` is given (an unread report is work waiting, not a
number). It also keeps `_user_names`, the id → display-name map that makes
a later `/stats users` readable. It returns what it cleared so the reply
states it rather than just claiming success.

**Usage counters (`usage_stats.py`)** — `record_command_for(update, name,
text)` at the top of every slash-command handler (a thin wrapper around
`record_command` that pulls `user_id`/`username`/`first_name` straight
off the `Update` so call sites don't each repeat that extraction),
`record_llm_call(model, backend)` inside `ollama_client.chat_json` itself
(the one place both cloud and local calls now actually go through -
`"cloud"` when `host == OLLAMA_CLOUD_HOST`, `"local"` otherwise; this
used to be called separately from `telegram_nlp._chat_json_once` and
`telegram_go._chat_json` before those collapsed into the shared client),
all persisted to `usage_stats.json` (gitignored, same
reload-survival reasoning as `reminders.json`). Deliberately scoped to
slash commands only - the free-text conversation entry points in
`telegram_assess.py`/`telegram_resources.py` aren't instrumented yet, so
"questions to the bot" undercounts by however much traffic comes in that
way rather than via a command. `record_llm_call` is called once per
actual HTTP attempt (including retries `telegram_nlp._chat_json`'s
wrapper makes), not once per logical "ask" - a retried call counts
twice, which is the more useful number for understanding real load on
Ollama. The model counter key is `"<model> (<backend>)"`, not just the
bare model name - added after a live ask ("not clear if local or cloud
model was used") made clear the raw name alone doesn't say which, and
that distinction is exactly the one that matters (cost, latency,
capability).

**`/stats` is per-user aware, not just a global total** - same live ask
("not clear which user was asking... let it see stats per user").
`usage_stats` keeps, per user id: a command counter, a last-seen display
name (`@username` if set, else first name), and a capped log
(`MAX_LOG_PER_USER = 40`, oldest dropped) of `{command, text, ts}` for
their actual recent questions - not just counts, the real text they
typed, so an admin can see *what* was asked, not only *how much*.
`/stats` (no args) stays the global summary (now also showing
`user_count`); `/stats users` lists every known user sorted by activity;
`/stats user <id|@username>` (via `usage_stats.find_user`, resolving
either form) shows that user's per-command breakdown plus their last 40
questions with timestamps. All three still gated by the same
`GO_ALLOWED_USER_IDS` allowlist `/go`/`/update_codex` use - this is
explicitly an admin surface, storing per-user activity logs is only
appropriate because of that gate.

**Tool-call counters (`usage_stats.record_tool_call`)** — every loop
action (`today`/`next`/`need`/`search_codex`/`query`/`events`/
`open_entry`/`knowledge_search`/`web_search`/`calculate`/`assess`/
`compare`/`build_optimize`/`towers`/`class_guide`/`ask`/`finish`, plus
the synthetic `_step_budget_exhausted` when a session runs
out of steps without finishing) increments a `usage_stats._orna_tools`
counter, surfaced in `/stats` as a "Дії /orna (ReAct loop)" section
(`telegram_bot.handle_stats`). This is what replaced `route_query`'s old
intent counters conceptually - there's no separate intent classification
step anymore, so "which intent fired" is now just "which tool the model
chose first," visible the same way.

**Fixed-text shortcuts for meta/"help" and reminder-shaped asks are
embedded verbatim in the loop's own system prompt, not a separate
classifier intent.** Before the ReAct rewrite, `route_query` had two
dedicated intents (`"other"` for a meta "what can you do" ask, `"need"`
for a quantity-bearing resource request) that short-circuited straight to
fixed replies or a different report. In the loop, both are just prompt
guidance telling the model what to `finish()` with, not special-cased
control flow:
- `telegram_orna._orna_system_prompt()` interpolates
  `_capabilities_text()!r` and `_REMINDER_NUDGE!r` directly into the
  prompt text with an instruction to copy either string **verbatim** into
  `finish()` when the ask is a meta "what can you do"/"допоможи" question
  with no real Orna subject, or shaped like a reminder request ("нагадай
  мені...", "remind me to..." - that's `/remind`'s job, `/orna` doesn't
  set reminders itself). Both stay fixed, deterministic text (not
  model-generated prose), same structured-over-freeform reasoning as
  everywhere else in this module - `_capabilities_text()` deliberately
  only mentions genuinely public commands (`/orna`, `/res_today`,
  `/res_next`, `/remind`), `/go` and its hidden siblings stay unlisted.
- `"need"` is now a real tool, `_run_need_tool(message, text)` - a
  quantity-bearing resource request ("треба 1000 балоріту") reuses the
  exact same `extract_resources`/`extract_quantities` extraction +
  `build_report`/`send_report_blocks` pipeline the free-text `/need` flow
  (`telegram_resources.py`) already has, instead of `next`'s bare date
  lookup with no proof-cost math. `text` is deliberately the UNTRANSLATED
  original request (both extraction calls already handle Ukrainian
  directly) - translating first would only risk mangling a material name
  before the exact-match step that needs it. A material extracted without
  a resolvable quantity falls back to `_next_text` (still useful, just
  without proof math) rather than opening a second clarifying round-trip -
  the loop already has `ask` for that if the model chooses to use it, no
  need for `need` to special-case it. If nothing in the request resolves
  to a known Material Forecast material at all, it falls through to
  `_run_codex_search` (same "let the next honest attempt take over"
  pattern as `next`'s own dead end).

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
- **A typed answer works for EVERY ask, not just the escape hatch.** Live
  2026-09-24: the bot asked, the user typed the full answer, and nothing
  happened - `_PENDING_ASK_TEXT` was armed only when the "Своя відповідь"
  button was tapped, so a perfectly good reply fell through to the other
  handlers and the request looked stuck with no status message. Posting an
  `ask` now arms the wait, and **tapping a real option clears it again**, so
  it can't capture an unrelated message once the question is answered.
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
- **An empty parse is never cached** (same guard as `orna_releases`) -
  aussiescodex is a JS app and pinning "there are no crucibles" for a week
  would be worse than retrying.
- Re-crawl: automatic on the 1-week TTL, and `/update_codex` forces it now
  alongside the codex, patch notes and sheets.
- Surfaced through `knowledge_search`, not a 19th tool - the prompt is
  ~30KB and this is another *provenance* of answer, not another question
  to ask. Verified end to end: "які слоти можуть мати crucible на avidity
  і який максимальний відсоток?" → all five slots, max 10%, cited.

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

- `orna_releases.py` mirrors `orna_aussies.py`'s cache exactly: disk cache
  in a gitignored dir, 1-week TTL, atomic temp-then-rename write, an
  unreadable cache treated as a miss (a file truncated by one of this
  repo's frequent `launchctl` reloads would otherwise raise on every call
  until the TTL expired). One addition: **an empty parse is never cached** -
  that would pin a silent "no patch notes exist" for a week if playorna's
  markup changed, so it raises instead.
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

### The ephemeral status message

A `/orna` request can legitimately run for minutes (`MAX_STEPS = 16`, plus
the `LOOP_TIMEOUT_SECONDS` ceiling), and the chat was previously silent for
all of it except whatever tools happened to post - no way to tell a working
request from a stuck one. `_Status` sends ONE message on the first update
("🤔 Думаю…"), EDITS it in place for each step ("🔎 Шукаю в кодексі…",
"📚 Читаю гайд…", from `_ACTION_LABELS`), and DELETES it when the request
ends, so a finished conversation reads exactly as it did before this
existed. Added 2026-09-24 on ask.
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

### `finish()` carries a "📚 Джерела" button - what the answer was actually built from

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
  the chat forever - tapping the button an hour later should still work.

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
