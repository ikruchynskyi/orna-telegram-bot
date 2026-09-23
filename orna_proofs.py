"""
orna_proofs.py
==============
Guild-proof exchange-rate math, ported from OrnaCodex's ProofView.vue:
https://github.com/67au/OrnaCodex/blob/main/src/views/ProofView.vue

Each guild sells materials in exchange for its own proof/currency item.
How many proofs a given amount of a material costs depends on:

  - the material's base exchange rate, derived from its tier + rarity
  - a per-currency scaling factor (Tower Shards and Coral/Deepshards are
    "worth" much less per unit than a Proof of X, so their count scales up)

    base_rate      = tier * 10 + RARITY_SCALING[rarity] * 5
    exchange_rate  = base_rate * GUILD_PROOFS[guild].scaling
    proofs_needed  = ceil(count * exchange_rate / 100)
"""
from __future__ import annotations

import math
from typing import Dict, NamedTuple


class GuildProof(NamedTuple):
    currency: str  # in-game name of the guild's proof/currency item
    scaling: int   # OrnaCodex "proofScaling" factor for that currency


# Guild name -> (currency name, scaling factor). Order/keys line up with
# orna_sheets.GUILD_NAMES — keep both in sync.
GUILD_PROOFS: Dict[str, GuildProof] = {
    "Agony":       GuildProof("Proof of Agony", 1),
    "Despair":     GuildProof("Proof of Despair", 1),
    "Melancholy":  GuildProof("Proof of Melancholy", 1),
    "Torment":     GuildProof("Proof of Torment", 1),
    "Coral":       GuildProof("Coral", 20),
    "Deepshards":  GuildProof("Deepshard", 20),
    "Remembrance": GuildProof("Proof of Remembrance", 2),
    "Sparring":    GuildProof("Proof of Sparring", 4),
    "Trials":      GuildProof("Proof of Trials", 2),
    "Towers":      GuildProof("Tower Shard", 200),
}

# ProofView.vue's rarityScaling
RARITY_SCALING: Dict[str, int] = {
    "common": 0,
    "rare": 1,
    "famed": 2,
    "legendary": 3,
}

CAL_SCALING = 100  # "calScaling" in ProofView.vue


def base_exchange_rate(tier: int, rarity: str) -> int:
    """ProofView.vue's getBaseExchangeRate: tier*10 + rarityScaling*5."""
    return tier * 10 + RARITY_SCALING.get(rarity.strip().lower(), 0) * 5


def proofs_needed(count: int, guild: str, base_rate: int) -> int:
    """ProofView.vue's proofCounts: ceil(count * scaling * base_rate / 100)."""
    guild_proof = GUILD_PROOFS[guild]
    exchange_rate = guild_proof.scaling * base_rate
    return math.ceil(count * exchange_rate / CAL_SCALING)


def _demo() -> None:
    """Pins this module's own formulas against known-correct values - a
    typo'd constant here (e.g. a wrong RARITY_SCALING or scaling factor)
    would otherwise only surface as a subtly-wrong-looking proof report
    days later. Run directly: python3 orna_proofs.py"""
    assert base_exchange_rate(10, "legendary") == 115  # 10*10 + 3*5
    assert base_exchange_rate(5, "common") == 50  # 5*10 + 0*5
    assert base_exchange_rate(7, "rare") == 75  # 7*10 + 1*5
    assert base_exchange_rate(10, "  Legendary  ") == base_exchange_rate(10, "legendary"), \
        "rarity lookup should be case/whitespace tolerant"
    assert base_exchange_rate(5, "mythic") == 50  # unknown rarity -> 0 scaling, not a KeyError

    assert proofs_needed(500, "Agony", 115) == 575  # scaling 1: 500*1*115/100, exact
    assert proofs_needed(500, "Towers", 115) == 115000  # scaling 200: "worth much less per unit"
    assert proofs_needed(1, "Coral", 50) == 10  # scaling 20, exact
    assert proofs_needed(1, "Remembrance", 33) == 1  # scaling 2: 0.66 -> ceil to 1

    print("orna_proofs: all self-checks passed")


if __name__ == "__main__":
    _demo()
