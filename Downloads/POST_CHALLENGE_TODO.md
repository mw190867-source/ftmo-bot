# Post-Challenge Expansion TODO

Parked ideas to revisit **after passing FTMO challenge**. Do NOT touch during live challenge.

---

## In-Challenge Operational Fixes (low-risk, non-strategy)

These were noticed during the May 11 V3 deployment but don't affect trading logic. Safe to apply during the challenge if a clean opportunity (end-of-day flat, no open positions) arises.

### A. Duplicate log lines from `| tee` launch
- **Symptom:** every line in `ftmo_v1.log` appears twice
- **Cause:** the launch command `... | tee -a ~/Downloads/ftmo_v1.log` writes stdout to the file, but Python's logger is already writing the same lines to the same file
- **Fix:** drop the `| tee` from the launch command. The logger handles file output. Use:
  ```bash
  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 wine ~/.wine/drive_c/python/python.exe ~/Downloads/FTMO_V1.py
  ```
- **Cost of leaving it:** ~2× log volume, rotation triggers twice as fast. Not urgent.

### B. `challenge_start_equity` rebased on every startup
- **Symptom:** after each restart, `[FTMO] Challenge start equity: <today's open equity>` is logged. The FTMO progress % shown in the dashboard is therefore relative to today's open, not the true challenge start (~£35,000).
- **Cause:** `challenge_start_equity` is set from `daily_open_equity` at every fresh startup rather than persisted to disk.
- **Fix:** persist the FIRST observed value to a small JSON sidecar (e.g. `ftmo_challenge.json`) and read it back on startup if present.
- **Cost of leaving it:** dashboard `ftmo_pct` is misleadingly low. P&L history elsewhere (FTMO portal, MT5 history) is the source of truth, so this is purely a dashboard cosmetic.

### C. Bot watchdog / auto-restart on crash
- **Background:** May 8 silent crash went undetected for 3.5 days (entire weekend + Mon morning). No FTMO daily DD breach risk but lost trading opportunity.
- **Idea:** systemd service or a simple `while true; do <launch>; sleep 30; done` wrapper. Plus a Telegram alert if the bot is silent for >15 minutes.
- **Status:** discussed in earlier "improvement roadmap item #9". Not yet implemented.
- **Effort:** low. Wrap launch in a shell script with restart loop + heartbeat-age monitoring (dashboard already exposes `heartbeat_age_minutes`).

---

## Expansion Candidates (in EV order)

### 1. Add USDJPY
- Clean NY-session coverage
- SMC works well on JPY structure
- **Needs calibration:** `atr_threshold`, `sl_pips`, `tp_pips`, `trail_mult`, `spread_limit`, `pip_value`, `min_sl_distance`
- **Notes:** typical ATR ~10-15 pips M15; trail_mult 0.5 likely fits

### 2. Add AUDUSD
- Complements Asia session without correlating strongly with EUR/GBP
- Risk: thinner liquidity outside Asia
- **Needs calibration:** similar to EUR/GBP but thinner spreads in Asia session

### 3. Session-Specific Score Thresholds
- Different `score_min_*` per killzone
- E.g. NY_OPEN KZ on EUR/GBP: `score_min = base - 1` (looser during highest-edge window)
- Asia session: `score_min = base + 1` (tighter during low-conviction hours)
- Low-risk addition, easily reverted

### 4. Consider ETHUSD
- Only if you want a BTC-correlated diversifier
- Separate SYMBOL_CONFIG needed
- Wider spreads — may not clear net of costs

---

## Explicit DO-NOTs

- **No V2 in parallel.** Combine everything in ONE bot. Separate instances create ambiguous risk management and daily-drawdown tracking.
- **No exotic FX** (TRY, ZAR, MXN) — spreads eat alpha
- **No indices** (US30, NAS100) — different regime; strategy may not transfer
- **No crypto alts beyond ETH** — thin books, manipulation

---

## Investigation Items for V2

### BTC rr=7 block — liquidity target tuning
- On 2026-04-23 NY session, BTC showed `rr=7` cumulative blocks (7 signals rejected for insufficient RR)
- Hypothesis: `detect_liquidity_target` returning nearby pools → sub-1.5R TPs
- Action: add instrumentation to log the actual TP distance and RR when `rr` gate fires
- Potential fix: widen `lookback=40` or loosen cluster threshold (currently `np.std(highs) * 0.15`)
- If BTC structurally caps at score 4 due to no valid liquidity target found, risk-scaling tier 5+ never activates on BTC
- Code refs: `detect_liquidity_target` at FTMO_V1.py:958, called from lines 1308 and 1632

### DD baseline timing bug
- `reset_daily_trackers()` uses `date.today()` (local calendar), not FTMO broker's daily boundary (~22:00 UTC)
- On restart mid-day, `daily_open_equity` is set to equity at restart time, NOT to equity at broker day start
- Result: bot's DD tracking is offset from FTMO's actual DD measurement
- Fix: persist `daily_open_equity` to disk (see state persistence TODO already in the code comments around line 264); use broker timezone for daily boundary detection
- Code ref: FTMO_V1.py:536-553

## Safe During-Challenge Micro-Improvements (if needed)

These are reversible and don't alter core strategy:

- [ ] Weekend BTC cooldown (disable Fri 22:00 → Sun 22:00 UTC) — protects against thin-book manipulation
- [ ] Spread-aware entry: skip if spread > 1.5× normal
- [ ] Session-aware score thresholds (see #3 above, in low-risk form)

---

## Current Config Baseline (as of 2026-04-23 10:30 BST)

Preserve these known-good values as anchor points before any future expansion:

| Param | Value | Notes |
|-------|-------|-------|
| SYMBOLS | XAU, EUR, GBP, BTC | 4 symbols |
| RISK_PERCENT | 0.002 base | Dynamic scaling: 0.003@score5, 0.004@score6 |
| MAX_TOTAL_RISK_PERCENT | 0.015 | |
| MAX_TRADES_PER_DAY | 3 (live) | |
| trail_mult | XAU 0.5, EUR 0.5, GBP 0.5, BTC 0.7 | Widened 2026-04-23 |
| mgmt_be_lock_r | 1.0 all (was 0.5) | |
| Filter stack | Bug #1/2/3 + range penalty + zone invalidation | All active |

---

## Challenge Math Reference

At 50% WR / 2R avg reward / 0.2% base risk:
- EV/trade ≈ +0.1% (flat sizing) → +0.13% (with scaling)
- Need ~50-65 net trades to reach +5% target from current -1.46%
- At 2-4 trades/day: 2-3 weeks realistic

---

## FTMO Bot V4 Gold Strategy (Implemented May 11 2026)

A comprehensive regime-aware gold trading system designed specifically for XAUUSD's unique characteristics.

### Architecture

**Regime Classification (every 30 min)**
- Three regimes: `TRENDING`, `CORRECTIVE`, `COMPRESSION`
- Based on H4 ATR ratio, H1 EMA50 direction, H4 EMA20 oscillation frequency
- Default: `CORRECTIVE` (most gold conditions)

**Regime-Specific Parameters**
| Regime | BOS Lookback | SL (pips) | Min RR | Pullback | Sweep Req | Claude Gate |
|--------|--------------|-----------|--------|----------|-----------|-------------|
| TRENDING | 30 | 15 | 1.5 | Disabled | No | Shadow |
| CORRECTIVE | 20 | 20 | 2.0 | Enabled | Yes | Hard |
| COMPRESSION | — | — | — | — | — | None (blocked) |

**Sweep Prerequisite (CORRECTIVE only)**
- Liquidity sweep must precede BOS within 30 minutes
- Direction must align: `SWEEP_HIGH`→SELL, `SWEEP_LOW`→BUY
- Prevents entering against trapped liquidity

**Claude Hard Gate**
- Blocking call before order send (5s timeout)
- Conservative prompt: requires confidence ≥0.7 for approval
- Only in CORRECTIVE regime; trending uses shadow mode

**Economic Calendar Integration**
- Source: `ff_calendar_thisweek.json` (ForexFactory)
- 1-hour cache TTL
- Blocks XAUUSD 15 min before/after high-impact USD events
- Automatically adjusts for news volatility

### Files Modified
- `FTMO_V1.py`: Regime classification, sweep tracking, hard gate, economic calendar
- `dashboard_api.py`: `/api/regime` endpoint
- `dashboard.html`: Regime display card with color coding

### V4 vs V3 Comparison
- V3: Single XAUUSD config, shadow-only Claude, no sweep logic
- V4: Dynamic regime selection, hard/soft gates, sweep prerequisite, news awareness

---

## When To Revisit This File

- Immediately after passing challenge (funded account stage)
- If bot sits idle >1 week with zero trades (indicates filters may be too restrictive → different fix, don't expand)
- NEVER during active challenge drawdown

## V5.4 Changes for Challenge 3 (May 19 2026)
- USDJPY candle_body_mult_prime: 0.35 → 0.20 (was blocking 1640 entries)
- USDJPY candle_body_ratio_min: 0.50 → 0.45
- USDJPY candle_wick_max_mult: 0.85 → 1.00
- USDJPY entry_momentum_block_atr: 0.5 → 0.3
- PB_NEAR: 0.446 → 0.382 (restored original)
- PB_FAR: 0.554 → 0.618 (restored original)
- XAUUSD CORRECTIVE regime HTF mismatch exception added
- USDJPY scanner-assisted BUY exception (≥0.80 confidence)
- GBPJPY BE lock: 0.5R → 0.3R
- MAX_TRADES_PER_DAY: 3 → 6
- RISK_PERCENT: 0.003 → 0.005
- displacement added to meta_base (was always 0 — critical bug)
- XAUUSD 22xx UTC block exempted (Sydney session active for gold)
- XAUUSD Asia block tightened 00:00-05:00 → 00:00-01:00 UTC (Tokyo gold session now open)
- GBPJPY sl_pips: 200→50, tp_pips: 350→100 (was 8x wider than GBPUSD in % terms)
- Claude SL context: JPY pairs now show points not pips (pip_value≥0.01 check)
- USDJPY sl_pips: 150→80, tp_pips: 225→160 (1.5 point SL was excessive)
- dashboard_api.py: today's date filtering switched from UTC to London timezone
- FTMO_V1.py: reconstructed trade PnL clamped to 0 if outside [-500, +1000] bounds
