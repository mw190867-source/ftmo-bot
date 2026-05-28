import MetaTrader5 as mt5
import time
import logging
import subprocess
import shutil
import threading
import urllib.request
import urllib.parse
import json
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, date, timedelta
from colorama import Fore, Style, init
import numpy as np
import pytz
from collections import deque, defaultdict
import sys as _sys, os as _os_mod
_sys.path.insert(0, _os_mod.path.dirname(_os_mod.path.abspath(__file__)))
from opportunity_scanner import (
    init_scanner, run_opportunity_scan,
    get_opportunity_score_bonus, get_opportunities_for_dashboard,
)

init(autoreset=True)

# TODO: Top up Anthropic API credits — Friday morning 00:01 BST
# XAUUSD CORRECTIVE claude_gate is currently set to "shadow" instead of "hard"
# Revert XAUUSD_REGIME_PARAMS CORRECTIVE claude_gate back to "hard" after topping up
# Then ftmo restart to restore full hard gate protection on gold

# =========================
# PHOENIX TELEMETRY (passive, best-effort, observe-only)
# =========================
_PHOENIX_URL     = "http://127.0.0.1:8000/events"
_PHOENIX_TIMEOUT = 1.0  # hard cap — never delays trading

def _phoenix_emit(event_type: str, symbol: str, message: str,
                  severity: str = "INFO", department: str = "TRADE",
                  metadata: dict = None) -> None:
    """Fire-and-forget telemetry to Phoenix. Silently dropped on any failure."""
    import threading as _th, json as _j, uuid as _uuid, urllib.request as _ur
    def _send():
        try:
            body = _j.dumps({
                "event_id":      str(_uuid.uuid4()),
                "timestamp_utc": datetime.utcnow().isoformat() + "Z",
                "severity":      severity,
                "department":    department,
                "desk":          "DESK_ALPHA",
                "bot_id":        "BOT_FXG_01",
                "account_id":    "FTMO_001",
                "symbol":        symbol,
                "event_type":    event_type,
                "message":       message,
                "metadata":      metadata or {},
            }, default=str).encode()
            req = _ur.Request(_PHOENIX_URL, data=body,
                              headers={"Content-Type": "application/json"})
            _ur.urlopen(req, timeout=_PHOENIX_TIMEOUT)
        except Exception:
            pass
    _th.Thread(target=_send, daemon=True).start()

# =========================
# PATHS / LOGGING
# =========================
BASE_DIR = Path(__file__).resolve().parent
BOT_NAME = "FTMO Challenge Bot V1.0"
BOT_VERSION = "V1.0"
LOG_FILE = BASE_DIR / "ftmo_v1.log"

class SafeFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        return msg.encode("ascii", "replace").decode("ascii")

def setup_logger():
    logger = logging.getLogger("SMC_Hybrid_PRO")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        fh = RotatingFileHandler(str(LOG_FILE), maxBytes=5_000_000, backupCount=20, encoding="utf-8")
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
# CLAUDE SHADOW MODE — API KEY
# =========================
import os as _os
from pathlib import Path as _Path

def _load_dotenv():
    """Auto-load .env from script directory into os.environ. No external deps."""
    env_path = _Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in _os.environ:
                _os.environ[key] = val
    except Exception as e:
        logger.debug("[ENV] failed to load .env: %s", e)

_load_dotenv()
ANTHROPIC_API_KEY = _os.environ.get("ANTHROPIC_API_KEY", "")

# =========================
# TELEGRAM NOTIFICATIONS
# =========================
TELEGRAM_ENABLED   = True
TELEGRAM_TOKEN     = _os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = _os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TIMEOUT   = 5  # seconds — never block trading loop

def _tg_send_blocking(text: str) -> None:
    """Internal — HTTP call. Run in a daemon thread so trading never waits."""
    if not TELEGRAM_ENABLED or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],  # Telegram message limit = 4096
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT).read()
    except Exception as e:
        # Never let Telegram failures propagate — just log & move on
        logger.debug("[TELEGRAM] send failed: %s", e)

def tg_send(text: str) -> None:
    """Fire-and-forget Telegram notification. Safe to call from anywhere."""
    if not TELEGRAM_ENABLED:
        return
    try:
        threading.Thread(target=_tg_send_blocking, args=(text,), daemon=True).start()
    except Exception:
        pass

# =========================
# CLAUDE SHADOW MODE — CONTEXT BUILDER & API CALL
# =========================
def _session_minutes_remaining(symbol, utc_dt):
    """Return (session_name, minutes_remaining) for the current session window."""
    cfg = SESSION_LOCAL[symbol]
    local_dt = utc_dt.astimezone(cfg["tz"])
    h = local_dt.hour + local_dt.minute / 60.0
    kz = in_killzone(utc_dt)
    if kz:
        # Calculate remaining killzone time
        local_dt = utc_dt.astimezone(cfg["tz"])
        h = local_dt.hour + local_dt.minute / 60.0
        kz_start, kz_end = KILLZONE_LOCAL[kz]
        remaining = max(0.0, (kz_end - h) * 60.0)
        return kz, round(remaining, 1)
    for start, end in cfg["windows"]:
        if start <= h < end:
            remaining = (end - h) * 60.0
            return f"session_{start}-{end}", remaining
    if not SYMBOL_CONFIG[symbol].get("session_gate", False):
        return "ungated_session", 999.0
    return "outside_session", 0.0

def _build_claude_context(symbol, direction, entry_mode, score, meta, utc_dt, atr, sl_dist):
    """Build structured context packet for Claude evaluation."""
    c = SYMBOL_CONFIG[symbol]
    # Session info
    session_name, session_min_remaining = _session_minutes_remaining(symbol, utc_dt)
    # ATR context
    atr_threshold = float(c.get("atr_threshold", 0))
    atr_ok = atr is not None and atr >= atr_threshold
    in_kz = in_killzone(utc_dt) is not None
    if atr_ok:
        atr_context = "atr_ok"
    elif in_kz:
        atr_context = "low_atr_in_kz"
    else:
        atr_context = "low_atr_in_session"
    # HTF trend
    htf = meta.get("htf", "UNKNOWN")
    # Zone info
    zone_low = meta.get("zone_low")
    zone_high = meta.get("zone_high")
    zone_width_atr_ratio = None
    if zone_low is not None and zone_high is not None and atr and atr > 0:
        zone_width_atr_ratio = round(abs(zone_high - zone_low) / atr, 3)
    # Last 5 M1 candles
    m1_rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 5)
    m1_candles = []
    if rates_ok(m1_rates, 5):
        for r in m1_rates:
            body = float(r["close"] - r["open"])
            direction_m1 = "BUY" if body >= 0 else "SELL"
            m1_candles.append(f"{direction_m1}:{abs(body):.5f}")
    # Displacement & BOS
    displacement = meta.get("displacement", 0)
    bos_idx = meta.get("bos_idx")
    # Recent losses this session for this symbol/direction
    target = canonical_symbol(symbol)
    now_ts = time.time()
    recent_losses = recent_losses_per_symbol.get(target, [])
    session_losses = [
        (t, d, ep) for (t, d, ep) in recent_losses
        if d == direction and (now_ts - t) < 14400  # last 4 hours
    ]
    time_since_last_loss = None
    if session_losses:
        last_loss_ts = max(t for (t, _, _) in session_losses)
        time_since_last_loss = round((now_ts - last_loss_ts) / 60.0, 1)
    # Session range position (0-1)
    rates_60 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 60)
    session_range_position = None
    if rates_ok(rates_60, 60):
        highs = [float(r["high"]) for r in rates_60]
        lows = [float(r["low"]) for r in rates_60]
        range_high = max(highs)
        range_low = min(lows)
        price = float(rates_60[-1]["close"])
        if range_high > range_low:
            session_range_position = round((price - range_low) / (range_high - range_low), 3)

    pip_val = float(c.get("pip_value", 0.0001))
    sl_pips_raw = round(sl_dist / pip_val, 1) if pip_val else None
    # Commodities and JPY pairs (pip_value=0.01) show in points not pips
    # to avoid misleading Claude with inflated pip counts
    if c.get("type") == "commodity" or float(c.get("pip_value", 0.0001)) >= 0.01:
        sl_context = f"{round(sl_dist, 2)} points"
    else:
        sl_context = f"{sl_pips_raw} pips" if sl_pips_raw else None

    context = {
        "symbol": symbol,
        "direction": direction,
        "entry_mode": entry_mode,
        "score": score,
        "session": {"name": session_name, "minutes_remaining": round(session_min_remaining, 1)},
        "atr_context": atr_context,
        "htf_trend": htf,
        "zone": {
            "low": zone_low,
            "high": zone_high,
            "width_atr_ratio": zone_width_atr_ratio,
        },
        "last_5_m1_candles": m1_candles,
        "displacement": displacement,
        "bos_idx": bos_idx,
        "recent_losses_this_session": len(session_losses),
        "time_since_last_loss_minutes": time_since_last_loss,
        "session_range_position": session_range_position,
        "sl_distance": sl_context,
    }
    return context

def _claude_shadow_evaluate(symbol, direction, entry_mode, score, meta, utc_dt, atr, sl_dist, ticket=None):
    """Fire-and-forget Claude evaluation. Returns immediately; logs result async."""
    if not CLAUDE_SHADOW_ENABLED or not ANTHROPIC_API_KEY:
        return None
    try:
        context = _build_claude_context(symbol, direction, entry_mode, score, meta, utc_dt, atr, sl_dist)
    except Exception as e:
        logger.debug("[CLAUDE_SHADOW] context build failed: %s", e)
        return None

    def _call_and_log():
        verdict = "pending"
        reason = "Signal passed all structural filters — AI review queued"
        confidence = 0.0
        try:
            import json as _json
            system_prompt = (
                "You are a forex trading evaluator. Analyze the trade context and respond ONLY with valid JSON. "
                "No explanations, no markdown, just the JSON object.\n"
                "Fields:\n"
                '- "verdict": one of "approve", "approve_reduced", "block"\n'
                '- "reason": one sentence maximum explaining your decision\n'
                '- "confidence": a float from 0.0 to 1.0 indicating your certainty'
            )
            user_msg = _json.dumps(context, default=str)

            req_data = _json.dumps({
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 80,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_msg}],
            }).encode("utf-8")

            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=8)
            body = _json.loads(resp.read().decode("utf-8"))
            content = body.get("content", [{}])[0].get("text", "{}")
            # Strip markdown code fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[-1]
                if content.endswith("```"):
                    content = content[:-3]
            result = _json.loads(content)
            verdict = result.get("verdict", "unknown")
            reason = result.get("reason", "")
            confidence = float(result.get("confidence", 0.0))
        except Exception as e:
            logger.debug("[CLAUDE_SHADOW] API call failed: %s", e)
            verdict = "pending"
            confidence = 0.0
            reason = "Signal passed all structural filters — AI review queued"

        logger.info("[CLAUDE_SHADOW] %s | %s | verdict=%s | confidence=%.2f | reason=%s | actual=EXECUTED",
                    symbol, direction, verdict, confidence, reason)
        # Update open_trades so outcome log can reference the verdict
        if ticket is not None and ticket in open_trades:
            open_trades[ticket]["shadow_verdict"] = verdict
        # Shadow accuracy CSV — log vote row (outcome back-filled on close)
        if ticket is not None:
            _tm = open_trades.get(ticket, {})
            _shadow_csv_log_vote(
                symbol, direction, confidence, verdict, ticket,
                _tm.get("entry", ""), _tm.get("sl", ""), _tm.get("tp", ""),
            )

    try:
        threading.Thread(target=_call_and_log, daemon=True).start()
    except Exception:
        pass
    return None


def _claude_hard_gate_evaluate(symbol, direction, entry_mode, score, meta, utc_dt, atr, sl_dist, timeout_secs=5):
    """BLOCKING Claude evaluation for HARD GATE mode.

    Returns True if Claude approves the trade, False if blocked or error.
    Used for XAUUSD CORRECTIVE regime where Claude acts as a hard gate.
    Timeout is shorter than shadow mode (5s vs 8s) since this blocks execution.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("[CLAUDE_HARD_GATE] No API key — blocking trade")
        return False

    try:
        context = _build_claude_context(symbol, direction, entry_mode, score, meta, utc_dt, atr, sl_dist)
    except Exception as e:
        logger.warning("[CLAUDE_HARD_GATE] Context build failed: %s", e)
        return False

    try:
        import json as _json
        system_prompt = (
            "You are a strict gold trading evaluator. XAUUSD is in CORRECTIVE regime. "
            "Analyze this trade context and respond ONLY with valid JSON. "
            "Be conservative — reject marginal setups.\n\n"
            "Response format (JSON only, no markdown):\n"
            '{"verdict": "approve" | "block", '
            '"reason": "one sentence", '
            '"confidence": 0.0-1.0}\n\n'
            'VERDICT RULES:\n'
            '- "approve": Only if setup has clear edge (score≥5, strong structure, sweep confirmation)\n'
            '- "block": If setup is marginal, late in session, or lacks clear confluence\n'
            '- confidence ≥0.7 required for approve'
        )
        user_msg = _json.dumps(context, default=str)

        req_data = _json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 80,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_msg}],
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=req_data,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST"
        )

        resp = urllib.request.urlopen(req, timeout=timeout_secs)
        body = _json.loads(resp.read().decode("utf-8"))
        content = body.get("content", [{}])[0].get("text", "{}")

        # Strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3]

        result = _json.loads(content)
        verdict = result.get("verdict", "block").lower()
        reason = result.get("reason", "no reason")
        confidence = float(result.get("confidence", 0.0))

        approved = verdict in ("approve", "approve_reduced") and confidence >= 0.7

        if approved:
            logger.info("[CLAUDE_HARD_GATE] %s %s | APPROVED | confidence=%.2f | %s",
                        symbol, direction, confidence, reason)
            cp("CLAUDE_GATE", f"✅ [CLAUDE] {symbol} {direction} APPROVED | conf={confidence:.2f}")
        else:
            logger.info("[CLAUDE_HARD_GATE] %s %s | BLOCKED | verdict=%s | confidence=%.2f | %s",
                        symbol, direction, verdict, confidence, reason)
            cp("CLAUDE_GATE", f"🚫 [CLAUDE] {symbol} {direction} BLOCKED | verdict={verdict}")

        return approved

    except Exception as e:
        logger.warning("[CLAUDE_HARD_GATE] API call failed: %s", e)
        logger.info("[CLAUDE_HARD_GATE] %s %s | pending | confidence=0.00 | Signal passed all structural filters — AI review queued",
                    symbol, direction)
        return False


# =========================
# CLAUDE PRE-SESSION BRIEF (daily 07:55 London -> Telegram)
# =========================
last_brief_date_london = None

def _build_brief_context():
    now_ts = time.time()
    cutoff = now_ts - 7200
    per_symbol = {}
    for sym in SYMBOLS:
        try:
            htf = get_htf_trend(sym) or "UNKNOWN"
        except Exception:
            htf = "UNKNOWN"
        hits = [(t, r) for (t, r) in recent_gate_hits.get(sym, []) if t >= cutoff]
        counter = defaultdict(int)
        for _, r in hits:
            counter[r] += 1
        top3 = sorted(counter.items(), key=lambda kv: -kv[1])[:3]
        pending = pending_pulls.get(sym)
        zone_info = None
        if pending:
            zone_info = {
                "direction": pending.get("direction"),
                "zone_low": round(float(pending.get("zone_low", 0)), 5),
                "zone_high": round(float(pending.get("zone_high", 0)), 5),
                "bos_type": pending.get("bos_type"),
                "age_min": round((now_ts - float(pending.get("timestamp", now_ts))) / 60.0, 1),
            }
        per_symbol[sym] = {
            "htf": htf,
            "top_gates_last_2h": [{"reason": r, "count": c} for (r, c) in top3],
            "gate_hits_last_2h_total": len(hits),
            "pullback_enabled": bool(SYMBOL_CONFIG.get(sym, {}).get("pullback_enabled", True)),
            "armed_zone": zone_info,
        }
    return {"utc_time": datetime.utcnow().replace(tzinfo=pytz.UTC).isoformat(timespec="seconds"),
            "symbols": per_symbol}

def _send_session_brief():
    """Build context, ask Claude for a 3-sentence outlook, and fire to Telegram.
    Falls back to raw stats if Claude is unavailable so the user still gets situational awareness.
    Runs async in a daemon thread — never blocks trading."""
    ctx = _build_brief_context()
    # Raw-stats fallback lines (always computed; used if Claude fails)
    raw_lines = []
    for sym, info in ctx["symbols"].items():
        gates_str = ", ".join(f"{g['reason']}:{g['count']}" for g in info["top_gates_last_2h"]) or "(none)"
        pb = "PB+BO" if info["pullback_enabled"] else "BO-only"
        z = info["armed_zone"]
        zone_str = f" | zone={z['direction']}[{z['zone_low']}-{z['zone_high']}]" if z else ""
        raw_lines.append(f"<b>{sym}</b> HTF={info['htf']} {pb} | gates2h: {gates_str}{zone_str}")

    def _call():
        brief_text = None
        try:
            if not ANTHROPIC_API_KEY:
                raise RuntimeError("no_api_key")
            import json as _json
            system_prompt = (
                "You are a forex trading session analyst. Given pre-session context across 4 symbols "
                "(XAUUSD, EURUSD, GBPUSD, USDJPY), write EXACTLY 3 sentences of plain English: "
                "(1) which symbols have best HTF alignment for today's session, "
                "(2) what to watch for in the London open, "
                "(3) any caution flags. No markdown, no lists, no preamble. Terse and practical."
            )
            user_msg = _json.dumps(ctx, default=str)
            req_data = _json.dumps({
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_msg}],
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=req_data,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=15)
            body = _json.loads(resp.read().decode("utf-8"))
            brief_text = body.get("content", [{}])[0].get("text", "").strip()
            if not brief_text:
                raise RuntimeError("empty_response")
            logger.info("[SESSION_BRIEF] Claude OK: %s", brief_text.replace("\n", " ")[:500])
        except Exception as e:
            logger.warning("[SESSION_BRIEF] Claude call failed (%s) — sending raw stats fallback", e)

        header = "\U0001F305 <b>Pre-session brief</b> (07:55 London)"
        if brief_text:
            tg_msg = f"{header}\n{brief_text}\n\n<i>" + "\n".join(raw_lines) + "</i>"
        else:
            tg_msg = header + "\n" + "\n".join(raw_lines)
        tg_send(tg_msg)

    try:
        threading.Thread(target=_call, daemon=True).start()
    except Exception as e:
        logger.debug("[SESSION_BRIEF] thread start failed: %s", e)

def _session_brief_check():
    """Called from main loop. Fires once per London calendar day at or after 07:55 local,
    but only within a narrow window (07:55–08:29) to avoid mid-day sends after restarts.
    Idempotent — tracks last fired date to prevent duplicates."""
    global last_brief_date_london
    try:
        now_london = datetime.now(LONDON_TZ)
    except Exception:
        return
    today_key = now_london.strftime("%Y-%m-%d")
    if last_brief_date_london == today_key:
        return
    hh = now_london.hour
    mm = now_london.minute
    # Window: 07:55 to 08:29 inclusive. Early enough to be pre-session (session starts 08:00),
    # narrow enough that a lunchtime restart won't trigger a stale brief.
    in_window = (hh == 7 and mm >= 55) or (hh == 8 and mm < 30)
    if not in_window:
        return
    last_brief_date_london = today_key  # mark BEFORE dispatch so a crash can't cause double-send
    logger.info("[SESSION_BRIEF] Firing pre-session brief for %s London", today_key)
    _send_session_brief()

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
    "ADOPT":         (Fore.CYAN,    Style.NORMAL),
    "DIAG":          (Fore.WHITE,   Style.DIM),
    "CLAUDE_GATE":   (Fore.MAGENTA, Style.BRIGHT),
    "GOLD_REGIME":   (Fore.YELLOW,  Style.BRIGHT),
    "MARKET_CLOSED": (Fore.WHITE,   Style.NORMAL),
    "SLIPPAGE":      (Fore.YELLOW,  Style.NORMAL),
    "HEADER":        (Fore.WHITE,   Style.BRIGHT),
    "POS_PROFIT":    (Fore.GREEN,   Style.NORMAL),
    "POS_LOSS":      (Fore.RED,     Style.NORMAL),
    "POS_FLAT":      (Fore.YELLOW,  Style.NORMAL),
    "SEPARATOR":     (Fore.CYAN,    Style.BRIGHT),
    "HEARTBEAT":     (Fore.CYAN,    Style.NORMAL),
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
# ── TESTING MODE ──────────────────────────────────────────────────
TESTING_MODE = False  # Production mode for FTMO challenge
CLAUDE_SHADOW_ENABLED = True  # Shadow-mode Claude reasoning — evaluates but never blocks
# ──────────────────────────────────────────────────────────────────

SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "GBPJPY"]

GBPJPY_SHADOW_MODE = False  # V5.3 — shadow validation complete, enabling live execution
GBPJPY_SHADOW_START = datetime.utcnow().replace(tzinfo=pytz.UTC)

# V5.3 — raised from 0.003/0.004/0.0035 — need higher risk to pass 14-day challenge
RISK_PERCENT     = 0.005              # V5.3 raised from 0.003 (0.3% → 0.5%)
MAX_RISK_PERCENT = 0.008              # unchanged cap
MAX_RISK_MULT    = 3
MAX_TOTAL_RISK_PERCENT = 0.015

# Dynamic risk scaling by conviction score. Base stays small on marginal setups;
# high-conviction confluence earns a modest bump. Capped by MAX_RISK_PERCENT.
# Rationale: asymmetric sizing is how you recover ground without raising DD on weak signals.
RISK_SCALING_ENABLED = True
RISK_BY_SCORE = {
    # score_min_for_tier : risk_percent
    7: 0.0065,   # V5.3 raised from 0.004  (0.4% → 0.65%)
    6: 0.006,    # V5.3 raised from 0.0035 (0.35% → 0.6%)
}
MAGIC            = 123456
DEVIATION        = 20

BASKET_TP_PCT = 0.05  # FTMO 5% profit target
BASKET_SL_PCT = 0.02

LOCK_IN_R_MULTIPLE = 1.0
LOCK_IN_DRAWBACK   = 0.75
MIN_RR             = 1.5

SCORE_MIN_PULLBACK     = 2   # V5 (May 13): Lowered 3→2 to increase trade frequency
SCORE_MIN_BREAKOUT     = 3   # V5 (May 13): Lowered 4→3 to increase trade frequency
SCORE_MIN_CONTINUATION = 4   # Raised from 3 — continuation is risky

BREAKOUT_DISP_MULT   = 1.2
BREAKOUT_BODY_RATIO  = 0.55
BREAKOUT_BODY_EXPAND = 1.0
BREAKOUT_CLOSE_PCT   = 0.30

ZONE_INVALIDATE_MULT = 2.0

CONTINUATION_THRESHOLD_MULT = 1.0
CONT_MAX_DIST_MULT          = 4.0
CONT_MOMENTUM_RATIO         = 0.60
CONT_MOMENTUM_MIN_DIST_MULT = 2.0

PB_EXHAUSTION_ATR_MULT = 5.0

PB_NEAR = 0.382  # V5.3 — restored to original 38.2%, tightened window was halving valid entries
PB_FAR  = 0.618  # V5.3 — restored to original 61.8%

DAILY_DRAWDOWN_LIMIT = 0.05  # FTMO 5% daily loss limit
MAX_TRADES_PER_DAY   = 6  if TESTING_MODE else 6  # V5.3 — raised from 3, need 6+/day to pass 14-day challenge

PARTIAL_CLOSE_PCT  = 0.35  # Backtest-validated: 35% partial close
PARTIAL_TP_R       = 1.5   # Backtest-validated: 1.5R partial TP
PARTIAL_TP_R_ASIA  = 1.0   # Backtest-validated: 1.0R in Asia
PARTIAL_TP_R_PRIME = 1.5

TIME_STOP_MIN_PROGRESS_R   = 0.10  # Lower threshold - allow more time
TIME_STOP_KILLZONE_MINUTES = 30   # Shorter — NY/London Open is fast, no time to wait
TIME_STOP_ASIA_MINUTES     = 45   # Shorter — was 90
TIME_STOP_DEFAULT_MINUTES  = 25   # Shorter — for slow periods, cut even tighter

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
    "XAUUSD": {"tz": LONDON_TZ, "windows": [(8, 12), (13, 18)]},  # Backtest-validated: 18:00 close
    "EURUSD": {"tz": LONDON_TZ, "windows": [(6, 18)]},  # V5 (May 13): Extended pre-session 06:00
    "GBPUSD": {"tz": LONDON_TZ, "windows": [(6, 18)]},  # V5 (May 13): Extended pre-session 06:00
    "USDJPY": {"tz": LONDON_TZ, "windows": [(8, 18)]},  # V2: London/NY overlap only; session_gate: True
    "GBPJPY": {"tz": LONDON_TZ, "windows": [(6, 18)]},  # V5 (May 13): Added with extended pre-session 06:00
}

KILLZONE_LOCAL = {
    "LONDON_OPEN": (8,  10),
    "NY_OPEN":     (13, 15),
}

ASIA_SESSION_UTC = (0, 7)

BROKER_UTC_OFFSET_HOURS     = 2
ROLLOVER_BLOCK_BROKER_HOURS = (23.85, 0.25)

ATR_HISTORY          = {s: deque(maxlen=100) for s in SYMBOLS}
last_trade_time      = {}
signal_cooldown      = {}
peak_profit_tracker  = {}
open_trades          = {}
be_locked            = {}
partial_done         = {}
r1_level             = {}
pending_pulls        = {}
_last_known_htf      = {}  # V5.1 — HTF trend cache to prevent transient data failures from blocking trades
# Unresolved close-detection retry queue. Key = ticket (int), value = dict
# with "first_try" (datetime), "attempts" (int), "meta" (snapshot of open_trades
# entry at detection time so symbol/direction are preserved after state cleanup).
# Populated by detect_closed_trades() when a fresh close has no deal history yet
# (MT5 lag). Drained in the same function on each cycle until resolved or
# PENDING_CLOSE_MAX_ATTEMPTS is exceeded.
_pending_deal_lookup: dict = {}
PENDING_CLOSE_MAX_ATTEMPTS = 30  # ~150s at 5s heartbeat cadence. FTMO broker's
# history_deals_get() takes >60s to index fresh closes (confirmed Apr 28 via
# diagnostic dumps — deals appear in MT5 Terminal immediately but API lags).

# Stage 1: OpportunityScanner global state
opportunity_alerts: dict = {}  # keyed by symbol: {direction, thesis, trigger, confidence, timestamp}
_last_opportunity_alert: dict = {}  # keyed by symbol: timestamp (rate limiting)
_opportunity_scanner_thread = None

last_bos_index       = {s: None for s in SYMBOLS}
last_seen_bos_index  = {s: None for s in SYMBOLS}
cont_fired: dict     = {}
same_bos_rearm_blocks = {}
# Persistent record of (symbol, direction, bos_idx, entry_mode) tuples that have
# been traded. Cleared only when a NEW BOS is detected (last_seen_bos_index
# changes). Prevents re-entering on the same BOS after a previous trade closes.
traded_bos_set: set = set()

# =========================
# LOSS CLUSTER BLOCK
# =========================
# Track recent losing trades per symbol to detect "same direction at same level
# keeps failing" patterns (e.g. Apr 28: 3 EUR SELLs at 1.16977-1.17009 all
# time-stopped within 90 min). Once 2 losses accumulate in the same direction
# inside the lookback window, that direction is blocked on that symbol for the
# block duration. Auto-expires — no manual reset needed.
recent_losses_per_symbol: dict = defaultdict(list)  # symbol -> [(ts_unix, direction, entry_price), ...]
LOSS_CLUSTER_WINDOW_S    = 90 * 60   # Look-back window for counting losses
LOSS_CLUSTER_THRESHOLD   = 2         # Losses required to trigger block
LOSS_CLUSTER_BLOCK_S     = 60 * 60   # Block duration after threshold reached
_loss_cluster_alerted: set = set()   # (symbol, direction) tuples already Telegram-alerted this block

def record_loss(symbol, direction, entry_price, profit, ts=None):
    """Record a closed losing trade. Called from close paths when profit < 0."""
    if profit is None or profit >= 0:
        return
    if direction not in ("BUY", "SELL"):
        return
    target = canonical_symbol(symbol)
    ts = ts if ts is not None else time.time()
    recent_losses_per_symbol[target].append((ts, direction, float(entry_price or 0)))
    # Prune old entries
    cutoff = ts - LOSS_CLUSTER_WINDOW_S
    recent_losses_per_symbol[target] = [
        e for e in recent_losses_per_symbol[target] if e[0] >= cutoff
    ]

def is_loss_cluster_blocked(symbol, direction):
    """Return (blocked: bool, minutes_remaining: float). Blocked if >= threshold
    losses in same direction within window AND latest loss is fresh enough."""
    target = canonical_symbol(symbol)
    losses = recent_losses_per_symbol.get(target, [])
    if not losses:
        return False, 0.0
    now_ts = time.time()
    cutoff = now_ts - LOSS_CLUSTER_WINDOW_S
    matching_ts = [t for (t, d, _) in losses if t >= cutoff and d == direction]
    if len(matching_ts) < LOSS_CLUSTER_THRESHOLD:
        return False, 0.0
    most_recent = max(matching_ts)
    elapsed = now_ts - most_recent
    if elapsed < LOSS_CLUSTER_BLOCK_S:
        return True, (LOSS_CLUSTER_BLOCK_S - elapsed) / 60.0
    return False, 0.0
continuation_log_state = {}
hold_log_state       = {}
_htf_mismatch_warned = {}
_last_gate_report_ts = 0.0
GATE_REPORT_INTERVAL = 300

last_deal_check_utc = None

# =========================
# AUDIBLE ALERTS (Linux/PulseAudio — runs natively, bypasses Wine)
# =========================
SOUND_ALERTS_ENABLED = True
SOUND_FILES = {
    "open":  "/usr/share/sounds/freedesktop/stereo/complete.oga",
    "close": "/usr/share/sounds/freedesktop/stereo/bell.oga",
    "error": "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
}
_paplay_bin = shutil.which("paplay") or shutil.which("aplay")

def play_sound(event="open"):
    """Fire-and-forget audible alert. Non-blocking, silent on failure."""
    if not SOUND_ALERTS_ENABLED or not _paplay_bin:
        return
    path = SOUND_FILES.get(event)
    if not path or not Path(path).exists():
        return
    try:
        subprocess.Popen(
            [_paplay_bin, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        logger.debug("play_sound failed: %s", e)

# =========================
# BOT MONITORING - HEARTBEAT & UPTIME
# =========================
bot_start_time_utc      = None
last_heartbeat_utc     = None
last_heartbeat_log_utc = None
HEARTBEAT_INTERVAL_SEC = 300  # Log heartbeat every 5 minutes
TELEGRAM_HEARTBEAT_INTERVAL_SEC = 3600  # Telegram status every hour
HEARTBEAT_FILE         = BASE_DIR / "bot_heartbeat.log"
SHADOW_ACCURACY_CSV    = BASE_DIR / "shadow_accuracy.csv"
STARTUP_CHECK_HOURS    = 2    # Check for trades closed in last 2 hours on startup
last_telegram_heartbeat_utc = None

# =========================
# TICK FRESHNESS CHECK - DATA STALL DETECTION
# =========================
last_tick_prices       = {}  # Track last price per symbol
stale_tick_count       = {s: 0 for s in SYMBOLS}  # Count consecutive stale ticks
STALE_TICK_THRESHOLD  = 12   # Force reconnect after 12 consecutive stale ticks (1 minute at 5s cycle)

# =========================
# FTMO CHALLENGE TRACKING
# =========================
FTMO_PROFIT_TARGET_PCT = 0.05  # FTMO 5% profit target
MAX_TOTAL_LOSS_PERCENT = 0.10  # FTMO 10% max total loss requirement
challenge_start_equity = None  # Track starting equity for total loss calculation
profit_target_reached = False  # Track if profit target has been reached

_daily_reset_date  = None
daily_open_equity  = None
daily_halt         = False
daily_trade_counts = {s: 0 for s in SYMBOLS}

# TODO (pre-live): Persist daily state across restarts before going live on FTMO.
#   Currently daily_trade_counts, daily_halt, _daily_reset_date, and
#   daily_open_equity are in-memory only — a restart resets them, which is
#   intentional during TESTING_MODE but unsafe in production (could breach
#   MAX_TRADES_PER_DAY or bypass the 5% DAILY_DRAWDOWN_LIMIT halt).
#   Plan:
#     1. Dump state to ~/Downloads/ftmo_daily_state.json after each change.
#     2. Load on startup; if saved date == today (UTC), restore counters/halt.
#     3. Add optional --reset-daily CLI flag for emergency overrides.
#   Do this when switching TESTING_MODE=False for the live challenge.

SYMBOL_CONFIG = {
    "XAUUSD": {
        "atr_mult_tp": 2.0, "trail_mult": 0.5,  # widened from 0.3 — 1.2pt trail was too tight for XAU's per-tick noise
        "cooldown": 300 if TESTING_MODE else 600,
        "max_trades": 3 if TESTING_MODE else 2,
        "weekdays": [0,1,2,3,4], "sl_pips": 12, "tp_pips": 35, "type": "commodity",
        "atr_threshold": 0.3, "pip_value": 0.01, "spread_limit": 0.65, "spread_limit_kz": 0.8,  # V5.3 — raised from 0.60, evening session hitting 0.64
        "bos_min_disp_mult": 0.25, "bos_lookback": 40,
        "bos_use_wicks": True, "bos_require_cross": False,
        "pb_entry_buffer_atr_mult": 0.15, "cont_trigger_atr_mult": 0.60,
        "pb_exhaustion_atr_mult": 15.0,
        "min_rr": 1.5,    # Raised from 1.25 — Apr 24 XAU trades all filled at RR 1.25-1.54, TP clipped by liquidity target. 1.5 matches EUR/GBP floor and forces better R setups.
        "session_score_bonus": False,
        "session_gate": False,
        "zone_invalidate_mult": 2.5,
        "zone_edge_pct": 0.15,  # Pullback entries only at zone edges (top 15% for SELL, bottom 15% for BUY)
        "pending_ttl": 600,
        "same_bos_rearm_block": True,
        "min_sl_distance": 0.15,
        "entry_momentum_block_atr": 0.6,  # NEW Apr 28 — block PULLBACK entries when last completed entry-TF bar body > 0.6×ATR against trade direction. Targets the 5 fast XAU SLs today (-£1,483) which all fired into active counter-bounces. Set to 0 to disable.
        "mgmt_be_lock_r": 0.3,          # Tightened from 0.5 (Apr 28) — 3 fast XAU SLs in 2 days (-£923 combined: 20s/2min/5min) all peaked between 0.27-0.45R then reversed without ever locking BE. Lower threshold protects against violent counter-bounces in volatile XAU sessions.
        "mgmt_trail_trigger_r": 1.5,
        "mgmt_partial_enabled": False,  # Was True — partial@1.5R truncates runner; let full TP play out
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": False,
        "strict_atr_pullback": True,
        "pullback_enabled": False,      # V2: Pullback disabled — 10 fast losses = -£2,992 (May 2026 post-mortem). Breakout only.
    },
    "EURUSD": {
        "atr_mult_tp": 1.8, "trail_mult": 0.5,  # widened from 0.4 for consistency with XAU/GBP
        "cooldown": 300 if TESTING_MODE else 600,
        "max_trades": 2,                # V5.3 — unchanged, keep conservative
        "weekdays": [0,1,2,3,4], "sl_pips": 15, "tp_pips": 40, "type": "forex",
        "atr_threshold": 0.00008, "pip_value": 0.0001, "spread_limit": 0.0002,
        "bos_min_disp_mult": 0.30, "bos_lookback": 50,
        "bos_use_wicks": False, "bos_require_cross": False,
        "pb_entry_buffer_atr_mult": 0.10,
        "cont_trigger_atr_mult": 0.70, "pb_exhaustion_atr_mult": 8.0,
        "score_min_pullback": 2,        # V5.2 (May 14): Lowered 3→2 to increase trade frequency
        "score_min_breakout": 3,        # V5 (May 13): Lowered 4→3 to increase trade frequency
        "score_min_continuation": 4,    # Raised from 2 — continuation is risky
        "candle_body_mult_prime": 0.24,
        "candle_body_ratio_min": 0.38,
        "candle_wick_max_mult": 1.00,
        "session_gate": True,
        "zone_invalidate_mult": 0,      # DISABLED (Apr 28) — fired twice today locking in -£158 and -£141 losses (EUR #435963874, #436051789). Logic was labelling failed trades, not protecting them. Let hard SL (tighter than 2×ATR) handle the worst case; let TIME_STOP cull flat/slow trades.
        "pending_ttl": 300,
        "same_bos_rearm_block": True,
        "min_sl_distance": 0.00015,
        "mgmt_be_lock_r": 0.5,          # Reverted from 1.0 — FTMO report evidence (see XAUUSD comment)
        "mgmt_trail_trigger_r": 1.5,
        "mgmt_partial_enabled": True,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": True,
        "mgmt_time_stop_bars": 5,
        "mgmt_time_stop_min_r": 0.0,    # Was 0.5 — only cull red/flat trades, never greens
        "pullback_enabled": True,       # V3 (May 11): Re-enabled with score_min_pullback=4. Original V2 disable was due to 78% loss rate in challenge 1; that bot lacked HTF mismatch / entry_momentum / zone_edge / loss-cluster filters which now exist. Without this, EURUSD is dead weight — produced 0 BOS structures during today's drought while gates piled up htf_mismatch on USDJPY and pullback_disabled on XAU/EUR.
        "min_rr": 1.2,  # V5.3 — reduced from global 1.5, rr_below_min blocking valid low-ATR setups
    },
    "GBPUSD": {
        "atr_mult_tp": 1.8, "trail_mult": 0.5,  # widened from 0.2 — 1.4pip trail was dangerously tight, outlier among all symbols
        "cooldown": 300 if TESTING_MODE else 600,
        "max_trades": 3,                # V5.3 — raised from 2
        "max_trades_per_day": 6,        # V5.3 — raised from 4
        "weekdays": [0,1,2,3,4], "sl_pips": 15, "tp_pips": 40, "type": "forex",
        "atr_threshold": 0.0001, "pip_value": 0.0001, "spread_limit": 0.0003,
        "bos_min_disp_mult": 0.25, "bos_lookback": 60,
        "bos_use_wicks": False, "bos_require_cross": False,
        "pb_entry_buffer_atr_mult": 0.10,
        "cont_trigger_atr_mult": 0.70, "pb_exhaustion_atr_mult": 10.0,
        "score_min_pullback": 2,        # V5 (May 13): Lowered 3→2 to increase trade frequency
        "score_min_breakout": 3,        # V5 (May 13): Lowered 4→3 to increase trade frequency
        "score_min_continuation": 4,    # Raised from 2 — continuation is risky
        "candle_body_mult_prime": 0.24,
        "candle_body_ratio_min": 0.38,
        "candle_wick_max_mult": 1.00,
        "session_gate": True,
        "zone_invalidate_mult": 2.0,
        "pending_ttl": 300,
        "same_bos_rearm_block": True,
        "min_sl_distance": 0.0002,
        "mgmt_be_lock_r": 0.5,          # Reverted from 1.0 — FTMO report evidence (see XAUUSD comment)
        "mgmt_trail_trigger_r": 1.5,
        "mgmt_partial_enabled": True,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": False,
        "min_rr": 1.3,                  # Lowered from 1.5 — 8 RR rejections on valid pullback setups
    },
    "USDJPY": {
        "atr_mult_tp": 1.8, "trail_mult": 0.5,
        "atr_mult_sl": 1.2, "cooldown": 300,
        "max_trades": 2,                # V5.3 — still proving itself
        "weekdays": [0,1,2,3,4], "sl_pips": 80, "tp_pips": 160, "type": "forex",  # V5.4 — reduced from 150/225, 1.5 point SL was excessive for USDJPY
        # spread_limit tightened 2026-05-06 from 0.03 → 0.015 after observing
        # 0.014 spread during NY overlap on day 1. Normal USDJPY spread is
        # 0.003-0.004, so 0.015 = ~4x normal headroom — generous enough to
        # accept legitimate volatility-driven widening while blocking the
        # anomalous spikes that erode effective RR.
        "atr_threshold": 0.03, "pip_value": 0.01, "spread_limit": 0.015,
        "bos_min_disp_mult": 0.5,
        "pb_entry_buffer_atr_mult": 0.15,
        "cont_trigger_atr_mult": 0.25,
        "pb_exhaustion_atr_mult": 0.5,
        "min_rr": 1.5,
        "session_score_bonus": 2,
        "session_gate": True,
        "zone_invalidate_mult": 2.0,
        "pending_ttl": 600,
        "same_bos_rearm_block": True,
        "min_sl_distance": 150,
        "entry_momentum_block_atr": 0.3,  # V5.3 — lowered from 0.5, less aggressive counter-momentum blocking
        "pullback_enabled": True,
        "score_min_pullback": 2,   # V5.3 reduced from 3
        "score_min_breakout": 2,   # V5.3 reduced from 3
        "score_min_continuation": 2,  # V5.3 reduced from 3
        "breakout_body_ratio": 0.55, "breakout_close_pct": 0.30,
        "candle_body_mult_prime": 0.20,  # V5.3 — lowered from 0.35, was blocking 1640 valid USDJPY entries (4.2pip body req too strict)
        "candle_body_ratio_min": 0.45,   # V5.3 — slightly relaxed from 0.50
        "candle_wick_max_mult": 1.00,    # V5.3 — fully lenient on wicks, matching GBPJPY config
        "mgmt_be_lock_r": 0.5,
        "mgmt_trail_trigger_r": 0.8,
        "mgmt_partial_enabled": True, "mgmt_partial_r": 0.5, "mgmt_partial_pct": 0.5,
        "mgmt_lockin_enabled": True, "mgmt_lockin_r": 1.0, "mgmt_lockin_drawback": 0.65,
        "mgmt_trailing_enabled": True, "mgmt_trailing_min_r": 0.5,
        "mgmt_time_stop_enabled": True,
        "mgmt_time_stop_bars": 5,
        "mgmt_time_stop_min_r": 0.0,
        "strict_atr_pullback": True,
    },
    "GBPJPY": {  # V5 (May 13): Added
        "atr_mult_tp": 1.8, "trail_mult": 0.5,
        "cooldown": 300 if TESTING_MODE else 600,
        "max_trades": 3 if TESTING_MODE else 2,
        "max_trades_per_day": 3,
        "weekdays": [0,1,2,3,4],
        "sl_pips": 50, "tp_pips": 100, "type": "forex",  # V5.4 — reduced from 200/350, was 8x wider than GBPUSD in % terms, Claude correctly blocking
        "atr_threshold": 0.05, "pip_value": 0.01, "spread_limit": 0.08,
        "bos_min_disp_mult": 0.30, "bos_lookback": 50,
        "bos_use_wicks": False, "bos_require_cross": False,
        "pb_entry_buffer_atr_mult": 0.10,
        "cont_trigger_atr_mult": 0.70, "pb_exhaustion_atr_mult": 10.0,
        "score_min_pullback": 3,
        "score_min_breakout": 4,
        "score_min_continuation": 4,
        "candle_body_mult_prime": 0.24,
        "candle_body_ratio_min": 0.38,
        "candle_wick_max_mult": 1.00,
        "session_gate": True,
        "zone_invalidate_mult": 2.0,
        "pending_ttl": 300,
        "same_bos_rearm_block": True,
        "min_sl_distance": 0.05,
        "min_rr": 1.5,
        "mgmt_be_lock_r": 0.3,  # V5.3 — lowered from 0.5, Asia session choppiness was stopping out BE trades
        "mgmt_trail_trigger_r": 1.5,
        "mgmt_partial_enabled": True,
        "mgmt_lockin_enabled": False,
        "mgmt_time_stop_enabled": True,
        "mgmt_time_stop_bars": 5,
        "mgmt_time_stop_min_r": 0.0,
        "pullback_enabled": True,
        "entry_momentum_block_atr": 0.5,
        "strict_atr_pullback": True,
    },
}

# =========================
# V4 GOLD REGIME SYSTEM
# =========================
# XAUUSD regime-aware parameter overrides. Base config from SYMBOL_CONFIG applies,
# then these regime-specific parameters overlay for TRENDING/CORRECTIVE/COMPRESSION.
XAUUSD_REGIME_PARAMS = {
    "TRENDING": {
        "confirm_tf": None,  # Use H1 for HTF trend in trending mode (None = use default)
        "bos_lookback": 30,
        "bos_min_disp_mult": 0.35,
        "score_min_breakout": 4,
        "pullback_enabled": False,  # Breakout only in trending
        "require_sweep": False,
        "claude_gate": "shadow",  # Shadow only in trending — trust the rules
        "sl_pips": 15,
        "min_rr": 1.5,
        "trading_enabled": True,
    },
    "CORRECTIVE": {
        "confirm_tf": None,
        "bos_lookback": 20,           # Shorter lookback catches intraday structure
        "bos_min_disp_mult": 0.25,    # Lower threshold — corrective moves are smaller
        "score_min_breakout": 5,      # Higher score required — less noise tolerance
        "pullback_enabled": True,     # Re-enable pullbacks in corrective mode
        "score_min_pullback": 5,      # But require strong confluence
        "require_sweep": True,        # MANDATORY: sweep must precede BOS
        "claude_gate": "shadow",  # V5.4 temp — credits exhausted, reverting to shadow until topped up
        "sl_pips": 20,               # Wider SL for noisy conditions
        "min_rr": 2.0,               # Higher RR required to compensate
        "max_session_range_pct": 0.7, # Block if price already moved >70% of typical range
        "trading_enabled": True,
    },
    "COMPRESSION": {
        "trading_enabled": False,     # Hard stop — no gold trades in compression
        "claude_gate": "none",
    }
}

# Current gold regime state — updated by classify_gold_regime every 30 minutes
_current_gold_regime = "CORRECTIVE"  # Default until first classification
_last_regime_log_time = 0.0  # Epoch timestamp of last regime log

# V4 Sweep tracking for CORRECTIVE regime prerequisite
# Sweep valid for 30 minutes after detection (relevant for XAUUSD CORRECTIVE regime)
_last_sweep_detected = {"timestamp": 0.0, "sweep_type": None, "direction": None}  # XAUUSD only
SWEEP_VALID_DURATION_SECS = 1800  # 30 minutes


def _get_xauusd_effective_param(param_name, default=None):
    """Get effective parameter for XAUUSD considering current regime.
    Falls back to SYMBOL_CONFIG base if regime doesn't define the param."""
    if _current_gold_regime in XAUUSD_REGIME_PARAMS:
        regime_cfg = XAUUSD_REGIME_PARAMS[_current_gold_regime]
        if param_name in regime_cfg:
            return regime_cfg[param_name]
    # Fall back to base SYMBOL_CONFIG
    return SYMBOL_CONFIG["XAUUSD"].get(param_name, default)


def _compute_atr_from_arrays(highs, lows, closes, period=14):
    """Compute ATR from numpy arrays without MT5 fetch.
    Used by classify_gold_regime which already has the price arrays."""
    if len(closes) < period + 1:
        return 0.0
    h = highs[-period:]
    l = lows[-period:]
    c = closes[-period-1:-1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - c), np.abs(l - c)))
    return float(np.mean(tr))


def classify_gold_regime(symbol="XAUUSD"):
    """Classify gold market regime as TRENDING, CORRECTIVE, or COMPRESSION.

    Uses three factors:
    1. H4 ATR (14 period) vs 20-day average H4 ATR:
       - >1.3x average = elevated volatility (trending-friendly)
       - <0.7x average = compressed volatility (compression regime)

    2. H1 EMA50 direction (60 bars):
       - Price > EMA50 + 0.5×H1_ATR = clear BULL
       - Price < EMA50 - 0.5×H1_ATR = clear BEAR
       - Otherwise = NEUTRAL (price in EMA zone)

    3. H4 oscillation frequency (EMA20 crossings in last 20 bars):
       - >4 crossings = choppy/corrective

    Regime rules:
    - TRENDING:     H1 clear direction + H4 ATR elevated + <3 H4 crosses
    - COMPRESSION:  H4 ATR compressed + H1 NEUTRAL
    - CORRECTIVE:   Everything else (default for ambiguous conditions)

    Returns: "TRENDING" | "CORRECTIVE" | "COMPRESSION"
    """
    global _current_gold_regime, _last_regime_log_time

    # Fetch required data
    # H4 data for ATR regime and oscillation
    rates_h4 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 40)
    if not rates_ok(rates_h4, 25):
        logger.debug("[GOLD_REGIME] insufficient H4 data")
        return _current_gold_regime  # Keep current regime if data unavailable

    # H1 data for EMA50 direction
    rates_h1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 70)
    if not rates_ok(rates_h1, 60):
        logger.debug("[GOLD_REGIME] insufficient H1 data")
        return _current_gold_regime

    # 1. H4 ATR Analysis
    h4_closes = np.array([r["close"] for r in rates_h4], dtype=float)
    h4_highs = np.array([r["high"] for r in rates_h4], dtype=float)
    h4_lows = np.array([r["low"] for r in rates_h4], dtype=float)

    # Current H4 ATR (14 period)
    h4_atr_current = _compute_atr_from_arrays(h4_highs, h4_lows, h4_closes, 14)

    # 20-day average H4 ATR (approximately 120 H4 bars = 20 days)
    # Use first 30 bars of our 40-bar fetch as historical reference
    h4_atr_historical = _compute_atr_from_arrays(h4_highs[:30], h4_lows[:30], h4_closes[:30], 14) if len(h4_highs) >= 30 else h4_atr_current

    h4_atr_ratio = h4_atr_current / h4_atr_historical if h4_atr_historical > 0 else 1.0
    elevated_vol = h4_atr_ratio > 1.3
    compressed_vol = h4_atr_ratio < 0.7

    # 2. H1 EMA50 Direction
    h1_closes = np.array([r["close"] for r in rates_h1], dtype=float)
    h1_highs = np.array([r["high"] for r in rates_h1], dtype=float)
    h1_lows = np.array([r["low"] for r in rates_h1], dtype=float)

    h1_ema50 = calculate_ema(h1_closes, 60)
    h1_atr_14 = _compute_atr_from_arrays(h1_highs, h1_lows, h1_closes, 14)

    current_price = h1_closes[-1]
    h1_direction = "NEUTRAL"
    if h1_ema50 is not None and h1_atr_14 > 0:
        ema_distance = current_price - h1_ema50
        if ema_distance > 0.5 * h1_atr_14:
            h1_direction = "BULL"
        elif ema_distance < -0.5 * h1_atr_14:
            h1_direction = "BEAR"

    # 3. H4 Oscillation Frequency (EMA20 crossings in last 20 bars)
    h4_ema20 = calculate_ema(h4_closes, 20)
    h4_crosses = 0
    if h4_ema20 is not None:
        # Count crossings in last 20 bars
        for i in range(-20, 0):
            if i == -20:
                continue
            prev_diff = h4_closes[i-1] - h4_ema20
            curr_diff = h4_closes[i] - h4_ema20
            if prev_diff * curr_diff < 0:  # Sign change = crossing
                h4_crosses += 1

    # Regime Determination
    new_regime = "CORRECTIVE"  # Default

    if compressed_vol and h1_direction == "NEUTRAL":
        new_regime = "COMPRESSION"
    elif h1_direction in ("BULL", "BEAR") and elevated_vol and h4_crosses < 3:
        new_regime = "TRENDING"
    # else stays CORRECTIVE

    # Update global state
    _previous_regime = _current_gold_regime
    _current_gold_regime = new_regime

    if _previous_regime is not None and _previous_regime != _current_gold_regime:
        logger.info("[GOLD_REGIME_CHANGE] XAUUSD | %s → %s | h1_ema=%s | "
                    "h4_atr_ratio=%.2f | h4_crosses=%d",
                    _previous_regime, _current_gold_regime,
                    h1_direction, h4_atr_ratio, h4_crosses)

    # Log every 30 minutes
    now = time.time()
    if now - _last_regime_log_time >= 1800:  # 30 minutes
        _last_regime_log_time = now
        logger.info("[GOLD_REGIME] %s | regime=%s | h1_ema=%s | h4_atr_ratio=%.2f | h4_crosses=%d",
                    symbol, new_regime, h1_direction, h4_atr_ratio, h4_crosses)
        cp("GOLD_REGIME",
           f"[GOLD_REGIME] {symbol} | regime={new_regime} | h1_ema={h1_direction} | "
           f"h4_atr_ratio={h4_atr_ratio:.2f} | h4_crosses={h4_crosses}")
        _phoenix_emit("REGIME_CHANGE", symbol,
                      f"Gold regime: {new_regime} | h1={h1_direction} | atr_ratio={h4_atr_ratio:.2f}",
                      severity="INFO", department="TRADE",
                      metadata={"regime": new_regime, "h1_ema": h1_direction,
                                "h4_atr_ratio": round(h4_atr_ratio, 2), "h4_crosses": h4_crosses})

    return new_regime


def get_gold_regime_for_dashboard():
    """Return current gold regime for dashboard display."""
    return _current_gold_regime


def _update_sweep_tracking(symbol, sweep_result, direction):
    """Update global sweep tracking state when a sweep is detected.
    Only tracks for XAUUSD."""
    global _last_sweep_detected
    if symbol != "XAUUSD" or sweep_result is None:
        return
    _last_sweep_detected = {
        "timestamp": time.time(),
        "sweep_type": sweep_result,  # "SWEEP_HIGH" or "SWEEP_LOW"
        "direction": direction,       # "BUY" or "SELL"
    }
    logger.debug("[GOLD_REGIME] Sweep tracked: %s %s at %s",
                 sweep_result, direction, time.strftime("%H:%M:%S"))


def _sweep_prerequisite_met(symbol, direction):
    """Check if sweep prerequisite is met for CORRECTIVE regime.
    Returns True if:
    - Symbol is not XAUUSD (no sweep req)
    - Current regime is not CORRECTIVE
    - require_sweep is False for current regime
    - Sweep was detected within last 30 minutes matching trade direction
    Otherwise returns False and should block trade.
    """
    if symbol != "XAUUSD":
        return True
    if _current_gold_regime != "CORRECTIVE":
        return True
    if not _get_xauusd_effective_param("require_sweep", False):
        return True

    # Check if sweep is still valid (within 30 minutes)
    now = time.time()
    if now - _last_sweep_detected["timestamp"] > SWEEP_VALID_DURATION_SECS:
        return False

    # Check if sweep direction aligns with trade direction
    # SWEEP_HIGH aligns with SELL (swept liquidity above, now falling)
    # SWEEP_LOW aligns with BUY (swept liquidity below, now rising)
    sweep_type = _last_sweep_detected.get("sweep_type")
    if sweep_type == "SWEEP_HIGH" and direction == "SELL":
        return True
    if sweep_type == "SWEEP_LOW" and direction == "BUY":
        return True

    return False


# =========================
# V4 ECONOMIC CALENDAR INTEGRATION
# =========================
# Cache for economic calendar data with 1-hour TTL
EconomicCalendarCache = {
    "data": [],           # List of event dicts
    "last_fetch": 0.0,   # Epoch timestamp
    "ttl_secs": 3600,    # 1 hour TTL
}
ECONOMIC_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# 15 minute window before/after high-impact USD events
NEWS_WINDOW_MINUTES = 15


def _fetch_economic_calendar():
    """Fetch economic calendar from ForexFactory JSON API.
    Returns list of event dicts with fields: title, country, date, impact, time.
    Only fetches if cache is expired or empty."""
    now = time.time()
    cache = EconomicCalendarCache

    # Return cached data if still valid (even if empty within TTL)
    if (now - cache["last_fetch"]) < cache["ttl_secs"]:
        return cache["data"]  # return cached (even if empty) within TTL window

    try:
        req = urllib.request.Request(
            ECONOMIC_CALENDAR_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"},
            method="GET"
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))

        # Parse events - filter to high-impact USD events
        events = []
        for item in data:
            if not isinstance(item, dict):
                continue
            country = item.get("country", "").upper()
            impact = item.get("impact", "").upper()

            # Only care about high-impact USD events (affects gold)
            if country == "USD" and impact in ("HIGH", "H"):
                try:
                    # Parse date and time
                    date_str = item.get("date", "")
                    time_str = item.get("time", "")
                    if date_str and time_str and time_str != "All Day":
                        # Parse datetime
                        dt_str = f"{date_str} {time_str}"
                        event_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                        events.append({
                            "title": item.get("title", "Unknown"),
                            "datetime_utc": event_dt,
                            "impact": impact,
                        })
                except Exception:
                    continue

        # Update cache
        cache["data"] = events
        cache["last_fetch"] = now
        logger.info("[ECONOMIC_CALENDAR] Fetched %d high-impact USD events", len(events))
        return events

    except Exception as e:
        # Rate limit the retry — don't hammer the API
        if "429" in str(e):
            cache["last_fetch"] = now  # back off for full TTL period
            logger.debug("[ECONOMIC_CALENDAR] Rate limited (429) — backing off 1h")
        else:
            logger.warning("[ECONOMIC_CALENDAR] Fetch failed: %s", e)
        return cache.get("data", [])


def in_economic_news_window(symbol=None, minutes_before_after=NEWS_WINDOW_MINUTES):
    """Check if current time is within window of high-impact USD economic event.

    Args:
        symbol: Optional symbol (defaults to blocking all symbols if USD news)
        minutes_before_after: Minutes before and after event to consider as window

    Returns:
        (in_window: bool, event_info: dict|None)
    """
    # Only block for XAUUSD by default (other symbols less sensitive to USD news)
    if symbol and symbol != "XAUUSD":
        return False, None

    events = _fetch_economic_calendar()
    if not events:
        return False, None

    now_utc = datetime.utcnow()
    window_delta = timedelta(minutes=minutes_before_after)

    for event in events:
        event_dt = event.get("datetime_utc")
        if not event_dt:
            continue

        # Check if current time is within window
        if (event_dt - window_delta) <= now_utc <= (event_dt + window_delta):
            return True, event

    return False, None


# =========================
# CANONICAL SYMBOL HELPERS
# =========================
def canonical_symbol(symbol):
    """Map broker-suffixed symbols to base symbols (e.g., XAUUSD.suffix -> XAUUSD)."""
    s = str(symbol or "").upper()
    for base in SYMBOLS:
        if s == base or s.startswith(base):
            return base
    return s

def positions_for_symbol(symbol):
    """Get positions for a symbol using canonical matching (handles broker suffixes)."""
    target = canonical_symbol(symbol)
    return [p for p in (mt5.positions_get() or [])
            if canonical_symbol(getattr(p, "symbol", "")) == target]

# V5.3 — signed correlation map. +1 = block when both trades are the same direction.
# USDJPY removed: a EURUSD SELL and a USDJPY SELL are both USD-long via opposite legs
# (EUR-base vs JPY-quote) — not true correlation. GBPJPY correlates only via the GBP
# leg (i.e. with GBPUSD), not with EURUSD. Old flat map cost a GBPJPY trade on 14 May.
CORRELATION_MAP = {
    "EURUSD": [("GBPUSD", +1)],
    "GBPUSD": [("EURUSD", +1)],
    "USDJPY": [],
    "GBPJPY": [("GBPUSD", +1)],
    "XAUUSD": [],
}

def has_correlated_open_trade(symbol, direction):
    """Block opens on positively-correlated pairs only (signed map).

    Returns (blocked: bool, paired_symbol: str|None, paired_direction: str|None).
    +1 multiplier = block when open_direction == new_signal_direction.
    -1 multiplier = block when opposite (reserved for future inverse pairs).
    """
    target = canonical_symbol(symbol)
    if target not in CORRELATION_MAP:
        return False, None, None
    for pos in (mt5.positions_get() or []):
        pos_sym = canonical_symbol(pos.symbol)
        for paired_sym, multiplier in CORRELATION_MAP[target]:
            if pos_sym != paired_sym:
                continue
            pos_dir = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
            if multiplier > 0 and pos_dir == direction:
                return True, pos_sym, pos_dir
            if multiplier < 0 and pos_dir != direction:
                return True, pos_sym, pos_dir
    return False, None, None

def has_same_bos_open_trade(symbol, direction, bos_idx, entry_mode="pullback"):
    """True when the same symbol/direction/BOS is already represented by an open tracked trade."""
    target = canonical_symbol(symbol)
    for trade in open_trades.values():
        if canonical_symbol(trade.get("symbol")) != target:
            continue
        if trade.get("type") != direction:
            continue
        if trade.get("entry_mode") != entry_mode:
            continue
        if trade.get("bos_idx") == bos_idx:
            return True
    return False

def is_same_bos_rearm_blocked(symbol, direction, bos_idx, entry_mode="pullback"):
    target = canonical_symbol(symbol)
    if has_same_bos_open_trade(target, direction, bos_idx, entry_mode=entry_mode):
        return True
    if same_bos_rearm_blocks.get((target, direction, entry_mode)) == bos_idx:
        return True
    # Persistent check: has this exact BOS already been traded (even if closed)?
    if (target, direction, bos_idx, entry_mode) in traded_bos_set:
        return True
    return False

def validate_position_modify(pos, new_sl):
    """
    Validate SL modification for an ACTIVE position.
    For BUY: new SL must be below current bid
    For SELL: new SL must be above current ask
    """
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        return False
    if pos.type == mt5.ORDER_TYPE_BUY:
        if new_sl >= tick.bid:
            return False
    elif pos.type == mt5.ORDER_TYPE_SELL:
        if new_sl <= tick.ask:
            return False
    return True

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

# Verify AutoTrading is enabled in MT5 client. If disabled, MT5 silently
# rejects every order with retcode 10027 ("AutoTrading disabled by client"),
# making the bot look healthy while it captures zero trades. Abort loudly.
_ti = mt5.terminal_info()
if _ti is None or not getattr(_ti, "trade_allowed", False):
    _msg = ("AutoTrading is DISABLED in MT5. Enable it (press Ctrl+E or "
            "click the AutoTrading toolbar button so it turns GREEN), "
            "then restart the bot.")
    cp("ERROR", "❌❌❌ " + _msg + " ❌❌❌")
    logger.critical(_msg)
    raise SystemExit(2)
logger.info("[STARTUP] AutoTrading verified enabled (trade_allowed=True)")
tg_send(f"✅ <b>{BOT_NAME}</b> started\nAutoTrading verified | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# =========================
# DAILY TRACKERS
# =========================
def reset_daily_trackers():
    global _daily_reset_date, daily_open_equity, daily_halt, daily_trade_counts
    today = date.today()
    if _daily_reset_date == today:
        return
    daily_halt         = False
    daily_trade_counts = {s: 0 for s in SYMBOLS}
    _daily_reset_date  = today
    acc = mt5.account_info()
    if acc is None:
        logger.warning("Daily reset: account_info unavailable (counters reset anyway)")
        return
    daily_open_equity = acc.equity
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
           f"🛑 DAILY DD STOP | {drawdown*100:.2f}% "
           f"| open={daily_open_equity:.2f} | now={acc.equity:.2f}")
        tg_send(
            f"🚨 <b>DAILY DD HALT TRIGGERED</b>\n"
            f"Drawdown: <b>{drawdown*100:.2f}%</b>\n"
            f"Open: £{daily_open_equity:.2f}\n"
            f"Now: £{acc.equity:.2f}\n"
            f"<i>Bot will halt new trades until tomorrow.</i>"
        )
        logger.warning("DAILY DD STOP | %.2f%% | open=%.2f | now=%.2f",
                       drawdown * 100, daily_open_equity, acc.equity)
        _phoenix_emit("DAILY_DD_WARNING", "ALL",
                      f"Daily DD halt triggered | drawdown={drawdown*100:.2f}%",
                      severity="CRITICAL", department="RISK",
                      metadata={"drawdown_pct": round(drawdown * 100, 2),
                                "open_equity": daily_open_equity, "current_equity": acc.equity})
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
        now_utc   = get_utc_time()
        start_utc = (approx_open_utc - timedelta(days=2)
                     if isinstance(approx_open_utc, datetime)
                     else now_utc - timedelta(days=7))
        try:
            orders = mt5.history_orders_get(start_utc, now_utc, position=ticket)
        except Exception:
            orders = None
        if not orders:
            return []
        return [o for o in orders if getattr(o, "position_id", None) == ticket]

    def _history_deals_for_position(ticket, approx_open_utc):
        now_utc   = get_utc_time()
        start_utc = (approx_open_utc - timedelta(days=2)
                     if isinstance(approx_open_utc, datetime)
                     else now_utc - timedelta(days=7))
        try:
            deals = mt5.history_deals_get(start_utc, now_utc, position=ticket)
        except Exception:
            deals = None
        if not deals:
            return []
        return [d for d in deals if getattr(d, "position_id", None) == ticket]

    def _infer_open_utc(pos, deals):
        t = getattr(pos, "time", None)
        if isinstance(t, (int, float)) and t > 0:
            try:
                return datetime.fromtimestamp(t, tz=pytz.UTC)
            except Exception:
                pass
        in_deals = [d for d in (deals or []) if getattr(d, "entry", None) == mt5.DEAL_ENTRY_IN]
        if in_deals:
            dt = getattr(in_deals[0], "time", None)
            if isinstance(dt, (int, float)) and dt > 0:
                try:
                    return datetime.fromtimestamp(dt, tz=pytz.UTC)
                except Exception:
                    pass
        return None

    def _infer_initial_sl_tp(pos, orders):
        direction = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        init_sl = init_tp = None
        for o in orders or []:
            o_sl = float(getattr(o, "sl", 0.0) or 0.0)
            o_tp = float(getattr(o, "tp", 0.0) or 0.0)
            if o_sl > 0: init_sl = o_sl
            if o_tp > 0: init_tp = o_tp
            if init_sl is not None or init_tp is not None:
                break
        if init_sl is None: init_sl = float(pos.sl or 0.0)
        if init_tp is None: init_tp = float(pos.tp or 0.0)
        if not init_sl:
            atr = get_atr(pos.symbol, SIGNAL_TF)
            pos_key = canonical_symbol(pos.symbol)
            c   = SYMBOL_CONFIG.get(pos_key, {})
            dist = max(
                float(c.get("sl_pips", 0)) * float(c.get("pip_value", 0)),
                float(atr or 0),
            ) or abs(pos.price_open) * 0.001
            init_sl = normalize_price(pos.symbol,
                                      pos.price_open - dist if direction == "BUY"
                                      else pos.price_open + dist)
            cp("WARNING", f"⚠️  {pos.symbol} | Ticket:{pos.ticket} no SL — emergency SL={init_sl:.5f}")
            logger.warning("%s | Ticket:%s no SL — emergency SL=%.5f", pos.symbol, pos.ticket, init_sl)
            res = modify_position_sl_tp(pos, init_sl, pos.tp)
            if not (res and res.retcode == mt5.TRADE_RETCODE_DONE):
                log_order_result(res, "modify_sl_tp")
        return float(init_sl or 0.0), float(init_tp or 0.0)

    for pos in positions:
        pos_key = canonical_symbol(pos.symbol)
        if pos.ticket in open_trades or pos_key not in SYMBOL_CONFIG:
            continue
        direction       = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
        approx_open_utc = None
        try:
            if getattr(pos, "time", None):
                approx_open_utc = datetime.fromtimestamp(int(pos.time), tz=pytz.UTC)
        except Exception:
            pass
        deals    = _history_deals_for_position(pos.ticket, approx_open_utc)
        open_utc = _infer_open_utc(pos, deals)
        orders   = _history_orders_for_position(pos.ticket, open_utc or approx_open_utc)
        init_sl, init_tp = _infer_initial_sl_tp(pos, orders)
        sl_dist  = abs(pos.price_open - init_sl) if init_sl else 0.0
        open_trades[pos.ticket] = {
            "symbol": pos.symbol, "type": direction,
            "entry":  pos.price_open, "sl": init_sl, "tp": init_tp,
            "lot":    pos.volume, "risk_1r": sl_dist,
            "entry_mode": "manual_adopted" if pos.magic != MAGIC else "reconciled",
            "open_utc": open_utc,
            "partial_r": PARTIAL_TP_R,
            "trail_adj": 1.0,
        }
        be_locked[pos.ticket]           = (pos.sl >= pos.price_open if direction == "BUY"
                                           else (pos.sl <= pos.price_open and pos.sl != 0.0))
        if sl_dist > 0:
            r1_level[pos.ticket]        = (pos.price_open + sl_dist if direction == "BUY"
                                           else pos.price_open - sl_dist)
        peak_profit_tracker[pos.ticket] = max(pos.profit, 0.0)
        partial_done[pos.ticket]        = False

        # Option 2: Apply BE protection to reconciled trades if profitable
        if pos.magic == MAGIC and not be_locked[pos.ticket] and sl_dist > 0:
            sym_cfg = SYMBOL_CONFIG.get(pos_key, {})
            be_lock_r = sym_cfg.get("mgmt_be_lock_r", 1.0)
            be_target = (pos.price_open + sl_dist * be_lock_r if direction == "BUY"
                         else pos.price_open - sl_dist * be_lock_r)
            tick_info = mt5.symbol_info_tick(pos.symbol)
            if tick_info:
                current_price = tick_info.bid if direction == "BUY" else tick_info.ask
                spread_cost = tick_info.ask - tick_info.bid
                if (direction == "BUY" and current_price >= be_target) or (direction == "SELL" and current_price <= be_target):
                    info = mt5.symbol_info(pos.symbol)
                    one_pip = info.point if info else 0.0
                    be_sl = (pos.price_open + spread_cost + one_pip if direction == "BUY"
                            else pos.price_open - spread_cost - one_pip)
                    new_sl = normalize_price(pos.symbol, be_sl)
                    res = modify_position_sl_tp(pos, new_sl, pos.tp)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        be_locked[pos.ticket] = True
                        cp("BE_LOCK", f"[BE_LOCK] {pos.symbol} | Ticket:{pos.ticket} | SL moved to BE @ {new_sl:.5f}")
                        logger.info("[BE_LOCK] %s | Ticket:%s | SL moved to BE @ %.5f", pos.symbol, pos.ticket, new_sl)

        reconciled += 1
        tag = "ADOPT" if pos.magic != MAGIC else "RECONCILE"
        cp(tag, f"[{tag}] {pos.symbol} | Ticket:{pos.ticket} | {direction} | BE:{be_locked[pos.ticket]}")
        logger.info("[%s] %s | Ticket:%s | %s | BE:%s",
                    tag, pos.symbol, pos.ticket, direction, be_locked[pos.ticket])
    if reconciled:
        cp("SYSTEM", f"✅ Reconciled {reconciled} existing position(s)")
    else:
        cp("MARKET_CLOSED", "ℹ️  No existing positions to reconcile")

# =========================
# TIME / SESSION
# =========================
def get_utc_time():
    return datetime.utcnow().replace(tzinfo=pytz.UTC)

reconcile_open_positions()

def in_session(symbol, utc_dt):
    cfg      = SESSION_LOCAL[symbol]
    local_dt = utc_dt.astimezone(cfg["tz"])
    h        = local_dt.hour + local_dt.minute / 60.0
    for start, end in cfg["windows"]:
        if start <= h < end:
            return True
    return False

def in_blocked_hour(symbol, utc_dt):
    """V2: Hard block specific hours identified as high-loss windows."""
    h = utc_dt.hour + utc_dt.minute / 60.0
    # Block 22:xx UTC all symbols (4 trades, -£1,057, 0 wins in challenge)
    # V5.4 — 22xx block forex only, not XAUUSD (Sydney session active for gold)
    if 22.0 <= h < 23.0 and symbol != "XAUUSD":
        return True, "22xx_utc_block"
    # V5 (May 13): Narrowed XAUUSD block 15:00-17:00 UTC → 15:30-16:30 UTC (US data window only)
    if symbol == "XAUUSD" and 15.5 <= h < 16.5:
        return True, "xau_1530-1630_utc_block"
    return False, None

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
    if kz:   return f"KZ:{kz}"
    if in_asia_session(utc_dt): return "ASIA"
    if in_session(symbol, utc_dt): return "PRIME"
    return "OFF"

def check_spread_acceptable(symbol, context="check"):
    """Return True if current spread is within configured limit.
    `context` is a short tag used in the rejection log line so we can tell
    main-signal vs at-fill rechecks apart (e.g. context="at_fill").
    """
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False
    spread = tick.ask - tick.bid
    cfg = SYMBOL_CONFIG[symbol]
    utc_now = get_utc_time()
    if in_killzone(utc_now):
        limit = float(cfg.get("spread_limit_kz", cfg["spread_limit"]))
    else:
        limit = cfg["spread_limit"]
    if spread > limit:
        logger.info("%s spread_too_wide [%s] | actual=%.5f > limit=%.5f",
                    symbol, context, spread, limit)
        return False
    return True

# =========================
# FIX 1 (V13.1) — SESSION GATE FUNCTION
# =========================
# _session_gate_ok() was missing from V13. The session_gate flag in
# SYMBOL_CONFIG only took effect inside execute_trade(), which is too late —
# BOS detection, pending setup storage, and continuation evaluation all ran
# on EURUSD/GBPUSD overnight even though the backtest excluded those hours.
#
# Fix: _session_gate_ok() is now called at the TOP of both get_signal() and
# check_pending_pullbacks(). For symbols with session_gate=True (EURUSD,
# GBPUSD), the function returns False outside session windows AND outside
# killzones — matching the exact backtest entry condition.
# Gold is unaffected (session_gate=False → always True).

def _session_gate_ok(symbol, utc_dt):
    """
    Return True when this symbol is allowed to generate or evaluate signals.
    - session_gate=False (XAUUSD): always allowed.
    - session_gate=True  (EURUSD, GBPUSD): allowed only during session windows
      OR during a killzone (London Open 08-10, NY Open 13-15 London time).
    """
    if not SYMBOL_CONFIG[symbol].get("session_gate", False):
        return True
    return in_session(symbol, utc_dt) or (in_killzone(utc_dt) is not None)

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
    cached = _last_known_htf.get(symbol)
    if cached and time.time() - cached["ts"] < 600:
        return cached["trend"]
    rates = mt5.copy_rates_from_pos(symbol, CONFIRM_TF, 0, 60)
    if not rates_ok(rates, 50):
        if cached:
            return cached["trend"]
        return None
    closes = np.array([r["close"] for r in rates], dtype=float)
    ema50  = calculate_ema(closes, 50)
    if ema50 is None:
        if cached:
            return cached["trend"]
        return None
    if closes[-1] > ema50:   trend = "BULL"
    elif closes[-1] < ema50: trend = "BEAR"
    else:                    trend = None
    if trend:
        _last_known_htf[symbol] = {"trend": trend, "ts": time.time()}
    return trend

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

def _atr_bar_ok(arr, bar_idx):
    """
    Evaluate the three rejection criteria (below_p15, above_p90_spike,
    declining+low) against a single bar at `bar_idx` in the ATR history.
    Returns (True, None) on pass, (False, reason) on fail.
    """
    n = len(arr)
    if bar_idx < 0 or -bar_idx > n:
        return True, None  # not enough history; don't gate
    value = arr[bar_idx]
    p15   = np.percentile(arr, 15)
    p90   = np.percentile(arr, 90)
    if value < p15:
        return False, f"below_p15 | val={value:.6f} p15={p15:.6f}"
    if value > p90 * 1.35:
        return False, f"above_p90_spike | val={value:.6f} p90x1.35={p90 * 1.35:.6f}"
    # Declining check uses 5-bar lookback from this bar.
    lookback_idx = bar_idx - 5  # e.g. bar_idx=-1 → compare to arr[-6]
    if -lookback_idx <= n:
        ref = arr[lookback_idx]
        if value < ref * 0.85 and value < np.percentile(arr, 20):
            return False, (f"declining+low | val={value:.6f} "
                           f"ref*0.85={ref * 0.85:.6f} p20={np.percentile(arr, 20):.6f}")
    return True, None


def atr_regime_ok(symbol):
    """
    N=2 consecutive-bar hysteresis: both the current bar AND the prior bar must
    pass all three regime checks before we signal OK. This prevents single-tick
    flips (observed Apr 24: XAU flipped LOW→OK within 5s and fired a losing
    pullback entry). Costs ~1 bar of delay on genuine regime recoveries.
    """
    arr = list(ATR_HISTORY[symbol])
    if len(arr) < 30:
        return True

    cur_ok,  cur_reason  = _atr_bar_ok(arr, -1)
    if not cur_ok:
        logger.debug("%s atr_regime_ok=False | reason=%s", symbol, cur_reason)
        return False

    # Hysteresis — require prior bar to have also passed, when available.
    if len(arr) >= 2:
        prev_ok, prev_reason = _atr_bar_ok(arr, -2)
        if not prev_ok:
            logger.debug("%s atr_regime_ok=False | reason=hysteresis_prev_bar | prev_fail=%s",
                         symbol, prev_reason)
            return False

    return True

def should_trade_given_atr(symbol, utc_dt):
    """
    Return (ok, context) tuple for ATR-based entry decisions.
    During quiet periods (not session/KZ), LOW ATR blocks entry entirely.
    During active periods (session/KZ), allow it but increase score bar.
    """
    if atr_regime_ok(symbol):
        return True, "atr_ok"
    # LOW ATR detected
    if in_killzone(utc_dt):
        # NY/London Open — accept LOW ATR, but signal upstream for score penalty
        return True, "low_atr_in_kz"
    if in_session(symbol, utc_dt):
        # During session but not KZ — require higher evidence
        return True, "low_atr_in_session"
    # V5 (May 13): killzone always allowed regardless of ATR regime (defensive duplicate of earlier check)
    if in_killzone(utc_dt):
        return True, "low_atr_in_kz"
    # Off-session, quiet period — BLOCK entirely
    return False, "low_atr_blocking"

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
            if highs[i] > window_high and ((not require_cross) or (highs[i-1] <= window_high)):
                disp = highs[i] - window_high
                if disp > min_disp:
                    return "BOS_BUY", i, float(disp)
            if lows[i] < window_low and ((not require_cross) or (lows[i-1] >= window_low)):
                disp = window_low - lows[i]
                if disp > min_disp:
                    return "BOS_SELL", i, float(disp)
        else:
            if closes[i] > window_high and ((not require_cross) or (closes[i-1] <= window_high)):
                disp = closes[i] - window_high
                if disp > min_disp:
                    return "BOS_BUY", i, float(disp)
            if closes[i] < window_low and ((not require_cross) or (closes[i-1] >= window_low)):
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
    current  = closes[-1]
    lookback = 40
    if len(closes) < lookback:
        return None
    min_tp_dist = abs(entry - sl) * MIN_RR
    if direction == "BUY":
        pools = [max(highs[-lookback:])]
        for i in range(5, lookback):
            h = highs[-i]
            if abs(h - highs[-i-1]) <= np.std(highs[-lookback:]) * 0.15:
                if h > current and (h - entry) >= min_tp_dist:
                    pools.append(h)
        return min([p for p in pools if p > current and (p - entry) >= min_tp_dist], default=None)
    pools = [min(lows[-lookback:])]
    for i in range(5, lookback):
        l = lows[-i]
        if abs(l - lows[-i-1]) <= np.std(lows[-lookback:]) * 0.15:
            if l < current and (entry - l) >= min_tp_dist:
                pools.append(l)
    return max([p for p in pools if p < current and (entry - p) >= min_tp_dist], default=None)

def pullback_zone_from_impulse(direction, impulse_low, impulse_high):
    swing = impulse_high - impulse_low
    if direction == "BUY":
        return impulse_high - swing * PB_FAR, impulse_high - swing * PB_NEAR
    return impulse_low + swing * PB_NEAR, impulse_low + swing * PB_FAR

def zone_is_invalidated(direction, zone_low, zone_high, price, atr, mult=None):
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
        if impulse_low is None:  return False
        return price < (impulse_low - buf)
    if impulse_high is None: return False
    return price > (impulse_high + buf)

def pullback_impulse_exhaustion_ok(symbol, impulse_low, impulse_high, atr):
    impulse_range = impulse_high - impulse_low
    max_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("pb_exhaustion_atr_mult", PB_EXHAUSTION_ATR_MULT))
    return impulse_range <= atr * max_mult

def entry_momentum_counter_block(symbol, direction, atr, rates_entry):
    """
    Block PULLBACK entries when the most recent COMPLETED entry-TF bar shows
    strong counter-direction momentum. Pattern observed Apr 28: 5 fast XAU SLs
    (-£1,483 combined, 1-6 min duration) all fired during active bullish thrusts
    on the entry timeframe — SELL pullback signals firing into a continuing
    counter-bounce rather than a stable retracement, leading to instant SL hits.

    Threshold is per-symbol via 'entry_momentum_block_atr' config (0/missing = disabled).
    A value of 0.6 means: if last completed bar's body is >0.6×ATR against the
    intended trade direction, block the entry. Returns True if entry should be blocked.
    """
    threshold_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("entry_momentum_block_atr", 0) or 0)
    if threshold_mult <= 0 or atr is None or atr <= 0:
        return False
    if not rates_ok(rates_entry, 2):
        return False
    last_bar = rates_entry[-2]  # last COMPLETED bar (-1 is current forming bar)
    body = float(last_bar['close']) - float(last_bar['open'])
    threshold = threshold_mult * atr
    if direction == "SELL" and body > threshold:
        return True
    if direction == "BUY" and -body > threshold:
        return True
    return False

# =========================
# BREAKOUT CANDLE QUALITY FILTER
# =========================
def bos_candle_quality_ok(rates_signal, bos_idx, direction, symbol=None):
    if bos_idx < 3 or bos_idx >= len(rates_signal):
        return False
    cfg            = SYMBOL_CONFIG.get(symbol, {}) if symbol else {}
    body_ratio_min = float(cfg.get("breakout_body_ratio", BREAKOUT_BODY_RATIO))
    body_expand    = float(cfg.get("breakout_body_expand", BREAKOUT_BODY_EXPAND))
    close_pct      = float(cfg.get("breakout_close_pct", BREAKOUT_CLOSE_PCT))
    bos_c = rates_signal[bos_idx]
    o, h, l, c = bos_c["open"], bos_c["high"], bos_c["low"], bos_c["close"]
    body       = abs(c - o)
    rng        = max(h - l, 1e-9)
    if body / rng < body_ratio_min:
        logger.debug("BOS body_ratio %.2f < %.2f", body / rng, body_ratio_min)
        return False
    prior_bodies = [abs(rates_signal[j]["close"] - rates_signal[j]["open"])
                    for j in range(max(0, bos_idx - 3), bos_idx)]
    if prior_bodies:
        avg_prior = np.mean(prior_bodies)
        if avg_prior > 0 and body < avg_prior * body_expand:
            logger.debug("BOS body %.5f < avg_prior %.5f", body, avg_prior)
            return False
    if direction == "BUY" and c < h - rng * close_pct:
        logger.debug("BOS BUY close not in top %.0f%%", close_pct * 100)
        return False
    if direction == "SELL" and c > l + rng * close_pct:
        logger.debug("BOS SELL close not in bottom %.0f%%", close_pct * 100)
        return False
    return True

# =========================
# CONTINUATION QUALITY CHECKS
# =========================
def continuation_momentum_ok(rates_signal):
    if len(rates_signal) < 5:
        return True
    current_body = abs(rates_signal[-2]["close"] - rates_signal[-2]["open"])
    prior_bodies = [abs(rates_signal[-(i+3)]["close"] - rates_signal[-(i+3)]["open"])
                    for i in range(3)]
    avg_prior = np.mean(prior_bodies)
    if avg_prior == 0:
        return True
    ratio = current_body / avg_prior
    if ratio < CONT_MOMENTUM_RATIO:
        logger.debug("Continuation momentum fading: closed_ratio=%.2f < %.2f", ratio, CONT_MOMENTUM_RATIO)
        return False
    return True

def continuation_distance_ok(direction, bos_level, price, atr):
    dist = abs(price - bos_level)
    if dist > atr * CONT_MAX_DIST_MULT:
        logger.debug("Continuation distance %.5f > max %.1f×ATR", dist, CONT_MAX_DIST_MULT)
        return False
    return True

# =========================
# CANDLE CONFIRMATION (M1)
# =========================
CANDLE_FRESHNESS_SECS  = 30
CANDLE_BODY_MULT_ASIA  = 0.20
CANDLE_BODY_MULT_PRIME = 0.35

def _pick_confirmation_candle(rates_entry):
    """
    Use the last CLOSED candle ([-2]) when the gap between the two most recent
    candle open-times exceeds CANDLE_FRESHNESS_SECS, meaning [-1] is still forming.
    Otherwise use [-1] as it is fresh and representative.
    """
    if not rates_ok(rates_entry, 3):
        return -1
    try:
        time_gap = float(rates_entry[-1]["time"]) - float(rates_entry[-2]["time"])
        if time_gap > CANDLE_FRESHNESS_SECS:
            return -2
    except (KeyError, TypeError, ValueError):
        pass
    return -1

def candle_confirmation(direction, rates_entry, atr, is_asia=False, symbol=None):
    if not rates_ok(rates_entry, 3) or atr is None:
        return False
    cfg             = SYMBOL_CONFIG.get(symbol, {}) if symbol else {}
    body_mult_asia  = float(cfg.get("candle_body_mult_asia",  CANDLE_BODY_MULT_ASIA))
    body_mult_prime = float(cfg.get("candle_body_mult_prime", CANDLE_BODY_MULT_PRIME))
    body_ratio_min  = float(cfg.get("candle_body_ratio_min",  0.50))
    wick_max_mult   = float(cfg.get("candle_wick_max_mult",   0.85))
    candle_idx      = _pick_confirmation_candle(rates_entry)
    c               = rates_entry[candle_idx]
    o  = c["open"];  h = c["high"];  l = c["low"]
    body       = abs(c["close"] - o)
    rng        = max(h - l, 1e-9)
    upper_wick = h - max(o, c["close"])
    lower_wick = min(o, c["close"]) - l
    body_mult  = body_mult_asia if is_asia else body_mult_prime
    if body < atr * body_mult:
        logger.debug("candle_confirmation FAIL | body=%.5f < atr*%.2f=%.5f | session=%s",
                     body, body_mult, atr * body_mult, "Asia" if is_asia else "Prime")
        return False
    if direction == "BUY":
        ok = body / rng >= body_ratio_min and lower_wick <= body * wick_max_mult
    else:
        ok = body / rng >= body_ratio_min and upper_wick <= body * wick_max_mult
    if not ok:
        logger.debug("candle_confirmation FAIL | body_ratio=%.2f | wick check | dir=%s",
                     body / rng, direction)
    return ok

# =========================
# SCORING
# =========================
def trade_quality_score(sweep, displacement, atr, session_ok,
                        pullback_hit, liquidity_tp, killzone,
                        entry_mode="pullback", symbol=None, direction=None):
    score = 0
    if sweep in ("SWEEP_LOW", "SWEEP_HIGH"):                                score += 1
    if displacement is not None and atr is not None and displacement >= atr * 0.8:
        score += 1
    sess_bonus = SYMBOL_CONFIG.get(symbol, {}).get("session_score_bonus", True) if symbol else True
    if session_ok and sess_bonus:                                            score += 1
    if pullback_hit:                                                         score += 1
    if liquidity_tp is not None:                                             score += 2  # INCREASED from 1 — liquidity target is most reliable confluence
    if killzone is not None:                                                 score += 1
    if entry_mode == "continuation":                                         score -= 1
    # Stage 3: opportunity scanner tailwind — Claude-aligned setups get +1
    if symbol and direction:
        score += get_opportunity_score_bonus(symbol, direction)
    return score

# =========================
# RANGE POSITION PENALTY
# =========================
# Soft filter: penalises late entries in the exhausted end of the session range.
# Uses last RANGE_LOOKBACK_BARS on the signal TF as a proxy for session range.
# Returns -1 if direction is counter to the favourable end of range, else 0.
RANGE_LOOKBACK_BARS = 60
RANGE_EXHAUSTION_PCT = 0.20  # Bottom/top 20% counts as exhausted

def range_position_penalty(symbol, direction, rates_signal, price):
    try:
        if rates_signal is None or len(rates_signal) < 20:
            return 0
        lookback = min(RANGE_LOOKBACK_BARS, len(rates_signal))
        recent   = rates_signal[-lookback:]
        rng_high = float(max(r["high"] for r in recent))
        rng_low  = float(min(r["low"]  for r in recent))
        rng      = rng_high - rng_low
        if rng <= 0:
            return 0
        pos = (price - rng_low) / rng
        if direction == "SELL" and pos < RANGE_EXHAUSTION_PCT:
            logger.debug("%s range position %.0f%% — score penalty for late SELL", symbol, pos * 100)
            return -1
        if direction == "BUY" and pos > (1.0 - RANGE_EXHAUSTION_PCT):
            logger.debug("%s range position %.0f%% — score penalty for late BUY", symbol, pos * 100)
            return -1
    except Exception as e:
        logger.debug("range_position_penalty error on %s: %s", symbol, e)
    return 0

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
# SIGNAL ENGINE — V13.1
# =========================
# V13.1 fixes vs V13:
#
# FIX 1 — _session_gate_ok() added and called at TOP of get_signal().
#          For EURUSD/GBPUSD: returns HOLD immediately outside session+KZ.
#          This matches the backtest condition and stops overnight BOS
#          detection/pending setup storage for session-gated symbols.
#
# FIX 2 — HTF mismatch remains a hard unconditional HOLD (carried from V13,
#          unchanged). Warning logged once per BOS index to avoid spam.
#
# FIX 3 — Candle confirmation uses _pick_confirmation_candle() (carried
#          from V13, unchanged).
#
# FIX 4 (mgmt defaults) — see manage_trades() below.

def get_signal(symbol):
    if not market_open(symbol):
        _gate_hit(symbol, "market_closed")
        return "HOLD", 0.0, None

    utc_now = get_utc_time()

    # FIX 1 (V13.1) — session gate: block signal generation outside session
    if not _session_gate_ok(symbol, utc_now):
        _gate_hit(symbol, "session_gate")
        return "HOLD", 0.0, None

    if in_rollover_block(symbol, utc_now):
        _gate_hit(symbol, "rollover")
        return "HOLD", 0.0, None
    # V2: Hard block specific high-loss hours
    blocked, block_reason = in_blocked_hour(symbol, utc_now)
    if blocked:
        _gate_hit(symbol, block_reason)
        return "HOLD", 0.0, None
    if not check_spread_acceptable(symbol, context="signal"):
        _gate_hit(symbol, "spread")
        return "HOLD", 0.0, None

    rates_signal  = mt5.copy_rates_from_pos(symbol, SIGNAL_TF,  0, 80)
    rates_confirm = mt5.copy_rates_from_pos(symbol, CONFIRM_TF, 0, 70)
    rates_entry   = mt5.copy_rates_from_pos(symbol, ENTRY_TF,   0, 70)
    if not rates_ok(rates_signal,  40): _gate_hit(symbol, "rates_signal");  return "HOLD", 0.0, None
    if not rates_ok(rates_confirm, 50): _gate_hit(symbol, "rates_confirm"); return "HOLD", 0.0, None
    if not rates_ok(rates_entry,   20): _gate_hit(symbol, "rates_entry");   return "HOLD", 0.0, None

    s_highs  = np.array([r["high"]  for r in rates_signal], dtype=float)
    s_lows   = np.array([r["low"]   for r in rates_signal], dtype=float)
    s_closes = np.array([r["close"] for r in rates_signal], dtype=float)
    e_closes = np.array([r["close"] for r in rates_entry],  dtype=float)

    price = float(e_closes[-1])
    atr   = get_atr(symbol, SIGNAL_TF)
    if atr is None:
        _gate_hit(symbol, "atr_none")
        return "HOLD", price, None

    atr_entry  = get_entry_atr(symbol) or atr
    killzone   = in_killzone(utc_now)
    session_ok = in_session(symbol, utc_now)
    
    # NEW ATR regime gate — hard block in quiet periods, score penalties in active periods
    atr_ok, atr_context = should_trade_given_atr(symbol, utc_now)
    if not atr_ok:
        _gate_hit(symbol, atr_context)
        return "HOLD", price, None
    if atr_context != "atr_ok":
        logger.debug("%s ATR regime %s — proceeding with score penalty", symbol, atr_context)

    # V4 GOLD REGIME — classify and apply regime-specific parameters for XAUUSD
    xau_regime = None
    if symbol == "XAUUSD":
        xau_regime = classify_gold_regime(symbol)
        # Check if trading is disabled (COMPRESSION regime)
        if not _get_xauusd_effective_param("trading_enabled", True):
            _gate_hit(symbol, "gold_compression")
            return "HOLD", price, None
        # V4: Check economic calendar - block XAUUSD during high-impact USD news
        in_news, news_event = in_economic_news_window(symbol="XAUUSD")
        if in_news:
            event_title = news_event.get("title", "Unknown") if news_event else "High-impact USD event"
            _gate_hit(symbol, "economic_news")
            cp("WARNING", f"⚠️  {symbol} | BLOCKED — Economic news window | {event_title}")
            logger.info("%s | BLOCKED — Economic news window: %s", symbol, event_title)
            return "HOLD", price, None
        logger.debug("[GOLD_REGIME] %s using regime=%s", symbol, xau_regime)

    # Use regime-aware BOS parameters for XAUUSD, base config for others
    bos_lb        = _get_xauusd_effective_param("bos_lookback", 30) if symbol == "XAUUSD" else SYMBOL_CONFIG.get(symbol, {}).get("bos_lookback", 30)
    bos_mult      = _get_xauusd_effective_param("bos_min_disp_mult", 0.5) if symbol == "XAUUSD" else SYMBOL_CONFIG.get(symbol, {}).get("bos_min_disp_mult", 0.5)
    bos_wicks     = bool(SYMBOL_CONFIG.get(symbol, {}).get("bos_use_wicks", False))
    bos_req_cross = bool(SYMBOL_CONFIG.get(symbol, {}).get("bos_require_cross", True))
    bos_type, bos_idx, displacement = find_bos_candle_index(
        s_closes, s_highs, s_lows, atr,
        lookback=int(bos_lb), min_disp_mult=float(bos_mult),
        use_wicks=bos_wicks, require_cross=bos_req_cross,
    )
    if bos_type is None:
        last_seen_bos_index[symbol] = None
        _htf_mismatch_warned.pop((symbol, None), None)
        _gate_hit(symbol, "bos_none")
        return "HOLD", price, None

    if last_seen_bos_index.get(symbol) != bos_idx:
        _htf_mismatch_warned.pop((symbol, last_seen_bos_index.get(symbol)), None)
        _htf_mismatch_warned.pop((symbol, last_seen_bos_index.get(symbol), "pending"), None)
        # New BOS detected — drop traded_bos_set entries for OLD indices on this
        # symbol so fresh structure is tradable. Keep entries for the new idx
        # (just in case), but clear anything older.
        target = canonical_symbol(symbol)
        stale = {t for t in traded_bos_set if t[0] == target and t[2] != bos_idx}
        if stale:
            traded_bos_set.difference_update(stale)
            logger.debug("%s | cleared %d stale traded_bos entries on new BOS idx=%s",
                         symbol, len(stale), bos_idx)
    last_seen_bos_index[symbol] = bos_idx

    htf_trend = get_htf_trend(symbol)
    if htf_trend is None:
        _gate_hit(symbol, "htf_none")
        return "HOLD", price, None

    htf_mismatch = ((htf_trend == "BULL" and bos_type == "BOS_SELL") or
                    (htf_trend == "BEAR" and bos_type == "BOS_BUY"))
    # V5.3 — In CORRECTIVE regime, XAUUSD oscillates by definition
    # HTF flips constantly — allow both directions when CORRECTIVE
    gold_corrective = (symbol == "XAUUSD" and
                       _current_gold_regime == "CORRECTIVE")

    if htf_mismatch and not gold_corrective:
        warn_key = (symbol, bos_idx)
        if warn_key not in _htf_mismatch_warned:
            cp("WARNING",
               f"⚠️  {symbol} | HTF mismatch (HTF={htf_trend} vs BOS={bos_type}) — counter-trend blocked")
            logger.info("%s HTF mismatch blocked | HTF=%s BOS=%s", symbol, htf_trend, bos_type)
            _htf_mismatch_warned[warn_key] = True
        _gate_hit(symbol, "htf_mismatch")
        return "HOLD", price, None

    # V5.4 — Only block 00:00-01:00 UTC (rollover/thin liquidity)
    # 01:00-05:00 UTC is genuine Tokyo gold session — allow trading
    # Spread limit (0.65) and CORRECTIVE regime filter bad setups naturally
    if symbol == "XAUUSD" and in_asia_session(utc_now):
        utc_hour = utc_now.hour + utc_now.minute / 60.0
        if utc_hour < 1.0:
            _gate_hit(symbol, "asia_disabled")
            return "HOLD", price, None
        # 01:00-07:00 UTC allowed — Tokyo gold session + pre-London window

    direction = "BUY" if bos_type == "BOS_BUY" else "SELL"

    impulse_low, impulse_high = get_structural_impulse(s_highs, s_lows, bos_type, bos_idx)
    pull_low, pull_high       = pullback_zone_from_impulse(direction, impulse_low, impulse_high)
    sweep   = detect_sweep_improved(s_highs, s_lows, s_closes)
    # V4: Track sweep for CORRECTIVE regime prerequisite
    _update_sweep_tracking(symbol, sweep, direction)
    sl_mult = get_sl_atr_multiplier(utc_now)
    c       = SYMBOL_CONFIG[symbol]
    sl_dist = max(c["sl_pips"] * c["pip_value"], atr * 0.8 * sl_mult)
    prov_sl = price - sl_dist if direction == "BUY" else price + sl_dist
    liquidity_tp = detect_liquidity_target(symbol, direction, rates_signal, price, prov_sl)

    meta_base = {
        "liquidity_tp": liquidity_tp, "zone_low":  pull_low,
        "zone_high":    pull_high,    "atr":       atr,
        "sl_mult":      sl_mult,
        "bos_idx":      bos_idx,
        "htf":          htf_trend,
        "displacement": displacement,
    }

    pb_buf_mult    = float(SYMBOL_CONFIG.get(symbol, {}).get("pb_entry_buffer_atr_mult", 0.0))
    cont_trig_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("cont_trigger_atr_mult", CONTINUATION_THRESHOLD_MULT))
    # Use regime-aware score thresholds for XAUUSD
    score_min_bo   = int(_get_xauusd_effective_param("score_min_breakout", SCORE_MIN_BREAKOUT)) if symbol == "XAUUSD" else int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_breakout", SCORE_MIN_BREAKOUT))
    score_min_pb   = int(_get_xauusd_effective_param("score_min_pullback", SCORE_MIN_PULLBACK)) if symbol == "XAUUSD" else int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_pullback", SCORE_MIN_PULLBACK))
    score_min_cont = int(SYMBOL_CONFIG.get(symbol, {}).get("score_min_continuation", SCORE_MIN_CONTINUATION))
    asia           = in_asia_session(utc_now)

    # --- MODE 1: BREAKOUT ---
    # V5.4 — TRENDING breakout diagnostic logging
    if symbol == "XAUUSD" and _current_gold_regime == "TRENDING":
        _bos_fresh = is_fresh_bos(symbol, bos_idx)
        _htf_aligned = not ((htf_trend == "BULL" and bos_type == "BOS_SELL") or
                            (htf_trend == "BEAR" and bos_type == "BOS_BUY"))
        _disp_ok = displacement >= atr * BREAKOUT_DISP_MULT if displacement else False
        _blocked_by = (
            "stale_bos" if not _bos_fresh else
            "htf_mismatch" if not _htf_aligned else
            "insufficient_displacement" if not _disp_ok else
            "score_or_candle"
        )
        logger.info(
            "[XAUUSD_TRENDING_BREAKOUT] regime=%s | htf=%s | "
            "bos_type=%s | bos_idx=%s | fresh=%s | "
            "htf_aligned=%s | disp=%.5f | atr=%.5f | "
            "disp_req=%.5f | blocked_by=%s",
            _current_gold_regime, htf_trend,
            bos_type, bos_idx, _bos_fresh,
            _htf_aligned,
            displacement if displacement else 0,
            atr if atr else 0,
            (atr * BREAKOUT_DISP_MULT) if atr else 0,
            _blocked_by if not _bos_fresh or not _htf_aligned or not _disp_ok else "NONE_PROCEEDING",
        )

    if is_fresh_bos(symbol, bos_idx):
        if displacement >= atr * BREAKOUT_DISP_MULT:
            if bos_candle_quality_ok(rates_signal, bos_idx, direction, symbol=symbol):
                rates_entry_r = mt5.copy_rates_from_pos(symbol, ENTRY_TF, 0, 5)
                if rates_ok(rates_entry_r, 3):
                    if candle_confirmation(direction, rates_entry_r, atr_entry, is_asia=asia, symbol=symbol):
                        score     = trade_quality_score(sweep, displacement, atr, session_ok,
                                                        False, liquidity_tp, killzone, "breakout", symbol=symbol, direction=direction)
                        kz_str    = f" | KZ={killzone}" if killzone else ""
                        color_key = "BREAKOUT" if direction == "BUY" else "SELL"
                        cp(color_key,
                           f"{symbol} | BREAKOUT | {direction} | BOS[{bos_idx}] "
                           f"| DISP={displacement:.5f} | SCORE={score} | HTF={htf_trend}{kz_str}")
                        logger.info("%s | BREAKOUT | %s | BOS[%d] | DISP=%.5f | SCORE=%d | HTF=%s | KZ=%s",
                                    symbol, direction, bos_idx, displacement, score, htf_trend, killzone)
                        # Range position penalty (soft): -1 if entering late into exhausted range
                        score += range_position_penalty(symbol, direction, rates_signal, price)
                        # ATR-based score adjustment: session +2, kz +1
                        score_min_adjustment = 0
                        if atr_context == "low_atr_in_session":
                            score_min_adjustment = 2
                        elif atr_context == "low_atr_in_kz":
                            score_min_adjustment = 1
                        score_min = score_min_bo + score_min_adjustment
                        # V5.3 — sweep only required for PULLBACK in CORRECTIVE, not breakout
                        # Breakout displacement serves the same structural confirmation purpose
                        if score >= score_min:
                            pending_pulls.pop(symbol, None)
                            return direction, price, {**meta_base, "score": score, "entry_mode": "breakout"}
                        _gate_hit(symbol, "atr_regime" if atr_context != "atr_ok" else "score_below")
                        if atr_context == "atr_ok":
                            _log_rejected_signal(symbol, direction, "breakout", score, "score_below")
                    else:
                        _gate_hit(symbol, "candle_confirm")
                        _log_rejected_signal(symbol, direction, "breakout", 0, "candle_confirm")
                else:
                    _gate_hit(symbol, "rates_entry")
            else:
                cp("WARNING", f"⚠️  {symbol} | BREAKOUT blocked — weak BOS candle")
                logger.info("%s | BREAKOUT blocked — candle quality", symbol)
                _gate_hit(symbol, "bos_quality")

        bos_level = float(s_closes[bos_idx])
        if bos_wicks:
            bos_level = float(s_highs[bos_idx]) if bos_type == "BOS_BUY" else float(s_lows[bos_idx])
        pending_pulls[symbol] = {
            "direction": direction, "zone_low": pull_low, "zone_high": pull_high,
            "bos_type":  bos_type,  "bos_idx":  bos_idx,  "displacement": displacement,
            "sweep": sweep, "atr": atr, "session_ok": session_ok, "killzone": killzone,
            "bos_level": bos_level, "impulse_low": impulse_low, "impulse_high": impulse_high,
            "timestamp": time.time(),
        }
        return "HOLD", price, None

    # --- STRUCTURE INVALIDATION ---
    pending = pending_pulls.get(symbol)
    if pending:
        _stored_atr = pending.get("atr", atr)
        _pdir       = pending.get("direction", direction)
        if impulse_is_broken(_pdir, pending.get("impulse_low"), pending.get("impulse_high"),
                             price, _stored_atr, buffer_mult=0.25):
            cp("WARNING", f"⚠️  {symbol} | structure invalidated | clearing pending")
            logger.info("%s structure invalidated | dir=%s | price=%.5f | clearing pending (BOS idx retained)",
                        symbol, _pdir, price)
            pending_pulls.pop(symbol, None)
            _gate_hit(symbol, "invalidated")
            return "HOLD", price, None

    # --- MODE 2: PULLBACK ---
    pb_buf = atr * pb_buf_mult
    if (pull_low - pb_buf) <= price <= (pull_high + pb_buf):
        # Bug #2 — Universal hard-block on pullback entries in any LOW ATR regime.
        # Pullbacks require a zone wide enough to absorb retracement noise; LOW ATR
        # means the zone is too narrow and the SL is statistically certain to get
        # tagged. Breakouts are unaffected (they create their own momentum).
        if atr_context != "atr_ok":
            cp("WARNING",
               f"⚠️  {symbol} | PULLBACK blocked — LOW ATR regime ({atr_context})")
            logger.info("%s PULLBACK blocked — LOW ATR regime (%s)", symbol, atr_context)
            _gate_hit(symbol, "pb_low_atr")
            return "HOLD", price, None
        
        max_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("pb_exhaustion_atr_mult", PB_EXHAUSTION_ATR_MULT))
        if not pullback_impulse_exhaustion_ok(symbol, impulse_low, impulse_high, atr):
            imp_rng = impulse_high - impulse_low
            cp("WARNING",
               f"⚠️  {symbol} | PULLBACK blocked — impulse exhausted "
               f"(range={imp_rng:.5f} > {max_mult:.1f}×ATR={atr*max_mult:.5f})")
            logger.info("%s | PULLBACK blocked — impulse exhausted | range=%.5f > %.1f×ATR",
                        symbol, imp_rng, max_mult)
            _gate_hit(symbol, "pb_exhaustion")
            return "HOLD", price, None
        # V2/V4: Check if pullback is enabled for this symbol (regime-aware for XAUUSD)
        pb_enabled = _get_xauusd_effective_param("pullback_enabled", True) if symbol == "XAUUSD" else SYMBOL_CONFIG.get(symbol, {}).get("pullback_enabled", True)
        if not pb_enabled:
            logger.debug("%s | PULLBACK blocked — pullback disabled in config (V2/V4)", symbol)
            _gate_hit(symbol, "pullback_disabled")
            return "HOLD", price, None
        rates_entry_r = mt5.copy_rates_from_pos(symbol, ENTRY_TF, 0, 5)
        if rates_ok(rates_entry_r, 3):
            if entry_momentum_counter_block(symbol, direction, atr_entry, rates_entry_r):
                cp("WARNING",
                   f"⚠️  {symbol} | PULLBACK blocked — counter-momentum bar (last bar body > "
                   f"{SYMBOL_CONFIG.get(symbol, {}).get('entry_momentum_block_atr', 0)}×ATR against {direction})")
                logger.info("%s | PULLBACK blocked — counter_momentum | dir=%s",
                            symbol, direction)
                _gate_hit(symbol, "counter_momentum")
                return "HOLD", price, None
            if candle_confirmation(direction, rates_entry_r, atr_entry, is_asia=asia, symbol=symbol):
                score     = trade_quality_score(sweep, displacement, atr, session_ok,
                                                True, liquidity_tp, killzone, "pullback", symbol=symbol, direction=direction)
                if (SYMBOL_CONFIG.get(symbol, {}).get("same_bos_rearm_block", False)
                        and is_same_bos_rearm_blocked(symbol, direction, bos_idx, entry_mode="pullback")):
                    _gate_hit(symbol, "same_bos_open")
                    return "HOLD", price, None
                kz_str    = f" | KZ={killzone}" if killzone else ""
                sig_key   = "BUY" if direction == "BUY" else "SELL"
                cp(sig_key,
                   f"{symbol} | PULLBACK | {direction} | BOS[{bos_idx}] "
                   f"| DISP={displacement:.5f} | SCORE={score} | HTF={htf_trend}{kz_str}")
                logger.info("%s | PULLBACK | %s | BOS[%d] | DISP=%.5f | SCORE=%d | HTF=%s | KZ=%s",
                            symbol, direction, bos_idx, displacement, score, htf_trend, killzone)
                # Range position penalty (soft): -1 if entering late into exhausted range
                score += range_position_penalty(symbol, direction, rates_signal, price)
                # ATR-based score adjustment: session +2, kz +1
                score_min_adjustment = 0
                if atr_context == "low_atr_in_session":
                    score_min_adjustment = 2
                elif atr_context == "low_atr_in_kz":
                    score_min_adjustment = 1
                score_min = score_min_pb + score_min_adjustment
                # V4: Check sweep prerequisite for CORRECTIVE regime
                if not _sweep_prerequisite_met(symbol, direction):
                    _gate_hit(symbol, "no_sweep")
                    _log_rejected_signal(symbol, direction, "pullback", 0, "no_sweep")
                    cp("WARNING", f"⚠️  {symbol} | PULLBACK blocked — CORRECTIVE regime requires sweep within 30min")
                    logger.info("%s | PULLBACK blocked — no sweep in CORRECTIVE regime", symbol)
                    return "HOLD", price, None
                if score >= score_min:
                    pending_pulls.pop(symbol, None)
                    return direction, price, {**meta_base, "score": score, "entry_mode": "pullback"}
                _gate_hit(symbol, "atr_regime" if atr_context != "atr_ok" else "score_below")
                if atr_context == "atr_ok":
                    _log_rejected_signal(symbol, direction, "pullback", score, "score_below")
            else:
                _gate_hit(symbol, "candle_confirm")
                _log_rejected_signal(symbol, direction, "pullback", 0, "candle_confirm")
        else:
            _gate_hit(symbol, "rates_entry")

    # --- MODE 3: CONTINUATION ---
    if pending and "bos_level" in pending:
        bos_level = pending["bos_level"]
    else:
        bos_level = float(s_closes[bos_idx])
        if bos_wicks:
            bos_level = float(s_highs[bos_idx]) if bos_type == "BOS_BUY" else float(s_lows[bos_idx])

    cont_triggered = (price < bos_level - atr * cont_trig_mult if direction == "SELL"
                      else price > bos_level + atr * cont_trig_mult)
    if cont_triggered:
        cont_key = (symbol, bos_idx)
        if cont_fired.get(cont_key, False):
            _gate_hit(symbol, "cont_already")
            return "HOLD", price, None
        if not continuation_distance_ok(direction, bos_level, price, atr):
            cp("WARNING", f"⚠️  {symbol} | CONTINUATION blocked — distance > {CONT_MAX_DIST_MULT:.1f}×ATR")
            logger.info("%s | CONTINUATION blocked — distance", symbol)
            _gate_hit(symbol, "cont_distance")
            return "HOLD", price, None
        
        # NEW — tighten distance requirement during LOW ATR
        dist = abs(price - bos_level)
        if atr_context != "atr_ok":
            max_cont_dist = atr * CONT_MAX_DIST_MULT * 0.5  # half distance in LOW regime
            if dist > max_cont_dist:
                cp("WARNING", f"⚠️  {symbol} | CONTINUATION blocked — distance too far (LOW ATR)")
                logger.info("%s | CONTINUATION blocked — distance in LOW ATR regime", symbol)
                _gate_hit(symbol, "cont_distance_low_atr")
                return "HOLD", price, None
        if dist > atr * CONT_MOMENTUM_MIN_DIST_MULT:
            if not continuation_momentum_ok(list(rates_signal)):
                cp("WARNING", f"⚠️  {symbol} | CONTINUATION blocked — momentum fading")
                logger.info("%s | CONTINUATION blocked — momentum < %.2f", symbol, CONT_MOMENTUM_RATIO)
                _gate_hit(symbol, "cont_momentum")
                return "HOLD", price, None
        rates_entry_r = mt5.copy_rates_from_pos(symbol, ENTRY_TF, 0, 5)
        if rates_ok(rates_entry_r, 3):
            if candle_confirmation(direction, rates_entry_r, atr_entry, is_asia=asia, symbol=symbol):
                score  = trade_quality_score(sweep, displacement, atr, session_ok,
                                             False, liquidity_tp, killzone, "continuation", symbol=symbol, direction=direction)
                dist   = abs(price - bos_level)
                kz_str = f" | KZ={killzone}" if killzone else ""
                if _should_emit_continuation_log(symbol, bos_idx, direction, score, entry_mode="continuation"):
                    cp("CONTINUATION",
                       f"{symbol} | CONTINUATION | {direction} | BOS[{bos_idx}] "
                       f"| DIST={dist:.5f} | SCORE={score} | HTF={htf_trend}{kz_str}")
                    logger.info("%s | CONTINUATION | %s | BOS[%d] | DIST=%.5f | SCORE=%d | HTF=%s | KZ=%s",
                                symbol, direction, bos_idx, dist, score, htf_trend, killzone)
                # Range position penalty (soft): -1 if entering late into exhausted range
                score += range_position_penalty(symbol, direction, rates_signal, price)
                # ATR-based score adjustment: session +2, kz +1
                score_min_adjustment = 0
                if atr_context == "low_atr_in_session":
                    score_min_adjustment = 2
                elif atr_context == "low_atr_in_kz":
                    score_min_adjustment = 1
                score_min = score_min_cont + score_min_adjustment
                if score >= score_min:
                    cont_fired[cont_key] = True
                    pending_pulls.pop(symbol, None)
                    return direction, price, {**meta_base, "score": score, "entry_mode": "continuation"}
                _gate_hit(symbol, "atr_regime" if atr_context != "atr_ok" else "score_below")
            else:
                _gate_hit(symbol, "candle_confirm")
        else:
            _gate_hit(symbol, "rates_entry")

    return "HOLD", price, None

# =========================
# PENDING PULLBACK MONITOR
# =========================
PENDING_TTL = 300

def check_pending_pullbacks():
    expired = []
    utc_now = get_utc_time()

    for symbol, setup in list(pending_pulls.items()):
        ttl = SYMBOL_CONFIG[symbol].get("pending_ttl", PENDING_TTL)
        if time.time() - setup["timestamp"] > ttl:
            cp("DIAG", f"└ [PENDING] {symbol} setup expired after {ttl}s | clearing")
            logger.debug("[PENDING] %s expired after %ds", symbol, ttl)
            expired.append(symbol)
            _gate_hit(symbol, "pending_ttl")
            continue

        # FIX 1 (V13.1) — session gate applies in pending monitor too
        if not _session_gate_ok(symbol, utc_now):
            _gate_hit(symbol, "session_gate")
            continue

        if in_rollover_block(symbol, utc_now):
            _gate_hit(symbol, "rollover")
            continue
        if not market_open(symbol) or not check_spread_acceptable(symbol, context="pending_scan"):
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

        # HTF mismatch check — discard setup if HTF has flipped against direction
        htf_trend = get_htf_trend(symbol)
        if htf_trend is not None:
            htf_mismatch = ((htf_trend == "BULL" and direction == "SELL") or
                            (htf_trend == "BEAR" and direction == "BUY"))
            if htf_mismatch:
                pend_bos = setup.get("bos_idx", -1)
                warn_key = (symbol, pend_bos, "pending")
                if warn_key not in _htf_mismatch_warned:
                    cp("WARNING",
                       f"⚠️  {symbol} | HTF flipped vs pending {direction} — discarding")
                    logger.info("%s pending HTF mismatch | HTF=%s dir=%s — discarded",
                                symbol, htf_trend, direction)
                    _htf_mismatch_warned[warn_key] = True
                expired.append(symbol)
                _gate_hit(symbol, "htf_mismatch")
                continue

        _stored_atr = setup.get("atr", atr)
        inv_price   = tick.bid if direction == "BUY" else tick.ask
        if impulse_is_broken(direction, setup.get("impulse_low"), setup.get("impulse_high"),
                             inv_price, _stored_atr, buffer_mult=0.25):
            cp("WARNING", f"⚠️  {symbol} | pending structure invalidated | clearing")
            logger.info("%s pending structure invalidated | dir=%s | inv_price=%.5f",
                        symbol, direction, inv_price)
            expired.append(symbol)
            _gate_hit(symbol, "invalidated")
            continue

        # Bug #3 — Retracement exhaustion: expire pending setup if price has
        # retraced more than 65% of the original BOS displacement back toward
        # the BOS origin. Prevents re-entering on a stale structure after price
        # has done a full round-trip (classic late-entry trap).
        orig_displacement = float(setup.get("displacement", 0) or 0)
        if orig_displacement > 0:
            bos_origin_px = setup.get("bos_level")
            if bos_origin_px is not None:
                if direction == "SELL":
                    retracement_pct = (price - bos_origin_px) / orig_displacement
                else:  # BUY
                    retracement_pct = (bos_origin_px - price) / orig_displacement
                if retracement_pct > 0.65:
                    cp("WARNING",
                       f"⚠️  {symbol} | pending expired — {retracement_pct:.0%} retracement vs BOS")
                    logger.info("%s pending expired — retracement %.0f%% > 65%%",
                                symbol, retracement_pct * 100)
                    expired.append(symbol)
                    _gate_hit(symbol, "pb_retracement")
                    continue

        # Bug #2 — Hard-block pullback fills in LOW ATR regime (pending monitor).
        # Mirrors the same block in get_signal's pullback mode.
        _pending_atr_ok, _pending_atr_ctx = should_trade_given_atr(symbol, utc_now)
        if _pending_atr_ok and _pending_atr_ctx != "atr_ok":
            cp("WARNING",
               f"⚠️  {symbol} | pending PULLBACK blocked — LOW ATR regime ({_pending_atr_ctx})")
            logger.info("%s pending PULLBACK blocked — LOW ATR (%s)", symbol, _pending_atr_ctx)
            _gate_hit(symbol, "pb_low_atr")
            continue

        rates_signal = mt5.copy_rates_from_pos(symbol, SIGNAL_TF, 0, 80)
        if not rates_ok(rates_signal, 40):
            _gate_hit(symbol, "rates_signal")
            continue

        killzone     = in_killzone(utc_now)
        sl_mult      = get_sl_atr_multiplier(utc_now)
        c            = SYMBOL_CONFIG[symbol]
        sl_dist      = max(c["sl_pips"] * c["pip_value"], atr * 0.8 * sl_mult)
        prov_sl      = price - sl_dist if direction == "BUY" else price + sl_dist
        liquidity_tp = detect_liquidity_target(symbol, direction, rates_signal, price, prov_sl)

        pb_buf_mult    = float(SYMBOL_CONFIG.get(symbol, {}).get("pb_entry_buffer_atr_mult", 0.0))
        cont_trig_mult = float(SYMBOL_CONFIG.get(symbol, {}).get("cont_trigger_atr_mult", CONTINUATION_THRESHOLD_MULT))
        pb_buf         = atr * pb_buf_mult

        in_zone    = (setup["zone_low"] - pb_buf) <= price <= (setup["zone_high"] + pb_buf)
        cont_check = (price < bos_level - atr * cont_trig_mult if direction == "SELL"
                      else price > bos_level + atr * cont_trig_mult)

        if in_zone or cont_check:
            asia        = in_asia_session(utc_now)
            rates_entry = mt5.copy_rates_from_pos(symbol, ENTRY_TF, 0, 5)
            if not rates_ok(rates_entry, 3):
                _gate_hit(symbol, "rates_entry")
                continue
            atr_entry = get_entry_atr(symbol) or atr
            if entry_momentum_counter_block(symbol, direction, atr_entry, rates_entry):
                cp("WARNING",
                   f"⚠️  {symbol} | pending PULLBACK blocked — counter-momentum bar (last bar body > "
                   f"{SYMBOL_CONFIG.get(symbol, {}).get('entry_momentum_block_atr', 0)}×ATR against {direction})")
                logger.info("%s | pending PULLBACK blocked — counter_momentum | dir=%s",
                            symbol, direction)
                _gate_hit(symbol, "counter_momentum")
                continue
            if not candle_confirmation(direction, rates_entry, atr_entry, is_asia=asia, symbol=symbol):
                logger.debug("[PENDING] %s | candle_confirmation FAIL | %s | asia=%s",
                             symbol, direction, asia)
                _gate_hit(symbol, "candle_confirm")
                continue

        if in_zone:
            # Zone edge check - only enter at zone edges, not middle
            zone_edge_pct = float(SYMBOL_CONFIG.get(symbol, {}).get("zone_edge_pct", 0.0))
            if zone_edge_pct > 0:
                zone_height = setup["zone_high"] - setup["zone_low"]
                if direction == "SELL":
                    # For SELL, price must be in top zone_edge_pct of zone (near zone_high)
                    edge_threshold = setup["zone_high"] - zone_height * zone_edge_pct
                    if price < edge_threshold:
                        logger.info("%s | PENDING PULLBACK blocked — price %.2f below edge threshold %.2f (zone edge requirement)",
                                   symbol, price, edge_threshold)
                        _gate_hit(symbol, "zone_edge")
                        continue
                else:  # BUY
                    # For BUY, price must be in bottom zone_edge_pct of zone (near zone_low)
                    edge_threshold = setup["zone_low"] + zone_height * zone_edge_pct
                    if price > edge_threshold:
                        logger.info("%s | PENDING PULLBACK blocked — price %.2f above edge threshold %.2f (zone edge requirement)",
                                   symbol, price, edge_threshold)
                        _gate_hit(symbol, "zone_edge")
                        continue
            # V2/V4: Check if pullback is enabled for this symbol (regime-aware for XAUUSD)
            pb_enabled = _get_xauusd_effective_param("pullback_enabled", True) if symbol == "XAUUSD" else SYMBOL_CONFIG.get(symbol, {}).get("pullback_enabled", True)
            if not pb_enabled:
                logger.debug("%s | PENDING PULLBACK blocked — pullback disabled in config (V2/V4)", symbol)
                _gate_hit(symbol, "pullback_disabled")
                continue
            imp_lo = setup.get("impulse_low")
            imp_hi = setup.get("impulse_high")
            if imp_lo is not None and imp_hi is not None:
                if not pullback_impulse_exhaustion_ok(symbol, imp_lo, imp_hi, atr):
                    logger.info("%s | PENDING PULLBACK blocked — impulse exhausted", symbol)
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

        session_ok_now = in_session(symbol, utc_now)
        # V2: Hard block specific high-loss hours
        blocked, block_reason = in_blocked_hour(symbol, utc_now)
        if blocked:
            logger.info("%s | Signal blocked — %s (V2 hard block)", symbol, block_reason)
            _gate_hit(symbol, block_reason)
            continue
        score = trade_quality_score(
            setup.get("sweep"), setup["displacement"], atr,
            session_ok_now, in_zone, liquidity_tp, killzone, entry_mode, symbol=symbol, direction=direction,
        )
        # Range position penalty (soft): -1 if entering late into exhausted range
        score += range_position_penalty(symbol, direction, rates_signal, price)

        color_key = "CONTINUATION" if entry_mode == "continuation" else (
            "BUY" if direction == "BUY" else "SELL")
        if entry_mode != "continuation" or _should_emit_continuation_log(symbol, setup.get("bos_idx", -1), direction, score, entry_mode="pending_continuation"):
            cp(color_key, f"[PENDING→{entry_mode.upper()}] {symbol} | {direction} | SCORE={score}")
            logger.info("[PENDING→%s] %s | %s | SCORE=%d", entry_mode.upper(), symbol, direction, score)

        if score >= score_min:
            if (entry_mode == "pullback"
                    and SYMBOL_CONFIG.get(symbol, {}).get("same_bos_rearm_block", False)
                    and is_same_bos_rearm_blocked(symbol, direction, setup.get("bos_idx", -1), entry_mode="pullback")):
                expired.append(symbol)
                _gate_hit(symbol, "same_bos_open")
                continue
            # Re-validate spread at the moment of fill. Pending setups can sit
            # for up to pending_ttl seconds — spread conditions at setup-time
            # may be stale by fill time (observed Apr 24: XAU spread widened
            # from 0.3 to 0.57 during pending window, trade still fired).
            if not check_spread_acceptable(symbol, context="at_fill"):
                _gate_hit(symbol, "spread_at_fill")
                continue
            if entry_mode == "continuation":
                cont_fired[(symbol, setup.get("bos_idx", -1))] = True
            expired.append(symbol)
            execute_trade(symbol, direction, {
                "liquidity_tp": liquidity_tp, "zone_low":  setup["zone_low"],
                "zone_high":    setup["zone_high"], "score": score,
                "atr":          atr,               "sl_mult": sl_mult,
                "entry_mode":   entry_mode,
                "bos_idx":      setup.get("bos_idx"),
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
    done_retcode = getattr(mt5, "TRADE_RETCODE_DONE", None)
    retryable_retcodes = {
        code for code in (
            getattr(mt5, "TRADE_RETCODE_REQUOTE", None),
            getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", None),
            getattr(mt5, "TRADE_RETCODE_NO_PRICES", None),
        ) if code is not None
    }
    for _ in range(retries + 1):
        res = mt5.order_send(req)
        if res and done_retcode is not None and res.retcode == done_retcode:
            return res
        if res and res.retcode in retryable_retcodes:
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
    min_dist = max((info.trade_stops_level or 0) * point,
                   (info.trade_freeze_level or 0) * point)
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

    def _min_lot_loss():
        try:
            ts = sym.point; tv = sym.trade_tick_value
            if ts and tv:
                return (sl_distance / ts * tv) * sym.volume_min
        except Exception:
            pass
        p = (mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, symbol, sym.volume_min, entry_price, sl_price)
             if entry_price > sl_price
             else mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, sym.volume_min, entry_price, sl_price))
        return abs(p) if p is not None else None

    min_lot_risk = _min_lot_loss()
    if min_lot_risk is not None and min_lot_risk > risk_money * MAX_RISK_MULT:
        logger.warning("%s lot skip | min_lot_risk=$%.2f > %d× budget=$%.2f",
                       symbol, min_lot_risk, MAX_RISK_MULT, risk_money)
        cp("WARNING", f"⚠️  {symbol} skipped — min lot ${min_lot_risk:.2f} > "
                      f"{MAX_RISK_MULT}× budget ${risk_money:.2f}")
        return 0

    try:
        ts = sym.point; tv = sym.trade_tick_value
        if tv and ts:
            lot = risk_money / (sl_distance / ts * tv)
            lot = max(sym.volume_min, min(lot, max_lot))
            return round(round(lot / step) * step, 2)
    except Exception:
        pass

    def simulate_loss(test_lot):
        p = (mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, symbol, test_lot, entry_price, sl_price)
             if entry_price > sl_price
             else mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, test_lot, entry_price, sl_price))
        return float("inf") if p is None else abs(p)

    lot = best_lot = sym.volume_min
    while lot <= max_lot:
        if simulate_loss(lot) > risk_money:
            break
        best_lot = lot
        lot = round(lot + step, 2)
    return round(max(sym.volume_min, min(best_lot, max_lot)) / step * step, 2)

def dynamic_max_lot(equity):
    if equity < 100:    return 0.01
    if equity < 500:    return 0.03
    if equity < 1000:   return 0.05
    if equity < 5000:   return 0.15
    if equity < 10000:  return 0.30
    if equity < 25000:  return 0.60
    if equity < 50000:  return 1.00
    if equity < 100000: return 1.50
    if equity < 150000: return 2.00
    if equity < 200000: return 2.50
    return 3.00

def get_account_currency():
    acc = mt5.account_info()
    return acc.currency if acc else "GBP"

def current_open_risk_amount():
    total_risk = 0.0
    positions = mt5.positions_get() or []
    for pos in positions:
        if getattr(pos, "magic", None) != MAGIC:
            continue
        try:
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                continue
            current_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
            profit = mt5.order_calc_profit(
                mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                pos.symbol,
                pos.volume,
                current_price,
                pos.sl,
            )
            if profit is not None:
                total_risk += abs(profit)
        except Exception:
            pass
    return total_risk

def check_total_exposure(symbol, proposed_risk_money):
    acc = mt5.account_info()
    if not acc or acc.equity <= 0:
        return False, "no_account"
    open_risk = current_open_risk_amount()
    max_total_risk = acc.equity * MAX_TOTAL_RISK_PERCENT
    if open_risk + proposed_risk_money > max_total_risk:
        return False, f"open_risk={open_risk:.2f}|new_risk={proposed_risk_money:.2f}|max={max_total_risk:.2f}"
    return True, "ok"

def check_margin_available(symbol, lot, direction, price):
    acc = mt5.account_info()
    if not acc:
        return False
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    margin_needed = mt5.order_calc_margin(order_type, symbol, lot, price)
    if margin_needed is None:
        return False
    return acc.margin_free >= margin_needed * 1.5

def calculate_sl_tp_liquidity(symbol, direction, entry, liquidity_tp, atr, sl_mult, entry_mode,
                              zone_low=None, zone_high=None, utc_dt=None):
    c           = SYMBOL_CONFIG[symbol]
    sym_min_rr  = float(c.get("min_rr", MIN_RR))
    atr_sl_mult = 0.6 if entry_mode == "continuation" else 0.8
    buf_k = 0.25
    if utc_dt is not None:
        if in_killzone(utc_dt):    buf_k = 0.20
        elif in_asia_session(utc_dt): buf_k = 0.35
    min_sl_distance = float(c.get("min_sl_distance", 0.0))
    sl_distance = max(c["sl_pips"] * c["pip_value"], atr * atr_sl_mult * sl_mult, min_sl_distance)
    if direction == "BUY":
        sl = entry - sl_distance
        if zone_low is not None:
            try: sl = min(sl, float(zone_low) - atr * buf_k)
            except Exception: pass
        tp = (liquidity_tp if liquidity_tp is not None and liquidity_tp > entry
              else entry + max(atr * c["atr_mult_tp"] * sl_mult, sl_distance * sym_min_rr))
    else:
        sl = entry + sl_distance
        if zone_high is not None:
            try: sl = max(sl, float(zone_high) + atr * buf_k)
            except Exception: pass
        tp = (liquidity_tp if liquidity_tp is not None and liquidity_tp < entry
              else entry - max(atr * c["atr_mult_tp"] * sl_mult, sl_distance * sym_min_rr))
    return normalize_price(symbol, sl), normalize_price(symbol, tp)

# =========================
# TRADE EXECUTION
# =========================
def execute_trade(symbol, direction, meta):
    global daily_trade_counts

    if daily_halt:
        _gate_hit(symbol, "daily_halt"); return
    # Per-symbol daily cap override (e.g. GBPUSD raised to 4); falls back to global MAX_TRADES_PER_DAY.
    sym_daily_cap = int(SYMBOL_CONFIG.get(symbol, {}).get("max_trades_per_day", MAX_TRADES_PER_DAY))
    if daily_trade_counts.get(symbol, 0) >= sym_daily_cap:
        cp("WARNING", f"⚠️  {symbol} daily trade limit ({sym_daily_cap}) reached")
        _gate_hit(symbol, "daily_limit"); return

    # V5.1 — GBPJPY shadow mode: observe but don't execute during validation period
    if symbol == "GBPJPY" and GBPJPY_SHADOW_MODE:
        elapsed_days = (datetime.utcnow().replace(tzinfo=pytz.UTC) - GBPJPY_SHADOW_START).total_seconds() / 86400
        if elapsed_days < 7:
            # Calculate what the trade would have been
            _tick = mt5.symbol_info_tick(symbol)
            _entry = (_tick.ask if direction == "BUY" else _tick.bid) if _tick else 0
            _atr = meta.get("atr") or get_atr(symbol, SIGNAL_TF) or 0
            _sl_mult = meta.get("sl_mult", get_sl_atr_multiplier(get_utc_time()))
            _entry_mode = meta.get("entry_mode", "pullback")
            _sl, _tp = calculate_sl_tp_liquidity(
                symbol, direction, _entry, meta.get("liquidity_tp"), _atr, _sl_mult, _entry_mode,
                zone_low=meta.get("zone_low"), zone_high=meta.get("zone_high"), utc_dt=get_utc_time()
            ) if _entry > 0 else (0, 0)
            _sl_dist = abs(_entry - _sl)
            _tp_dist = abs(_tp - _entry)
            _rr = round(_tp_dist / _sl_dist, 2) if _sl_dist > 0 else 0
            _lot = calculate_lot(symbol, _entry, _sl, risk_percent=RISK_PERCENT) if _entry > 0 else 0

            logger.info("[GBPJPY_SHADOW] %s %s | score=%s | entry=%.3f | sl=%.3f | tp=%.3f | rr=%.2fR | lot=%.2f | day=%.1f/7",
                        symbol, direction, meta.get("score", 0), _entry, _sl, _tp, _rr, _lot, elapsed_days)
            cp("WARNING", f"👻 [GBPJPY_SHADOW] {direction} | score={meta.get('score',0)} | {_entry:.3f} → TP:{_tp:.3f} SL:{_sl:.3f} | {_rr}R | Day {elapsed_days:.1f}/7")
            tg_send(
                f"👻 <b>GBPJPY SHADOW</b> {direction} ({_entry_mode.upper()})\n"
                f"Entry: <code>{_entry:.3f}</code>\n"
                f"SL: <code>{_sl:.3f}</code> | TP: <code>{_tp:.3f}</code>\n"
                f"RR: {_rr}R | Lot: {_lot} | Score: {meta.get('score',0)}\n"
                f"Day {elapsed_days:.1f}/7 — shadow only"
            )
            _gate_hit(symbol, "gbpjpy_shadow")
            _log_rejected_signal(symbol, direction, meta.get("entry_mode", "?"), meta.get("score", 0), "gbpjpy_shadow")
            return

    c          = SYMBOL_CONFIG[symbol]
    now        = time.time()
    entry_mode = meta.get("entry_mode", "pullback")

    # Correlation guard — block same-direction opens on correlated pairs
    corr_blocked, corr_sym, corr_dir = has_correlated_open_trade(symbol, direction)
    if corr_blocked:
        cp("WARNING", f"⚠️  {symbol} | correlation block | {direction} blocked by {corr_sym} {corr_dir} (corr=+1)")
        logger.info("%s correlation block | dir=%s | paired with %s %s (corr=+1)",
                    symbol, direction, corr_sym, corr_dir)
        _gate_hit(symbol, "correlation_open"); return

    # Score validation
    score     = meta.get("score", 0)
    score_min = int(SYMBOL_CONFIG.get(symbol, {}).get(
        "score_min_breakout"     if entry_mode == "breakout"     else
        "score_min_continuation" if entry_mode == "continuation" else
        "score_min_pullback",
        SCORE_MIN_BREAKOUT if entry_mode == "breakout" else
        SCORE_MIN_CONTINUATION if entry_mode == "continuation" else SCORE_MIN_PULLBACK,
    ))
    if score < score_min:
        cp("WARNING", f"⚠️  {symbol} score {score} < {score_min} for {entry_mode} — blocked")
        logger.warning("%s score validation blocked | score=%d < %d | mode=%s",
                       symbol, score, score_min, entry_mode)
        _gate_hit(symbol, "score_validation"); return

    # Session gate (defence-in-depth — primary gate is now in get_signal)
    if c.get("session_gate", False):
        utc_now = get_utc_time()
        if not in_session(symbol, utc_now) and in_killzone(utc_now) is None:
            cp("WARNING", f"⚠️  {symbol} | session gate blocked at execution")
            logger.info("%s session gate blocked at execution | mode=%s", symbol, entry_mode)
            _gate_hit(symbol, "session_gate"); return

    if (signal_cooldown.get(symbol) == direction
            and now - last_trade_time.get(symbol, 0) < c["cooldown"]):
        _gate_hit(symbol, "cooldown"); return

    # Loss-cluster gate: 2+ losses in same direction within 90 min => block 60 min
    _lc_blocked, _lc_minutes_left = is_loss_cluster_blocked(symbol, direction)
    if _lc_blocked:
        cp("WARNING",
           f"⛔ {symbol} {direction} blocked by loss cluster | "
           f"{_lc_minutes_left:.0f}min remaining")
        logger.info("%s loss-cluster block | dir=%s | %.0fmin remaining",
                    symbol, direction, _lc_minutes_left)
        # Telegram alert once per (symbol, direction) until block expires
        _alert_key = (canonical_symbol(symbol), direction)
        if _alert_key not in _loss_cluster_alerted:
            _loss_cluster_alerted.add(_alert_key)
            tg_send(
                f"⛔ <b>LOSS CLUSTER BLOCK</b>\n"
                f"{symbol} {direction} blocked for {_lc_minutes_left:.0f} min\n"
                f"<i>2+ losses in same direction in last 90 min.</i>"
            )
        _log_rejected_signal(symbol, direction, entry_mode, meta.get("score", 0), "loss_cluster")
        _gate_hit(symbol, "loss_cluster"); return
    else:
        # Block has expired — clear any prior alert flag so a future cluster re-alerts
        _loss_cluster_alerted.discard((canonical_symbol(symbol), direction))

    open_pos = positions_for_symbol(symbol)
    if len(open_pos) >= c["max_trades"]:
        cp("WARNING", f"⚠️  {symbol} max open positions ({c['max_trades']}) reached")
        _gate_hit(symbol, "maxtrades"); return

    same_dir = [p for p in open_pos
                if (direction == "BUY"  and p.type == mt5.ORDER_TYPE_BUY) or
                   (direction == "SELL" and p.type == mt5.ORDER_TYPE_SELL)]
    if same_dir:
        bos_idx = (meta or {}).get("bos_idx")
        entry_mode = (meta or {}).get("entry_mode", "pullback")
        if bos_idx is not None and SYMBOL_CONFIG.get(symbol, {}).get("same_bos_rearm_block", False):
            same_bos_rearm_blocks[(canonical_symbol(symbol), direction, entry_mode)] = bos_idx
        cp("WARNING", f"⚠️  {symbol} | same-direction block | {direction} already open")
        logger.info("%s same-direction block | dir=%s | existing=%d", symbol, direction, len(same_dir))
        _gate_hit(symbol, "same_direction"); return

    if not ensure_symbol(symbol):
        _gate_hit(symbol, "ensure_symbol"); return
    if not check_spread_acceptable(symbol, context="execute"):
        cp("WARNING", f"⚠️  {symbol} spread too wide at execution")
        _gate_hit(symbol, "spread"); return

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        cp("ERROR", f"❌ No tick for {symbol}")
        _gate_hit(symbol, "tick_none"); return

    utc_dt  = get_utc_time()
    entry   = tick.ask if direction == "BUY" else tick.bid
    atr     = meta.get("atr") or get_atr(symbol, SIGNAL_TF)
    sl_mult = meta.get("sl_mult", get_sl_atr_multiplier(utc_dt))
    if atr is None:
        _gate_hit(symbol, "atr_none"); return

    sl, tp  = calculate_sl_tp_liquidity(
        symbol, direction, entry, meta.get("liquidity_tp"), atr, sl_mult, entry_mode,
        zone_low=meta.get("zone_low"), zone_high=meta.get("zone_high"), utc_dt=utc_dt,
    )
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)

    sym_min_rr = float(SYMBOL_CONFIG[symbol].get("min_rr", MIN_RR))
    if tp_dist < sl_dist * sym_min_rr:
        cp("WARNING", f"⚠️  {symbol} RR={tp_dist/sl_dist:.2f}R < {sym_min_rr}R — skip")
        _log_rejected_signal(symbol, direction, entry_mode, meta.get("score", 0), "rr_below_min")
        _gate_hit(symbol, "rr"); return

    ok, reason = validate_sl_tp(symbol, entry, sl, tp, direction)
    if not ok:
        cp("ERROR", f"❌ SL/TP invalid {symbol} | {reason}")
        _gate_hit(symbol, "sltp"); return

    # Score-based risk scaling: high-conviction setups (5+/6+) get a modest bump.
    # Falls back to base RISK_PERCENT on marginal scores or if scaling is disabled.
    scaled_risk = RISK_PERCENT
    if RISK_SCALING_ENABLED:
        trade_score = int(meta.get("score", 0))
        for min_score, tier_risk in sorted(RISK_BY_SCORE.items(), reverse=True):
            if trade_score >= min_score:
                scaled_risk = min(tier_risk, MAX_RISK_PERCENT)
                logger.info("%s risk-scaling | score=%d → risk=%.3f%% (base=%.3f%%)",
                            symbol, trade_score, scaled_risk * 100, RISK_PERCENT * 100)
                break

    # Capture £-denominated 1R risk before lot sizing (used for accurate r_multiple on close)
    _acc_at_open = mt5.account_info()
    risk_gbp = float(_acc_at_open.equity * scaled_risk) if _acc_at_open else 0.0
    lot = calculate_lot(symbol, entry, sl, risk_percent=scaled_risk)
    if lot <= 0:
        cp("ERROR", f"❌ Invalid lot for {symbol}")
        _gate_hit(symbol, "lot"); return

    proposed_risk_money = abs(mt5.order_calc_profit(
        mt5.ORDER_TYPE_SELL if direction == "BUY" else mt5.ORDER_TYPE_BUY,
        symbol,
        lot,
        entry,
        sl,
    ) or 0.0)
    exposure_ok, exposure_reason = check_total_exposure(symbol, proposed_risk_money)
    if not exposure_ok:
        cp("WARNING", f"⚠️  {symbol} exposure limit reached | {exposure_reason}")
        _gate_hit(symbol, "exposure_limit"); return

    if not check_margin_available(symbol, lot, direction, entry):
        cp("WARNING", f"⚠️  {symbol} insufficient margin for {lot} lots")
        _gate_hit(symbol, "margin"); return

    # V4: Hard gate check for XAUUSD CORRECTIVE regime
    claude_gate_mode = _get_xauusd_effective_param("claude_gate", "shadow") if symbol == "XAUUSD" else "shadow"
    if symbol == "XAUUSD" and claude_gate_mode == "hard":
        approved = _claude_hard_gate_evaluate(
            symbol, direction, entry_mode, meta.get("score", 0), meta, utc_dt, atr, sl_dist
        )
        if not approved:
            _gate_hit(symbol, "claude_hard_block")
            _log_rejected_signal(symbol, direction, entry_mode, meta.get("score", 0), "claude_hard_block")
            cp("WARNING", f"🚫 {symbol} {direction} blocked by Claude hard gate")
            logger.warning("%s %s | Hard gate blocked trade", symbol, direction)
            return

    # Fix 1: Claude hard gate when HTF=UNKNOWN OR low-confidence setup on any symbol
    htf_at_execution = get_htf_trend(symbol)
    if "htf" not in meta:
        meta["htf"] = htf_at_execution or _last_known_htf.get(symbol, {}).get("trend", "UNKNOWN")
    htf_in_meta = meta.get("htf")
    htf_unknown = (htf_at_execution is None or htf_in_meta in (None, "UNKNOWN", "?"))
    score_marginal = int(meta.get("score", 0)) <= int(SYMBOL_CONFIG.get(symbol, {}).get(
        "score_min_breakout" if entry_mode == "breakout" else "score_min_pullback", 3))

    if htf_unknown or score_marginal:
        approved = _claude_hard_gate_evaluate(
            symbol, direction, entry_mode, meta.get("score", 0), meta, utc_dt, atr, sl_dist
        )
        if not approved:
            _gate_hit(symbol, "claude_htf_unknown_block")
            _log_rejected_signal(symbol, direction, entry_mode, meta.get("score", 0), "claude_htf_unknown")
            cp("WARNING", f"🚫 {symbol} {direction} blocked — HTF={'UNKNOWN' if htf_unknown else 'marginal_score'}, Claude rejected")
            logger.warning("%s %s | HTF_UNKNOWN or marginal score hard gate blocked | htf_exec=%s htf_meta=%s score=%s",
                           symbol, direction, htf_at_execution, htf_in_meta, meta.get("score"))
            return

    # V5.3 — USDJPY scanner exception
    # When scanner has ≥0.80 BUY confidence + HTF=BULL + killzone + score≥3
    # allow BUY entries treating scanner as structural confirmation
    if symbol == "USDJPY" and direction == "BUY":
        htf_now = meta.get("htf") or get_htf_trend(symbol) or \
                  _last_known_htf.get(symbol, {}).get("trend", "UNKNOWN")
        scanner_alert = opportunity_alerts.get("USDJPY", {})
        scanner_conf = float(scanner_alert.get("confidence", 0))
        scanner_dir = scanner_alert.get("direction", "").upper()
        scanner_age = time.time() - float(scanner_alert.get("ts", 0))
        scanner_valid = (scanner_conf >= 0.80 and
                        scanner_dir == "BUY" and
                        scanner_age < 1800)
        in_kz = in_killzone(get_utc_time()) is not None
        score_ok = int(meta.get("score", 0)) >= 3

        if htf_now == "BULL" and scanner_valid and in_kz and score_ok:
            logger.info("[USDJPY_SCANNER] BUY allowed via scanner exception | "
                       "conf=%.2f | score=%s | kz=%s",
                       scanner_conf, meta.get("score"),
                       in_killzone(get_utc_time()))
            cp("SYSTEM", f"🎯 [USDJPY_SCANNER] BUY via scanner | conf={scanner_conf:.0%}")
            # Continue to execution — fall through

    req = {
        "action":       mt5.TRADE_ACTION_DEAL, "symbol": symbol,
        "volume":       lot,
        "type":         mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
        "price":        entry, "sl": sl, "tp": tp,
        "deviation":    DEVIATION, "magic": MAGIC,
        "comment":      f"SMC {BOT_VERSION} {entry_mode[:4]}",
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
        tg_send(
            f"🟢 <b>OPEN {direction}</b> {symbol} ({entry_mode.upper()})\n"
            f"Ticket: <code>{result.order}</code>\n"
            f"Entry: <code>{actual_entry:.5f}</code>\n"
            f"SL: <code>{sl:.5f}</code> | TP: <code>{tp:.5f}</code>\n"
            f"RR: {rr}R | Lot: {lot}"
        )
        play_sound("open")
        r1_level[result.order]            = (entry + sl_dist if direction == "BUY" else entry - sl_dist)
        be_locked[result.order]           = False
        partial_done[result.order]        = False
        peak_profit_tracker[result.order] = 0.0
        open_trades[result.order] = {
            "symbol":   symbol,       "type":       direction,
            "entry":    actual_entry, "sl":         sl,
            "tp":       tp,           "lot":        lot,
            "risk_1r":  sl_dist,      "risk_gbp":   risk_gbp,
            "entry_mode": entry_mode,
            "bos_idx":  meta.get("bos_idx"),
            "open_utc": utc_dt,
            "session":  _session_tag_for_time(symbol, utc_dt),
            "partial_r": (PARTIAL_TP_R_ASIA if in_asia_session(utc_dt) else PARTIAL_TP_R_PRIME),
            "trail_adj": (0.85 if in_killzone(utc_dt) else
                          1.15 if in_asia_session(utc_dt) else 1.0),
            # Zone tracking for early invalidation exit in manage_trades
            "zone_low":  meta.get("zone_low"),
            "zone_high": meta.get("zone_high"),
            "be_target_reached_ts": None,
            "atr_open":  meta.get("atr"),
            "shadow_verdict": "pending",
        }
        _phoenix_emit("TRADE_OPEN", symbol,
                      f"{direction} {symbol} | entry={actual_entry:.5f} | ticket={result.order}",
                      severity="INFO", department="TRADE",
                      metadata={"ticket": result.order, "direction": direction,
                                "entry": actual_entry, "sl": sl, "tp": tp,
                                "lot": lot, "rr": rr, "entry_mode": entry_mode,
                                "score": meta.get("score", 0),
                                "session": open_trades[result.order]["session"]})
        # Claude shadow evaluation — fire-and-forget, never blocks
        _claude_shadow_evaluate(symbol, direction, entry_mode, int(meta.get("score", 0)),
                                meta, utc_dt, atr, sl_dist, ticket=result.order)
        last_trade_time[symbol]    = now
        signal_cooldown[symbol]    = direction
        daily_trade_counts[symbol] = daily_trade_counts.get(symbol, 0) + 1
        # Record this BOS as traded — prevents re-entry on same structure after close
        bos_idx_meta = meta.get("bos_idx")
        if bos_idx_meta is not None:
            traded_bos_set.add((canonical_symbol(symbol), direction, bos_idx_meta, entry_mode))
        cp("DAILY", f"📊 {symbol} trades today: {daily_trade_counts[symbol]}/{MAX_TRADES_PER_DAY}")
    else:
        if result:
            logger.warning("[ORDER_FAIL] %s | retcode=%d", symbol, result.retcode)
        _gate_hit(symbol, "order_fail")
        log_order_result(result, "open_trade")
        _phoenix_emit("ORDER_REJECTED", symbol,
                      f"Order failed {symbol} | retcode={getattr(result, 'retcode', 'none')}",
                      severity="WARNING", department="TRADE",
                      metadata={"retcode": getattr(result, "retcode", None), "direction": direction})

# =========================
# TRADE MANAGEMENT
# =========================
def close_position(pos, reason):
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        cp("ERROR", f"❌ No tick to close {pos.symbol}"); return
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
        _pl_icon = "🟢" if pos.profit >= 0 else "🔴"
        tg_send(
            f"{_pl_icon} <b>CLOSE</b> {pos.symbol} | {reason}\n"
            f"Ticket: <code>{pos.ticket}</code>\n"
            f"P/L: <b>£{pos.profit:.2f}</b>"
        )
        # Loss-cluster tracking: record losing closes to detect repeated failures
        if pos.profit < 0:
            _dir = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
            record_loss(pos.symbol, _dir, pos.price_open, pos.profit)
        for d in (open_trades, be_locked, r1_level, peak_profit_tracker, partial_done):
            d.pop(pos.ticket, None)
    else:
        log_order_result(res, "close_position")

def try_partial_close(pos):
    if partial_done.get(pos.ticket, False):
        return
    pos_key = canonical_symbol(pos.symbol)
    if not SYMBOL_CONFIG.get(pos_key, {}).get("mgmt_partial_enabled", False):
        return
    trade_meta = open_trades.get(pos.ticket, {})
    risk_1r    = trade_meta.get("risk_1r", abs(pos.price_open - pos.sl))
    if risk_1r == 0:
        return
    partial_r      = float(trade_meta.get("partial_r", PARTIAL_TP_R))
    partial_target = (pos.price_open + risk_1r * partial_r if pos.type == mt5.ORDER_TYPE_BUY
                      else pos.price_open - risk_1r * partial_r)
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick: return
    current = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    if pos.type == mt5.ORDER_TYPE_BUY  and current < partial_target: return
    if pos.type == mt5.ORDER_TYPE_SELL and current > partial_target: return
    sym = mt5.symbol_info(pos.symbol)
    if sym is None: return
    close_vol = round(pos.volume * PARTIAL_CLOSE_PCT / sym.volume_step) * sym.volume_step
    close_vol = max(sym.volume_min, min(close_vol, pos.volume - sym.volume_min))
    close_vol = round(close_vol, 2)
    if close_vol <= 0: return
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
           f"[PARTIAL_TP] {pos.symbol} | Ticket:{pos.ticket} "
           f"{close_vol} lots @ {price:.5f} | Runner:{runner}")
        logger.info("[PARTIAL_TP] %s | Ticket:%s | closed=%.2f | runner=%.2f",
                    pos.symbol, pos.ticket, close_vol, runner)
        new_sl = normalize_price(pos.symbol, pos.price_open)
        # Use validate_position_modify for mid-trade SL adjustments
        if validate_position_modify(pos, new_sl):
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
        "magic": MAGIC, "comment": f"SMC {BOT_VERSION} trail",
    })

def _try_resolve_close(ticket, known_symbol, is_retry=False, meta=None, attempt_num=1):
    """
    Attempt to locate the closing deal for `ticket` in MT5 history using a
    multi-strategy resolver. The FTMO broker has been observed to return
    deals where position_id is 0/missing or differs from the opening order
    ticket, breaking naive matching. We try four strategies in order:

      1. Direct match: deal.position_id == ticket
      2. Indirect match: find opening deal via deal.order == ticket, then
         re-match using its (broker-assigned) position_id
      3. Meta-fallback: match by (symbol, volume, DEAL_ENTRY_OUT, recency)
         using the snapshot we recorded at open time
      4. None — log a diagnostic sample of the raw deals so we can see
         exactly which fields are populated

    Returns (matched_deal_or_None, reason_str).
    """
    global last_deal_check_utc
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    if last_deal_check_utc is None:
        last_deal_check_utc = now_utc - timedelta(minutes=30)

    if is_retry:
        # Widen retry window to bot start — covers long-running trades that
        # opened more than 4 hours ago (previous floor missed those).
        floor_dt = bot_start_time_utc if bot_start_time_utc is not None else (now_utc - timedelta(hours=4))
        search_from = min(last_deal_check_utc, floor_dt)
    else:
        # Narrow window for fresh closes — avoids the position-filter bug
        # returning random older deals from the day.
        search_from = now_utc - timedelta(seconds=120)
    search_to = now_utc + timedelta(seconds=5)

    # Convert to Unix timestamps. MT5 Python API is unreliable when passed
    # tz-aware datetimes (especially under Wine — silent empty returns).
    # int seconds since epoch is unambiguous and broker-portable.
    from_ts = int(search_from.timestamp())
    to_ts   = int(search_to.timestamp())

    # Diagnostic on attempt 1: prove what we're actually sending to MT5.
    if attempt_num == 1:
        logger.info(
            "[CLOSE_DIAG] Ticket:%s | attempt 1 | from_ts=%d to_ts=%d | "
            "from_dt=%s to_dt=%s | types=%s/%s",
            ticket, from_ts, to_ts, search_from, search_to,
            type(search_from).__name__, type(search_to).__name__,
        )

    target_ticket = int(ticket)

    # Primary lookup: position-direct query. Bypasses time-window slicing
    # entirely — broker indexes deals by position_id server-side. This is
    # the most reliable form when it works.
    all_deals = None
    try:
        pos_deals = mt5.history_deals_get(from_ts, to_ts, position=target_ticket)
    except Exception as e:
        logger.debug("history_deals_get(position=) error for %s: %s", ticket, e)
        pos_deals = None

    if pos_deals:
        logger.info("[CLOSE_DIAG] Ticket:%s | attempt %d | POSITION_DIRECT | deals=%d",
                    ticket, attempt_num, len(pos_deals))
        all_deals = list(pos_deals)
    else:
        # Fallback: broad time-window query. Used when broker returns empty
        # for the position filter (some brokers don't index immediately, or
        # the broker's position_id differs from our opening order ticket).
        try:
            win_deals = mt5.history_deals_get(from_ts, to_ts)
        except Exception as e:
            logger.debug("history_deals_get(window) error for %s: %s", ticket, e)
            win_deals = None
        if win_deals:
            logger.info("[CLOSE_DIAG] Ticket:%s | attempt %d | WINDOW_FALLBACK | deals=%d",
                        ticket, attempt_num, len(win_deals))
            all_deals = list(win_deals)

    if not all_deals:
        logger.warning("[CLOSE_DIAG] Ticket:%s | attempt %d | from_ts=%d to_ts=%d | EMPTY_HISTORY",
                      ticket, attempt_num, from_ts, to_ts)
        return None, "empty_history"

    # Strategy 1: direct position_id match
    matched = [d for d in all_deals
               if int(getattr(d, "position_id", 0) or 0) == target_ticket]
    if matched:
        closing = [d for d in matched if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT]
        if closing:
            logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY1_MATCH | pos_id_direct | closing_deal=%s",
                        ticket, getattr(closing[-1], "ticket", "?"))
        else:
            logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY1_MATCH | pos_id_direct | NO_OUT_DEAL | using_last_matched=%s",
                        ticket, getattr(matched[-1], "ticket", "?"))
        return (closing[-1] if closing else matched[-1]), "matched_pos_id"
    logger.debug("[CLOSE_DIAG] Ticket:%s | STRATEGY1_FAIL | no direct position_id match", ticket)

    # Strategy 2: find opening deal via deal.order == ticket → use its position_id
    opening = next(
        (d for d in all_deals
         if int(getattr(d, "order", 0) or 0) == target_ticket
         and getattr(d, "entry", None) == mt5.DEAL_ENTRY_IN),
        None,
    )
    if opening is not None:
        broker_pos_id = int(getattr(opening, "position_id", 0) or 0)
        logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY2_FOUND_OPENING | order_match | broker_pos_id=%s",
                    ticket, broker_pos_id)
        if broker_pos_id and broker_pos_id != target_ticket:
            re_matched = [d for d in all_deals
                          if int(getattr(d, "position_id", 0) or 0) == broker_pos_id]
            closing = [d for d in re_matched if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT]
            logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY2_REMATCH | broker_pos_id=%s | re_matched_count=%d | closing_count=%d",
                        ticket, broker_pos_id, len(re_matched), len(closing))
            if closing:
                logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY2_MATCH | order_chain | closing_deal=%s",
                            ticket, getattr(closing[-1], "ticket", "?"))
                return closing[-1], "matched_order_chain"
        else:
            logger.warning("[CLOSE_DIAG] Ticket:%s | STRATEGY2_FAIL | broker_pos_id=%s (invalid or equals ticket)",
                          ticket, broker_pos_id)
    else:
        logger.debug("[CLOSE_DIAG] Ticket:%s | STRATEGY2_FAIL | no opening deal found via order==ticket", ticket)

    # Strategy 3: meta-fallback by (symbol + volume + DEAL_ENTRY_OUT + recency)
    if meta is None:
        meta = (open_trades.get(ticket)
                or _pending_deal_lookup.get(ticket, {}).get("meta", {}))
    target_sym  = canonical_symbol(meta.get("symbol") or known_symbol or "")
    target_vol  = float(meta.get("lot", 0) or 0)
    logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY3_META | sym=%s vol=%.2f",
                ticket, target_sym, target_vol)
    if target_sym and target_vol > 0:
        candidates = [
            d for d in all_deals
            if canonical_symbol(getattr(d, "symbol", "")) == target_sym
            and getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT
            and abs(float(getattr(d, "volume", 0) or 0) - target_vol) < 0.001
        ]
        logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY3_CANDIDATES | raw_count=%d", ticket, len(candidates))
        # Filter out any we already know belong to OTHER live positions
        live_pos_ids = {int(getattr(p, "ticket", 0) or 0)
                        for p in (mt5.positions_get() or [])}
        logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY3_LIVE_POS | live_count=%d live_ids=%s",
                    ticket, len(live_pos_ids), sorted(live_pos_ids))
        candidates = [
            c for c in candidates
            if int(getattr(c, "position_id", 0) or 0) not in live_pos_ids
        ]
        logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY3_FILTERED | filtered_count=%d", ticket, len(candidates))
        if candidates:
            candidates.sort(key=lambda d: getattr(d, "time", 0), reverse=True)
            chosen = candidates[0]
            logger.info(
                "[CLOSE_DIAG] Ticket:%s | matched_via_meta_fallback | "
                "sym=%s vol=%.2f deal=%s pos_id=%s",
                ticket, target_sym, target_vol,
                getattr(chosen, "ticket", "?"),
                getattr(chosen, "position_id", "?"),
            )
            return chosen, "matched_meta_fallback"
    logger.info("[CLOSE_DIAG] Ticket:%s | STRATEGY3_FAIL | no candidates after filtering", ticket)

    # All strategies failed — log a diagnostic sample so we can see what
    # the broker is actually returning.
    sample = []
    for d in all_deals[:6]:
        sample.append(
            f"deal={getattr(d, 'ticket', '?')} "
            f"order={getattr(d, 'order', '?')} "
            f"pos_id={getattr(d, 'position_id', '?')} "
            f"sym={getattr(d, 'symbol', '?')} "
            f"entry={getattr(d, 'entry', '?')} "
            f"vol={getattr(d, 'volume', '?')} "
            f"profit={getattr(d, 'profit', '?')}"
        )
    # Rate-limit full diagnostic: only dump on 1st, every-10th, and final attempts.
    _is_final = attempt_num >= PENDING_CLOSE_MAX_ATTEMPTS
    if attempt_num == 1 or attempt_num % 10 == 0 or _is_final:
        logger.warning(
            "[CLOSE_DIAG] Ticket:%s | ALL_STRATEGIES_FAILED | attempt %d/%d | deals=%d | meta_sym=%s meta_vol=%s | sample: %s",
            ticket, attempt_num, PENDING_CLOSE_MAX_ATTEMPTS, len(all_deals),
            meta.get("symbol") if meta else "?",
            meta.get("lot") if meta else "?",
            " || ".join(sample),
        )
    return None, f"no_match_in_{len(all_deals)}_deals"


def _finalise_close(ticket, known_symbol, deal, meta=None):
    """Emit the CLOSE log lines and play sound alert."""
    if meta is None:
        meta = open_trades.get(ticket) or {}
    if deal is not None:
        symbol   = known_symbol if known_symbol != "?" else getattr(deal, "symbol", "?")
        profit   = float(getattr(deal, "profit", 0.0) or 0.0)
        comment  = getattr(deal, "comment", "") or ""
        reason_s = str(getattr(deal, "reason", "?"))
        cp("CLOSE", f"[CLOSE] {symbol} | Ticket:{ticket} | reason={reason_s} | P/L:{profit:.2f} | {comment}")
        logger.info("[CLOSE] %s | Ticket:%s | reason=%s | P/L:%.2f | %s",
                    symbol, ticket, reason_s, profit, comment)
        # Claude shadow outcome — link original verdict to actual P/L
        shadow_v = meta.get("shadow_verdict", "unknown")
        logger.info("[CLAUDE_SHADOW_OUTCOME] %s | ticket=%s | shadow_verdict=%s | actual_pl=%.2f",
                    symbol, ticket, shadow_v, profit)
        # V5.1 — Structured trade result for statistical analysis
        r_multiple = 0  # declared outside try so _shadow_csv_fill_outcome can always read it
        try:
            duration_mins = 0
            open_utc = meta.get("open_utc")
            if isinstance(open_utc, datetime):
                duration_mins = round((datetime.utcnow().replace(tzinfo=pytz.UTC) - open_utc).total_seconds() / 60)
            risk_1r  = float(meta.get("risk_1r",  0) or 0)
            risk_gbp = float(meta.get("risk_gbp", 0) or 0)
            # V5.3 — use £-denominated risk captured at open (risk_1r was price-units, gave ~950R)
            r_multiple = round(profit / risk_gbp, 2) if risk_gbp > 0 else 0
            logger.info(
                "[TRADE_RESULT] symbol=%s | direction=%s | mode=%s | score=%s | "
                "session=%s | duration_mins=%d | r_multiple=%.2f | "
                "claude_verdict=%s | opportunity_alert=%s | pnl=%.2f",
                symbol,
                meta.get("type", "?"),
                meta.get("entry_mode", "?"),
                meta.get("score", "?"),
                meta.get("session", "?"),
                duration_mins,
                r_multiple,
                shadow_v,
                "yes" if symbol in opportunity_alerts else "no",
                profit,
            )
        except Exception as _e:
            logger.debug("[TRADE_RESULT] log failed: %s", _e)
        _shadow_csv_fill_outcome(ticket, float(getattr(deal, "price", 0) or 0), profit, r_multiple)
        _phoenix_emit("TRADE_CLOSE", symbol,
                      f"CLOSE {symbol} | ticket={ticket} | P/L={profit:.2f} | {r_multiple}R",
                      severity="INFO", department="TRADE",
                      metadata={"ticket": ticket, "profit": profit, "r_multiple": r_multiple,
                                "close_reason": reason_s, "shadow_verdict": shadow_v,
                                "entry_mode": meta.get("entry_mode", "?"),
                                "direction": meta.get("type", "?")})
        _pl_icon = "🟢" if profit >= 0 else "🔴"
        tg_send(
            f"{_pl_icon} <b>CLOSE</b> {symbol} | {comment or reason_s}\n"
            f"Ticket: <code>{ticket}</code>\n"
            f"P/L: <b>£{profit:.2f}</b>"
        )
        # Loss-cluster tracking: record losing closes to detect repeated failures
        if profit < 0:
            record_loss(symbol, meta.get("type"), meta.get("entry"), profit)
    else:
        # All deal-lookup strategies exhausted. Try history_orders_get as last resort.
        _dir     = meta.get("type",    "?") if meta else "?"
        _entry   = float(meta.get("entry", 0) or 0) if meta else 0.0
        _orig_sl = float(meta.get("sl",    0) or 0) if meta else 0.0
        _tp      = float(meta.get("tp",    0) or 0) if meta else 0.0
        _lot     = float(meta.get("lot",   0) or 0) if meta else 0.0
        _risk_gbp = float(meta.get("risk_gbp", 0) or 0) if meta else 0.0
        _shadow_v = meta.get("shadow_verdict", "unknown") if meta else "unknown"
        est_close = None
        est_pl    = None
        recon_src = None
        try:
            _now_ts  = int(time.time())
            _from_ts = _now_ts - 86400
            _ho = mt5.history_orders_get(_from_ts, _now_ts, position=int(ticket)) \
                  if hasattr(mt5, "history_orders_get") else None
            _filled = [o for o in (_ho or [])
                       if int(getattr(o, "ticket", 0)) != int(ticket)]
            if _filled:
                _co = sorted(_filled, key=lambda o: getattr(o, "time_done", 0))[-1]
                _px = float(getattr(_co, "price_current", 0) or getattr(_co, "price_open", 0))
                if _px:
                    est_close = _px
                    recon_src = "history_orders"
        except Exception as _re:
            logger.debug("[CLOSE_RECONSTRUCT] history_orders_get failed: %s", _re)

        if est_close and _entry and _risk_gbp > 0:
            _sign = 1 if _dir == "BUY" else -1
            _move = _sign * (est_close - _entry)
            _sl_dist = abs(_entry - _orig_sl) if _orig_sl else 0
            est_pl = round(_move / _sl_dist * _risk_gbp, 2) if _sl_dist > 0 else None

        if est_close is not None:
            cp("CLOSE", f"[CLOSE_RECONSTRUCTED] {known_symbol} | Ticket:{ticket}"
                        f" | est_close={est_close:.5f} | est_pl={est_pl} | src={recon_src} | VERIFY_IN_MT5")
            logger.warning("[CLOSE_RECONSTRUCTED] %s | Ticket:%s | est_close=%.5f"
                           " | est_pl=%s | src=%s | VERIFY_IN_MT5",
                           known_symbol, ticket, est_close, est_pl, recon_src)
        else:
            cp("CLOSE", f"[CLOSE_UNKNOWN] {known_symbol} | Ticket:{ticket}"
                        f" | dir={_dir} | entry={_entry:.5f} | orig_sl={_orig_sl:.5f}"
                        f" | tp={_tp:.5f} | lot={_lot} | VERIFY_IN_MT5")
            logger.warning("[CLOSE_UNKNOWN] %s | Ticket:%s | dir=%s | entry=%.5f"
                           " | orig_sl=%.5f | tp=%.5f | lot=%s | VERIFY_IN_MT5",
                           known_symbol, ticket, _dir, _entry, _orig_sl, _tp, _lot)

        # Sanity check — reconstructed PnL should be within reasonable bounds
        # Max possible loss at max risk = 0.8% of 35000 = £280
        MAX_REASONABLE_LOSS = -500
        MAX_REASONABLE_WIN = 1000
        if est_pl is not None and (est_pl < MAX_REASONABLE_LOSS or est_pl > MAX_REASONABLE_WIN):
            # Try to get actual profit from MT5 deal history
            deals = mt5.history_deals_get(ticket=ticket)
            if deals:
                actual_pl = sum(d.profit for d in deals)
                logger.warning("[TRADE_RESULT] Reconstructed PnL %.2f clamped — using MT5 actual %.2f",
                               est_pl, actual_pl)
                est_pl = actual_pl
            else:
                logger.warning("[TRADE_RESULT] Reconstructed PnL %.2f outside bounds, no deals found — using 0", est_pl)
                est_pl = 0.0

        # Always emit TRADE_RESULT so dashboard/analytics have a row regardless
        logger.info(
            "[TRADE_RESULT] symbol=%s | direction=%s | mode=%s | score=%s | session=%s"
            " | duration_mins=%d | r_multiple=%.2f | claude_verdict=%s"
            " | opportunity_alert=%s | pnl=%.2f | reconstructed=True",
            known_symbol, _dir,
            meta.get("entry_mode", "?") if meta else "?",
            meta.get("score",      "?") if meta else "?",
            meta.get("session",    "?") if meta else "?",
            0,
            round((est_pl or 0) / _risk_gbp, 2) if _risk_gbp > 0 else 0,
            _shadow_v,
            "yes" if known_symbol in opportunity_alerts else "no",
            est_pl or 0.0,
        )
        tg_send(
            f"⚪ <b>CLOSE</b> {known_symbol} | Ticket: <code>{ticket}</code>\n"
            + (f"P/L: <i>est £{est_pl:.2f} — VERIFY IN MT5</i>"
               if est_pl is not None else "P/L: <i>unknown — check MT5 manually</i>")
        )
        _shadow_csv_fill_outcome(ticket, est_close or 0.0, est_pl, 0.0)
    play_sound("close")


def _cleanup_closed_trade_state(ticket):
    """Pop ticket from all per-trade state dicts. Does NOT touch traded_bos_set
    (intentional — that persists past close to prevent same-BOS re-entry)."""
    for d in (open_trades, be_locked, r1_level, peak_profit_tracker, partial_done):
        d.pop(ticket, None)


# =============================================================================
# SHADOW ACCURACY CSV  (Task 3 — V5.3)
# Records every shadow vote at open, back-fills outcome on close.
# Minimum 30-50 trades before shadow gate should be trusted as a hard veto.
# =============================================================================

_SHADOW_CSV_COLS = [
    "timestamp", "symbol", "direction", "shadow_confidence", "shadow_decision",
    "ticket", "entry", "sl", "tp",
    "close_price", "outcome_R", "outcome_gbp", "shadow_was_right",
]


def _shadow_csv_log_vote(symbol, direction, confidence, verdict, ticket, entry, sl, tp):
    """Append a shadow-vote row at trade open. Outcome columns left blank."""
    import csv as _csv
    write_header = (not SHADOW_ACCURACY_CSV.exists() or SHADOW_ACCURACY_CSV.stat().st_size == 0)
    try:
        with open(SHADOW_ACCURACY_CSV, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=_SHADOW_CSV_COLS)
            if write_header:
                w.writeheader()
            w.writerow({
                "timestamp":         datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol":            symbol,
                "direction":         direction,
                "shadow_confidence": round(float(confidence), 3),
                "shadow_decision":   verdict,
                "ticket":            ticket,
                "entry":             entry,
                "sl":                sl,
                "tp":                tp,
                "close_price":       "",
                "outcome_R":         "",
                "outcome_gbp":       "",
                "shadow_was_right":  "",
            })
    except Exception as _e:
        logger.debug("[SHADOW_CSV] vote write failed: %s", _e)


def _shadow_csv_fill_outcome(ticket, close_price, outcome_gbp, r_multiple):
    """Back-fill outcome columns for a ticket row after trade closes."""
    import csv as _csv
    if not SHADOW_ACCURACY_CSV.exists():
        return
    try:
        with open(SHADOW_ACCURACY_CSV, "r", newline="") as f:
            rows = list(_csv.DictReader(f))
        updated = False
        for row in rows:
            if str(row.get("ticket")) == str(ticket) and row.get("outcome_gbp") == "":
                verdict = row.get("shadow_decision", "")
                won = outcome_gbp is not None and float(outcome_gbp) > 0
                row["close_price"]    = round(float(close_price), 5) if close_price else ""
                row["outcome_R"]      = r_multiple
                row["outcome_gbp"]    = round(float(outcome_gbp), 2) if outcome_gbp is not None else ""
                if verdict == "block" and not won:
                    row["shadow_was_right"] = True
                elif verdict in ("approve", "approve_reduced") and won:
                    row["shadow_was_right"] = True
                else:
                    row["shadow_was_right"] = False
                updated = True
                break
        if updated:
            with open(SHADOW_ACCURACY_CSV, "w", newline="") as f:
                w = _csv.DictWriter(f, fieldnames=_SHADOW_CSV_COLS)
                w.writeheader()
                w.writerows(rows)
    except Exception as _e:
        logger.debug("[SHADOW_CSV] outcome update failed: %s", _e)


def _shadow_accuracy_report():
    """Log weekly shadow gate calibration stats. Fired Sunday 18:00-19:00 UTC."""
    import csv as _csv
    if not SHADOW_ACCURACY_CSV.exists():
        logger.info("[SHADOW_ACCURACY] No data yet — CSV not found")
        return
    try:
        with open(SHADOW_ACCURACY_CSV, "r", newline="") as f:
            rows = [r for r in _csv.DictReader(f) if r.get("outcome_gbp") not in ("", None)]
        if len(rows) < 5:
            logger.info("[SHADOW_ACCURACY] Only %d completed trades — need 30+ for calibration",
                        len(rows))
            return
        total    = len(rows)
        blocks   = [r for r in rows if r["shadow_decision"] == "block"]
        approves = [r for r in rows if r["shadow_decision"] in ("approve", "approve_reduced")]
        hi_conf  = [r for r in rows
                    if float(r.get("shadow_confidence", 0)) >= 0.80]
        b_right  = sum(1 for r in blocks   if str(r.get("shadow_was_right")) == "True")
        a_right  = sum(1 for r in approves if str(r.get("shadow_was_right")) == "True")
        h_right  = sum(1 for r in hi_conf  if str(r.get("shadow_was_right")) == "True")
        logger.info(
            "[SHADOW_ACCURACY] Weekly report | trades=%d"
            " | blocks=%d (%.0f%% correct) | approves=%d (%.0f%% correct)"
            " | hi_conf≥0.80: %d trades (%.0f%% correct)",
            total,
            len(blocks),   (b_right / len(blocks)   * 100) if blocks   else 0,
            len(approves), (a_right / len(approves) * 100) if approves else 0,
            len(hi_conf),  (h_right / len(hi_conf)  * 100) if hi_conf  else 0,
        )
    except Exception as _e:
        logger.debug("[SHADOW_ACCURACY] report failed: %s", _e)


def detect_closed_trades():
    global last_deal_check_utc
    now_utc  = datetime.utcnow().replace(tzinfo=pytz.UTC)
    existing = {p.ticket for p in (mt5.positions_get() or [])}

    # (1) Detect freshly-closed positions. Queue for deal-history lookup rather
    #     than resolving inline — MT5 deal history has observable lag on close.
    for ticket in list(open_trades.keys()):
        if ticket not in existing and ticket not in _pending_deal_lookup:
            meta_snapshot = dict(open_trades.get(ticket, {}))
            _pending_deal_lookup[ticket] = {
                "first_try": now_utc,
                "attempts":  0,
                "meta":      meta_snapshot,
            }
            # Do NOT cleanup state yet — we still need meta for the lookup.

    # (2) Drain the pending queue. Each ticket gets up to PENDING_CLOSE_MAX_ATTEMPTS
    #     tries before we give up and finalise with a warning.
    for ticket in list(_pending_deal_lookup.keys()):
        entry        = _pending_deal_lookup[ticket]
        entry["attempts"] += 1
        known_symbol = entry["meta"].get("symbol", "?")
        deal, reason = _try_resolve_close(
            ticket, known_symbol,
            is_retry=entry["attempts"] > 1,
            meta=entry["meta"],
            attempt_num=entry["attempts"],
        )

        if deal is not None:
            _finalise_close(ticket, known_symbol, deal, meta=entry["meta"])
            _cleanup_closed_trade_state(ticket)
            _pending_deal_lookup.pop(ticket, None)
        elif entry["attempts"] >= PENDING_CLOSE_MAX_ATTEMPTS:
            logger.warning("[CLOSE_DIAG] Ticket:%s | gave up after %d attempts | reason=%s",
                           ticket, entry["attempts"], reason)
            _finalise_close(ticket, known_symbol, None, meta=entry["meta"])
            _cleanup_closed_trade_state(ticket)
            _pending_deal_lookup.pop(ticket, None)
        else:
            logger.debug("[CLOSE_DIAG] Ticket:%s | attempt %d/%d pending | reason=%s",
                         ticket, entry["attempts"], PENDING_CLOSE_MAX_ATTEMPTS, reason)

    last_deal_check_utc = now_utc

def log_heartbeat():
    """Log heartbeat to detect unexpected stops. Sends hourly Telegram status."""
    global last_heartbeat_utc, last_heartbeat_log_utc, last_telegram_heartbeat_utc
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    last_heartbeat_utc = now_utc
    
    uptime_sec = (now_utc - bot_start_time_utc).total_seconds() if bot_start_time_utc else 0
    uptime_str = f"{uptime_sec/3600:.1f}h" if uptime_sec >= 3600 else f"{uptime_sec/60:.0f}m"
    
    # Only log to file every HEARTBEAT_INTERVAL_SEC to avoid spam
    if last_heartbeat_log_utc is None or (now_utc - last_heartbeat_log_utc).total_seconds() >= HEARTBEAT_INTERVAL_SEC:
        heartbeat_msg = f"[HEARTBEAT] Uptime:{uptime_str} | Active trades:{len(open_trades)} | {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        logger.info(heartbeat_msg)
        cp("SYSTEM", f"💓 {heartbeat_msg}")
        
        # Also write to separate heartbeat file for easy monitoring
        try:
            with open(HEARTBEAT_FILE, "a") as f:
                f.write(f"{now_utc.isoformat()} | {heartbeat_msg}\n")
        except Exception as e:
            logger.error("Failed to write heartbeat file: %s", e)
        
        _phoenix_emit("HEARTBEAT", "ALL", heartbeat_msg,
                      severity="INFO", department="INFRA",
                      metadata={"uptime": uptime_str, "open_trades": len(open_trades)})
        last_heartbeat_log_utc = now_utc

    # Telegram status every hour
    if last_telegram_heartbeat_utc is None or (now_utc - last_telegram_heartbeat_utc).total_seconds() >= TELEGRAM_HEARTBEAT_INTERVAL_SEC:
        try:
            acc = mt5.account_info()
            if acc:
                eq = acc.equity
                # Daily metrics
                daily_pnl = eq - daily_open_equity if daily_open_equity else 0.0
                daily_dd = (daily_open_equity - eq) / daily_open_equity * 100 if daily_open_equity else 0.0
                # FTMO progress
                ftmo_pnl = eq - challenge_start_equity if challenge_start_equity else 0.0
                ftmo_pct = ftmo_pnl / challenge_start_equity * 100 if challenge_start_equity else 0.0
                # Open trade summary
                open_lines = []
                total_open_pnl = 0.0
                for ticket, t in open_trades.items():
                    pos = mt5.positions_get(ticket=ticket)
                    if pos and len(pos) > 0:
                        pnl = pos[0].profit
                        total_open_pnl += pnl
                        sym = t.get("symbol", "?")
                        mode = t.get("entry_mode", "?")
                        open_lines.append(f"  {sym} {mode}: £{pnl:.2f}")
                open_str = "\n".join(open_lines) if open_lines else "  None"
                
                tg_msg = (
                    f"💓 <b>Hourly Status</b>\n"
                    f"Uptime: <b>{uptime_str}</b> | {now_utc.strftime('%H:%M UTC')}\n"
                    f"Equity: <b>£{eq:.2f}</b>\n"
                    f"Daily P/L: <b>£{daily_pnl:+.2f}</b> | DD: <b>{daily_dd:.2f}%</b>\n"
                    f"FTMO P/L: <b>£{ftmo_pnl:+.2f}</b> ({ftmo_pct:+.2f}%)\n"
                    f"Open trades ({len(open_trades)}):\n{open_str}"
                )
                tg_send(tg_msg)
        except Exception as e:
            logger.debug("[TELEGRAM] heartbeat send failed: %s", e)
        # Sunday 18:00-19:00 UTC — weekly shadow gate accuracy report
        if now_utc.weekday() == 6 and 18 <= now_utc.hour < 19:
            _shadow_accuracy_report()
        last_telegram_heartbeat_utc = now_utc

def check_offline_closures():
    """Check for trades that closed while bot was offline (startup check)."""
    global last_deal_check_utc
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    
    if last_deal_check_utc is None:
        # First startup - check back STARTUP_CHECK_HOURS
        search_from = now_utc - timedelta(hours=STARTUP_CHECK_HOURS)
    else:
        # Bot restart - check from last known deal check
        search_from = last_deal_check_utc
    
    logger.info("[STARTUP_CHECK] Scanning for trades closed between %s and %s", 
                search_from.strftime('%Y-%m-%d %H:%M:%S UTC'), 
                now_utc.strftime('%Y-%m-%d %H:%M:%S UTC'))
    
    # Get all deals in the time window
    try:
        deals = mt5.history_deals_get(search_from, now_utc)
        if not deals:
            logger.info("[STARTUP_CHECK] No deals found in time window")
            last_deal_check_utc = now_utc
            return
        
        # Find closed positions (deals with entry type OUT)
        closed_tickets = set()
        for d in deals:
            if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT:
                ticket = getattr(d, "position_id", None)
                if ticket:
                    closed_tickets.add(ticket)
        
        if not closed_tickets:
            logger.info("[STARTUP_CHECK] No closed positions found in time window")
            last_deal_check_utc = now_utc
            return
        
        # Log each closure
        for ticket in closed_tickets:
            # Get deal details for this ticket
            ticket_deals = [d for d in deals if getattr(d, "position_id", None) == ticket]
            closing_deals = [d for d in ticket_deals if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT]
            
            if closing_deals:
                d = closing_deals[-1]
                symbol = getattr(d, "symbol", "?")
                profit = float(getattr(d, "profit", 0.0) or 0.0)
                comment = getattr(d, "comment", "") or ""
                reason_s = str(getattr(d, "reason", "?"))
                close_time = datetime.fromtimestamp(getattr(d, "time", 0), tz=pytz.UTC)
                
                cp("CLOSE", f"[OFFLINE_CLOSE] {symbol} | Ticket:{ticket} | reason={reason_s} | P/L:{profit:.2f} | closed_at={close_time.strftime('%H:%M:%S UTC')} | {comment}")
                logger.warning("[OFFLINE_CLOSE] %s | Ticket:%s | reason=%s | P/L:%.2f | closed_at=%s | %s",
                              symbol, ticket, reason_s, profit, close_time.strftime('%Y-%m-%d %H:%M:%S UTC'), comment)
        
        last_deal_check_utc = now_utc
        
    except Exception as e:
        logger.error("[STARTUP_CHECK] Failed to scan for offline closures: %s", e)
        last_deal_check_utc = now_utc

def check_uptime_warning():
    """Check if bot was offline for an extended period."""
    global last_heartbeat_utc
    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    
    if last_heartbeat_utc is not None:
        offline_duration = (now_utc - last_heartbeat_utc).total_seconds()
        # Warn if offline for more than 10 minutes during active trading hours
        if offline_duration > 600:
            logger.warning("[UPTIME_WARNING] Bot was offline for %.1f minutes - possible unexpected stop", offline_duration / 60)
            cp("WARNING", f"⚠️  Bot was offline for {offline_duration/60:.1f} minutes - possible unexpected stop")

def check_total_loss():
    """Check total loss from challenge start for FTMO compliance."""
    global challenge_start_equity
    if challenge_start_equity is None:
        acc = mt5.account_info()
        if acc:
            challenge_start_equity = acc.equity
            logger.info("[FTMO] Challenge start equity: %.2f", challenge_start_equity)
        return False
    
    acc = mt5.account_info()
    if acc is None:
        return False
    
    total_loss = (challenge_start_equity - acc.equity) / challenge_start_equity
    if total_loss >= MAX_TOTAL_LOSS_PERCENT:
        cp("HALT", f"🛑 FTMO TOTAL LOSS STOP | {total_loss*100:.2f}% | LIMIT REACHED")
        logger.critical("[FTMO] TOTAL LOSS STOP | %.2f%% | start=%.2f | current=%.2f | LIMIT REACHED",
                       total_loss * 100, challenge_start_equity, acc.equity)
        for pos in (mt5.positions_get() or []):
            if pos.magic == MAGIC:
                close_position(pos, "FTMO_TOTAL_LOSS_STOP")
        return True
    
    # Check if profit target reached
    global profit_target_reached
    profit_pct = (acc.equity - challenge_start_equity) / challenge_start_equity
    if profit_pct >= FTMO_PROFIT_TARGET_PCT and not profit_target_reached:
        profit_target_reached = True
        cp("SYSTEM", f"🎯 FTMO PROFIT TARGET REACHED | {profit_pct*100:.2f}% | Target: £{acc.equity - challenge_start_equity:.2f}")
        logger.info("[FTMO] PROFIT TARGET REACHED | %.2f%% | start=%.2f | current=%.2f",
                   profit_pct * 100, challenge_start_equity, acc.equity)
    
    return False

def check_tick_freshness():
    """Check for data stalls by detecting stale tick prices."""
    global last_tick_prices, stale_tick_count
    
    for symbol in SYMBOLS:
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            continue
        
        current_price = tick.bid  # Use bid as reference price
        
        # Initialize first price
        if symbol not in last_tick_prices:
            last_tick_prices[symbol] = current_price
            stale_tick_count[symbol] = 0
            continue
        
        # Check if price is unchanged
        if current_price == last_tick_prices[symbol]:
            stale_tick_count[symbol] += 1
        else:
            stale_tick_count[symbol] = 0
            last_tick_prices[symbol] = current_price
        
        # Force reconnect if stale for too long
        if stale_tick_count[symbol] >= STALE_TICK_THRESHOLD:
            logger.warning("[TICK_STALE] %s | price unchanged for %d cycles | forcing reconnect", 
                          symbol, stale_tick_count[symbol])
            cp("WARNING", f"⚠️  {symbol} data stall detected - forcing reconnect")
            # Reset counter after warning
            stale_tick_count[symbol] = 0
            return True  # Signal that reconnect is needed
    
    return False

def print_positions():
    positions = mt5.positions_get() or []
    if not positions: return
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
    if not positions: return
    print_positions()

    acc          = mt5.account_info()
    basket_tp    =  (acc.equity * BASKET_TP_PCT) if acc else  800
    basket_sl    = -(acc.equity * BASKET_SL_PCT) if acc else -200
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
        pos_key = canonical_symbol(pos.symbol)
        if pos_key not in SYMBOL_CONFIG:
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

        sym_cfg = SYMBOL_CONFIG.get(pos_key, {})

        # FIX 3 (V13.1) — mgmt defaults changed to False.
        # Previously sym_cfg.get("mgmt_lockin_enabled", True) meant any symbol
        # without an explicit key silently had lock-in enabled, contradicting
        # the backtest finding that lock-in hurts most symbols.
        # Default is now False — only symbols with explicit True get the feature.
        if sym_cfg.get("mgmt_lockin_enabled", False):
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

        # Zone invalidation exit — close early if price has breached the original
        # pullback zone by zone_invalidate_mult × ATR. Rationale: once price breaks
        # through the zone in the wrong direction by this margin, the structural
        # premise is broken; waiting for the hard SL just enlarges the loss.
        zone_low_open  = trade_meta.get("zone_low")
        zone_high_open = trade_meta.get("zone_high")
        atr_open       = trade_meta.get("atr_open")
        zone_inv_mult  = sym_cfg.get("zone_invalidate_mult", 2.0)
        if (zone_low_open is not None and zone_high_open is not None
                and atr_open and atr_open > 0 and zone_inv_mult):
            inv_threshold = atr_open * zone_inv_mult
            current_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
            # SELL: entered near zone_high; if price rallies above zone_high + threshold, invalidated
            # BUY:  entered near zone_low;  if price falls below zone_low  - threshold, invalidated
            zone_breached = False
            if pos.type == mt5.ORDER_TYPE_SELL and current_price > zone_high_open + inv_threshold:
                zone_breached = True
            elif pos.type == mt5.ORDER_TYPE_BUY and current_price < zone_low_open - inv_threshold:
                zone_breached = True
            if zone_breached and not be_locked.get(pos.ticket, False):
                # Only fire before BE lock — after BE, the trade is already protected
                cp("CLOSE",
                   f"[ZONE_INV] {pos.symbol} | Ticket:{pos.ticket} "
                   f"| price={current_price:.5f} breached zone by {zone_inv_mult}×ATR")
                logger.info("[ZONE_INV] %s | Ticket:%s | price=%.5f | zone=[%.5f,%.5f] | thresh=%.5f",
                            pos.symbol, pos.ticket, current_price,
                            zone_low_open, zone_high_open, inv_threshold)
                close_position(pos, "ZONE_INVALIDATED")
                continue

        # FIX 3 (V13.1) — time stop default also False
        if sym_cfg.get("mgmt_time_stop_enabled", False):
            open_utc = trade_meta.get("open_utc")
            if isinstance(open_utc, datetime):
                age_s = (get_utc_time() - open_utc).total_seconds()
                if risk_1r and risk_1r > 0:
                    current_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                    progress_r    = ((current_price - pos.price_open) / risk_1r
                                     if pos.type == mt5.ORDER_TYPE_BUY
                                     else (pos.price_open - current_price) / risk_1r)
                    utc_now     = get_utc_time()
                    timeout_min = TIME_STOP_DEFAULT_MINUTES
                    if in_killzone(utc_now):     timeout_min = TIME_STOP_KILLZONE_MINUTES
                    elif in_asia_session(utc_now): timeout_min = TIME_STOP_ASIA_MINUTES
                    ts_min_r = sym_cfg.get("mgmt_time_stop_min_r", TIME_STOP_MIN_PROGRESS_R)
                    if age_s >= timeout_min * 60 and progress_r < ts_min_r:
                        cp("CLOSE",
                           f"[TIME_STOP] {pos.symbol} | Ticket:{pos.ticket} "
                           f"| age={int(age_s/60)}m | R={progress_r:.2f}")
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

        new_sl   = pos.sl
        new_tp   = pos.tp
        want_be  = False

        if not be_locked.get(pos.ticket, False):
            info    = mt5.symbol_info(pos.symbol)
            one_pip = info.point if info else 0.0
            spread_cost = tick.ask - tick.bid if tick else 0.0
            be_ts_key = "be_target_reached_ts"
            trade_data = open_trades.get(pos.ticket, {})

            if pos.type == mt5.ORDER_TYPE_BUY and tick.bid >= be_target:
                # SL at entry + spread cost to account for true break-even
                be_sl = pos.price_open + spread_cost + one_pip
                new_sl = normalize_price(pos.symbol, be_sl)
                want_be = True
                open_trades[pos.ticket][be_ts_key] = time.time()
            elif pos.type == mt5.ORDER_TYPE_SELL and tick.ask <= be_target:
                # SL at entry - spread cost to account for true break-even
                be_sl = pos.price_open - spread_cost - one_pip
                new_sl = normalize_price(pos.symbol, be_sl)
                want_be = True
                open_trades[pos.ticket][be_ts_key] = time.time()
            elif trade_data.get(be_ts_key):
                # Fix 2: BE lock retry buffer — price hit target recently, lock BE even if pulled back
                elapsed = time.time() - trade_data[be_ts_key]
                if elapsed <= 30:
                    be_sl = (pos.price_open + spread_cost + one_pip if pos.type == mt5.ORDER_TYPE_BUY
                             else pos.price_open - spread_cost - one_pip)
                    new_sl = normalize_price(pos.symbol, be_sl)
                    want_be = True
                    logger.info("[BE_RETRY_BUFFER] %s | Ticket:%s | elapsed=%.1fs | locking BE on pullback",
                                pos.symbol, pos.ticket, elapsed)

        if be_locked.get(pos.ticket, False):
            trail_adj = float(open_trades.get(pos.ticket, {}).get("trail_adj", 1.0))
            trail     = atr * sym_cfg.get("trail_mult", 0.3) * trail_adj
            if pos.type == mt5.ORDER_TYPE_BUY and tick.bid >= r2_target:
                potential = tick.bid - trail
                if potential > new_sl:
                    new_sl = potential
            elif pos.type == mt5.ORDER_TYPE_SELL and tick.ask <= r2_target:
                potential = tick.ask + trail
                if potential < new_sl:
                    new_sl = potential

        new_sl     = normalize_price(pos.symbol, new_sl)
        new_tp     = normalize_price(pos.symbol, new_tp)
        # Use validate_position_modify for mid-trade SL adjustments (checks against current price)
        if not validate_position_modify(pos, new_sl):
            if want_be:
                tick = mt5.symbol_info_tick(pos.symbol)
                curr = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask if tick else 0
                logger.warning("[BE_REJECT] %s | Ticket:%s | sl=%.5f vs curr=%.5f",
                               pos.symbol, pos.ticket, new_sl, curr)
            continue

        if abs(new_sl - pos.sl) > 1e-9:
            res = modify_position_sl_tp(pos, new_sl, new_tp)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                if want_be:
                    be_locked[pos.ticket] = True
                    cp("BE_LOCK",
                       f"[BE_LOCK] {pos.symbol} | Ticket:{pos.ticket} | SL→{new_sl:.5f} ({be_lock_r}R)")
                    logger.info("[BE_LOCK] %s | Ticket:%s | SL→%.5f | trigger=%.1fR",
                                pos.symbol, pos.ticket, new_sl, be_lock_r)
                else:
                    cp("TRAIL",
                       f"[TRAIL] {pos.symbol} | Ticket:{pos.ticket} | SL:{pos.sl:.5f}→{new_sl:.5f}")
                    logger.info("[TRAIL] %s | Ticket:%s | SL:%.5f→%.5f",
                                pos.symbol, pos.ticket, pos.sl, new_sl)
            else:
                if want_be:
                    logger.warning("[BE_LOCK_FAIL] %s | Ticket:%s", pos.symbol, pos.ticket)
                log_order_result(res, "modify_sl_tp")
        elif want_be:
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
    sg_str  = "ON" if SYMBOL_CONFIG[symbol].get("session_gate", False) else "OFF"
    parts.append(f"KZ={kz}")
    parts.append(f"SL×{sl_mult:.1f}")
    parts.append(f"SG={sg_str}")
    try:
        if daily_open_equity:
            acc = mt5.account_info()
            dd  = (daily_open_equity - acc.equity) / daily_open_equity * 100 if acc else 0
            parts.append(f"DD={dd:.2f}%")
    except Exception:
        parts.append("DD=?")
    try:
        if challenge_start_equity:
            acc = mt5.account_info()
            ftmo_pct = (challenge_start_equity - acc.equity) / challenge_start_equity * 100 if acc else 0
            ftmo_pct = max(0, ftmo_pct)
            parts.append(f"FTMO={ftmo_pct:.2f}%")
    except Exception:
        parts.append("FTMO=?")
    sym_daily_cap = int(SYMBOL_CONFIG.get(symbol, {}).get("max_trades_per_day", MAX_TRADES_PER_DAY))
    parts.append(f"T={daily_trade_counts.get(symbol,0)}/{sym_daily_cap}")
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
# GATE TRACKING — defined before main loop
# =========================
gate_stats      = defaultdict(lambda: defaultdict(int))
last_gate_reason = {s: None for s in SYMBOLS}
# Rolling 2-hour buffer of gate hits keyed by symbol — feeds the pre-session brief.
# Deque elements are (epoch_ts, reason). Cap=4096 per symbol covers >2h of dense rejection bursts.
recent_gate_hits = {s: deque(maxlen=4096) for s in SYMBOLS}

def _gate_hit(symbol, reason):
    gate_stats[symbol][reason] += 1
    last_gate_reason[symbol] = reason
    try:
        recent_gate_hits[symbol].append((time.time(), reason))
    except KeyError:
        # Symbol not in SYMBOLS list (shouldn't happen, but don't break trading)
        pass

def _log_rejected_signal(symbol, direction, entry_mode, score, reason, claude_confidence=None):
    """V5.1 — Log structured rejection for dashboard display."""
    claude_str = f" | claude_conf={claude_confidence:.2f}" if claude_confidence is not None else ""
    logger.info("[REJECTED_SIGNAL] %s | %s | %s | score=%s | reason=%s%s",
                symbol, direction, entry_mode, score, reason, claude_str)

def _should_emit_continuation_log(symbol, bos_idx, direction, score, entry_mode="continuation"):
    state = (bos_idx, direction, score)
    key = (canonical_symbol(symbol), entry_mode)
    if continuation_log_state.get(key) == state:
        return False
    continuation_log_state[key] = state
    return True

def _should_emit_hold_log(symbol, diag_line):
    key = canonical_symbol(symbol)
    if hold_log_state.get(key) == diag_line:
        return False
    hold_log_state[key] = diag_line
    return True

def _gate_report_if_due():
    global _last_gate_report_ts
    now = time.time()
    if now - _last_gate_report_ts < GATE_REPORT_INTERVAL:
        return
    _last_gate_report_ts = now
    try:
        for sym in SYMBOLS:
            c = gate_stats.get(sym)
            if not c: continue
            top   = sorted(c.items(), key=lambda kv: kv[1], reverse=True)[:6]
            top_s = " ".join([f"{k}={v}" for k, v in top])
            lr    = last_gate_reason.get(sym, "-")
            logger.info("[GATES] %s last=%s | %s", sym, lr, top_s)
    except Exception:
        pass

# =========================
# MAIN LOOP
# =========================
cp("SYSTEM", "━" * 65)
if TESTING_MODE:
    cp("WARNING", "  ⚠️  TESTING MODE — relaxed limits active")
    cp("WARNING", f"     max_open={SYMBOL_CONFIG['XAUUSD']['max_trades']}/sym  "
                  f"max_day={MAX_TRADES_PER_DAY}/sym  "
                  f"cooldown={SYMBOL_CONFIG['XAUUSD']['cooldown']}s  "
                  f"DD={DAILY_DRAWDOWN_LIMIT*100:.0f}%")
cp("SYSTEM", f"  {BOT_NAME} {BOT_VERSION}")
cp("SYSTEM", "  Overnight test build:")
cp("SYSTEM", "    FIX 1 — _session_gate_ok() added; called at TOP of get_signal() and")
cp("SYSTEM", "           check_pending_pullbacks(). EUR/GBP hard-blocked outside session+KZ.")
cp("SYSTEM", "           Matches backtest entry conditions. Primary gate is now in signal engine.")
cp("SYSTEM", "    FIX 2 — mgmt_lockin_enabled and mgmt_time_stop_enabled defaults → False.")
cp("SYSTEM", "           Prevents silent enablement for symbols without explicit config keys.")
cp("SYSTEM", "    FIX 3 — BE uses one-pip buffer (entry ± 1 point) instead of live spread.")
cp("SYSTEM", "           Stable under spread widening. Validated correct for BUY/SELL.")
cp("SYSTEM", "    FIX 4 — Same-BOS pullback re-arm blocking enabled in runtime config.")
cp("SYSTEM", "    FIX 5 — Repeated same-BOS continuation low-score logging deduped.")
cp("SYSTEM", "  V13 features retained: HTF hard block | freshness-aware candle | score validation")
cp("SYSTEM", "  Entry modes: BREAKOUT | PULLBACK | CONTINUATION")
cp("SYSTEM", f"  Score: BO≥{SCORE_MIN_BREAKOUT} PB≥{SCORE_MIN_PULLBACK} CONT≥{SCORE_MIN_CONTINUATION}")
cp("SYSTEM", f"  PB zone: {PB_NEAR*100:.0f}%–{PB_FAR*100:.0f}% Fib | Exhaust >{PB_EXHAUSTION_ATR_MULT:.0f}×ATR blocked")
cp("SYSTEM", f"  Zone inval: XAU={SYMBOL_CONFIG['XAUUSD']['zone_invalidate_mult']} "
             f"EUR/GBP={SYMBOL_CONFIG['EURUSD']['zone_invalidate_mult']}")
cp("SYSTEM", f"  Pending TTL: XAU={SYMBOL_CONFIG['XAUUSD']['pending_ttl']}s "
             f"EUR/GBP={SYMBOL_CONFIG['EURUSD']['pending_ttl']}s")
cp("SYSTEM", f"  Cont: trig={CONTINUATION_THRESHOLD_MULT}×ATR | max={CONT_MAX_DIST_MULT}×ATR"
             f" | mom≥{CONT_MOMENTUM_RATIO} after {CONT_MOMENTUM_MIN_DIST_MULT}×ATR from BOS")
_sg = {s: "ON" if SYMBOL_CONFIG[s].get("session_gate", False) else "OFF" for s in SYMBOLS}
cp("SYSTEM", f"  Session gate: " + " ".join(f"{s}={_sg[s]}" for s in SYMBOLS))
cp("SYSTEM", f"  RR: XAU≥{SYMBOL_CONFIG['XAUUSD'].get('min_rr', MIN_RR)} "
             f"EUR/GBP≥{MIN_RR} | "
             f"Risk={RISK_PERCENT*100:.1f}% | DD={DAILY_DRAWDOWN_LIMIT*100:.0f}%")
cp("SYSTEM", f"  Session score: XAU={'ON' if SYMBOL_CONFIG['XAUUSD'].get('session_score_bonus', True) else 'OFF'} "
             f"EUR/GBP=ON")
for _s in SYMBOLS:
    _c     = SYMBOL_CONFIG[_s]
    _parts = [f"BE@{_c.get('mgmt_be_lock_r', 1.0)}R (1-pip)"]
    if _c.get("mgmt_partial_enabled",  False): _parts.append("Partial")
    if _c.get("mgmt_lockin_enabled",   False): _parts.append(
        f"LK@{_c.get('mgmt_lockin_r', LOCK_IN_R_MULTIPLE)}R/{_c.get('mgmt_lockin_drawback', LOCK_IN_DRAWBACK)}")
    if _c.get("mgmt_time_stop_enabled", False): _parts.append(
        f"TS(minR={_c.get('mgmt_time_stop_min_r', TIME_STOP_MIN_PROGRESS_R)})")
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

# FIX 4 (V13.1) — logger updated
logger.info("%s %s | session gate in signal engine | mgmt defaults False | BE 1-pip | continuation log dedupe",
            BOT_NAME, BOT_VERSION)

# Initialize bot monitoring
bot_start_time_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
last_heartbeat_utc = bot_start_time_utc
logger.info("[STARTUP] Bot monitoring initialized | start_time=%s", bot_start_time_utc.strftime('%Y-%m-%d %H:%M:%S UTC'))
_phoenix_emit("BOT_START", "ALL",
              f"Bot started | version={BOT_VERSION} | symbols={','.join(SYMBOLS)}",
              severity="INFO", department="INFRA",
              metadata={"bot_name": BOT_NAME, "bot_version": BOT_VERSION,
                        "symbols": SYMBOLS, "magic": MAGIC,
                        "risk_pct": RISK_PERCENT, "dd_limit": DAILY_DRAWDOWN_LIMIT,
                        "start_time": bot_start_time_utc.isoformat()})

for s in SYMBOLS:
    ensure_symbol(s)

# Check for trades that closed while bot was offline
check_offline_closures()

# Check if bot was offline for extended period
check_uptime_warning()

# Log initial heartbeat
log_heartbeat()

# Initialise opportunity scanner and seed with first scan
init_scanner(
    symbols=SYMBOLS,
    api_key=ANTHROPIC_API_KEY,
    tg_send=tg_send,
    get_htf=get_htf_trend,
    gate_hits=recent_gate_hits,
    losses=recent_losses_per_symbol,
    pending=pending_pulls,
    get_regime=get_gold_regime_for_dashboard,
    get_calendar=_fetch_economic_calendar,
)
run_opportunity_scan()

while True:
    if not is_connected():
        cp("WARNING", "⚠️  MT5 connection lost — reconnecting…")
        logger.warning("MT5 connection lost")
        reconnect_mt5()
        reconcile_open_positions()
    
    # Check for data stalls even if connection appears healthy
    if check_tick_freshness():
        cp("WARNING", "⚠️  Data stall detected — forcing reconnect…")
        logger.warning("Data stall detected - forcing reconnect")
        reconnect_mt5()
        reconcile_open_positions()

    reset_daily_trackers()

    if check_daily_drawdown():
        cp("HALT", "🛑 Daily halt — management only")
        detect_closed_trades()
        manage_trades()
        time.sleep(5)
        continue

    if check_total_loss():
        cp("HALT", "🛑 FTMO total loss limit reached — stopping bot")
        logger.critical("FTMO total loss limit reached - bot stopped")
        break  # Stop bot if FTMO total loss limit reached

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
            cp(color_key, f"└ {symbol} ✅ BUY [{mode.upper()}] | Price={price:.5f}")
            logger.info("%s BUY [%s] | Price=%s", symbol, mode, price)
            execute_trade(symbol, "BUY", meta or {})

        elif sig == "SELL":
            mode      = meta.get("entry_mode", "pullback") if meta else "pullback"
            color_key = "CONTINUATION" if mode == "continuation" else "SELL"
            cp(color_key, f"└ {symbol} 🔻 SELL [{mode.upper()}] | Price={price:.5f}")
            logger.info("%s SELL [%s] | Price=%s", symbol, mode, price)
            execute_trade(symbol, "SELL", meta or {})

        else:
            diag = build_diagnostic_line(symbol, price, utc_time)
            if _should_emit_hold_log(symbol, diag):
                cp("DIAG", f"└ {diag}")
                logger.info("%s", diag)

    check_pending_pullbacks()
    _gate_report_if_due()
    detect_closed_trades()
    manage_trades()
    log_heartbeat()
    _session_brief_check()
    run_opportunity_scan()
    time.sleep(5)
