"""Not all sources are equal. Each headline gets a reliability score; high-impact claims (war, emergency rate
moves, defaults...) count as confirmed only when a top-tier source reports them or two independent reliable
sources agree. A trade veto must rest on reliable evidence."""
import math
import re
from dataclasses import dataclass

RELIABILITY = {
    "federalreserve.gov": 1.0, "bls.gov": 1.0, "bea.gov": 1.0, "treasury.gov": 1.0, "ecb.europa.eu": 1.0,
    "reuters.com": 0.95, "bloomberg.com": 0.95, "apnews.com": 0.9, "wsj.com": 0.9, "ft.com": 0.9,
    "cnbc.com": 0.85, "bbc.co.uk": 0.85, "bbc.com": 0.85, "bbci.co.uk": 0.85, "marketwatch.com": 0.75, "nytimes.com": 0.85,
    "fxstreet.com": 0.65, "kitco.com": 0.65, "investing.com": 0.6, "forexfactory.com": 0.6, "faireconomy.media": 0.6,
    "finance.yahoo.com": 0.6, "yahoo.com": 0.6, "seekingalpha.com": 0.45,
    "x.com": 0.2, "twitter.com": 0.2, "reddit.com": 0.15, "t.me": 0.1,
}
DEFAULT_RELIABILITY = 0.3
HIGH_IMPACT = ("war", "attack", "strike", "invasion", "nuclear", "emergency", "default", "sanction", "assassinat",
               "coup", "missile", "ceasefire", "intervention", "collapse", "bank run", "martial law", "embargo")
ENTITIES = ("iran", "israel", "russia", "ukraine", "china", "taiwan", "fed", "ecb", "opec", "us ", "u.s.", "nato",
            "north korea", "saudi", "gaza", "treasury")


def domain_of(src):
    s = str(src).lower().strip()
    s = re.sub(r"^https?://", "", s).split("/")[0].replace("www.", "").replace("feeds.", "")
    return s


def reliability(src):
    d = domain_of(src)
    if d in RELIABILITY:
        return RELIABILITY[d]
    for k, v in RELIABILITY.items():
        if d.endswith("." + k):
            return v
    return DEFAULT_RELIABILITY


@dataclass
class Headline:
    title: str
    source: str
    age_min: float
    reliability: float
    relevance: int
    high_impact: bool
    claim: frozenset
    confirmed: bool = False

    @property
    def weight(self):
        return self.reliability * math.exp(-self.age_min / 180) * min(1.0, 0.4 + 0.2 * self.relevance)


def assess(items):
    """items: iterable of (age_min, source, relevance_hits, title). Returns list[Headline] (newest first)."""
    hs = []
    for age, src, rel, title in items:
        low = f" {title.lower()} "
        hi = [w for w in HIGH_IMPACT if w in low]
        ents = [e.strip() for e in ENTITIES if e in low]
        hs.append(Headline(title, domain_of(src), float(age), reliability(src), int(rel), bool(hi), frozenset(hi + ents)))
    for h in hs:
        if not h.high_impact:
            h.confirmed = True
            continue
        if h.reliability >= 0.9:
            h.confirmed = True
            continue
        backers = {o.source for o in hs if o.reliability >= 0.6 and len(o.claim & h.claim) >= 2}
        h.confirmed = len(backers) >= 2
    return sorted(hs, key=lambda h: h.age_min)


def enforce(intel, headlines, ic):
    """Downgrade vetoes / extreme risk that are not backed by reliable evidence. Returns (intel, notes)."""
    notes = []
    if not ic.veto_requires_reliable_source:
        return intel, notes
    reliable = any(reliability(d.get("source", "")) >= ic.min_veto_reliability for d in intel.get("key_drivers", []))
    reliable = reliable or any(h.high_impact and h.confirmed for h in headlines)
    if intel.get("trade_veto") and not reliable:
        intel["trade_veto"] = False
        notes.append("veto dropped: not backed by a reliable or confirmed source")
    if intel.get("risk_level") == "extreme" and not reliable:
        intel["risk_level"] = "high"
        notes.append("extreme risk downgraded: unconfirmed")
    if notes:
        intel["contradictions"] = list(intel.get("contradictions", [])) + notes
    return intel, notes
