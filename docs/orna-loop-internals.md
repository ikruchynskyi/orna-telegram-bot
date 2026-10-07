# `/orna` loop internals & incident history

Moved out of CLAUDE.md (2026-09-27) to keep that file readable. This is the full design rationale of the `/orna` ReAct loop: the complete `query` condition vocabulary and field-resolution rules, every tool's design, and the incident history (routing schemes, timeouts, the many "Live 2026-…" post-mortems) behind each rule.

CLAUDE.md's `## The /orna command` section carries the short design summary and the loop's standing invariants; read that first, then this for depth. **Add new /orna incidents and deep rationale HERE, not in CLAUDE.md.**

---


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
- **The CALL CHAIN is one `logger.info` per step** (`_advance_inner`, right
  after `action_input` is parsed so it covers `finish` and `ask` too, not just
  tool dispatch): `orna: step 1/35 sid=<sid> action=search_codex input=...`.
  Grouped by sid, so `grep "sid=<x>" telegrambot_error.log` is one request's
  whole trace and `grep "orna: step"` is all of them. Added 2026-09-27 on ask -
  before it, production had ONLY the exception-path `orna:` lines (18 across
  three days), so the ordered trace the harness prints for a request you run
  yourself had no production equivalent. `action_input` is capped at 120 chars
  and each arg value at 80, or a `class_guide` excerpt buries the log.
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
  and after: only the intended one changed. `python3 -m knowledge.orna_knowledge`
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
  path unchanged. Pinned in `python3 -m knowledge.orna_knowledge`.
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

**A MISSPELLED codex name is fuzzy-matched against the real name vocabulary -
the mechanical ladder cannot reach a typo INSIDE a word.** Live 2026-09-26:
"/orna what crest of feeling does?" answered "No such item exists in the current
codex database", having itself listed `Crest of the Felling` - ONE substituted
letter away - among the alternatives it offered. Every retry `_run_codex_search`
had strips things from the EDGES (quality words, possessives, a trailing word, a
stray number, duplicated letters), so `"feeling"` -> `"felling"` was structurally
out of reach, and `_name_candidates` produced only junk for it
(`"crest's of feeling"`, `"crest of"`).
- `orna_aussies.fuzzy_codex_name` matches the whole query against
  `all_codex_names()` - every display name across all nine categories, ~5,064 of
  them, built in 0.02s and searched by `difflib` in under 10ms. Same
  "fuzzy-correct against the corpus's own vocabulary" fix
  `orna_knowledge.search` and `_resolve_stat_field` already use.
- **Cutoff 0.72, measured both ways**: it corrects `crest of feeling`, `balor
  sord`, `judge trifecta falks`, `celestial arcistaff`, `vritra charme`, `lost
  helmut` (6/6) and refuses to invent a name for `what is the best weapon`,
  `how do i level up`, `mag > 250`, `zzzzqqqq` or a bare `sword` (5/5). A query
  under 4 characters is never corrected.
- Placed AFTER every existing fallback in `_run_codex_search`, so it can only
  turn a dead end into a hit, and wired into `_resolve_aussies_entry` too -
  `assess`/`compare`/`build_optimize`/`estimate_stats` all dead-end there and a
  typo is just as likely from them.
- **The observation SAYS it was a correction** ("that looks like a misspelling of
  X ... SAY that is how you read the question"), or the model presents the answer
  as though the user's spelling was right and they never learn the real name.
  Measured after: the reported request answers in **2 steps** with "You likely
  meant Crest of the Felling - Tier 7 Famed accessory, Raid Rewards +50%", where
  before it denied the item existed. Pinned in `_demo`.

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

**`/ban` / `/unban` block an abuser or spammer, and the enforcement is ONE
pre-dispatch guard, not a check per handler.** `drop_banned` is a
`TypeHandler(Update, ...)` registered in **group -1**, i.e. ahead of everything,
which raises `ApplicationHandlerStop` for a banned user. That single check
therefore covers commands, free text, photos, button taps, inline queries AND
the stateful assess/resources conversations - where a per-handler check would
have to be added to each of the ~20 handlers `main()` registers and would be
forgotten by the 21st. Notes:
- **Deliberately SILENT** - no "you are banned" reply. These are spammers and
  abusers; answering both invites an argument and confirms the bot is
  listening. Same reasoning as `/go`'s unauthorised path, which just returns.
- Gated by the same `GO_ALLOWED_USER_IDS` allowlist `/go`/`/stats`/
  `/update_codex` use, and left out of `set_my_commands` like they are.
- `/ban` with NO argument lists who is currently blocked, so there is no third
  command to remember (same shape as a bare `/stats reset` showing what it
  would clear).
- **Two guards stop a mistyped id locking the operators out of their own bot:**
  an admin in `GO_ALLOWED_USER_IDS` cannot be banned, and neither can the
  caller themselves.
- **A bare numeric id is accepted even for someone `usage_stats` has never
  seen.** Free-text messages are not instrumented (see the usage-counters
  note), so a spammer who never sent a slash command has no record here - and
  they are precisely who needs banning. `@username` resolution goes through the
  existing `usage_stats.find_user`, so it only works for someone already known.
- `usage_stats._banned` is persisted in the same store (surviving the frequent
  `launchctl` reloads) and is **NOT cleared by `/stats reset`** - a ban is a
  moderation decision, not a statistic, so wiping the counters must not quietly
  readmit everyone who was blocked. Pinned by the suite's `ban-guard` tier-0
  check, which redirects `_STORE_PATH` to a temp file so a routine suite run
  can never mutate live moderation state.

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


---

**knowledge_search retrieval: Pinecone, one ranked list, keyword fallback
(2026-10-06).** Until this date every corpus had its own word scorer
(`orna_knowledge`, `orna_mechanics`, `orna_echo` + Ornabook, `orna_qa`,
`orna_reddit`), and `_gather_knowledge` glued their results into fixed
blocks under a 40k total cap. Measured on 12 questions, half of them
reworded the way players ask: the answering passage reached the model 8/12
with grep and 11/12 with Pinecone (dense vectors, integrated
`llama-text-embed-v2` embedding, one namespace per corpus). The grep
misses were all paraphrases ("two weapons with exp boost, double?" never
contains "dual wield").

- **Score floors.** A vector search always returns its top-k, so
  `PINECONE_MIN_SCORE` (0.25) drops noise: junk queries ("xyzzy plugh",
  "best pizza recipe") topped out at 0.164, real hits ran 0.25-0.57. Plus a
  RELATIVE floor, best score minus 0.15 (`_VECTOR_RELATIVE`): without it
  every query filled the 40k cap.
- **One list, not blocks.** With fixed blocks the cap dropped whole blocks
  from the END. "Are summons followers" lost its best hit that way: a
  dev's direct answer (reddit, 0.50, the last block) went while 14 sheet
  rows at ~0.30 stayed. Now exact-name lookups (amities, class stats) come
  first, then one list across all corpora, best first, each hit tagged
  (`[dev]`, `[guide]`, `[discord]`, ...) with every tag's trust note shown
  once (`_SOURCE_NOTES`). The budget is 16k, and omissions are counted.
  Median result: 14.4k (was ~36k with grep).
- **Fallback.** With Pinecone off or failing, `orna_textindex` (SQLite
  FTS5/BM25 over the very same chunks) is the whole retrieval: 8/13 on the
  benchmark, the same as the five scorers it replaces.
- **Tried, not adopted: hybrid.** Merging BM25 into Pinecone's ranking by
  reciprocal-rank fusion was meant to catch exact words dense vectors miss.
  It changed nothing measured (8/8 exact-name questions and 12/13 either
  way) and grew results ~1k chars. The case that motivated it, "my pet
  keeps dying", ranks the Followers section 18th in BM25 too, and is the
  benchmark's one known miss.
- **Benchmark.** `_RETRIEVAL_CASES` in `orna_test_suite.py` (tier 0, needs
  Pinecone, no LLM). Pass mark is cases - 2; the median size must stay
  under 20k.
- **Quota.** The starter plan allows 250k embedding tokens a MINUTE. A
  96-record batch of dense table rows came close to that alone, so
  `orna_pinecone.upsert` caps each request at 50k chars and waits out a
  429. `index_corpus` checks the namespace's record count before reporting
  success.

**PLAN primer.** `_call_plan_model` gets the top 5 retrieval hits (~3k
chars) as "game background", so the planner picks tools knowing the
mechanic (e.g. that a formula lives in a guide, not in the codex). It is
labelled as background only: every fact in the answer must still come from
a tool call.

**Discord.** See `orna_discord_search.py`'s docstring. Reading servers you
are only a member of needs a user account; automating one is against
Discord's terms (risk accepted by the user 2026-10-06). A Selenium Chrome
is driven through Discord's own search box, and an XHR hook swaps in the
URL we want, so the token never leaves the browser. A non-search response
leaves the app's search panel stuck, hence the reload after each one.
Measured: keyword search over chat mostly returns people ASKING the
question, and one forum alone held 224k messages. So the harvest takes only
curated posts: FAQ/guide channels (~180 messages) and pins (~100).
- **Images.** The harvest's 53 images go through a vision model.
  `kimi-k3` was exact on a 59-row number table and wrong on 0-1 of 72
  cells of an X-grid. `gemma4:31b` missed the same grid cell every run, and
  deepseek-v4.1-flash got 4-5 rows wrong. Named cells ("B Hydrus: Warrior,
  Thief") instead of positional ones did not fix gemma's miss. Changing the
  model re-transcribes everything (cache key).
- **`discord_search`.** The live tool is refused in code until both
  `knowledge_search` and `web_search` have run (a prompt rule is ~70-90%
  reliable). Discord search is full-text AND, so queries are cut to their
  distinctive words and widened on a miss. Each hit carries the 10 messages
  either side, because the hit is often the question and the answer is in
  the replies. Found conversations are kept (`discord_live` namespace) so
  the next ask is answered by `knowledge_search`.

**Ollama Cloud concurrency.** Ollama Cloud limits concurrent requests per
account; a 4-worker transcription job made every other call 429. Before the
fix, a 429 raised `OllamaUnavailable`, which PARKED the cloud for every user
for 5 minutes. Now `chat_json` holds a process-wide slot
(`OLLAMA_CLOUD_CONCURRENCY`), retries a 429 after 2/5/10s, and then raises
`OllamaBusy`. That falls back to local for the one call and never parks the
cloud.

**community_search: Reddit joins Discord (2026-10-06).** r/OrnaRPG live
search was added the same way as Discord: search, use, keep. It was merged with
`discord_search` into ONE action, `community_search`: same last-resort gate,
same role, and one fewer tool for the model to choose from. Each side runs in
its own browser, in parallel, and either can fail without losing the other's
results; the failure is named in the observation.
- **Access.** Anonymous API-style access 403s, and so does a HEADLESS
  Chrome. A normal Chrome window gets 200 from `/r/OrnaRPG/search.json`
  even logged out, so the search uses one positioned off-screen. A
  Selenium-driven login with Google is refused by Google ("This browser or
  app may not be secure"); logging in once in a normal Chrome on the same
  profile works, but no login turned out to be needed.
- **Search.** Reddit search is keyword AND ("prometheus sigil" -> 0
  posts). A miss is retried OR'ed and ranked by relevance; ranked by top,
  an OR query returns popular threads that merely mention a word.
- **Rendering and keeping.** Threads render exactly like the crawled Q&A
  corpus (`orna_scrape_qa._block`: top 3 answers, DEV flag, no other
  names). A thread already crawled is rendered from `.qa_cache` with no
  request. New ones are kept in `.reddit_cache/live.json` and upserted
  into the `qa` namespace, keyed by post id, so a re-index keeps them.
- **Verified live.** 26s for both sides, 2 new threads kept (qa 849 ->
  851), found again by `knowledge_search`.

**System prompt rewritten in ASD-STE100 style (2026-10-06).** The prompt
was 52.8k chars (~13k tokens) and was sent on every step. Most of the size
came from two things:
- The same principles were restated inside many tool descriptions:
  evidence-only 3x, "a tool posted, finish briefly" 7x, "never guess, ask"
  4x, "the codex card posts itself" 3x.
- Most rules carried their own justification and incident story.

Now each shared principle is in `_GENERAL_RULES` once. Single-task rules
are numbered procedures (BOSS STRATEGY, CLASS BUILD, BONUS TOTALS). All
prompt text is ~80% STE: one instruction per sentence, imperative, one
term per concept (CLAUDE.md, LLM conventions). Result: 36k chars, median
sentence 11 words. Sentences over 25 words went from 131 to 36, mostly
schema lines and name lists.

Two contradictions surfaced and were fixed:
- "Reply in the user's language" vs the English-only loop with
  translation gates. The gates are what runs, so the reply-language rule
  went.
- "Base stats need specialization + AL, class optional" vs estimate_stats,
  which needs the tier-10 class. The rule now follows the tool.

The rewrite also lost one fact: that a codex page is the UNSCALED base, so
a godforged/legendary item needs assess. godforged-orn fell to 1/2 until
that went into assess's own description.

Separately, the word "orna" in a knowledge_search query diluted its
embedding: "ward capacity orna" ranked the formula 8th of 28, and the
budget cut it in 2 of 4 runs. `_retrieve` now drops corpus-wide words
(`_CORPUS_WIDE_WORDS`).

End to end (TIER=1,2,3): old prompt 28/30. New prompt with both fixes:
tier 1 8/8, tiers 2-3 33/33.

**Empty finish, explicit community search, slow cloud (2026-10-06).** One
live request, "use community search to find tips for heretic build on the
Blades of Finesse arena", hit three faults at once:
- **Slow cloud parked as an outage.** The cloud step took more than 45s.
  A `ReadTimeout` was raised as `OllamaUnavailable` with an empty message
  (str() of a ReadTimeout is ""), and the cloud was parked for EVERY user
  for 5 minutes. All 6 parkings in the log for Oct 4-6 were this. A read
  timeout is now `OllamaSlow`: local for that one call, nothing parked.
  Error messages now carry the exception type.
- **Empty finish accepted.** The local model then finished with an EMPTY
  `action_input` (its thought said "I need to synthesize the tips"), and the
  user got "Не вдалося сформувати відповідь". An empty finish is correct
  only after a tool posted something. Otherwise it is sent back once
  (`pushed_for_empty`). `_run_tool` now records whether ANY tool sent a
  message (`anything_posted`); only the listing tools set `posted_note`.
- **Explicit request refused.** community_search refused the request in
  3 of 3 runs, although the user asked for it by name. Its gate now opens
  when the user's OWN turns name community search, Discord or Reddit
  (`_user_asked_for_community`). Tool results and system notes never count:
  the refusal text itself names the tool.

**Opened codex entries carry community knowledge (2026-10-07).**
"які ефекти дає Тигель Освяченого" opened Hallowed Crucible (tier, rarity,
nothing else). The model queried the codex effects table (0 rows) and
answered "gives no effects" at confidence 90 - never calling
knowledge_search, although the Discord cheat sheet of the passives the
crucible rolls was in the index. The general failure: missing codex data was
read as evidence that nothing exists, and "effects" was taken literally.

Fix in code: `open_entry` appends `_entity_knowledge` - up to 2 retrieval
hits that NAME the entity (with their image list), ~0.4s. The model sees
"codex: nothing" and "community: HP +1000-2000, Two-Handed Power 4-7%, ..."
in one result. General rule 10 backs it up: a codex entry shows what a thing
IS; what it gives or does is often only in community knowledge, so never
answer "none" from the codex alone.

Result: 3/3 correct answers (cheat sheet attached 2/3); new tier-2 case
crucible-passives; all 16 end-to-end cases pass.
