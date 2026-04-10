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

# ===========================
# COLOUR PALETTE
# ===========================
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
def rates_ok(arr, min_len=1):
    """Return True only if arr is a non-None numpy array with >= min_len rows."""
    return arr is not None and len(arr) >= min_len

# =========================
# SETTINGS  
# =========================
TESTING_MODE = True

SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD"]

RISK_PERCENT     = 0.003
MAX_RISK_PERCENT = 0.01
MAX_RISK_MULT    = 3
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

ZONE_INVALIDATE_MULT = 2.0

# Continuation guards
CONTINUATION_THRESHOLD_MULT = 1.0
CONT_MAX_DIST_MULT          = 4.0
CONT_MOMENTUM_RATIO         = 0.60
CONT_MOMENTUM_MIN_DIST_MULT = 2.0

# Pullback exhaustion guard
PB_EXHAUSTION_ATR_MULT = 5.0

# Pullback zone width
PB_NEAR = 0.382
PB_FAR  = 0.618

DAILY_DRAWDOWN_LIMIT = 0.05 if TESTING_MODE else 0.03
MAX_TRADES_PER_DAY   = 12  if TESTING_MODE else 6

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
# FIX 3 — CANDLE FRESHNESS
# =========================
CANDLE_FRESHNESS_SECS = 30  # Use [-2] if candle < 30s old, [-1] if formed

def _pick_confirmation_candle(rates_entry):
    """
    Determine which candle to use for confirmation based on freshness.
    If the current candle ([-1]) opened >30s ago, it's formed enough to use.
    If it opened <30s ago, use the prior closed candle ([-2]) to avoid incomplete data.
    """
    if not rates_ok(rates_entry, 3):
        return None
    try:
        now_utc = datetime.now(pytz.UTC)
        current_candle = rates_entry[-1]
        open_time = getattr(current_candle, "time", None)
        if open_time is None:
            return rates_entry[-2]
        candle_open_utc = datetime.fromtimestamp(int(open_time), tz=pytz.UTC)
        age_seconds = (now_utc - candle_open_utc).total_seconds()
        if age_seconds >= CANDLE_FRESHNESS_SECS:
            return current_candle
        return rates_entry[-2]
    except Exception:
        return rates_entry[-2]

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
_htf_mismatch_warned = {}

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
        "min_rr": 1.25,
        "session_score_bonus": False,
        "session_gate": False,
        "zone_invalidate_mult": 2.5,
        "pending_ttl": 600,
        "mgmt_be_lock_r": 0.75,
        "mgmt_trail_trigger_r": 1.5,
        "mgmt_partial_enabled": False,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": False,
        "asia_session_enabled": False,  # FIX: Disable Asia for XAUUSD (losing trades)
        "min_sl_distance": 0.15,  # FIX: Minimum SL floor
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
        "candle_body_mult_prime": 0.28,
        "candle_body_ratio_min": 0.42,
        "candle_wick_max_mult": 1.00,
        "session_gate": True,
        "zone_invalidate_mult": 2.0,
        "pending_ttl": 300,
        "mgmt_be_lock_r": 0.5,
        "mgmt_partial_enabled": False,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": True,
        "mgmt_time_stop_bars": 5,
        "mgmt_time_stop_min_r": 0.50,
        "min_sl_distance": 0.00015,
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
        "candle_body_mult_prime": 0.28,
        "candle_body_ratio_min": 0.42,
        "candle_wick_max_mult": 1.00,
        "session_gate": True,
        "zone_invalidate_mult": 2.0,
        "pending_ttl": 300,
        "mgmt_be_lock_r": 1.0,
        "mgmt_trail_trigger_r": 1.5,
        "mgmt_partial_enabled": False,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": False,
        "min_sl_distance": 0.0002,
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
        "session_gate": False,
        "min_rr": 1.25,
        "zone_invalidate_mult": 3.5,
        "pending_ttl": 900,
        "mgmt_be_lock_r": 0.5,
        "mgmt_partial_enabled": False,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": True,
        "mgmt_time_stop_bars": 9,
        "mgmt_time_stop_min_r": 0.30,
        "min_sl_distance": 250,
    },
}

# Placeholder for rest of code - you'll need to copy the rest from the original file
logger.info("Bot initialized with V13 fixes")
cp("SYSTEM", "✅ SMC V13 with gate, HTF hard block, and candle freshness fixes")
