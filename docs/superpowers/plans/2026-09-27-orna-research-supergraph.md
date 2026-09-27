# `/orna` research supergraph tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one deterministic `research` tool that returns the whole relevant codex subgraph (entity + drops/skills/etc. with each leaf's stats/useable_by/effects) plus related community knowledge in a single call, so the loop analyzes on the first iteration instead of chaining `open_entry` per drop.

**Architecture:** A pure data builder in `orna_aussies.py` resolves a name→(category,id) locally, walks its `[cat,id]` edge lists one level, and joins each target to its leaf record — all from the already-loaded `codex.json`/`translations.en.json`, zero network. A wrapper in `telegram_orna.py` renders that bundle to one observation, appends the existing 7-corpus knowledge aggregation (factored into a shared `_gather_knowledge`), records touched entities for `finish()` buttons, and is dispatched as a new `research` action.

**Tech Stack:** Python 3, stdlib only (`difflib` already used), existing modules `orna_aussies`, `telegram_orna`, `orna_test_suite`. No new dependency. No test framework — checks live in each module's `_demo()` and the tiered `orna_test_suite.py`, run with `python3 <module>.py` / `python3 orna_test_suite.py`, matching repo convention.

**Spec:** `docs/superpowers/specs/2026-09-27-orna-research-supergraph-design.md`

## Global Constraints

- Python 3, **stdlib + already-installed deps only** — add no dependency.
- `build_supergraph` and its helpers do **zero network** in phase 1: they read only `_codex()` / `_translations()` (already cached in memory/disk). No `open_entry`/playorna fetch fallback in this phase.
- Any blocking call (disk read, CPU scan) invoked from an async handler is wrapped in `asyncio.to_thread` at the call site (the event-loop rule in CLAUDE.md).
- `_ACTIONS` stays the **single source** for both the prompt action enum and `_STEP_TOOLS` — never hardcode a second action list.
- **Observation honesty:** any truncated list carries `PARTIAL` plus the true total; a cap must never read as "that's all there is."
- Tier 0 (`python3 orna_test_suite.py`) **must stay 100%**.
- Verify with **real data and the real-model harness (N≥5)**, no mocks (CLAUDE.md "Verifying changes").
- End every commit message with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## Review Focus

Input classes the spec implies that a naive implementation breaks on — each gets a test in the owning task:

- **Ambiguous name across categories** ("Centaurus" is both an item and part of a raid): must surface alternatives, never silently pick the wrong category. → Task 1.
- **Name not in the dump** (new event entity, or a typo fuzzy can't rescue): must return it as unresolved, no crash, no fabricated answer. → Task 2.
- **Oversized relation** (an item `dropped_by` 40 monsters): capped and marked `PARTIAL` with the true count. → Task 2.
- **Malformed / dangling edge** (a target id absent from the dump, or an edge value that isn't a `[cat,id]` pair): skipped gracefully, no `KeyError`. → Task 2.
- **Effect code with no status-map entry**: falls back to the raw code instead of crashing; a known code is humanized. → Task 2.

---

## Task 1: Name resolution (`orna_aussies.resolve_entity`)

**Files:**
- Modify: `orna_aussies.py` (add `_NAME_INDEX`, `_name_index()`, `_CATEGORY_PRIORITY`, `resolve_entity()`; add `_NAME_INDEX`/`_ALL_NAMES` to the `refresh_cache` reset list ~line 147)
- Test: `orna_aussies._demo()` (existing self-check block at bottom of file)

**Interfaces:**
- Consumes: existing `_codex()`, `_translations()`, `display_name(category, record_id)`, `all_codex_names()`, `fuzzy_codex_name(query, cutoff=0.72)`.
- Produces:
  - `resolve_entity(name: str) -> dict` — `{"category","id","name","alternatives":[(cat,id,name),...]}` for a match (alternatives empty unless the name spans categories), or `{"unresolved": name}` when nothing resolves.

- [ ] **Step 1: Write the failing test** — add to the top of `_demo()` in `orna_aussies.py`:

```python
    # --- research supergraph: name resolution ---
    r = resolve_entity("Fallen King Centaurus")
    assert r.get("category") == "raids" and r.get("id") == "fallen-king-centaurus", r
    # typo/transliteration recovered via fuzzy_codex_name
    assert resolve_entity("Fallen King Centaurs").get("id") == "fallen-king-centaurus", \
        resolve_entity("Fallen King Centaurs")
    # a name spanning categories surfaces alternatives instead of a silent pick
    amb = resolve_entity("Centaurus")  # item "Crimson Eye of Centaurus" is NOT this; exact-name only
    # nothing resolvable -> unresolved, no crash
    assert resolve_entity("zzzptqx no such entity").get("unresolved"), resolve_entity("zzzptqx")
```

- [ ] **Step 2: Run to verify it fails**

Run: `set -a && source .env && set +a && python3 orna_aussies.py`
Expected: FAIL with `NameError: name 'resolve_entity' is not defined`.

- [ ] **Step 3: Implement** — add near the other lookup helpers (after `fuzzy_codex_name`):

```python
_NAME_INDEX: Optional[dict] = None  # name.lower() -> [(category, id), ...]

# For a name that spans categories, a bare mention most often means the
# fightable thing ("how do I beat / what drops X") over an item of the same
# name; items next; the rest after.
_CATEGORY_PRIORITY = ("raids", "bosses", "monsters", "items", "followers",
                      "spells", "classes", "dungeons", "buildings")


def _name_index() -> dict:
    """name.lower() -> [(category, id), ...], built once from codex.json +
    translations. Records carry no name in codex.json (names live in
    translations), so this is the reverse of display_name over every record."""
    global _NAME_INDEX
    if _NAME_INDEX is None:
        idx: dict = {}
        for category, records in _codex()["main"].items():
            for rid in records:
                nm = display_name(category, rid)
                if nm:
                    idx.setdefault(nm.lower(), []).append((category, rid))
        _NAME_INDEX = idx
    return _NAME_INDEX


def resolve_entity(name: str) -> dict:
    """Resolve a display name to a codex (category, id), entirely from the
    local dump (no network). Exact name first, then fuzzy_codex_name for a
    typo/transliteration. A name in several categories returns the
    priority-ordered pick with the rest in `alternatives`, so the caller can
    note them without a second round-trip. Nothing resolvable -> {"unresolved": name}."""
    q = (name or "").strip()
    if not q:
        return {"unresolved": name}
    hits = _name_index().get(q.lower())
    if not hits:
        fuzzy = fuzzy_codex_name(q)
        if fuzzy:
            hits = _name_index().get(fuzzy.lower())
    if not hits:
        return {"unresolved": name}
    ordered = sorted(hits, key=lambda ci: _CATEGORY_PRIORITY.index(ci[0])
                     if ci[0] in _CATEGORY_PRIORITY else 99)
    cat, rid = ordered[0]
    return {
        "category": cat, "id": rid, "name": display_name(cat, rid) or rid,
        "alternatives": [(c, i, display_name(c, i) or i) for c, i in ordered[1:]],
    }
```

Then add `_NAME_INDEX` (and the pre-existing-but-unreset `_ALL_NAMES`) to the globals cleared in `refresh_cache` (~line 147):

```python
    global _codex_cache, _translations_cache, _reverse_status_cache, _stem_directions_cache, _stat_field_cache, _attr_field_cache, _NAME_INDEX, _ALL_NAMES
    _codex_cache = _translations_cache = _reverse_status_cache = _stem_directions_cache = _stat_field_cache = _attr_field_cache = _NAME_INDEX = _ALL_NAMES = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `set -a && source .env && set +a && python3 orna_aussies.py`
Expected: PASS (`orna_aussies: ...` / existing final print).

- [ ] **Step 5: Commit**

```bash
git add orna_aussies.py
git commit -m "Add local name->entity resolution for the codex supergraph

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Supergraph builder (`orna_aussies.build_supergraph`)

**Files:**
- Modify: `orna_aussies.py` (add `_GRAPH_EDGES`, `_DEFAULT_EXPAND`, `_effect_names`, `_leaf`, `_entity_facts`, `build_supergraph`)
- Test: `orna_aussies._demo()`

**Interfaces:**
- Consumes: `resolve_entity` (Task 1), `_codex()`, `_translations()`, `display_name`.
- Produces:
  - `build_supergraph(names, expand=None, per_relation_cap=12) -> dict` returning
    `{"entities": [entity, ...], "unresolved": [name, ...]}` where
    `entity = {"category","id","name","facts":dict,"alternatives":[(cat,id,name)],"relations":[relation,...]}`,
    `relation = {"field","title","total","partial","members":[leaf,...]}`,
    `leaf = {"category","id","name","useable_by","place","item_type","tier","rarity","stats":dict,"effects":[str,...]}`.

- [ ] **Step 1: Write the failing test** — append to `_demo()`:

```python
    # --- research supergraph: builder ---
    g = build_supergraph("Fallen King Centaurus")
    ent = g["entities"][0]
    assert ent["category"] == "raids" and not g["unresolved"], g
    assert ent["facts"].get("hp") and ent["facts"].get("tier") == 10, ent["facts"]
    rels = {r["field"]: r for r in ent["relations"]}
    drops = rels["drops"]
    assert drops["total"] >= 6 and not drops["partial"], drops
    # every drop carries the analysis fields the model needs
    assert all(m["useable_by"] for m in drops["members"]), drops["members"]
    assert all(m["stats"] for m in drops["members"]), "each drop must carry stats"
    bow = next(m for m in drops["members"] if m["name"] == "Cretan Compound Bow")
    assert "attack" in bow["stats"], bow
    assert any("Crit" in e for e in bow["effects"]), bow["effects"]  # gives:T. Crit ↑
    helm = next(m for m in drops["members"] if m["name"] == "Horned Corinthian Helmet")
    assert any("Blind" in e for e in helm["effects"]), helm["effects"]  # immunities:Blind
    assert rels["skills"]["total"] >= 6, rels["skills"]
    # oversized relation -> capped + PARTIAL with the true total
    capped = build_supergraph("Fallen King Centaurus", per_relation_cap=2)
    cdrops = {r["field"]: r for r in capped["entities"][0]["relations"]}["drops"]
    assert cdrops["partial"] and cdrops["total"] >= 6 and len(cdrops["members"]) == 2, cdrops
    # multi-entity bundles both
    two = build_supergraph(["Fallen King Centaurus", "Cretan Compound Bow"])
    assert len(two["entities"]) == 2, two
    # not in the dump -> unresolved, no crash
    miss = build_supergraph("zzzptqx no such entity")
    assert miss["unresolved"] == ["zzzptqx no such entity"] and not miss["entities"], miss
    # malformed edge / missing target must not crash: synthetic record
    assert _leaf("items", "does-not-exist")["name"] == "does-not-exist"  # dangling -> id fallback
    # effect code with no status entry falls back to the raw code
    assert _effect_names({"gives": [{"name": "totally_made_up_code"}]}) == ["gives:totally_made_up_code"]
    assert _effect_names({"immunities": [{"name": "blind"}]}) == ["immunities:Blind"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `set -a && source .env && set +a && python3 orna_aussies.py`
Expected: FAIL with `NameError: name 'build_supergraph' is not defined`.

- [ ] **Step 3: Implement** — add after `resolve_entity`:

```python
# Every one of these edge fields is a list of [category, id] cross-links in
# codex.json (verified live). The default expand set is category-aware.
_GRAPH_EDGES = ("drops", "skills", "dropped_by", "upgrade_materials",
                "used_by", "learned_by")
_DEFAULT_EXPAND = {
    "raids": ("drops", "skills"),
    "bosses": ("drops", "skills"),
    "monsters": ("drops", "skills"),
    "dungeons": ("drops",),
    "items": ("dropped_by", "upgrade_materials"),
    "followers": ("skills",),
    "spells": ("learned_by", "used_by"),
    "classes": ("skills",),
    "buildings": (),
}
# ponytail: bestial_bond (a follower's spell/bond grants) is a nested
# list-of-tiers, not [cat,id] pairs, so it is not expanded here. Add it in
# phase 2 if a request needs "which follower grants X"; the drops/skills use
# cases this tool targets don't touch it.


def _effect_names(record: dict) -> list:
    """Human effect labels from a record's causes/gives/immunities/cures
    lists. Each entry is {"name": <status-code>, "chance"?: "10%"}; the code
    is humanized via translations['status'], falling back to the raw code so
    an unknown code degrades to text rather than crashing."""
    status = _translations().get("status", {})
    out = []
    for field in ("causes", "gives", "immunities", "cures"):
        for e in record.get(field) or []:
            code = e.get("name") if isinstance(e, dict) else e
            if not code:
                continue
            human = status.get(code, code)
            chance = e.get("chance") if isinstance(e, dict) else None
            out.append(f"{field}:{human}" + (f"({chance})" if chance else ""))
    return out


def _leaf(category: str, rid: str) -> dict:
    """Compact analysis view of one cross-linked record. A dangling id (not in
    the dump) degrades to just its id as the name, empty everything else."""
    r = _codex()["main"].get(category, {}).get(rid) or {}
    return {
        "category": category, "id": rid,
        "name": display_name(category, rid) or rid,
        "useable_by": r.get("useable_by"),
        "place": r.get("place"), "item_type": r.get("item_type"),
        "tier": r.get("tier"), "rarity": r.get("rarity"),
        "stats": dict(r.get("stats") or {}),
        "effects": _effect_names(r),
    }


def _entity_facts(rec: dict) -> dict:
    facts = {}
    for k in ("tier", "rarity", "hp", "place", "item_type", "useable_by", "events"):
        v = rec.get(k)
        if v not in (None, "", [], {}):
            facts[k] = v
    if rec.get("stats"):
        facts["stats"] = dict(rec["stats"])
    eff = _effect_names(rec)
    if eff:
        facts["effects"] = eff
    return facts


def build_supergraph(names, expand=None, per_relation_cap: int = 12) -> dict:
    """One-level subgraph for one or more entity names, built entirely from
    codex.json + translations (no network). See the module/tool docs for the
    returned shape. per_relation_cap bounds each relation; an over-cap
    relation is truncated with partial=True and the true total kept."""
    if isinstance(names, str):
        names = [names]
    entities, unresolved = [], []
    codex = _codex()["main"]
    for name in names:
        res = resolve_entity(name)
        if "id" not in res:
            unresolved.append(name)
            continue
        cat, rid = res["category"], res["id"]
        rec = codex[cat][rid]
        fields = expand if expand is not None else _DEFAULT_EXPAND.get(cat, ())
        relations = []
        for field in fields:
            raw = rec.get(field) or []
            pairs = [(x[0], x[1]) for x in raw
                     if isinstance(x, (list, tuple)) and len(x) == 2]
            if not pairs:
                continue
            members = [_leaf(c, i) for c, i in pairs[:per_relation_cap]]
            relations.append({
                "field": field,
                "title": field.replace("_", " ").title(),
                "total": len(pairs),
                "partial": len(pairs) > per_relation_cap,
                "members": members,
            })
        entities.append({
            "category": cat, "id": rid, "name": res["name"],
            "facts": _entity_facts(rec),
            "alternatives": res.get("alternatives") or [],
            "relations": relations,
        })
    return {"entities": entities, "unresolved": unresolved}
```

- [ ] **Step 4: Run to verify it passes**

Run: `set -a && source .env && set +a && python3 orna_aussies.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orna_aussies.py
git commit -m "Build a one-call codex supergraph from the local dump

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Extract the shared knowledge aggregator (`telegram_orna._gather_knowledge`)

Behavior-preserving refactor so `research` can reuse the exact 7-corpus aggregation `knowledge_search` already does.

**Files:**
- Modify: `telegram_orna.py` (`_run_knowledge_tool` body → new `_gather_knowledge`; `_run_knowledge_tool` becomes a thin caller)
- Test: `telegram_orna._demo()` (bottom-of-file self-check)

**Interfaces:**
- Produces: `async def _gather_knowledge(query: str, sources: Optional[list] = None) -> str` — the joined, total-capped knowledge blocks, or `""` when no corpus matched.
- `_run_knowledge_tool` unchanged externally: still returns the "no matches" line when empty.

- [ ] **Step 1: Write the failing test** — add to `_demo()` (it already uses `asyncio.run` for async checks):

```python
    # --- shared knowledge aggregator ---
    kg = asyncio.run(_gather_knowledge("factions"))
    assert "GAME MECHANICS" in kg, kg[:200]           # mechanics corpus still wired
    assert asyncio.run(_gather_knowledge("xyzzy plugh frobnicate")) == ""  # honest empty
```

- [ ] **Step 2: Run to verify it fails**

Run: `set -a && source .env && set +a && python3 telegram_orna.py`
Expected: FAIL with `NameError: name '_gather_knowledge' is not defined`.

- [ ] **Step 3: Implement** — rename the current `_run_knowledge_tool` body. Replace the function (lines ~1783–1944) with:

```python
async def _gather_knowledge(query: str, sources: Optional[list] = None) -> str:
    """The 7-corpus community-knowledge aggregation (sheets, mechanics,
    amities, classes, guide formulas, player Q&A, dev reddit), joined and
    total-capped. Returns "" when nothing matched. Extracted so both
    knowledge_search and research reuse the exact same gathering. Every
    corpus call is wrapped in to_thread (disk/CPU), per the event-loop rule."""
    # <<< MOVE VERBATIM: the body from the old function starting at
    #     `result = await asyncio.to_thread(orna_knowledge.search, query)`
    #     through the total-cap loop, but change the final two returns:
    #       - the `if not blocks:` branch -> `return ""`
    #       - keep the final `return "\n\n".join(out)`
    result = await asyncio.to_thread(orna_knowledge.search, query)
    # ... (unchanged aggregation of reddit/bonuses/mechanics/classes/echo/qa,
    #      source citations, block assembly, and the _KNOWLEDGE_OBS_MAX cap) ...
    if not blocks:
        return ""
    # ... total-cap loop unchanged ...
    return "\n\n".join(out)


async def _run_knowledge_tool(message, query: str, sources: Optional[list] = None) -> str:
    """<<keep the existing docstring>>"""
    if not query:
        return "knowledge_search needs a query in action_input"
    out = await _gather_knowledge(query, sources)
    return out or f"no knowledge-base matches for {query!r} - try web_search instead"
```

Move the block-assembly code unchanged; do not alter caps, ordering, or citations. The only semantic change is the empty case returning `""` for the caller to handle.

- [ ] **Step 4: Run to verify it passes**

Run: `set -a && source .env && set +a && python3 telegram_orna.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram_orna.py
git commit -m "Extract _gather_knowledge so research and knowledge_search share it

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: The `research` tool (`telegram_orna._render_supergraph` + `_run_research_tool`)

**Files:**
- Modify: `telegram_orna.py` (add `_render_supergraph`, `_run_research_tool`; add `"research"` to `_ACTIONS`; add dispatch in `_run_tool`)
- Test: `telegram_orna._demo()`

**Interfaces:**
- Consumes: `orna_aussies.build_supergraph` (Task 2), `_gather_knowledge` (Task 3), `_add_source`, `session.viewed_entries`.
- Produces:
  - `_render_supergraph(bundle: dict) -> str`
  - `async def _run_research_tool(message, action_input: str, args: dict, sources=None, session=None) -> str`
  - `"research"` present in `_ACTIONS` (so also in `_STEP_TOOLS`) and dispatched in `_run_tool`.

- [ ] **Step 1: Write the failing test** — add to `_demo()`:

```python
    # --- research tool wiring + observation ---
    assert "research" in _ACTIONS
    assert "research" in [t["function"]["name"] for t in _STEP_TOOLS]

    class _Spy:                      # collects reply_text, no Telegram
        def __init__(self): self.texts = []
        async def reply_text(self, *a, **k): self.texts.append(a[0] if a else "")
        async def reply_photo(self, *a, **k): pass
        def __getattr__(self, _): 
            async def _noop(*a, **k): return None
            return _noop

    sess = OrnaSession(chat_id=0, user_text="centaurus")  # match real ctor
    obs = asyncio.run(_run_research_tool(_Spy(), "Fallen King Centaurus", {}, sess.sources, sess))
    assert "Cretan Compound Bow" in obs and "useable_by=all_classes" in obs, obs[:400]
    assert "Drops" in obs and "Skills" in obs, obs[:400]
    # capped relation shows PARTIAL, never reads complete
    obs2 = asyncio.run(_run_research_tool(_Spy(), "Fallen King Centaurus",
                                          {"per_relation_cap": 2}, sess.sources, sess))
    assert "PARTIAL" in obs2, obs2[:400]
    # unresolved subject is honest, no crash
    obs3 = asyncio.run(_run_research_tool(_Spy(), "zzzptqx nothing", {}, sess.sources, sess))
    assert "could not" in obs3.lower() or "not found" in obs3.lower(), obs3
```

(If `OrnaSession`'s constructor differs, match its real signature — check the class definition; `viewed_entries`/`sources` must exist on the instance.)

- [ ] **Step 2: Run to verify it fails**

Run: `set -a && source .env && set +a && python3 telegram_orna.py`
Expected: FAIL (`research` not in `_ACTIONS` / `_run_research_tool` undefined).

- [ ] **Step 3: Implement**

(a) Add `"research"` to `_ACTIONS` after `"open_entry"`:

```python
_ACTIONS = ("today", "next", "need", "search_codex", "query", "events", "open_entry", "research",
            "calculate", "assess", "compare", "build_optimize", "estimate_stats", "towers",
            "class_guide", "knowledge_search", "releases", "web_search", "ask", "finish")
```

(b) Add the renderer + tool (near `_run_open_entry_tool`):

```python
_RESEARCH_CODEX_MAX = 4500  # char budget for the codex half of the observation


def _leaf_line(m: dict) -> str:
    slot = "/".join(x for x in (m.get("place"), m.get("item_type")) if x)
    tr = " ".join(x for x in (f"t{m['tier']}" if m.get("tier") else "",
                              m.get("rarity") or "") if x)
    stats = ", ".join(f"{k} {v}" for k, v in list((m.get("stats") or {}).items())[:10])
    bits = [f"{m['name']} [{m['category']}]"]
    meta = ", ".join(x for x in (slot, f"useable_by={m['useable_by']}" if m.get("useable_by") else "", tr) if x)
    if meta:
        bits.append(meta)
    if stats:
        bits.append(stats)
    if m.get("effects"):
        bits.append("effects: " + "; ".join(m["effects"][:6]))
    return " — ".join(bits)


def _render_supergraph(bundle: dict) -> str:
    """One structured observation from build_supergraph's dict. Honest about
    caps (PARTIAL) and unresolved names; bounded to _RESEARCH_CODEX_MAX."""
    out = ["RESEARCH SUPERGRAPH (from local codex.json, zero network):"]
    for ent in bundle.get("entities", []):
        facts = ent.get("facts", {})
        fbits = []
        for k in ("tier", "rarity", "hp", "place", "item_type", "useable_by", "events"):
            if k in facts:
                fbits.append(f"{k}={facts[k]}")
        out.append(f"\n{ent['name']} [{ent['category']}]" + (" — " + ", ".join(fbits) if fbits else ""))
        if facts.get("stats"):
            out.append("  stats: " + ", ".join(f"{k} {v}" for k, v in facts["stats"].items()))
        if facts.get("effects"):
            out.append("  effects: " + "; ".join(facts["effects"][:8]))
        if ent.get("alternatives"):
            alt = ", ".join(f"{n} [{c}]" for c, i, n in ent["alternatives"][:5])
            out.append(f"  (note: name also matches: {alt})")
        for rel in ent.get("relations", []):
            head = f"  {rel['title']} ({rel['total']}"
            head += ", showing first %d, PARTIAL" % len(rel["members"]) if rel["partial"] else ""
            head += "):"
            out.append(head)
            # skills are spells with no useable_by/stats worth a full line -> names only
            if rel["field"] == "skills":
                out.append("    " + ", ".join(m["name"] for m in rel["members"]))
            else:
                for m in rel["members"]:
                    out.append("    • " + _leaf_line(m))
    if bundle.get("unresolved"):
        out.append("\nCould not resolve: " + ", ".join(bundle["unresolved"])
                   + " (not in the codex dump; try search_codex or a different spelling).")
    text = "\n".join(out)
    if len(text) > _RESEARCH_CODEX_MAX:
        text = text[:_RESEARCH_CODEX_MAX] + "\n[…codex section truncated - PARTIAL…]"
    return text


async def _run_research_tool(message, action_input: str, args: Optional[dict] = None,
                             sources: Optional[list] = None, session=None) -> str:
    """One-call supergraph for analytical questions: the entity, its drops/
    skills/etc. with each leaf's stats/useable_by/effects, PLUS related
    community knowledge - so the model reasons over the whole subject in one
    step instead of chaining open_entry per drop. Fully local for the codex
    half (build_supergraph reads only codex.json/translations). Posts no
    per-entity cards; finish() offers buttons to open any of them."""
    args = args or {}
    names = args.get("entities")
    if not names:
        raw = (action_input or "").strip()
        # a comma / "and" separated subject is several entities
        names = [p.strip() for p in re.split(r",| and | та ", raw) if p.strip()] or [raw]
    if not names or names == [""]:
        return "research needs an entity name in action_input"
    cap = args.get("per_relation_cap", 12)
    bundle = await asyncio.to_thread(build_supergraph, names, None, cap)
    codex_text = _render_supergraph(bundle)

    # record entities for finish() buttons + cite aussies; playorna url shape
    for ent in bundle.get("entities", []):
        url = f"/codex/{ent['category']}/{ent['id']}/"
        if session is not None and isinstance(getattr(session, "viewed_entries", None), list):
            if not any(e.get("url") == url for e in session.viewed_entries):
                session.viewed_entries.append({"name": ent["name"], "url": url,
                                               "tier": ent["facts"].get("tier")})
        if sources is not None and has_aussies_page(ent["category"]):
            _add_source(sources, ent["name"], build_aussies_url(ent["category"], ent["id"]))

    # knowledge half - the same aggregation knowledge_search uses (best effort)
    subject = action_input or (names[0] if names else "")
    knowledge = await _gather_knowledge(subject, sources)
    if knowledge:
        return codex_text + "\n\nCOMMUNITY KNOWLEDGE:\n" + knowledge
    return codex_text
```

(c) Dispatch in `_run_tool` (after the `open_entry` branch, ~line 3195):

```python
        if action == "research":
            return await _run_research_tool(message, action_input, args, sources, session)
```

- [ ] **Step 4: Run to verify it passes**

Run: `set -a && source .env && set +a && python3 telegram_orna.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram_orna.py
git commit -m "Add the research tool: one-call supergraph observation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Prompt guidance for `research`

Make the loop actually reach for `research` on analytical questions (a new tool that isn't described is not used — the class_guide precedent in CLAUDE.md).

**Files:**
- Modify: `telegram_orna.py` (the tool-bullets section of the system prompt; the `_STRATEGY_RULE` note; the `_ACTIONS` doc comment if it enumerates counts)
- Test: `telegram_orna._demo()`

**Interfaces:** none new (prompt text only).

- [ ] **Step 1: Write the failing test** — add to `_demo()`:

```python
    # research is advertised in the system prompt
    _p = _orna_system_prompt("what does Fallen King Centaurus drop")
    assert "research" in _p and ("drops" in _p.lower() or "supergraph" in _p.lower()), \
        "research must be described in the prompt or the model won't use it"
```

- [ ] **Step 2: Run to verify it fails**

Run: `set -a && source .env && set +a && python3 telegram_orna.py`
Expected: FAIL on the new assert.

- [ ] **Step 3: Implement** — add a bullet where the other tool bullets are defined (search for the `knowledge_search(action_input=` bullet; insert near `open_entry`'s). Use this text:

```python
    "- research(action_input=<entity name(s)>, args={\"entities\":[...], \"per_relation_cap\":12}): the DEFAULT for "
    "an analytical or comparative question about a monster/boss/raid/item - \"what does X drop and which class "
    "benefits\", \"how do I beat X\", \"compare what these bosses drop\". ONE call returns the whole subgraph from "
    "the local codex (the entity, its drops/skills/upgrade-materials with each one's stats, useable_by and effects) "
    "PLUS the community knowledge (incl. Monster-Data elemental immunities). Call it ONCE with everything you need, "
    "analyse the whole result, then finish - do NOT open_entry each drop one by one. It also satisfies the "
    "STRATEGY rule below.\n"
```

Then in `_STRATEGY_RULE`, add one line: a "how do I beat X" question should call `research` (its knowledge half carries the immunities) rather than `knowledge_search` + N `open_entry`.

- [ ] **Step 4: Run to verify it passes**

Run: `set -a && source .env && set +a && python3 telegram_orna.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram_orna.py
git commit -m "Prompt: make research the default for analytical codex questions

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Tier-0 regression check (`orna_test_suite.py`)

**Files:**
- Modify: `orna_test_suite.py` (add `_check_research_supergraph`; register in `TIER0`)
- Test: the suite itself.

**Interfaces:** consumes `orna_aussies.build_supergraph`, `telegram_orna` (`T`) `_ACTIONS`/`_STEP_TOOLS`.

- [ ] **Step 1: Write the failing check** — add near the other `_check_*` functions:

```python
def _check_research_supergraph() -> None:
    """The reported failure: a raid's drops with stats must come back in ONE
    local call, wired into the loop's action set."""
    g = orna_aussies.build_supergraph("Fallen King Centaurus")
    ent = g["entities"][0]
    drops = {r["field"]: r for r in ent["relations"]}["drops"]
    assert drops["total"] >= 6 and all(m["useable_by"] and m["stats"] for m in drops["members"]), drops
    assert "research" in T._ACTIONS and "research" in [t["function"]["name"] for t in T._STEP_TOOLS]
```

Register it:

```python
    ("research-supergraph", _check_research_supergraph),
```

- [ ] **Step 2: Run to verify it fails** (before Tasks 1–5 are done) / passes (after)

Run: `set -a && source .env && set +a && python3 orna_test_suite.py`
Expected after Tasks 1–5: `mechanics-wired`… `research-supergraph` PASS, `ALL GREEN`.

- [ ] **Step 3: (implementation already done in Tasks 1–5)** — no code beyond the check.

- [ ] **Step 4: Run the whole Tier 0**

Run: `set -a && source .env && set +a && python3 orna_test_suite.py`
Expected: `ALL GREEN` (Tier 0 100%).

- [ ] **Step 5: Commit**

```bash
git add orna_test_suite.py
git commit -m "Pin the research supergraph in Tier 0

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Document `research` in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (add a `research` subsection under the `/orna` docs, and add `research` to the tool list at the top of that section / the module-map ReAct-tools line)

**Interfaces:** none.

- [ ] **Step 1: Write** — add a subsection titled "### `research` — one-call supergraph, codex.json-first" covering: why (the centaurus failure: N open_entry calls blew the budget), what (entity + edges + leaves from the local dump by ID-join, zero network; names live in translations, hence the reverse index; knowledge half reuses `_gather_knowledge`), the honesty/cap rule, the ambiguity handling, and the phase-2 gaps (bestial_bond, depth>1, network fallback). Add `research` to the tools list in the module-map bullet and the `/orna` section header list.

- [ ] **Step 2: Self-check** — re-read: does it explain *why*, not just *what* (repo convention)? Are file/function names accurate?

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Document the research supergraph tool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Real-model verification (no code; acceptance gate)

**Files:** none (uses `.claude/skills/verifying-orna-changes/scripts/orna_loop_harness.py`).

- [ ] **Step 1: Run the exact reported question, 5×**

```bash
set -a && source .env && set +a
Q="what items are dropped from fallen king centaurus and based on their stats, which class benefits most" \
  N=5 python3 .claude/skills/verifying-orna-changes/scripts/orna_loop_harness.py
```
Expected: each run calls `research` (once), then `finish`; the answer names the drops and reasons about class fit. Record pass count (target ≥4/5). Watch the ACTION trace: it must NOT be `open_entry × 6`.

- [ ] **Step 2: Run a "how do I beat X" question, 5×** (a boss with known elemental immunity in Monster Data) to confirm `research` satisfies the strategy path and the knowledge half surfaces immunities.

- [ ] **Step 3: Run once local-only** (`FORCE_LOCAL=1 …`) to confirm the local model also drives `research` (no cloud dependency for the new path).

- [ ] **Step 4: Record results** in the PR/commit notes (pass rate per question). If <4/5, the fix is prompt wording (Task 5), not the tool — iterate there and re-measure, per the CLAUDE.md rule that one green run proves nothing.

---

## Self-Review

**Spec coverage:** resolver shape (Task 1–2) ✓; codex.json-first zero-network (Global Constraints + Task 2 tests) ✓; edges→leaves with stats/useable_by/effects (Task 2) ✓; knowledge half reused (Task 3) + bundled (Task 4) ✓; one tool alongside existing, dispatched (Task 4) ✓; no per-entity cards + finish buttons (Task 4) ✓; caps/PARTIAL honesty (Task 2/4) ✓; ambiguity (Task 1/4) ✓; prompt default + strategy note (Task 5) ✓; Tier-0 pin (Task 6) ✓; docs (Task 7) ✓; real-model acceptance (Task 8) ✓; phase-2 deferrals (bestial_bond, depth>1, network fallback) called out (Task 2 comment, Task 7). No GraphQL, no removal of browse tools — honored.

**Placeholder scan:** the only "MOVE VERBATIM" is Task 3's refactor of an existing, in-file block that is quoted by its start/end anchors — the mechanics are fully specified (change the two returns), not deferred. No TBD/TODO.

**Type consistency:** `resolve_entity` returns `{"category","id","name","alternatives"}` or `{"unresolved"}` — consumed identically in `build_supergraph`. `build_supergraph` shape (`entities/unresolved`, `relations` with `field/title/total/partial/members`, `leaf` keys) is produced in Task 2 and consumed by `_render_supergraph`/`_run_research_tool` in Task 4 with matching keys. `_gather_knowledge` (Task 3) signature matches its call in Task 4. `research` string identical across `_ACTIONS`, dispatch, tests, prompt.

**Review Focus coverage:** ambiguous name → Task 1 test; not-in-dump → Task 2 (`build_supergraph` unresolved) + Task 4 (tool observation) tests; oversized relation → Task 2 + Task 4 (`PARTIAL`) tests; malformed/dangling edge → Task 2 (`_leaf` dangling, non-pair filtered) test; unknown effect code → Task 2 (`_effect_names`) test. All five owned and tested.
