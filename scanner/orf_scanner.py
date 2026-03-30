#!/usr/bin/env python3
"""
Opening Range Fade (ORF) Scanner
----------------------------------
Identifies stocks that spike above their 9:30–9:45 AM ET opening range high,
then break back below it — a high-probability short setup as trapped buyers
from the initial spike are forced to sell.

Strategy Logic:
  1. At 9:45 AM ET: scan for stocks that moved >2% in the first 15 minutes.
     Record the Opening Range (OR) high and low for each.
  2. At 9:45–11:00 AM ET: monitor for OR high breakdown — price closing back
     below the OR high after a spike above it.
  3. Entry: short at OR high breakdown (price crosses back below OR high)
  4. Stop loss: 3% above OR high
  5. Target 1: OR low (bottom of the opening range)
  6. Target 2: Prior day close
  7. Time stop: 11:00 AM ET

Scoring (each /10, total /60):
  1. Prior trend (declining stock) — weak stocks fade harder
  2. OR size (range as % of price) — larger OR = more trapped buyers
  3. Gap direction — gapped up into OR high = more fuel for fade
  4. OR volume vs average — confirms conviction of initial spike
  5. Catalyst check — manual (same rule: M&A/FDA = do not trade)
  6. Rejection quality — how cleanly price rejected OR high

Tiers:
  ≥35 → A+ setup
  20–34 → Monitor
  <20 → Skip

Usage:
  python orf_scanner.py --mode scan      # 9:45 AM ET: scan + record OR levels
  python orf_scanner.py --mode monitor   # 10:00–10:45 AM ET: check for breakdown entry
  python orf_scanner.py --mode close     # 11:00 AM ET: time stop
  python orf_scanner.py --mode summary   # 11:30 AM ET: P&L summary

SGT equivalents:
  scan:    9:45 PM SGT
  monitor: 10:00, 10:30 PM SGT
  close:   11:00 PM SGT
  summary: 11:30 PM SGT
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
HISTORY_DIR = DATA_DIR / "history"
ORF_STATE_FILE = DATA_DIR / "orf_state.json"
ORF_TRADES_FILE = DATA_DIR / "orf_paper_trades.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CAPITAL = 10_000
RISK_PER_TRADE = 100          # 1% of capital
STOP_PCT = 0.03               # 3% above OR high (tighter than Bagholder)
MIN_MOVE_PCT = 2.0            # Minimum % move in first 15 min to qualify
OR_WINDOW_MINUTES = 15        # Opening range = first 15 min
TIME_STOP_ET = "11:00 AM ET"
TIME_STOP_SGT = "11:00 PM SGT"

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
    et_label = "EDT" if 3 <= now.month <= 11 else "EST"
    return f"{_fmt(_utc_to_et(now))} {et_label} / {_fmt(_utc_to_sgt(now))} SGT"


def _today_et() -> str:
    return _now_et().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# TradingView price fetching
# ---------------------------------------------------------------------------

def fetch_opening_range_movers() -> list[dict]:
    """
    Fetch stocks that have moved >2% since the open using TradingView Scanner.
    Returns stocks with current price, open price, high, low, volume, prev_close.
    Called at 9:45 AM ET — 15 min after open.
    """
    print("  📡 TradingView — fetching opening range movers...")
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "options": {"lang": "en"},
        "columns": [
            "name",
            "close",                   # prev day close
            "open_price",              # today's open
            "High",                    # today's high so far
            "Low",                     # today's low so far
            "last_price",              # last price
            "volume",                  # today's volume so far
            "average_volume_10d_calc",
            "market_cap_basic",
        ],
        "filter": [
            {"left": "is_primary", "operation": "equal", "right": True},
            {"left": "market_cap_basic", "operation": "greater", "right": 1000000},
        ],
        "filter2": {
            "operator": "and",
            "operands": [
                {
                    "operation": {
                        "operator": "or",
                        "operands": [
                            {"expression": {"left": "type", "operation": "equal", "right": "stock"}},
                        ],
                    }
                }
            ],
        },
        "sort": {"sortBy": "volume", "sortOrder": "desc"},
        "range": [0, 100],
    }
    headers = {
        **HEADERS,
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    [!] TradingView error: {e}")
        return []

    results = []
    for item in data.get("data", []):
        d = item.get("d", [])
        if len(d) < 9:
            continue
        name, prev_close, open_p, high, low, lp, volume, avg_vol, mkt_cap = d[:9]

        if not name or not open_p or not lp:
            continue

        ticker = name.split(":")[1] if ":" in name else name

        or_high = float(high) if high else float(lp)
        or_low = float(low) if low else float(lp)
        current_price = float(lp)
        open_price = float(open_p)
        prev_close_f = float(prev_close) if prev_close else None

        # Calculate move % from open (computed, not from API field)
        move_pct = (
            (current_price - open_price) / open_price * 100 if open_price else 0
        )

        # Filter: price ≥ $0.50
        if current_price < 0.50:
            continue

        # Filter: market cap ≥ $1M
        if mkt_cap and float(mkt_cap) < 1_000_000:
            continue

        # Only include stocks that actually moved >2% from open
        if move_pct < MIN_MOVE_PCT:
            continue

        # Calculate gap % from prev close to open
        gap_pct = ((open_price - prev_close_f) / prev_close_f * 100) if prev_close_f and prev_close_f > 0 else 0

        # OR size as % of price
        or_size_pct = ((or_high - or_low) / or_low * 100) if or_low > 0 else 0

        results.append({
            "ticker": ticker,
            "prev_close": prev_close_f,
            "open_price": open_price,
            "or_high": round(or_high, 4),
            "or_low": round(or_low, 4),
            "or_size_pct": round(or_size_pct, 2),
            "current_price": round(current_price, 4),
            "move_from_open_pct": round(move_pct, 2),
            "gap_pct": round(gap_pct, 2),
            "volume": volume,
            "avg_daily_volume": avg_vol,
            "market_cap_m": round(float(mkt_cap) / 1_000_000, 2) if mkt_cap else None,
            # Breakdown tracking
            "spiked_above_or_high": current_price > or_high * 0.99,  # currently at/above OR high
            "breakdown_triggered": False,
            "entry_price": None,
        })

    print(f"    Found {len(results)} opening range movers (>{MIN_MOVE_PCT}% from open)")
    return results


def fetch_current_price(ticker: str) -> Optional[float]:
    """Fetch latest price for a ticker from TradingView."""
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "markets": ["america"],
        "symbols": {
            "query": {"types": ["stock"]},
            "tickers": [f"NASDAQ:{ticker}", f"NYSE:{ticker}", f"AMEX:{ticker}"],
        },
        "options": {"lang": "en"},
        "columns": ["name", "lp", "close"],
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
            if len(d) < 2:
                continue
            name, lp = d[0], d[1]
            t = name.split(":")[1] if name and ":" in name else name
            if t and t.upper() == ticker.upper() and lp:
                return float(lp)
    except Exception as e:
        print(f"    [!] Price fetch error for {ticker}: {e}")
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_orf(candidate: dict) -> dict:
    """Score a candidate on all 6 ORF conditions."""

    # C1: Prior trend — is this a declining stock? (0–10)
    # We don't have 6m perf here without watchlist cross-ref, default to 5
    # (neutral — this strategy works on any stock, not just declining ones)
    c1 = 5

    # C2: OR size — larger range = more trapped buyers = better fade (0–10)
    or_size = candidate.get("or_size_pct", 0)
    if or_size >= 10:
        c2 = 10
    elif or_size >= 7:
        c2 = 8
    elif or_size >= 5:
        c2 = 7
    elif or_size >= 3:
        c2 = 5
    elif or_size >= 2:
        c2 = 3
    else:
        c2 = 1

    # C3: Gap direction — gapped up into OR high = more trapped longs (0–10)
    gap_pct = candidate.get("gap_pct", 0)
    if gap_pct >= 30:
        c3 = 10
    elif gap_pct >= 20:
        c3 = 9
    elif gap_pct >= 10:
        c3 = 7
    elif gap_pct >= 5:
        c3 = 5
    elif gap_pct > 0:
        c3 = 3
    else:
        c3 = 1  # gapped down or flat — fade still possible but weaker

    # C4: OR volume vs average — high volume spike = more trapped buyers (0–10)
    vol = candidate.get("volume") or 0
    avg_vol = candidate.get("avg_daily_volume") or 0
    # By 9:45 AM, expect ~15/390 = ~4% of daily volume. Scale accordingly.
    # If vol at 9:45 AM > 10% of avg daily vol, that's elevated for 15 minutes
    if avg_vol > 0:
        vol_ratio = vol / avg_vol
        if vol_ratio >= 0.30:    # 30% of avg daily in first 15 min = extreme
            c4 = 10
        elif vol_ratio >= 0.20:
            c4 = 8
        elif vol_ratio >= 0.10:
            c4 = 6
        elif vol_ratio >= 0.05:
            c4 = 4
        else:
            c4 = 2
    else:
        c4 = 3  # unknown volume

    # C5: Catalyst check — manual, placeholder 5/10
    c5 = 5

    # C6: Rejection quality — how far above OR high did price go before coming back?
    # More overshoot = weaker buyers = better fade
    move_pct = candidate.get("move_from_open_pct", 0)
    or_size_p = candidate.get("or_size_pct", 1) or 1
    # Rejection ratio: move vs OR size. If move >> OR size, spike was overdone
    rejection_ratio = move_pct / or_size_p if or_size_p > 0 else 0
    if rejection_ratio >= 2.0:
        c6 = 10
    elif rejection_ratio >= 1.5:
        c6 = 8
    elif rejection_ratio >= 1.0:
        c6 = 6
    elif rejection_ratio >= 0.5:
        c6 = 4
    else:
        c6 = 2

    total = c1 + c2 + c3 + c4 + c5 + c6
    tier = "A+" if total >= 35 else "Monitor" if total >= 20 else "Skip"

    candidate["scores"] = {
        "c1_prior_trend": c1,
        "c2_or_size": c2,
        "c3_gap_direction": c3,
        "c4_or_volume": c4,
        "c5_catalyst_MANUAL": c5,
        "c6_rejection_quality": c6,
        "total": total,
        "tier": tier,
    }
    candidate["total_score"] = total
    candidate["tier"] = tier

    # Trade parameters
    if tier in ("A+", "Monitor"):
        or_high = candidate["or_high"]
        or_low = candidate["or_low"]
        prev_close = candidate.get("prev_close") or or_low

        stop = round(or_high * (1 + STOP_PCT), 4)
        stop_distance = stop - or_high
        shares = int(RISK_PER_TRADE / stop_distance) if stop_distance > 0 else 0
        target1 = round(or_low, 4)                  # OR low
        target2 = round(prev_close, 4)              # prior day close

        reward_t1 = round(shares * (or_high - target1), 2) if shares else 0
        reward_t2 = round(shares * (or_high - target2), 2) if shares else 0
        rr = round(reward_t1 / RISK_PER_TRADE, 2) if RISK_PER_TRADE > 0 else 0

        candidate["trade"] = {
            "entry": or_high,           # short at OR high breakdown
            "stop": stop,
            "stop_distance": round(stop_distance, 4),
            "target1": target1,
            "target2": target2,
            "shares": shares,
            "risk_usd": RISK_PER_TRADE,
            "reward_t1": reward_t1,
            "reward_t2": reward_t2,
            "rr_ratio": rr,
            "time_stop_et": TIME_STOP_ET,
            "time_stop_sgt": TIME_STOP_SGT,
        }

    return candidate


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    today = _today_et()
    if ORF_STATE_FILE.exists():
        with open(ORF_STATE_FILE) as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
        # Archive previous day
        _archive_file(ORF_STATE_FILE, f"orf_state_{state.get('date','unknown')}.json")
    return {"date": today, "candidates": {}, "scanned_at": None}


def _save_state(state: dict) -> None:
    with open(ORF_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _load_trades() -> dict:
    today = _today_et()
    if ORF_TRADES_FILE.exists():
        with open(ORF_TRADES_FILE) as f:
            data = json.load(f)
        if data.get("date") == today:
            return data
        _archive_file(ORF_TRADES_FILE, f"orf_trades_{data.get('date','unknown')}.json")
    return {"date": today, "strategy": "ORF", "capital": CAPITAL, "positions": {}, "closed_trades": [], "summary": None}


def _save_trades(data: dict) -> None:
    with open(ORF_TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _archive_file(src: Path, dest_name: str) -> None:
    dest = HISTORY_DIR / dest_name
    if src.exists():
        with open(src) as f:
            data = json.load(f)
        with open(dest, "w") as f:
            json.dump(data, f, indent=2, default=str)


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


def _send_scan_alert(a_plus: list, monitor: list) -> None:
    """Send Telegram alert for scan results. Silent if nothing qualifies."""
    if not a_plus and not monitor:
        print("  [Telegram] No qualifying ORF setups — skipping notification.")
        return

    now_str = _fmt_et_sgt()

    if not a_plus:
        # Monitor only
        lines = [
            f"👀 *ORF Scanner — {now_str}*",
            f"{len(monitor)} Monitor setup(s), no A+.",
            "",
        ]
        for c in monitor[:3]:
            t = c.get("trade", {})
            lines.append(
                f"  • *{c['ticker']}*  OR: ${c['or_low']}–${c['or_high']} "
                f"({c['or_size_pct']}%)  Score: {c['total_score']}/60"
            )
        lines.append("\n_Watch for breakdown below OR high. No entry yet._")
        _send_telegram("\n".join(lines))
        return

    lines = [
        f"🚨 *ORF ALERT* — {now_str}",
        f"*{len(a_plus)} A+ setup(s)* — Opening Range Fade",
        "",
    ]
    for c in a_plus:
        t = c.get("trade", {})
        sc = c.get("scores", {})
        lines += [
            f"📉 *{c['ticker']}*  |  Score: {c['total_score']}/60  |  Tier: A+",
            f"  OR: ${c['or_low']} – ${c['or_high']} ({c['or_size_pct']}% range)",
            f"  Gap from prev close: +{c.get('gap_pct', 0):.1f}%  |  Move from open: +{c.get('move_from_open_pct', 0):.1f}%",
            f"  Entry (short at OR high breakdown): ${t.get('entry', '?')}",
            f"  Stop: ${t.get('stop', '?')} (+{STOP_PCT*100:.0f}% above OR high)",
            f"  Target 1 (OR low): ${t.get('target1', '?')}  |  Target 2 (prev close): ${t.get('target2', '?')}",
            f"  Shares: {t.get('shares', '?')}  |  Risk: ${t.get('risk_usd', '?')}  |  R/R: {t.get('rr_ratio', '?')}:1",
            f"  🔗 Reddit: https://www.reddit.com/search/?q={c['ticker']}",
            f"  🔗 Stocktwits: https://stocktwits.com/symbol/{c['ticker']}",
            "",
        ]
    if monitor:
        lines.append(f"👀 Also watching: {', '.join(c['ticker'] for c in monitor[:5])}")
        lines.append("")
    lines += [
        "⚠️ *Enter SHORT only when price closes back BELOW OR high.*",
        "⚠️ *Verify catalyst is hollow (Reddit/Stocktwits).*",
        f"⚠️ *Time stop: {TIME_STOP_SGT} / {TIME_STOP_ET}.*",
        "🚫 *If catalyst is M&A / FDA / earnings — DO NOT TRADE.*",
    ]
    _send_telegram("\n".join(lines))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_scan():
    """
    9:45 AM ET: Scan for opening range movers, score them, record OR levels.
    """
    print(f"\n{'='*60}")
    print(f"  ORF SCANNER — OPENING RANGE SCAN — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    movers = fetch_opening_range_movers()

    if not movers:
        print("  No opening range movers found.")
        return

    # Apply quality filters
    movers = [m for m in movers if (m.get("current_price") or 0) >= 0.50]
    movers = [m for m in movers if (m.get("or_size_pct") or 0) >= 1.0]
    print(f"  After quality filters: {len(movers)} candidates")

    # Score
    scored = [score_orf(m) for m in movers]
    scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    a_plus = [s for s in scored if s.get("tier") == "A+"]
    monitor = [s for s in scored if s.get("tier") == "Monitor"]

    print(f"\n  RESULTS: {len(a_plus)} A+ | {len(monitor)} Monitor | {len(scored)-len(a_plus)-len(monitor)} Skip")

    if a_plus:
        print("\n🔥 A+ SETUPS:")
        for c in a_plus:
            sc = c.get("scores", {})
            t = c.get("trade", {})
            print(f"\n  {c['ticker']}  Score:{c['total_score']}/60  OR:${c['or_low']}–${c['or_high']} ({c['or_size_pct']}%)")
            print(f"  C1:{sc.get('c1_prior_trend')} C2:{sc.get('c2_or_size')} C3:{sc.get('c3_gap_direction')} "
                  f"C4:{sc.get('c4_or_volume')} C5:{sc.get('c5_catalyst_MANUAL')}(manual) C6:{sc.get('c6_rejection_quality')}")
            print(f"  Entry:${t.get('entry')} Stop:${t.get('stop')} T1:${t.get('target1')} T2:${t.get('target2')}")
            print(f"  Shares:{t.get('shares')} Risk:${t.get('risk_usd')} R/R:{t.get('rr_ratio')}:1")

    if monitor:
        print("\n👀 MONITOR:")
        for c in monitor[:5]:
            print(f"  {c['ticker']:6s}  OR:${c['or_low']}–${c['or_high']} ({c['or_size_pct']}%)  Score:{c['total_score']}/60")

    # Save state
    state = _load_state()
    state["scanned_at"] = _fmt_et_sgt()
    state["candidates"] = {c["ticker"]: c for c in scored}
    _save_state(state)

    # Save history snapshot
    hist_file = HISTORY_DIR / f"orf_scan_{_now_et().strftime('%Y%m%d_%H%M%S')}.json"
    with open(hist_file, "w") as f:
        json.dump({"scanned_at": state["scanned_at"], "candidates": scored}, f, indent=2, default=str)

    # Telegram
    _send_scan_alert(a_plus, monitor)

    # Open paper positions for A+ setups
    if a_plus:
        _open_paper_positions(a_plus)


def mode_monitor():
    """
    10:00–10:45 AM ET: Check if any A+/Monitor candidates have broken below OR high.
    If breakdown confirmed → enter paper short, notify via Telegram.
    Also check exit conditions for already-open positions.
    """
    print(f"\n{'='*60}")
    print(f"  ORF SCANNER — MONITORING — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state = _load_state()
    trades = _load_trades()

    candidates = state.get("candidates", {})
    if not candidates:
        print("  No ORF candidates in state — run scan first.")
        return

    # --- Check for new breakdown entries ---
    for ticker, candidate in list(candidates.items()):
        if candidate.get("breakdown_triggered"):
            continue  # already entered
        if candidate.get("tier") not in ("A+", "Monitor"):
            continue

        or_high = candidate["or_high"]
        print(f"  [{ticker}] Checking for OR high breakdown (OR high: ${or_high})...")
        price = fetch_current_price(ticker)
        if price is None:
            print(f"  [{ticker}] Could not fetch price.")
            continue

        # Breakdown = price is now BELOW OR high (after having been at/above it)
        if price < or_high:
            candidate["breakdown_triggered"] = True
            candidate["breakdown_price"] = price
            candidate["breakdown_time_et"] = _fmt(_now_et())
            print(f"  ✅ [{ticker}] BREAKDOWN @ ${price} (OR high was ${or_high})")

            # Open paper position if not already open
            if ticker not in trades["positions"]:
                _open_single_paper_position(candidate, price, trades)
                _save_trades(trades)

                t = candidate.get("trade", {})
                _send_telegram(
                    f"📉 *ORF Breakdown — {ticker}*\n"
                    f"_{_fmt_et_sgt()}_\n\n"
                    f"Broke below OR high ${or_high} → now at *${price}*\n"
                    f"Paper short entered @ ${price}\n"
                    f"Stop: ${t.get('stop', '?')}  |  T1: ${t.get('target1', '?')}  |  T2: ${t.get('target2', '?')}\n"
                    f"Shares: {t.get('shares', '?')}  |  Risk: ${t.get('risk_usd', '?')}\n"
                    f"⚠️ *Verify catalyst before real entry!*"
                )
        else:
            print(f"  [{ticker}] Still above OR high @ ${price} — no breakdown yet.")

        time.sleep(0.5)

    # Update state
    state["candidates"] = candidates
    _save_state(state)

    # --- Monitor open positions for exit ---
    _monitor_open_positions(trades)
    _save_trades(trades)


def mode_close():
    """
    11:00 AM ET: Time stop — force-close all open ORF positions.
    """
    print(f"\n{'='*60}")
    print(f"  ORF SCANNER — TIME STOP CLOSE — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    trades = _load_trades()
    if not trades["positions"]:
        print("  No open ORF positions.")
        return

    closed = []
    for ticker in list(trades["positions"].keys()):
        position = trades["positions"][ticker]
        price = fetch_current_price(ticker) or position.get("last_price") or position["entry_price"]
        c = _close_position(position, price, "TIME_STOP", trades)
        closed.append(c)
        emoji = "✅" if c["outcome"] == "WIN" else "❌"
        print(f"  {emoji} [{ticker}] Closed @ ${price} | P&L: ${c['pnl_usd']} ({c['pnl_pct']}%)")

    _save_trades(trades)

    if closed:
        lines = [f"⏱️ *ORF Time Stop — {TIME_STOP_SGT}*", f"_{_fmt_et_sgt()}_", ""]
        for c in closed:
            emoji = "✅" if c["outcome"] == "WIN" else "❌"
            lines.append(f"{emoji} *{c['ticker']}* @ ${c['exit_price']} | P&L: ${c['pnl_usd']} ({c['pnl_pct']}%)")
        _send_telegram("\n".join(lines))


def mode_summary():
    """
    11:30 AM ET: Send full P&L summary for the ORF session.
    """
    print(f"\n{'='*60}")
    print(f"  ORF SCANNER — SESSION SUMMARY — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    trades = _load_trades()

    # Force-close any stragglers
    if trades["positions"]:
        for ticker in list(trades["positions"].keys()):
            position = trades["positions"][ticker]
            price = position.get("last_price") or position["entry_price"]
            _close_position(position, price, "SUMMARY_FORCE_CLOSE", trades)
        _save_trades(trades)

    closed = trades["closed_trades"]
    if not closed:
        print("  No ORF trades taken today.")
        return

    total_pnl = round(sum(c["pnl_usd"] for c in closed), 2)
    wins = [c for c in closed if c["outcome"] == "WIN"]
    losses = [c for c in closed if c["outcome"] == "LOSS"]
    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0
    avg_win = round(sum(c["pnl_usd"] for c in wins) / len(wins), 2) if wins else 0
    avg_loss = round(sum(c["pnl_usd"] for c in losses) / len(losses), 2) if losses else 0

    print(f"  Trades:{len(closed)} Wins:{len(wins)} Losses:{len(losses)} Win Rate:{win_rate}%")
    print(f"  Total P&L: ${total_pnl} | Avg Win: ${avg_win} | Avg Loss: ${avg_loss}")

    result_emoji = "🟢" if total_pnl > 0 else "🔴" if total_pnl < 0 else "⚪"
    lines = [
        f"📊 *ORF Summary — {_fmt_et_sgt()}*",
        "",
        f"{result_emoji} *Total P&L: ${total_pnl}* | Win Rate: {win_rate}%",
        f"Trades: {len(closed)} | Wins: {len(wins)} | Losses: {len(losses)}",
        f"Avg Win: ${avg_win} | Avg Loss: ${avg_loss}",
        "",
    ]
    for c in closed:
        emoji = "✅" if c["outcome"] == "WIN" else "❌"
        lines.append(
            f"{emoji} *{c['ticker']}*  {c['exit_reason']}  "
            f"${c['entry_price']} → ${c['exit_price']}  P&L: ${c['pnl_usd']} ({c['pnl_pct']}%)"
        )
    lines += ["", "_Paper trading only. No real money involved._"]

    trades["summary"] = {
        "total_pnl": total_pnl, "trades": len(closed),
        "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
    }
    _save_trades(trades)
    _archive_file(ORF_TRADES_FILE, f"orf_trades_{_today_et()}.json")
    _send_telegram("\n".join(lines))


# ---------------------------------------------------------------------------
# Paper trading helpers
# ---------------------------------------------------------------------------

def _open_paper_positions(candidates: list[dict]) -> None:
    """Open paper positions for a list of A+ candidates at OR high."""
    trades = _load_trades()
    for c in candidates:
        _open_single_paper_position(c, c["or_high"], trades)
    _save_trades(trades)


def _open_single_paper_position(candidate: dict, entry_price: float, trades: dict) -> None:
    ticker = candidate["ticker"]
    if ticker in trades["positions"]:
        return

    t = candidate.get("trade", {})
    position = {
        "ticker": ticker,
        "strategy": "ORF",
        "scan_score": candidate.get("total_score"),
        "or_high": candidate["or_high"],
        "or_low": candidate["or_low"],
        "or_size_pct": candidate["or_size_pct"],
        "prev_close": candidate.get("prev_close"),
        "entry_price": entry_price,
        "entry_time_et": _fmt(_now_et()),
        "entry_time_sgt": _fmt(_utc_to_sgt(_now_utc())),
        "shares": t.get("shares", 0),
        "stop_loss": t.get("stop", 0),
        "target1": t.get("target1", 0),
        "target2": t.get("target2", 0),
        "risk_usd": t.get("risk_usd", RISK_PER_TRADE),
        "status": "open",
        "price_history": [{"price": entry_price, "time_et": _fmt(_now_et())}],
    }
    trades["positions"][ticker] = position
    print(f"  📋 [{ticker}] Paper short opened @ ${entry_price} | Stop:${t.get('stop')} T1:${t.get('target1')}")


def _monitor_open_positions(trades: dict) -> None:
    """Check open positions for stop/target exits."""
    for ticker in list(trades["positions"].keys()):
        position = trades["positions"][ticker]
        price = fetch_current_price(ticker)
        if price is None:
            continue

        position["price_history"].append({"price": price, "time_et": _fmt(_now_et())})
        position["last_price"] = price

        unrealized = round(position["shares"] * (position["entry_price"] - price), 2)
        print(f"  [{ticker}] ${price} | Unrealized: ${unrealized}")

        # Check exits
        if price >= position["stop_loss"]:
            c = _close_position(position, price, "STOP_LOSS", trades)
            _send_telegram(
                f"❌ *ORF Stop Loss — {ticker}*\n"
                f"Exit @ ${price} | P&L: ${c['pnl_usd']} ({c['pnl_pct']}%)"
            )
        elif price <= position["target1"]:
            c = _close_position(position, price, "TARGET_1", trades)
            _send_telegram(
                f"✅ *ORF Target Hit — {ticker}*\n"
                f"T1 @ ${price} | P&L: ${c['pnl_usd']} ({c['pnl_pct']}%)"
            )

        time.sleep(0.3)


def _close_position(position: dict, exit_price: float, exit_reason: str, trades: dict) -> dict:
    entry = position["entry_price"]
    shares = position["shares"]
    pnl = round(shares * (entry - exit_price), 2)
    pnl_pct = round((entry - exit_price) / entry * 100, 2) if entry else 0
    closed = {
        **position,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_time_et": _fmt(_now_et()),
        "exit_time_sgt": _fmt(_utc_to_sgt(_now_utc())),
        "pnl_usd": pnl,
        "pnl_pct": pnl_pct,
        "outcome": "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT",
    }
    trades["closed_trades"].append(closed)
    del trades["positions"][position["ticker"]]
    return closed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ORF Scanner")
    parser.add_argument(
        "--mode",
        choices=["scan", "monitor", "close", "summary"],
        required=True,
    )
    args = parser.parse_args()

    {"scan": mode_scan, "monitor": mode_monitor,
     "close": mode_close, "summary": mode_summary}[args.mode]()


if __name__ == "__main__":
    main()
