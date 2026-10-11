"""Portfolio risk: positions in correlated instruments are not independent bets. Correlations come from factor
loadings (USD, GOLD, EUR...) in config; risk is aggregated as sqrt(s' C s) over signed per-position risk."""
import math


def correlation(a, b, pc):
    if a == b:
        return 1.0
    la, lb = pc.loadings.get(a), pc.loadings.get(b)
    if not la or not lb:
        return pc.unknown_correlation
    keys = set(la) | set(lb)
    dot = sum(la.get(k, 0.0) * lb.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in la.values()))
    nb = math.sqrt(sum(v * v for v in lb.values()))
    return dot / (na * nb) if na and nb else pc.unknown_correlation


def portfolio_risk_pct(exposures, pc):
    """exposures: list of (symbol, signed_risk_pct). Long positive, short negative."""
    tot = 0.0
    for sa, ra in exposures:
        for sb, rb in exposures:
            tot += ra * rb * correlation(sa, sb, pc)
    return math.sqrt(max(tot, 0.0))


def factor_exposure(exposures, pc):
    out = {}
    for sym, r in exposures:
        for f, w in pc.loadings.get(sym, {}).items():
            out[f] = out.get(f, 0.0) + r * w
    return {k: round(v, 3) for k, v in out.items()}


class PortfolioRisk:
    def __init__(self, cfg, client):
        self.pc, self.c = cfg.portfolio, client

    def exposures(self, positions):
        acc = self.c.account()
        eq = float(getattr(acc, "equity", 0.0) or 0.0)
        if not math.isfinite(eq) or eq <= 0:
            raise ValueError("portfolio risk unavailable: invalid account equity")
        out = []
        for p in positions:
            d = 1 if p.type == 0 else -1
            symbol = getattr(p, "symbol", self.c.symbol)
            try:
                entry, sl, volume = float(p.price_open), float(p.sl or 0.0), float(p.volume)
            except (TypeError, ValueError) as ex:
                raise ValueError(f"portfolio risk unavailable: invalid position data for {symbol}") from ex
            if not all(math.isfinite(x) for x in (entry, sl, volume)) or entry <= 0 or volume <= 0:
                raise ValueError(f"portfolio risk unavailable: invalid entry/SL/volume for {symbol}")
            if sl <= 0 or (entry - sl) * d <= 0:
                raise ValueError(f"portfolio risk unavailable: unprotected/invalid stop for {symbol}")
            loss_per_lot = float(self.c.calc_loss_per_lot_for(symbol, d, entry, sl))
            if not math.isfinite(loss_per_lot) or loss_per_lot <= 0:
                raise ValueError(f"portfolio risk unavailable: invalid loss calculation for {symbol}")
            loss = volume * loss_per_lot
            out.append((symbol, d * 100 * loss / eq))
        return out

    def check(self, symbol, d, new_risk_pct, positions):
        cur = self.exposures(positions)
        before = portfolio_risk_pct(cur, self.pc)
        after = portfolio_risk_pct(cur + [(symbol, d * new_risk_pct)], self.pc)
        ok = after <= self.pc.max_correlated_exposure_pct + 1e-9 or after <= before
        return ok, round(before, 3), round(after, 3), factor_exposure(cur + [(symbol, d * new_risk_pct)], self.pc)
