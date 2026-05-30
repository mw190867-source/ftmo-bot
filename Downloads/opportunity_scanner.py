import time, json, threading, logging, urllib.request
from collections import Counter
from datetime import datetime
import pytz

logger = logging.getLogger("SMC_Hybrid_PRO")

_symbols, _api_key, _tg_send, _get_htf = [], "", None, None
_recent_gate_hits, _recent_losses, _pending_pulls = {}, {}, {}
_get_regime, _get_calendar = None, None
opportunity_alerts, _last_opportunity_alert = {}, {}
_last_scan_time, SCAN_INTERVAL = 0.0, 300


def init_scanner(symbols, api_key, tg_send, get_htf, gate_hits, losses, pending, get_regime, get_calendar):
    global _symbols, _api_key, _tg_send, _get_htf
    global _recent_gate_hits, _recent_losses, _pending_pulls, _get_regime, _get_calendar
    _symbols, _api_key, _tg_send, _get_htf = symbols, api_key, tg_send, get_htf
    _recent_gate_hits, _recent_losses, _pending_pulls = gate_hits, losses, pending
    _get_regime, _get_calendar = get_regime, get_calendar
    logger.info("[OPPORTUNITY_SCANNER] Initialised | symbols=%s", symbols)


def _build_context():
    now = time.time()
    ctx = {
        "utc": datetime.utcnow().replace(tzinfo=pytz.UTC).isoformat(timespec="seconds"),
        "gold_regime": _get_regime() if _get_regime else "UNKNOWN",
        "calendar_events": (_get_calendar() or [])[:3],
        "symbols": {},
    }
    for s in _symbols:
        try:
            htf = _get_htf(s)
        except Exception:
            htf = None
        gates = Counter(r for ts, r in _recent_gate_hits.get(s, []) if ts >= now - 7200).most_common(3)
        losses = []
        for entry in _recent_losses.get(s, []):
            try:
                ts = entry[0]
                if ts >= now - 14400:
                    losses.append({"mins_ago": round((now - ts) / 60)})
            except Exception:
                continue
        htf_val = htf.value if hasattr(htf, "value") else (htf if htf else "UNKNOWN")
        ctx["symbols"][s] = {
            "htf": htf_val,
            "armed_zone": s in _pending_pulls,
            "top_gates_2h": gates,
            "recent_losses_4h": losses,
        }
    return ctx


SCANNER_PROMPT = (
    "You are an elite forex opportunity scanner for a live SMC trading bot.\n\n"
    "Analyse market context across XAUUSD, EURUSD, GBPUSD, USDJPY and identify the "
    "highest-probability setups RIGHT NOW.\n\n"
    "Principles:\n"
    "1. Cross-symbol narrative matters\n"
    "2. Avoid symbols with recent loss clusters\n"
    "3. HTF=UNKNOWN means no trade\n"
    "4. Diversity over stacking\n\n"
    "Respond ONLY with valid JSON, no markdown:\n"
    '{"narrative": "one sentence", "top_opportunity": "SYMBOL or null", '
    '"opportunities": [{"symbol": "GBPUSD", "direction": "SELL", "confidence": 0.85, '
    '"thesis": "why edge", "trigger": "entry condition", "caution": "invalidation", '
    '"window_minutes": 30, "priority": 1}], '
    '"avoid": [{"symbol": "XAUUSD", "reason": "why"}]}\n\n'
    "Only include opportunities with confidence >= 0.65."
)


def _call_claude(ctx):
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 400,
        "system": SCANNER_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(ctx, default=str)}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": _api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=20)
    body = json.loads(resp.read().decode("utf-8"))
    text = body["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    text = text.replace('’', "'").replace('‘', "'")
    text = text.replace('“', '"').replace('”', '"')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        raise


def run_opportunity_scan():
    global _last_scan_time
    if not _api_key:
        return
    now = time.time()
    if now - _last_scan_time < SCAN_INTERVAL:
        return
    _last_scan_time = now

    def _execute():
        try:
            ctx = _build_context()
            data = _call_claude(ctx)
        except Exception as e:
            logger.warning("[OPPORTUNITY_SCAN] Failed: %s", e)
            return
        opportunity_alerts.clear()
        opps = data.get("opportunities", [])
        for opp in opps:
            sym = opp.get("symbol")
            if not sym:
                continue
            opp["ts"] = time.time()
            opportunity_alerts[sym] = opp
            conf = float(opp.get("confidence", 0))
            if (conf >= 0.80
                    and time.time() - _last_opportunity_alert.get(sym, 0) > 1800
                    and _tg_send):
                try:
                    _tg_send(
                        f"OPPORTUNITY {sym} {opp.get('direction', '?')}\n"
                        f"{opp.get('thesis', '')}\n"
                        f"Trigger: {opp.get('trigger', '')}\n"
                        f"Confidence: {conf:.0%} | Window: {opp.get('window_minutes', '?')}min"
                    )
                except Exception:
                    pass
                _last_opportunity_alert[sym] = time.time()
        _persist_alerts()
        narrative = data.get("narrative", "")
        avoid_list = [a.get("symbol") for a in data.get("avoid", [])]
        logger.info(
            "[OPPORTUNITY_SCAN] %d alerts | top=%s | avoid=%s | narrative=%s",
            len(opps), data.get("top_opportunity", "none"), avoid_list, narrative[:120],
        )

    threading.Thread(target=_execute, daemon=True).start()


def get_opportunity_score_bonus(symbol, direction):
    alert = opportunity_alerts.get(symbol)
    if not alert:
        return 0
    if time.time() - alert.get("ts", 0) > 1800:
        return 0
    if float(alert.get("confidence", 0)) < 0.75:
        return 0
    return 1 if alert.get("direction", "").upper() == direction.upper() else 0


def get_opportunities_for_dashboard():
    return {
        sym: {
            "direction": a.get("direction"),
            "confidence": a.get("confidence"),
            "thesis": a.get("thesis"),
            "trigger": a.get("trigger"),
            "caution": a.get("caution"),
            "window_minutes": a.get("window_minutes"),
            "priority": a.get("priority"),
            "age_minutes": round((time.time() - a.get("ts", time.time())) / 60, 1),
        }
        for sym, a in opportunity_alerts.items()
    }


# Stage 4: file-based bridge for dashboard (separate process)
import os
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opportunity_alerts.json")

def _persist_alerts():
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(get_opportunities_for_dashboard(), f)
    except Exception as e:
        logger.debug("[OPPORTUNITY_SCAN] persist failed: %s", e)
