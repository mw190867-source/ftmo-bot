"""
FTMO Dashboard API — read-only telemetry server for FTMO_V1.py

Architecture: separate process from the bot. Tails ftmo_v1.log for events
and uses MT5 Python API for live account state. Stdlib only — no Flask,
no install needed. Runs on port 5001 with permissive CORS.
"""
import json
import re
import threading
import time
import os
from collections import deque, defaultdict
from datetime import datetime, timezone
import pytz
_LONDON_TZ = pytz.timezone('Europe/London')
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Optional MT5 — if unavailable, we degrade to log-only data
try:
    import MetaTrader5 as mt5
    _HAS_MT5 = True
except Exception:
    _HAS_MT5 = False

BASE_DIR = Path(__file__).resolve().parent

# Auto-load .env from script directory so we don't need shell exports
def _load_dotenv():
    env_path = BASE_DIR / ".env"
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
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as e:
        print(f"[env] failed to load .env: {e}")

_load_dotenv()

LOG_FILE = BASE_DIR / "ftmo_v1.log"
HEARTBEAT_FILE = BASE_DIR / "bot_heartbeat.log"
STATE_FILE = BASE_DIR / "dashboard_state.json"
PORT = 5002
MAGIC = 123456
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]
FTMO_PROFIT_TARGET_PCT = 0.05
DAILY_DRAWDOWN_LIMIT = 0.05

# Commission per round-trip per symbol (£) — used for net P/L calc on dashboard
COMMISSION_PER_TRADE = {
    "XAUUSD": 4.0,
    "EURUSD": 7.40,
    "GBPUSD": 7.40,
    "USDJPY": 7.40,
}

# =========================
# STATE — populated by log tailer
# =========================
state_lock = threading.Lock()
state = {
    "bot_alive": False,
    "bot_start_utc": None,
    "last_heartbeat_utc": None,
    "claude_enabled": True,
    "trades": deque(maxlen=200),       # closed trades
    "claude_decisions": deque(maxlen=50),
    "gates_per_symbol": defaultdict(lambda: deque(maxlen=20)),  # (ts, gate)
    "shadow_by_ticket": {},            # ticket -> verdict info
    "open_meta": {},                   # ticket -> {entry_mode, opened_at, ...}
    "challenge_start_equity": None,
    "challenge_start_date": None,  # ISO date (YYYY-MM-DD) — backfill ignores trades closed before this
    "daily_open_equity": None,
    "daily_equity_date": None,  # ISO date string for daily reset detection
    # V4 Gold Regime State
    "gold_regime": {
        "regime": "CORRECTIVE",  # Default until first classification
        "h1_ema": "NEUTRAL",
        "h4_atr_ratio": 0.0,
        "h4_crosses": 0,
        "last_classification_ts": None,
    },
    "sweep_status": {
        "last_sweep_type": None,  # SWEEP_HIGH or SWEEP_LOW
        "last_sweep_direction": None,  # BUY or SELL
        "last_sweep_ts": None,
        "sweep_valid": False,  # Within 30-minute window
    },
    "claude_hard_decisions": deque(maxlen=10),  # Hard gate verdicts
    "rejected_signals": deque(maxlen=50),       # V5.1 — structured rejection log
    "economic_calendar": {
        "events": [],  # Cached high-impact USD events
        "last_fetch_ts": None,
        "fetch_error": None,
    },
}


def _load_persisted_state():
    """Load challenge_start_equity and daily equity from disk on startup."""
    if not STATE_FILE.exists():
        return
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        state["challenge_start_equity"] = data.get("challenge_start_equity")
        state["challenge_start_date"] = data.get("challenge_start_date")
        # Only restore daily equity if it's still today (London date)
        today = datetime.now(_LONDON_TZ).date().isoformat()
        if data.get("daily_equity_date") == today:
            state["daily_open_equity"] = data.get("daily_open_equity")
            state["daily_equity_date"] = today
        print(f"[state] loaded: challenge_start={state['challenge_start_equity']} "
              f"challenge_start_date={state['challenge_start_date']} "
              f"daily_open={state['daily_open_equity']} (date={data.get('daily_equity_date')})")
    except Exception as e:
        print(f"[state] load failed: {e}")


def _persist_state():
    """Save current state to disk. Called when values change."""
    try:
        data = {
            "challenge_start_equity": state["challenge_start_equity"],
            "challenge_start_date": state["challenge_start_date"],
            "daily_open_equity": state["daily_open_equity"],
            "daily_equity_date": state["daily_equity_date"],
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[state] save failed: {e}")

# =========================
# LOG PARSERS
# =========================
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")

RE_OPEN = re.compile(
    r"\[OPEN\|(\w+)\] (\w+) \| Ticket:(\d+) \| Entry:([\d.]+) \| SL:([\d.]+) \| TP:([\d.]+) \| RR:([\d.]+)R"
)
# bot logs symbol via cp() but file logger version doesn't include sym in this line.
# We'll capture it from the colour print line which includes the symbol.
RE_OPEN_CP = re.compile(
    r"\[OPEN\|(\w+)\] (\w+) \| (\w+) \| Ticket:(\d+) \| Entry:([\d.]+) \| SL:([\d.]+) \| TP:([\d.]+) \| RR:([\d.]+)R"
)

RE_CLOSE = re.compile(
    # Handles BOTH bot log formats:
    #   "[CLOSE] SYM | Ticket:N | REASON | P/L:X"          (most common — _finalise_close)
    #   "[CLOSE] SYM | Ticket:N | reason=REASON | P/L:X"   (offline-closure path)
    # The optional `reason=` prefix is non-capturing.
    r"\[CLOSE\] (\w+) \| Ticket:(\d+) \| (?:reason=)?([^|]+?) \| P/L:(-?[\d.]+)(?: \| (.*))?"
)
# Emitted by `scan_for_offline_closures` when the bot recovers a trade close
# that was missed by the live deal-history poll (e.g. CLOSE landed with
# "closed (no deal history after retries)" first). Carries the real P/L and
# an embedded UTC close timestamp that we should prefer over the log-line ts.
# Example:
#   [OFFLINE_CLOSE] GBPUSD | Ticket:441946498 | reason=4 | P/L:78.23 | closed_at=2026-05-06 11:37:54 UTC | [sl 1.36105]
RE_OFFLINE_CLOSE = re.compile(
    r"\[OFFLINE_CLOSE\] (\w+) \| Ticket:(\d+) \| reason=([^|]+?) \| P/L:(-?[\d.]+) \| closed_at=([^|]+?)(?: \| (.*))?$"
)
RE_SHADOW = re.compile(
    r"\[CLAUDE_SHADOW\] (\w+) \| (\w+) \| verdict=(\w+) \| confidence=([\d.]+) \| reason=(.+?) \| actual=EXECUTED"
)
RE_SHADOW_OUTCOME = re.compile(
    r"\[CLAUDE_SHADOW_OUTCOME\] (\w+) \| ticket=(\d+) \| shadow_verdict=(\w+) \| actual_pl=(-?[\d.]+)"
)
RE_GATES = re.compile(r"\[GATES\] (\w+) last=([^|]+) \| (.*)")
RE_HEARTBEAT = re.compile(r"\[HEARTBEAT\] Uptime:(\S+) \| Active trades:(\d+) \| (\S+ \S+ UTC)")
RE_STARTUP = re.compile(r"\[STARTUP\] Bot monitoring initialized \| start_time=(.+UTC)")

# V4 Regime and Gate Patterns
RE_GOLD_REGIME = re.compile(
    r"\[GOLD_REGIME\] (\w+) \| regime=(\w+) \| h1_ema=(\w+) \| h4_atr_ratio=([\d.]+) \| h4_crosses=(\d+)"
)
RE_CLAUDE_HARD_GATE = re.compile(
    r"\[CLAUDE_HARD_GATE\] (\w+) (\w+) \| (\w+) \| confidence=([\d.]+) \| (.+)"
)
RE_ECONOMIC_CALENDAR_FETCH = re.compile(
    r"\[ECONOMIC_CALENDAR\] Fetched (\d+) high-impact USD events"
)
RE_ECONOMIC_CALENDAR_ERROR = re.compile(
    r"\[ECONOMIC_CALENDAR\] (Fetch failed|Error): (.+)"
)
RE_SWEEP_DETECTED = re.compile(
    r"\[SWEEP\] (\w+) \| (SWEEP_HIGH|SWEEP_LOW) \| direction=(BUY|SELL)"
)
# V5.1 — Rejected signal log lines from FTMO_V1.py _log_rejected_signal()
RE_REJECTED = re.compile(
    r"\[REJECTED_SIGNAL\] (\w+) \| (\w+) \| (\w+) \| score=(\S+) \| reason=(\S+)"
    r"(?:\s*\|\s*claude_conf=([\d.]+))?"
)


def _parse_ts(line):
    m = RE_TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _process_line(line):
    ts = _parse_ts(line)
    # Don't use log line timestamp - it's BST due to Wine timezone bug
    # Use system UTC time instead
    now = datetime.now(timezone.utc)

    with state_lock:
        # V5.1 — Rejected signal capture (cheap regex; check first)
        m = RE_REJECTED.search(line)
        if m:
            sym, direction, mode, score, reason, conf = m.groups()
            try:
                score_v = int(score) if score.isdigit() else score
            except Exception:
                score_v = score
            state["rejected_signals"].append({
                "timestamp": now.isoformat(),
                "symbol": sym,
                "direction": direction,
                "mode": mode,
                "score": score_v,
                "reason": reason,
                "claude_confidence": float(conf) if conf else None,
            })
            return
        # OPEN — fall back to short pattern (no symbol) if needed
        m = RE_OPEN_CP.search(line)
        if m:
            mode, direction, sym, ticket, entry, sl, tp, rr = m.groups()
            state["open_meta"][int(ticket)] = {
                "symbol": sym, "direction": direction, "entry_mode": mode.lower(),
                "entry": float(entry), "sl": float(sl), "tp": float(tp),
                "rr": float(rr), "opened_at": now.isoformat(),
            }
            state["bot_alive"] = True
            return

        m = RE_OPEN.search(line)
        if m:
            mode, direction, ticket, entry, sl, tp, rr = m.groups()
            # Symbol unknown from this regex — leave for shadow to fill in
            state["open_meta"].setdefault(int(ticket), {
                "symbol": "?", "direction": direction, "entry_mode": mode.lower(),
                "entry": float(entry), "sl": float(sl), "tp": float(tp),
                "rr": float(rr), "opened_at": now.isoformat(),
            })
            return

        # OFFLINE_CLOSE — handle BEFORE the generic [CLOSE] regex because the line
        # also contains the substring "[CLOSE]" (the bot's tag tooling). We treat
        # an OFFLINE_CLOSE as authoritative: if a CLOSE was already recorded for
        # this ticket we update its P/L and reason; otherwise we insert it.
        m = RE_OFFLINE_CLOSE.search(line)
        if m:
            sym, ticket, raw_reason, pl, closed_at_str, comment = m.groups()
            ticket = int(ticket)
            try:
                pl_f = float(pl)
            except ValueError:
                return
            display_reason, is_junk = _normalize_close_reason(raw_reason, comment)
            if is_junk:
                return
            try:
                closed_at_iso = datetime.strptime(closed_at_str.strip(), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                closed_at_iso = now.isoformat()
            # If this ticket is already in the trades deque (e.g. emitted twice or
            # we processed a stub earlier), update in place instead of duplicating.
            existing = None
            for tr in state["trades"]:
                if tr.get("ticket") == ticket:
                    existing = tr
                    break
            commission = COMMISSION_PER_TRADE.get(sym, 7.40)
            meta = state["open_meta"].get(ticket, {})
            if existing is not None:
                existing["pl"] = pl_f
                existing["net_pl"] = round(pl_f - commission, 2)
                existing["reason"] = display_reason
                existing["closed_at"] = closed_at_iso
            else:
                state["trades"].appendleft({
                    "symbol": sym, "ticket": ticket,
                    "reason": display_reason,
                    "pl": pl_f,
                    "net_pl": round(pl_f - commission, 2),
                    "commission": commission,
                    "shadow_verdict": "—",
                    "shadow_confidence": 0.0,
                    "claude_correct": None,
                    "closed_at": closed_at_iso,
                    "entry_mode": meta.get("entry_mode", "?"),
                    "direction": meta.get("direction", "?"),
                })
            state["open_meta"].pop(ticket, None)
            return

        m = RE_CLOSE.search(line)
        if m:
            sym, ticket, raw_reason, pl, comment = m.groups()
            print(f"[CLOSE_DETECTED] {sym} | Ticket:{ticket} | P/L:{pl}")
            ticket = int(ticket)
            try:
                pl_f = float(pl)
            except ValueError:
                return
            display_reason, is_junk = _normalize_close_reason(raw_reason, comment)
            if is_junk:
                # Wrong-deal-lookup artefact (P/L:0 + entry magic comment). Drop it.
                state["open_meta"].pop(ticket, None)
                return
            shadow = state["shadow_by_ticket"].get(ticket, {})
            verdict = shadow.get("verdict", "—")
            confidence = shadow.get("confidence", 0.0)
            # Was Claude correct? approve+win or block+loss = match
            match = None
            if verdict in ("approve", "approve_reduced"):
                match = pl_f > 0
            elif verdict == "block":
                match = pl_f < 0
            commission = COMMISSION_PER_TRADE.get(sym, 7.40)
            state["trades"].appendleft({
                "symbol": sym, "ticket": ticket,
                "reason": display_reason,
                "pl": pl_f,
                "net_pl": pl_f - commission,
                "commission": commission,
                "shadow_verdict": verdict,
                "shadow_confidence": confidence,
                "claude_correct": match,
                "closed_at": now.isoformat(),
                "entry_mode": state["open_meta"].get(ticket, {}).get("entry_mode", "?"),
                "direction": state["open_meta"].get(ticket, {}).get("direction", "?"),
            })
            state["open_meta"].pop(ticket, None)
            return

        m = RE_SHADOW.search(line)
        if m:
            sym, direction, verdict, confidence, reason = m.groups()
            decision = {
                "symbol": sym, "direction": direction,
                "verdict": verdict, "confidence": float(confidence),
                "reason": reason.strip(), "ts": now.isoformat(),
            }
            state["claude_decisions"].appendleft(decision)
            # Match to most recent open trade for this symbol/direction
            for ticket, meta in list(state["open_meta"].items()):
                if meta.get("symbol") == sym and meta.get("direction") == direction:
                    state["shadow_by_ticket"][ticket] = {
                        "verdict": verdict, "confidence": float(confidence),
                        "reason": reason.strip(),
                    }
                    decision["entry_mode"] = meta.get("entry_mode", "?")
                    break
            return

        m = RE_SHADOW_OUTCOME.search(line)
        if m:
            sym, ticket, verdict, pl = m.groups()
            # Already handled by [CLOSE] — but ensure shadow_by_ticket recorded
            state["shadow_by_ticket"].setdefault(int(ticket), {
                "verdict": verdict, "confidence": 0.0, "reason": ""
            })
            return

        m = RE_GATES.search(line)
        if m:
            sym, last, details = m.groups()
            state["gates_per_symbol"][sym].appendleft({
                "ts": now.isoformat(), "last": last.strip(), "details": details.strip(),
            })
            return

        m = RE_HEARTBEAT.search(line)
        if m:
            # Use the UTC timestamp from the heartbeat message content, not the log line timestamp
            # (log line timestamp is BST due to Wine timezone bug, heartbeat message has correct UTC)
            try:
                ts_str = f"{m.group(3)} UTC"
                hb_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                state["last_heartbeat_utc"] = hb_dt.isoformat()
            except Exception:
                state["last_heartbeat_utc"] = now.isoformat()
            state["bot_alive"] = True
            return

        m = RE_STARTUP.search(line)
        if m:
            state["bot_start_utc"] = now.isoformat()
            state["bot_alive"] = True
            return

        # V4: Gold Regime Classification
        m = RE_GOLD_REGIME.search(line)
        if m:
            sym, regime, h1_ema, atr_ratio, crosses = m.groups()
            state["gold_regime"].update({
                "regime": regime,
                "h1_ema": h1_ema,
                "h4_atr_ratio": float(atr_ratio),
                "h4_crosses": int(crosses),
                "last_classification_ts": now.isoformat(),
            })
            return

        # V4: Claude Hard Gate Verdict
        m = RE_CLAUDE_HARD_GATE.search(line)
        if m:
            sym, direction, verdict, confidence, reason = m.groups()
            state["claude_hard_decisions"].appendleft({
                "symbol": sym,
                "direction": direction,
                "verdict": verdict,  # APPROVED or BLOCKED
                "confidence": float(confidence),
                "reason": reason.strip(),
                "ts": now.isoformat(),
                "gate_type": "hard",  # Distinguish from shadow
            })
            return

        # V4: Economic Calendar Fetch
        m = RE_ECONOMIC_CALENDAR_FETCH.search(line)
        if m:
            count = int(m.group(1))
            state["economic_calendar"]["last_fetch_ts"] = now.isoformat()
            state["economic_calendar"]["fetch_error"] = None
            return

        # V4: Economic Calendar Error
        m = RE_ECONOMIC_CALENDAR_ERROR.search(line)
        if m:
            error_type, error_msg = m.groups()
            state["economic_calendar"]["fetch_error"] = f"{error_type}: {error_msg.strip()}"
            return

        # V4: Sweep Detection
        m = RE_SWEEP_DETECTED.search(line)
        if m:
            sym, sweep_type, direction = m.groups()
            if sym == "XAUUSD":
                state["sweep_status"].update({
                    "last_sweep_type": sweep_type,
                    "last_sweep_direction": direction,
                    "last_sweep_ts": now.isoformat(),
                    "sweep_valid": True,
                })
            return


# MT5 deal reason code → human label fallback (used only when comment is empty/uninformative).
# These follow the MetaTrader5 DEAL_REASON enum:
#   0=CLIENT, 1=MOBILE, 2=WEB, 3=EXPERT(bot-initiated), 4=SL, 5=TP, 6=STOP_OUT, 7=ROLLOVER, 8=VMARGIN, 9=GATEWAY, 10=SO
_MT5_REASON_CODE_LABELS = {
    "0": "MANUAL", "1": "MANUAL", "2": "MANUAL",
    "3": "EXPERT", "4": "SL", "5": "TP", "6": "STOP_OUT",
    "7": "ROLLOVER", "8": "MARGIN", "9": "GATEWAY", "10": "STOP_OUT",
}


def _post_label_for_pl(reason: str, pl: float) -> str:
    """Refine the close-reason label using the realised P/L.

    The MT5 deal-reason for any SL-line touch is the same (DEAL_REASON_SL=4)
    whether it's the original stop-loss being hit OR a trailing stop above
    entry being clipped after a winning move. Both end up labelled "SL" by
    `_normalize_close_reason`, which is technically accurate but misleading
    in the trade table — a +£78 "SL" row reads like a stop-out loss.

    Heuristic: if the reason is SL and the realised P/L is positive, the SL
    must have been a trailing stop (or BE-lock above entry). Relabel as
    TRAIL_STOP to make the distinction unambiguous in the dashboard.
    """
    if reason == "SL" and pl > 0:
        return "TRAIL_STOP"
    return reason


def _normalize_close_reason(raw_reason: str, comment: str | None) -> tuple[str, bool]:
    """Translate the [CLOSE] log line's reason+comment into a human-readable label.

    Returns (display_reason, is_junk).
    `is_junk` is True when the line looks like a wrong-deal-lookup artefact
    (P/L:0.00 paired with the entry's magic comment "SMC V1.0 ..."), which the
    bot occasionally emits when the deal-history call returns the entry deal
    instead of the close deal. Such rows are useless and should be filtered.

    Format A (active close):
      [CLOSE] SYM | Ticket:N | ZONE_INVALIDATED | P/L:X
        -> raw_reason = "ZONE_INVALIDATED", comment = None
        -> display = "ZONE_INVALIDATED"

    Format B (deal-history close):
      [CLOSE] SYM | Ticket:N | reason=4 | P/L:X | [sl 1.36165]
        -> raw_reason = "4" (numeric MT5 code), comment = "[sl 1.36165]"
        -> display = "SL"
    """
    raw = (raw_reason or "").strip()
    cm = (comment or "").strip()

    # Heuristic: bot emitted CLOSE but deal-history returned the ENTRY deal —
    # comment looks like the entry's magic ("SMC V1.0 pull|brea|cont"). Skip.
    if cm.startswith("SMC V1.0"):
        return ("", True)

    # Format A — non-numeric raw reason means the bot wrote the human label directly.
    if not raw.isdigit():
        return (raw or "?", False)

    # Format B — raw is a numeric MT5 deal reason code.
    # Prefer the comment field if it carries useful info.
    if cm:
        cl = cm.lower()
        # Bot's own close request: "close:ZONE_INVALIDATED"
        if cl.startswith("close:"):
            return (cm.split(":", 1)[1].strip().upper(), False)
        # MT5 SL/TP closes: "[sl 1.36165]" or "[tp 1.36284]" or just "tp"/"sl"
        if cl.startswith("[sl") or cl == "sl":
            return ("SL", False)
        if cl.startswith("[tp") or cl == "tp":
            return ("TP", False)
        if "stop out" in cl or cl == "so":
            return ("STOP_OUT", False)
        # Some other free-form comment — surface it as-is, capped to keep the table tidy.
        return (cm[:30], False)

    # Comment empty: fall back to the numeric code.
    return (_MT5_REASON_CODE_LABELS.get(raw, f"reason={raw}"), False)


def _backfill_closed_trades(max_trades=50):
    """Scan the current log file plus rotated siblings (ftmo_v1.log.1, .2, ...) and
    seed `state["trades"]` with the most recent challenge-window CLOSE events.

    Two-pass design:
      1. Walk every log file and build `ticket_meta`: ticket -> {direction, mode}
         from `[OPEN|MODE] DIRECTION | Ticket:N ...` lines. Cross-referencing here
         is necessary because [CLOSE] lines do not carry direction or mode.
      2. Walk again and parse [CLOSE] lines, joining with `ticket_meta` and
         normalizing the reason via `_normalize_close_reason`.

    Filters applied:
      - challenge_start_date (from dashboard_state.json) — drops pre-challenge
        trades (Challenge 1, BTC test runs, etc.).
      - junk wrong-deal-lookup rows where comment is the entry magic.
      - Partial closes are already excluded because they log as [PARTIAL_TP],
        not [CLOSE], so RE_CLOSE never matches them.
    """
    try:
        log_files = sorted(BASE_DIR.glob("ftmo_v1.log*"))

        # Cutoff for the date filter — start-of-day at challenge_start_date in UTC.
        with state_lock:
            start_date_str = state.get("challenge_start_date")
        cutoff_dt = None
        if start_date_str:
            try:
                cutoff_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception as e:
                print(f"[backfill] invalid challenge_start_date {start_date_str!r}: {e}")

        # ---- Pass 1: build ticket -> {direction, entry_mode} from OPEN lines ----
        ticket_meta = {}
        re_open_short = RE_OPEN          # [OPEN|MODE] DIRECTION | Ticket:...
        re_open_long  = RE_OPEN_CP       # [OPEN|MODE] DIRECTION | SYM | Ticket:...
        for lf in log_files:
            try:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "[OPEN|" not in line:
                            continue
                        m = re_open_long.search(line)
                        if m:
                            mode, direction, _sym, ticket, *_ = m.groups()
                            ticket_meta[int(ticket)] = {
                                "direction": direction,
                                "entry_mode": mode.lower(),
                            }
                            continue
                        m = re_open_short.search(line)
                        if m:
                            mode, direction, ticket, *_ = m.groups()
                            ticket_meta.setdefault(int(ticket), {
                                "direction": direction,
                                "entry_mode": mode.lower(),
                            })
            except Exception as e:
                print(f"[backfill] OPEN-pass skipping {lf.name}: {e}")

        # ---- Pass 2: collect CLOSE and OFFLINE_CLOSE events ----
        # Dedupe by ticket. LIVE [CLOSE] wins over [OFFLINE_CLOSE] when both exist,
        # because the bot writes its own human reason verbatim to the live log
        # (e.g. "ZONE_INVALIDATED") whereas the OFFLINE_CLOSE record carries the
        # MT5 deal.comment which is truncated to ~27 chars by the broker
        # (e.g. "close:ZONE_INVAL"). P/L values match in the both-exist case so
        # nothing is lost; we just keep the cleaner reason.
        # OFFLINE_CLOSE is essential as the SOLE record for tickets whose live
        # CLOSE was the "closed (no deal history after retries)" variant — that
        # variant has no P/L field and therefore doesn't match RE_CLOSE at all,
        # so it never makes it into by_ticket. Without OFFLINE_CLOSE those
        # trades would be invisible (which is exactly the bug ticket
        # 441946498 / +£78.23 demonstrated).
        by_ticket: dict[int, dict] = {}
        skipped_pre  = 0
        skipped_junk = 0
        for lf in log_files:
            try:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    for raw_line in f:
                        line = raw_line.rstrip("\n")
                        is_offline = "[OFFLINE_CLOSE]" in line
                        is_close   = "[CLOSE]" in line and not is_offline
                        if not (is_close or is_offline):
                            continue

                        if is_offline:
                            m = RE_OFFLINE_CLOSE.search(line)
                            if not m:
                                continue
                            sym, ticket, raw_reason, pl, closed_at_str, comment = m.groups()
                            # Prefer the embedded UTC close timestamp over the log-line ts.
                            try:
                                ts = datetime.strptime(closed_at_str.strip(), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                            except Exception:
                                ts_m = RE_TS.match(line)
                                if not ts_m:
                                    continue
                                ts = datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        else:
                            m = RE_CLOSE.search(line)
                            if not m:
                                continue
                            ts_m = RE_TS.match(line)
                            if not ts_m:
                                continue
                            try:
                                ts = datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            except Exception:
                                continue
                            sym, ticket, raw_reason, pl, comment = m.groups()

                        if cutoff_dt is not None and ts < cutoff_dt:
                            skipped_pre += 1
                            continue
                        try:
                            pl_f = float(pl)
                        except ValueError:
                            continue
                        display_reason, is_junk = _normalize_close_reason(raw_reason, comment)
                        if is_junk:
                            skipped_junk += 1
                            continue
                        meta = ticket_meta.get(int(ticket), {})
                        rec = {
                            "ts": ts,
                            "symbol": sym,
                            "ticket": int(ticket),
                            "reason": display_reason,
                            "pl": pl_f,
                            "direction": meta.get("direction", "?"),
                            "entry_mode": meta.get("entry_mode", "?"),
                            "source": "offline" if is_offline else "live",
                        }
                        existing = by_ticket.get(rec["ticket"])
                        # LIVE CLOSE overrides a prior OFFLINE_CLOSE (cleaner reason);
                        # OFFLINE_CLOSE only fills slots where no LIVE record exists.
                        if existing is None or (rec["source"] == "live" and existing["source"] == "offline"):
                            by_ticket[rec["ticket"]] = rec
            except Exception as e:
                print(f"[backfill] CLOSE-pass skipping {lf.name}: {e}")
                continue

        all_closes = sorted(by_ticket.values(), key=lambda r: r["ts"], reverse=True)
        keep = all_closes[:max_trades]
        with state_lock:
            for rec in reversed(keep):
                commission = COMMISSION_PER_TRADE.get(rec["symbol"], 7.40)
                state["trades"].appendleft({
                    "symbol": rec["symbol"],
                    "ticket": rec["ticket"],
                    "reason": rec["reason"],
                    "pl": rec["pl"],
                    "net_pl": round(rec["pl"] - commission, 2),
                    "commission": commission,
                    "shadow_verdict": "—",
                    "shadow_confidence": 0.0,
                    "claude_correct": None,
                    "closed_at": rec["ts"].isoformat(),
                    "entry_mode": rec["entry_mode"],
                    "direction": rec["direction"],
                })
        print(f"[backfill] seeded {len(keep)} trades from {len(log_files)} log file(s) "
              f"| OPEN map={len(ticket_meta)} | skipped pre-challenge={skipped_pre} junk={skipped_junk}")
    except Exception as e:
        print(f"[backfill] failed: {e}")


def tail_log():
    """Background thread that follows the rotating log file."""
    last_size = 0
    last_inode = None
    while True:
        try:
            if not LOG_FILE.exists():
                time.sleep(1)
                continue
            st = LOG_FILE.stat()
            if last_inode is not None and st.st_ino != last_inode:
                # Rotated
                last_size = 0
            last_inode = st.st_ino
            if st.st_size < last_size:
                # Truncated
                last_size = 0
            if st.st_size == last_size:
                time.sleep(0.5)
                continue
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                if last_size == 0 and st.st_size > 200_000:
                    # Initial startup: only read last ~200KB to backfill recent state
                    f.seek(st.st_size - 200_000)
                    f.readline()  # skip partial
                else:
                    f.seek(last_size)
                for line in f:
                    _process_line(line)
                last_size = f.tell()
        except Exception as e:
            print(f"[tail] error: {e}")
            time.sleep(1)


# =========================
# MT5 LIVE DATA
# =========================
mt5_initialized = False


def mt5_init_once():
    global mt5_initialized
    if not _HAS_MT5 or mt5_initialized:
        return mt5_initialized
    try:
        if mt5.initialize():
            mt5_initialized = True
            print(f"[mt5] connected, build={mt5.version()}")
        else:
            print(f"[mt5] initialize failed: {mt5.last_error()}")
    except Exception as e:
        print(f"[mt5] init exception: {e}")
    return mt5_initialized


def get_account_live():
    if not mt5_init_once():
        return None
    try:
        acc = mt5.account_info()
        if acc:
            return {
                "equity": float(acc.equity),
                "balance": float(acc.balance),
                "currency": acc.currency,
                "margin": float(acc.margin),
                "margin_free": float(acc.margin_free),
            }
    except Exception:
        pass
    return None


def get_positions_live():
    if not mt5_init_once():
        return []
    try:
        positions = mt5.positions_get() or []
        out = []
        for p in positions:
            if p.magic != MAGIC:
                continue
            ticket = p.ticket
            meta = state["open_meta"].get(ticket, {})
            shadow = state["shadow_by_ticket"].get(ticket, {})
            entry = float(p.price_open)
            sl = float(p.sl) if p.sl else 0.0
            tp = float(p.tp) if p.tp else 0.0
            current = float(p.price_current)
            direction = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
            risk_dist = abs(entry - sl) if sl else 0.0
            if direction == "BUY":
                progress = (current - entry) / risk_dist if risk_dist else 0
            else:
                progress = (entry - current) / risk_dist if risk_dist else 0
            out.append({
                "ticket": ticket,
                "symbol": p.symbol,
                "direction": direction,
                "entry_mode": meta.get("entry_mode", "?"),
                "entry": entry, "sl": sl, "tp": tp,
                "current": current,
                "lot": float(p.volume),
                "pl": float(p.profit),
                "r_progress": round(progress, 2),
                "opened_utc": int(p.time),
                "shadow_verdict": shadow.get("verdict", "—"),
                "shadow_confidence": shadow.get("confidence", 0.0),
            })
        return out
    except Exception as e:
        print(f"[mt5] positions error: {e}")
        return []


# =========================
# ENDPOINT BUILDERS
# =========================
def _heartbeat_age_minutes():
    with state_lock:
        last = state["last_heartbeat_utc"]
    if not last:
        return None
    try:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return None


def api_status():
    age = _heartbeat_age_minutes()
    alive = age is not None and age < 5
    with state_lock:
        return {
            "bot_alive": alive,
            "heartbeat_age_minutes": round(age, 1) if age is not None else None,
            "bot_start_utc": state["bot_start_utc"],
            "last_heartbeat_utc": state["last_heartbeat_utc"],
            "claude_enabled": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "symbols": SYMBOLS,
        }


def api_account():
    acc = get_account_live()
    today = datetime.now(_LONDON_TZ).date().isoformat()
    needs_persist = False
    with state_lock:
        if state["challenge_start_equity"] is None and acc:
            # First time we see equity, capture as fallback start
            state["challenge_start_equity"] = acc["equity"]
            needs_persist = True
        # Daily reset: capture daily_open_equity once per London day
        if acc and (state["daily_open_equity"] is None or state["daily_equity_date"] != today):
            state["daily_open_equity"] = acc["equity"]
            state["daily_equity_date"] = today
            needs_persist = True
        start_eq = state["challenge_start_equity"]
        daily_eq = state["daily_open_equity"]
    if needs_persist:
        _persist_state()

    if not acc:
        return {"available": False}

    eq = acc["equity"]
    daily_pnl = eq - daily_eq if daily_eq else 0.0
    daily_dd_pct = ((daily_eq - eq) / daily_eq * 100) if daily_eq and eq < daily_eq else 0.0
    ftmo_pnl = eq - start_eq if start_eq else 0.0
    ftmo_pct = (ftmo_pnl / start_eq * 100) if start_eq else 0.0
    target_amount = start_eq * FTMO_PROFIT_TARGET_PCT if start_eq else 0
    return {
        "available": True,
        "equity": round(eq, 2),
        "balance": round(acc["balance"], 2),
        "currency": acc["currency"],
        "daily_pnl": round(daily_pnl, 2),
        "daily_dd_pct": round(daily_dd_pct, 2),
        "daily_dd_limit_pct": DAILY_DRAWDOWN_LIMIT * 100,
        "ftmo_pnl": round(ftmo_pnl, 2),
        "ftmo_pct": round(ftmo_pct, 2),
        "ftmo_target_pct": FTMO_PROFIT_TARGET_PCT * 100,
        "ftmo_target_amount": round(target_amount, 2),
        "challenge_start_equity": round(start_eq, 2) if start_eq else None,
    }


def api_positions():
    return {"positions": get_positions_live()}


def api_trades():
    with state_lock:
        trades = list(state["trades"])[:20]
    # Aggregate today's metrics (London date — trades after midnight London count as today)
    today_london = datetime.now(_LONDON_TZ).date()
    todays = [t for t in trades if datetime.fromisoformat(t["closed_at"]).astimezone(_LONDON_TZ).date() == today_london]
    gross = sum(t["pl"] for t in todays)
    commission = sum(t["commission"] for t in todays)
    net = gross - commission
    correct = sum(1 for t in todays if t["claude_correct"] is True)
    total_judged = sum(1 for t in todays if t["claude_correct"] is not None)
    return {
        "trades": trades,
        "today": {
            "count": len(todays),
            "gross_pl": round(gross, 2),
            "commission": round(commission, 2),
            "net_pl": round(net, 2),
            "claude_correct": correct,
            "claude_total": total_judged,
        },
    }


def api_claude():
    with state_lock:
        # Combine shadow and hard gate decisions, sorted by timestamp (most recent first)
        shadow = list(state["claude_decisions"])
        hard = list(state["claude_hard_decisions"])
        # Tag each with source for display
        for d in shadow:
            d.setdefault("gate_type", "shadow")
        for d in hard:
            d.setdefault("gate_type", "hard")
        # Merge and sort by timestamp descending
        combined = sorted(shadow + hard, key=lambda x: x.get("ts", ""), reverse=True)
        decisions = combined[:10]
    return {"decisions": decisions, "enabled": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def api_gates():
    """V4: Enhanced gate statistics with top 3 reasons and V4-specific breakdown."""
    now = datetime.now(timezone.utc)
    # 2-hour window for statistics
    stats_cutoff = (now.timestamp()) - 7200
    # 5-minute window for recent gates
    recent_cutoff = (now.timestamp()) - 300

    out = {}
    with state_lock:
        for sym, entries in state["gates_per_symbol"].items():
            # Count gates by reason over 2-hour window
            reason_counts = defaultdict(int)
            recent = []
            v4_gates = {
                "no_sweep": 0,
                "economic_news": 0,
                "gold_compression": 0,
                "claude_hard_block": 0,
            }

            for e in entries:
                try:
                    ts = datetime.fromisoformat(e["ts"]).timestamp()
                except Exception:
                    continue

                gate_reason = e.get("last", "").strip()

                # Count in 2-hour window
                if ts >= stats_cutoff:
                    reason_counts[gate_reason] += 1
                    # Count V4-specific gates for XAUUSD
                    if sym == "XAUUSD" and gate_reason in v4_gates:
                        v4_gates[gate_reason] += 1

                # Collect recent entries (5-min window)
                if ts >= recent_cutoff:
                    recent.append(e)

            # Get top 3 reasons by count
            top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:3]

            out[sym] = {
                "last_5min": recent[:5],
                "current_last_gate": recent[0]["last"] if recent else None,
                "top_3_reasons": [{"reason": r, "count": c} for r, c in top_reasons],
                "v4_gates": v4_gates if sym == "XAUUSD" else None,
            }
    return {"gates": out}


def api_regime():
    """V4: Return comprehensive XAUUSD gold regime status from parsed log state."""
    now = datetime.now(timezone.utc)

    with state_lock:
        regime_data = state["gold_regime"].copy()
        sweep_data = state["sweep_status"].copy()

        # Calculate sweep validity (30-minute window) and minutes ago
        sweep_ts_str = sweep_data.get("last_sweep_ts")
        minutes_ago = None
        is_valid = False
        if sweep_ts_str:
            try:
                sweep_ts = datetime.fromisoformat(sweep_ts_str)
                elapsed = (now - sweep_ts).total_seconds()
                minutes_ago = int(elapsed / 60)
                is_valid = elapsed < 1800  # 30 minutes
            except Exception:
                pass

        sweep_data["minutes_ago"] = minutes_ago
        sweep_data["sweep_valid"] = is_valid

        # Calculate time since last regime classification
        last_class_ts = regime_data.get("last_classification_ts")
        minutes_since_classification = None
        if last_class_ts:
            try:
                class_ts = datetime.fromisoformat(last_class_ts)
                minutes_since_classification = int((now - class_ts).total_seconds() / 60)
            except Exception:
                pass
        regime_data["minutes_since_classification"] = minutes_since_classification

        return {
            "regime": regime_data.get("regime", "CORRECTIVE"),
            "h1_ema": regime_data.get("h1_ema", "NEUTRAL"),
            "h4_atr_ratio": regime_data.get("h4_atr_ratio", 0.0),
            "h4_crosses": regime_data.get("h4_crosses", 0),
            "last_classification_ts": last_class_ts,
            "minutes_since_classification": minutes_since_classification,
            "sweep": sweep_data,
            "timestamp": now.isoformat(),
        }


def api_calendar():
    """V4: Return cached economic calendar data with countdowns."""
    now = datetime.now(timezone.utc)

    with state_lock:
        cal = state["economic_calendar"]
        events = cal.get("events", [])
        last_fetch = cal.get("last_fetch_ts")
        fetch_error = cal.get("fetch_error")

        # Calculate minutes since last fetch
        minutes_since_fetch = None
        if last_fetch:
            try:
                fetch_ts = datetime.fromisoformat(last_fetch)
                minutes_since_fetch = int((now - fetch_ts).total_seconds() / 60)
            except Exception:
                pass

        return {
            "events": events,  # List of {title, datetime_utc, impact}
            "last_fetch_ts": last_fetch,
            "minutes_since_fetch": minutes_since_fetch,
            "fetch_error": fetch_error,
            "timestamp": now.isoformat(),
        }


# =========================
# HTTP SERVER
# =========================
def api_rejected():
    """V5.1 — Return last 50 rejected signals with breakdown counts."""
    with state_lock:
        items = list(state["rejected_signals"])
    claude_blocks = sum(1 for r in items if r["reason"] in ("claude_hard_block", "claude_htf_unknown"))
    score_blocks = sum(1 for r in items if r["reason"] == "score_below")
    structural = sum(1 for r in items if r["reason"] in ("candle_confirm", "no_sweep"))
    return {
        "items": list(reversed(items)),  # newest first
        "counts": {
            "total": len(items),
            "claude": claude_blocks,
            "score": score_blocks,
            "structural": structural,
        },
    }


def api_opportunities():
    """Read opportunity_alerts.json (written by opportunity_scanner.py in bot process)."""
    try:
        path = BASE_DIR / "opportunity_alerts.json"
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        return {"error": str(e)}
    return {}


ROUTES = {
    "/api/status": api_status,
    "/api/account": api_account,
    "/api/positions": api_positions,
    "/api/trades": api_trades,
    "/api/claude": api_claude,
    "/api/gates": api_gates,
    "/api/regime": api_regime,
    "/api/calendar": api_calendar,
    "/api/opportunities": api_opportunities,
    "/api/rejected": api_rejected,
}


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        handler = ROUTES.get(path)
        if path == "/" or path == "/dashboard.html":
            try:
                with open(BASE_DIR / "dashboard.html", "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self._cors()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception:
                self.send_error(404, "dashboard.html not found")
                return
        if not handler:
            self.send_error(404, "Not found")
            return
        try:
            data = handler()
            body = json.dumps(data, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, fmt, *args):
        pass  # silence access log


def main():
    print(f"[dashboard] starting on http://0.0.0.0:{PORT}")
    print(f"[dashboard] log file: {LOG_FILE}")
    print(f"[dashboard] state file: {STATE_FILE}")
    print(f"[dashboard] MT5 available: {_HAS_MT5}")
    _load_persisted_state()
    _backfill_closed_trades(max_trades=50)
    threading.Thread(target=tail_log, daemon=True).start()
    if _HAS_MT5:
        mt5_init_once()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
