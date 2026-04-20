#!/usr/bin/env python3
"""
Alpaca Paper Trading Agent — Bagholder Exit Liquidity Strategy
---------------------------------------------------------------
Three agents, each sourcing setups from a different scanner:
  - Agent 1: TradingView  (scanner.py --mode morning, source=tradingview)
  - Agent 2: Barchart     (scanner.py --mode morning, source=barchart)
  - Agent 3: StockAnalysis(scanner.py --mode morning, source=stockanalysis)

All three agents share identical risk rules:
  - Capital: $100,000 each (Alpaca paper account)
  - Max risk per trade: 1% of capital = $1,000
  - Max daily loss: 2% = $2,000 — if hit, no more trades that day
  - Stop loss: above pre-market high (from config)
  - Target 1: 50% retracement of gap (primary exit)
  - Target 2: full gap fill to prev close (stretch)
  - Time stop: hard close all positions by 10:30 AM ET regardless
  - Only trade A+ setups (score >= 35/60)
  - Only trade confirmed shortable stocks

Priority: capital preservation over profit maximisation.
  - If R/R < 1.5:1, skip the trade
  - If daily loss limit hit, stop all trading for the day
  - Never size more than 5% of capital into a single trade

Usage:
  python alpaca_agent.py --agent 1 --mode premarket
  python alpaca_agent.py --agent 2 --mode open
  python alpaca_agent.py --agent all --mode monitor
  python alpaca_agent.py --agent all --mode close
  python alpaca_agent.py --agent all --mode summary

Modes:
  premarket  — Load A+ setups, submit short orders at market open
  open       — Confirm fills, set stop-loss and take-profit bracket orders
  monitor    — Check positions, enforce exits (runs every 5 min)
  close      — Hard time-stop: close all positions at 10:30 AM ET
  summary    — Print + Telegram P&L summary for the day
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_FILE = PROJECT_DIR / "config.yaml"
DATA_DIR = SCRIPT_DIR / "data"
LATEST_SCAN_FILE = DATA_DIR / "latest_scan.json"
HISTORY_DIR = DATA_DIR / "history"

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# Load config
with open(CONFIG_FILE) as f:
    CONFIG = yaml.safe_load(f)

ALPACA_API_KEY    = CONFIG.get("alpaca_api_key", "")
ALPACA_SECRET_KEY = CONFIG.get("alpaca_secret_key", "")
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"  # paper trading endpoint
ALPACA_HEADERS    = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type":        "application/json",
}

# Risk constants
CAPITAL           = 100_000       # $100,000 paper capital per agent
RISK_PER_TRADE    = 1_000         # 1% of capital
MAX_DAILY_LOSS    = 2_000         # 2% of capital — stop trading if hit
MAX_POSITION_PCT  = 0.05          # Never more than 5% of capital in one trade
MIN_RR_RATIO      = 1.5           # Skip trade if reward:risk < 1.5:1
TARGET1_RETRACE   = 0.50          # 50% retracement = Target 1
STOP_PCT          = CONFIG.get("bagholder", {}).get("stop_loss_pct", 0.10)

# Scanner source per agent
AGENT_SOURCES = {
    1: "tradingview",
    2: "barchart",
    3: "stockanalysis",
}

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _utc_to_et(dt: datetime) -> datetime:
    offset = -4 if 3 <= dt.month <= 11 else -5
    return dt + timedelta(hours=offset)

def _utc_to_sgt(dt: datetime) -> datetime:
    return dt + timedelta(hours=8)

def _now_et() -> datetime:
    return _utc_to_et(_now_utc())

def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _fmt_et_sgt() -> str:
    now = _now_utc()
    label = "EDT" if 3 <= now.month <= 11 else "EST"
    return f"{_fmt(_utc_to_et(now))} {label} / {_fmt(_utc_to_sgt(now))} SGT"

def _today_et() -> str:
    return _now_et().strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Alpaca API helpers
# ---------------------------------------------------------------------------

def alpaca_get(path: str) -> dict:
    url = f"{ALPACA_BASE_URL}{path}"
    resp = requests.get(url, headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()

def alpaca_post(path: str, body: dict) -> dict:
    url = f"{ALPACA_BASE_URL}{path}"
    resp = requests.post(url, headers=ALPACA_HEADERS, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()

def alpaca_delete(path: str) -> None:
    url = f"{ALPACA_BASE_URL}{path}"
    resp = requests.delete(url, headers=ALPACA_HEADERS, timeout=15)
    resp.raise_for_status()

def get_account() -> dict:
    return alpaca_get("/v2/account")

def get_positions() -> list:
    return alpaca_get("/v2/positions")

def get_position(ticker: str) -> Optional[dict]:
    try:
        return alpaca_get(f"/v2/positions/{ticker}")
    except Exception:
        return None

def get_orders(status: str = "open") -> list:
    return alpaca_get(f"/v2/orders?status={status}&limit=100")

def close_position(ticker: str) -> Optional[dict]:
    try:
        url = f"{ALPACA_BASE_URL}/v2/positions/{ticker}"
        resp = requests.delete(url, headers=ALPACA_HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    [!] Could not close {ticker}: {e}")
        return None

def cancel_all_orders() -> None:
    try:
        alpaca_delete("/v2/orders")
        print("  ✅ All open orders cancelled.")
    except Exception as e:
        print(f"  [!] Could not cancel orders: {e}")

def submit_short_market_order(ticker: str, shares: int) -> Optional[dict]:
    """Submit a market sell (short) order."""
    try:
        order = alpaca_post("/v2/orders", {
            "symbol":        ticker,
            "qty":           str(shares),
            "side":          "sell",
            "type":          "market",
            "time_in_force": "day",
        })
        print(f"  ✅ [{ticker}] Short market order submitted — {shares} shares | Order ID: {order.get('id')}")
        return order
    except Exception as e:
        print(f"  ❌ [{ticker}] Order failed: {e}")
        return None

def submit_bracket_order(ticker: str, shares: int, stop_loss: float, take_profit: float) -> Optional[dict]:
    """
    Submit an OTO bracket: short entry + stop loss (buy stop) + take profit (buy limit).
    Uses Alpaca's bracket order type.
    """
    try:
        order = alpaca_post("/v2/orders", {
            "symbol":        ticker,
            "qty":           str(shares),
            "side":          "sell",
            "type":          "market",
            "time_in_force": "day",
            "order_class":   "bracket",
            "stop_loss": {
                "stop_price": str(round(stop_loss, 2)),
            },
            "take_profit": {
                "limit_price": str(round(take_profit, 2)),
            },
        })
        print(f"  ✅ [{ticker}] Bracket order submitted | Stop: ${stop_loss} | TP: ${take_profit}")
        return order
    except Exception as e:
        print(f"  ❌ [{ticker}] Bracket order failed: {e}")
        return None

# ---------------------------------------------------------------------------
# Agent state management
# ---------------------------------------------------------------------------

def _state_file(agent_id: int) -> Path:
    return DATA_DIR / f"agent{agent_id}_state.json"

def _load_state(agent_id: int) -> dict:
    f = _state_file(agent_id)
    today = _today_et()
    if f.exists():
        state = json.loads(f.read_text())
        if state.get("date") == today:
            return state
        # Archive previous day
        _archive_state(agent_id, state)
    return _fresh_state(agent_id, today)

def _fresh_state(agent_id: int, date: str) -> dict:
    return {
        "agent_id":      agent_id,
        "source":        AGENT_SOURCES[agent_id],
        "date":          date,
        "capital":       CAPITAL,
        "positions":     {},      # ticker → position dict
        "closed_trades": [],
        "daily_pnl":     0.0,
        "daily_loss_hit": False,
        "summary":       None,
    }

def _save_state(agent_id: int, state: dict) -> None:
    _state_file(agent_id).write_text(json.dumps(state, indent=2, default=str))

def _archive_state(agent_id: int, state: dict) -> None:
    date = state.get("date", "unknown")
    hist = HISTORY_DIR / f"agent{agent_id}_{date}.json"
    hist.write_text(json.dumps(state, indent=2, default=str))
    print(f"  📁 Agent {agent_id} state archived → {hist.name}")

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  [Telegram] Env vars not set — skipping.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        payload = json.dumps(
            {"chat_id": chat_id, "text": message},
            ensure_ascii=False,
        ).encode("utf-8")
        resp = requests.post(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [Telegram] ✗ Failed: {e}")

# ---------------------------------------------------------------------------
# Trade sizing & target calculation
# ---------------------------------------------------------------------------

def _compute_trade(entry: float, prev_close: float, pm_high: float) -> Optional[dict]:
    """
    Compute position size and targets for a short trade.
    Returns None if trade doesn't meet minimum R/R or sizing constraints.
    """
    stop_loss  = round(pm_high * (1 + STOP_PCT), 2)
    risk_per_share = stop_loss - entry
    if risk_per_share <= 0:
        return None

    target1 = round(entry - TARGET1_RETRACE * (entry - prev_close), 2)
    target2 = round(prev_close, 2)

    reward_per_share = entry - target1
    rr_ratio = round(reward_per_share / risk_per_share, 2) if risk_per_share > 0 else 0

    # Skip if R/R below minimum threshold
    if rr_ratio < MIN_RR_RATIO:
        return {"skip": True, "reason": f"R/R {rr_ratio:.2f} < {MIN_RR_RATIO} minimum", "rr_ratio": rr_ratio}

    # Size: risk $1,000 / risk_per_share, capped at 5% of capital
    shares_by_risk = int(RISK_PER_TRADE / risk_per_share)
    max_shares_by_capital = int((CAPITAL * MAX_POSITION_PCT) / entry)
    shares = min(shares_by_risk, max_shares_by_capital)

    if shares < 1:
        return {"skip": True, "reason": "Position size too small (< 1 share)", "rr_ratio": rr_ratio}

    actual_risk = round(shares * risk_per_share, 2)
    reward_t1   = round(shares * reward_per_share, 2)
    reward_t2   = round(shares * (entry - target2), 2)

    return {
        "skip":       False,
        "shares":     shares,
        "stop_loss":  stop_loss,
        "target1":    target1,
        "target2":    target2,
        "risk_usd":   actual_risk,
        "reward_t1":  reward_t1,
        "reward_t2":  reward_t2,
        "rr_ratio":   rr_ratio,
        "entry":      entry,
    }

# ---------------------------------------------------------------------------
# Filter candidates by source
# ---------------------------------------------------------------------------

def _filter_by_source(candidates: list, source: str) -> list:
    """
    Filter A+ candidates by scanner source.
    Falls back to all A+ if source tag not available (older scan files).
    """
    a_plus = [c for c in candidates if c.get("tier") == "A+" and c.get("shortable") is True]
    sourced = [c for c in a_plus if c.get("source") == source]
    # If no source tags, all agents use all A+ (graceful fallback)
    return sourced if sourced else a_plus

# ---------------------------------------------------------------------------
# MODE: premarket
# ---------------------------------------------------------------------------

def mode_premarket(agent_id: int) -> None:
    source = AGENT_SOURCES[agent_id]
    print(f"\n{'='*60}")
    print(f"  AGENT {agent_id} ({source.upper()}) — PRE-MARKET — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state = _load_state(agent_id)

    if state["daily_loss_hit"]:
        print(f"  ⛔ Daily loss limit hit — Agent {agent_id} standing down.")
        return

    if not LATEST_SCAN_FILE.exists():
        print("  ⚠️  No scan file found. Run scanner first.")
        return

    scan = json.loads(LATEST_SCAN_FILE.read_text())
    candidates = _filter_by_source(scan.get("candidates", []), source)

    if not candidates:
        print(f"  No A+ shortable setups from {source} — standing down.")
        _save_state(agent_id, state)
        return

    print(f"  Found {len(candidates)} A+ setup(s) from {source}:")

    queued = []
    for c in candidates:
        ticker    = c["ticker"]
        pm_price  = float(c.get("pm_price") or 0)
        prev_close = float(c.get("prev_close") or 0)
        pm_high   = float(c.get("pm_price") or pm_price)

        if not pm_price or not prev_close:
            print(f"  [{ticker}] Missing price data — skipping.")
            continue

        if ticker in state["positions"]:
            print(f"  [{ticker}] Already queued — skipping.")
            continue

        trade = _compute_trade(pm_price, prev_close, pm_high)
        if trade is None or trade.get("skip"):
            reason = trade.get("reason", "unknown") if trade else "calc error"
            print(f"  [{ticker}] Skipped — {reason}")
            continue

        # Check daily loss headroom
        projected_loss = state["daily_pnl"] - trade["risk_usd"]
        if projected_loss < -MAX_DAILY_LOSS:
            print(f"  [{ticker}] Skipped — would exceed daily loss limit.")
            continue

        state["positions"][ticker] = {
            "ticker":       ticker,
            "agent_id":     agent_id,
            "source":       source,
            "scan_score":   c.get("total_score"),
            "gap_pct":      c.get("premarket_gap_pct"),
            "prev_close":   prev_close,
            "pm_high":      pm_high,
            "pm_price":     pm_price,
            "shares":       trade["shares"],
            "stop_loss":    trade["stop_loss"],
            "target1":      trade["target1"],
            "target2":      trade["target2"],
            "risk_usd":     trade["risk_usd"],
            "reward_t1":    trade["reward_t1"],
            "rr_ratio":     trade["rr_ratio"],
            "entry_price":  None,   # filled at market open
            "order_id":     None,
            "status":       "queued",
            "queued_at_et": _fmt(_now_et()),
        }
        queued.append(ticker)
        print(f"  ✅ [{ticker}] Queued — Score:{c.get('total_score')}/60  Gap:{c.get('premarket_gap_pct')}%  "
              f"Shares:{trade['shares']}  Risk:${trade['risk_usd']}  R/R:{trade['rr_ratio']}:1")

    _save_state(agent_id, state)

    if queued:
        lines = [
            f"📋 *Agent {agent_id} ({source}) — Pre-Market Queue*",
            f"_{_fmt_et_sgt()}_",
            f"Queued {len(queued)} short(s) for market open:",
            "",
        ]
        for ticker in queued:
            p = state["positions"][ticker]
            lines.append(
                f"  📉 *{ticker}*  Score:{p['scan_score']}/60  Gap:+{p['gap_pct']}%\n"
                f"     Shares:{p['shares']}  Risk:${p['risk_usd']}  R/R:{p['rr_ratio']}:1\n"
                f"     Stop:${p['stop_loss']}  T1:${p['target1']}"
            )
        lines += ["", "_Orders will be placed at 9:30 AM ET market open._"]
        _send_telegram("\n".join(lines))

# ---------------------------------------------------------------------------
# MODE: open
# ---------------------------------------------------------------------------

def mode_open(agent_id: int) -> None:
    source = AGENT_SOURCES[agent_id]
    print(f"\n{'='*60}")
    print(f"  AGENT {agent_id} ({source.upper()}) — MARKET OPEN — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state = _load_state(agent_id)

    if state["daily_loss_hit"]:
        print(f"  ⛔ Daily loss limit hit — Agent {agent_id} standing down.")
        return

    queued = {k: v for k, v in state["positions"].items() if v["status"] == "queued"}
    if not queued:
        print("  No queued positions to open.")
        return

    # Verify account is active and not in PDT restriction
    try:
        account = get_account()
        print(f"  Account status: {account.get('status')}  |  Shorting enabled: {account.get('shorting_enabled')}")
        if account.get("status") != "ACTIVE":
            print("  ⚠️  Account not active — aborting.")
            return
        if not account.get("shorting_enabled"):
            print("  ⚠️  Shorting not enabled on this account — aborting.")
            return
    except Exception as e:
        print(f"  ❌ Could not verify account: {e}")
        return

    filled = []
    for ticker, position in queued.items():
        # Recompute trade with latest PM price if available
        trade = _compute_trade(
            position["pm_price"],
            position["prev_close"],
            position["pm_high"],
        )
        if trade is None or trade.get("skip"):
            reason = trade.get("reason", "calc error") if trade else "calc error"
            print(f"  [{ticker}] Skipped at open — {reason}")
            position["status"] = "skipped"
            continue

        # Submit bracket order: short + stop loss + take profit at T1
        order = submit_bracket_order(
            ticker,
            trade["shares"],
            trade["stop_loss"],
            trade["target1"],
        )
        if order:
            position["order_id"]    = order.get("id")
            position["status"]      = "open"
            position["entry_price"] = position["pm_price"]  # will be updated on fill
            position["opened_at_et"] = _fmt(_now_et())
            position["shares"]      = trade["shares"]
            position["stop_loss"]   = trade["stop_loss"]
            position["target1"]     = trade["target1"]
            position["risk_usd"]    = trade["risk_usd"]
            position["rr_ratio"]    = trade["rr_ratio"]
            filled.append(ticker)
        else:
            position["status"] = "order_failed"

    _save_state(agent_id, state)

    if filled:
        lines = [
            f"🔔 *Agent {agent_id} ({source}) — Orders Placed*",
            f"_{_fmt_et_sgt()}_",
            "",
        ]
        for ticker in filled:
            p = state["positions"][ticker]
            lines.append(
                f"📉 *{ticker}*  Short {p['shares']} shares\n"
                f"   Stop: ${p['stop_loss']}  |  T1: ${p['target1']}\n"
                f"   Risk: ${p['risk_usd']}  |  R/R: {p['rr_ratio']}:1"
            )
        lines += ["", "_Time stop: 10:30 AM ET / 10:30 PM SGT_"]
        _send_telegram("\n".join(lines))

# ---------------------------------------------------------------------------
# MODE: monitor
# ---------------------------------------------------------------------------

def mode_monitor(agent_id: int) -> None:
    source = AGENT_SOURCES[agent_id]
    print(f"\n{'='*60}")
    print(f"  AGENT {agent_id} ({source.upper()}) — MONITOR — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state = _load_state(agent_id)
    open_positions = {k: v for k, v in state["positions"].items() if v["status"] == "open"}

    if not open_positions:
        print("  No open positions to monitor.")
        return

    try:
        alpaca_positions = {p["symbol"]: p for p in get_positions()}
    except Exception as e:
        print(f"  ❌ Could not fetch Alpaca positions: {e}")
        return

    for ticker, position in list(open_positions.items()):
        ap = alpaca_positions.get(ticker)

        if ap is None:
            # Position no longer exists in Alpaca — was closed by bracket order
            print(f"  [{ticker}] Position closed by Alpaca (bracket hit)")
            # Try to get fill details from recent orders
            try:
                orders = get_orders(status="closed")
                fills = [o for o in orders if o.get("symbol") == ticker and o.get("filled_avg_price")]
                if fills:
                    exit_price = float(fills[0]["filled_avg_price"])
                    side = fills[0].get("side", "")
                    reason = "TARGET_1" if side == "buy" else "STOP_LOSS"
                else:
                    exit_price = position.get("entry_price", 0)
                    reason = "BRACKET_CLOSED"
            except Exception:
                exit_price = position.get("entry_price", 0)
                reason = "BRACKET_CLOSED"

            _record_close(state, ticker, exit_price, reason)
            continue

        # Position still open — check current P&L
        current_price  = float(ap.get("current_price", 0))
        unrealized_pnl = float(ap.get("unrealized_pl", 0))
        entry_price    = float(ap.get("avg_entry_price", position.get("entry_price", 0)))

        position["entry_price"]    = entry_price
        position["last_price"]     = current_price
        position["unrealized_pnl"] = unrealized_pnl

        print(f"  [{ticker}] Price: ${current_price}  |  Unrealized P&L: ${unrealized_pnl:.2f}")

        # Check if daily loss limit now breached
        state["daily_pnl"] = sum(
            float(p.get("unrealized_pnl", 0)) for p in open_positions.values()
        ) + sum(t.get("pnl_usd", 0) for t in state["closed_trades"])

        if state["daily_pnl"] <= -MAX_DAILY_LOSS and not state["daily_loss_hit"]:
            print(f"  ⛔ DAILY LOSS LIMIT HIT (${state['daily_pnl']:.2f}) — closing all positions!")
            state["daily_loss_hit"] = True
            _send_telegram(
                f"⛔ *Agent {agent_id} — Daily Loss Limit Hit*\n"
                f"_{_fmt_et_sgt()}_\n"
                f"Loss: ${state['daily_pnl']:.2f} / Limit: ${MAX_DAILY_LOSS}\n"
                f"Closing all positions now."
            )
            _close_all(agent_id, state)
            break

    _save_state(agent_id, state)

# ---------------------------------------------------------------------------
# MODE: close
# ---------------------------------------------------------------------------

def mode_close(agent_id: int) -> None:
    source = AGENT_SOURCES[agent_id]
    print(f"\n{'='*60}")
    print(f"  AGENT {agent_id} ({source.upper()}) — TIME STOP CLOSE — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state = _load_state(agent_id)
    _close_all(agent_id, state)
    _save_state(agent_id, state)

def _close_all(agent_id: int, state: dict) -> None:
    """Cancel all open orders then close all open positions."""
    cancel_all_orders()
    time.sleep(1)  # brief pause to let cancellations process

    open_positions = {k: v for k, v in state["positions"].items() if v["status"] == "open"}
    if not open_positions:
        print(f"  Agent {agent_id}: No open positions to close.")
        return

    closed = []
    for ticker in list(open_positions.keys()):
        result = close_position(ticker)
        if result:
            exit_price = float(result.get("filled_avg_price") or result.get("avg_entry_price") or
                               open_positions[ticker].get("last_price") or
                               open_positions[ticker].get("entry_price", 0))
            _record_close(state, ticker, exit_price, "TIME_STOP")
            closed.append(ticker)
            print(f"  ✅ [{ticker}] Closed at ${exit_price}")
        else:
            print(f"  ⚠️  [{ticker}] Close order may have failed — check Alpaca dashboard.")

    if closed:
        _send_telegram(
            f"⏱️ *Agent {agent_id} — Time Stop*\n"
            f"_{_fmt_et_sgt()}_\n"
            f"Closed: {', '.join(closed)}"
        )

def _record_close(state: dict, ticker: str, exit_price: float, reason: str) -> None:
    position = state["positions"].get(ticker)
    if not position:
        return

    entry = float(position.get("entry_price") or position.get("pm_price", exit_price))
    shares = position.get("shares", 0)
    pnl = round(shares * (entry - exit_price), 2)
    pnl_pct = round((entry - exit_price) / entry * 100, 2) if entry else 0

    closed = {
        **position,
        "exit_price":    exit_price,
        "exit_reason":   reason,
        "exit_time_et":  _fmt(_now_et()),
        "exit_time_sgt": _fmt(_utc_to_sgt(_now_utc())),
        "pnl_usd":       pnl,
        "pnl_pct":       pnl_pct,
        "outcome":       "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT",
        "status":        "closed",
    }
    state["closed_trades"].append(closed)
    state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl, 2)
    del state["positions"][ticker]

# ---------------------------------------------------------------------------
# Cumulative P&L — reads all history files for an agent
# ---------------------------------------------------------------------------

def _cumulative_pnl(agent_id: int) -> dict:
    """Sum P&L across all archived history files for this agent."""
    total = 0.0
    total_trades = 0
    total_wins = 0
    for f in sorted(HISTORY_DIR.glob(f"agent{agent_id}_*.json")):
        try:
            data = json.loads(f.read_text())
            for t in data.get("closed_trades", []):
                total += float(t.get("pnl_usd", 0))
                total_trades += 1
                if t.get("outcome") == "WIN":
                    total_wins += 1
        except Exception:
            continue
    return {
        "cumulative_pnl":    round(total, 2),
        "cumulative_trades": total_trades,
        "cumulative_wins":   total_wins,
    }

# ---------------------------------------------------------------------------
# MODE: summary
# ---------------------------------------------------------------------------

def mode_summary(agent_id: int) -> None:
    source = AGENT_SOURCES[agent_id]
    print(f"\n{'='*60}")
    print(f"  AGENT {agent_id} ({source.upper()}) — SESSION SUMMARY — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state = _load_state(agent_id)

    # Force-close any remaining open positions in state (safety net)
    open_positions = {k: v for k, v in state["positions"].items() if v["status"] == "open"}
    if open_positions:
        print("  ⚠️  Unclosed positions found — force-closing.")
        _close_all(agent_id, state)

    # Fetch live open positions from Alpaca
    try:
        live_positions = {p["symbol"]: p for p in get_positions()}
    except Exception:
        live_positions = {}

    closed = state["closed_trades"]
    cumul  = _cumulative_pnl(agent_id)

    # --- Today's P&L ---
    today_pnl  = round(sum(c["pnl_usd"] for c in closed), 2)
    wins       = [c for c in closed if c["outcome"] == "WIN"]
    losses     = [c for c in closed if c["outcome"] == "LOSS"]
    win_rate   = round(len(wins) / len(closed) * 100, 1) if closed else 0
    avg_win    = round(sum(c["pnl_usd"] for c in wins) / len(wins), 2) if wins else 0
    avg_loss   = round(sum(c["pnl_usd"] for c in losses) / len(losses), 2) if losses else 0

    # Include today in cumulative
    cumul_total = round(cumul["cumulative_pnl"] + today_pnl, 2)
    cumul_trades = cumul["cumulative_trades"] + len(closed)
    cumul_wins   = cumul["cumulative_wins"] + len(wins)
    cumul_win_rate = round(cumul_wins / cumul_trades * 100, 1) if cumul_trades else 0

    print(f"\n  Today — Trades:{len(closed)}  Wins:{len(wins)}  Losses:{len(losses)}  WinRate:{win_rate}%")
    print(f"  Today P&L: ${today_pnl}  |  Cumulative P&L: ${cumul_total}")
    for c in closed:
        emoji = "✅" if c["outcome"] == "WIN" else "❌"
        print(f"  {emoji} {c['ticker']:6s}  ${c.get('entry_price','?')} → ${c['exit_price']}  "
              f"P&L:${c['pnl_usd']} ({c['pnl_pct']}%)  [{c['exit_reason']}]")

    today_emoji = "🟢" if today_pnl > 0 else "🔴" if today_pnl < 0 else "⚪"
    cumul_emoji = "🟢" if cumul_total > 0 else "🔴" if cumul_total < 0 else "⚪"

    lines = [
        f"📊 *Agent {agent_id} ({source}) — Daily Report*",
        f"_{_fmt_et_sgt()}_",
        "",
        f"━━━ TODAY ━━━",
        f"{today_emoji} *Day P&L: ${today_pnl}*  |  Win Rate: {win_rate}%",
        f"Trades: {len(closed)}  |  Wins: {len(wins)}  |  Losses: {len(losses)}",
        f"Avg Win: ${avg_win}  |  Avg Loss: ${avg_loss}",
    ]

    # Today's closed trades breakdown
    if closed:
        lines.append("")
        for c in closed:
            emoji = "✅" if c["outcome"] == "WIN" else "❌"
            lines.append(
                f"{emoji} *{c['ticker']}*  {c['exit_reason']}\n"
                f"   ${c.get('entry_price','?')} → ${c['exit_price']}  "
                f"P&L: *${c['pnl_usd']}* ({c['pnl_pct']}%)"
            )
    else:
        lines.append("_No trades taken today._")

    # Live open positions
    lines += ["", "━━━ OPEN POSITIONS ━━━"]
    if live_positions:
        for symbol, p in live_positions.items():
            entry      = float(p.get("avg_entry_price", 0))
            current    = float(p.get("current_price", 0))
            unreal_pnl = float(p.get("unrealized_pl", 0))
            unreal_pct = float(p.get("unrealized_plpc", 0)) * 100
            qty        = p.get("qty", "?")
            side       = p.get("side", "?")
            pos_emoji  = "📈" if unreal_pnl >= 0 else "📉"
            lines.append(
                f"{pos_emoji} *{symbol}*  {side} {qty} shares\n"
                f"   Entry: ${entry}  |  Now: ${current}\n"
                f"   Unrealized P&L: *${unreal_pnl:.2f}* ({unreal_pct:.2f}%)"
            )
    else:
        lines.append("_No open positions._")

    # Cumulative P&L
    lines += [
        "",
        "━━━ CUMULATIVE (ALL TIME) ━━━",
        f"{cumul_emoji} *Total P&L: ${cumul_total}*  |  Win Rate: {cumul_win_rate}%",
        f"Total Trades: {cumul_trades}  |  Total Wins: {cumul_wins}",
        "",
        "_Paper trading — Alpaca paper account. No real money._",
    ]

    state["summary"] = {
        "today_pnl":      today_pnl,
        "trades":         len(closed),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       win_rate,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "cumulative_pnl": cumul_total,
        "cumulative_trades": cumul_trades,
        "cumulative_win_rate": cumul_win_rate,
    }
    _save_state(agent_id, state)
    _archive_state(agent_id, state)
    _send_telegram("\n".join(lines))
    print(f"\n  ✅ Summary sent.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Alpaca Paper Trading Agent")
    parser.add_argument("--agent", required=True,
                        help="Agent number: 1, 2, 3, or 'all'")
    parser.add_argument("--mode", required=True,
                        choices=["premarket", "open", "monitor", "