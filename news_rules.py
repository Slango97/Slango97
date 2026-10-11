"""Deterministic headline reasoning for gold (no LLM needed). Ported ideas from the News Brain hotfix:
one vote per causal family, headline-only confidence capped. Fixed weaknesses measured in that module:
negations ("not expected to hike"), reversals ("slowed less than expected"), mixed headlines (split into clauses)
and price echoes ("gold surged" is a price report, not a driver).

Polarity is for GOLD: +1 bullish, -1 bearish. Families: inflation, labor, fed_policy, yields, usd, geopolitics, cb_gold.
Headline-only evidence can only ADD CAUTION in decisions (it never adds a bonus and can never veto by itself)."""
import re
from dataclasses import dataclass, field

HEADLINE_CONF_CAP = 0.45
NEGATORS = ("not", "no", "unlikely", "won't", "wont", "never", "without", "rules out", "ruled out", "fails to", "failed to",
            "doesn't", "does not", "didn't", "did not", "isn't", "is not")
REVERSERS = ("less than expected", "less than forecast", "less than anticipated", "less than feared")
SPLIT = re.compile(r"\s*(?:;|,|\bbut\b|\bwhile\b|\bas\b|\bafter\b|\bamid\b|\bdespite\b|\beven as\b|\band\b|\bwhereas\b)\s*")

# family -> (topic words, [(signal words, polarity_for_gold)])
RULES = {
    "inflation": (("cpi", "inflation", "pce", "ppi", "price index", "consumer prices", "producer prices"),
                  [(("hotter", "higher than expected", "above expectations", "above forecast", "accelerat", "rose more than expected",
                     "sticky", "picked up", "jumped", "surged", "rose", "climbed", "heats up"), -1),
                   (("cooler", "lower than expected", "below expectations", "below forecast", "slowed", "eased", "cooled",
                     "fell more than expected", "decelerat", "moderated", "fell", "dropped", "declined"), +1)]),
    "labor": (("payroll", "jobs", "nfp", "employment", "labor market", "labour market", "hiring", "wages"),
              [(("stronger than expected", "beat", "beats", "added more", "robust", "strong", "surged", "jumped", "rose"), -1),
               (("weaker than expected", "missed", "misses", "weak", "slowed", "fell", "declined", "cooling", "shed"), +1)]),
    "jobless": (("jobless claims", "unemployment claims", "unemployment rate", "unemployment"),
                [(("rose", "rises", "jumped", "climbed", "higher", "increased", "spiked"), +1),
                 (("fell", "falls", "dropped", "lower", "declined", "decreased"), -1)]),
    "fed_policy": (("fed", "fomc", "powell", "central bank", "rate", "policy makers", "policymakers"),
                   [(("hike", "hikes", "hawkish", "higher for longer", "restrictive", "tighten", "tightening", "raise rates"), -1),
                    (("cut", "cuts", "dovish", "easing", "ease policy", "lower rates", "pivot"), +1)]),
    "yields": (("yield", "treasur", "bond"),
               [(("rise", "rises", "rose", "climb", "jump", "surge", "higher", "spike", "soar"), -1),
                (("fall", "falls", "fell", "drop", "slide", "lower", "ease", "decline", "retreat"), +1)]),
    "usd": (("dollar", "usd", "dxy", "greenback"),
            [(("strengthen", "rises", "rose", "climbs", "jumps", "rallies", "firmer", "gains", "surges"), -1),
             (("weaken", "falls", "fell", "slides", "slumps", "drops", "softer", "loses", "retreats"), +1)]),
    "geopolitics": (("war", "attack", "missile", "strike", "invasion", "conflict", "tension", "escalat", "ceasefire", "truce",
                     "sanction", "military"),
                    [(("escalat", "attack", "missile", "invasion", "war", "strike", "tensions rise", "conflict widens"), +1),
                     (("ceasefire", "truce", "peace deal", "de-escalat", "tensions ease", "talks progress"), -1)]),
    "cb_gold": (("central bank gold", "gold reserves", "gold purchases", "gold buying", "bought gold", "gold holdings"),
                [(("buy", "bought", "purchases", "added", "increase", "record"), +1), (("sold", "selling", "reduce", "cut"), -1)]),
}
# Non-US central banks: polarity +1 = hawkish = that currency strengthens (mapped to the instrument later)
CENTRAL_BANKS = {"EUR": ("ecb", "lagarde", "european central bank"), "GBP": ("bank of england", "boe ", "bailey"),
                 "JPY": ("bank of japan", "boj", "ueda"), "AUD": ("reserve bank of australia", "rba"),
                 "CAD": ("bank of canada", "boc ", "macklem"), "CHF": ("swiss national bank", "snb"),
                 "NZD": ("reserve bank of new zealand", "rbnz")}
CB_SIGNALS = [(("hike", "hikes", "raises rates", "raise rates", "hawkish", "tighten", "higher for longer"), +1),
              (("cut", "cuts", "lowers rates", "dovish", "easing", "lower rates"), -1)]
US_FAMILIES = ("inflation", "labor", "jobless", "fed_policy", "usd")
for _ccy, _topics in CENTRAL_BANKS.items():
    RULES["cb_" + _ccy] = (_topics, CB_SIGNALS)
PRICE_ECHO = re.compile(r"\b(gold|xau|bullion)\b.*\b(rise|rises|rose|fall|falls|fell|surge|surged|jump|jumped|slump|slumped|"
                        r"climb|climbed|drop|dropped|record|hits|gains|loses|rallies|tumbles)\b")


def _negated(clause, pos):
    before = clause[max(0, pos - 30):pos]
    return any(re.search(r"(^|\W)" + re.escape(n) + r"(\W|$)", before) for n in NEGATORS)


def clause_votes(clause):
    """[(family, polarity)] for one clause."""
    c = " " + clause.lower().strip() + " "
    votes = []
    foreign_cb = any(t in c for f, (tp, _) in RULES.items() if f.startswith("cb_") and f != "cb_gold" for t in tp)
    for fam, (topics, signals) in RULES.items():
        if not any(t in c for t in topics):
            continue
        if fam == "fed_policy" and foreign_cb and not any(k in c for k in ("fed", "fomc", "powell")):
            continue                                   # "ECB raises rates" is not a Fed vote
        best = None
        for words, pol in signals:
            for w in words:
                k = c.find(w)
                if k >= 0 and (best is None or k < best[0]):
                    p = pol
                    if _negated(c, k):
                        p = -p
                    if any(r in c[k:k + len(w) + 40] for r in REVERSERS):
                        p = -p
                    best = (k, p)
        if best is not None:
            votes.append((fam, best[1]))
    if fam_dup := {f for f, _ in votes if f == "labor"} & {f for f, _ in votes if f == "jobless"}:
        votes = [v for v in votes if v[0] != "labor"] if fam_dup else votes
    return votes


def headline_votes(title):
    votes = []
    for clause in [x for x in SPLIT.split(title) if x and x.strip()]:
        v = clause_votes(clause)
        if not v and PRICE_ECHO.search(clause.lower()):
            continue                                   # pure price report: not causal evidence
        votes += v
    return votes


@dataclass
class RulesEvidence:
    bias: float = 0.0
    confidence: float = 0.0
    families: dict = field(default_factory=dict)       # family -> vote (+1 / -1)
    conflicts: list = field(default_factory=list)
    used: int = 0


def instrument_polarity(family, raw, profile):
    """Convert a family vote (gold polarity for US families, currency polarity for central banks) to the instrument."""
    if profile is None:
        return raw
    if family in US_FAMILIES:
        return (-raw) * profile.usd_beta
    if family == "yields":
        return (-raw) * profile.yields_beta
    if family == "geopolitics":
        return raw * (-profile.risk_beta)
    if family == "cb_gold":
        return raw * profile.gold_beta
    if family.startswith("cb_"):
        return raw * profile.currency_beta(family[3:])
    return raw


def assess_headlines(headlines, profile=None):
    """headlines: objects with .title and optional .weight (reliability x recency). One vote per family.
    profile None = gold polarity (V4.3.1 behaviour); otherwise votes are signed for the traded instrument."""
    acc = {}
    used = 0
    for h in headlines:
        w = float(getattr(h, "weight", 1.0) or 0.0)
        v = headline_votes(getattr(h, "title", str(h)))
        if v:
            used += 1
        for fam, pol in v:
            s, ww = acc.get(fam, (0.0, 0.0))
            acc[fam] = (s + pol * w, max(ww, w))
    acc = {f: (instrument_polarity(f, sv, profile), w) for f, (sv, w) in acc.items()}
    fam_votes = {f: (1 if s > 0 else -1) for f, (s, _) in acc.items() if abs(s) > 1e-9}
    if not fam_votes:
        return RulesEvidence(used=used)
    weights = {f: acc[f][1] for f in fam_votes}
    tot = sum(weights.values()) or 1.0
    bias = sum(fam_votes[f] * weights[f] for f in fam_votes) / tot
    bull = [f for f, v in fam_votes.items() if v > 0]
    bear = [f for f, v in fam_votes.items() if v < 0]
    conflicts = [f"{b} bullish vs {r} bearish" for b in bull for r in bear][:6]
    agree = abs(bias)
    conf = min(HEADLINE_CONF_CAP, HEADLINE_CONF_CAP * agree * min(1.0, len(fam_votes) / 3) + 0.05 * (len(fam_votes) > 0))
    return RulesEvidence(round(bias, 3), round(conf, 3), fam_votes, conflicts, used)
