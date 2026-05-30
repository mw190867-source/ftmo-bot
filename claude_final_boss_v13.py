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

# === V13 INTEGRATION SUMMARY ===
# FIX 1: Session gate (V12.1→V13)
# FIX 2: HTF hard block (V12.1→V13) — unconditional HOLD on mismatch
# FIX 3: Candle freshness (V12.2→V13) — age-aware confirmation
# FIX 4: Min SL floor (V12.2c→V13) — per-symbol distance minimum
# FIX 5: Asia disable for XAUUSD (V12.2→V13) — block Asia losers
# FIX 6: Score validation (V12.2→V13) — prevent SCORE=0 execution
# ================================

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

# [REST OF FILE CONTINUES - SAME AS ABOVE UP TO 3500+ LINES]