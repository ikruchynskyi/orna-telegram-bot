"""What a build STACKS: one community build (scrapers/aussie_builds) turned into totals per mechanic family, from
the codex records of every item, adornment, anguish bonus and amity it uses, next to the class's own effects.

The archetypes (data/orna_build_archetypes.txt) were mined from these profiles: which families a class line's
builds push far above the line's average, and which combinations recur. Spec:
docs/superpowers/specs/2026-11-06-orna-build-archetypes-design.md.

    python3 -m knowledge.orna_build_profiles <build id>   # one build's profile
    python3 -m knowledge.orna_build_profiles              # self-check
"""

from __future__ import annotations

import json
import re
import sys

import paths

# codex stat key -> mechanic family. Percent-type keys only: flat attack/magic/ward are reported apart (CORE).
FAMILIES = {
    "CRIT": ("crit", "crit_chance", "crit_damage", "critical_chain"),
    "CHAIN": ("chain_damage_chance", "collateral_chance", "collateral_damage", "multi-target_damage"),
    "APEX": ("apex_power", "apex_rate", "apex_saver_chance", "apex_start"),
    "PET": ("follower_stats", "follower_act", "follower_summon_ai", "bestial_bond", "player_→_follower_ability_chance",
            "follower_summon_damage_taken", "no_follower_bonus"),
    "SUMMON": ("summon_stats", "summon_pact_power", "summon_spell_start", "instant_summon_chance",
               "double_summon_chance", "summon_turns"),
    "WARD": ("ward", "ward_power", "ward_recovery", "ward_start", "ward_absorption", "ward_recovery_turn",
             "hp-ward_recovery", "mana-ward_recovery", "defend_power", "damage_protect"),
    "FLASK": ("manaflask_power", "manaflask_charge", "flask_saver_chance", "flask_charge_on_defeat"),
    "WEAKNESS": ("elemental_weaknesses",),
    "FACTION": ("faction_element_damage",),
    "STATUS_OFF": ("status_effect_chance", "effect_damage"),
    "STATUS_DEF": ("status_protection", "status_reflection", "disperse_status_effect_chance", "debuff_fade"),
    "BUFF": ("buff_duration", "ally_effect_chance"),
    "TWO_HANDED": ("two-handed_power",),
    "LOW_HP": ("low_hp_→_stats", "damage_per_turn_→_stats"),
    "BYPASS": ("ward_bypass", "def_res_penetration", "damage_to_ward"),
    "TURNS": ("turn_reduction", "turn_delay_chance", "frenzy", "dodge_chance"),
    "SUSTAIN": ("life_siphon", "healing", "hp_regen", "mana_regen", "mana_reduction"),
    "FARM": ("dungeon_horde_bonus", "avidity_chance", "orn_bonus", "gold_bonus", "exp_bonus", "luck_bonus",
             "view_distance", "monster_encounters", "berserk_encounters", "memory_hunting_rewards", "quest_rewards",
             "item_drop_quality_bonus", "tower_shards", "dungeon_cooldown"),
}
_KEY_FAMILY = {k: f for f, keys in FAMILIES.items() for k in keys}
ELEMENTS = ("dark", "holy", "dragon", "arcane", "fire", "water", "earthen", "lightning", "physical")
CORE = ("attack", "magic", "dexterity", "defense", "resistance", "hp", "mana", "foresight")
# an anguish bonus or amity effect, by its words -> family (the build data names them in prose)
_TEXT_FAMILY = (("limitbreak", "CAP"), ("maximumdamagelimit", "CAP"), ("crit", "CRIT"), ("chain", "CHAIN"),
                ("multitarget", "CHAIN"), ("collateral", "CHAIN"), ("apex", "APEX"), ("follower", "PET"),
                ("petact", "PET"), ("summon", "SUMMON"), ("pact", "SUMMON"), ("ward", "WARD"), ("defend", "WARD"),
                ("flask", "FLASK"), ("weakness", "WEAKNESS"), ("faction", "FACTION"), ("statuseffect", "STATUS_OFF"),
                ("buffduration", "BUFF"), ("positivestatus", "BUFF"), ("twohanded", "TWO_HANDED"),
                ("lowhp", "LOW_HP"), ("lowmana", "LOW_HP"), ("frenzy", "TURNS"), ("hybrid", "HYBRID"),
                ("avidity", "FARM"))


def _num(v) -> float:
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group(0)) if m else 0.0


def _text_family(text: str) -> str:
    """An anguish bonus ("Chain Damage Chance") or amity effect ("increasesdragondamageby%") -> family; an
    element's damage -> "EL:<element>". The build data writes amities without spaces, so letters only."""
    low = re.sub(r"[^a-z]", "", text.lower())
    el = next((e for e in ELEMENTS if f"{e}damage" in low or (e == "earthen" and "earthdamage" in low)), None)
    if el:
        return f"EL:{el}"
    return next((f for word, f in _TEXT_FAMILY if word in low), "OTHER")


def _class_effects(name: str) -> dict:
    try:
        d = json.loads((paths.DATA / "orna_classes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return ((d.get("spec_extras") or {}).get(name) or {}).get("bonusStats") or {}


def profile(build: dict, items: dict = None) -> dict:
    """{"families": {family: % total}, "core": {stat: total}, "gives": [...], "causes": [...], "skills": [...],
    "class_effects": {...}, "mode", "class", "spec", "unknown": [ids not in the codex]}."""
    if items is None:
        from orna.orna_aussies import _codex
        items = _codex()["main"]["items"]
    fam, core, gives, causes, skills, unknown, elements = {}, {}, [], [], [], [], {}

    def put(f: str, value: float) -> None:
        if f.startswith("EL:"):
            elements[f[3:]] = elements.get(f[3:], 0) + value
        else:
            fam[f] = fam.get(f, 0) + value

    def add(record: dict, copies: int = 1) -> None:
        for k, v in (record.get("stats") or {}).items():
            if k.endswith("_damage") and k[:-7] in ELEMENTS:
                put(f"EL:{k[:-7]}", _num(v) * copies)
            elif k in _KEY_FAMILY:
                fam[_KEY_FAMILY[k]] = fam.get(_KEY_FAMILY[k], 0) + _num(v) * copies
            elif k in CORE:
                core[k] = core.get(k, 0) + _num(v) * copies
            elif k.startswith("+"):
                skills.append(f"{k[1:]}: {v}")
        gives.extend(g.get("name") for g in record.get("gives") or [] if isinstance(g, dict))
        causes.extend(g.get("name") for g in record.get("causes") or [] if isinstance(g, dict))

    for slot, e in (build.get("equipment") or {}).items():
        if not isinstance(e, dict) or not e.get("itemId"):
            continue
        ids = [e["itemId"]] + [a.get("itemId") for a in e.get("adornments") or [] if a.get("itemId")]
        for i in ids:
            if i in items:
                add(items[i])
            else:
                unknown.append(i)
        ab = e.get("anguishBonus") or {}
        if ab.get("name"):
            put(_text_family(ab["name"]), _num(ab.get("percent")))
    for amity in build.get("amities") or []:
        for b in amity.get("bonuses") or []:
            put(_text_family(b.get("effect", "")), sum(b.get("rolls") or [0]))
    if any(v > 0 for v in elements.values()):
        fam["ELEMENT"] = sum(v for v in elements.values() if v > 0)
    cls = build.get("selectedClass") or ""
    return {"class": cls, "spec": build.get("selectedSpecialisation"), "mode": "PvP" if build.get("pvpMode") else "PvE",
            "families": {k: round(v, 1) for k, v in sorted(fam.items(), key=lambda kv: -kv[1])},
            "core": {k: round(v) for k, v in core.items()}, "elements": {k: round(v) for k, v in elements.items() if v},
            "gives": sorted(set(gives)), "causes": sorted(set(causes)),
            "skills": skills, "class_effects": _class_effects(cls), "unknown": unknown}


# Each archetype / engine section (data/orna_build_archetypes.txt, by title prefix) -> the codex stat keys whose
# best current items build_check lists, so a new item with those stats is found the day the codex has it.
ARCHETYPE_KEYS = {
    "Archetype: Apex chain": ("apex_saver_chance", "apex_rate", "apex_power", "chain_damage_chance"),
    "Archetype: Apex crit": ("crit", "crit_damage", "apex_power", "apex_rate"),
    "Archetype: Chain crit": ("chain_damage_chance", "collateral_chance", "crit"),
    "Archetype: Two-handed": ("two-handed_power", "crit"),
    "Archetype: Pet element": ("follower_stats", "follower_act", "dark_damage", "holy_damage"),
    "Archetype: Weakness flask": ("manaflask_power", "manaflask_charge", "elemental_weaknesses"),
    "Archetype: Summoner": ("summon_stats", "summon_pact_power", "instant_summon_chance"),
    "Archetype: Ward conversion": ("ward_power", "ward", "ward_recovery"),
    "Archetype: Status support": ("status_effect_chance", "status_protection"),
    "Archetype: Redline crit": ("crit", "crit_damage", "low_hp_→_stats", "ward_bypass"),
    "Archetype: Farming": ("view_distance", "dungeon_horde_bonus", "avidity_chance"),
    "Archetype: Defensive PvP": ("ward", "defend_power", "status_protection"),
    "Build engine: the pet": ("follower_stats", "follower_act", "follower_summon_ai"),
    "Build engine: summons": ("summon_stats", "summon_pact_power"),
    "Build engine: Apex": ("apex_power", "apex_rate", "apex_saver_chance"),
    "Build engine: flasks": ("manaflask_power", "manaflask_charge"),
    "Build engine: ward turned": ("ward_power", "ward_recovery"),
    "Build engine: statuses": ("status_effect_chance",),
}


def keys_for(title: str) -> tuple:
    return next((v for k, v in ARCHETYPE_KEYS.items() if title.startswith(k)), ())


def top_items(stat: str, limit: int = 3, items: dict = None) -> list:
    """[(name, place, useable_by, value)] - the codex items with the most of `stat`, best first, one list per
    slot type folded together (gear before adornments)."""
    if items is None:
        from orna.orna_aussies import _codex
        items = _codex()["main"]["items"]
    from orna.orna_aussies import display_name
    rows = []
    for i, r in items.items():
        v = (r.get("stats") or {}).get(stat)
        if v is None or _num(v) <= 0:
            continue
        kind = "adornment" if r.get("item_type") == "adornment" else (r.get("place") or "?")
        rows.append((display_name("items", i) or i, kind, r.get("useable_by") or "", _num(v)))
    rows.sort(key=lambda x: -x[3])
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row[1], []).append(row)
    return [row for kind in sorted(by_kind, key=lambda k: -by_kind[k][0][3]) for row in by_kind[kind][:limit]]


def class_line(name: str) -> str:
    """'Beowulf Auriga' -> 'Beowulf', 'Diety Ursa' -> 'Deity', 'None' -> 'None'."""
    base = (name or "None").split()[0]
    return {"Diety": "Deity", "Grand": "Grand Summoner"}.get(base, base)


def _demo() -> None:
    items = {"a": {"stats": {"crit": "4%", "attack": "12"}}, "w": {"stats": {"chain_damage_chance": "+30%",
                                                                              "+spell": "Shadowveil"}}}
    b = {"selectedClass": "Beowulf Auriga", "pvpMode": True, "equipment": {
        "weapon": {"itemId": "w", "adornments": [{"itemId": "a"}, {"itemId": "a"}],
                   "anguishBonus": {"name": "Chain Damage Chance", "percent": 15}},
        "helmet": {"itemId": "zz"}},
        "amities": [{"bonuses": [{"effect": "criticalhitswillbe%moreeffective", "rolls": [40]},
                                 {"effect": "increasesdragondamageby%", "rolls": [30]}]}]}
    p = profile(b, items)
    assert p["families"] == {"CRIT": 48.0, "CHAIN": 45.0, "ELEMENT": 30}, p
    assert p["elements"] == {"dragon": 30} and _text_family("increasesdarkdamageby%") == "EL:dark"
    assert _text_family("exploitingelementalweaknesseswilldeal%additionaldamage") == "WEAKNESS"
    assert p["core"] == {"attack": 24} and p["unknown"] == ["zz"] and p["mode"] == "PvP" and p["skills"] == ["spell: Shadowveil"]
    assert p["class_effects"].get("follower_stats") == 130 and class_line("Diety Ara") == "Deity"
    assert keys_for("Archetype: Pet element (Beowulf Auriga, the pet does the damage)")[0] == "follower_stats"
    fake = {"x": {"stats": {"follower_stats": "+7%"}, "place": "weapon"}, "y": {"stats": {"follower_stats": "+12%"},
            "place": "weapon"}, "z": {"stats": {"follower_stats": "+5%"}, "place": "x", "item_type": "adornment"}}
    got = top_items("follower_stats", 1, fake)
    assert [(r[1], r[3]) for r in got] == [("weapon", 12.0), ("adornment", 5.0)], got
    print("orna_build_profiles: ok")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        from scrapers import aussie_builds
        b = next(x for x in aussie_builds.load() if x["id"] == sys.argv[1])
        print(json.dumps(profile(b), ensure_ascii=False, indent=1))
    else:
        _demo()
