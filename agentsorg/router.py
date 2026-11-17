"""Model router. Rule-based, zero LLM calls. Cheapest tier that fits; opus only on flag/escalation."""
from __future__ import annotations

import re

T1_WORDS = r"\b(summari[sz]e|classify|tag|categori[sz]e|rewrite|shorten|proofread|translate|headline|tagline|" \
           r"subject line|tweet|caption|reply|respond|faq|list|extract|format|rename|status|bullet|slogan|title)\b"
T3_WORDS = r"\b(architect(ure)?|system design|security audit|threat model|novel|deep[- ]dive|" \
           r"complex|refactor (the )?(whole|entire)|migration plan|due diligence|financial model)\b"
FORCE = re.compile(r"(?:^|\s)!(haiku|sonnet|opus|t[123])\b", re.I)
FORCE_MAP = {"haiku": 1, "t1": 1, "sonnet": 2, "t2": 2, "opus": 3, "t3": 3}


def forced_tier(text: str) -> tuple[int | None, str]:
    """`!haiku` / `!sonnet` / `!opus` in a command forces a tier. Returns (tier, cleaned_text)."""
    m = FORCE.search(text)
    if not m:
        return None, text
    return FORCE_MAP[m.group(1).lower()], (text[:m.start()] + text[m.end():]).strip()


def clamp_tier(tier) -> int | None:
    """A tier from model output: '2', 2.0, 'sonnet' or junk -> 1..3 or None."""
    if isinstance(tier, str):
        tier = FORCE_MAP.get(tier.strip().lower(), tier.strip())
    try:
        t = int(tier)
    except (TypeError, ValueError):
        return None
    return max(1, min(t, 3))


def pick_tier(text: str, member_tier: int, max_tier: int, hint: int | None = None) -> int:
    """hint = tier suggested by the CEO router (already a Haiku judgement, free here)."""
    t = text.lower()
    tier = member_tier
    if hint := clamp_tier(hint):
        tier = hint
    if re.search(T1_WORDS, t) and len(t) < 600 and not re.search(T3_WORDS, t):
        tier = min(tier, 1)
    if re.search(T3_WORDS, t):
        tier = max(tier, 2) + 1
    return max(1, min(tier, max_tier, 3))


def escalate(tier: int, max_tier: int) -> int | None:
    nxt = tier + 1
    return nxt if nxt <= min(max_tier + 1, 3) else None   # one step above member cap allowed on escalation
