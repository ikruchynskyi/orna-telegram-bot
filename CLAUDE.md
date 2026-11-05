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

## Layout

```
telegram_bot.py   entry point (launchd runs it from the repo root)
paths.py          every file location: data/, data/cache/, data/state/
bot/              Telegram handlers (telegram_*.py) + usage_stats
llm/              ollama_client (THE Ollama call path), telegram_nlp
orna/             game data and math: codex, aussies, assess, towers, sheets, ...
knowledge/        corpora readers, Pinecone + keyword retrieval, Discord/Reddit search
scrapers/         corpus builders - run as `python3 -m scrapers.<name>`
tests/            `python3 tests/orna_test_suite.py`
data/             corpora (committed); cache/ and state/ are gitignored
deploy/           Dockerfile*, docker-compose.yml, install.sh
```
Imports are package-qualified (`from orna import orna_codex`); run a module's
self-check with `python3 -m <package>.<module>` from the repo root. Never build a
file path from `__file__` or the working directory - use `paths`.

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
  `python3 -m knowledge.orna_pinecone [ns ...]`. Also feeds the PLAN call a short primer (`_plan_primer`).
- `orna_textindex.py` → `.textindex.sqlite3` - SQLite FTS5 over the same chunks; the fallback when
  Pinecone is off/failing. Rebuilds itself when a source file is newer.
- `orna_questline.txt` (`scrapers/orna_scrape_questline.py`) - Konq's story questline guide, one section per quest.
- `orna_discord_search.py` - Discord through a Selenium Chrome logged in as the user (`.discord_chrome/`;
  user-account automation, ToS risk accepted by the user). `harvest`: FAQ/guide channels + pinned posts,
  images transcribed by a vision model (new images only; cached per attachment) → `.discord_cache/` →
  `discord` namespace.
- `orna_reddit_search.py` - live r/OrnaRPG search through an off-screen, NON-headless Chrome
  (`data/state/reddit_chrome`). Since 2026-11 Reddit blocks every `.json` from it ("network security"), so it
  opens the search and thread pages as a reader would and reads posts/comments/scores out of the DOM
  (`_SEARCH_JS`, `_THREAD_JS`, run via DevTools). 6 s between page loads: rapid loads got a CAPTCHA and a 429;
  a block/CAPTCHA/429 page raises, never retried around. Threads rendered like `orna_qa` (`orna_scrape_qa._block`).
- `community_search` action = Reddit + Discord live search in parallel: last resort, refused in code until
  `knowledge_search` AND `web_search` ran. What it finds is kept (`.reddit_cache/live.json` → `qa`
  namespace; `.discord_cache/live.json` → `discord_live`) so the next ask hits `knowledge_search`.
- `orna_material_names_uk.*` - static EN↔UK material names.

## `/orna` loop - what you need to know to change it

- One action per step, JSON `{thought, action, action_input, args}` - or `parallel` with up to `_PARALLEL_MAX`
  independent `_PARALLEL_OK` calls run together (several native tool calls in one reply become one). `_ACTIONS` is the single
  list feeding both the prompt's action enum and `_STEP_TOOLS` (native tools declaration -
  required, or gpt-oss emits undeclared tool calls → Ollama 500s).
- Budget: `MAX_STEPS`, `LOOP_TIMEOUT_SECONDS`; running out SUMMARISES via `_close_out`, never blank.
- English inside the loop; translation gates at input (`build_loop_messages`) and output.
  Proper nouns (items, classes) are never translated; general game terms use the game's own Ukrainian
  (`data/orna_terms_uk.json` from `scrapers/orna_scrape_terms_uk.py`: Tier = Ранг, Followers = Компаньйони).
- Every request gets a PLAN call (gemma4) and a REVIEW call before posting; REVIEW runs on `REVIEW_MODEL`,
  never the step model (it graded its own drafts). Each rewrite is logged with its draft in
  `data/state/orna_reviews.jsonl` - judge from it whether rewrites help. PLAN also gets the recipe rules
  (`_STRATEGY_RULE`, `_CLASS_GUIDE_RULE`, `_OPTIMIZE_RULE`). `_swaps_a_number` rejects a rewrite only when it
  is a near-identical text with a changed number, or brings a number the evidence lacks.
- Build questions (`_OPTIMIZE_RULE`): every crucible side by side (`orna_bonuses.overview()`, on any crucible
  query), the pet's per-skill act/landing estimate in `research` (`orna_aussies._act_summary`, the Beowulf
  guide's act-rate formula), then `build_check` - one result with every mechanic that can decide a build
  (CONTEXT, PIPELINE, CAPS, STACKING, SOURCE & RELIABILITY, SATURATION, SLOTS, TRADE-OFFS, FRESHNESS): each
  dimension heads with the curated sentences of `orna_mechanics.txt` "Build math" (+ formula, stat bonuses),
  then focused search passages; CAPS lists where the cap can be raised (`orna_bonuses.where_rolls`). REVIEW
  sees a whole checklist and point 9 checks it. Spec/plan: `docs/superpowers/*/2026-11-04-orna-build-reasoning*`.
- Context: `_compact` sends the last `_KEEP_FULL_RESULTS` tool results whole and cuts older long ones (the
  repeat cache returns the full text); `_working_state` is appended to every step.
- Freshness: `knowledge_search`/`research` append `orna_releases.search_strict` patch notes; they win over the
  undated corpora. Ranking adds `_recency` (dated passages: newer up, 3+ years down) and the result legend
  says the newer dated passage wins a disagreement. `_RANKED_MAX` 10000, `_HIT_MAX` 3000 (benchmark: same 12/13 as 16000).
  Removed game content is scrubbed from the corpora by `scrapers/removed_content.py`.
- Self-written knowledge (`knowledge/orna_curated.py`): `corrections` (outranks everything; from
  `python3 -m scrapers.patch_audit` - patch notes vs the corpora, human review in `data/patch_audit_report.md`,
  rejections in the cache's `_rejected` - and admin `/correct`) and `verified` (admin `/verify`).
- Feedback (`bot/orna_feedback.py`): every answer logged; reactions are votes (groups: only if the bot is
  admin); `/feedback`, `/verify`, `/correct`.
- Reddit refresh: `python3 -m scrapers.reddit_delta [--dry-run]` - new items only, novelty-judged, appended,
  tail upserted. With REDDIT_CLIENT_ID/SECRET or REDDIT_COOKIE it uses JSON; otherwise the dev listings come
  from RSS and r/OrnaRPG from the `/new/` + thread pages (2026-11-04 the `/new/` page only reached Jul-Aug
  posts - check its dates before trusting a run to cover the latest month).
- Discord threads: `python3 -m scrapers.discord_threads [fetch|summarize|pending N [SINCE]|accept F|index]` -
  the official server's #discussion-threads, only threads with >50 messages and only windows since 2025 (owner's
  scope), through the logged-in Discord profile, delta per thread. ~9k-char windows become dated facts (cached
  per window): `summarize` uses cloud deepseek (`SUMMARY_MODEL`; paid only after the free quota), or
  `pending`/`accept` lets Claude write them. Namespace `discord_threads`, only new chunks upserted. The 2026
  windows are done; the 2025 ones are not.
- ORN feedback: `python3 -m scrapers.orn_feedback [fetch|stale]` - #orn-feedback, the ORN's own per-event
  summaries (first post edited in place, so every thread is re-read and a text hash marks stale facts).
  Facts live in `data/cache/discord/orn_feedback_facts.json` (written by hand), go into the same
  `discord_threads` corpus via `discord_threads.write_corpus()`; run `discord_threads index` after.
- Monthly refresh: `python3 -m scrapers.refresh` (launchd: `deploy/com.username.ornarefresh.plist`) re-scrapes,
  re-indexes what changed (a corpus that shrank >30% is put back), runs the delta steps and tier 0, writes
  `data/cache/logs/refresh-<date>.md`. It commits nothing.
- Eval suite: a case's `follow_ups` are later turns (`continue_conversation`, the /clarify path); the suite
  runs `_maybe_plan` like `_start_orna` - without it `original_request` was empty and the finish gates
  never fired.
- Continuity: a reply to a message an /orna conversation sent continues it (`_ConversationRecorder`,
  `_ReplyToOrnaFilter`); sessions live 60 min; `/me` profiles (`data/state/orna_profiles.json`) ride along.
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
   `_SOURCE_NOTES` - then `python3 -m knowledge.orna_pinecone <ns>`. `knowledge_search` returns ONE ranked list
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

- All calls go through `ollama_client.chat_json` (`think: True`, except the local leg: `LOCAL_THINK`, off for
  qwen to cut its step time, always on for gpt-oss - `False` breaks it).
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
  retrieval benchmark (`_RETRIEVAL_CASES`, needs `PINECONE=1`: 12/13 on 2026-10-06) - the number to watch
  when changing chunking, `MIN_SCORE`, top-k or `_SOURCE_NOTES`.
  Full gate: `TIER=0,1,2,3 N=3 python3 tests/orna_test_suite.py`.
- Suite and harness run with Pinecone OFF (keyword index only) - free plan egress is 1 GB/month.
  `PINECONE=1` opts in; use it only when changing retrieval, with `N=1`.
- Pure logic you add gets an `assert`-based `_demo()` in its module.
- End-to-end: `Q="..." N=5 python3 .claude/skills/verifying-orna-changes/scripts/orna_loop_harness.py`
  (`FORCE_LOCAL=1` for local only). Env: `set -a && source .env && set +a`.
- Don't benchmark against public Reddit answers - the bot's corpora and web search contain them.
