import MetaTrader5 as mt5
import time
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, date, timedelta
from colorama import Fore, Style, init
import numpy as np
import pytz
from collections import deque, defaultdict

init(autoreset=True)

# =========================
# PATHS / LOGGING
# =========================
BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "smc_hybrid_pro_v13.log"

class SafeFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        return msg.encode("ascii", "replace").decode("ascii")

def setup_logger():
    logger = logging.getLogger("SMC_Hybrid_PRO")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        fh = RotatingFileHandler(str(LOG_FILE), maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        fmt = SafeFormatter("%(asctime)s | %(levelname)s | %(message)s")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger

logger = setup_logger()

# =========================
# COLOUR PALETTE
# =========================
C = {
    "SYSTEM":        (Fore.CYAN,    Style.BRIGHT),
    "BUY":           (Fore.GREEN,   Style.BRIGHT),
    "SELL":          (Fore.RED,     Style.BRIGHT),
    "HOLD":          (Fore.WHITE,   Style.NORMAL),
    "PENDING":       (Fore.MAGENTA, Style.BRIGHT),
    "BREAKOUT":      (Fore.GREEN,   Style.BRIGHT),
    "CONTINUATION":  (Fore.YELLOW,  Style.BRIGHT),
    "TRAIL":         (Fore.CYAN,    Style.BRIGHT),
    "BE_LOCK":       (Fore.YELLOW,  Style.BRIGHT),
    "PARTIAL_TP":    (Fore.GREEN,   Style.BRIGHT),
    "LOCK_IN":       (Fore.MAGENTA, Style.BRIGHT),
    "CLOSE":         (Fore.MAGENTA, Style.BRIGHT),
    "BASKET":        (Fore.YELLOW,  Style.BRIGHT),
    "WARNING":       (Fore.YELLOW,  Style.NORMAL),
    "ERROR":         (Fore.RED,     Style.BRIGHT),
    "DAILY":         (Fore.CYAN,    Style.BRIGHT),
    "HALT":          (Fore.RED,     Style.BRIGHT),
    "RECONCILE":     (Fore.CYAN,    Style.NORMAL),
    "DIAG":          (Fore.WHITE,   Style.DIM),
    "MARKET_CLOSED": (Fore.WHITE,   Style.NORMAL),
    "SLIPPAGE":      (Fore.YELLOW,  Style.NORMAL),
    "HEADER":        (Fore.WHITE,   Style.BRIGHT),
    "POS_PROFIT":    (Fore.GREEN,   Style.NORMAL),
    "POS_LOSS":      (Fore.RED,     Style.NORMAL),
    "POS_FLAT":      (Fore.YELLOW,  Style.NORMAL),
    "SEPARATOR":     (Fore.CYAN,    Style.BRIGHT),
}

def cprint(msg, color=Fore.WHITE, style=Style.NORMAL):
    print(color + style + msg + Style.RESET_ALL)

def cp(key, msg):
    color, style = C[key]
    cprint(msg, color, style)

def mt5_error(prefix="MT5"):
    return f"{prefix} | last_error={mt5.last_error()}"

def log_order_result(result, context="order_send"):
    if result is None:
        logger.error("%s returned None | %s", context, mt5_error(context))
        cp("ERROR", f"❌ {context} returned None")
        return
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error("%s failed | retcode=%s | comment=%s | %s",
                     context, result.retcode, getattr(result, "comment", ""), mt5_error(context))
        try:
            logger.error("result=%s", result._asdict())
        except Exception:
            pass
        cp("ERROR", f"❌ {context} failed | retcode={result.retcode} | {getattr(result, 'comment', '')}")

# =========================
# NUMPY GUARD HELPER
# =========================
# FIX A — Centralised safe check for MT5 rate arrays.
# mt5.copy_rates_from_pos() returns a numpy structured array,
# NOT a Python list. The expression `if arr` on a numpy array
# raises "ValueError: truth value of array is ambiguous" when
# the array has more than one element — this was the crash.
# Use rates_ok() everywhere instead of bare truthiness checks.

def rates_ok(arr, min_len=1):
    """Return True only if arr is a non-None numpy array with >= min_len rows."""
    return arr is not None and len(arr) >= min_len

# =========================
# SETTINGS
# =========================
# ── TESTING MODE ──────────────────────────────────────────────────
# Set True to use relaxed trade limits (FTMO-style). Resets on restart.
# Set False to restore conservative production limits.
TESTING_MODE = True
# ──────────────────────────────────────────────────────────────────

SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD"]

RISK_PERCENT     = 0.003
MAX_RISK_PERCENT = 0.01
MAX_RISK_MULT    = 3          # skip trade if min-lot risk > 3× intended risk
MAGIC            = 123456
DEVIATION        = 20

BASKET_TP_PCT = 0.04
BASKET_SL_PCT = 0.02

LOCK_IN_R_MULTIPLE = 1.0
LOCK_IN_DRAWBACK   = 0.75
MIN_RR             = 1.5

# Score thresholds
SCORE_MIN_PULLBACK     = 2
SCORE_MIN_BREAKOUT     = 3
SCORE_MIN_CONTINUATION = 3

# Breakout quality
BREAKOUT_DISP_MULT   = 1.2
BREAKOUT_BODY_RATIO  = 0.55
BREAKOUT_BODY_EXPAND = 1.0
BREAKOUT_CLOSE_PCT   = 0.30

# Zone invalidation — global fallback only, overridden per-symbol in SYMBOL_CONFIG
ZONE_INVALIDATE_MULT = 2.0   # fallback for any symbol not specifying its own

# Continuation guards
CONTINUATION_THRESHOLD_MULT = 1.0
CONT_MAX_DIST_MULT          = 4.0
CONT_MOMENTUM_RATIO         = 0.60  # last closed M5 body ≥ 60% of avg prior 3 closed bodies
CONT_MOMENTUM_MIN_DIST_MULT = 2.0   # only enforce momentum gate when price is > 2.0×ATR away from BOS

# Pullback exhaustion guard — block pullback entries after overextended impulses
PB_EXHAUSTION_ATR_MULT = 5.0   # impulse range > 5×ATR ⇒ move is spent, skip pullback

# Pullback zone width
PB_NEAR = 0.382
PB_FAR  = 0.618

DAILY_DRAWDOWN_LIMIT = 0.05 if TESTING_MODE else 0.03   # FTMO=5%, prod=3%
MAX_TRADES_PER_DAY   = 12  if TESTING_MODE else 6      # FTMO=unlimited, 12 is plenty for testing

PARTIAL_CLOSE_PCT = 0.50
PARTIAL_TP_R      = 1.5

PARTIAL_TP_R_ASIA  = 1.0
PARTIAL_TP_R_PRIME = 1.5

TIME_STOP_MIN_PROGRESS_R = 0.30
TIME_STOP_KILLZONE_MINUTES = 25
TIME_STOP_ASIA_MINUTES     = 60
TIME_STOP_DEFAULT_MINUTES  = 45

SL_ATR_MULT_ASIA  = 1.5
SL_ATR_MULT_PRIME = 1.0

SIGNAL_TF  = mt5.TIMEFRAME_M5
CONFIRM_TF = mt5.TIMEFRAME_M15
ENTRY_TF   = mt5.TIMEFRAME_M1

# =========================
# SESSION WINDOWS — LOCAL MARKET TIME
# =========================
LONDON_TZ = pytz.timezone("Europe/London")

SESSION_LOCAL = {
    "XAUUSD": {"tz": LONDON_TZ, "windows": [(8, 12), (13, 18)]},
    "EURUSD": {"tz": LONDON_TZ, "windows": [(8, 12), (13, 18)]},
    "GBPUSD": {"tz": LONDON_TZ, "windows": [(8, 12), (13, 18)]},
    "BTCUSD": {"tz": pytz.UTC,  "windows": [(0, 24)]},
}

KILLZONE_LOCAL = {
    "LONDON_OPEN": (8,  10),
    "NY_OPEN":     (13, 15),
}

ASIA_SESSION_UTC = (0, 7)

BROKER_UTC_OFFSET_HOURS = 2
ROLLOVER_BLOCK_BROKER_HOURS = (23.85, 0.25)

ATR_HISTORY         = {s: deque(maxlen=100) for s in SYMBOLS}
last_trade_time     = {}
signal_cooldown     = {}
peak_profit_tracker = {}
open_trades         = {}
be_locked           = {}
partial_done        = {}
r1_level            = {}
pending_pulls       = {}
last_bos_index      = {s: None for s in SYMBOLS}
last_seen_bos_index = {s: None for s in SYMBOLS}
cont_fired: dict    = {}
_htf_mismatch_warned = {}   # (symbol, bos_idx) -> True; suppress repeated warnings

last_deal_check_utc = None

_daily_reset_date  = None
daily_open_equity  = None
daily_halt         = False
daily_trade_counts = {s: 0 for s in SYMBOLS}

SYMBOL_CONFIG = {
    "XAUUSD": {
        "atr_mult_tp": 2.0, "trail_mult": 0.3,  "cooldown": 300 if TESTING_MODE else 600, "max_trades": 3 if TESTING_MODE else 2,
        "weekdays": [0,1,2,3,4], "sl_pips": 12, "tp_pips": 35,  "type": "commodity",
        "atr_threshold": 0.3, "pip_value": 0.01, "spread_limit": 0.5,
        "bos_min_disp_mult": 0.25,
        "bos_lookback": 40,
        "bos_use_wicks": True,
        "bos_require_cross": False,
        "pb_entry_buffer_atr_mult": 0.15,
        "cont_trigger_atr_mult": 0.60,
        "pb_exhaustion_atr_mult": 15.0,
        # Backtest: RR 1.25 => E=+0.396R PF=2.17 (vs 1.5 => E=+0.346R PF=1.87)
        "min_rr": 1.25,
        # Backtest: session filter kills XAUUSD edge (+0.346 -> +0.232); disable score bonus
        "session_score_bonus": False,
        "session_gate": False,   # no hard block — gold trades all session
        # Zone buffer: Gold wicks ~1.5x ATR on news; 2.5x gives enough breathing room
        "zone_invalidate_mult": 2.5,
        # Pending TTL: Gold setups can take 10 mins to retrace; 600s is safe
        "pending_ttl": 600,
        # FIX 4 (V13): Min SL floor to prevent whipsaws during Asia low-vol period
        "min_sl_distance": 0.15,
        # Mgmt backtest: raw E=+0.396R; all mgmt combined drops to +0.159R (-60%)
        # Best: BE@0.75R + Trail@1.5R(0.2xATR) (E=+0.387R PF=2.56)
        "mgmt_be_lock_r": 0.75,
        "mgmt_trail_trigger_r": 1.5,
        "mgmt_partial_enabled": False,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": False,
    },
    "EURUSD": {
        "atr_mult_tp": 1.8, "trail_mult": 0.2,  "cooldown": 300 if TESTING_MODE else 600, "max_trades": 3 if TESTING_MODE else 2,
        "weekdays": [0,1,2,3,4], "sl_pips": 15, "tp_pips": 40,  "type": "forex",
        "atr_threshold": 0.00008, "pip_value": 0.0001, "spread_limit": 0.0002,
        "bos_min_disp_mult": 0.30,
        "bos_lookback": 50,
        "bos_use_wicks": False,
        "bos_require_cross": False,
        "cont_trigger_atr_mult": 0.70,
        "pb_exhaustion_atr_mult": 8.0,
        "candle_body_mult_prime": 0.28,   # was 0.30
        "candle_body_ratio_min": 0.42,    # was 0.45
        "candle_wick_max_mult": 1.00,     # was 0.95
        "session_gate": True,    # Backtest: session is best single filter for EURUSD
        # Forex is more structured; 2.0× is correct
        "zone_invalidate_mult": 2.0,
        "pending_ttl": 300,
        # FIX 4 (V13): Min SL floor
        "min_sl_distance": 0.00015,
        # Mgmt backtest: TS@5bars/0.5R is best (PF=1.64, totR=+30.07 vs raw +23.74)
        # BE@0.5R + TS combo (PF=1.66). Lock-in/partial hurt.
        "mgmt_be_lock_r": 0.5,
        "mgmt_partial_enabled": False,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": True,
        "mgmt_time_stop_bars": 5,
        "mgmt_time_stop_min_r": 0.50,
    },
    "GBPUSD": {
        "atr_mult_tp": 1.8, "trail_mult": 0.2,  "cooldown": 300 if TESTING_MODE else 600, "max_trades": 3 if TESTING_MODE else 2,
        "weekdays": [0,1,2,3,4], "sl_pips": 15, "tp_pips": 40,  "type": "forex",
        "atr_threshold": 0.0001, "pip_value": 0.0001, "spread_limit": 0.0003,
        "bos_min_disp_mult": 0.25,
        "bos_lookback": 60,
        "bos_use_wicks": False,
        "bos_require_cross": False,
        "cont_trigger_atr_mult": 0.70,
        "pb_exhaustion_atr_mult": 10.0,
        "candle_body_mult_prime": 0.28,   # was 0.30
        "candle_body_ratio_min": 0.42,    # was 0.45
        "candle_wick_max_mult": 1.00,     # was 0.95
        "session_gate": True,    # Backtest: session is best single filter for GBPUSD
        "zone_invalidate_mult": 2.0,
        "pending_ttl": 300,
        # FIX 4 (V13): Min SL floor
        "min_sl_distance": 0.0002,
        # Mgmt backtest: BE@1.0R + Trail@1.5R(0.2x) is transformative (E=+0.419R PF=2.18)
        # Lockin@1R/0.75 best totR but trail is better expectancy. Time stop hurts.
        "mgmt_be_lock_r": 1.0,
        "mgmt_trail_trigger_r": 1.5,
        "mgmt_partial_enabled": False,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": False,
    },
    "BTCUSD": {
        "atr_mult_tp": 2.5, "trail_mult": 0.3,  "cooldown": 300 if TESTING_MODE else 600, "max_trades": 3 if TESTING_MODE else 2,
        "weekdays": [0,1,2,3,4,5,6], "sl_pips": 200, "tp_pips": 600, "type": "crypto",
        "atr_threshold": 30, "pip_value": 1, "spread_limit": 150,
        "bos_min_disp_mult": 0.15,
        "bos_lookback": 60,
        "bos_use_wicks": True,
        "bos_require_cross": False,
        "pb_entry_buffer_atr_mult": 0.15,
        "cont_trigger_atr_mult": 0.60,
        "pb_exhaustion_atr_mult": 15.0,
        "score_min_continuation": 1,
        "breakout_body_ratio": 0.35,
        "breakout_close_pct": 0.40,
        "candle_body_mult_asia": 0.15,
        "candle_body_mult_prime": 0.25,
        "candle_body_ratio_min": 0.40,
        "candle_wick_max_mult": 1.20,
        "session_gate": False,   # BTC is 24/7; session window already (0,24)
        # Backtest: RR 1.25 => E=+0.282R PF=1.73 (vs 1.5 => E=+0.280R PF=1.66)
        "min_rr": 1.25,
        # FIX 4 (V13): Min SL floor to prevent whipsaws
        "min_sl_distance": 250,
        # BTC wicks aggressively — a 71-point push above zone with ATR=105 is normal
        # liquidity sweep behaviour, NOT invalidation. Use 3.5x to avoid false clears.
        "zone_invalidate_mult": 3.5,
        # Asia BTC can take 15+ mins to form a continuation candle
        "pending_ttl": 900,
        # Mgmt backtest: TS@9bars/0.3R best totR (+137.58 PF=2.11 vs raw +109.53)
        # BE@0.5R great DD reduction (DD=5.0R vs 7.9R). Lock-in/partial hurt.
        "mgmt_be_lock_r": 0.5,
        "mgmt_partial_enabled": False,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": True,
        "mgmt_time_stop_bars": 9,
        "mgmt_time_stop_min_r": 0.30,
    },
}

# =========================
# MT5 INIT / RECONNECT
# =========================
MAX_RECONNECT_WAIT = 60

def mt5_connect():
    if not mt5.initialize():
        logger.error("MT5 initialize failed | %s", mt5_error("initialize"))
        cp("ERROR", "❌ MT5 connection failed")
        return False
    logger.info("Connected to MT5 (build %s)", mt5.version())
    cp("SYSTEM", f"✅ Connected to MT5 (build {mt5.version()})")
    return True

def is_connected():
    return mt5.account_info() is not None

def reconnect_mt5():
    delay = 5
    attempt = 0
    while True:
        attempt += 1
        mt5.shutdown()
        cp("WARNING", f"🔄 Reconnect attempt {attempt} (waiting {delay}s)…")
        logger.warning("Reconnect attempt %d (delay %ds)", attempt, delay)
        time.sleep(delay)
        if mt5_connect():
            for s in SYMBOLS:
                ensure_symbol(s)
            return
        delay = min(delay * 2, MAX_RECONNECT_WAIT)

def ensure_symbol(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        cp("ERROR", f"❌ Symbol not found: {symbol}")
        return False
    if not info.visible and not mt5.symbol_select(symbol, True):
        cp("ERROR", f"❌ symbol_select failed for {symbol}")
        return False
    return True

if not mt5_connect():
    raise SystemExit(1)

# =========================
# DAILY TRACKERS
# =========================
def reset_daily_trackers():
    global _daily_reset_date, daily_open_equity, daily_halt, daily_trade_counts
    today = date.today()
    if _daily_reset_date == today:
        return

    # Always reset counters on date change so we never get stuck at trade limits
    # if MT5 temporarily fails to provide account info at midnight.
    daily_halt         = False
    daily_trade_counts = {s: 0 for s in SYMBOLS}
    _daily_reset_date  = today

    acc = mt5.account_info()
    if acc is None:
        logger.warning("Daily reset: account_info unavailable (counters reset anyway)")
        return

    daily_open_equity  = acc.equity
    cp("DAILY",
       f"📅 Daily reset | equity={daily_open_equity:.2f} | "
       f"DD_limit={DAILY_DRAWDOWN_LIMIT*100:.1f}% | max_trades/symbol={MAX_TRADES_PER_DAY}")
    logger.info("Daily reset | equity=%.2f | DD=%.1f%% | max=%d",
                daily_open_equity, DAILY_DRAWDOWN_LIMIT * 100, MAX_TRADES_PER_DAY)

def check_daily_drawdown():
    global daily_halt
    if daily_halt:
        return True
    if daily_open_equity is None:
        return False
    acc = mt5.account_info()
    if acc is None:
        return False
    drawdown = (daily_open_equity - acc.equity) / daily_open_equity
    if drawdown >= DAILY_DRAWDOWN_LIMIT:
        daily_halt = True
        cp("HALT",
           f"🛑 DAILY DRAWDOWN HARD STOP | {drawdown*100:.2f}% "
           f"| open={daily_open_equity:.2f} | now={acc.equity:.2f}")
        logger.warning("DAILY DD STOP | %.2f%% | open=%.2f | now=%.2f",
                       drawdown * 100, daily_open_equity, acc.equity)
        for pos in (mt5.positions_get() or []):
            if pos.magic == MAGIC:
                close_position(pos, "DAILY_DD_STOP")
        return True
    return False

# =========================
# STARTUP RECONCILIATION
# =========================
def reconcile_open_positions():
    positions  = mt5.positions_get() or []
    reconciled = 0

    def _history_orders_for_position(ticket, approx_open_utc):
        if not hasattr(mt5, "history_orders_get"):
            return []
        now_utc = get_utc_time()
        start_utc = approx_open_utc - timedelta(days=2) if isinstance(approx_open_utc, datetime) else (now_utc - timedelta(days=7))
        try:
            orders = mt5.history_orders_get(start_utc, now_utc, position=ticket)
        except Exception:
            orders = None
        if not orders:
            return []
        matched = [o for o in orders if getattr(o, "position_id", None) == ticket]
        return matched

    def _history_deals_for_position(ticket, approx_open_utc):
        now_utc = get_utc_time()
        start_utc = approx_open_utc - timedelta(days=2) if isinstance(approx_open_utc, datetime) else (now_utc - timedelta(days=7))
        try:
            deals = mt5.history_deals_get(start_utc, now_utc, position=ticket)
        except Exception:
            deals = None
        if not deals:
            return []
        matched = [d for d in deals if getattr(d, "position_id", None) == ticket]
        return matched

    def _infer_open_utc(pos, deals):
        t = getattr(pos, "time", None)
        if isinstance(t, (int, float)) and t > 0:
            try:
                return datetime.fromtimestamp(t, tz=pytz.UTC)
            except Exception:
                pass
        in_deals = [d for d in (deals or []) if getattr(d, "entry", None) == mt5.DEAL_ENTRY_IN]
        if in_deals:
            d0 = in_deals[0]
            dt = getattr(d0, "time", None)
            if isinstance(dt, (int, float)) and dt > 0:
                try:
                    return datetime.fromtimestamp(dt, tz=pytz.UTC)
                except Exception:
                    pass
        return None

    def _infer_initial_sl_tp(pos, orders):
        direction = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        init_sl = None
        init_tp = None
        for o in orders or []:
            o_sl = float(getattr(o, "sl", 0.0) or 0.0)
            o_tp = float(getattr(o, "tp", 0.0) or 0.0)
            if o_sl and o_sl > 0:
                init_sl = o_sl
            if o_tp and o_tp > 0:
                init_tp = o_tp
            if init_sl is not None or init_tp is not None:
                break

        # Fallback: current broker-side SL/TP
        if init_sl is None:
            init_sl = float(pos.sl or 0.0)
        if init_tp is None:
            init_tp = float(pos.tp or 0.0)

        # Emergency SL if missing
        if not init_sl:
            atr = get_atr(pos.symbol, SIGNAL_TF)
            c = SYMBOL_CONFIG.get(pos.symbol, {})
            pip_value = float(c.get("pip_value", 0.0) or 0.0)
            sl_pips = float(c.get("sl_pips", 0.0) or 0.0)
            dist_pips = sl_pips * pip_value if pip_value and sl_pips else 0.0
            dist_atr = float(atr or 0.0)
            dist = max(dist_pips, dist_atr)
            if dist <= 0:
                dist = abs(pos.price_open) * 0.001
            init_sl = (pos.price_open - dist) if direction == "BUY" else (pos.price_open + dist)
            init_sl = normalize_price(pos.symbol, init_sl)
            cp("WARNING", f"⚠️  {pos.symbol} | Ticket:{pos.ticket} has no SL — emergency SL set to {init_sl:.5f}")
            logger.warning("%s | Ticket:%s no SL found in history — emergency SL=%.5f", pos.symbol, pos.ticket, init_sl)

            # Apply the emergency SL to the live position
            res = modify_position_sl_tp(pos, init_sl, pos.tp)
            if not (res and res.retcode == mt5.TRADE_RETCODE_DONE):
                log_order_result(res, "modify_sl_tp")

        return float(init_sl or 0.0), float(init_tp or 0.0)

    for pos in positions:
        if pos.ticket in open_trades:
            continue
        if pos.symbol not in SYMBOL_CONFIG:
            continue

        direction = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        approx_open_utc = None
        try:
            if getattr(pos, "time", None):
                approx_open_utc = datetime.fromtimestamp(int(pos.time), tz=pytz.UTC)
        except Exception:
            approx_open_utc = None

        deals = _history_deals_for_position(pos.ticket, approx_open_utc)
        open_utc = _infer_open_utc(pos, deals)
        orders = _history_orders_for_position(pos.ticket, open_utc or approx_open_utc)
        init_sl, init_tp = _infer_initial_sl_tp(pos, orders)

        sl_dist = abs(pos.price_open - init_sl) if init_sl else 0.0
        open_trades[pos.ticket] = {
            "symbol": pos.symbol,
            "type": direction,
            "entry": pos.price_open,
            "sl": init_sl,
            "tp": init_tp,
            "lot": pos.volume,
            "risk_1r": sl_dist,
            "entry_mode": "manual_adopted" if pos.magic != MAGIC else "reconciled",
            "open_utc": open_utc,
        }

        be_locked[pos.ticket] = (pos.sl >= pos.price_open if direction == "BUY"
                                 else (pos.sl <= pos.price_open and pos.sl != 0.0))
        if sl_dist > 0:
            r1_level[pos.ticket] = (pos.price_open + sl_dist if direction == "BUY"
                                    else pos.price_open - sl_dist)
        peak_profit_tracker[pos.ticket] = max(pos.profit, 0.0)
        partial_done[pos.ticket] = False
        reconciled += 1

        tag = "ADOPT" if pos.magic != MAGIC else "RECONCILE"
        cp(tag, f"[{tag}] {pos.symbol} | Ticket:{pos.ticket} | {direction} | BE:{be_locked[pos.ticket]}")
        logger.info("[%s] %s | Ticket:%s | %s | BE:%s", tag, pos.symbol, pos.ticket, direction, be_locked[pos.ticket])
    if reconciled:
        cp("SYSTEM", f"✅ Reconciled {reconciled} existing position(s)")
    else:
        cp("MARKET_CLOSED", "ℹ️  No existing positions to reconcile")

# =========================
# TIME / SESSION
# =========================
def get_utc_time():
    return datetime.now(pytz.UTC)

reconcile_open_positions()

def in_session(symbol, utc_dt):
    cfg      = SESSION_LOCAL[symbol]
    local_dt = utc_dt.astimezone(cfg["tz"])
    h        = local_dt.hour + local_dt.minute / 60.0
    for start, end in cfg["windows"]:
        if start <= h < end:
            return True
    return False

def market_open(symbol):
    now = get_utc_time()
    if now.weekday() >= 5:
        return False
    info = mt5.symbol_info(symbol)
    if info is None:
        return False
    if hasattr(mt5, "SYMBOL_TRADE_MODE_DISABLED") and info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        return False
    if hasattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY") and info.trade_mode == mt5.SYMBOL_TRADE_MODE_CLOSEONLY:
        return False
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False
    if tick.bid is None or tick.ask is None or tick.bid <= 0 or tick.ask <= 0:
        return False
    return True

def in_killzone(utc_dt):
    local_dt = utc_dt.astimezone(LONDON_TZ)
    h        = local_dt.hour + local_dt.minute / 60.0
    for name, (start, end) in KILLZONE_LOCAL.items():
        if start <= h < end:
            return name
    return None

def in_asia_session(utc_dt):
    h = utc_dt.hour + utc_dt.minute / 60.0
    return ASIA_SESSION_UTC[0] <= h < ASIA_SESSION_UTC[1]

def broker_time_from_utc(utc_dt):
    return utc_dt + timedelta(hours=float(BROKER_UTC_OFFSET_HOURS))

def in_rollover_block(symbol, utc_dt):
    if SYMBOL_CONFIG.get(symbol, {}).get("type") != "forex":
        return False
    broker_dt = broker_time_from_utc(utc_dt)
    h = broker_dt.hour + broker_dt.minute / 60.0
    start, end = ROLLOVER_BLOCK_BROKER_HOURS
    if start <= end:
        return start <= h < end
    return (h >= start) or (h < end)

def get_sl_atr_multiplier(utc_dt):
    return SL_ATR_MULT_ASIA if in_asia_session(utc_dt) else SL_ATR_MULT_PRIME

def _session_tag_for_time(symbol, utc_dt):
    kz = in_killzone(utc_dt)
    if kz:
        return f"KZ:{kz}"
    if in_asia_session(utc_dt):
        return "ASIA"
    if in_session(symbol, utc_dt):
        return "PRIME"
    return "OFF"

def check_spread_acceptable(symbol):
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False
    spread = tick.ask - tick.bid
    limit  = SYMBOL_CONFIG[symbol]["spread_limit"]
    if spread > limit:
        logger.debug("%s spread %.5f > limit %.5f", symbol, spread, limit)
        return False
    return True

# =========================
# INDICATORS
# =========================
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    prices = np.asarray(prices, dtype=float)
    mult   = 2 / (period + 1)
    ema    = np.mean(prices[:period])
    for p in prices[period:]:
        ema = p * mult + ema * (1 - mult)
    return ema

def get_htf_trend(symbol):
    rates = mt5.copy_rates_from_pos(symbol, CONFIRM_TF, 0, 60)
    if not rates_ok(rates, 50):
        return None
    closes = np.array([r["close"] for r in rates], dtype=float)
    ema50  = calculate_ema(closes, 50)
    if ema50 is None:
        return None
    if closes[-1] > ema50: return "BULL"
    if closes[-1] < ema50: return "BEAR"
    return None

def get_atr(symbol, tf=SIGNAL_TF, period=14):
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, period + 1)
    if not rates_ok(rates, period + 1):
        return None
    highs  = np.array([r["high"]  for r in rates], dtype=float)
    lows   = np.array([r["low"]   for r in rates], dtype=float)
    closes = np.array([r["close"] for r in rates], dtype=float)
    tr  = np.maximum(highs[1:] - lows[1:],
          np.maximum(np.abs(highs[1:] - closes[:-1]),
                     np.abs(lows[1:]  - closes[:-1])))
    atr = float(np.mean(tr))
    ATR_HISTORY[symbol].append(atr)
    return atr

def get_entry_atr(symbol, period=14):
    return get_atr(symbol, ENTRY_TF, period)

# =========================
# FIX B — ATR REGIME: ADVISORY ONLY DURING KILLZONE
# =========================
# Previously atr_regime_ok() was a hard gate — if ATR was below the
# 15th percentile, all signals were blocked. During trend expansion
# phases (like today's NY session), ATR can be transiently low while
# price is actually moving fast. This caused EURUSD/GBPUSD to show
# ATR=LOW and miss the entire move.
#
# Fix: atr_regime_ok() now returns the result as before, but
# get_signal() only hard-blocks on it when OUTSIDE a killzone.
# Inside London Open or NY Open the ATR regime is advisory — we log
# it but allow the signal through. This is safe because killzones
# already require a higher score (displacement + session_ok + killzone
# bonus) to fire.

def atr_regime_ok(symbol):
    arr = list(ATR_HISTORY[symbol])
    if len(arr) < 30:
        return True
    current = arr[-1]
    p15 = np.percentile(arr, 15)
    p90 = np.percentile(arr, 90)
    if current < p15:                               return False
    if current > p90 * 1.35:                        return False
    if len(arr) >= 6 and current < arr[-6] * 0.85:  return False
    return True

# =========================
# STRUCTURE DETECTION
# =========================
def find_bos_candle_index(closes, highs, lows, atr, lookback=30, min_disp_mult=0.5,
                          use_wicks=False, require_cross=True):
    if len(closes) < lookback + 5 or atr is None:
        return None, None, 0.0
    min_disp = atr * float(min_disp_mult)
    for i in range(len(closes) - 1, lookback, -1):
        window_high = max(highs[i - lookback: i - 2])
        window_low  = min(lows[i - lookback:  i - 2])
        if use_wicks:
            if highs[i] > window_high and ((not require_cross) or (highs[i - 1] <= window_high)):
                disp = highs[i] - window_high
                if disp > min_disp:
                    return "BOS_BUY", i, float(disp)
            if lows[i] < window_low and ((not require_cross) or (lows[i - 1] >= window_low)):
                disp = window_low - lows[i]
                if disp > min_disp:
                    return "BOS_SELL", i, float(disp)
        else:
            if closes[i] > window_high and ((not require_cross) or (closes[i - 1] <= window_high)):
                disp = closes[i] - window_high
                if disp > min_disp:
                    return "BOS_BUY", i, float(disp)
            if closes[i] < window_low and ((not require_cross) or (closes[i - 1] >= window_low)):
                disp = window_low - closes[i]
                if disp > min_disp:
                    return "BOS_SELL", i, float(disp)
    return None, None, 0.0

def get_structural_impulse(highs, lows, bos_type, bos_idx, lookback=20):
    start = max(0, bos_idx - lookback)
    if bos_type == "BOS_BUY":
        return float(np.min(lows[start:bos_idx + 1])), float(highs[bos_idx])
    return float(lows[bos_idx]), float(np.max(highs[start:bos_idx + 1]))

def detect_sweep_improved(highs, lows, closes):
    if len(closes) < 10:
        return None
    prev_high     = max(highs[-10:-2])
    prev_low      = min(lows[-10:-2])
    current_close = closes[-1]
    if highs[-1] >= prev_high and current_close < (prev_high + lows[-1]) / 2:
        return "SWEEP_HIGH"
    if lows[-1] <= prev_low and current_close > (highs[-1] + prev_low) / 2:
        return "SWEEP_LOW"
    return None

def detect_liquidity_target(symbol, direction, rates_signal, entry, sl):
    highs  = np.array([r["high"]  for r in rates_signal], dtype=float)
    lows   = np.array([r["low"]   for r in rates_signal], dtype=float)
    closes = np.array([r["close"] for r in rates_signal], dtype=float)
    current     = closes[-1]
    lookback    = 40
    if len(closes) < lookback:
        return None
    min_tp_dist = abs(entry - sl) * MIN_RR
    if direction == "BUY":
        pools = [max(highs[-lookback:])]
        for i in range(5, lookback):
            h = highs[-i]
            if abs(h - highs[-i - 1]) <= np.std(highs[-lookback:]) * 0.15:
                if h > current and (h - entry) >= min_tp_dist:
                    pools.append(h)
        return min([p for p in pools if p > current and (p - entry) >= min_tp_dist], default=None)
    pools = [min(lows[-lookback:])]
    for i in range(5, lookback):
        l = lows[-i]
        if abs(l - lows[-i - 1]) <= np.std(lows[-lookback:]) * 0.15:
            if l < current and (entry - l) >= min_tp_dist:
                pools.append(l)
    return max([p for p in pools if p < current and (entry - p) >= min_tp_dist], default=None)

def pullback_zone_from_impulse(direction, impulse_low, impulse_high):
    swing = impulse_high - impulse_low
    if direction == "BUY":
        return impulse_high - swing * PB_FAR, impulse_high - swing * PB_NEAR
    return impulse_low + swing * PB_NEAR, impulse_low + swing * PB_FAR

def zone_is_invalidated(direction, zone_low, zone_high, price, atr, mult=None):
    """
    A setup is invalidated when price moves far enough in the WRONG direction:

      SELL setup: invalidated when price RISES above zone_high by > threshold
      BUY  setup: invalidated when price FALLS below zone_low  by > threshold

    FIX 1 (V12.2c): mult is now a per-symbol value from SYMBOL_CONFIG.
      BTC  uses 3.5× — aggressive wicks are normal, not invalidation.
      Gold uses 2.5× — news wicks need headroom.
      Forex uses 2.0× — structured moves, tighter buffer correct.

    FIX 2 (V12.2c): callers must pass the ATR stored in the pending setup,
      NOT a freshly fetched live ATR. A rising ATR mid-move was widening the
      threshold and causing false invalidations during BTC expansion phases.
    """
    m         = mult if mult is not None else ZONE_INVALIDATE_MULT
    threshold = atr * m
    if direction == "SELL" and price > zone_high + threshold:
        return True
    if direction == "BUY"  and price < zone_low  - threshold:
        return True
    return False


def impulse_is_broken(direction, impulse_low, impulse_high, price, atr, buffer_mult=0.25):
    if atr is None:
        return False
    buf = atr * buffer_mult
    if direction == "BUY":
        if impulse_low is None:
            return False
        return price < (impulse_low - buf)
    if impulse_high is None:
        return False
    return price > (impulse_high + buf)

# =========================
# BREAKOUT CANDLE QUALITY FILTER
# =========================
def bos_candle_quality_ok(rates_signal, bos_idx, direction, symbol=None):
    if bos_idx < 3 or bos_idx >= len(rates_signal):
        return False
    cfg = SYMBOL_CONFIG.get(symbol, {}) if symbol else {}
    body_ratio_min = float(cfg.get("breakout_body_ratio", BREAKOUT_BODY_RATIO))
    body_expand    = float(cfg.get("breakout_body_expand", BREAKOUT_BODY_EXPAND))
    close_pct      = float(cfg.get("breakout_close_pct", BREAKOUT_CLOSE_PCT))
    bos_c = rates_signal[bos_idx]
    o, h, l, c = bos_c["open"], bos_c["high"], bos_c["low"], bos_c["close"]
    body       = abs(c - o)
    rng        = max(h - l, 1e-9)
    body_ratio = body / rng
    if body_ratio < body_ratio_min:
        logger.debug("BOS body_ratio %.2f < %.2f — breakout blocked", body_ratio, body_ratio_min)
        return False
    prior_bodies = [abs(rates_signal[j]["close"] - rates_signal[j]["open"])
                    for j in range(max(0, bos_idx - 3), bos_idx)]
    if prior_bodies:
        avg_prior = np.mean(prior_bodies)
        if avg_prior > 0 and body < avg_prior * body_expand:
            logger.debug("BOS body %.5f < avg_prior %.5f — breakout blocked", body, avg_prior)
            return False
    if direction == "BUY":
        if c < h - rng * close_pct:
            logger.debug("BOS BUY close not in top %.0f%% — breakout blocked", close_pct * 100)
            return False
    else:
        if c > l + rng * close_pct:
            logger.debug("BOS SELL close not in bottom %.0f%% — breakout blocked", close_pct * 100)
            return False
    return True

# =========================
# CONTINUATION QUALITY CHECKS
# =========================
def continuation_momentum_ok(rates_signal):
    """
    Check whether momentum is still present on M5.

    CRITICAL FIX (V12.2d): rates_signal[-1] is the CURRENT OPEN candle.
    Its body is tiny mid-candle because it hasn't closed yet — this was
    causing the momentum check to always fail during steady grinding trends
    where the "current" 30-second-old candle body is smaller than recently
    closed candles. We now use rates_signal[-2] (the last CLOSED candle)
    as "current" and compare against rates_signal[-3], [-4], [-5].

    Also lowered CONT_MOMENTUM_RATIO from 0.80 to 0.60 — the original
    0.80 threshold was too tight for slow NY-afternoon continuation moves.
    """
    if len(rates_signal) < 5:
        return True
    # Use [-2] = last closed candle, not [-1] = currently open candle
    current_body = abs(rates_signal[-2]["close"] - rates_signal[-2]["open"])
    prior_bodies = [abs(rates_signal[-(i+3)]["close"] - rates_signal[-(i+3)]["open"])
                    for i in range(3)]
    avg_prior = np.mean(prior_bodies)
    if avg_prior == 0:
        return True
    ratio = current_body / avg_prior
    if ratio < CONT_MOMENTUM_RATIO:
        logger.debug("Continuation momentum fading: closed_ratio=%.2f < %.2f",
                     ratio, CONT_MOMENTUM_RATIO)
        return False
    return True

def continuation_distance_ok(direction, bos_level, price, atr):
    dist = abs(price - bos_level)
    if dist > atr * CONT_MAX_DIST_MULT:
        logger.debug("Continuation distance %.5f > max %.5f×ATR", dist, CONT_MAX_DIST_MULT)
        return False
    return True

def pullback_impulse_exhaustion_ok(symbol, impulse_low, impulse_high, atr):
    """Block pullback entries when the impulse range is overextended (move is spent)."""
    impulse_range = impulse_high - impulse_low
    max_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("pb_exhaustion_atr_mult", PB_EXHAUSTION_ATR_MULT))
    if impulse_range > atr * max_mult:
        return False
    return True

# =========================
# CANDLE FRESHNESS & CONFIRMATION
# =========================
CANDLE_FRESHNESS_SECS  = 30     # Use [-2] if candle is >30s old (closed), [-1] if <30s (forming)
CANDLE_BODY_MULT_ASIA  = 0.20   # Asia:  20% of M5 ATR minimum body
CANDLE_BODY_MULT_PRIME = 0.35   # Prime: 35% of M5 ATR minimum body

def _pick_confirmation_candle(rates_entry):
    """
    FIX 3 (V13): Candle age detection for M1 entry confirmation.
    
    The current open M1 candle ([-1]) forms slowly. A 30-second-old entry signal
    can use a 1-minute candle that opened 60s ago, creating a stale data problem:
    price entered at 1.0850 at 30s mark but the [-1] candle only opened 5s ago,
    so its body/wick don't reflect the actual entry conditions.
    
    Solution: Use the LAST CLOSED candle ([-2]) if the current candle in-progress
    for >30 seconds. Otherwise use the most recent data ([-1]).
    
    Heuristic: Check the difference between the last two close timestamps.
    If the gap is >30s, the [-2] candle finished forming >30s ago—use it.
    Otherwise, the current move is fresh—use latest ([-1]).
    """
    if not rates_ok(rates_entry, 3):
        return -1  # Fallback to current candle if data missing
    
    try:
        time_gap = float(rates_entry[-1]["time"]) - float(rates_entry[-2]["time"])
        if time_gap > CANDLE_FRESHNESS_SECS:
            # Candle formed >30s ago; use closed candle
            return -2
    except (KeyError, TypeError, ValueError):
        pass
    
    # Use current/most recent
    return -1

def candle_confirmation(direction, rates_entry, atr, is_asia=False, symbol=None):
    """
    FIX 3 (V13): Updated to use freshness-aware candle selection.
    """
    # FIX A — use rates_ok() guard, never bare truthiness on numpy array
    if not rates_ok(rates_entry, 3) or atr is None:
        return False
    cfg = SYMBOL_CONFIG.get(symbol, {}) if symbol else {}
    body_mult_asia  = float(cfg.get("candle_body_mult_asia",  CANDLE_BODY_MULT_ASIA))
    body_mult_prime = float(cfg.get("candle_body_mult_prime", CANDLE_BODY_MULT_PRIME))
    body_ratio_min  = float(cfg.get("candle_body_ratio_min",  0.50))
    wick_max_mult   = float(cfg.get("candle_wick_max_mult",   0.85))
    
    # FIX 3 (V13) — pick freshness-aware candle
    candle_idx = _pick_confirmation_candle(rates_entry)
    c          = rates_entry[candle_idx]
    o          = c["open"]
    h          = c["high"]
    l          = c["low"]
    body       = abs(c["close"] - o)
    rng        = max(h - l, 1e-9)
    upper_wick = h - max(o, c["close"])
    lower_wick = min(o, c["close"]) - l

    # Session-aware body threshold
    body_mult = body_mult_asia if is_asia else body_mult_prime
    disp_ok   = body >= atr * body_mult

    if not disp_ok:
        logger.debug("candle_confirmation FAIL | body=%.5f < atr*%.2f=%.5f | session=%s",
                     body, body_mult, atr * body_mult, "Asia" if is_asia else "Prime")
        return False

    if direction == "BUY":
        ok = body / rng >= body_ratio_min and lower_wick <= body * wick_max_mult
    else:
        ok = body / rng >= body_ratio_min and upper_wick <= body * wick_max_mult

    if not ok:
        logger.debug("candle_confirmation FAIL | body_ratio=%.2f wick check | dir=%s",
                     body / rng, direction)
    return ok

# =========================
# SCORING
# =========================
def trade_quality_score(sweep, displacement, atr, session_ok,
                        pullback_hit, liquidity_tp, killzone,
                        entry_mode="pullback", symbol=None):
    score = 0
    if sweep in ("SWEEP_LOW", "SWEEP_HIGH"):                                score += 1
    if displacement is not None and atr is not None and displacement >= atr * 0.8:
        score += 1
    # Backtest: session bonus kills XAUUSD edge; per-symbol opt-out
    sess_bonus = SYMBOL_CONFIG.get(symbol, {}).get("session_score_bonus", True) if symbol else True
    if session_ok and sess_bonus:                                            score += 1
    if pullback_hit:                                                         score += 1
    if liquidity_tp is not None:                                             score += 1
    if killzone is not None:                                                 score += 1
    if entry_mode == "continuation":                                         score -= 1
    return score

# =========================
# BOS STALENESS GUARD
# =========================
def is_fresh_bos(symbol, bos_idx):
    if last_bos_index[symbol] == bos_idx:
        logger.debug("%s stale BOS idx=%d", symbol, bos_idx)
        return False
    last_bos_index[symbol] = bos_idx
    return True

# =========================
# THREE-MODE SIGNAL ENGINE — V12.2
# =========================
# Changes from V12.1:
#
# FIX A — All `if rates_entry_r` replaced with `rates_ok(rates_entry_r, N)`
#          Prevents the numpy boolean crash entirely.
#
# FIX B — ATR regime is now advisory inside killzones.
#          Outside killzones: hard block as before.
#          Inside killzones: log warning but allow signal through.
#
# FIX C — Zone invalidation no longer resets last_bos_index to None.
#          Previously: invalidate → reset → next loop finds same BOS
#          as "fresh" → store pending → invalidate → loop forever.
#          Now: invalidation clears the pending setup only. The BOS
#          index stays recorded as stale so find_bos_candle_index()
#          must detect a genuinely new BOS candle before anything fires.
#          This breaks the infinite detect/invalidate loop.
#
# FIX D — Continuation path now uses pending_pulls["bos_level"] when
#          available so it correctly evaluates distance from the
#          original BOS even across multiple loops.

def get_signal(symbol):
    if not market_open(symbol):
        _gate_hit(symbol, "market_closed")
        return "HOLD", 0.0, None
    utc_now = get_utc_time()
    if in_rollover_block(symbol, utc_now):
        _gate_hit(symbol, "rollover")
        return "HOLD", 0.0, None
    if not check_spread_acceptable(symbol):
        _gate_hit(symbol, "spread")
        return "HOLD", 0.0, None

    rates_signal  = mt5.copy_rates_from_pos(symbol, SIGNAL_TF,  0, 80)
    rates_confirm = mt5.copy_rates_from_pos(symbol, CONFIRM_TF, 0, 70)
    rates_entry   = mt5.copy_rates_from_pos(symbol, ENTRY_TF,   0, 70)
    # FIX A — explicit None + length checks, never bare truthiness
    if not rates_ok(rates_signal,  40):
        _gate_hit(symbol, "rates_signal")
        return "HOLD", 0.0, None
    if not rates_ok(rates_confirm, 50):
        _gate_hit(symbol, "rates_confirm")
        return "HOLD", 0.0, None
    if not rates_ok(rates_entry,   20):
        _gate_hit(symbol, "rates_entry")
        return "HOLD", 0.0, None

    s_highs  = np.array([r["high"]  for r in rates_signal], dtype=float)
    s_lows   = np.array([r["low"]   for r in rates_signal], dtype=float)
    s_closes = np.array([r["close"] for r in rates_signal], dtype=float)
    e_closes = np.array([r["close"] for r in rates_entry],  dtype=float)

    price = float(e_closes[-1])
    atr   = get_atr(symbol, SIGNAL_TF)
    if atr is None:
        _gate_hit(symbol, "atr_none")
        return "HOLD", price, None

    atr_entry = get_entry_atr(symbol) or atr

    # FIX B — ATR regime: hard block outside killzone, advisory inside
    utc_now    = utc_now
    killzone   = in_killzone(utc_now)
    session_ok = in_session(symbol, utc_now)
    regime_ok = atr_regime_ok(symbol)
    atr_low_quality_filter = (not regime_ok and killzone is None and not session_ok)
    if not regime_ok and not atr_low_quality_filter:
        logger.debug("%s ATR regime LOW inside active session/killzone (session_ok=%s, KZ=%s) — proceeding with caution",
                     symbol, session_ok, killzone)

    bos_lb    = SYMBOL_CONFIG.get(symbol, {}).get("bos_lookback", 30)
    bos_mult  = SYMBOL_CONFIG.get(symbol, {}).get("bos_min_disp_mult", 0.5)
    bos_wicks = bool(SYMBOL_CONFIG.get(symbol, {}).get("bos_use_wicks", False))
    bos_req_cross = bool(SYMBOL_CONFIG.get(symbol, {}).get("bos_require_cross", True))
    bos_type, bos_idx, displacement = find_bos_candle_index(
        s_closes, s_highs, s_lows, atr,
        lookback=int(bos_lb),
        min_disp_mult=float(bos_mult),
        use_wicks=bos_wicks,
        require_cross=bos_req_cross,
    )
    if bos_type is None:
        last_seen_bos_index[symbol] = None
        _htf_mismatch_warned.pop((symbol, None), None)
        _gate_hit(symbol, "bos_none")
        return "HOLD", price, None

    if last_seen_bos_index.get(symbol) != bos_idx:
        # New BOS — clear stale mismatch warnings for this symbol
        _htf_mismatch_warned.pop((symbol, last_seen_bos_index.get(symbol)), None)
        _htf_mismatch_warned.pop((symbol, last_seen_bos_index.get(symbol), "pending"), None)
    last_seen_bos_index[symbol] = bos_idx

    htf_trend = get_htf_trend(symbol)
    if htf_trend is None:
        _gate_hit(symbol, "htf_none")
        return "HOLD", price, None
    # FIX 2 (V13): HTF hard block — unconditional HOLD when HTF ≠ BOS direction
    htf_mismatch = ((htf_trend == "BULL" and bos_type == "BOS_SELL") or
                    (htf_trend == "BEAR" and bos_type == "BOS_BUY"))
    if htf_mismatch:
        warn_key = (symbol, bos_idx)
        if warn_key not in _htf_mismatch_warned:
            cp("WARNING", f"⚠️  {symbol} | HTF mismatch (HTF={htf_trend} vs BOS={bos_type}) — counter-trend blocked")
            logger.info("%s HTF mismatch counter-trend blocked | HTF=%s BOS=%s", symbol, htf_trend, bos_type)
            _htf_mismatch_warned[warn_key] = True
        _gate_hit(symbol, "htf_mismatch")
        return "HOLD", price, None

    # FIX 9 (V13): Disable Asia entries for XAUUSD (Asia session loses consistently)
    if symbol == "XAUUSD" and in_asia_session(utc_now):
        _gate_hit(symbol, "asia_disabled")
        return "HOLD", price, None
    
    direction = "BUY" if bos_type == "BOS_BUY" else "SELL"

    impulse_low, impulse_high = get_structural_impulse(s_highs, s_lows, bos_type, bos_idx)
    pull_low, pull_high       = pullback_zone_from_impulse(direction, impulse_low, impulse_high)

    sweep        = detect_sweep_improved(s_highs, s_lows, s_closes)
    sl_mult      = get_sl_atr_multiplier(utc_now)

    c       = SYMBOL_CONFIG[symbol]
    sl_dist = max(c["sl_pips"] * c["pip_value"], atr * 0.8 * sl_mult)
    prov_sl = price - sl_dist if direction == "BUY" else price + sl_dist
    liquidity_tp = detect_liquidity_target(symbol, direction, rates_signal, price, prov_sl)

    meta_base = {
        "liquidity_tp": liquidity_tp, "zone_low":  pull_low,
        "zone_high":    pull_high,    "atr":       atr,
        "sl_mult":      sl_mult,
    }

    pb_buf_mult    = float(SYMBOL_CONFIG.get(symbol, {}).get("pb_entry_buffer_atr_mult", 0.0))
    cont_trig_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("cont_trigger_atr_mult", CONTINUATION_THRESHOLD_MULT))

    score_min_breakout     = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_breakout", SCORE_MIN_BREAKOUT))
    score_min_pullback     = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_pullback", SCORE_MIN_PULLBACK))
    score_min_continuation = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_continuation", SCORE_MIN_CONTINUATION))

    # -----------------------------------------------------------
    # MODE 1 — BREAKOUT (fresh BOS only)
    # -----------------------------------------------------------
    asia = in_asia_session(utc_now)   # computed once, reused across all three modes
    if is_fresh_bos(symbol, bos_idx):
        if displacement >= atr * BREAKOUT_DISP_MULT:
            if bos_candle_quality_ok(rates_signal, bos_idx, direction, symbol=symbol):
                # FIX A — rates_ok() guard replaces `if rates_entry_r`
                rates_entry_r = mt5.copy_rates_from_pos(symbol, ENTRY_TF, 0, 5)
                if rates_ok(rates_entry_r, 3):
                    if candle_confirmation(direction, rates_entry_r, atr_entry, is_asia=asia, symbol=symbol):
                        score = trade_quality_score(sweep, displacement, atr, session_ok,
                                                    False, liquidity_tp, killzone, "breakout", symbol=symbol)
                        kz_str = f" | KZ={killzone}" if killzone else ""
                        cp("BREAKOUT" if direction == "BUY" else "SELL",
                           f"{symbol} | BREAKOUT | {direction} | BOS[{bos_idx}] "
                           f"| DISP={displacement:.5f} | SCORE={score} | HTF={htf_trend}{kz_str}")
                        logger.info("%s | BREAKOUT | %s | BOS[%d] | DISP=%.5f | SCORE=%d | HTF=%s | KZ=%s",
                                    symbol, direction, bos_idx, displacement, score, htf_trend, killzone)
                        base_min  = score_min_breakout
                        score_min = base_min + (1 if atr_low_quality_filter else 0)
                        if score >= score_min:
                            pending_pulls.pop(symbol, None)
                            return direction, price, {**meta_base, "score": score, "entry_mode": "breakout"}
                        if atr_low_quality_filter and score >= base_min:
                            _gate_hit(symbol, "atr_regime")
                        else:
                            _gate_hit(symbol, "score_below")
                    else:
                        _gate_hit(symbol, "candle_confirm")
                else:
                    _gate_hit(symbol, "rates_entry")
            else:
                cp("WARNING", f"⚠️  {symbol} | BREAKOUT blocked — weak BOS candle")
                logger.info("%s | BREAKOUT blocked — candle quality", symbol)
                _gate_hit(symbol, "bos_quality")

        # Store pending setup for pullback / continuation evaluation
        bos_level = float(s_closes[bos_idx])
        if bos_wicks:
            bos_level = float(s_highs[bos_idx]) if bos_type == "BOS_BUY" else float(s_lows[bos_idx])

        pending_pulls[symbol] = {
            "direction":    direction,    "zone_low":     pull_low,
            "zone_high":    pull_high,    "bos_type":     bos_type,
            "bos_idx":      bos_idx,      "displacement": displacement,
            "sweep":        sweep,        "atr":          atr,
            "session_ok":   session_ok,   "killzone":     killzone,
            "bos_level":    bos_level,
            "impulse_low":  impulse_low,
            "impulse_high": impulse_high,
            "timestamp":    time.time(),
        }
        return "HOLD", price, None

    # -----------------------------------------------------------
    # ZONE INVALIDATION CHECK
    # FIX C — do NOT reset last_bos_index to None on invalidation.
    # Resetting caused the detect→store→invalidate→reset→detect loop.
    # Clearing pending_pulls is sufficient; the stale BOS guard
    # ensures a new BOS candle is required before anything fires.
    # -----------------------------------------------------------
    pending = pending_pulls.get(symbol)
    if pending:
        _stored_atr = pending.get("atr", atr)
        _pdir       = pending.get("direction", direction)
        if impulse_is_broken(
            _pdir,
            pending.get("impulse_low"),
            pending.get("impulse_high"),
            price,
            _stored_atr,
            buffer_mult=0.25,
        ):
            buf = _stored_atr * 0.25
            cp("WARNING",
               f"⚠️  {symbol} | structure invalidated | clearing pending")
            logger.info(
                "%s structure invalidated | dir=%s | price=%.5f | impulse_low=%s | impulse_high=%s | buf=%.5f | clearing pending (BOS idx retained)",
                symbol,
                _pdir,
                price,
                f"{pending.get('impulse_low'):.5f}" if pending.get("impulse_low") is not None else "None",
                f"{pending.get('impulse_high'):.5f}" if pending.get("impulse_high") is not None else "None",
                buf,
            )
            pending_pulls.pop(symbol, None)
            _gate_hit(symbol, "invalidated")
            return "HOLD", price, None

    # -----------------------------------------------------------
    # MODE 2 — PULLBACK (price inside 38.2–61.8% zone)
    # -----------------------------------------------------------
    pb_buf = atr * pb_buf_mult
    if (pull_low - pb_buf) <= price <= (pull_high + pb_buf):
        max_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("pb_exhaustion_atr_mult", PB_EXHAUSTION_ATR_MULT))
        if not pullback_impulse_exhaustion_ok(symbol, impulse_low, impulse_high, atr):
            imp_rng = impulse_high - impulse_low
            cp("WARNING",
               f"⚠️  {symbol} | PULLBACK blocked — impulse exhausted "
               f"(range={imp_rng:.5f} > {max_mult:.1f}×ATR={atr*max_mult:.5f})")
            logger.info("%s | PULLBACK blocked — impulse exhausted | range=%.5f > %.1f×ATR=%.5f",
                        symbol, imp_rng, max_mult, atr * max_mult)
            _gate_hit(symbol, "pb_exhaustion")
            return "HOLD", price, None
        # FIX A — rates_ok() guard
        rates_entry_r = mt5.copy_rates_from_pos(symbol, ENTRY_TF, 0, 5)
        if rates_ok(rates_entry_r, 3):
            if candle_confirmation(direction, rates_entry_r, atr_entry, is_asia=asia, symbol=symbol):
                score = trade_quality_score(sweep, displacement, atr, session_ok,
                                            True, liquidity_tp, killzone, "pullback", symbol=symbol)
                kz_str  = f" | KZ={killzone}" if killzone else ""
                sig_key = "BUY" if direction == "BUY" else "SELL"
                cp(sig_key,
                   f"{symbol} | PULLBACK | {direction} | BOS[{bos_idx}] "
                   f"| DISP={displacement:.5f} | SCORE={score} | HTF={htf_trend}{kz_str}")
                logger.info("%s | PULLBACK | %s | BOS[%d] | DISP=%.5f | SCORE=%d | HTF=%s | KZ=%s",
                            symbol, direction, bos_idx, displacement, score, htf_trend, killzone)
                base_min  = score_min_pullback
                score_min = base_min + (1 if atr_low_quality_filter else 0)
                if score >= score_min:
                    pending_pulls.pop(symbol, None)
                    return direction, price, {**meta_base, "score": score, "entry_mode": "pullback"}
                if atr_low_quality_filter and score >= base_min:
                    _gate_hit(symbol, "atr_regime")
                else:
                    _gate_hit(symbol, "score_below")
            else:
                _gate_hit(symbol, "candle_confirm")
        else:
            _gate_hit(symbol, "rates_entry")

    # -----------------------------------------------------------
    # MODE 3 — CONTINUATION (trending past BOS without pullback)
    # FIX D — prefer bos_level from pending setup if available,
    # so evaluation is consistent across multiple loops.
    # -----------------------------------------------------------
    if pending and "bos_level" in pending:
        bos_level = pending["bos_level"]
    else:
        bos_level = float(s_closes[bos_idx])
        if bos_wicks:
            bos_level = float(s_highs[bos_idx]) if bos_type == "BOS_BUY" else float(s_lows[bos_idx])

    if direction == "SELL":
        cont_triggered = price < bos_level - atr * cont_trig_mult
    else:
        cont_triggered = price > bos_level + atr * cont_trig_mult

    if cont_triggered:
        cont_key = (symbol, bos_idx)

        if cont_fired.get(cont_key, False):
            logger.debug("%s continuation already fired for BOS[%d]", symbol, bos_idx)
            _gate_hit(symbol, "cont_already")
            return "HOLD", price, None

        if not continuation_distance_ok(direction, bos_level, price, atr):
            cp("WARNING",
               f"⚠️  {symbol} | CONTINUATION blocked — too far from BOS (exhaustion risk)")
            logger.info("%s | CONTINUATION blocked — distance > %.1f×ATR", symbol, CONT_MAX_DIST_MULT)
            _gate_hit(symbol, "cont_distance")
            return "HOLD", price, None

        dist = abs(price - bos_level)
        if dist > atr * CONT_MOMENTUM_MIN_DIST_MULT:
            if not continuation_momentum_ok(list(rates_signal)):
                cp("WARNING",
                  f"⚠️  {symbol} | CONTINUATION blocked — momentum fading (candles shrinking)")
                logger.info("%s | CONTINUATION blocked — momentum < %.2f", symbol, CONT_MOMENTUM_RATIO)
                _gate_hit(symbol, "cont_momentum")
                return "HOLD", price, None

        # FIX A — rates_ok() guard
        rates_entry_r = mt5.copy_rates_from_pos(symbol, ENTRY_TF, 0, 5)
        if rates_ok(rates_entry_r, 3):
            if candle_confirmation(direction, rates_entry_r, atr_entry, is_asia=asia, symbol=symbol):
                score = trade_quality_score(sweep, displacement, atr, session_ok,
                                            False, liquidity_tp, killzone, "continuation", symbol=symbol)
                dist   = abs(price - bos_level)
                kz_str = f" | KZ={killzone}" if killzone else ""
                cp("CONTINUATION",
                   f"{symbol} | CONTINUATION | {direction} | BOS[{bos_idx}] "
                   f"| DIST={dist:.5f} | SCORE={score} | HTF={htf_trend}{kz_str}")
                logger.info("%s | CONTINUATION | %s | BOS[%d] | DIST=%.5f | SCORE=%d | HTF=%s | KZ=%s",
                            symbol, direction, bos_idx, dist, score, htf_trend, killzone)
                base_min  = score_min_continuation
                score_min = base_min + (1 if atr_low_quality_filter else 0)
                if score >= score_min:
                    cont_fired[cont_key] = True
                    pending_pulls.pop(symbol, None)
                    return direction, price, {**meta_base, "score": score, "entry_mode": "continuation"}
                if atr_low_quality_filter and score >= base_min:
                    _gate_hit(symbol, "atr_regime")
                else:
                    _gate_hit(symbol, "score_below")
            else:
                _gate_hit(symbol, "candle_confirm")
        else:
            _gate_hit(symbol, "rates_entry")

    return "HOLD", price, None

# =========================
# PENDING PULLBACK MONITOR
# =========================
PENDING_TTL = 300   # global fallback — overridden per-symbol via SYMBOL_CONFIG["pending_ttl"]

def check_pending_pullbacks():
    expired = []
    utc_now = get_utc_time()

    for symbol, setup in list(pending_pulls.items()):
        # FIX 3 (V12.2c) — per-symbol TTL: BTC Asia setups need up to 15 mins
        ttl = SYMBOL_CONFIG[symbol].get("pending_ttl", PENDING_TTL)
        if time.time() - setup["timestamp"] > ttl:
            cp("DIAG", f"└ [PENDING] {symbol} setup expired after {ttl}s | clearing")
            logger.debug("[PENDING] %s expired after %ds", symbol, ttl)
            expired.append(symbol)
            _gate_hit(symbol, "pending_ttl")
            continue
        if in_rollover_block(symbol, utc_now):
            _gate_hit(symbol, "rollover")
            continue
        if not market_open(symbol) or not check_spread_acceptable(symbol):
            _gate_hit(symbol, "market_closed" if not market_open(symbol) else "spread")
            continue

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            _gate_hit(symbol, "tick_none")
            continue

        direction = setup["direction"]
        price     = tick.ask if direction == "BUY" else tick.bid
        atr       = setup.get("atr") or get_atr(symbol, SIGNAL_TF)
        if atr is None:
            _gate_hit(symbol, "atr_none")
            continue

        bos_level = setup.get("bos_level")
        if bos_level is None:
            bos_level = (setup.get("zone_low", price) + setup.get("zone_high", price)) / 2.0

        _stored_atr = setup.get("atr", atr)
        inv_price   = tick.bid if direction == "BUY" else tick.ask
        if impulse_is_broken(
            direction,
            setup.get("impulse_low"),
            setup.get("impulse_high"),
            inv_price,
            _stored_atr,
            buffer_mult=0.25,
        ):
            buf = _stored_atr * 0.25
            cp("WARNING",
               f"⚠️  {symbol} | pending structure invalidated | clearing")
            logger.info(
                "%s pending structure invalidated | dir=%s | inv_price=%.5f | impulse_low=%s | impulse_high=%s | buf=%.5f",
                symbol,
                direction,
                inv_price,
                f"{setup.get('impulse_low'):.5f}" if setup.get("impulse_low") is not None else "None",
                f"{setup.get('impulse_high'):.5f}" if setup.get("impulse_high") is not None else "None",
                buf,
            )
            expired.append(symbol)
            _gate_hit(symbol, "invalidated")
            continue

        rates_signal = mt5.copy_rates_from_pos(symbol, SIGNAL_TF, 0, 80)
        if not rates_ok(rates_signal, 40):
            _gate_hit(symbol, "rates_signal")
            continue

        killzone = in_killzone(utc_now)
        sl_mult  = get_sl_atr_multiplier(utc_now)
        c        = SYMBOL_CONFIG[symbol]
        sl_dist  = max(c["sl_pips"] * c["pip_value"], atr * 0.8 * sl_mult)
        prov_sl  = price - sl_dist if direction == "BUY" else price + sl_dist
        liquidity_tp = detect_liquidity_target(symbol, direction, rates_signal, price, prov_sl)

        pb_buf_mult    = float(SYMBOL_CONFIG.get(symbol, {}).get("pb_entry_buffer_atr_mult", 0.0))
        cont_trig_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("cont_trigger_atr_mult", CONTINUATION_THRESHOLD_MULT))
        pb_buf = atr * pb_buf_mult

        in_zone   = (setup["zone_low"] - pb_buf) <= price <= (setup["zone_high"] + pb_buf)
        cont_check = (price < bos_level - atr * cont_trig_mult
                      if direction == "SELL"
                      else price > bos_level + atr * cont_trig_mult)

        if in_zone or cont_check:
            asia        = in_asia_session(utc_now)
            rates_entry = mt5.copy_rates_from_pos(symbol, ENTRY_TF, 0, 5)
            if not rates_ok(rates_entry, 3):
                _gate_hit(symbol, "rates_entry")
                continue
            atr_entry = get_entry_atr(symbol) or atr
            if not candle_confirmation(direction, rates_entry, atr_entry, is_asia=asia, symbol=symbol):
                logger.debug("[PENDING] %s | candle_confirmation FAIL | %s | asia=%s",
                             symbol, direction, asia)
                _gate_hit(symbol, "candle_confirm")
                continue

        htf_trend = get_htf_trend(symbol)
        if htf_trend is None:
            _gate_hit(symbol, "htf_none")
            continue
        # FIX 2 (V13): HTF hard block in pending checks too
        htf_mismatch = ((htf_trend == "BULL" and direction == "SELL") or
                        (htf_trend == "BEAR" and direction == "BUY"))
        if htf_mismatch:
            pend_bos = setup.get("bos_idx", -1)
            warn_key = (symbol, pend_bos, "pending")
            if warn_key not in _htf_mismatch_warned:
                cp("WARNING", f"⚠️  {symbol} | HTF mismatch (HTF={htf_trend} vs {direction}) — counter-trend pending blocked")
                logger.info("%s pending HTF mismatch counter-trend blocked | HTF=%s dir=%s", symbol, htf_trend, direction)
                _htf_mismatch_warned[warn_key] = True
            _gate_hit(symbol, "htf_mismatch")
            continue

        if in_zone:
            imp_lo = setup.get("impulse_low")
            imp_hi = setup.get("impulse_high")
            if imp_lo is not None and imp_hi is not None:
                if not pullback_impulse_exhaustion_ok(symbol, imp_lo, imp_hi, atr):
                    imp_rng = imp_hi - imp_lo
                    cp("WARNING",
                       f"⚠️  {symbol} | PENDING PULLBACK blocked — impulse exhausted "
                       f"(range={imp_rng:.2f})")
                    logger.info("%s | PENDING PULLBACK blocked — impulse exhausted | range=%.5f",
                                symbol, imp_rng)
                    _gate_hit(symbol, "pb_exhaustion")
                    continue
            entry_mode = "pullback"
            score_min  = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_pullback", SCORE_MIN_PULLBACK))
        elif cont_check:
            cont_key = (symbol, setup.get("bos_idx", -1))
            if cont_fired.get(cont_key, False):
                _gate_hit(symbol, "cont_already")
                continue
            if not continuation_distance_ok(direction, bos_level, price, atr):
                _gate_hit(symbol, "cont_distance")
                continue
            dist = abs(price - bos_level)
            if dist > atr * CONT_MOMENTUM_MIN_DIST_MULT:
                if not continuation_momentum_ok(list(rates_signal)):
                    _gate_hit(symbol, "cont_momentum")
                    continue
            entry_mode = "continuation"
            score_min  = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_continuation", SCORE_MIN_CONTINUATION))
        else:
            continue

        score = trade_quality_score(
            setup.get("sweep"), setup["displacement"], atr,
            setup.get("session_ok", False), in_zone, liquidity_tp, killzone, entry_mode,
            symbol=symbol,
        )

        color_key = "CONTINUATION" if entry_mode == "continuation" else (
            "BUY" if direction == "BUY" else "SELL")
        cp(color_key,
           f"[PENDING→{entry_mode.upper()}] {symbol} | {direction} | SCORE={score}")
        logger.info("[PENDING→%s] %s | %s | SCORE=%d", entry_mode.upper(), symbol, direction, score)

        if score >= score_min:
            if entry_mode == "continuation":
                cont_fired[(symbol, setup.get("bos_idx", -1))] = True
            expired.append(symbol)
            execute_trade(symbol, direction, {
                "liquidity_tp": liquidity_tp, "zone_low":   setup["zone_low"],
                "zone_high":    setup["zone_high"], "score": score,
                "atr":          atr,               "sl_mult": sl_mult,
                "entry_mode":   entry_mode,
            })
        else:
            _gate_hit(symbol, "score_below")

    for sym in expired:
        pending_pulls.pop(sym, None)

# =========================
# ORDER / RISK
# =========================
def order_send_with_retry(req, retries=2):
    res = None
    for _ in range(retries + 1):
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            return res
        if res and res.retcode in [
            mt5.TRADE_RETCODE_REQUOTE,
            mt5.TRADE_RETCODE_PRICE_CHANGED,
            mt5.TRADE_RETCODE_NO_PRICES,
        ]:
            time.sleep(0.25)
            tick = mt5.symbol_info_tick(req["symbol"])
            if tick:
                req["price"] = tick.ask if req["type"] == mt5.ORDER_TYPE_BUY else tick.bid
            continue
        return res
    return res

def normalize_price(symbol, price):
    info = mt5.symbol_info(symbol)
    if info is None:
        return price
    return round(price, info.digits)

def validate_sl_tp(symbol, entry, sl, tp, direction):
    info = mt5.symbol_info(symbol)
    if info is None:
        return False, "missing_symbol_info"
    point    = info.point
    min_dist = max(
        (info.trade_stops_level  or 0) * point,
        (info.trade_freeze_level or 0) * point,
    )
    if direction == "BUY":
        if sl >= entry or tp <= entry: return False, "invalid_buy_levels"
    else:
        if sl <= entry or tp >= entry: return False, "invalid_sell_levels"
    if abs(entry - sl) < min_dist: return False, f"sl_too_close_min={min_dist}"
    if abs(entry - tp) < min_dist: return False, f"tp_too_close_min={min_dist}"
    return True, "ok"

def calculate_lot(symbol, entry_price, sl_price, risk_percent=RISK_PERCENT):
    acc = mt5.account_info()
    sym = mt5.symbol_info(symbol)
    if not acc or not sym:
        return 0.01
    if entry_price <= 0 or sl_price <= 0:
        return sym.volume_min
    risk_money  = acc.equity * min(risk_percent, MAX_RISK_PERCENT)
    sl_distance = abs(entry_price - sl_price)
    if sl_distance == 0:
        return sym.volume_min
    max_lot = min(sym.volume_max, dynamic_max_lot(acc.equity))
    step    = sym.volume_step

    # --- over-risk guard: skip if min lot risks too much ---
    def _min_lot_loss():
        try:
            ts = sym.point
            tv = sym.trade_tick_value
            if ts and tv:
                return (sl_distance / ts * tv) * sym.volume_min
        except Exception:
            pass
        if entry_price > sl_price:
            p = mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, symbol, sym.volume_min, entry_price, sl_price)
        else:
            p = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, sym.volume_min, entry_price, sl_price)
        return abs(p) if p is not None else None

    min_lot_risk = _min_lot_loss()
    if min_lot_risk is not None and min_lot_risk > risk_money * MAX_RISK_MULT:
        logger.warning("%s lot skip | min_lot_risk=$%.2f > %d× budget=$%.2f | equity=%.2f",
                       symbol, min_lot_risk, MAX_RISK_MULT, risk_money, acc.equity)
        cp("WARNING", f"⚠️  {symbol} skipped — min lot (${min_lot_risk:.2f}) > "
                      f"{MAX_RISK_MULT}× risk budget (${risk_money:.2f})")
        return 0  # execute_trade will block on lot <= 0

    try:
        tick_size  = sym.point
        tick_value = sym.trade_tick_value
        if tick_value and tick_size:
            lot = risk_money / (sl_distance / tick_size * tick_value)
            lot = max(sym.volume_min, min(lot, max_lot))
            return round(round(lot / step) * step, 2)
    except Exception:
        pass
    def simulate_loss(test_lot):
        if entry_price > sl_price:
            profit = mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, symbol, test_lot, entry_price, sl_price)
        else:
            profit = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY,  symbol, test_lot, entry_price, sl_price)
        return float("inf") if profit is None else abs(profit)
    lot = best_lot = sym.volume_min
    while lot <= max_lot:
        if simulate_loss(lot) > risk_money:
            break
        best_lot = lot
        lot = round(lot + step, 2)
    return round(max(sym.volume_min, min(best_lot, max_lot)) / step * step, 2)

def dynamic_max_lot(equity):
    if equity < 1000:  return 0.05
    if equity < 5000:  return 0.10
    if equity < 10000: return 0.20
    return 0.30

def calculate_sl_tp_liquidity(symbol, direction, entry, liquidity_tp, atr, sl_mult, entry_mode,
                              zone_low=None, zone_high=None, utc_dt=None):
    c           = SYMBOL_CONFIG[symbol]
    sym_min_rr  = float(c.get("min_rr", MIN_RR))
    atr_sl_mult = 0.6 if entry_mode == "continuation" else 0.8

    buf_k = 0.25
    if utc_dt is not None:
        if in_killzone(utc_dt):
            buf_k = 0.20
        elif in_asia_session(utc_dt):
            buf_k = 0.35

    # FIX 4 (V13): Min SL floor per symbol to prevent whipsaws during low-vol periods
    min_sl_distance = float(c.get("min_sl_distance", 0.0))
    sl_distance = max(c["sl_pips"] * c["pip_value"], atr * atr_sl_mult * sl_mult, min_sl_distance)
    if direction == "BUY":
        sl = entry - sl_distance
        if zone_low is not None:
            try:
                sl = min(sl, float(zone_low) - atr * buf_k)
            except Exception:
                pass
        tp = (liquidity_tp if liquidity_tp is not None and liquidity_tp > entry
              else entry + max(atr * c["atr_mult_tp"] * sl_mult, sl_distance * sym_min_rr))
    else:
        sl = entry + sl_distance
        if zone_high is not None:
            try:
                sl = max(sl, float(zone_high) + atr * buf_k)
            except Exception:
                pass
        tp = (liquidity_tp if liquidity_tp is not None and liquidity_tp < entry
              else entry - max(atr * c["atr_mult_tp"] * sl_mult, sl_distance * sym_min_rr))
    return normalize_price(symbol, sl), normalize_price(symbol, tp)

# =========================
# TRADE EXECUTION
# =========================
def execute_trade(symbol, direction, meta):
    global daily_trade_counts

    if daily_halt:
        logger.info("%s trade blocked — daily DD halt", symbol)
        _gate_hit(symbol, "daily_halt")
        return
    if daily_trade_counts.get(symbol, 0) >= MAX_TRADES_PER_DAY:
        cp("WARNING", f"⚠️  {symbol} daily trade limit ({MAX_TRADES_PER_DAY}) reached")
        _gate_hit(symbol, "daily_limit")
        return

    c          = SYMBOL_CONFIG[symbol]
    now        = time.time()
    entry_mode = meta.get("entry_mode", "pullback")
    
    # FIX 10 (V13): Score validation — block trades where score < required minimum
    score = meta.get("score", 0)
    if entry_mode == "breakout":
        score_min = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_breakout", SCORE_MIN_BREAKOUT))
    elif entry_mode == "continuation":
        score_min = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_continuation", SCORE_MIN_CONTINUATION))
    else:  # pullback
        score_min = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_pullback", SCORE_MIN_PULLBACK))
    
    if score < score_min:
        cp("WARNING", f"⚠️  {symbol} score validation failed | score={score} < {score_min} for {entry_mode}")
        logger.warning("%s score validation blocked | score=%d < %d for %s", symbol, score, score_min, entry_mode)
        _gate_hit(symbol, "score_validation")
        return

    if c.get("session_gate", False):
        utc_now = get_utc_time()
        if not in_session(symbol, utc_now) and in_killzone(utc_now) is None:
            cp("WARNING", f"⚠️  {symbol} | session gate — outside session window, trade blocked")
            logger.info("%s session gate blocked | dir=%s | mode=%s", symbol, direction, entry_mode)
            _gate_hit(symbol, "session_gate")
            return

    if (signal_cooldown.get(symbol) == direction
            and now - last_trade_time.get(symbol, 0) < c["cooldown"]):
        _gate_hit(symbol, "cooldown")
        return
    open_pos = mt5.positions_get(symbol=symbol) or []
    if len(open_pos) >= c["max_trades"]:
        cp("WARNING", f"⚠️  {symbol} max open positions ({c['max_trades']}) reached")
        _gate_hit(symbol, "maxtrades")
        return
    # Block stacking: no second position in the same direction on the same symbol
    same_dir = [p for p in open_pos
                if (direction == "BUY" and p.type == mt5.ORDER_TYPE_BUY) or
                   (direction == "SELL" and p.type == mt5.ORDER_TYPE_SELL)]
    if same_dir:
        cp("WARNING",
           f"⚠️  {symbol} | already have {len(same_dir)} {direction} position(s) open — blocked")
        logger.info("%s same-direction block | dir=%s | existing=%d", symbol, direction, len(same_dir))
        _gate_hit(symbol, "same_direction")
        return
    if not ensure_symbol(symbol):
        _gate_hit(symbol, "ensure_symbol")
        return
    if not check_spread_acceptable(symbol):
        cp("WARNING", f"⚠️  {symbol} spread too wide at execution")
        _gate_hit(symbol, "spread")
        return

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        cp("ERROR", f"❌ No tick for {symbol}")
        _gate_hit(symbol, "tick_none")
        return

    utc_dt  = get_utc_time()
    entry   = tick.ask if direction == "BUY" else tick.bid
    atr     = meta.get("atr") or get_atr(symbol, SIGNAL_TF)
    sl_mult = meta.get("sl_mult", get_sl_atr_multiplier(utc_dt))
    if atr is None:
        _gate_hit(symbol, "atr_none")
        return

    sl, tp  = calculate_sl_tp_liquidity(
        symbol, direction, entry,
        meta.get("liquidity_tp"), atr, sl_mult, entry_mode,
        zone_low=meta.get("zone_low"),
        zone_high=meta.get("zone_high"),
        utc_dt=utc_dt,
    )
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)

    sym_min_rr = float(SYMBOL_CONFIG[symbol].get("min_rr", MIN_RR))
    if tp_dist < sl_dist * sym_min_rr:
        cp("WARNING", f"⚠️  {symbol} RR={tp_dist/sl_dist:.2f}R < {sym_min_rr}R — skip")
        _gate_hit(symbol, "rr")
        return

    ok, reason = validate_sl_tp(symbol, entry, sl, tp, direction)
    if not ok:
        cp("ERROR", f"❌ SL/TP invalid {symbol} | {reason}")
        _gate_hit(symbol, "sltp")
        return

    lot = calculate_lot(symbol, entry, sl)
    if lot <= 0:
        cp("ERROR", f"❌ Invalid lot for {symbol}")
        _gate_hit(symbol, "lot")
        return

    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       lot,
        "type":         mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
        "price":        entry,
        "sl":           sl,
        "tp":           tp,
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      f"SMC V12.2 {entry_mode[:4]}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = order_send_with_retry(req, retries=2)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        rr           = round(tp_dist / sl_dist, 2) if sl_dist else 0
        actual_entry = result.price if hasattr(result, "price") and result.price else entry
        slippage     = abs(actual_entry - entry)

        if slippage > 0:
            cp("SLIPPAGE",
               f"⚠️  [SLIP] {symbol} req={entry:.5f} fill={actual_entry:.5f} slip={slippage:.5f}")
            logger.warning("[SLIP] %s req=%.5f fill=%.5f slip=%.5f", symbol, entry, actual_entry, slippage)

        mode_key = ("BREAKOUT"     if entry_mode == "breakout"     else
                    "CONTINUATION" if entry_mode == "continuation" else
                    "BUY"          if direction  == "BUY"          else "SELL")
        cp(mode_key,
           f"[OPEN|{entry_mode.upper()}] {direction} | {symbol} | Ticket:{result.order} "
           f"| Entry:{actual_entry:.5f} | SL:{sl:.5f} | TP:{tp:.5f} | RR:{rr}R")
        logger.info("[OPEN|%s] %s | Ticket:%s | Entry:%.5f | SL:%.5f | TP:%.5f | RR:%.2fR",
                    entry_mode.upper(), direction, result.order, actual_entry, sl, tp, rr)

        r1_level[result.order]            = (entry + sl_dist if direction == "BUY"
                                              else entry - sl_dist)
        be_locked[result.order]           = False
        partial_done[result.order]        = False
        peak_profit_tracker[result.order] = 0.0
        open_trades[result.order]         = {
            "symbol":     symbol,       "type":       direction,
            "entry":      actual_entry, "sl":         sl,
            "tp":         tp,           "lot":        lot,
            "risk_1r":    sl_dist,      "entry_mode": entry_mode,
            "open_utc":   utc_dt,
            "session":    _session_tag_for_time(symbol, utc_dt),
            "partial_r":  (PARTIAL_TP_R_ASIA if in_asia_session(utc_dt) else PARTIAL_TP_R_PRIME),
            "trail_adj":  (0.85 if in_killzone(utc_dt) else (1.15 if in_asia_session(utc_dt) else 1.0)),
        }
        last_trade_time[symbol]    = now
        signal_cooldown[symbol]    = direction
        daily_trade_counts[symbol] = daily_trade_counts.get(symbol, 0) + 1
        cp("DAILY", f"📊 {symbol} trades today: {daily_trade_counts[symbol]}/{MAX_TRADES_PER_DAY}")
    else:
        _gate_hit(symbol, "order_fail")
        log_order_result(result, "open_trade")

# =========================
# TRADE MANAGEMENT
# =========================
def close_position(pos, reason):
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        cp("ERROR", f"❌ No tick to close {pos.symbol}")
        return
    typ   = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid            if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    req   = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
        "type": typ, "position": pos.ticket, "price": price, "deviation": DEVIATION,
        "magic": MAGIC, "comment": f"close:{reason}",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = order_send_with_retry(req, retries=2)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        cp("CLOSE", f"[CLOSE] {pos.symbol} | Ticket:{pos.ticket} | {reason} | P/L:{pos.profit:.2f}")
        logger.info("[CLOSE] %s | Ticket:%s | %s | P/L:%.2f",
                    pos.symbol, pos.ticket, reason, pos.profit)
        for d in (open_trades, be_locked, r1_level, peak_profit_tracker, partial_done):
            d.pop(pos.ticket, None)
    else:
        log_order_result(res, "close_position")

def try_partial_close(pos):
    if partial_done.get(pos.ticket, False):
        return
    if not SYMBOL_CONFIG.get(pos.symbol, {}).get("mgmt_partial_enabled", True):
        return
    trade_meta = open_trades.get(pos.ticket, {})
    risk_1r    = trade_meta.get("risk_1r", abs(pos.price_open - pos.sl))
    if risk_1r == 0:
        return
    partial_r = float(trade_meta.get("partial_r", PARTIAL_TP_R))
    partial_target = (pos.price_open + risk_1r * partial_r if pos.type == mt5.ORDER_TYPE_BUY
                      else pos.price_open - risk_1r * partial_r)
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        return
    current = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    if pos.type == mt5.ORDER_TYPE_BUY  and current < partial_target: return
    if pos.type == mt5.ORDER_TYPE_SELL and current > partial_target: return
    sym = mt5.symbol_info(pos.symbol)
    if sym is None:
        return
    close_vol = round(pos.volume * PARTIAL_CLOSE_PCT / sym.volume_step) * sym.volume_step
    close_vol = max(sym.volume_min, min(close_vol, pos.volume - sym.volume_min))
    close_vol = round(close_vol, 2)
    if close_vol <= 0:
        return
    typ   = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid            if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    req   = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": close_vol,
        "type": typ, "position": pos.ticket, "price": price, "deviation": DEVIATION,
        "magic": MAGIC, "comment": "partial_tp",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = order_send_with_retry(req, retries=2)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        partial_done[pos.ticket] = True
        runner = round(pos.volume - close_vol, 2)
        cp("PARTIAL_TP",
           f"[PARTIAL_TP] {pos.symbol} | Ticket:{pos.ticket} | "
           f"{close_vol} lots @ {price:.5f} ({PARTIAL_TP_R}R) | Runner:{runner}")
        logger.info("[PARTIAL_TP] %s | Ticket:%s | closed=%.2f | runner=%.2f",
                    pos.symbol, pos.ticket, close_vol, runner)
        new_sl = normalize_price(pos.symbol, pos.price_open)
        d_str  = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        ok, _  = validate_sl_tp(pos.symbol, pos.price_open, new_sl, pos.tp, d_str)
        if ok:
            mod = mt5.order_send({
                "action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket,
                "symbol": pos.symbol, "sl": new_sl, "tp": pos.tp,
                "magic": MAGIC, "comment": "partial_be",
            })
            if mod and mod.retcode == mt5.TRADE_RETCODE_DONE:
                be_locked[pos.ticket] = True
                cp("BE_LOCK",
                   f"[PARTIAL_BE] {pos.symbol} | Ticket:{pos.ticket} | SL→{new_sl:.5f}")
    else:
        log_order_result(res, "partial_close")

def modify_position_sl_tp(pos, new_sl, new_tp):
    return mt5.order_send({
        "action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket,
        "symbol": pos.symbol, "sl": new_sl, "tp": new_tp,
        "magic": MAGIC, "comment": "SMC V12.2 trail",
    })

def _log_closed_trade(ticket):
    global last_deal_check_utc
    now_utc = datetime.now(pytz.UTC)
    if last_deal_check_utc is None:
        last_deal_check_utc = now_utc - timedelta(minutes=30)

    meta = open_trades.get(ticket, {})
    known_symbol = meta.get("symbol", "?")

    # Widen search window to catch broker-side SL/TP hits
    search_from = min(last_deal_check_utc, now_utc - timedelta(minutes=5))

    # Try to fetch the closing deal for this position ticket.
    deals = None
    try:
        deals = mt5.history_deals_get(search_from, now_utc, position=ticket)
    except Exception:
        deals = None

    # MT5 position filter is unreliable — validate position_id on each deal
    matched = []
    if deals:
        matched = [d for d in deals if getattr(d, "position_id", None) == ticket]
        if len(matched) != len(deals):
            logger.warning("[CLOSE_DIAG] Ticket:%s | MT5 returned %d deals, %d matched position_id "
                           "(filtered out %d stale/wrong deals)",
                           ticket, len(deals), len(matched), len(deals) - len(matched))

    if matched:
        closing = [d for d in matched if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT]
        d = closing[-1] if closing else matched[-1]
        deal_symbol = getattr(d, "symbol", "?")
        profit      = float(getattr(d, "profit", 0.0) or 0.0)
        comment     = getattr(d, "comment", "") or ""
        reason      = getattr(d, "reason", None)
        reason_s    = str(reason) if reason is not None else "?"

        # Use known symbol from open_trades if available; log mismatch
        symbol = known_symbol if known_symbol != "?" else deal_symbol
        if known_symbol != "?" and deal_symbol != known_symbol:
            logger.warning("[CLOSE_DIAG] Ticket:%s | symbol mismatch: open_trades=%s deal=%s — using %s",
                           ticket, known_symbol, deal_symbol, symbol)

        cp("CLOSE", f"[CLOSE] {symbol} | Ticket:{ticket} | deal_reason={reason_s} | P/L:{profit:.2f} | {comment}")
        logger.info("[CLOSE] %s | Ticket:%s | deal_reason=%s | P/L:%.2f | %s",
                    symbol, ticket, reason_s, profit, comment)
    else:
        if deals:
            logger.warning("[CLOSE_DIAG] Ticket:%s | %d deals returned but NONE matched position_id — "
                           "discarding all (would have shown wrong symbol/P&L)", ticket, len(deals))
        cp("CLOSE", f"[CLOSE] {known_symbol} | Ticket:{ticket} | closed (no matching deal history)")
        logger.info("[CLOSE] %s | Ticket:%s | closed (no matching deal history)", known_symbol, ticket)

    last_deal_check_utc = now_utc

def detect_closed_trades():
    existing = {p.ticket for p in (mt5.positions_get() or [])}
    for ticket in list(open_trades.keys()):
        if ticket not in existing:
            _log_closed_trade(ticket)
            for d in (open_trades, be_locked, r1_level, peak_profit_tracker, partial_done):
                d.pop(ticket, None)

def print_positions():
    positions = mt5.positions_get() or []
    if not positions:
        return
    cp("SEPARATOR", "--- ACTIVE POSITIONS ---")
    for p in positions:
        mode   = open_trades.get(p.ticket, {}).get("entry_mode", "?")
        p_str  = " [P✓]"  if partial_done.get(p.ticket) else ""
        be_str = " [BE✓]" if be_locked.get(p.ticket)    else ""
        pl_str = f"+{p.profit:.2f}" if p.profit > 0 else f"{p.profit:.2f}"
        key    = "POS_PROFIT" if p.profit > 0 else "POS_LOSS" if p.profit < 0 else "POS_FLAT"
        arrow  = "▲" if p.profit > 0 else "▼" if p.profit < 0 else "—"
        cp(key, f"  {arrow} {p.symbol} | Ticket:{p.ticket} | P/L:{pl_str} | Mode:{mode}{p_str}{be_str}")
        logger.info("%s | Ticket:%s | P/L:%.2f | Mode:%s | P:%s | BE:%s",
                    p.symbol, p.ticket, p.profit, mode,
                    partial_done.get(p.ticket, False), be_locked.get(p.ticket, False))
    cp("SEPARATOR", "------------------------")

def manage_trades():
    positions = mt5.positions_get() or []
    if not positions:
        return
    print_positions()

    acc       = mt5.account_info()
    basket_tp =  acc.equity * BASKET_TP_PCT  if acc else  800
    basket_sl = -acc.equity * BASKET_SL_PCT  if acc else -200

    total_profit = sum(p.profit for p in positions)
    if total_profit >= basket_tp or total_profit <= basket_sl:
        cp("BASKET",
           f"[BASKET CLOSE] P/L:{total_profit:.2f} | TP:{basket_tp:.2f} | SL:{basket_sl:.2f}")
        logger.warning("[BASKET CLOSE] P/L:%.2f | TP:%.2f | SL:%.2f",
                       total_profit, basket_tp, basket_sl)
        for pos in positions:
            close_position(pos, "BASKET")
        return

    for pos in positions:
        if pos.symbol not in SYMBOL_CONFIG:
            continue
        try_partial_close(pos)

        peak_profit_tracker[pos.ticket] = max(
            peak_profit_tracker.get(pos.ticket, pos.profit), pos.profit)
        peak    = peak_profit_tracker[pos.ticket]
        current = pos.profit

        trade_meta = open_trades.get(pos.ticket, {})
        risk_1r    = trade_meta.get("risk_1r", abs(pos.price_open - pos.sl))
        acc_now    = mt5.account_info()
        r1_profit  = mt5.order_calc_profit(
            mt5.ORDER_TYPE_BUY if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_SELL,
            pos.symbol, pos.volume,
            pos.price_open,
            pos.price_open + risk_1r if pos.type == mt5.ORDER_TYPE_BUY else pos.price_open - risk_1r,
        ) or ((acc_now.equity if acc_now else 1.0) * RISK_PERCENT)

        sym_cfg = SYMBOL_CONFIG.get(pos.symbol, {})
        if sym_cfg.get("mgmt_lockin_enabled", True):
            lockin_r  = sym_cfg.get("mgmt_lockin_r", LOCK_IN_R_MULTIPLE)
            lockin_db = sym_cfg.get("mgmt_lockin_drawback", LOCK_IN_DRAWBACK)
            if peak >= r1_profit * lockin_r and current < peak * lockin_db:
                cp("LOCK_IN",
                   f"[LOCK_IN] {pos.symbol} | Ticket:{pos.ticket} | Peak:{peak:.2f} | Now:{current:.2f}")
                logger.info("[LOCK_IN] %s | Ticket:%s | Peak:%.2f | Now:%.2f",
                            pos.symbol, pos.ticket, peak, current)
                close_position(pos, "LOCK_IN")
                continue

        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick:
            continue

        if sym_cfg.get("mgmt_time_stop_enabled", True):
            trade_meta = open_trades.get(pos.ticket, {})
            open_utc   = trade_meta.get("open_utc")
            if isinstance(open_utc, datetime):
                age_s = (get_utc_time() - open_utc).total_seconds()
                risk_1r = trade_meta.get("risk_1r", abs(pos.price_open - pos.sl))
                if risk_1r and risk_1r > 0:
                    current_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                    progress_r = ((current_price - pos.price_open) / risk_1r
                                  if pos.type == mt5.ORDER_TYPE_BUY
                                  else (pos.price_open - current_price) / risk_1r)

                    utc_now = get_utc_time()
                    timeout_min = TIME_STOP_DEFAULT_MINUTES
                    if in_killzone(utc_now):
                        timeout_min = TIME_STOP_KILLZONE_MINUTES
                    elif in_asia_session(utc_now):
                        timeout_min = TIME_STOP_ASIA_MINUTES

                    ts_min_r = sym_cfg.get("mgmt_time_stop_min_r", TIME_STOP_MIN_PROGRESS_R)
                    if age_s >= timeout_min * 60 and progress_r < ts_min_r:
                        cp("CLOSE", f"[TIME_STOP] {pos.symbol} | Ticket:{pos.ticket} | age={int(age_s/60)}m | R={progress_r:.2f}")
                        logger.info("[TIME_STOP] %s | Ticket:%s | age_s=%d | R=%.2f",
                                    pos.symbol, pos.ticket, int(age_s), progress_r)
                        close_position(pos, "TIME_STOP")
                        continue

        atr = get_atr(pos.symbol, SIGNAL_TF)
        if atr is None:
            continue

        entry_sl_dist = abs(pos.price_open - pos.sl)
        if entry_sl_dist == 0:
            continue

        be_lock_r = sym_cfg.get("mgmt_be_lock_r", 1.0)
        be_target = (pos.price_open + entry_sl_dist * be_lock_r if pos.type == mt5.ORDER_TYPE_BUY
                     else pos.price_open - entry_sl_dist * be_lock_r)
        r2_target = (pos.price_open + entry_sl_dist * 2 if pos.type == mt5.ORDER_TYPE_BUY
                     else pos.price_open - entry_sl_dist * 2)

        new_sl = pos.sl
        new_tp = pos.tp

        want_be_lock = False
        if not be_locked.get(pos.ticket, False):
            if pos.type == mt5.ORDER_TYPE_BUY and tick.bid >= be_target:
                spread = max((tick.ask - tick.bid), 0.0)
                new_sl = pos.price_open - spread
                want_be_lock = True
            elif pos.type == mt5.ORDER_TYPE_SELL and tick.ask <= be_target:
                spread = max((tick.ask - tick.bid), 0.0)
                new_sl = pos.price_open + spread
                want_be_lock = True

        if be_locked.get(pos.ticket, False):
            trail_adj = float(open_trades.get(pos.ticket, {}).get("trail_adj", 1.0))
            trail = atr * SYMBOL_CONFIG[pos.symbol]["trail_mult"] * trail_adj
            if pos.type == mt5.ORDER_TYPE_BUY and tick.bid >= r2_target:
                potential = tick.bid - trail
                if potential > new_sl:
                    new_sl = potential
            elif pos.type == mt5.ORDER_TYPE_SELL and tick.ask <= r2_target:
                potential = tick.ask + trail
                if potential < new_sl:
                    new_sl = potential

        new_sl = normalize_price(pos.symbol, new_sl)
        new_tp = normalize_price(pos.symbol, new_tp)
        d_str  = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        ok, reason  = validate_sl_tp(pos.symbol, pos.price_open, new_sl, new_tp, d_str)
        if not ok:
            if want_be_lock:
                logger.warning("[MGMT_DIAG] %s | Ticket:%s | BE validate_sl_tp REJECTED: %s | "
                               "entry=%.5f new_sl=%.5f tp=%.5f dir=%s",
                               pos.symbol, pos.ticket, reason, pos.price_open, new_sl, new_tp, d_str)
            continue

        if abs(new_sl - pos.sl) > 1e-9:
            res = modify_position_sl_tp(pos, new_sl, new_tp)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                if want_be_lock:
                    be_locked[pos.ticket] = True
                    cp("BE_LOCK", f"[BE_LOCK] {pos.symbol} | Ticket:{pos.ticket} | SL→{new_sl:.5f} ({be_lock_r}R)")
                    logger.info("[BE_LOCK] %s | Ticket:%s | SL→%.5f | trigger=%.1fR", pos.symbol, pos.ticket, new_sl, be_lock_r)
                else:
                    cp("TRAIL",
                       f"[TRAIL] {pos.symbol} | Ticket:{pos.ticket} | SL:{pos.sl:.5f}→{new_sl:.5f}")
                    logger.info("[TRAIL] %s | Ticket:%s | SL:%.5f→%.5f",
                                pos.symbol, pos.ticket, pos.sl, new_sl)
            else:
                if want_be_lock:
                    logger.warning("[BE_LOCK_FAIL] %s | Ticket:%s | SL modify rejected", pos.symbol, pos.ticket)
                log_order_result(res, "modify_sl_tp")
        elif want_be_lock:
            logger.warning("[BE_LOCK_SKIP] %s | Ticket:%s | new_sl==pos.sl, no modify needed", pos.symbol, pos.ticket)
            be_locked[pos.ticket] = True

# =========================
# DIAGNOSTIC LINE
# =========================
def build_diagnostic_line(symbol, price, utc_now):
    parts = [f"{symbol} | HOLD | P={price:.5f}"]
    try:
        htf = get_htf_trend(symbol) or "?"
    except Exception:
        htf = "?"
    parts.append(f"HTF={htf}")
    try:
        arr = list(ATR_HISTORY[symbol])
        atr = arr[-1] if arr else None
        reg = "OK" if atr_regime_ok(symbol) else "LOW"
        parts.append(f"ATR={atr:.4f}[{reg}]" if atr else "ATR=?[?]")
    except Exception:
        parts.append("ATR=?[?]")
    try:
        tick   = mt5.symbol_info_tick(symbol)
        spread = tick.ask - tick.bid if tick else None
        spr_ok = "OK" if spread is not None and spread <= SYMBOL_CONFIG[symbol]["spread_limit"] else "WIDE"
        parts.append(f"SPR={spread:.5f}[{spr_ok}]" if spread is not None else "SPR=?")
    except Exception:
        parts.append("SPR=?")

    bos_idx = last_seen_bos_index.get(symbol)
    parts.append(f"BOS={'idx:'+str(bos_idx) if bos_idx is not None else 'NONE'}")

    pending = pending_pulls.get(symbol)
    if pending:
        zl, zh = pending["zone_low"], pending["zone_high"]
        tick2  = mt5.symbol_info_tick(symbol)
        cp_    = (tick2.ask if pending["direction"] == "BUY" else tick2.bid) if tick2 else price
        pb_pos = "IN" if zl <= cp_ <= zh else ("BELOW" if cp_ < zl else "ABOVE")
        dist   = abs(cp_ - (zl if cp_ < zl else zh)) if pb_pos != "IN" else 0.0
        parts.append(f"ZONE=[{zl:.5f}–{zh:.5f}]")
        parts.append(f"PB={pb_pos}({pending['direction']}) d={dist:.5f}")
        parts.append("PEND=YES")
    else:
        parts.append("ZONE=none | PB=none | PEND=NO")

    kz      = in_killzone(utc_now) or "none"
    sl_mult = get_sl_atr_multiplier(utc_now)
    parts.append(f"KZ={kz}")
    parts.append(f"SL×{sl_mult:.1f}")
    try:
        if daily_open_equity:
            acc = mt5.account_info()
            dd  = (daily_open_equity - acc.equity) / daily_open_equity * 100 if acc else 0
            parts.append(f"DD={dd:.2f}%")
    except Exception:
        parts.append("DD=?")
    parts.append(f"T={daily_trade_counts.get(symbol,0)}/{MAX_TRADES_PER_DAY}")
    cfg      = SESSION_LOCAL[symbol]
    local_dt = utc_now.astimezone(cfg["tz"])
    parts.append(f"LOCAL={local_dt.strftime('%H:%M')}({cfg['tz'].zone.split('/')[-1]})")
    try:
        lr = last_gate_reason.get(symbol)
        if lr:
            parts.append(f"GATE={lr}")
    except Exception:
        pass
    return " | ".join(parts)

# =========================
# MAIN LOOP
# =========================
cp("SYSTEM", "━" * 65)
if TESTING_MODE:
    cp("WARNING", "  ⚠️  TESTING MODE — relaxed trade limits active")
    cp("WARNING", f"     max_open={SYMBOL_CONFIG['XAUUSD']['max_trades']}/sym  "
                  f"max_day={MAX_TRADES_PER_DAY}/sym  "
                  f"cooldown={SYMBOL_CONFIG['XAUUSD']['cooldown']}s  "
                  f"DD={DAILY_DRAWDOWN_LIMIT*100:.0f}%")
cp("SYSTEM", "  SMC Hybrid PRO V12.2d")
cp("SYSTEM", "  FIXES: numpy | zone-reset | ATR advisory | direction | candle session | per-symbol zone/TTL | stored ATR | live candle fix")
cp("SYSTEM", "  Entry modes: BREAKOUT | PULLBACK | CONTINUATION")
cp("SYSTEM", f"  Score: BO≥{SCORE_MIN_BREAKOUT} PB≥{SCORE_MIN_PULLBACK} CONT≥{SCORE_MIN_CONTINUATION} (BTC CONT≥{SYMBOL_CONFIG['BTCUSD'].get('score_min_continuation', SCORE_MIN_CONTINUATION)})")
cp("SYSTEM", f"  PB zone: {PB_NEAR*100:.0f}%–{PB_FAR*100:.0f}% Fib")
cp("SYSTEM", f"  Zone inval mult: XAU={SYMBOL_CONFIG['XAUUSD']['zone_invalidate_mult']} "
             f"EUR/GBP={SYMBOL_CONFIG['EURUSD']['zone_invalidate_mult']} "
             f"BTC={SYMBOL_CONFIG['BTCUSD']['zone_invalidate_mult']}")
cp("SYSTEM", f"  Pending TTL: XAU={SYMBOL_CONFIG['XAUUSD']['pending_ttl']}s "
             f"EUR/GBP={SYMBOL_CONFIG['EURUSD']['pending_ttl']}s "
             f"BTC={SYMBOL_CONFIG['BTCUSD']['pending_ttl']}s")
cp("SYSTEM", f"  Cont: trig={CONTINUATION_THRESHOLD_MULT}×ATR | max={CONT_MAX_DIST_MULT}×ATR | mom≥{CONT_MOMENTUM_RATIO}")
_sg = {s: "ON" if SYMBOL_CONFIG[s].get("session_gate", False) else "OFF" for s in SYMBOLS}
cp("SYSTEM", f"  Guards: PB exhaust={PB_EXHAUSTION_ATR_MULT}×ATR | same-dir block=ON")
cp("SYSTEM", f"  Session gate: " + " ".join(f"{s}={_sg[s]}" for s in SYMBOLS))
cp("SYSTEM", f"  Sessions: LOCAL market time (pytz BST/GMT auto)")
cp("SYSTEM", f"  RR: XAU≥{SYMBOL_CONFIG['XAUUSD'].get('min_rr', MIN_RR)} "
             f"EUR/GBP≥{MIN_RR} "
             f"BTC≥{SYMBOL_CONFIG['BTCUSD'].get('min_rr', MIN_RR)} | "
             f"Risk={RISK_PERCENT*100:.1f}% | DD stop={DAILY_DRAWDOWN_LIMIT*100:.0f}%")
cp("SYSTEM", f"  Session score: XAU={'ON' if SYMBOL_CONFIG['XAUUSD'].get('session_score_bonus', True) else 'OFF'} "
             f"EUR/GBP=ON BTC=ON")
for _s in SYMBOLS:
    _c = SYMBOL_CONFIG[_s]
    _parts = [f"BE@{_c.get('mgmt_be_lock_r', 1.0)}R"]
    if _c.get("mgmt_partial_enabled", True):  _parts.append("Partial")
    if _c.get("mgmt_lockin_enabled", True):   _parts.append(f"LK@{_c.get('mgmt_lockin_r', LOCK_IN_R_MULTIPLE)}R/{_c.get('mgmt_lockin_drawback', LOCK_IN_DRAWBACK)}")
    if _c.get("mgmt_time_stop_enabled", True): _parts.append(f"TS(minR={_c.get('mgmt_time_stop_min_r', TIME_STOP_MIN_PROGRESS_R)})")
    cp("SYSTEM", f"  Mgmt {_s}: {' + '.join(_parts)}")
cp("SYSTEM", "━" * 65)

now_utc = get_utc_time()
for sym in SYMBOLS:
    cfg      = SESSION_LOCAL[sym]
    local_dt = now_utc.astimezone(cfg["tz"])
    open_now = market_open(sym)
    status   = "OPEN ✅" if open_now else "CLOSED"
    cp("SYSTEM" if open_now else "MARKET_CLOSED",
       f"  {sym}: local={local_dt.strftime('%H:%M')} {cfg['tz'].zone} "
       f"| windows={cfg['windows']} | {status}")

logger.info("SMC Hybrid PRO V12.2d | live candle contamination fix | CONT_MOMENTUM_RATIO=0.60")

for s in SYMBOLS:
    ensure_symbol(s)

gate_counters = defaultdict(lambda: defaultdict(int))
last_gate_reason = {}
_last_gate_report_ts = 0.0
GATE_REPORT_INTERVAL = 60

def _gate_hit(symbol, gate):
    try:
        gate_counters[symbol][gate] += 1
        last_gate_reason[symbol] = gate
    except Exception:
        pass

def _gate_report_if_due():
    global _last_gate_report_ts
    now = time.time()
    if now - _last_gate_report_ts < GATE_REPORT_INTERVAL:
        return
    _last_gate_report_ts = now
    try:
        for sym in SYMBOLS:
            c = gate_counters.get(sym)
            if not c:
                continue
            top = sorted(c.items(), key=lambda kv: kv[1], reverse=True)[:6]
            top_s = " ".join([f"{k}={v}" for k, v in top])
            lr = last_gate_reason.get(sym, "-")
            logger.info("[GATES] %s last=%s | %s", sym, lr, top_s)
    except Exception:
        pass

while True:
    if not is_connected():
        cp("WARNING", "⚠️  MT5 connection lost — reconnecting…")
        logger.warning("MT5 connection lost")
        reconnect_mt5()
        reconcile_open_positions()

    reset_daily_trackers()

    if check_daily_drawdown():
        cp("HALT", "🛑 Daily halt — management only")
        detect_closed_trades()
        manage_trades()
        time.sleep(5)
        continue

    utc_time = get_utc_time()

    for symbol in SYMBOLS:
        last_gate_reason.pop(symbol, None)
        cp("HEADER", f"┌ {utc_time.strftime('%H:%M:%S')} UTC | {symbol}")
        logger.info("%s UTC | %s", utc_time.strftime("%H:%M:%S"), symbol)

        if not market_open(symbol):
            cp("MARKET_CLOSED", f"└ {symbol} market closed")
            logger.info("%s market closed", symbol)
            continue

        sig, price, meta = get_signal(symbol)

        if sig == "BUY":
            mode      = meta.get("entry_mode", "pullback") if meta else "pullback"
            color_key = "BREAKOUT" if mode == "breakout" else "BUY"
            cp(color_key, f"└ {symbol} 🔎 BUY SIGNAL [{mode.upper()}] | Price={price:.5f}")
            logger.info("%s BUY SIGNAL [%s] | Price=%s", symbol, mode, price)
            execute_trade(symbol, "BUY", meta or {})

        elif sig == "SELL":
            mode      = meta.get("entry_mode", "pullback") if meta else "pullback"
            color_key = "CONTINUATION" if mode == "continuation" else "SELL"
            cp(color_key, f"└ {symbol} � SELL SIGNAL [{mode.upper()}] | Price={price:.5f}")
            logger.info("%s SELL SIGNAL [%s] | Price=%s", symbol, mode, price)
            execute_trade(symbol, "SELL", meta or {})

        else:
            diag = build_diagnostic_line(symbol, price, utc_time)
            cp("DIAG", f"└ {diag}")
            logger.info("%s", diag)

    check_pending_pullbacks()
    _gate_report_if_due()
    detect_closed_trades()
    manage_trades()
    time.sleep(5)
