# CLAUDE.md

How to develop this bot. User-facing setup: `README.md`. `/orna` loop deep
dive: `docs/orna-loop-internals.md`. Old long-form rationale/incident log:
`docs/claude-md-history.md` (grep it only when you need the "why" of a
specific rule). Game mechanics: the `orna-game-mechanics` skill. Debugging and
verifying: the `verifying-orna-changes` skill.

## Stack & running

- Python 3, `python-telegram-bot` (async). No framework, no build. `python telegram_bot.py`.
- Live services, never mocked: Google Sheets, playorna.com codex, aussiescodex dump,
  Ollama (cloud first, local fallback), Tavily, Discord.
- Deployed via launchd: `~/Library/LaunchAgents/com.username.telegrambot.plist`
  runs THIS repo, loads `./.env`. Reload with `launchctl unload` + `load`.
  Log: `telegrambot_error.log` (`orna:` / `go:` prefixed lines).
- launchd `PATH` is minimal: call binaries by absolute path (`FFMPEG_PATH`,
  `FFPROBE_PATH`, `YTDLP_PATH`); every yt-dlp call passes `--ffmpeg-location`.
- Docker/compose files exist but have never been built - build before relying on them.

## Module map

Telegram features:
- `telegram_bot.py` - entry point; handler registration (ORDER MATTERS, see below).
- `telegram_orna.py` - `/orna`, a ReAct loop over all Orna tools. Biggest module after `/go`.
- `telegram_assess.py` - screenshot OCR → item assessment; hands off offerings/amity screens.
  In groups, only amity/offerings screenshots get a reply; OCR text goes to log only.
- `telegram_offerings.py` - altar "NEEDED OFFERINGS" OCR parsing.
- `telegram_resources.py` - "what do I need" conversation; shared `build_report`,
  `send_report_blocks`, `pre_table`; reminder buttons.
- `telegram_amity.py` - memory-hunt coordination, `/amity`, `/iam`.
- `telegram_remind.py` - hidden `/remind`; `schedule_reminder` is the ungated public entry.
- `telegram_announce.py` + `orna_discord.py` - relay followed Discord announcements, translated to UK; redeem codes never translated (`protect`/`restore`).
- `telegram_go.py` - hidden `/go` (non-Orna personal agent, video pipeline). Also hosts shared `_Status`.
- `usage_stats.py` - counters, `/stats`.

LLM:
- `ollama_client.py` - THE only Ollama call path (`chat_json`, `chat_json_with_fallback`, circuit breaker).
- `telegram_nlp.py` - material/quantity extraction.

Data sources (each = reader module + cache or committed file + scraper):
- `orna_codex.py` (playorna per-page JSON), `orna_aussies.py` (aussiescodex dump, `query_records`, `build_supergraph`),
  `orna_codex_db.py` → `codex.sqlite3` (read-only SQL over the dump; FTS5).
- `orna_classes.py/.json` (class/spec stats, `scale`), `orna_assess.py` (quality/upgrade math),
  `orna_proofs.py`, `orna_sheets.py`, `orna_towers.py` (port of OrnaCodex `tower.ts`),
  `orna_calendar.py`, `orna_releases.py`, `orna_bonuses.py`, `orna_monuments.py`.
- Text corpora for `knowledge_search`: `orna_knowledge` (sheets), `orna_echo` (+ `orna_ornabook.txt`),
  `orna_mechanics.txt` (ours), `orna_reddit` (dev comments), `orna_qa` (player Q&A).
  `orna_guides` + `orna_guide_*.txt` back `class_guide`.
- `orna_pinecone.py` - semantic search over those text corpora (one namespace each; `PINECONE_API_KEY`).
  `_units(ns)` chunks every corpus with its own module's parser. Re-index after re-scraping:
  `python3 orna_pinecone.py [ns ...]`. Also feeds the PLAN call a short primer (`_plan_primer`).
- `orna_textindex.py` → `.textindex.sqlite3` - SQLite FTS5 over the same chunks; the fallback when
  Pinecone is off/failing. Rebuilds itself when a source file is newer.
- `orna_questline.txt` (`scrapers/orna_scrape_questline.py`) - Konq's story questline guide, one section per quest.
- `orna_discord_search.py` - Discord through a Selenium Chrome logged in as the user (`.discord_chrome/`;
  user-account automation, ToS risk accepted by the user). `harvest`: FAQ/guide channels + pinned posts,
  images transcribed by a vision model (new images only; cached per attachment) → `.discord_cache/` →
  `discord` namespace.
- `orna_reddit_search.py` - live r/OrnaRPG search through an off-screen, NON-headless Chrome
  (`.reddit_chrome/`; headless gets 403). Threads rendered like `orna_qa` (reuses `orna_scrape_qa._block`).
- `community_search` action = Reddit + Discord live search in parallel: last resort, refused in code until
  `knowledge_search` AND `web_search` ran. What it finds is kept (`.reddit_cache/live.json` → `qa`
  namespace; `.discord_cache/live.json` → `discord_live`) so the next ask hits `knowledge_search`.
- `orna_material_names_uk.*` - static EN↔UK material names.

## `/orna` loop - what you need to know to change it

- One action per step, JSON `{thought, action, action_input, args}`. `_ACTIONS` is the single
  list feeding both the prompt's action enum and `_STEP_TOOLS` (native tools declaration -
  required, or gpt-oss emits undeclared tool calls → Ollama 500s).
- Budget: `MAX_STEPS`, `LOOP_TIMEOUT_SECONDS`; running out SUMMARISES via `_close_out`, never blank.
- English inside the loop; translation gates at input (`build_loop_messages`) and output.
  Proper nouns (items, classes) are never translated.
- Complex requests (`_looks_complex_request`) get a separate PLAN call and a REVIEW call before posting.
- `_confidence_gate` clamps self-reported confidence by actual evidence.
- Chat output: browse tools (`open_entry`, `search_codex`, `query`, `research`) post NOTHING,
  they record entries; `finish()` auto-posts ≤2 cards. Computed results (assess, estimate_stats,
  towers, today/next, need, build_optimize) post their own output. Inline mode collects into one message.
- Follow-ups go through `/clarify`; `ask` options must be real answers; `MAX_ASKS_PER_REQUEST` enforced in code.

### Adding or changing a tool - the rules

1. **Observation honesty**: the model only sees what the tool RETURNS. Any truncation/cap
   is marked `PARTIAL` with the true total. Never report a capped length as a count.
2. **Close it in the tool, not the prompt**: if a tool can return a valid-looking but useless
   answer (empty table, invented input, illegal loadout), make the tool refuse with an
   observation. Missing user input → prefix `NEEDS_INPUT:`. Prompt rules are advice, ~70-90% reliable.
3. **Blame the tool before the model**: check the tool's output for the exact args first.
4. **Never block the event loop**: disk/HTTP/CPU work goes through `asyncio.to_thread`.
5. **New knowledge source ≠ new tool**: add it as a Pinecone namespace - a branch in
   `orna_pinecone._units` + `NAMESPACES`, a top-k in `_VECTOR_TOP_K`, a tag and trust note in
   `_SOURCE_NOTES` - then `python3 orna_pinecone.py <ns>`. `knowledge_search` returns ONE ranked list
   across namespaces (never fixed per-source blocks: a total cap then drops the best hit). Not a new
   action - the prompt is already large. Run the retrieval benchmark (tier 0) before and after.
6. Add a label in `_ACTION_LABELS` (status line) and cite with `_add_source`.
7. Don't put a number in prose/corpora that the code computes - derive it from the code.
   Code-verified sources outrank prose (`towers`, `releases` > guides).

## Data conventions

- **Cache pattern** for fetched sources: gitignored disk cache, TTL, atomic write
  (temp + `os.replace`), unreadable = miss, **never cache an empty/partial parse**.
  `/update_codex` refreshes codex + DB + notes + knowledge together.
- Codex records carry no names - names are in `translations.en.json`.
- Use `AssessResult` projected per-level values, not `entry.stats` (unupgraded base).
- Item defaults: quality 100%, level 1. Quality and level are independent.
- `two_handed` is a codex TAG; weapon subtype says nothing.
- SQLite connection is `mode=ro`: that is the write guarantee.
- `orna_sheets.GUILD_NAMES` and `orna_proofs.GUILD_PROOFS` encode the same guild order - keep in sync.
- State files (`reminders.json`, `amities.json`, ...) are written atomically; don't bind-mount them individually.

## Telegram conventions

- Model text → `_markdown_to_html` + HTML parse mode (fallback to plain on `TelegramError`).
- Tables: `telegram_resources.pre_table` (aligned `<pre>`). Long reports: `send_report_blocks` chunks.
- **Handler order is load-bearing**: assess conversation before resources conversation.
- **Free-text prompts**: use the pending-filter pattern (`filters.MessageFilter` matching only
  a chat with a live pending ask, registered before the conversations). Never a bare `MessageHandler`.
- Bot lives in a ~180-person guild chat: minimise buttons; prefer plain text/links.
  Budget button label width (Telegram clips silently).
- `concurrent_updates(32)` is on; handlers must be safe to overlap.
- OCR regexes are deliberately unanchored (junk before each row).

## LLM conventions

- All calls go through `ollama_client.chat_json` (`think: True` always - `False` breaks gpt-oss).
- Ollama Cloud caps concurrent requests per account: `OLLAMA_CLOUD_CONCURRENCY` (process-wide), 429 is
  retried then `OllamaBusy` (local for that call, never parks the cloud). Batch jobs share the cap.
- **Prompt text is written ~80% of the way to ASD-STE100** (Simplified Technical English): one
  instruction per sentence, imperative, ≤20 words for an instruction and ≤25 for a description, one
  term per concept, numbered steps for a procedure. The other 20%: schemas, name lists, verbatim
  user-facing text and exact examples stay as precise as they need to be. Applies to the system
  prompt, tool descriptions, rules, PLAN/REVIEW prompts and tool observations meant for the model.
  - A principle that applies to every tool goes in `_GENERAL_RULES`, ONCE - never restated per tool.
  - No incident stories or rationale in prompt text ("live failure: ..."): the model reads it on every
    step. Put the why in a code comment or `docs/orna-loop-internals.md`.
  - Measure a prompt change end to end against the previous prompt (`TIER=1,2,3 N=2`) before keeping it.
- Check a cloud model's capabilities via `POST https://ollama.com/api/show`, don't guess.
- Local model is non-deterministic: one green run proves nothing; verify over N runs.

## Verifying

- `python3 tests/orna_test_suite.py` - tier 0, no LLM, must be 100%. Runs every module's `_demo()`, plus the
  retrieval benchmark (`_RETRIEVAL_CASES`, needs Pinecone: 12/13 on 2026-10-06) - the number to watch
  when changing chunking, `MIN_SCORE`, top-k or `_SOURCE_NOTES`.
  Full gate: `TIER=0,1,2,3 N=3 python3 tests/orna_test_suite.py`.
- Pure logic you add gets an `assert`-based `_demo()` in its module.
- End-to-end: `Q="..." N=5 python3 .claude/skills/verifying-orna-changes/scripts/orna_loop_harness.py`
  (`FORCE_LOCAL=1` for local only). Env: `set -a && source .env && set +a`.
- Don't benchmark against public Reddit answers - the bot's corpora and web search contain them.
