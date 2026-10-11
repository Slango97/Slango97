"""Portfolio risk: positions in correlated instruments are not independent bets. Risk is aggregated as
sqrt(s' C s) over signed per-position risk.

V5.8: C comes from the LIVE correlation matrix (correlation_matrix.py) when one is fresh, else from the factor
loadings in config (now covering the whole watchlist). Broker names are mapped to watchlist markets first
(XAUUSDs / GOLD -> XAUUSD). For each pair the more dangerous end of the measured range is used, and offsetting
(hedged) pairs only earn `hedge_credit` of their theoretical risk reduction."""
import math
import time

from correlation_matrix import market_key


def static_correlation(a, b, pc):
    """Factor-model correlation (cosine of loadings) on watchlist names; unknown_correlation when not modelled."""
    a, b = market_key(a), market_key(b)
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


def correlation_range(a, b, pc, matrix=None):
    if matrix is not None:
        r = matrix.range(a, b)
        if r is not None:
            return r
    c = static_correlation(a, b, pc)
    return c, c


def correlation(a, b, pc, matrix=None):
    """Point estimate (midpoint of the range) for reporting and backward compatibility."""
    lo, hi = correlation_range(a, b, pc, matrix)
    return (lo + hi) / 2


def portfolio_risk_pct(exposures, pc, matrix=None, hedge_credit=None):
    """exposures: list of (symbol, signed_risk_pct). Long positive, short negative. Conservative aggregation."""
    credit = pc.corr_hedge_credit if hedge_credit is None else hedge_credit
    tot = 0.0
    for i, (sa, ra) in enumerate(exposures):
        tot += ra * ra
        for sb, rb in exposures[i + 1:]:
            lo, hi = correlation_range(sa, sb, pc, matrix)
            rho = hi if ra * rb > 0 else lo               # the end of the range that adds the most risk
            term = 2 * ra * rb * rho
            tot += term * credit if term < 0 else term     # hedges only partly trusted
    return math.sqrt(max(tot, 0.0))


def factor_exposure(exposures, pc):
    out = {}
    for sym, r in exposures:
        for f, w in pc.loadings.get(market_key(sym), {}).items():
            out[f] = out.get(f, 0.0) + r * w
    return {k: round(v, 3) for k, v in out.items()}


class PortfolioRisk:
    def __init__(self, cfg, client):
        self.cfg, self.pc, self.c = cfg, cfg.portfolio, client
        self._matrix, self._matrix_at, self._refresh_at = None, 0.0, 0.0

    # ---- live correlation matrix -------------------------------------------------
    def matrix(self):
        """Fresh live matrix (own refresh or one written by another market instance), else None -> factor prior."""
        if not self.pc.live_correlation:
            return None
        from correlation_matrix import CorrelationMatrix
        if self._matrix is None or time.time() - self._matrix_at > 300:
            m = CorrelationMatrix.load(self.pc.corr_file)
            if m is not None:
                self._matrix, self._matrix_at = m, time.time()
        if self._matrix is not None and self._matrix.age_h() <= self.pc.corr_max_age_h:
            return self._matrix
        return None

    def refresh_matrix_if_due(self, markets, resolve=None, force=False):
        """Recompute from the broker's closed bars every corr_refresh_min. Never raises; returns the matrix or None."""
        if not self.pc.live_correlation or not hasattr(self.c, "rates_for"):
            return None
        if not force and time.time() - self._refresh_at < 60 * self.pc.corr_refresh_min:
            return None
        self._refresh_at = time.time()
        from correlation_matrix import CorrelationMatrix, compute, fetch_closes
        try:
            closes = fetch_closes(self.c, markets, self.pc.corr_timeframe, self.pc.corr_lookback_bars, resolve)
            if len(closes) < 2:
                return None
            m = CorrelationMatrix(compute(closes, self.pc, lambda a, b: static_correlation(a, b, self.pc)))
            m.save(self.pc.corr_file)
            self._matrix, self._matrix_at = m, time.time()
            return m
        except Exception:
            return None

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
        m = self.matrix()
        before = portfolio_risk_pct(cur, self.pc, m)
        after = portfolio_risk_pct(cur + [(symbol, d * new_risk_pct)], self.pc, m)
        ok = after <= self.pc.max_correlated_exposure_pct + 1e-9 or after <= before
        return ok, round(before, 3), round(after, 3), factor_exposure(cur + [(symbol, d * new_risk_pct)], self.pc)
