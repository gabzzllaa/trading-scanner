#!/usr/bin/env python3
"""
Paper Trading Agent — Bagholder Exit Liquidity Strategy
---------------------------------------------------------
Reads A+ setups from latest_scan.json and simulates the full trade lifecycle:

  Phase 1 — Pre-market (scanner fires → 9:30 AM ET)
    - Loads A+ setups from latest_scan.json
    - Records paper short entry at current pre-market price
    - Polls TradingView every 5 min to track price drift

  Phase 2 — Open (9:30 AM ET)
    - Locks entry at open price (first available real-market quote)
    - Monitors stop loss (10% above pre-market high) and Target 1 (50% retracement)

  Phase 3 — Time stop (10:30 AM ET)
    - Closes any remaining open positions at current price
    - Sends Telegram P&L summary

Usage:
  python paper_trader.py --mode premarket   # Load setups, track pre-market (run at scan time)
  python paper_trader.py --mode open        # Record open price, start monitoring
  python paper_trader.py --mode monitor     # Poll prices, check exits (run every 5 min)
  python paper_trader.py --mode close       # Force-close all open positions (10:30 AM ET)
  python paper_trader.py --mode summary     # Print + send today's P&L summary

GitHub Actions runs this script in sequence:
  - premarket: immediately after morning scanner (6–9 AM ET)
  - open: at 9:30 AM ET
  - monitor (x3): at 9:45, 10:00, 10:15 AM ET
  - close: at 10:30 AM ET
  - summary: at 11:00 AM ET
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = SCRIPT_DIR / "data"
PAPER_TRADES_FILE = DATA_DIR / "paper_trades.json"
LATEST_SCAN_FILE = DATA_DIR / "latest_scan.json"
HISTORY_DIR = DATA_DIR / "history"

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CAPITAL = 10_000
RISK_PER_TRADE = 100       # 1% of capital
STOP_PCT = 0.10            # 10% above pre-market high → stop loss for short
TARGET1_RETRACE = 0.50     # 50% retracement of the gap
POLL_INTERVAL_MIN = 5      # minutes between price polls

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc_to_et(dt: datetime) -> datetime:
    """Approximate ET (no pytz). EDT=UTC-4 Mar-Nov, EST=UTC-5 otherwise."""
    month = dt.month
    offset = -4 if 3 <= month <= 11 else -5
    return dt + timedelta(hours=offset)


def _utc_to_sgt(dt: datetime) -> datetime:
    return dt + timedelta(hours=8)


def _now_et() -> datetime:
    return _utc_to_et(_now_utc())


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_et_sgt() -> str:
    now = _now_utc()
    et_label = "EDT" if 3 <= now.month <= 11 else "EST"
    return f"{_fmt(_utc_to_et(now))} {et_label} / {_fmt(_utc_to_sgt(now))} SGT"


def _is_market_hours() -> bool:
    """True if current ET time is between 9:30 and 16:00."""
    et = _now_et()
    open_time = et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= et <= close_time


def _is_time_stop() -> bool:
    """True if ET time is >= 10:30 AM."""
    et = _now_et()
    stop = et.replace(hour=10, minute=30, second=0, microsecond=0)
    return et >= stop


# ---------------------------------------------------------------------------
# Price fetching — TradingView Scanner API
# ---------------------------------------------------------------------------

def fetch_current_price(ticker: str) -> Optional[float]:
    """
    Fetch the latest price for a ticker from TradingView Scanner.
    Returns pre-market price if pre-market, regular price if market open.
    """
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": [f"NASDAQ:{ticker}", f"NYSE:{ticker}", f"AMEX:{ticker}"]},
        "options": {"lang": "en"},
        "columns": ["name", "close", "premarket_close", "premarket_change", "lp", "lp_time"],
        "sort": {"sortBy": "name", "sortOrder": "asc"},
        "range": [0, 5],
    }
    headers = {
        **HEADERS,
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", []):
            d = item.get("d", [])
            # d = [name, close, premarket_close, premarket_change, lp, lp_time]
            if len(d) < 5:
                continue
            name, close, pm_close, pm_change, lp = d[:5]
            t = name.split(":")[1] if name and ":" in name else name
            if t and t.upper() == ticker.upper():
                # Use last price (lp) if available, else pre-market close, else close
                price = lp or pm_close or close
                if price:
                    return float(price)
    except Exception as e:
        print(f"    [!] Price fetch error for {ticker}: {e}")
    return None


def fetch_open_price(ticker: str) -> Optional[float]:
    """
    Fetch the opening price from TradingView (first available after 9:30 AM ET).
    Uses the 'open' column.
    """
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": [f"NASDAQ:{ticker}", f"NYSE:{ticker}", f"AMEX:{ticker}"]},
        "options": {"lang": "en"},
        "columns": ["name", "open", "close", "lp"],
        "sort": {"sortBy": "name", "sortOrder": "asc"},
        "range": [0, 5],
    }
    headers = {
        **HEADERS,
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", []):
            d = item.get("d", [])
            if len(d) < 4:
                continue
            name, open_p, close, lp = d[:4]
            t = name.split(":")[1] if name and ":" in name else name
            if t and t.upper() == ticker.upper():
                price = open_p or lp or close
                if price:
                    return float(price)
    except Exception as e:
        print(f"    [!] Open price fetch error for {ticker}: {e}")
    return None


# ---------------------------------------------------------------------------
# Paper trades state management
# ---------------------------------------------------------------------------

def _load_trades() -> dict:
    """Load today's paper trades file. Creates fresh if missing or stale."""
    today = _now_et().strftime("%Y-%m-%d")
    if PAPER_TRADES_FILE.exists():
        with open(PAPER_TRADES_FILE) as f:
            data = json.load(f)
        # If file is from a previous day, archive it and start fresh
        if data.get("date") != today:
            _archive_trades(data)
            return _fresh_trades(today)
        return data
    return _fresh_trades(today)


def _fresh_trades(date: str) -> dict:
    return {
        "date": date,
        "strategy": "Bagholder Exit Liquidity",
        "capital": CAPITAL,
        "positions": {},   # ticker → position dict
        "closed_trades": [],
        "summary": None,
    }


def _save_trades(data: dict) -> None:
    with open(PAPER_TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _archive_trades(data: dict) -> None:
    """Save completed day's trades to history folder."""
    date = data.get("date", "unknown")
    hist_file = HISTORY_DIR / f"paper_trades_{date}.json"
    with open(hist_file, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  📁 Archived previous day's trades → {hist_file}")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  [Telegram] Env vars not set — skipping.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
        print("  [Telegram] ✅ Message sent.")
    except Exception as e:
        print(f"  [Telegram] ✗ Failed: {e}")


# ---------------------------------------------------------------------------
# Trade logic helpers
# ---------------------------------------------------------------------------

def _compute_targets(entry: float, prev_close: float, pm_high: float) -> dict:
    """
    For a paper short:
      stop_loss   = pm_high * (1 + STOP_PCT)   ← price rises → loss
      target1     = entry - 0.5 * (entry - prev_close)  ← 50% retracement
      target2     = prev_close                             ← full gap fill
    """
    stop_loss = round(pm_high * (1 + STOP_PCT), 4)
    gap_size = entry - prev_close
    target1 = round(entry - TARGET1_RETRACE * gap_size, 4)
    target2 = round(prev_close, 4)

    # Shares: risk $100 / (stop - entry) distance
    risk_per_share = stop_loss - entry
    shares = int(RISK_PER_TRADE / risk_per_share) if risk_per_share > 0 else 0

    reward_t1 = round(shares * (entry - target1), 2) if shares else 0
    reward_t2 = round(shares * (entry - target2), 2) if shares else 0

    return {
        "stop_loss": stop_loss,
        "target1": target1,
        "target2": target2,
        "shares": shares,
        "risk_usd": round(shares * risk_per_share, 2) if shares else 0,
        "reward_t1": reward_t1,
        "reward_t2": reward_t2,
        "rr_ratio": round(reward_t1 / (shares * risk_per_share), 2) if shares and risk_per_share > 0 else 0,
    }


def _check_exit(position: dict, current_price: float) -> Optional[str]:
    """
    Returns exit reason string if position should be closed, else None.
    For shorts: stop = price goes UP past stop_loss; target = price goes DOWN to target1.
    """
    if current_price >= position["stop_loss"]:
        return "STOP_LOSS"
    if current_price <= position["target1"]:
        return "TARGET_1"
    return None


def _close_position(position: dict, exit_price: float, exit_reason: str, trades_data: dict) -> dict:
    """Close a position and compute P&L."""
    entry = position["entry_price"]
    shares = position["shares"]
    # Short P&L: profit when price goes down
    pnl = round(shares * (entry - exit_price), 2)
    pnl_pct = round((entry - exit_price) / entry * 100, 2)

    closed = {
        **position,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_time_utc": _now_utc().isoformat(),
        "exit_time_et": _fmt(_now_et()),
        "exit_time_sgt": _fmt(_utc_to_sgt(_now_utc())),
        "pnl_usd": pnl,
        "pnl_pct": pnl_pct,
        "outcome": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT",
    }
    trades_data["closed_trades"].append(closed)
    del trades_data["positions"][position["ticker"]]
    return closed


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_premarket():
    """
    Load A+ setups from latest_scan.json, create paper positions at current PM price.
    Run immediately after the morning scanner fires.
    """
    print(f"\n{'='*60}")
    print(f"  PAPER TRADER — PRE-MARKET LOAD — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    if not LATEST_SCAN_FILE.exists():
        print("  ⚠️  No scan file found. Run scanner first.")
        return

    with open(LATEST_SCAN_FILE) as f:
        scan = json.load(f)

    candidates = scan.get("candidates", [])
    a_plus = [c for c in candidates if c.get("tier") == "A+"]

    if not a_plus:
        print("  No A+ setups in latest scan — no paper positions to open.")
        return

    trades = _load_trades()
    opened = []

    for c in a_plus:
        ticker = c["ticker"]
        if ticker in trades["positions"]:
            print(f"  [{ticker}] Position already open — skipping.")
            continue

        prev_close = c.get("prev_close")
        pm_high = c.get("pm_price")

        # Fetch fresh current PM price
        print(f"  [{ticker}] Fetching current pre-market price...")
        current_pm_price = fetch_current_price(ticker)
        if current_pm_price is None:
            # Fall back to scanner's recorded PM price
            current_pm_price = pm_high
            print(f"    ⚠️  Could not fetch live price — using scanner price: ${current_pm_price}")
        else:
            print(f"    Current PM price: ${current_pm_price}")

        if not current_pm_price or not prev_close:
            print(f"  [{ticker}] Missing price data — skipping.")
            continue

        pm_high_price = max(float(pm_high or current_pm_price), float(current_pm_price))
        targets = _compute_targets(float(current_pm_price), float(prev_close), pm_high_price)

        position = {
            "ticker": ticker,
            "strategy": "Bagholder Exit Liquidity",
            "scan_score": c.get("total_score"),
            "scan_gap_pct": c.get("premarket_gap_pct"),
            "prev_close": float(prev_close),
            "pm_high": pm_high_price,
            "pm_entry_price": float(current_pm_price),
            "entry_price": float(current_pm_price),   # updated at open
            "entry_phase": "premarket",
            "entry_time_utc": _now_utc().isoformat(),
            "entry_time_et": _fmt(_now_et()),
            "entry_time_sgt": _fmt(_utc_to_sgt(_now_utc())),
            "shares": targets["shares"],
            "stop_loss": targets["stop_loss"],
            "target1": targets["target1"],
            "target2": targets["target2"],
            "risk_usd": targets["risk_usd"],
            "reward_t1": targets["reward_t1"],
            "status": "premarket",
            "price_history": [
                {"price": float(current_pm_price), "time_et": _fmt(_now_et()), "phase": "premarket_open"}
            ],
        }
        trades["positions"][ticker] = position
        opened.append(position)
        print(f"  ✅ [{ticker}] Paper short opened @ ${current_pm_price} | Stop: ${targets['stop_loss']} | T1: ${targets['target1']} | Shares: {targets['shares']}")

    _save_trades(trades)

    if opened:
        lines = [
            f"📋 *Paper Trader — Pre-Market Positions Opened*",
            f"_{_fmt_et_sgt()}_",
            "",
        ]
        for p in opened:
            lines += [
                f"📉 *{p['ticker']}* (Score: {p['scan_score']}/60, Gap: +{p['scan_gap_pct']}%)",
                f"  PM Entry: ${p['pm_entry_price']}  |  Stop: ${p['stop_loss']}",
                f"  T1: ${p['target1']}  |  T2: ${p['target2']}",
                f"  Shares: {p['shares']}  |  Max Risk: ${p['risk_usd']}",
                "",
            ]
        lines.append("_Monitoring until market open at 9:30 PM SGT / 9:30 AM ET._")
        _send_telegram("\n".join(lines))


def mode_open():
    """
    At market open (9:30 AM ET): fetch open price and update entry.
    """
    print(f"\n{'='*60}")
    print(f"  PAPER TRADER — MARKET OPEN — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    trades = _load_trades()
    if not trades["positions"]:
        print("  No open positions.")
        return

    updates = []
    for ticker, position in list(trades["positions"].items()):
        print(f"  [{ticker}] Fetching open price...")
        open_price = fetch_open_price(ticker)
        if open_price is None:
            open_price = fetch_current_price(ticker)
        if open_price is None:
            print(f"  [{ticker}] ⚠️  Could not get open price — keeping PM entry.")
            continue

        prev_close = position["prev_close"]
        pm_high = position["pm_high"]

        # Recompute targets with actual open price as entry
        targets = _compute_targets(open_price, prev_close, pm_high)
        position["entry_price"] = open_price
        position["entry_phase"] = "open"
        position["open_time_et"] = _fmt(_now_et())
        position["stop_loss"] = targets["stop_loss"]
        position["target1"] = targets["target1"]
        position["target2"] = targets["target2"]
        position["shares"] = targets["shares"]
        position["risk_usd"] = targets["risk_usd"]
        position["reward_t1"] = targets["reward_t1"]
        position["status"] = "open"
        position["price_history"].append({
            "price": open_price, "time_et": _fmt(_now_et()), "phase": "market_open"
        })
        updates.append(position)
        print(f"  ✅ [{ticker}] Open @ ${open_price} | Stop: ${targets['stop_loss']} | T1: ${targets['target1']}")

    _save_trades(trades)

    if updates:
        lines = [
            f"🔔 *Paper Trader — Market Open*",
            f"_{_fmt_et_sgt()}_",
            "",
        ]
        for p in updates:
            lines += [
                f"📉 *{p['ticker']}* entered short @ ${p['entry_price']}",
                f"  Stop: ${p['stop_loss']}  |  T1: ${p['target1']}  |  T2: ${p['target2']}",
                f"  Shares: {p['shares']}  |  Risk: ${p['risk_usd']}  |  R/R: {_compute_targets(p['entry_price'], p['prev_close'], p['pm_high'])['rr_ratio']}:1",
                "",
            ]
        lines.append("_Monitoring. Time stop at 10:30 PM SGT / 10:30 AM ET._")
        _send_telegram("\n".join(lines))


def mode_monitor():
    """
    Poll prices, check stop/target exits. Run every 5 min between 9:30–10:30 AM ET.
    """
    print(f"\n{'='*60}")
    print(f"  PAPER TRADER — MONITORING — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    trades = _load_trades()
    if not trades["positions"]:
        print("  No open positions to monitor.")
        return

    exited = []
    for ticker in list(trades["positions"].keys()):
        position = trades["positions"][ticker]
        print(f"  [{ticker}] Fetching price...")
        price = fetch_current_price(ticker)
        if price is None:
            print(f"  [{ticker}] ⚠️  Could not fetch price — skipping.")
            continue

        position["price_history"].append({
            "price": price, "time_et": _fmt(_now_et()), "phase": "monitoring"
        })
        position["last_price"] = price
        position["last_check_et"] = _fmt(_now_et())

        unrealized_pnl = round(position["shares"] * (position["entry_price"] - price), 2)
        pnl_pct = round((position["entry_price"] - price) / position["entry_price"] * 100, 2)
        print(f"  [{ticker}] Price: ${price} | Unrealized P&L: ${unrealized_pnl} ({pnl_pct}%)")

        exit_reason = _check_exit(position, price)
        if exit_reason:
            closed = _close_position(position, price, exit_reason, trades)
            exited.append(closed)
            emoji = "✅" if closed["outcome"] == "WIN" else "❌"
            print(f"  {emoji} [{ticker}] CLOSED — {exit_reason} @ ${price} | P&L: ${closed['pnl_usd']} ({closed['pnl_pct']}%)")
            _send_telegram(
                f"{emoji} *Paper Trade Closed — {ticker}*\n"
                f"_{_fmt_et_sgt()}_\n\n"
                f"Exit: *{exit_reason}* @ ${price}\n"
                f"Entry: ${position['entry_price']}  |  Shares: {position['shares']}\n"
                f"P&L: *${closed['pnl_usd']}* ({closed['pnl_pct']}%)  |  Outcome: *{closed['outcome']}*"
            )

    _save_trades(trades)
    remaining = len(trades["positions"])
    print(f"\n  {len(exited)} position(s) closed | {remaining} still open")


def mode_close():
    """
    10:30 AM ET time stop — force-close all remaining open positions.
    """
    print(f"\n{'='*60}")
    print(f"  PAPER TRADER — TIME STOP CLOSE — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    trades = _load_trades()
    if not trades["positions"]:
        print("  No open positions to close.")
        return

    force_closed = []
    for ticker in list(trades["positions"].keys()):
        position = trades["positions"][ticker]
        print(f"  [{ticker}] Fetching final price...")
        price = fetch_current_price(ticker)
        if price is None:
            price = position.get("last_price") or position["entry_price"]
            print(f"  [{ticker}] ⚠️  Using last known price: ${price}")

        closed = _close_position(position, price, "TIME_STOP", trades)
        force_closed.append(closed)
        emoji = "✅" if closed["outcome"] == "WIN" else "❌"
        print(f"  {emoji} [{ticker}] Time-stopped @ ${price} | P&L: ${closed['pnl_usd']} ({closed['pnl_pct']}%)")

    _save_trades(trades)

    if force_closed:
        lines = [f"⏱️ *Paper Trader — Time Stop (10:30 AM ET)*", f"_{_fmt_et_sgt()}_", ""]
        for c in force_closed:
            emoji = "✅" if c["outcome"] == "WIN" else "❌"
            lines.append(f"{emoji} *{c['ticker']}* closed @ ${c['exit_price']} | P&L: ${c['pnl_usd']} ({c['pnl_pct']}%)")
        _send_telegram("\n".join(lines))


def mode_summary():
    """
    Send end-of-session P&L summary via Telegram and print to console.
    """
    print(f"\n{'='*60}")
    print(f"  PAPER TRADER — SESSION SUMMARY — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    trades = _load_trades()

    # Force-close any stragglers (shouldn't happen but safety net)
    if trades["positions"]:
        print("  ⚠️  Found unclosed positions — force-closing at last known price.")
        for ticker in list(trades["positions"].keys()):
            position = trades["positions"][ticker]
            price = position.get("last_price") or position["entry_price"]
            _close_position(position, price, "SUMMARY_FORCE_CLOSE", trades)
        _save_trades(trades)

    closed = trades["closed_trades"]
    if not closed:
        msg = (
            f"📊 *Paper Trader — Session Summary*\n"
            f"_{_fmt_et_sgt()}_\n\n"
            f"No trades taken today."
        )
        print("  No trades taken today.")
        _send_telegram(msg)
        return

    total_pnl = round(sum(c["pnl_usd"] for c in closed), 2)
    wins = [c for c in closed if c["outcome"] == "WIN"]
    losses = [c for c in closed if c["outcome"] == "LOSS"]
    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0
    avg_win = round(sum(c["pnl_usd"] for c in wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(c["pnl_usd"] for c in losses) / len(losses), 2) if losses else 0

    # Print to console
    print(f"\n  Trades: {len(closed)}  |  Wins: {len(wins)}  |  Losses: {len(losses)}  |  Win Rate: {win_rate}%")
    print(f"  Total P&L: ${total_pnl}  |  Avg Win: ${avg_win}  |  Avg Loss: ${avg_loss}")
    for c in closed:
        emoji = "✅" if c["outcome"] == "WIN" else "❌"
        print(f"  {emoji} {c['ticker']:6s}  Entry: ${c['entry_price']}  Exit: ${c['exit_price']}  "
              f"P&L: ${c['pnl_usd']} ({c['pnl_pct']}%)  Reason: {c['exit_reason']}")

    # Build Telegram message
    result_emoji = "🟢" if total_pnl > 0 else "🔴" if total_pnl < 0 else "⚪"
    lines = [
        f"📊 *Paper Trader — Session Summary*",
        f"_{_fmt_et_sgt()}_",
        "",
        f"{result_emoji} *Total P&L: ${total_pnl}*  |  Win Rate: {win_rate}%",
        f"Trades: {len(closed)}  |  Wins: {len(wins)}  |  Losses: {len(losses)}",
        f"Avg Win: ${avg_win}  |  Avg Loss: ${avg_loss}",
        "",
    ]
    for c in closed:
        emoji = "✅" if c["outcome"] == "WIN" else "❌"
        lines.append(
            f"{emoji} *{c['ticker']}*  {c['exit_reason']}  "
            f"${c['entry_price']} → ${c['exit_price']}  "
            f"P&L: ${c['pnl_usd']} ({c['pnl_pct']}%)"
        )
    lines += [
        "",
        "_Paper trading only. No real money involved._",
    ]

    # Save summary to trades file
    trades["summary"] = {
        "total_pnl": total_pnl,
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }
    _save_trades(trades)
    _archive_trades(trades)

    _send_telegram("\n".join(lines))
    print(f"\n  ✅ Summary sent.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Paper Trading Agent")
    parser.add_argument(
        "--mode",
        choices=["premarket", "open", "monitor", "close", "summary"],
        required=True,
        help="Which phase to run",
    )
    args = parser.parse_args()

    mode_map = {
        "premarket": mode_premarket,
        "open": mode_open,
        "monitor": mode_monitor,
        "close": mode_close,
        "summary": mode_summary,
    }
    mode_map[args.mode]()


if __name__ == "__main__":
    main()
