---
name: verifying-orna-changes
description: >-
  The no-mocks method for developing, debugging, and verifying ANY change to
  this Orna Telegram bot. Use this WHENEVER you are writing or modifying bot
  code, or a /orna, /go, /need, /res_today, or free-text answer returns
  wrong/invalid/incomplete results, a class_guide or codex/aussies/knowledge
  retrieval looks off, the ReAct loop routes to the wrong tool, OCR/offerings
  parsing misbehaves, or you are about to claim a bot change works. Do not
  hand-wave a fix: this skill gives an ordered, layered debugging procedure
  (ground-truth -> deterministic repro -> real-model end-to-end repro ->
  multi-run verification), a ready-to-run harness that drives the real /orna
  ReAct loop against the live Ollama/codex/sheets with a fake Telegram
  message, and a catalog of this codebase's recurring bug patterns
  (references/common-pitfalls.md) to check new code against. Reach for it even
  when the user just says "the bot returned wrong items", "why did /orna say
  X", "add a tool to /orna", or "test my fix" - reproduce and verify this way
  instead of guessing.
---

# Verifying & debugging changes to the Orna Telegram bot

## The one idea

This bot is thin glue over live, unmocked services: Google Sheets, the
playorna/aussiescodex codex, and Ollama (local + cloud). There is **no test
suite and no mocking layer** (see the repo's `CLAUDE.md`). So the only
faithful way to reproduce a bug or prove a fix is to run the **real code
against the real services** and read what actually comes back - never
reason about what "should" happen from the source alone, and never conclude
from a single model run.

Guessing from the user-facing symptom is the trap. The same wrong answer
can come from three different layers, and the fix is completely different
for each. Your whole job is: **reproduce it, find WHICH layer is wrong, fix
that layer, then prove it over several real runs.**

## Writing or changing code here? Read the pitfalls first.

Before you write a fix or a new feature (a new `/orna` tool, a new parser,
new math), skim **`references/common-pitfalls.md`** - the recurring bug
*shapes* in this codebase, each with a real example and the rule that
prevents it (case-insensitive matching, falsy-value collapse, unvalidated LLM
args, boundary math, event-loop blocking, lossy-query retrieval, cache
staleness, double-delivered callbacks, regex ordering, and fixing the shared
function not the symptom). They recur because this code is glue over fuzzy
inputs; the "obvious" version is wrong at the fuzzy edge. Then check your diff
against that list.

## Follow this procedure in order. Do not skip steps.

### Step 0 - Establish ground truth (what SHOULD the answer be?)

Before touching code, find the correct answer from the source of truth, so
you can recognize right vs wrong output. Where truth lives depends on the
feature:

- **Guides / knowledge** (`orna_guide_*.txt`, `orna_knowledge.txt`): these
  are generated from public Google Sheets by `orna_scrape_guides.py` /
  `orna_scrape_knowledge.py`. Read those scripts' `_SOURCES` / `_TABLES`
  lists for the spreadsheet id + gid, then fetch the tab directly as CSV to
  see the real data:

  ```python
  import csv, io, requests
  r = requests.get("https://docs.google.com/spreadsheets/d/<SHEET_ID>/export",
                   params={"format": "csv", "gid": "<GID>"}, timeout=20)
  r.raise_for_status()
  for row in csv.reader(io.StringIO(r.text)):
      print(" | ".join(c.strip() for c in row if c.strip()))
  ```

- **Codex facts/stats/drops**: the live codex is authoritative
  (`orna_codex.fetch_codex_json` / aussiescodex `codex.json`).
- **Then confirm the generated file matches the sheet.** `grep -n` the
  `orna_guide_*.txt` / `orna_knowledge.txt` for the entity and compare to
  the CSV. If they differ, the bug is in the scraper (Step 2, data layer).
  If they match, the stored data is fine - the bug is downstream.

Write down the expected items/values. You will grade every run against this.

### Step 1 - Reproduce. Deterministic first, then end-to-end.

Reproduce the failure for real. Two flavors, cheapest first:

1. **Deterministic (no LLM) - do this whenever the suspect logic is a pure
   function** (retrieval/ranking, parsing, math, formatting). Import the
   module and call the function directly with the failing input. This is
   instant, repeatable, and pins the bug precisely. Example - probing the
   `class_guide` excerpt picker:

   ```python
   import orna_guides as g
   ex = g.guide_excerpt(g.read_guide("heretic"), "omniflask raid", 6000)
   for kw in ["Omniflask Weakness", "Omniflask Raiding", "--- Raids ---"]:
       print(kw, "->", kw in ex)
   ```

   `orna_guides`, `orna_assess`, `orna_proofs`, `orna_towers`,
   `orna_aussies.query_records` and similar are pure/stdlib enough to probe
   like this without any API keys.

2. **End-to-end against the real models** - for anything that depends on
   the LLM's choices (which tool the ReAct loop picks, what query it passes,
   how it phrases the answer). **Use the bundled harness - do not write your
   own; getting `FakeMessage`/`_advance`/session wiring right is fiddly:**

   ```bash
   set -a && source .env && set +a
   Q="show codex items for raid heretic using omniflask build" \
       python3 .claude/skills/verifying-orna-changes/scripts/orna_loop_harness.py
   ```

   It prints the **ACTION TRACE** (which tool each turn, with args) and every
   **USER-VISIBLE REPLY** (search cards, button labels, final `finish()`
   text) - without a live chat or bot restart. See "Reading the output".

If you cannot reproduce it, you do not understand it yet - gather more data,
do not start editing.

### Step 2 - Localize the layer (this is the crux)

The ACTION TRACE tells you which of three layers is wrong. Match the symptom:

- **Data layer** - the stored file is wrong/missing/stale. Symptom: even the
  deterministic probe or a `grep` of the generated file lacks the right data,
  and it disagrees with the source sheet (Step 0). Fix the scraper
  (`orna_scrape_*.py`) or re-run it; fix the parser (`telegram_offerings`,
  OCR). NOT a model problem.
- **Retrieval layer** - the tool fetched the wrong slice of correct data.
  Symptom: the model routed to the RIGHT tool with a reasonable arg, but the
  tool returned wrong content (wrong section, wrong entry, empty). This is a
  pure function - reproduce it deterministically (Step 1.1) and fix it there
  (e.g. `orna_guides.guide_excerpt`, `orna_aussies._eval_condition`, a codex
  search fallback). Add a runnable self-check next to it.
- **Model layer** - the loop chose the wrong tool, or dropped a key word
  from its arg, or ignored what a tool returned. Symptom: the ACTION TRACE
  shows a bad `action`/`args`, or a good observation followed by a `finish()`
  that contradicts it. Fix the **system prompt / tool description**
  (`telegram_orna.py`'s `_TOOLS_TEXT`, `_CONDITION_RULES`, the `_*_RULE`
  blocks) - make the instruction specific and explain WHY, grounded in the
  real failure.

A single symptom can span two layers (a retrieval bug the prompt can't work
around). Fix each layer you confirmed, and re-verify - a fix to one can
reveal the next.

### Step 3 - Fix the root cause, not the symptom

- Fix in the **shared function all callers route through**, not in the one
  path the report named (this repo's `CLAUDE.md` is full of sibling-caller
  bugs). `grep` the callers before editing.
- When you touch a pure function that is currently embedded in an async /
  I/O handler, **extract it to a stdlib-only module** (e.g. the excerpt
  logic now lives in `orna_guides.guide_excerpt`, not inside
  `telegram_orna._run_class_guide_tool`). That makes it unit-testable
  without API keys and shrinks the handler.
- **Leave one runnable check behind.** Add asserts to the module's
  `_demo()` / `__main__` (the repo convention - see `orna_guides.py`,
  `orna_towers.py`, `orna_assess.py`). The check must fail if the bug
  returns. Assert the RIGHT thing is present AND the wrong thing is absent
  (e.g. Raids gear present, Early-T10 gear NOT present).

### Step 4 - Verify. Self-check, then MANY real runs.

Do not claim success until you have evidence:

1. Run the deterministic self-check: `python3 orna_guides.py` (or the
   relevant module) - must print its "all checks passed" line.
2. Confirm the module still imports/compiles: `python3 -m py_compile
   telegram_orna.py`.
3. **Run the end-to-end harness 3-5 times, because the model is
   non-deterministic.** One green run proves nothing - a fix that works 1/3
   of the time is not fixed.

   ```bash
   Q="<the failing request>" N=5 \
       python3 .claude/skills/verifying-orna-changes/scripts/orna_loop_harness.py
   ```

   Grade every run's items against the Step-0 ground truth. Also test the
   **cloud path** (default) AND at least once with `FORCE_LOCAL=1`, since
   real users hit the cloud model first but fall back to local.
4. Report honestly: "5/5 correct" or "3/5 - here's the failing case". If a
   prompt fix is only ~partly reliable, that is a finding, not a done.

## Reading the harness output

```
-- ACTION TRACE (which tool the model picked each turn) --
  class_guide :: input='None' args={"topic": "heretic", "query": "omniflask raid"}
  search_codex :: input='Scholar's Chargeblade' args={}
  ...
-- USER-VISIBLE REPLIES (tool cards + final finish text) --
  [reply_text] 🔎 <b>Scholar's Chargeblade</b> — 1 результат(и)
       buttons: ["Beheaded Scholar's Chargeblade (★5)"]
  ...
  [reply_text] <final finish() answer>
```

- **ACTION TRACE** = the model's decisions. Wrong tool or a dropped word in
  `args` (e.g. `query:"omniflask"` when the user said "raid") = model layer.
- **REPLIES** = what the user sees. The `search_codex`/`query` cards ARE the
  "items returned" - grade THOSE against ground truth, not just the prose in
  the final `finish()` (the model sometimes summarizes loosely).
- A tool posts its own card and returns a SHORT observation to the model;
  `finish()` is just a closing line. So "wrong items" almost always means a
  wrong `search_codex`/`query`/`open_entry` call in the trace.

## Environment & secrets

- `SHEETS_API_KEY` is **required just to import** the modules (read at import
  by `orna_sheets`). `OLLAMA_API_KEY` enables the cloud model (the real prod
  path); `TAVILY_API_KEY` only matters if the request hits `web_search`.
- Load them from the gitignored `.env`: `set -a && source .env && set +a`.
- **Never hardcode or commit tokens** - pass them via the environment only.
  If a key is missing, `FORCE_LOCAL=1` still exercises the loop on local
  Ollama.

## Gotchas (all live-confirmed in this repo)

- **Model flakiness is expected, not a bug in your change.** Ollama
  occasionally returns empty/non-JSON content mid-reasoning; the loop retries
  once and falls back cloud->local. A stray `OllamaError` traceback in one
  run, with the other runs correct, is that - not your fix. This is exactly
  why you run several times.
- **Local `gpt-oss:20b` is much less reliable at routing than the cloud
  model.** If a local-only run makes a bad tool choice but cloud is
  consistent, weigh accordingly - but still make the fix robust for both.
- **`class_guide` / guide retrieval:** these guides nest `=== Build ===`
  sections under `--- Tab ---` sections; the same build name can recur across
  tabs. A query word can collide as a substring (`"raid"` in `"Raiding"`),
  and the user's spelling may not match the data (`"archstaff"` vs
  `"archistaff"`). Probe `orna_guides.guide_excerpt` deterministically.
- **Don't block the event loop.** Any new tool that does sync I/O or a
  CPU-heavy scan must be wrapped in `asyncio.to_thread` - otherwise it stalls
  every chat, not just the slow one (see `CLAUDE.md`'s `concurrent_updates`
  note).
- **Deploy is separate from fixing.** The live bot runs via launchd from
  this repo; a fix isn't live until the service is reloaded (`launchctl
  unload`/`load` the plist) - and only commit/deploy when the user asks.

## Adapting the harness to /go or a specific tool

The same philosophy covers the hidden `/go` command and individual tools:
call the internal function directly against the real services in a throwaway
script (`telegram_go._download_youtube`, `_run_search`, `_call_model`; or a
single `telegram_orna._run_*_tool` with a `FakeMessage`). The bundled
harness targets the full `/orna` loop because that path is the fiddliest to
wire up by hand; for a single pure tool, a 3-line deterministic probe
(Step 1.1) is faster.
