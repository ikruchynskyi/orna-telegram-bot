# Common pitfalls in this codebase (read before writing or changing code)

These are the bug *patterns* that keep recurring here - every one below was a
real, shipped bug found by reviewing the whole app. They cluster into a
handful of shapes. When you write or change code, check your diff against
this list: the same shapes reappear because the codebase is glue over fuzzy
inputs (LLM tool args, OCR text, transliterated names, scraped HTML, live
caches), and the "obvious" code is wrong at exactly the fuzzy edges.

The meta-rule: **this code runs on messy, adversarial-by-accident input.**
An LLM drops words and emits the wrong JSON type; OCR upper-cases and misreads;
a name is a transliteration, not the game's spelling; a stat is legitimately
0; a process is killed mid-write. Code that only handles the clean case is the
bug.

---

## 1. Case-sensitive fuzzy / string matching

**Why it happens:** `difflib.get_close_matches` and substring/`==` compares
are case-sensitive, and it's easy to compare a lowercase query against a
mixed-case corpus without noticing - it works in the demo (same case) and
fails on real input.

**Real examples:** `orna_knowledge._fuzzy_correct` compared `"sirius"` vs
`"Sirus"` (0.727, below the 0.75 cutoff) and silently found nothing;
`telegram_offerings.resolve_material_name`'s English fallback missed
`"ADARMANTITE"` → `"Adamantite"` (case + typo).

**Rule:** normalize case on **both** sides before any fuzzy/substring compare,
then map the match back to the canonical original-case value. The codebase
already does this in `orna_aussies._resolve_stat_field` - copy that shape,
don't reinvent the case-sensitive version.

## 2. Falsy-value collapse (0 / "" / False / absent are NOT the same)

**Why it happens:** `x or default`, `bool(x)`, and `x is True` all quietly
conflate "absent/empty" with "a legitimate 0 / False / specific value".

**Real examples:**
- `str(raw or "")` turned a real `tier=0` into `""`, so `{tier "=" 0}` never
  matched (`orna_aussies._eval_condition`).
- `raw is True` / `raw is False` (identity) is always False for a concrete
  int like `1`, so a boolean-flag branch that also caught value `0`/`1`
  broke every `{tier "=" 1}` query.
- `is_equippable = bool(place) and not is_adornment` treated `place=="material"`
  as gear, so a crafting material was assessed as upgradable
  (`telegram_orna._aussies_record_to_codex_entry`).

**Rule:** when 0 / "" / False are legitimate values, branch on `is None`
(absent) explicitly, and match a value against an explicit allowed set
(`place in {...gear slots...}`), never "truthy and not the one bad case".

## 3. Unvalidated LLM tool arguments

**Why it happens:** the model is told to emit JSON, but it sometimes emits the
wrong *type* (a bare string where an array is expected), the wrong *case*
(`"OR"`/`"ASC"`), or an *empty* value - and Python does something silently
wrong rather than erroring.

**Real examples:** `compare`/`build_optimize` iterated a bare-string `items`
arg character-by-character; `query_records` compared `combinator`/`sort_dir`
case-sensitively (a stray `"OR"` silently flipped to `and`); an empty
`useable_by` value matched every record (`"" in raw_text` is always True).

**Rule:** treat every tool arg as untrusted. Coerce types at the boundary
(`if isinstance(x, str): x = [x]`), lowercase literal enums before comparing,
and **fail closed** on empty (guard with `bool(value) and ...`, like the
sibling branches already do). See how `query`'s own `conditions` handling
guards `isinstance(..., list)` - match that discipline for new tools.

## 4. Boundary / off-by-one in ported math

**Why it happens:** range checks mix inclusive and exclusive bounds
(`in_range` is `[lo, hi)` here), and a single boundary integer falls through
every branch.

**Real examples:** `get_quality_code(170)` returned Broken(0) - Legendary was
`[140,170)` and Ornate was `>170`, leaving exactly 170 uncovered;
`get_full_result`'s celestial-slot lookup used a 1-based level as a 0-based
index.

**Rule:** when you port or edit range math, test the exact boundary values
(`139/140/170/171`, not just `150`), and leave a `_demo()`/`__main__`
assert-check pinning them (this repo's convention - see `orna_towers.py`,
`orna_proofs.py`, and the `get_quality_code` check now in `orna_assess.py`).

## 5. Blocking / unbounded work on the async event loop

**Why it happens:** a handler coroutine calls something synchronous - a
network fetch, a disk read, a CPU-heavy scan, or model-driven big-int math -
directly, and it stalls **every** chat, not just that request. It never shows
up in single-request manual testing.

**Real examples:** `telegram_go._calculate("9**(10**7)")` ran for seconds of
big-int `pow` on the loop; the documented aussies-fetch and
`orna_knowledge.search` cases needed `asyncio.to_thread`.

**Rule:** bound model-driven compute at the source (cap the exponent - the
root fix, so it can't blow up regardless of threading), AND wrap any genuine
blocking I/O or heavy CPU in `asyncio.to_thread`. Judge by magnitude: a
multi-hundred-ms operation must be offloaded; a sub-millisecond one (a tiny
JSON write) is fine and offloading it can add a worse bug (a shared temp-file
write race) - don't cargo-cult `to_thread` onto everything.

## 6. Retrieval that hides alternatives or drops disambiguating context

**Why it happens:** the LLM's query into a retrieval tool is lossy (it drops a
word, transliterates, guesses a name). If retrieval returns a single "best"
hit and strips the surrounding context, the model can't recover from a near-miss.

**Real example:** the class-guide excerpt returned one build section and
stripped its `--- Tab ---` header, so `"omniflask raid"` landed on the wrong
same-named build in another tab (the bug this whole skill started from).

**Rule:** make retrieval robust to a lossy query - surface enough to
disambiguate (all top hits, each with its section/parent context), and make
the model's job easier via the tool description (tell it to keep the
distinguishing words). Assume the query is imperfect; don't assume one hit is
the answer.

## 7. Caches without a staleness bound, refresh path, or crash-safe write

**Why it happens:** an `@lru_cache` or on-disk cache is added for speed, but
with no TTL, no way to force a refresh, and a plain `write_text` that a
mid-write crash can truncate.

**Real examples:** `orna_codex`'s `lru_cache`d fetchers had no TTL and their
`clear_cache()` had no caller (stale stats after a game patch until restart);
`orna_aussies._fetch_json` read the cache with no guard, so a truncated file
raised `JSONDecodeError` on every call until the week-long TTL expired.

**Rule:** every cached data source needs (a) a staleness bound or an
operational refresh hook (wire it into `/update_codex`), (b) a read that
treats a corrupt/unreadable cache as a miss and re-fetches, and (c) an atomic
write (temp file then `replace`), never a bare `write_text`.

## 8. Double-delivered callbacks and unlocked shared session state

**Why it happens:** Telegram can redeliver a callback, or a user double-taps,
and `concurrent_updates` means two coroutines can touch the same mutable
session with no lock.

**Real example:** the `/orna` "ask" callback resumed the shared `OrnaSession`
with no idempotency guard - a duplicate delivery double-appended messages,
double-spent steps, and posted duplicate replies.

**Rule:** make a callback that mutates shared state idempotent - consume the
pending action synchronously (no `await` between the check and the consume, so
it's atomic on the single-threaded loop) so a repeat delivery no-ops. See the
reminder-button `"scheduled"` set and the fixed ask-callback for the pattern.

## 9. Regex transform ordering and greedy spans

**Why it happens:** a chain of `re.sub` passes runs in an order where an
earlier pass consumes markers a later pass needed, or a greedy/`re.S` pattern
spans past where it should.

**Real examples (all `telegram_go._markdown_to_html`):** the italic pass ran
before the bullet pass and ate `*` bullet markers; the `_` italic pattern
paired underscores across identifiers/filenames (`orna_guides.py`); a heading
containing `**bold**` got double-wrapped.

**Rule:** order transforms so earlier ones don't consume later ones' markers
(bullets before italics), anchor emphasis to word boundaries so intraword
`_`/`*` don't match, and prefer `[^\n]` over `.`+`re.S` so a span can't leak
across lines. Extend the converter's `_demo()` with the new shape you're
handling.

## 10. Fixing the symptom, not the shared function

**Why it happens:** a bug report names one path, so the fix goes there - while
sibling call sites with the same flaw stay broken.

**Real examples:** the `is_equippable` flaw existed in both
`_aussies_record_to_codex_entry` and `orna_codex.parse_codex_html`; the
greedy-`{.*}` JSON-recovery bug existed in both `telegram_go` and
`telegram_nlp` (per CLAUDE.md).

**Rule:** before editing, `grep` every caller / every copy of the logic. Fix
it once where all callers route through, or if the logic is genuinely
duplicated, fix (or better, de-duplicate) every copy in the same change.

---

## The habit that catches all ten

After writing a change, **reproduce the fuzzy edge, don't just run the happy
path** - the deterministic probe and the multi-run real-model harness in this
skill's SKILL.md exist for exactly this. Ask: what does this do when the input
is `0`, `""`, upper-cased, a bare string, a transliteration, a duplicate
delivery, or a killed-mid-write file? If you didn't test that, you haven't
tested the part that breaks here.
