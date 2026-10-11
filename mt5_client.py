import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd

import broker_diag
from indicators import DataError, validate_ohlc
from symbol_resolver import discover_gold_symbol


class BrokerDataError(RuntimeError):
    pass

log = logging.getLogger("mt5")

TF = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
      "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1}

RC = lambda name, default: getattr(mt5, name, default)
RC_DONE, RC_PARTIAL, RC_NOCHANGE = RC("TRADE_RETCODE_DONE", 10009), RC("TRADE_RETCODE_DONE_PARTIAL", 10010), RC("TRADE_RETCODE_NO_CHANGES", 10025)
RC_TIMEOUT = RC("TRADE_RETCODE_TIMEOUT", 10012)
RETRYABLE = {RC("TRADE_RETCODE_REQUOTE", 10004), RC("TRADE_RETCODE_PRICE_CHANGED", 10020),
             RC("TRADE_RETCODE_PRICE_OFF", 10021), RC("TRADE_RETCODE_TOO_MANY_REQUESTS", 10024)}
DEAL_REASON = {0: "client", 1: "mobile", 2: "web", 3: "expert", 4: "sl", 5: "tp", 6: "stopout"}


@dataclass
class ExecResult:
    ok: bool
    ticket: int = 0
    requested_price: float = 0.0
    fill_price: float = 0.0
    volume: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    slippage: float = 0.0          # adverse slippage in price units (positive = worse than requested)
    retcode: int = 0
    comment: str = ""
    attempts: int = 0
    ambiguous: bool = False        # outcome unknown: the order may or may not have executed
    latency_ms: float = 0.0
    position: object = None
    warnings: list = field(default_factory=list)


class MT5Client:
    def __init__(self, cfg):
        self.cfg = cfg
        self.symbol = cfg.system.symbol
        self.symbol_auto_detected = False
        self.symbol_resolution = None
        self.account_identity = None
        self.offset_h = cfg.system.server_utc_offset_h or 0.0
        self._offset_at = 0.0
        self.read_only = cfg.dry_run
        self.diag = None
        self.tick_sizing_trusted = not cfg.features.broker_fail_closed
        self.pulls = 0                      # MT5 data pulls (runtime metric)
        from collections import deque
        self._drift = deque(maxlen=31)
        self._last_tick_ms = None

    # ---- connection -------------------------------------------------
    def _init_kwargs(self):
        kw = {}
        if os.getenv("MT5_PATH"):
            kw["path"] = os.getenv("MT5_PATH")
        if os.getenv("MT5_LOGIN"):
            kw.update(login=int(os.getenv("MT5_LOGIN")), password=os.getenv("MT5_PASSWORD", ""),
                      server=os.getenv("MT5_SERVER", ""))
        return kw

    def _resolve_symbol(self):
        previous = self.symbol if self.symbol_auto_detected else None
        pin = self.cfg.system.symbol if str(self.cfg.system.symbol or "").upper() != "AUTO" else None
        cand = discover_gold_symbol(mt5, preferred="XAUUSD", aliases=self.cfg.system.symbol_aliases, pin=pin)
        if cand is None:
            return False
        if previous and cand.name != previous:
            raise RuntimeError(f"auto-detected trading symbol changed from {previous} to {cand.name}; trading is fail-closed")
        self.symbol = cand.name
        self.symbol_auto_detected = (str(self.cfg.system.symbol or "").upper() == "AUTO" or cand.name != str(self.cfg.system.symbol or ""))
        self.symbol_resolution = {"method": "semantic_inventory" if cand.score >= 1100 else "alias_or_exact",
                                  "selected": cand.name, "score": cand.score, "reasons": cand.reasons}
        log.info("symbol resolved: %s score=%.0f auto=%s reasons=%s", cand.name, cand.score, self.symbol_auto_detected, ", ".join(cand.reasons))
        return True

    def _account_fingerprint(self):
        acc, term = mt5.account_info(), mt5.terminal_info()
        if acc is None or term is None:
            return None
        return (getattr(acc, "login", None), getattr(acc, "server", None), getattr(acc, "company", None),
                getattr(acc, "trade_mode", None), getattr(acc, "currency", None), getattr(acc, "leverage", None),
                getattr(acc, "margin_mode", None), getattr(term, "path", None))

    def _account_identity_current(self):
        if self.account_identity is None:
            return False
        return self._account_fingerprint() == self.account_identity

    def _capture_or_verify_account(self):
        fp = self._account_fingerprint()
        if fp is None:
            raise RuntimeError("MT5 account/terminal identity unavailable")
        if self.account_identity is None:
            self.account_identity = fp
            acc = mt5.account_info()
            log.info("account auto-detected: login=%s server=%s company=%s mode=%s currency=%s leverage=%s",
                     getattr(acc, "login", "?"), getattr(acc, "server", "?"), getattr(acc, "company", "?"),
                     self.account_mode(), getattr(acc, "currency", "?"), getattr(acc, "leverage", "?"))
            return True
        if fp != self.account_identity:
            raise RuntimeError("connected MT5 account identity changed; trading is fail-closed until restarted/verified")
        return True

    def connect(self):
        if not mt5.initialize(**self._init_kwargs()):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not self._capture_or_verify_account():
            raise RuntimeError("unable to identify the MT5 account")
        if not self._resolve_symbol():
            raise RuntimeError(f"No tradable gold symbol found; inspected configured aliases and MT5 symbol inventory")
        self._refresh_offset(force=True)
        acc = mt5.account_info()
        log.info("connected: account %s (%s) symbol %s autodetected=%s server-offset UTC%+.0f mode=%s", acc.login,
                 self.account_mode(), self.symbol, self.symbol_auto_detected, self.offset_h, self.cfg.system.mode)

    def shutdown(self):
        mt5.shutdown()

    def is_connected(self):
        ti = mt5.terminal_info()
        return bool(ti and getattr(ti, "connected", False))

    def healthy(self):
        # Connectivity health is deliberately separate from market-data freshness.
        # A connected terminal with stale ticks must degrade the trading engine, not trigger
        # an endless reconnect loop.
        return self.is_connected() and self._account_identity_current()

    def try_reconnect(self):
        """ONE reconnect attempt (the engine's ReconnectPolicy decides when). Never sleeps."""
        mt5.shutdown()
        ok = bool(mt5.initialize(**self._init_kwargs()) and self.is_connected())
        if not ok:
            return False
        try:
            self._capture_or_verify_account()
            return bool(self._resolve_symbol() and self.is_connected())
        except Exception as ex:
            log.critical("reconnect identity/symbol verification failed: %s", ex)
            return False

    def broker_diagnosis(self):
        self.diag = broker_diag.diagnose(mt5, self.symbol, self.cfg)
        self.tick_sizing_trusted = (not self.cfg.features.broker_fail_closed) or self.diag.tick_sizing_trusted
        return self.diag

    def switch_symbol(self, symbol):
        """Safely switch the active execution symbol for adaptive market selection.

        The caller must already have enforced the account/position safety policy. This method
        only selects a broker-visible symbol and resets symbol-local execution drift state.
        """
        symbol = str(symbol or "").strip()
        if not symbol:
            raise ValueError("empty market symbol")
        if not self.symbol_available(symbol):
            raise RuntimeError(f"market symbol unavailable: {symbol}")
        if symbol == self.symbol:
            return False
        old = self.symbol
        self.symbol = symbol
        self._drift.clear()
        self._last_tick_ms = None
        self.symbol_resolution = {"method": "adaptive_market_selection", "selected": symbol, "previous": old}
        log.info("adaptive market selection: %s -> %s", old, symbol)
        return True

    def symbol_available(self, s):
        return mt5.symbol_info(s) is not None and bool(mt5.symbol_select(s, True))

    def symbol_inventory(self):
        """Return the connected terminal's visible/inventory symbol names."""
        try:
            return [str(x.name) for x in (mt5.symbols_get() or ()) if getattr(x, "name", None)]
        except Exception:
            return []

    def resolve_market_symbol(self, requested, aliases=None):
        """Resolve a logical watchlist market to a broker-specific symbol without changing the primary pin."""
        req = str(requested or "").strip()
        aliases = tuple(dict.fromkeys([req] + list(aliases or ())))
        inv = self.symbol_inventory()
        if not inv:
            return req if self.symbol_available(req) else None
        upper = {name.upper(): name for name in inv}
        for alias in aliases:
            hit = upper.get(str(alias).upper())
            if hit and self.symbol_available(hit):
                return hit
        for alias in aliases:
            a = str(alias).upper()
            for name in inv:
                u = name.upper()
                if u.startswith(a) or u.endswith(a) or a in u:
                    try:
                        if self.symbol_available(name):
                            return name
                    except Exception:
                        continue
        return None

    def rates_for(self, symbol, tf, n):
        """Closed candles for an arbitrary watchlist symbol, using the same validated OHLC contract as primary data."""
        self._refresh_offset()
        r = mt5.copy_rates_from_pos(symbol, TF[tf], 0, n + 1)
        self.pulls += 1
        if r is None or len(r) < 3:
            raise DataError(f"no rates for {symbol} {tf}: {mt5.last_error()}")
        df = pd.DataFrame(r)
        df["time"] = pd.to_datetime(df["time"], unit="s") - pd.Timedelta(hours=self.offset_h)
        df = df.set_index("time").iloc[:-1][["open", "high", "low", "close", "tick_volume"]]
        return validate_ohlc(df, min_bars=min(60, n))

    def tick_for(self, symbol):
        return mt5.symbol_info_tick(symbol)

    def tick_age_for(self, symbol):
        t = mt5.symbol_info_tick(symbol)
        if t is None or not getattr(t, "time", 0):
            return 1e9
        return max(0.0, time.time() - (t.time - self.offset_h * 3600))

    def symbol_info_for(self, symbol):
        return mt5.symbol_info(symbol)

    def aux_rates(self, symbol, tf, n):
        r = mt5.copy_rates_from_pos(symbol, TF[tf], 0, n + 1)
        self.pulls += 1
        if r is None or len(r) < 30:
            raise DataError(f"no rates for {symbol}")
        df = pd.DataFrame(r)
        df["time"] = pd.to_datetime(df["time"], unit="s") - pd.Timedelta(hours=self.offset_h)
        return df.set_index("time").iloc[:-1][["close"]]

    def orders(self):
        os_ = mt5.orders_get(symbol=self.symbol) if hasattr(mt5, "orders_get") else ()
        return [o for o in (os_ or ()) if getattr(o, "magic", None) == self.cfg.system.magic]

    def ensure_connected(self, attempts=3):
        if self.cfg.features.reconnect_backoff:
            return self.healthy()           # reconnect policy owns retry timing; tick freshness is a separate safety gate
        if self.is_connected() and self._account_identity_current():
            return True
        for i in range(attempts):
            log.warning("MT5 connection lost, reconnect attempt %d/%d", i + 1, attempts)
            mt5.shutdown()
            time.sleep(min(2 ** i, 5))
            if mt5.initialize(**self._init_kwargs()) and self.is_connected():
                try:
                    self._capture_or_verify_account()
                    if self._resolve_symbol() and self.is_connected():
                        return True
                except Exception as ex:
                    log.critical("reconnect identity/symbol verification failed: %s", ex)
                    return False
        return False

    def account_mode(self):
        a = mt5.account_info()
        return {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(getattr(a, "trade_mode", -1), "UNKNOWN")

    def _refresh_offset(self, force=False):
        if self.cfg.system.server_utc_offset_h is not None:
            self.offset_h = float(self.cfg.system.server_utc_offset_h)
            return
        if not force and time.time() - self._offset_at < 3600:
            return
        t = mt5.symbol_info_tick(self.symbol)
        if t and t.time:
            delta = (t.time - time.time()) / 3600
            if abs(delta - round(delta)) < 0.2:       # a fresh tick gives a near-integer hour offset
                self.offset_h, self._offset_at = float(round(delta)), time.time()

    def validate_broker(self):
        """Startup checks. Returns (errors, warnings); any error means DO NOT TRADE."""
        err, warn = [], []
        info, acc, term = mt5.symbol_info(self.symbol), mt5.account_info(), mt5.terminal_info()
        if info is None or acc is None or term is None:
            return ["symbol, account or terminal info unavailable"], warn
        if getattr(term, "trade_allowed", True) is False:
            err.append("Algo Trading is disabled in the MT5 terminal")
        if getattr(acc, "trade_allowed", True) is False or getattr(acc, "trade_expert", True) is False:
            err.append("trading by expert advisors is disabled for this account")
        if info.trade_mode != RC("SYMBOL_TRADE_MODE_FULL", 4):
            err.append(f"symbol trade_mode is {info.trade_mode}, full trading required")
        if info.point <= 0 or info.trade_tick_size <= 0 or info.trade_tick_value <= 0:
            err.append("invalid point / tick size / tick value")
        if info.volume_min <= 0 or info.volume_step <= 0 or info.volume_max < info.volume_min:
            err.append("invalid volume limits")
        if not info.filling_mode:
            err.append("no supported order filling mode")
        if self.cfg.features.broker_fail_closed:
            dg = self.broker_diagnosis()
            err += [f"broker diagnostics: {e}" for e in dg.errors]
            warn += [f"broker diagnostics: {w}" for w in dg.warnings] + [f"broker diagnostics cause: {dg.cause or 'n/a'}"]
            return err, warn
        exp = self.cfg.system.expected_contract_size
        if abs(info.trade_contract_size - exp) > 1e-9:
            warn.append(f"contract size {info.trade_contract_size} differs from expected {exp}")
        if info.trade_stops_level * info.point > 10:
            err.append(f"abnormally large stops level ({info.trade_stops_level} pts)")
        if info.trade_freeze_level * info.point > 10:
            warn.append(f"large freeze level ({info.trade_freeze_level} pts)")
        t = self.tick()
        if t is None or t.bid <= 0 or t.ask <= 0:
            err.append("no valid tick")
        else:
            lpl = self.calc_loss_per_lot(1, t.ask, t.ask - 1.0)
            tick_based = 1.0 / info.trade_tick_size * info.trade_tick_value
            if lpl <= 0:
                err.append("order_calc_profit returned no usable value")
            elif abs(lpl - tick_based) / tick_based > 0.10:
                warn.append(f"order_calc_profit ({lpl:.2f}) differs from tick-value sizing ({tick_based:.2f})")
        warn.append(f"margin_mode={getattr(acc, 'margin_mode', '?')} stops_level={info.trade_stops_level} "
                    f"digits={info.digits} filling_mode={info.filling_mode}")
        return err, warn

    # ---- market data ------------------------------------------------
    def utc_now(self):
        return datetime.now(timezone.utc)

    def rates(self, tf, n):
        """Closed candles with a UTC index, validated. Raises DataError if unusable."""
        self._refresh_offset()
        r = mt5.copy_rates_from_pos(self.symbol, TF[tf], 0, n + 1)
        self.pulls += 1
        if r is None or len(r) < 3:
            raise DataError(f"no rates for {tf}: {mt5.last_error()}")
        df = pd.DataFrame(r)
        df["time"] = pd.to_datetime(df["time"], unit="s") - pd.Timedelta(hours=self.offset_h)
        df = df.set_index("time").iloc[:-1][["open", "high", "low", "close", "tick_volume"]]
        return validate_ohlc(df, min_bars=min(60, n))

    def last_bar_time(self, tf):
        """UTC timestamp of the most recent CLOSED candle (cheap new-bar detection)."""
        self._refresh_offset()
        r = mt5.copy_rates_from_pos(self.symbol, TF[tf], 0, 2)
        if r is None or len(r) < 2:
            return None
        return pd.Timestamp(int(r[0]["time"]), unit="s") - pd.Timedelta(hours=self.offset_h)

    def position_by_ticket(self, ticket):
        found = mt5.positions_get(ticket=ticket)
        return found[0] if found else None

    def tick(self):
        return mt5.symbol_info_tick(self.symbol)

    def tick_age_s(self):
        t = self.tick()
        if t is None or not t.time:
            return 1e9
        return max(0.0, time.time() - (t.time - self.offset_h * 3600))

    def symbol_info(self):
        return mt5.symbol_info(self.symbol)

    def account(self):
        return mt5.account_info()

    def spread_price(self):
        t = self.tick()
        return float(t.ask - t.bid)

    def spread_points(self):
        return self.spread_price() / self.symbol_info().point

    def min_stop_distance(self):
        info = self.symbol_info()
        return info.trade_stops_level * info.point + self.spread_price()

    def norm(self, price):
        return round(price, self.symbol_info().digits)

    # ---- broker-side calculations ----------------------------------
    def calc_loss_per_lot(self, direction, entry, sl):
        """Money lost per 1.0 lot if price goes from entry to sl, using MT5's own calculation."""
        typ = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL
        p = mt5.order_calc_profit(typ, self.symbol, 1.0, entry, sl)
        if p is not None and p < 0:
            return float(-p)
        if not self.tick_sizing_trusted:
            raise BrokerDataError("order_calc_profit unavailable and tick metadata is not trusted: refusing to size")
        info = self.symbol_info()
        return abs(entry - sl) / info.trade_tick_size * info.trade_tick_value

    def calc_loss_per_lot_for(self, symbol, direction, entry, sl):
        """Return positive broker-calculated loss per lot; fail closed on invalid data."""
        if direction not in (-1, 1):
            raise BrokerDataError(f"invalid trade direction for {symbol}: {direction}")
        try:
            entry, sl = float(entry), float(sl)
        except (TypeError, ValueError) as ex:
            raise BrokerDataError(f"invalid entry/SL for {symbol}") from ex
        if not math.isfinite(entry) or not math.isfinite(sl) or entry <= 0 or sl <= 0:
            raise BrokerDataError(f"non-finite or non-positive entry/SL for {symbol}")
        if (entry - sl) * direction <= 0:
            raise BrokerDataError(f"stop is not on the loss side for {symbol}")
        if symbol == self.symbol:
            loss = float(self.calc_loss_per_lot(direction, entry, sl))
        else:
            typ = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL
            p = mt5.order_calc_profit(typ, symbol, 1.0, entry, sl)
            if p is None or not math.isfinite(float(p)) or float(p) >= 0:
                raise BrokerDataError(f"broker could not calculate a valid loss for {symbol}")
            loss = -float(p)
        if not math.isfinite(loss) or loss <= 0:
            raise BrokerDataError(f"invalid loss-per-lot result for {symbol}")
        return loss

    def calc_margin_for(self, symbol, direction, lots, price):
        typ = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_margin(typ, symbol, lots, price)

    def calc_margin(self, direction, lots, price):
        typ = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_margin(typ, self.symbol, lots, price)

    # ---- positions --------------------------------------------------
    def positions(self):
        ps = mt5.positions_get(symbol=self.symbol) or ()
        return [p for p in ps if p.magic == self.cfg.system.magic]

    def all_positions(self):
        """Agent positions on every symbol (for portfolio-level risk)."""
        base = self.cfg.system.magic
        return [p for p in (mt5.positions_get() or ()) if p.magic == base]

    def excursions(self, d, entry, open_ts_utc, close_ts_utc):
        """Max favorable / adverse price excursion between two UTC epoch times, from 1-minute bars."""
        try:
            fr = pd.Timestamp(open_ts_utc + self.offset_h * 3600, unit="s").to_pydatetime()
            to = pd.Timestamp(close_ts_utc + self.offset_h * 3600, unit="s").to_pydatetime()
            r = mt5.copy_rates_range(self.symbol, mt5.TIMEFRAME_M1, fr, to)
        except Exception:
            return None
        if r is None or len(r) == 0:
            return None
        hi, lo = float(r["high"].max()), float(r["low"].min())
        return ((hi - entry) if d > 0 else (entry - lo)), ((entry - lo) if d > 0 else (hi - entry))

    def pos_age_hours(self, p):
        return (time.time() - (p.time - self.offset_h * 3600)) / 3600

    def deal_summary(self, position_id):
        """Realized result of a closed position from deal history."""
        deals = mt5.history_deals_get(position=position_id)
        if not deals:
            return None
        outs = [d for d in deals if d.entry in (RC("DEAL_ENTRY_OUT", 1), RC("DEAL_ENTRY_OUT_BY", 3), RC("DEAL_ENTRY_INOUT", 2))]
        if not outs:
            return None
        ins = [d for d in deals if d.entry == RC("DEAL_ENTRY_IN", 0)]
        vol = sum(d.volume for d in outs)
        last = max(outs, key=lambda d: d.time)
        return {"net": sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals),
                "exit_price": sum(d.price * d.volume for d in outs) / vol, "volume": vol,
                "entry_price": ins[0].price if ins else None,
                "exit_ts": last.time - self.offset_h * 3600, "reason": DEAL_REASON.get(last.reason, str(last.reason)),
                "commission": sum(d.commission for d in deals), "swap": sum(d.swap for d in deals)}

    # ---- execution --------------------------------------------------
    def _guard(self):
        if self.read_only:
            raise PermissionError("client is read-only (DRY_RUN): refusing to send orders")

    def _filling(self, pending=False):
        """Select a broker-allowed filling policy. Pending LIMIT orders may use RETURN only on
        execution modes that support it; otherwise prefer an explicitly allowed mode."""
        info = self.symbol_info()
        fm = int(getattr(info, "filling_mode", 0)) if info else 0
        # MQL5 docs: RETURN is always enabled except Market execution; pending limit/stop types use RETURN
        # only for Exchange/Market-compatible symbols. We still validate through order_check when enabled.
        if pending:
            return mt5.ORDER_FILLING_RETURN
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def observe_tick(self):
        """Sample clock drift ONLY from ticks seen arriving between two polls (so they are fresh). A quiet market is
        not clock drift: stale ticks have their own gate (max_tick_age_s)."""
        t = mt5.symbol_info_tick(self.symbol)
        if t is None:
            return
        ms = int(getattr(t, "time_msc", 0) or 0) or int(getattr(t, "time", 0) or 0) * 1000
        if not ms:
            return
        if self._last_tick_ms is not None and ms != self._last_tick_ms:
            self._drift.append(abs(time.time() - (ms / 1000.0 - self.offset_h * 3600.0)))
        self._last_tick_ms = ms

    def server_time_drift_s(self):
        """Median clock disagreement between broker server and this PC from fresh-tick samples; 0.0 while unknown
        (V4.3 measured the age of the last tick instead, which blocked trading in quiet markets)."""
        self.observe_tick()
        if not self._drift:
            return 0.0
        s = sorted(self._drift)
        return float(s[len(s) // 2])

    def _order_check(self, req):
        if not getattr(self.cfg.execution, "require_order_check", True):
            return True, "disabled"
        fn = getattr(mt5, "order_check", None)
        if fn is None:
            return False, "MetaTrader5.order_check unavailable"
        try:
            res = fn(req)
        except Exception as ex:
            return False, f"order_check exception: {ex}"
        if res is None:
            return False, f"order_check returned None: {mt5.last_error()}"
        rc = int(getattr(res, "retcode", -1))
        # MQL5 order_check is a preflight check; retcode 0 is the successful check result.
        # Any non-zero result fails closed rather than being mistaken for an executable order.
        if rc not in (0, int(RC_DONE)):                    # 0 ("Done") is the documented check success; DONE accepted too
            return False, f"order_check rejected: {rc} {getattr(res, 'comment', '')}"
        return True, "ok"

    def _find_new_position(self, d, comment, since_ts):
        want = mt5.POSITION_TYPE_BUY if d > 0 else mt5.POSITION_TYPE_SELL
        for p in sorted(self.positions(), key=lambda p: p.time, reverse=True):
            if p.type == want and p.comment == comment[:31] and p.time >= since_ts + self.offset_h * 3600 - 5:
                return p
        return None

    def _from_position(self, p, req_price, d, attempts, res=None):
        out = ExecResult(True, ticket=p.ticket, requested_price=req_price, fill_price=p.price_open, volume=p.volume,
                         sl=p.sl, tp=p.tp, slippage=(p.price_open - req_price) * d, attempts=attempts, position=p,
                         retcode=getattr(res, "retcode", 0) if res else 0)
        if not p.sl:
            out.warnings.append("position opened without a stop loss")
        return out

    def send_market(self, d, lots, sl, tp, comment):
        """Send a market order and verify the resulting position. Retries only on price-related
        rejections; an ambiguous outcome (timeout / no reply) is checked against open positions and
        is never blindly retried, so duplicate trades cannot be created."""
        self._guard()
        ex = self.cfg.execution
        comment = comment[:31]
        since, attempts = time.time(), 0
        while True:
            attempts += 1
            t = self.tick()
            if t is None or self.tick_age_s() > ex.max_tick_age_s:
                return ExecResult(False, comment="stale or missing tick", attempts=attempts)
            price = t.ask if d > 0 else t.bid
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol, "volume": lots,
                   "type": mt5.ORDER_TYPE_BUY if d > 0 else mt5.ORDER_TYPE_SELL, "price": price,
                   "sl": self.norm(sl), "tp": self.norm(tp), "deviation": ex.max_slippage_points,
                   "magic": self.cfg.system.magic, "comment": comment, "type_time": mt5.ORDER_TIME_GTC,
                   "type_filling": self._filling()}
            ok_check, check_msg = self._order_check(req)
            if not ok_check:
                return ExecResult(False, requested_price=price, attempts=attempts, retcode=0, comment=check_msg)
            t_send = time.perf_counter()
            res = mt5.order_send(req)
            latency = (time.perf_counter() - t_send) * 1000
            if res is None or res.retcode == RC_TIMEOUT:
                p = self._find_new_position(d, comment, since)
                if p:
                    out = self._from_position(p, price, d, attempts, res)
                    out.latency_ms = latency
                    return out
                return ExecResult(False, requested_price=price, attempts=attempts, ambiguous=True, latency_ms=latency,
                                  retcode=getattr(res, "retcode", 0), comment="no confirmation from broker")
            if res.retcode in (RC_DONE, RC_PARTIAL):
                pid, fill = None, price
                if getattr(res, "deal", 0):
                    deals = mt5.history_deals_get(ticket=res.deal)
                    if deals:
                        pid, fill = deals[0].position_id, deals[0].price
                found = (mt5.positions_get(ticket=pid) if pid else None) or \
                        (mt5.positions_get(ticket=res.order) if getattr(res, "order", 0) else None)
                p = found[0] if found else self._find_new_position(d, comment, since)
                if p is None:
                    return ExecResult(False, requested_price=price, attempts=attempts, ambiguous=True,
                                      retcode=res.retcode, comment="order reported done but no position found")
                out = self._from_position(p, price, d, attempts, res)
                out.latency_ms = latency
                if abs(p.volume - lots) > 1e-9:
                    out.warnings.append(f"volume {p.volume} differs from requested {lots}")
                return out
            if res.retcode in RETRYABLE and attempts <= ex.max_retries:
                log.warning("order retryable retcode %s (attempt %d)", res.retcode, attempts)
                time.sleep(ex.retry_delay_s)
                continue
            return ExecResult(False, requested_price=price, attempts=attempts, retcode=res.retcode, comment=res.comment)

    def send_limit(self, d, lots, price, sl, tp, comment, expiry_utc_ts):
        """Place a buy/sell LIMIT with broker-side SL/TP and expiry. Returns (ok, order_ticket, message)."""
        self._guard()
        req = {"action": mt5.TRADE_ACTION_PENDING, "symbol": self.symbol, "volume": lots,
               "type": mt5.ORDER_TYPE_BUY_LIMIT if d > 0 else mt5.ORDER_TYPE_SELL_LIMIT, "price": self.norm(price),
               "sl": self.norm(sl), "tp": self.norm(tp), "magic": self.cfg.system.magic, "comment": comment[:31],
               "type_time": mt5.ORDER_TIME_SPECIFIED, "expiration": int(expiry_utc_ts + self.offset_h * 3600),
               "type_filling": self._filling(pending=True)}
        ok_check, check_msg = self._order_check(req)
        if not ok_check:
            return False, 0, check_msg
        res = mt5.order_send(req)
        if res is None or res.retcode not in (RC_DONE, RC("TRADE_RETCODE_PLACED", 10008)):
            return False, 0, "no reply" if res is None else f"{res.retcode} {res.comment}"
        ticket = int(getattr(res, "order", 0) or 0)
        if ticket <= 0:
            return False, 0, "broker accepted pending request without an order ticket"
        # Verify the pending order is actually visible after placement; a successful API reply alone
        # is not sufficient for a stateful engine that reserves risk against the ticket.
        visible = {int(o.ticket) for o in self.orders()}
        if ticket not in visible:
            return False, 0, f"pending order {ticket} was acknowledged but not visible on broker"
        return True, ticket, "placed"

    def cancel_order(self, ticket):
        self._guard()
        req = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket, "magic": self.cfg.system.magic}
        ok_check, check_msg = self._order_check(req)
        if not ok_check:
            # Risk-reducing actions must remain best-effort. A failed preflight must never prevent
            # the engine from attempting to cancel an exposed pending order.
            log.warning("cancel order_check failed for %s: %s; proceeding with remove", ticket, check_msg)
        res = mt5.order_send(req)
        ok = res is not None and res.retcode in (RC_DONE, RC_NOCHANGE)
        if not ok:
            log.error("cancel failed for %s: %s", ticket, None if res is None else (res.retcode, res.comment))
            return False
        return not any(int(o.ticket) == int(ticket) for o in self.orders())

    def modify_sltp(self, pos, sl=None, tp=None):
        self._guard()
        new_sl = self.norm(sl) if sl is not None else pos.sl
        new_tp = self.norm(tp) if tp is not None else pos.tp
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": self.symbol,
               "sl": new_sl, "tp": new_tp, "magic": self.cfg.system.magic}
        ok_check, check_msg = self._order_check(req)
        if not ok_check:
            log.error("modify order_check failed: %s", check_msg)
            return False
        res = mt5.order_send(req)
        if res is None or res.retcode not in (RC_DONE, RC_NOCHANGE):
            log.error("modify failed: %s", None if res is None else (res.retcode, res.comment))
            return False
        chk = mt5.positions_get(ticket=pos.ticket)
        if chk and abs(chk[0].sl - new_sl) > 2 * self.symbol_info().point:
            log.error("modify not applied: wanted SL %s got %s", new_sl, chk[0].sl)
            return False
        return True

    def close(self, pos, volume=None):
        self._guard()
        long = pos.type == mt5.POSITION_TYPE_BUY
        for attempt in range(self.cfg.execution.max_retries + 1):
            t = self.tick()
            if t is None or self.tick_age_s() > self.cfg.execution.max_tick_age_s:
                log.error("close aborted: stale or missing tick for %s", pos.ticket)
                return False
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol, "position": pos.ticket,
                   "volume": volume or pos.volume, "type": mt5.ORDER_TYPE_SELL if long else mt5.ORDER_TYPE_BUY,
                   "price": t.bid if long else t.ask, "deviation": self.cfg.execution.max_slippage_points * 2,
                   "magic": self.cfg.system.magic, "comment": "agent close", "type_time": mt5.ORDER_TIME_GTC,
                   "type_filling": self._filling()}
            ok_check, check_msg = self._order_check(req)
            if not ok_check:
                # Closing/flattening is risk reduction. Preflight failure must not suppress the actual
                # broker request; order_send remains the final arbiter.
                log.warning("close order_check failed: %s; proceeding with close", check_msg)
            before_vol = float(getattr(pos, "volume", 0.0))
            res = mt5.order_send(req)
            if res is not None and res.retcode in (RC_DONE, RC_PARTIAL):
                remaining = self.position_by_ticket(pos.ticket)
                requested = float(volume or before_vol)
                if remaining is None:
                    return True
                reduced = max(0.0, before_vol - float(getattr(remaining, "volume", before_vol)))
                if reduced + 1e-9 >= requested * 0.999:
                    return True
                log.error("close acknowledged but volume not reduced enough: ticket=%s before=%s after=%s requested=%s",
                          pos.ticket, before_vol, getattr(remaining, "volume", None), requested)
                return False
            if res is None or res.retcode not in RETRYABLE:
                log.error("close failed: %s", None if res is None else (res.retcode, res.comment))
                return False
            time.sleep(self.cfg.execution.retry_delay_s)
        return False

    def close_all_agent_positions(self):
        """Emergency flatten. Returns (closed_tickets, failed_tickets)."""
        self._guard()
        closed, failed = [], []
        for p in self.positions():
            (closed if self.close(p) else failed).append(p.ticket)
        return closed, failed
