"""Instrument profiles: which currencies' news matters for a market and how it reacts to the macro anchors the
trading documents name (DXY, US10Y, risk-on / risk-off). Used by the calendar filter, the actual-vs-forecast
direction, headline reasoning and the intermarket feeds, so the same kernel analyses gold, FX, indices, oil and
crypto correctly. Betas are qualitative directions (+1 / -1 / fractions), not fitted coefficients."""
import re
from dataclasses import asdict, dataclass

CCY = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNH", "CNY", "SEK", "NOK", "MXN", "ZAR", "TRY", "SGD", "HKD", "PLN")
RISK = {"AUD": 1.0, "NZD": 1.0, "MXN": 0.8, "ZAR": 0.9, "CAD": 0.6, "NOK": 0.5, "SEK": 0.4, "GBP": 0.3, "CNH": 0.3, "CNY": 0.3,
        "EUR": 0.2, "PLN": 0.4, "TRY": 0.6, "SGD": 0.2, "HKD": 0.0, "USD": -0.4, "CHF": -0.8, "JPY": -1.0}
INDEX = {"US30": "USD", "DJ30": "USD", "WS30": "USD", "DOW": "USD", "US500": "USD", "SPX500": "USD", "SP500": "USD", "SPX": "USD",
         "NAS100": "USD", "USTEC": "USD", "US100": "USD", "NDX": "USD", "GER40": "EUR", "GER30": "EUR", "DE40": "EUR", "DE30": "EUR",
         "DAX": "EUR", "FRA40": "EUR", "EU50": "EUR", "STOXX50": "EUR", "ESP35": "EUR", "UK100": "GBP", "FTSE": "GBP",
         "JP225": "JPY", "JPN225": "JPY", "NIKKEI": "JPY", "AUS200": "AUD", "HK50": "HKD"}
ENERGY = ("USOIL", "UKOIL", "WTI", "BRENT", "XTI", "XBR", "CRUDE", "OIL", "NGAS", "XNG")
CRYPTO = ("BTC", "ETH", "XRP", "SOL", "LTC", "ADA", "DOGE", "BNB", "DOT", "AVAX")
INFLATION_POLICY = ("inflation", "policy")


@dataclass(frozen=True)
class InstrumentProfile:
    symbol: str
    asset_class: str                 # metal | fx | index | energy | crypto | other
    base: str
    quote: str
    calendar_currencies: tuple
    usd_beta: float                  # move when the US dollar strengthens
    yields_beta: float               # move when US yields rise
    risk_beta: float                 # move when risk appetite rises (risk-on)
    gold_beta: float = 0.0           # gold-specific drivers (central-bank gold buying)
    trades_weekends: bool = False

    def currency_beta(self, ccy):
        """Move of this instrument when `ccy` strengthens."""
        if self.asset_class == "fx":
            return 1.0 if ccy == self.base else -1.0 if ccy == self.quote else 0.0
        if self.asset_class == "metal":
            return self.usd_beta if ccy == "USD" == self.quote else (-1.0 if ccy == self.quote else 0.0)
        if self.asset_class == "index" and ccy == self.quote:
            return -0.5                          # hawkish / stronger home currency weighs on its equity index
        return self.usd_beta if ccy == "USD" else 0.0

    def to_dict(self):
        return asdict(self)


def instrument_effect(p, ccy, category):
    """Direction of the instrument when an economic release is GOOD for currency `ccy`.
    category: inflation | policy | growth (hawkish/inflationary news hurts equities and crypto; growth helps)."""
    if p.asset_class in ("fx", "metal", "other"):
        return p.currency_beta(ccy)
    home = p.quote if p.asset_class == "index" else "USD"
    if ccy != home and not (p.asset_class == "index" and ccy == "USD"):
        return 0.0
    scale = 1.0 if ccy == home else 0.5
    if p.asset_class == "index":
        return scale * (-1.0 if category in INFLATION_POLICY else 0.5)
    if p.asset_class == "crypto":
        return -0.8 if category in INFLATION_POLICY else 0.3
    if p.asset_class == "energy":
        return -0.4 if category in INFLATION_POLICY else 0.5
    return 0.0


def _clip(x):
    return max(-1.0, min(1.0, x))


def profile_for(symbol, info=None):
    raw = str(symbol or "")
    s = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if s.startswith(("XAU", "GOLD")):
        q = "USD" if "USD" in s or s in ("GOLD",) else s[3:6]
        return InstrumentProfile(raw, "metal", "XAU", q, (q, "All"), -1.0, -1.0, -1.0, 1.0)
    if s.startswith(("XAG", "SILVER")):
        q = "USD" if "USD" in s or s == "SILVER" else s[3:6]
        return InstrumentProfile(raw, "metal", "XAG", q, (q, "All"), -1.0, -0.8, -0.2, 0.5)
    if any(s.startswith(c) for c in CRYPTO):
        return InstrumentProfile(raw, "crypto", s[:3], "USD", ("USD", "All"), -0.5, -0.7, 1.0, 0.0, True)
    if any(k in s for k in ENERGY):
        return InstrumentProfile(raw, "energy", "OIL", "USD", ("USD", "CAD", "All"), -0.4, 0.0, 0.6)
    for k in sorted(INDEX, key=len, reverse=True):
        if s.startswith(k):
            home = INDEX[k]
            cur = (home, "All") if home == "USD" else (home, "USD", "All")
            return InstrumentProfile(raw, "index", k, home, cur, -0.3, -1.0, 1.0)
    b, q = s[:3], s[3:6]
    if b not in CCY or q not in CCY:
        b = str(getattr(info, "currency_base", "") or "").upper()
        q = str(getattr(info, "currency_profit", "") or "").upper()
    if b in CCY and q in CCY:
        usd = 1.0 if b == "USD" else -1.0 if q == "USD" else 0.0
        yl = (1.0 if q == "JPY" else 0.8) if b == "USD" else -0.6 if q == "USD" else 0.0
        return InstrumentProfile(raw, "fx", b, q, (b, q, "All"), usd, yl, _clip(RISK.get(b, 0.0) - RISK.get(q, 0.0)))
    return InstrumentProfile(raw, "other", b or s[:3], q or "USD", ("USD", "All"), 0.0, 0.0, 0.0)


def affected_markets(currency, category, symbols):
    """Markets in `symbols` a release for `currency` moves, with the direction when the data is GOOD for that
    currency (+1 instrument up / -1 down), strongest first. Drives the 'affected markets' line of news alerts."""
    out = []
    for s in symbols or ():
        try:
            eff = instrument_effect(profile_for(s), currency, category)
        except Exception:
            continue
        if eff:
            out.append((s, eff))
    return sorted(out, key=lambda x: -abs(x[1]))
