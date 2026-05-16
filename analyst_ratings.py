"""
Analyst rating extractor — detects upgrades, downgrades, and price target
changes from news headlines and weights them by firm track record.

Tier 1 firms (highest historical accuracy on stock calls) get 3x weight.
Tier 2 get 2x. Unknown firms get 1x. This is additive to the base sentiment
score — a Goldman upgrade can push a borderline conviction score over the
threshold.

Source data: Alpaca news feed (same headlines the sentiment module reads).
No extra API calls needed — we parse what we already fetch.
"""

import re
from dataclasses import dataclass

# ── Firm tiers by historical call accuracy ──────────────────────────────────
# Tier 1: Top banks + shops with strong public track records.
# Tier 2: Solid mid-tier research.
# Unknown firms still count, just at 1x weight.

TIER_1_FIRMS = {
    "goldman sachs", "morgan stanley", "jp morgan", "jpmorgan",
    "bank of america", "bofa", "merrill lynch",
    "barclays", "ubs", "deutsche bank", "citigroup", "citi",
    "wells fargo", "jefferies", "raymond james",
    "bernstein", "sanford bernstein",
}

TIER_2_FIRMS = {
    "piper sandler", "wedbush", "needham", "oppenheimer",
    "rbc capital", "rbc", "td cowen", "cowen",
    "stifel", "baird", "keybanc", "bmo capital", "bmo",
    "evercore", "wolfe research", "mizuho",
    "truist", "canaccord", "loop capital", "rosenblatt",
    "susquehanna", "tigress", "argus", "cfra",
    "morningstar", "da davidson",
}

# ── Rating action patterns ──────────────────────────────────────────────────

@dataclass
class AnalystAction:
    firm: str
    action: str       # "upgrade", "downgrade", "pt_raise", "pt_cut", "initiate_buy", "initiate_sell", "reiterate_buy", "reiterate_sell"
    tier: int          # 1, 2, or 3 (unknown)
    weight: int        # signed: +3 for tier1 upgrade, -3 for tier1 downgrade, etc.
    headline: str


# Patterns to detect analyst actions in headlines
# These match common headline formats from Benzinga, MT Newswires, etc.
_UPGRADE_PATTERNS = [
    re.compile(r"(?P<firm>.+?)\s+upgrades?\s+\w+", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+raises?\s+(?:\w+\s+)?(?:to\s+)?(?:buy|outperform|overweight)", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+initiates?\s+(?:\w+\s+)?(?:with\s+)?(?:a\s+)?(?:buy|outperform|overweight)", re.IGNORECASE),
]

_DOWNGRADE_PATTERNS = [
    re.compile(r"(?P<firm>.+?)\s+downgrades?\s+\w+", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+cuts?\s+(?:\w+\s+)?(?:to\s+)?(?:sell|underperform|underweight)", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+initiates?\s+(?:\w+\s+)?(?:with\s+)?(?:a\s+)?(?:sell|underperform|underweight)", re.IGNORECASE),
]

_PT_RAISE_PATTERNS = [
    re.compile(r"(?P<firm>.+?)\s+raises?\s+(?:\w+\s+)?price\s+target", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+(?:boosts?|lifts?|hikes?)\s+(?:\w+\s+)?(?:price\s+)?target", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+maintains?\s+(?:buy|outperform).+raises?\s+(?:price\s+)?target", re.IGNORECASE),
]

_PT_CUT_PATTERNS = [
    re.compile(r"(?P<firm>.+?)\s+(?:cuts?|lowers?|slashes?|reduces?)\s+(?:\w+\s+)?price\s+target", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+(?:cuts?|lowers?|slashes?|reduces?)\s+(?:\w+\s+)?(?:price\s+)?target", re.IGNORECASE),
]

_REITERATE_BUY_PATTERNS = [
    re.compile(r"(?P<firm>.+?)\s+reiterates?\s+(?:\w+\s+)?(?:buy|outperform|overweight)", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+maintains?\s+(?:\w+\s+)?(?:buy|outperform|overweight)", re.IGNORECASE),
]

_REITERATE_SELL_PATTERNS = [
    re.compile(r"(?P<firm>.+?)\s+reiterates?\s+(?:\w+\s+)?(?:sell|underperform|underweight)", re.IGNORECASE),
    re.compile(r"(?P<firm>.+?)\s+maintains?\s+(?:\w+\s+)?(?:sell|underperform|underweight)", re.IGNORECASE),
]

# Price target amount extraction (optional — used for logging)
_PT_AMOUNT = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)")


def _identify_firm(raw: str) -> tuple[str, int]:
    """
    Clean up the firm name extracted by regex and return (firm, tier).
    The regex captures everything before 'upgrades/downgrades/...' which
    may include noise. We try to match known firms in the captured text.
    """
    text = raw.strip().lower()
    # Try tier 1 first
    for firm in TIER_1_FIRMS:
        if firm in text:
            return firm.title(), 1
    for firm in TIER_2_FIRMS:
        if firm in text:
            return firm.title(), 2
    # Unknown firm — still useful, just lower weight
    # Clean up: take last 2-3 words as probable firm name
    words = raw.strip().split()
    name = " ".join(words[-3:]) if len(words) > 3 else raw.strip()
    return name.strip(",.:;—- "), 3


def extract_analyst_actions(headlines: list[str]) -> list[AnalystAction]:
    """
    Parse a list of news headlines and extract any analyst rating actions.
    Returns list of AnalystAction, each with signed weight.

    Weight scale:
      Tier 1 upgrade/PT raise  : +3
      Tier 2 upgrade/PT raise  : +2
      Unknown upgrade/PT raise : +1
      Reiterate buy/sell       : ±1 (regardless of tier — not new info)
      Downgrades/PT cuts       : negative of above
    """
    actions = []

    for headline in headlines:
        action_type = None
        firm_raw    = None

        # Check upgrade patterns
        for pat in _UPGRADE_PATTERNS:
            m = pat.search(headline)
            if m:
                firm_raw    = m.group("firm")
                action_type = "upgrade"
                break

        # Check downgrade patterns (only if upgrade didn't match)
        if not action_type:
            for pat in _DOWNGRADE_PATTERNS:
                m = pat.search(headline)
                if m:
                    firm_raw    = m.group("firm")
                    action_type = "downgrade"
                    break

        # PT raise
        if not action_type:
            for pat in _PT_RAISE_PATTERNS:
                m = pat.search(headline)
                if m:
                    firm_raw    = m.group("firm")
                    action_type = "pt_raise"
                    break

        # PT cut
        if not action_type:
            for pat in _PT_CUT_PATTERNS:
                m = pat.search(headline)
                if m:
                    firm_raw    = m.group("firm")
                    action_type = "pt_cut"
                    break

        # Reiterate buy
        if not action_type:
            for pat in _REITERATE_BUY_PATTERNS:
                m = pat.search(headline)
                if m:
                    firm_raw    = m.group("firm")
                    action_type = "reiterate_buy"
                    break

        # Reiterate sell
        if not action_type:
            for pat in _REITERATE_SELL_PATTERNS:
                m = pat.search(headline)
                if m:
                    firm_raw    = m.group("firm")
                    action_type = "reiterate_sell"
                    break

        if not action_type or not firm_raw:
            continue

        firm, tier = _identify_firm(firm_raw)

        # Compute signed weight
        tier_weight = {1: 3, 2: 2, 3: 1}[tier]
        if action_type in ("reiterate_buy", "reiterate_sell"):
            # Reiterations are confirmations, not new info — fixed ±1
            weight = 1 if action_type == "reiterate_buy" else -1
        elif action_type in ("upgrade", "pt_raise"):
            weight = tier_weight
        elif action_type in ("downgrade", "pt_cut"):
            weight = -tier_weight
        else:
            weight = 0

        actions.append(AnalystAction(
            firm=firm, action=action_type, tier=tier,
            weight=weight, headline=headline,
        ))

    return actions


def analyst_score(headlines: list[str]) -> tuple[int, list[str]]:
    """
    Convenience function: extract analyst actions and return
    (total_score, list_of_summary_strings).

    Score is clamped to [-5, +5] to avoid a single stock with 10 analyst
    headlines dominating everything.
    """
    actions = extract_analyst_actions(headlines)
    if not actions:
        return 0, []

    total    = sum(a.weight for a in actions)
    clamped  = max(-5, min(5, total))
    summaries = []
    for a in actions:
        tier_label = {1: "Tier-1", 2: "Tier-2", 3: ""}[a.tier]
        sign       = "+" if a.weight > 0 else ""
        summaries.append(
            f"{a.firm} {a.action.replace('_', ' ')} "
            f"({sign}{a.weight}) {tier_label}".strip()
        )

    return clamped, summaries
