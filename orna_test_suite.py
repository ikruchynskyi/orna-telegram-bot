#!/usr/bin/env python3
"""
orna_test_suite.py - regression suite for the bot, in three tiers, built to be
as deterministic as a suite over an LLM-driven ReAct loop can be.

Run it before and after a major update and compare. There is no framework and
no mocking layer, deliberately: this bot is glue over live services (Google
Sheets, playorna/aussiescodex, Ollama), so a suite that mocks them tests
nothing that breaks in production. See .claude/skills/verifying-orna-changes.

    set -a && source .env && set +a
    python3 orna_test_suite.py                  # tier 0 only (fast, exact)
    TIER=0,1,2,3 N=3 python3 orna_test_suite.py # the real gate
    TIER=2 CASE=trifecta-mages python3 orna_test_suite.py
    SAVE_BASELINE=1 TIER=0,1,2,3 N=3 python3 orna_test_suite.py   # record
    FORCE_LOCAL=1 JOBS=1 TIER=1 python3 orna_test_suite.py        # local model

THE TIERS
  0  deterministic  no LLM at all. Pure functions and retrieval against live
                    data. MUST be 100%: a failure here is a real bug, never
                    noise. This tier is the regression net for every pure-
                    function bug in CLAUDE.md, and it runs each module's own
                    _demo() rather than restating its asserts.
  1  simple         one fact, normally one tool ("what tier is X").
  2  medium         a few tools ("which items of set X can mages use", an
                    assess + a number, a live tower floor).
  3  hard           research and judgement - the loop must reach for a source
                    its codex data does not contain, keep working until its
                    claim is actually supported, and answer in the user's
                    language.

HOW DETERMINISM IS ACHIEVED (and where it stops)
  * Tier 0 is genuinely deterministic - same input, same answer, exactly.
  * Tiers 1-3 cannot be. The model picks tools and writes prose. So:
    - This file sets ORNA_LLM_TEMPERATURE=0 and ORNA_LLM_SEED before importing
      the bot, which ollama_client turns into Ollama's own sampling options.
      That makes a step's reply repeatable for an identical prompt. It is not a
      guarantee: a cloud model may be re-quantised or rerouted between runs,
      and the cloud->local fallback changes the model mid-request.
    - Grading is on FACTS, never on prose or exact strings: a substring that
      must appear, one that must not, which tools the trace must contain. Any
      phrasing that carries the right fact passes.
    - Every expectation is DERIVED FROM LIVE DATA at run time (see
      build_cases), never hardcoded. Orna is patched monthly; a suite full of
      pinned numbers fails on the patch, not on the regression.
    - Each case runs N times and scores a PASS RATE. One green run proves
      nothing about a non-deterministic system. The gate is the rate against a
      per-tier threshold, plus a comparison against the recorded baseline, so
      what you read is movement rather than a coin flip.

READING A FAILURE
  A tier-0 failure is a bug: fix it. A tier 1-3 rate that DROPPED against the
  baseline is the signal to chase; the printed reason names the missing fact or
  tool. A rate that was always ~0.6 is a known-weak prompt path, not a new
  regression - which is exactly why the baseline file exists.

  A SINGLE drop on one case is weak evidence. Measured while building this:
  `stacking-total` scored 1/3 and then 3/3 on consecutive batches with no code
  change in between, and `ukrainian-lock` went 1/2 then 3/3. So re-run the
  case before believing a regression (CASE=<id> N=5), and record a baseline
  with the largest N you can afford - a baseline captured from one lucky batch
  manufactures false alarms later.

  BEFORE "fixing" a failure, check the EXPECTATION. Twice while building this
  suite the grader was wrong and the bot was right: `min_tools=2` on the
  research cases was unsatisfiable (finish() never appears in the trace), and
  the original Judge-Trifecta case asserted a fact that was simply false
  (`Scroll of the Judges Trifecta` IS mage-useable; the substring "judge
  trifecta" does not match "judgeS trifecta"). A live run said so and was
  right. Read the actual answer and the trace before touching the bot.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

REPO_ROOT = os.environ.get("ORNA_REPO_ROOT") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)

if not os.environ.get("SHEETS_API_KEY"):
    sys.exit("SHEETS_API_KEY is not set - run `set -a && source .env && set +a` first "
             "(orna_sheets reads it at import, so the modules will not even load).")

# Must be set BEFORE importing the bot: ollama_client reads them at import.
os.environ.setdefault("ORNA_LLM_TEMPERATURE", "0")
os.environ.setdefault("ORNA_LLM_SEED", "20260925")

import orna_assess                      # noqa: E402
import orna_echo                        # noqa: E402
import orna_aussies                     # noqa: E402
import orna_guides                      # noqa: E402
import orna_knowledge                   # noqa: E402
import orna_mechanics                   # noqa: E402
import orna_towers                      # noqa: E402
import telegram_orna as T               # noqa: E402

CYRILLIC = re.compile(r"[Ѐ-ӿ]")
BASELINE_PATH = os.environ.get("BASELINE") or os.path.join(REPO_ROOT, "orna_test_baseline.json")
# A tier-1 fact lookup should never fail; the prompt-dependent tiers are
# measured, not assumed - these thresholds are what this repo has actually
# observed as healthy, not aspirations.
THRESHOLDS = {1: 1.0, 2: 0.66, 3: 0.66}


# ---------------------------------------------------------------- tier 0 ----

def _run_module_demos() -> None:
    """Each module's own _demo() - the repo's existing self-checks, which are
    the real pins for its pure-function bug history. Called rather than
    restated so there is no second copy to drift."""
    for mod in (orna_assess, orna_aussies, orna_guides, orna_knowledge, orna_mechanics, orna_towers, T):
        demo = getattr(mod, "_demo", None)
        if demo is None:
            continue
        demo()


def _check_useable_by_absent_field() -> None:
    """A record with no useable_by (every raid/monster/boss) must not match a
    class filter. It used to default to "all_classes" and match every one, so
    a RAID was reported as mage-useable."""
    mage = {"kind": "attr", "field": "useable_by", "cmp": "=", "value": "mage"}
    assert not orna_aussies._eval_condition({"category": "raids", "id": "r", "name": "R"}, mage)
    assert orna_aussies._eval_condition(
        {"category": "items", "id": "i", "name": "I", "useable_by": "all_classes"}, mage), \
        "an explicit all_classes item must still match a specific class"


def _check_bogus_field_is_reported() -> None:
    """A field that resolves to nothing must be REPORTED, not answered with 0
    rows - "nothing was searched" read as "nothing exists" live."""
    bad = orna_aussies.unresolvable_condition_fields(
        [{"kind": "attr", "field": "dropped_by", "value": "x"}])
    assert len(bad) == 1 and bad[0][1] == "dropped_by", bad
    assert orna_aussies.unresolvable_condition_fields(
        [{"kind": "attr", "field": "useable_by", "value": "mage"},
         {"kind": "stat", "field": "magic", "cmp": ">", "value": 250}]) == []


def _check_observation_is_honest() -> None:
    """The name list handed to the MODEL must never be silently shorter than
    the count it states - that is how a 13-item set got answered from 5."""
    many = [{"name": f"n{i}", "url": f"/u/{i}/"} for i in range(T._MAX_NAMES_IN_OBSERVATION + 3)]
    obs = T._names_observation(many)
    assert "+3 MORE not listed" in obs and "PARTIAL" in obs, obs[-100:]
    assert "more" not in T._names_observation(many[:2])


def _check_set_membership_is_complete() -> None:
    """The retrieval half of the Judge Trifecta failure: the class-split of the
    set must come back exhaustively, not from whichever few entries got opened.

    Keyed on the RAID'S OWN `drops` list, not on a name substring. The first
    version of this check asserted "no item whose NAME contains 'judge
    trifecta' is mage-useable" and was simply WRONG: `Scroll of the Judges
    Trifecta` is `all_classes`, and the substring "judge trifecta" does not
    match "judgeS trifecta". A live loop run said so and was right where this
    check was wrong - the same plural/possessive lossiness as the possessive
    name bug in `telegram_orna._name_candidates`. A set-name substring is a
    lossy stand-in for set membership; the drop list is the fact."""
    codex = orna_aussies._codex()["main"]
    drops = [d[1] for d in (codex["raids"]["judge-trifecta-maximus"].get("drops") or [])]
    assert len(drops) == 12, drops
    buckets = {}
    for rid in drops:
        buckets.setdefault(codex["items"][rid]["useable_by"], []).append(rid)
    assert sorted(buckets) == ["thief_classes", "valhallan_summoner_classes", "warrior_classes"], buckets
    assert all(len(v) == 4 for v in buckets.values()), buckets
    # ...and the scroll that is NOT a drop of that raid, but IS mage-useable -
    # recorded so this fact cannot be lost again.
    scroll = codex["items"]["scroll-of-the-judges-trifecta"]
    assert scroll["useable_by"] == "all_classes" and "scroll-of-the-judges-trifecta" not in drops


def _check_quality_boundaries() -> None:
    """get_quality_code's boundaries, incl. the exactly-170 case that used to
    return Broken because in_range was [lo, hi)."""
    assert orna_assess.get_quality_code(170, 1) == orna_assess.get_quality_code(169, 1)
    for level, code in ((11, 7), (12, 8), (13, 9)):
        assert orna_assess.get_quality_code(100, level) == code, level


def _check_name_resolution_forms() -> None:
    """The possessive goes both ways, and a phone's curly apostrophe counts."""
    assert "Ymir Brilliant Feathers" in T._name_candidates("Ymir's Brilliant Feathers")
    assert "Cupid's Locket" in T._name_candidates("Cupid Locket")
    assert "Cupid's Locket" in T._name_candidates("Cupid’s Locket")


def _check_quality_spec_axes() -> None:
    """Quality and LEVEL are independent - collapsing them projected every
    upgraded item unupgraded."""
    assert T._parse_quality_spec("185% lv10") == (185, 10)
    assert T._parse_quality_spec("godforged") == (100, 13)
    assert T._parse_quality_spec("100") == (100, 1)


def _check_towers_are_self_consistent() -> None:
    """Pure time math with no data source - so the tool and the module must
    agree exactly, at the same instant."""
    from datetime import datetime, timezone
    floors = orna_towers.get_tower_floors(datetime.now(timezone.utc))
    assert len(floors) == 5 and all(1 <= f.floor <= 50 for f in floors), floors


def _check_mechanics_wired_into_loop() -> None:
    """The verified mechanics corpus must be reachable through the SAME module
    the loop imports (T.orna_mechanics), not just as a standalone file - this
    is what would have caught a missing/typo'd import in _run_knowledge_tool.
    Synchronous on purpose (no network), unlike the full knowledge tool which
    also hits reddit/bonuses/classes."""
    fac = T.orna_mechanics.search("factions element damage")
    for name in ("Earthen Legion", "Stormforce", "Knights of Inferno", "Frozenguard"):
        assert name in fac, (name, fac[:200])
    assert "+25%" in fac and "-20%" in fac, fac[:120]
    assert "+1%" in T.orna_mechanics.search("ascension level"), "AL scaling must be findable"


def _check_ban_guard() -> None:
    """Ban/unban round-trip, persistence, and the pre-dispatch guard.

    _STORE_PATH is redirected to a temp file for the duration: usage_stats.ban
    writes the REAL usage_stats.json, and a suite meant to be run routinely
    must never mutate live moderation state or counters."""
    import asyncio as _asyncio
    import pathlib as _pathlib
    import tempfile

    import telegram_bot
    import usage_stats
    from telegram.ext import ApplicationHandlerStop

    victim = "999000111"
    real_path, real_banned = usage_stats._STORE_PATH, dict(usage_stats._banned)
    with tempfile.TemporaryDirectory() as tmp:
        usage_stats._STORE_PATH = _pathlib.Path(tmp) / "usage_stats.json"
        usage_stats._banned.clear()
        try:
            assert usage_stats.ban(victim, "spam", by=1) is True
            assert usage_stats.ban(victim) is False, "a second ban must report already-banned"
            assert usage_stats.is_banned(victim) and usage_stats.is_banned(int(victim)), \
                "is_banned must accept both an int and a str id"
            assert usage_stats._STORE_PATH.exists(), "a ban must be persisted immediately"
            # A ban is a moderation decision, not a statistic - clearing the
            # counters must not quietly readmit a spammer.
            usage_stats.reset()
            assert usage_stats.is_banned(victim), "reset() must not clear bans"

            class _U:
                def __init__(self, uid):
                    self.effective_user = type("u", (), {"id": uid})()
                    self.effective_chat = type("c", (), {"id": uid})()

            try:
                _asyncio.run(telegram_bot.drop_banned(_U(int(victim)), None))
                raise AssertionError("the guard must stop a banned user's update")
            except ApplicationHandlerStop:
                pass
            # ...and must be a no-op for everyone else
            _asyncio.run(telegram_bot.drop_banned(_U(4242), None))
            assert usage_stats.unban(victim) is True and not usage_stats.is_banned(victim)
            assert usage_stats.unban(victim) is False
        finally:
            usage_stats._STORE_PATH = real_path
            usage_stats._banned.clear()
            usage_stats._banned.update(real_banned)


TIER0 = [
    ("module-demos", _run_module_demos),
    ("useable-by-absent-field", _check_useable_by_absent_field),
    ("bogus-field-reported", _check_bogus_field_is_reported),
    ("observation-honesty", _check_observation_is_honest),
    ("set-membership-complete", _check_set_membership_is_complete),
    ("quality-boundaries", _check_quality_boundaries),
    ("name-resolution-forms", _check_name_resolution_forms),
    ("quality-vs-level", _check_quality_spec_axes),
    ("towers-consistent", _check_towers_are_self_consistent),
    ("mechanics-wired", _check_mechanics_wired_into_loop),
    ("echo-corpus", lambda: orna_echo._demo()),
    ("ban-guard", _check_ban_guard),
]


# ------------------------------------------------------------ loop cases ----

@dataclass
class Expect:
    """What must be true of a run. Two haystacks, because they answer
    different questions: `all_*` sees the whole transcript (the tool CARDS are
    where the data actually is - finish() is only a closing line), while
    `final_*` sees just the finish text (a card legitimately showing "Warrior
    classes" must not excuse an answer that CLAIMS the set is warrior-only)."""
    all_of: list = field(default_factory=list)          # every string present (transcript)
    any_of: list = field(default_factory=list)          # each inner list: >=1 present
    none_of: list = field(default_factory=list)
    final_all_of: list = field(default_factory=list)
    final_any_of: list = field(default_factory=list)
    final_none_of: list = field(default_factory=list)
    tools_all: list = field(default_factory=list)
    tools_any: list = field(default_factory=list)       # >=1 of these was called
    # NOTE finish() is NEVER in the trace: _advance_inner's finish branch
    # returns before the assistant message is appended to the session. So this
    # counts RESEARCH calls only - min_tools=1 catches "answered with no tool
    # at all" (the prompt-only-answer failure), and asking for 2 because "it
    # should research then finish" is unsatisfiable.
    min_tools: int = 1
    tool_counts: dict = field(default_factory=dict)     # tool -> minimum calls
    cyrillic: str = ""                                  # "" | "required" | "forbidden"

    def grade(self, transcript: str, final: str, tools: list) -> str:
        """Empty string = pass, else the first reason it failed."""
        hay, fin = transcript.lower(), final.lower()
        for needle in self.all_of:
            if needle.lower() not in hay:
                return f"transcript missing {needle!r}"
        for group in self.any_of:
            if not any(n.lower() in hay for n in group):
                return f"transcript has none of {group}"
        for needle in self.none_of:
            if needle.lower() in hay:
                return f"transcript should not contain {needle!r}"
        for needle in self.final_all_of:
            if needle.lower() not in fin:
                return f"answer missing {needle!r}"
        for group in self.final_any_of:
            if not any(n.lower() in fin for n in group):
                return f"answer has none of {group}"
        for needle in self.final_none_of:
            if needle.lower() in fin:
                return f"answer should not contain {needle!r}"
        if len(tools) < self.min_tools:
            return f"only {len(tools)} tool call(s), expected >= {self.min_tools}"
        for name in self.tools_all:
            if name not in tools:
                return f"never called {name} (called: {tools})"
        if self.tools_any and not any(n in tools for n in self.tools_any):
            return f"called none of {self.tools_any} (called: {tools})"
        for name, least in self.tool_counts.items():
            if tools.count(name) < least:
                return f"called {name} {tools.count(name)}x, expected >= {least}"
        if self.cyrillic == "forbidden" and CYRILLIC.search(final):
            return "answer must be in English but contains Cyrillic"
        if self.cyrillic == "required" and not CYRILLIC.search(final):
            return "answer must be in Ukrainian but contains no Cyrillic"
        return ""


@dataclass
class Case:
    id: str
    tier: int
    q: str
    expect: Expect


def build_cases() -> list:
    """Cases, with every expectation DERIVED FROM LIVE DATA now - so a game
    patch that legitimately changes a number does not read as a regression."""
    from datetime import datetime, timezone
    items = orna_aussies._codex()["main"]["items"]

    def rec(slug):
        return items[slug]

    def stat(slug, key):
        return (rec(slug).get("stats") or {}).get(key)

    falx, robe, helmet, feathers = (rec("judge-trifecta-falx"), rec("judge-trifecta-robe"),
                                    rec("lost-helmet"), rec("brilliant-feathers"))

    # godforged (level 13) bonus scaling, the same call _run_assess_tool makes
    gf_code = orna_assess.get_quality_code(100, 13)

    def gf_bonus(slug):
        raw = str(stat(slug, "orn_bonus") or "0").strip("%+")
        return orna_assess.get_quality_bonus(float(raw), 100, gf_code, key="orn_bonus")

    lh_orn = gf_bonus("lost-helmet")
    dm_orn = gf_bonus("dark-mage-hood")
    stacked_pct = ((1 + lh_orn / 100) * (1 + dm_orn / 100) - 1) * 100

    # The Ward formula as the corpus states it, e.g. "(HP + MP) / 2" - pulled
    # from the guide rather than hardcoded here.
    ward_hit = orna_echo.search_text("ward capacity base formula")
    ward_formula = next((ln.strip() for ln in ward_hit.splitlines()
                         if "ward_base" in ln.lower() and "=" in ln), "")
    assert ward_formula, "ward formula not found in orna_echo.txt - re-run orna_scrape_echo.py"
    ward_formula = ward_formula.split("=", 1)[1].strip()          # the right-hand side

    top_magic = orna_aussies.query_records([], sort_by="magic", sort_dir="desc", limit=1)
    eos = next(f.floor for f in orna_towers.get_tower_floors(datetime.now(timezone.utc))
               if f.kind == "eos")

    return [
        # ------------------------------------------------ tier 1: one fact --
        Case("falx-facts", 1, "what tier and rarity is Judge Trifecta Falx?",
             Expect(all_of=[str(falx["tier"]), falx["rarity"]],
                    tools_any=["search_codex", "open_entry", "query"])),
        Case("robe-classes", 1, "which classes can use Judge Trifecta Robe?",
             Expect(final_any_of=[["valhallan", "summoner"]],
                    tools_any=["search_codex", "open_entry", "query"])),
        Case("helmet-slot", 1, "what equipment slot does the Lost Helmet go in?",
             Expect(final_any_of=[[helmet["place"], "голов"]],
                    tools_any=["search_codex", "open_entry", "query"])),
        Case("feathers-slots", 1, "how many adornment slots does Brilliant Feathers have?",
             Expect(all_of=[str(int(feathers["stats"]["adornment_slots"]))],
                    tools_any=["search_codex", "open_entry", "query", "assess"])),

        # -------------------------------------------- tier 2: a few tools --
        # The live 2026-09-25 report: answered from 5 of 13 results and called
        # the set "warrior or thief classes only", missing 4 valhallan pieces.
        # Asked about the RAID'S DROPS on purpose. The original phrasing ("does
        # Judge Trifecta drop items useable by mages?") is genuinely ambiguous -
        # `Scroll of the Judges Trifecta` is all_classes but is NOT one of the
        # raid's 12 drops, so "yes" and "no" are both defensible and the case
        # graded a coin flip. Grade the thing with one right answer: the
        # three-way class split the loop used to get wrong by sampling.
        Case("trifecta-classes", 2,
             "which classes can use the items dropped by Judge Trifecta Maximus?",
             Expect(final_all_of=["warrior", "thief"],
                    final_any_of=[["valhallan", "summoner"]],
                    tools_any=["query", "open_entry"])),
        Case("godforged-orn", 2, "what is the orn bonus of a godforged Lost Helmet?",
             Expect(all_of=[f"{lh_orn:g}"], tools_all=["assess"])),
        Case("eos-floor", 2, "what floor is the Eos wild tower on right now?",
             Expect(all_of=[str(eos)], tools_all=["towers"])),
        Case("top-magic", 2, "which item has the highest magic stat?",
             Expect(all_of=[str(top_magic[0].sort_value)], tools_any=["query"])),

        # The playerecho guide corpus is the ONLY source that states a formula
        # outright; before it existed the bot had nothing to answer this from.
        # The expected formula is read out of the corpus at run time, so a
        # revision of the guide does not read as a regression.
        Case("ward-formula", 2, "how is ward capacity calculated in orna?",
             Expect(all_of=[ward_formula], tools_all=["knowledge_search"])),

        # --------------------------- tier 3: research and judgement --------
        # _STRATEGY_RULE: a boss's elemental immunities are in NO structured
        # source, so codex facts alone must not be enough to finish.
        Case("sirus-strategy", 3, "how do I kill Knight Sirus?",
             Expect(tools_any=["knowledge_search", "web_search"],
                    final_none_of=["no known weaknesses"])),
        # _CLASS_GUIDE_RULE + the language lock, which lost to the prompt's
        # own Ukrainian examples until it was made deterministic.
        Case("heretic-build", 3, "give me build advice for the heretic omniflask raid build",
             Expect(tools_all=["class_guide"], cyrillic="forbidden")),
        # _AGGREGATE_RULE: assess per NAMED item (a codex page shows the
        # UNSCALED base, only assess produces the quality-scaled number), then
        # stack multiplicatively and report the bonus form.
        Case("stacking-total", 3,
             "I have a godforged Lost Helmet and a godforged Dark Mage Hood. "
             "What is my total orn bonus from them?",
             Expect(all_of=[f"{lh_orn:g}"],
                    final_any_of=[[f"{stacked_pct:.0f}", f"{stacked_pct:.1f}"]],
                    tool_counts={"assess": 2})),
        # The language lock the other way, including PROPER NAMES staying in
        # English inside a Ukrainian reply - a translated name matches nothing.
        Case("ukrainian-lock", 3, "які класи можуть використовувати Judge Trifecta Falx?",
             Expect(cyrillic="required", final_any_of=[["thief", "злод", "розбій"]],
                    tools_any=["search_codex", "open_entry", "query"])),
    ]


# -------------------------------------------------------------- the loop ----

class FakeMessage:
    """Records replies instead of sending them - same stand-in the skill's
    harness uses. Any reply_* call becomes an async no-op that appends to a
    shared sink."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def __getattr__(self, name: str):
        async def _record(*args, **kwargs):
            text = kwargs.get("text") or kwargs.get("caption") or (args[0] if args else "") or ""
            self._sink.append((name, str(text)))
            return FakeMessage(self._sink)
        return _record


async def run_case_once(case: Case) -> tuple:
    """-> (reason, tools, final). reason is "" on pass."""
    replies: list = []
    messages = [{"role": "system", "content": T._orna_system_prompt(case.q)},
                {"role": "user", "content": case.q}]
    sid = T._new_orna_session(messages, T.MAX_STEPS)
    try:
        await T._advance(sid, FakeMessage(replies))
    except Exception as exc:
        return f"loop raised {exc!r}", [], ""

    tools, session = [], T._ORNA_SESSIONS.get(sid)
    for msg in (session.messages if session else []):
        if msg.get("role") != "assistant":
            continue
        try:
            action = json.loads(msg["content"]).get("action")
        except Exception:
            continue
        if action:
            tools.append(action)

    texts = [t for name, t in replies if name != "delete"]
    # The status message edits itself in place and is deleted at the end; it is
    # chrome, never an answer, so it must not be graded as the finish text.
    answers = [t for t in texts if not t.startswith(("\U0001F914", "\U0001F50E", "\U0001F4DA", "⏳"))]
    final = answers[-1] if answers else (texts[-1] if texts else "")
    return case.expect.grade("\n".join(texts), final, tools), tools, final


async def run_case(case: Case, n: int) -> dict:
    passes, reasons = 0, []
    for _ in range(n):
        reason, tools, _final = await run_case_once(case)
        if reason:
            reasons.append(f"{reason}  [tools: {','.join(tools) or 'none'}]")
        else:
            passes += 1
    return {"id": case.id, "tier": case.tier, "runs": n, "passes": passes,
            "rate": passes / n if n else 0.0, "reasons": reasons[:3]}


def run_tier0(only: str) -> list:
    """Synchronous on purpose: these checks need no event loop, and some
    module _demo()s call asyncio.run() themselves - which raises if we are
    already inside a loop. So tier 0 runs before the async part starts."""
    print("=" * 72, "\nTIER 0 - deterministic (must be 100%)\n" + "=" * 72)
    failures = []
    for name, check in TIER0:
        if only and only != name:
            continue
        try:
            check()
            print(f"  PASS  {name}")
        except Exception as exc:
            print(f"  FAIL  {name}: {exc}")
            failures.append(f"tier0/{name}")
    return failures


async def main(failures: list, started: float) -> int:
    tiers = {int(x) for x in (os.environ.get("TIER") or "0").split(",") if x.strip()}
    only = os.environ.get("CASE") or ""
    n = int(os.environ.get("N", "1"))
    jobs = max(1, int(os.environ.get("JOBS", "3")))
    if os.environ.get("FORCE_LOCAL") == "1":
        T.MAX_CLOUD_CALLS = 0      # every step tries cloud first; 0 forces local
    results = []

    cases = [c for c in build_cases() if c.tier in tiers and (not only or only == c.id)]
    if cases:
        mode = "LOCAL only" if os.environ.get("FORCE_LOCAL") == "1" else "CLOUD -> local"
        print("\n" + "=" * 72)
        print(f"TIERS {sorted(t for t in tiers if t)} - {len(cases)} case(s) x N={n}, "
              f"jobs={jobs}, model={mode}")
        print("temperature/seed pinned; rates are still rates - read movement, not one run")
        print("=" * 72)
        sem = asyncio.Semaphore(jobs)

        async def guarded(case):
            async with sem:
                res = await run_case(case, n)
                mark = "PASS" if res["rate"] >= THRESHOLDS.get(case.tier, 0.66) else "FAIL"
                print(f"  {mark}  [{case.tier}] {case.id:<18} {res['passes']}/{n}")
                for reason in res["reasons"]:
                    print(f"          - {reason}")
                return res

        results = await asyncio.gather(*(guarded(c) for c in cases))
        failures += [f"tier{r['tier']}/{r['id']}" for r in results
                     if r["rate"] < THRESHOLDS.get(r["tier"], 0.66)]

    # Baseline comparison is the real regression signal for a rate-based gate.
    baseline = {}
    if os.path.exists(BASELINE_PATH):
        try:
            with open(BASELINE_PATH) as fh:
                baseline = {r["id"]: r for r in json.load(fh).get("cases", [])}
        except Exception as exc:
            print(f"\n(could not read baseline {BASELINE_PATH}: {exc})")
    drops = [(r["id"], baseline[r["id"]]["rate"], r["rate"]) for r in results
             if r["id"] in baseline and r["rate"] < baseline[r["id"]]["rate"] - 1e-9]
    if drops:
        print("\nREGRESSIONS vs baseline:")
        for cid, was, now in drops:
            print(f"  {cid}: {was:.2f} -> {now:.2f}")
    elif baseline and results:
        print("\nNo case scored below its baseline.")

    if os.environ.get("SAVE_BASELINE") == "1" and results:
        # MERGE, never replace: a full N=3 sweep of every tier runs well past
        # any single sensible timeout, so a baseline is normally recorded one
        # tier at a time. Replacing would silently drop the tiers not in this
        # run and destroy the comparison they exist for.
        merged = dict(baseline)
        merged.update({r["id"]: {k: r[k] for k in ("id", "tier", "runs", "passes", "rate")}
                       for r in results})
        with open(BASELINE_PATH, "w") as fh:
            json.dump({"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "n": n,
                       "cases": sorted(merged.values(), key=lambda r: (r["tier"], r["id"]))},
                      fh, indent=2)
        print(f"Baseline updated ({len(results)} of {len(merged)} cases) -> {BASELINE_PATH}")

    print(f"\n{'=' * 72}\n{'FAILED: ' + ', '.join(failures) if failures else 'ALL GREEN'}"
          f"   ({time.time() - started:.0f}s)\n{'=' * 72}")
    return 1 if failures or drops else 0


if __name__ == "__main__":
    _tiers = {int(x) for x in (os.environ.get("TIER") or "0").split(",") if x.strip()}
    _started = time.time()
    _failures = run_tier0(os.environ.get("CASE") or "") if 0 in _tiers else []
    sys.exit(asyncio.run(main(_failures, _started)))
