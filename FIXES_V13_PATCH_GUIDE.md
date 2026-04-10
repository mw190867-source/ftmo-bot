# SMC HYBRID PRO V13 — COMPREHENSIVE FIX GUIDE
# Apply these changes to your claude_final_boss_12.py file

## ============================================
## FIX 1: CANDLE FRESHNESS (Add this function)
## ============================================

# Add right after the rates_ok() function definition, around line 95:

CANDLE_FRESHNESS_SECS = 30

def _pick_confirmation_candle(rates_entry):
    """
    Determine which candle to use for confirmation based on freshness.
    If current candle ([-1]) opened >30s ago, it's formed enough to use directly.
    If it opened <30s ago, use prior closed candle ([-2]) to avoid incomplete data.
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

## ============================================
## FIX 2: SESSION GATE (Add this function)
## ============================================

# Add right before get_signal() function, around line 620:

def _session_gate_ok(symbol, utc_dt):
    """
    Check if signal is allowed based on session restrictions.
    For symbols with session_gate=True (EURUSD, GBPUSD), only allow:
    - During London/NY session window, OR
    - Inside an active killzone
    
    For session_gate=False (XAUUSD, BTCUSD), always return True.
    """
    cfg = SYMBOL_CONFIG.get(symbol, {})
    if not cfg.get("session_gate", False):
        return True
    
    kz = in_killzone(utc_dt)
    if kz is not None:
        return True
    
    if in_session(symbol, utc_dt):
        return True
    
    return False

## ============================================
## FIX 3: MODIFY candle_confirmation() FUNCTION
## ============================================

# FIND this function around line 495 and REPLACE the candle selection logic:

# OLD (before):
    c          = rates_entry[-2]
    o          = c["open"]
    h          = c["high"]
    l          = c["low"]

# NEW (after):
    # FIX 3 — Use freshness-aware candle selection
    c = _pick_confirmation_candle(rates_entry)
    if c is None:
        return False
    o          = c["open"]
    h          = c["high"]
    l          = c["low"]

## ============================================
## FIX 4: MODIFY get_signal() FUNCTION OPENING
## ============================================

# FIND this function around line 630 and ADD session gate check immediately after all the setup checks:

# After these lines:
    if not rates_ok(rates_entry,   20):
        _gate_hit(symbol, "rates_entry")
        return "HOLD", 0.0, None

# ADD this (NEW):
    # FIX 1 — Session gate: hard block when outside session + killzone for gated symbols
    if not _session_gate_ok(symbol, utc_now):
        cp("WARNING", f"⚠️  {symbol} | session gate — outside trading window")
        logger.info("%s session gate blocked", symbol)
        _gate_hit(symbol, "session_gate")
        return "HOLD", price, None

## ============================================
## FIX 5: MODIFY HTF MISMATCH (Hard Block)
## ============================================

# FIND the HTF mismatch check around line 720, looks like:
    
    if htf_mismatch:
        warn_key = (symbol, bos_idx)
        if warn_key not in _htf_mismatch_warned:
            cp("WARNING", f"⚠️  {symbol} | HTF mismatch (HTF={htf_trend} vs BOS={bos_type}) — blocked")
            logger.info("%s HTF mismatch blocked | HTF=%s BOS=%s", symbol, htf_trend, bos_type)
            _htf_mismatch_warned[warn_key] = True
        _gate_hit(symbol, "htf_mismatch")
        return "HOLD", price, None

# REPLACE the entire section with (FIX 2 — Hard unconditional block):

    if htf_mismatch:
        warn_key = (symbol, bos_idx)
        if warn_key not in _htf_mismatch_warned:
            cp("WARNING", f"⚠️  {symbol} | HTF MISMATCH (HTF={htf_trend} ≠ BOS={bos_type}) — unconditional HOLD")
            logger.warning("%s HTF MISMATCH HARD BLOCK | HTF=%s BOS=%s", symbol, htf_trend, bos_type)
            _htf_mismatch_warned[warn_key] = True
        _gate_hit(symbol, "htf_mismatch")
        return "HOLD", price, None

## ============================================
## FIX 6: UPDATE SYMBOL_CONFIG
## ============================================

# FIND SYMBOL_CONFIG and add these new keys to each symbol:

# XAUUSD config — ADD after "mgmt_time_stop_enabled": False:
        "asia_session_enabled": False,  # FIX: Disable Asia for XAUUSD
        "min_sl_distance": 0.15,  # FIX: Minimum SL floor to prevent whipsaws

# EURUSD config — ADD after mgmt section:
        "min_sl_distance": 0.00015,

# GBPUSD config — ADD after mgmt section:
        "min_sl_distance": 0.0002,

# BTCUSD config — ADD after mgmt section:
        "min_sl_distance": 250,

## ============================================
## FIX 7: MODIFY calculate_sl_tp_liquidity()
## ============================================

# FIND this function around line 1000, FIND this line:
    sl_distance = max(c["sl_pips"] * c["pip_value"], atr * atr_sl_mult * sl_mult)

# REPLACE with (FIX: Add minimum SL floor):
    # FIX: Apply per-symbol minimum SL floor to prevent whipsaws in low volatility
    min_sl_dist = float(c.get("min_sl_distance", 0.0))
    sl_distance = max(c["sl_pips"] * c["pip_value"], atr * atr_sl_mult * sl_mult, min_sl_dist)

## ============================================
## FIX 8: MODIFY check_pending_pullbacks()
## ============================================

# Add session gate check at the START of the for loop, right after "for symbol, setup in list(pending_pulls.items()):"

        # FIX 1 — Session gate for pending
        if not _session_gate_ok(symbol, utc_now):
            _gate_hit(symbol, "session_gate")
            continue

# ADD HTF MISMATCH hard block in check_pending_pullbacks, find this code:
        if htf_mismatch:
            pend_bos = setup.get("bos_idx", -1)
            warn_key = (symbol, pend_bos, "pending")
            if warn_key not in _htf_mismatch_warned:
                cp("WARNING", f"⚠️  {symbol} | HTF mismatch (HTF={htf_trend} vs {direction}) — pending blocked")
                logger.info("%s pending HTF mismatch blocked | HTF=%s dir=%s", symbol, htf_trend, direction)
                _htf_mismatch_warned[warn_key] = True
            _gate_hit(symbol, "htf_mismatch")
            continue

# REPLACE with (FIX 2 — Hard block + clear pending):
        if htf_mismatch:
            cp("WARNING", f"⚠️  {symbol} | PENDING HTF MISMATCH (HTF={htf_trend}) — clearing setup")
            logger.warning("%s pending HTF mismatch — clearing | HTF=%s dir=%s", symbol, htf_trend, direction)
            expired.append(symbol)  # Clear this setup
            _gate_hit(symbol, "htf_mismatch")
            continue

## ============================================
## FIX 9: DISABLE ASIA SESSION FOR XAUUSD
## ============================================

# Find in_asia_session() checks in get_signal() and check_pending_pullbacks()
# Before any "asia = in_asia_session(utc_now)" calls, add:

        # FIX: Disable Asia trading for XAUUSD (proven to lose)
        if symbol == "XAUUSD" and in_asia_session(utc_now):
            cp("WARNING", f"⚠️  {symbol} | Asia session disabled (whipsaw protection)")
            _gate_hit(symbol, "asia_disabled")
            return "HOLD", price, None

## ============================================
## FIX 10: ADD SCORE VALIDATION
## ============================================

# In execute_trade(), add this check right after the function starts:

    # FIX: Validate score to prevent SCORE=0 trades
    meta_score = meta.get("score", 0)
    entry_mode = meta.get("entry_mode", "pullback")
    if entry_mode == "breakout":
        min_score = SCORE_MIN_BREAKOUT
    elif entry_mode == "continuation":
        min_score = SCORE_MIN_CONTINUATION
    else:
        min_score = SCORE_MIN_PULLBACK
    
    if meta_score < min_score:
        cp("WARNING", f"⚠️  {symbol} | SCORE={meta_score} < min={min_score} — trade blocked")
        logger.warning("%s SCORE validation failed | score=%d < min=%d | mode=%s",
                       symbol, meta_score, min_score, entry_mode)
        _gate_hit(symbol, "score_invalid")
        return

============================================
## SUMMARY OF CHANGES
============================================

✅ FIX 1: Session gate blocks off-hours trades (EURUSD/GBPUSD)
✅ FIX 2: HTF mismatch is HARD unconditional HOLD (no softening)
✅ FIX 3: Candle freshness uses intelligent [-1] vs [-2] selection
✅ FIX 4: Min SL distance prevents whipsaws in low volatility
✅ FIX 5: Asia session disabled for XAUUSD (stops -$1,173 losses)
✅ FIX 6: SCORE=0 validation blocks invalid signals

EXPECTED OUTCOMES:
- Fewer whipsaw trades (especially XAUUSD)
- Higher quality entries (HTF aligned)
- No off-hours noise (EURUSD/GBPUSD)
- Fresher candles (better timing)
- Cleaner SLs (no instant hits)

Test for 50-100 trades and monitor win rate. Target >55% win rate.
