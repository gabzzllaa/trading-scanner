#!/usr/bin/env python3
"""
Earnings Fade Scanner
----------------------
Identifies stocks that gap up on earnings releases but show "sell the news"
dynamics — beat on weak guidance, one-time items, or already-priced-in results.
Trapped buyers from the pre-market spike are forced out as the stock fades
back toward the prior close by 11 AM ET.

Strategy Logic:
  1. Pre-market (6–9 AM ET): Pull today's earnings calendar, cross-reference
     with pre-market gappers. Score each qualifying stock.
  2. 9:30 AM ET (open): Record actual open price vs pre-market high.
  3. 10:00 AM ET (monitor): Check if stock is fading below open price — entry signal.
  4. 10:30 AM ET (monitor): Track open positions, check exits.
  5. 11:00 AM ET (close): Time stop — close all positions.
  6. 11:30 AM ET (summary): P&L report via Telegram.

Scoring (each /10, total /60):
  1. Pre-market gap size (5–20% sweet spot; >25% = overextended)
  2. Earnings quality — ALWAYS MANUAL (fixed 5/10 placeholder)
  3. Pre-market volume vs ADV (≥3× confirms retail over-reaction)
  4. Stock profile — price $5–$50, market cap $500M–$10B
  5. Prior trend — near 52-week high (more sellers ready to exit)
  6. Open reaction — checked at monitor phase (fading below open immediately)

Tiers:
  ≥35 → A+ setup
  20–34 → Monitor
  <20 → Skip

Hard skip rule: EPS beat + revenue beat + raised guidance = DO NOT TRADE.

Usage:
  python earnings_scanner.py --mode scan      # 6–9 AM ET: pull earnings, score gappers
  python earnings_scanner.py --mode open      # 9:30 AM ET: record open prices
  python earnings_scanner.py --mode monitor   # 10:00 & 10:30 AM ET: check fade entries
  python earnings_scanner.py --mode close     # 11:00 AM ET: time stop
  python earnings_scanner.py --mode summary   # 11:30 AM ET: P&L report

SGT equivalents:
  scan:    6:00–9:00 PM SGT
  open:    9:30 PM SGT
  monitor: 10:00 PM, 10:30 PM SGT
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
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).parent.resolve()
DATA_DIR     = SCRIPT_DIR / "data"
HISTORY_DIR  = DATA_DIR / "history"
CONFIG_FILE  = SCRIPT_DIR.parent / "config.yaml"

EF_STATE_FILE  = DATA_DIR / "earnings_state.json"
EF_TRADES_FILE = DATA_DIR / "earnings_paper_trades.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config.yaml. Falls back to built-in defaults if file is missing."""
    if CONFIG_FILE.exists():
        try:
            import yaml
            with open(CONFIG_FILE) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"  [!] Could not load config.yaml: {e} — using defaults")
    return {}

_CFG = _load_config()
_EF  = _CFG.get("earnings_fade", {})

def _g(key, default):
    return _CFG.get(key, default)

def _e(key, default):
    return _EF.get(key, default)

# ---------------------------------------------------------------------------
# Constants (read from config.yaml → earnings_fade section)
# ---------------------------------------------------------------------------
CAPITAL        = _g("capital_usd",        10_000)
RISK_PER_TRADE = CAPITAL * _g("risk_per_trade_pct", 0.01)
STOP_PCT       = _e("stop_loss_pct",      0.05)   # 5% above pre-market high
MIN_GAP_PCT    = _e("min_gap_pct",        5.0)    # min pre-market gap % to scan
TIME_STOP_ET   = "11:00 AM ET"
TIME_STOP_SGT  = "11:00 PM SGT"

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

def _yesterday_et() -> str:
    return (_now_et() - timedelta(days=1)).strftime("%Y-%m-%d")


# ===========================================================================
# DATA FETCHING
# ===========================================================================

def fetch_earnings_calendar() -> list[str]:
    """
    Pull today's earnings announcements from the Nasdaq earnings calendar API.
    Returns a list of tickers that reported after-hours yesterday or
    before-hours today (i.e. results are available pre-market this morning).

    Endpoint: https://api.nasdaq.com/api/calendar/earnings
    Returns up to 200 results. Free, no API key required.
    """
    today    = _today_et()
    yesterday = _yesterday_et()

    tickers = set()

    for date_str in [today, yesterday]:
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
        try:
            resp = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=15)
            if not resp.ok:
                print(f"  [!] Nasdaq earnings API {resp.status_code} for {date_str}")
                continue
            data = resp.json()
            rows = (
                data.get("data", {}).get("rows", [])
                or data.get("data", {}).get("earning", {}).get("rows", [])
                or []
            )
            for row in rows:
                # time field: "time" or "eps_time" — "After Market Close" or "Before Open"
                report_time = (row.get("time") or row.get("eps_time") or "").lower()
                ticker = (row.get("symbol") or row.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                # Include AMC from yesterday and BMO from today
                if date_str == yesterday and "after" in report_time:
                    tickers.add(ticker)
                elif date_str == today and ("before" in report_time or "pre" in report_time):
                    tickers.add(ticker)
        except Exception as e:
            print(f"  [!] Earnings calendar fetch error ({date_str}): {e}")

    print(f"  📅 Earnings calendar: {len(tickers)} stocks reporting today")
    return sorted(tickers)


def fetch_premarket_data(tickers: list[str]) -> list[dict]:
    """
    For a list of earnings tickers, fetch pre-market prices from TradingView
    and compute gap % from prior close. Returns only stocks gapping up ≥ MIN_GAP_PCT.
    Batches in groups of 50 to avoid oversized payloads.
    """
    if not tickers:
        return []

    print(f"  📡 TradingView — fetching pre-market prices for {len(tickers)} earnings tickers...")
    url = "https://scanner.tradingview.com/america/scan"
    headers = {
        **HEADERS,
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "Content-Type": "application/json",
    }

    results = []
    # TradingView accepts ticker prefixes — try common exchanges
    tv_tickers = []
    for t in tickers:
        tv_tickers += [f"NASDAQ:{t}", f"NYSE:{t}", f"AMEX:{t}"]

    # Batch into groups of 150 to stay within payload limits
    batch_size = 150
    for i in range(0, len(tv_tickers), batch_size):
        batch = tv_tickers[i:i + batch_size]
        payload = {
            "markets": ["america"],
            "symbols": {"query": {"types": ["stock"]}, "tickers": batch},
            "options": {"lang": "en"},
            "columns": [
                "name",
                "close",                    # prev day close
                "premarket_close",          # current pre-market price
                "premarket_volume",         # pre-market volume
                "average_volume_10d_calc",  # 10-day ADV
                "market_cap_basic",         # market cap
                "52_week_high",             # 52W high
                "lp",                       # last price (fallback)
            ],
            "filter": [
                {"left": "is_primary", "operation": "equal", "right": True},
            ],
            "sort": {"sortBy": "premarket_volume", "sortOrder": "desc"},
            "range": [0, 200],
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=20)
            if not resp.ok:
                print(f"  [!] TradingView error {resp.status_code}: {resp.text[:300]}")
                continue
            data = resp.json()
        except Exception as e:
            print(f"  [!] TradingView fetch error: {e}")
            continue

        for item in data.get("data", []):
            d = item.get("d", [])
            if len(d) < 8:
                continue
            name, prev_close, pm_price, pm_vol, avg_vol, mkt_cap, w52_high, lp = d[:8]

            if not name:
                continue
            ticker = name.split(":")[1] if ":" in name else name

            # Use premarket price if available, else last price
            pm_p = float(pm_price) if pm_price else (float(lp) if lp else None)
            prev_c = float(prev_close) if prev_close else None

            if not pm_p or not prev_c or prev_c <= 0:
                continue

            gap_pct = (pm_p - prev_c) / prev_c * 100

            # Only upside gaps above threshold
            if gap_pct < MIN_GAP_PCT:
                continue

            # Only stocks that are actually in our earnings list
            if ticker.upper() not in [t.upper() for t in tickers]:
                continue

            mkt_cap_m = float(mkt_cap) / 1_000_000 if mkt_cap else None
            w52h      = float(w52_high) if w52_high else None
            adv       = float(avg_vol)  if avg_vol  else None
            pm_volume = float(pm_vol)   if pm_vol   else None

            # Near 52W high: how close is prev_close to 52W high (%)
            near_high_pct = None
            if w52h and w52h > 0:
                near_high_pct = (w52h - prev_c) / w52h * 100  # 0% = at high

            results.append({
                "ticker":         ticker.upper(),
                "prev_close":     round(prev_c, 4),
                "pm_price":       round(pm_p,   4),
                "gap_pct":        round(gap_pct, 2),
                "pm_volume":      pm_volume,
                "avg_daily_vol":  adv,
                "market_cap_m":   round(mkt_cap_m, 2) if mkt_cap_m else None,
                "w52_high":       round(w52h, 4) if w52h else None,
                "near_high_pct":  round(near_high_pct, 2) if near_high_pct is not None else None,
                # Runtime fields (filled in later)
                "open_price":     None,
                "open_reaction":  None,    # % below pm_price at open
                "entry_price":    None,
                "fade_triggered": False,
            })

    # Deduplicate by ticker (keep highest gap_pct)
    seen = {}
    for r in results:
        t = r["ticker"]
        if t not in seen or r["gap_pct"] > seen[t]["gap_pct"]:
            seen[t] = r
    results = list(seen.values())
    results.sort(key=lambda x: x["gap_pct"], reverse=True)

    print(f"  Found {len(results)} earnings gapper(s) with gap ≥ {MIN_GAP_PCT}%")
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
        "columns": ["name", "lp"],
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
        print(f"  [!] Price fetch error for {ticker}: {e}")
    return None


# ===========================================================================
# SCORING
# ===========================================================================

def score_earnings(candidate: dict) -> dict:
    """Score a candidate on all 6 Earnings Fade conditions."""

    # C1: Pre-market gap size — sweet spot 5–20%
    # Thresholds from config.yaml → earnings_fade.c1_gap_*
    gap = candidate.get("gap_pct", 0)
    t1 = _e("c1_gap_10", 20)
    t2 = _e("c1_gap_8",  15)
    t3 = _e("c1_gap_7",  10)
    t4 = _e("c1_gap_5",   7)
    t5 = _e("c1_gap_3",   5)
    if gap >= t1:        c1 = 10
    elif gap >= t2:      c1 = 8
    elif gap >= t3:      c1 = 7
    elif gap >= t4:      c1 = 5
    elif gap >= t5:      c1 = 3
    else:                c1 = 1

    # C2: Earnings quality — ALWAYS manual, fixed 5/10 placeholder
    c2 = 5

    # C3: Pre-market volume vs ADV
    # Thresholds from config.yaml → earnings_fade.c3_vol_*
    pm_vol  = candidate.get("pm_volume")  or 0
    avg_vol = candidate.get("avg_daily_vol") or 0
    t3_1 = _e("c3_vol_10", 10)
    t3_2 = _e("c3_vol_8",   7)
    t3_3 = _e("c3_vol_7",   5)
    t3_4 = _e("c3_vol_5",   3)
    t3_5 = _e("c3_vol_3",   2)
    if avg_vol > 0:
        vol_ratio = pm_vol / avg_vol
        if vol_ratio >= t3_1:        c3 = 10
        elif vol_ratio >= t3_2:      c3 = 8
        elif vol_ratio >= t3_3:      c3 = 7
        elif vol_ratio >= t3_4:      c3 = 5
        elif vol_ratio >= t3_5:      c3 = 3
        else:                        c3 = 1
    else:
        c3 = 3  # unknown volume

    # C4: Stock profile — price $5–$50, market cap $500M–$10B
    # Thresholds from config.yaml → earnings_fade.c4_*
    price     = candidate.get("pm_price")    or 0
    mkt_cap_m = candidate.get("market_cap_m") or 0
    min_p  = _e("c4_min_price",     5)
    max_p  = _e("c4_max_price",    50)
    min_mc = _e("c4_min_mktcap",  500)
    max_mc = _e("c4_max_mktcap", 10000)
    price_ok  = min_p  <= price     <= max_p
    mktcap_ok = min_mc <= mkt_cap_m <= max_mc if mkt_cap_m else False
    if price_ok and mktcap_ok:    c4 = 10
    elif price_ok or mktcap_ok:   c4 = 5
    else:                          c4 = 1

    # C5: Prior trend — near 52-week high
    # Thresholds from config.yaml → earnings_fade.c5_near_high_*
    near_h = candidate.get("near_high_pct")  # 0% = exactly at 52W high
    t5_1 = _e("c5_near_high_10",  5)
    t5_2 = _e("c5_near_high_8",  10)
    t5_3 = _e("c5_near_high_6",  20)
    t5_4 = _e("c5_near_high_3",  30)
    if near_h is None:                c5 = 3  # unknown
    elif near_h <= t5_1:              c5 = 10
    elif near_h <= t5_2:              c5 = 8
    elif near_h <= t5_3:              c5 = 6
    elif near_h <= t5_4:              c5 = 3
    else:                             c5 = 1

    # C6: Open reaction — how far below PM high did price open?
    # Scored at open/monitor phase. At scan time, placeholder 5/10.
    open_reaction = candidate.get("open_reaction")
    t6_1 = _e("c6_below_pm_10", 3)
    t6_2 = _e("c6_below_pm_7",  1)
    if open_reaction is None:
        c6 = 5  # placeholder until open price is recorded
    elif open_reaction >= t6_1:    c6 = 10
    elif open_reaction >= t6_2:    c6 = 7
    elif open_reaction >= 0:       c6 = 4
    else:                          c6 = 1  # opened above PM high (still surging)

    total = c1 + c2 + c3 + c4 + c5 + c6
    _aplus   = _e("tier_a_plus_min",  35)
    _monitor = _e("tier_monitor_min", 20)
    tier = "A+" if total >= _aplus else "Monitor" if total >= _monitor else "Skip"

    candidate["scores"] = {
        "c1_gap_size":           c1,
        "c2_earnings_MANUAL":    c2,
        "c3_pm_volume":          c3,
        "c4_stock_profile":      c4,
        "c5_prior_trend":        c5,
        "c6_open_reaction":      c6,
        "total":                 total,
        "tier":                  tier,
    }
    candidate["total_score"] = total
    candidate["tier"] = tier

    # Trade parameters for qualifying setups
    if tier in ("A+", "Monitor"):
        pm_p  = candidate["pm_price"]
        prev_c = candidate["prev_close"]

        stop          = round(pm_p * (1 + STOP_PCT), 4)
        stop_distance = stop - pm_p
        shares        = int(RISK_PER_TRADE / stop_distance) if stop_distance > 0 else 0
        gap_amount    = pm_p - prev_c
        target1       = round(pm_p - gap_amount * 0.5, 4)   # 50% gap fill
        target2       = round(prev_c, 4)                      # full gap fill

        reward_t1 = round(shares * (pm_p - target1), 2) if shares else 0
        reward_t2 = round(shares * (pm_p - target2), 2) if shares else 0
        rr        = round(reward_t1 / RISK_PER_TRADE, 2)  if RISK_PER_TRADE > 0 else 0

        candidate["trade"] = {
            "entry":         pm_p,
            "stop":          stop,
            "stop_distance": round(stop_distance, 4),
            "target1":       target1,
            "target2":       target2,
            "shares":        shares,
            "risk_usd":      RISK_PER_TRADE,
            "reward_t1":     reward_t1,
            "reward_t2":     reward_t2,
            "rr_ratio":      rr,
            "time_stop_et":  TIME_STOP_ET,
            "time_stop_sgt": TIME_STOP_SGT,
        }

    return candidate


# ===========================================================================
# STATE MANAGEMENT
# ===========================================================================

def _load_state() -> dict:
    today = _today_et()
    if EF_STATE_FILE.exists():
        with open(EF_STATE_FILE) as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
        _archive_file(EF_STATE_FILE, f"ef_state_{state.get('date','unknown')}.json")
    return {"date": today, "candidates": {}, "scanned_at": None}

def _save_state(state: dict) -> None:
    with open(EF_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

def _load_trades() -> dict:
    today = _today_et()
    if EF_TRADES_FILE.exists():
        with open(EF_TRADES_FILE) as f:
            data = json.load(f)
        if data.get("date") == today:
            return data
        _archive_file(EF_TRADES_FILE, f"ef_trades_{data.get('date','unknown')}.json")
    return {
        "date":          today,
        "strategy":      "Earnings Fade",
        "capital":       CAPITAL,
        "positions":     {},
        "closed_trades": [],
        "summary":       None,
    }

def _save_trades(data: dict) -> None:
    with open(EF_TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

def _archive_file(src: Path, dest_name: str) -> None:
    dest = HISTORY_DIR / dest_name
    if src.exists():
        with open(src) as f:
            data = json.load(f)
        with open(dest, "w") as f:
            json.dump(data, f, indent=2, default=str)


# ===========================================================================
# TELEGRAM
# ===========================================================================

def _send_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
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
    if not a_plus and not monitor:
        print("  [Telegram] No qualifying Earnings Fade setups — skipping.")
        return

    now_str = _fmt_et_sgt()

    if not a_plus:
        lines = [
            f"👀 *Earnings Fade — {now_str}*",
            f"{len(monitor)} Monitor setup(s), no A+.",
            "",
        ]
        for c in monitor[:3]:
            lines.append(
                f"  • *{c['ticker']}*  Gap: +{c['gap_pct']}%  "
                f"PM: ${c['pm_price']}  Score: {c['total_score']}/60"
            )
        lines.append("\n_Watch at open. Verify earnings quality manually._")
        _send_telegram("\n".join(lines))
        return

    lines = [
        f"🚨 *EARNINGS FADE ALERT* — {now_str}",
        f"*{len(a_plus)} A+ setup(s)* — Sell The News",
        "",
    ]
    for c in a_plus:
        t  = c.get("trade", {})
        sc = c.get("scores", {})
        lines += [
            f"💰 *{c['ticker']}*  |  Score: {c['total_score']}/60  |  Tier: A+",
            f"  Gap: +{c['gap_pct']}%  |  PM Price: ${c['pm_price']}  |  Prev Close: ${c['prev_close']}",
            f"  Mkt Cap: ${c.get('market_cap_m','?')}M  |  Near 52W High: {c.get('near_high_pct','?')}% below",
            f"  C1(gap):{sc.get('c1_gap_size')} C2(earnings):{sc.get('c2_earnings_MANUAL')}⚠️manual "
            f"C3(vol):{sc.get('c3_pm_volume')} C4(profile):{sc.get('c4_stock_profile')} "
            f"C5(trend):{sc.get('c5_prior_trend')} C6(open):{sc.get('c6_open_reaction')}⏳",
            f"  Entry (short near open): ${t.get('entry','?')}",
            f"  Stop: ${t.get('stop','?')} (+{STOP_PCT*100:.0f}% above PM high)",
            f"  Target 1 (50% fill): ${t.get('target1','?')}  |  Target 2 (full fill): ${t.get('target2','?')}",
            f"  Shares: {t.get('shares','?')}  |  Risk: ${t.get('risk_usd','?')}  |  R/R: {t.get('rr_ratio','?')}:1",
            f"  🔗 Earnings: https://stockanalysis.com/stocks/{c['ticker'].lower()}/financials/",
            f"  🔗 Stocktwits: https://stocktwits.com/symbol/{c['ticker']}",
            f"  🔗 Reddit: https://www.reddit.com/search/?q={c['ticker']}",
            "",
        ]
    if monitor:
        lines.append(f"👀 Also watching: {', '.join(c['ticker'] for c in monitor[:5])}")
        lines.append("")
    lines += [
        "⚠️ *C2 (Earnings Quality) MUST be verified manually before trading.*",
        "⚠️ *Clean beat (EPS + revenue + raised guidance) → DO NOT TRADE.*",
        "⚠️ *C6 (Open Reaction) updated at 10:00 PM SGT / 10:00 AM ET monitor.*",
        f"⚠️ *Time stop: {TIME_STOP_SGT} / {TIME_STOP_ET}.*",
    ]
    _send_telegram("\n".join(lines))


def _send_monitor_alert(ticker: str, candidate: dict, action: str) -> None:
    t  = candidate.get("trade", {})
    sc = candidate.get("scores", {})
    price = candidate.get("entry_price") or t.get("entry", "?")
    lines = [
        f"📉 *EARNINGS FADE — {action}* — {_fmt_et_sgt()}",
        f"*{ticker}*  Score: {candidate['total_score']}/60  Tier: {candidate['tier']}",
        f"  Gap: +{candidate['gap_pct']}%  |  PM High: ${candidate['pm_price']}",
        f"  Open reaction: {candidate.get('open_reaction','?')}% below PM high  (C6 updated: {sc.get('c6_open_reaction','?')}pts)",
        f"  Entry: ${price}  |  Stop: ${t.get('stop','?')}  |  T1: ${t.get('target1','?')}  |  T2: ${t.get('target2','?')}",
        f"  Shares: {t.get('shares','?')}  |  Risk: ${t.get('risk_usd','?')}",
        "",
        "⚠️ *Confirm earnings quality is weak before entering.*",
        "🚫 *Skip if EPS + revenue + guidance all beat.*",
    ]
    _send_telegram("\n".join(lines))


def _send_summary(trades: dict) -> None:
    closed  = trades.get("closed_trades", [])
    open_p  = trades.get("positions", {})
    now_str = _fmt_et_sgt()

    total_pnl   = sum(t.get("pnl_usd", 0)  for t in closed)
    win_count   = sum(1 for t in closed if t.get("pnl_usd", 0) > 0)
    loss_count  = sum(1 for t in closed if t.get("pnl_usd", 0) <= 0)

    lines = [
        f"📊 *Earnings Fade — Daily Summary — {now_str}*",
        f"  Closed trades: {len(closed)}  |  Wins: {win_count}  Losses: {loss_count}",
        f"  Total P&L: ${total_pnl:+.2f}",
        "",
    ]
    for t in closed:
        pnl = t.get("pnl_usd", 0)
        emoji = "✅" if pnl > 0 else "❌"
        lines.append(
            f"  {emoji} *{t['ticker']}*  Entry: ${t.get('entry_price','?')}  "
            f"Exit: ${t.get('exit_price','?')}  P&L: ${pnl:+.2f}  "
            f"Reason: {t.get('exit_reason','?')}"
        )
    if open_p:
        lines += ["", f"  ⚠️ {len(open_p)} position(s) still open — manual close required."]
        for ticker in open_p:
            lines.append(f"    • {ticker}")
    if not closed and not open_p:
        lines.append("  No trades today.")
    _send_telegram("\n".join(lines))


# ===========================================================================
# PAPER TRADING HELPERS
# ===========================================================================

def _open_paper_position(ticker: str, candidate: dict, trades: dict) -> None:
    if ticker in trades["positions"]:
        return
    t = candidate.get("trade", {})
    entry = candidate.get("entry_price") or candidate.get("pm_price")
    trades["positions"][ticker] = {
        "ticker":      ticker,
        "entry_price": entry,
        "entry_time":  _fmt_et_sgt(),
        "stop":        t.get("stop"),
        "target1":     t.get("target1"),
        "target2":     t.get("target2"),
        "shares":      t.get("shares", 0),
        "risk_usd":    t.get("risk_usd", RISK_PER_TRADE),
        "hit_target1": False,
        "strategy":    "Earnings Fade",
    }
    print(f"  [PAPER] Opened short: {ticker} @ ${entry}")


def _check_exits(trades: dict, state: dict, force_close: bool = False) -> None:
    """Check stop/target/time-stop exits for all open positions."""
    positions = trades.get("positions", {})
    closed    = trades.get("closed_trades", [])
    to_close  = []

    for ticker, pos in positions.items():
        price = fetch_current_price(ticker)
        if price is None:
            print(f"  [{ticker}] Could not fetch price for exit check.")
            continue

        entry  = pos["entry_price"]
        stop   = pos["stop"]
        t1     = pos["target1"]
        shares = pos.get("shares", 0)

        reason = None
        exit_p = price

        if force_close:
            reason = "time_stop"
        elif price >= stop:
            reason = "stop_loss"
            exit_p = stop
        elif not pos.get("hit_target1") and price <= t1:
            pos["hit_target1"] = True
            print(f"  [{ticker}] ✅ Target 1 hit @ ${price:.4f} — holding for Target 2")
            continue
        elif pos.get("hit_target1") and price <= pos["target2"]:
            reason = "target2"
            exit_p = pos["target2"]

        if reason:
            pnl = round((entry - exit_p) * shares, 2)
            closed.append({
                "ticker":      ticker,
                "entry_price": entry,
                "exit_price":  round(exit_p, 4),
                "shares":      shares,
                "pnl_usd":     pnl,
                "exit_reason": reason,
                "exit_time":   _fmt_et_sgt(),
                "strategy":    "Earnings Fade",
            })
            to_close.append(ticker)
            emoji = "✅" if pnl > 0 else "❌"
            print(f"  [{ticker}] {emoji} Closed — Reason: {reason} | Exit: ${exit_p:.4f} | P&L: ${pnl:+.2f}")
            _send_telegram(
                f"{emoji} *Earnings Fade EXIT* — {_fmt_et_sgt()}\n"
                f"*{ticker}*  Reason: `{reason}`  Entry: ${entry}  Exit: ${exit_p:.4f}  P&L: ${pnl:+.2f}"
            )

    for t in to_close:
        del positions[t]

    trades["positions"]     = positions
    trades["closed_trades"] = closed


# ===========================================================================
# RUN MODES
# ===========================================================================

def mode_scan():
    """
    6–9 AM ET: Pull earnings calendar, fetch pre-market prices, score gappers.
    """
    print(f"\n{'='*60}")
    print(f"  EARNINGS FADE — PRE-MARKET SCAN — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    # Pull earnings tickers
    tickers = fetch_earnings_calendar()
    if not tickers:
        print("  No earnings tickers found for today.")
        return

    # Fetch pre-market prices and filter by gap
    candidates = fetch_premarket_data(tickers)
    if not candidates:
        print(f"  No earnings gappers ≥{MIN_GAP_PCT}% found.")
        return

    # Score
    scored = [score_earnings(c) for c in candidates]
    scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    a_plus  = [s for s in scored if s.get("tier") == "A+"]
    monitor = [s for s in scored if s.get("tier") == "Monitor"]

    print(f"\n  RESULTS: {len(a_plus)} A+ | {len(monitor)} Monitor | {len(scored)-len(a_plus)-len(monitor)} Skip")

    if a_plus:
        print("\n🔥 A+ SETUPS:")
        for c in a_plus:
            sc = c.get("scores", {})
            t  = c.get("trade", {})
            print(f"\n  {c['ticker']}  Gap:+{c['gap_pct']}%  PM:${c['pm_price']}  Score:{c['total_score']}/60")
            print(f"  C1:{sc.get('c1_gap_size')} C2:{sc.get('c2_earnings_MANUAL')}(manual) "
                  f"C3:{sc.get('c3_pm_volume')} C4:{sc.get('c4_stock_profile')} "
                  f"C5:{sc.get('c5_prior_trend')} C6:{sc.get('c6_open_reaction')}(pending open)")
            print(f"  Entry:${t.get('entry')} Stop:${t.get('stop')} "
                  f"T1:${t.get('target1')} T2:${t.get('target2')}")
            print(f"  Shares:{t.get('shares')} Risk:${t.get('risk_usd')} R/R:{t.get('rr_ratio')}:1")
            print(f"  ⚠️  Verify earnings: https://stockanalysis.com/stocks/{c['ticker'].lower()}/financials/")

    if monitor:
        print("\n👀 MONITOR:")
        for c in monitor[:5]:
            print(f"  {c['ticker']:6s}  Gap:+{c['gap_pct']}%  PM:${c['pm_price']}  Score:{c['total_score']}/60")

    # Save state
    state = _load_state()
    state["scanned_at"]  = _fmt_et_sgt()
    state["candidates"]  = {c["ticker"]: c for c in scored}
    state["earnings_tickers"] = tickers
    _save_state(state)

    # Archive
    hist_file = HISTORY_DIR / f"ef_scan_{_now_et().strftime('%Y%m%d_%H%M%S')}.json"
    with open(hist_file, "w") as f:
        json.dump({"scanned_at": state["scanned_at"], "candidates": scored}, f, indent=2, default=str)

    # Alert
    _send_scan_alert(a_plus, monitor)


def mode_open():
    """
    9:30 AM ET: Record the actual open price for each candidate.
    Compute open reaction (how much below PM high did it open).
    Update C6 score accordingly.
    """
    print(f"\n{'='*60}")
    print(f"  EARNINGS FADE — MARKET OPEN — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state = _load_state()
    candidates = state.get("candidates", {})
    if not candidates:
        print("  No candidates in state — run scan first.")
        return

    qualifying = {t: c for t, c in candidates.items() if c.get("tier") in ("A+", "Monitor")}
    print(f"  Checking open prices for {len(qualifying)} qualifying candidate(s)...")

    updated = 0
    for ticker, candidate in qualifying.items():
        time.sleep(0.5)  # Rate limit
        price = fetch_current_price(ticker)
        if price is None:
            print(f"  [{ticker}] Could not fetch open price.")
            continue

        pm_p = candidate["pm_price"]
        # Open reaction: how far below PM high (positive = fading, negative = still surging)
        open_reaction = round((pm_p - price) / pm_p * 100, 2)
        candidate["open_price"]    = round(price, 4)
        candidate["open_reaction"] = open_reaction

        # Re-score C6 now that we have open data
        t6_1 = _e("c6_below_pm_10", 3)
        t6_2 = _e("c6_below_pm_7",  1)
        if open_reaction >= t6_1:       c6_new = 10
        elif open_reaction >= t6_2:     c6_new = 7
        elif open_reaction >= 0:        c6_new = 4
        else:                           c6_new = 1

        old_c6 = candidate.get("scores", {}).get("c6_open_reaction", 5)
        candidate["scores"]["c6_open_reaction"] = c6_new
        new_total = candidate["total_score"] - old_c6 + c6_new
        candidate["total_score"] = new_total
        candidate["scores"]["total"] = new_total

        # Re-tier
        _aplus   = _e("tier_a_plus_min",  35)
        _monitor = _e("tier_monitor_min", 20)
        candidate["tier"] = "A+" if new_total >= _aplus else "Monitor" if new_total >= _monitor else "Skip"
        candidate["scores"]["tier"] = candidate["tier"]

        updated += 1
        direction = "▼" if open_reaction > 0 else "▲"
        print(f"  [{ticker}] Open: ${price:.2f}  PM: ${pm_p}  "
              f"Reaction: {direction}{abs(open_reaction):.1f}%  "
              f"C6: {old_c6}→{c6_new}  Score: {new_total}/60  Tier: {candidate['tier']}")

    state["candidates"] = {**candidates, **qualifying}
    state["open_checked_at"] = _fmt_et_sgt()
    _save_state(state)
    print(f"\n  Updated {updated} candidate(s) with open prices.")


def mode_monitor():
    """
    10:00 & 10:30 AM ET: Check for fade entries and manage open positions.
    Entry condition: price drops below open price (confirming sellers in control).
    """
    print(f"\n{'='*60}")
    print(f"  EARNINGS FADE — MONITORING — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state  = _load_state()
    trades = _load_trades()
    candidates = state.get("candidates", {})

    if not candidates:
        print("  No candidates in state — run scan first.")
        return

    # Check for new fade entries
    for ticker, candidate in list(candidates.items()):
        if candidate.get("fade_triggered"):
            continue
        if candidate.get("tier") not in ("A+", "Monitor"):
            continue
        if ticker in trades["positions"]:
            continue

        open_p = candidate.get("open_price")
        if not open_p:
            print(f"  [{ticker}] No open price recorded — skipping (run open mode first).")
            continue

        print(f"  [{ticker}] Checking fade (open: ${open_p})...")
        price = fetch_current_price(ticker)
        if price is None:
            print(f"  [{ticker}] Could not fetch price.")
            continue

        # Entry: price below open price confirms sellers are in control
        if price < open_p:
            fade_pct = round((open_p - price) / open_p * 100, 2)
            print(f"  [{ticker}] 🚨 FADE TRIGGERED — price ${price:.4f} is {fade_pct}% below open ${open_p}")
            candidate["fade_triggered"] = True
            candidate["entry_price"]    = round(price, 4)
            _open_paper_position(ticker, candidate, trades)
            _send_monitor_alert(ticker, candidate, "FADE ENTRY")
        else:
            print(f"  [{ticker}] No fade yet — price ${price:.4f} (open: ${open_p})")

    # Check exits for already-open positions
    if trades["positions"]:
        print(f"\n  Checking exits for {len(trades['positions'])} open position(s)...")
        _check_exits(trades, state)

    state["candidates"] = candidates
    _save_state(state)
    _save_trades(trades)


def mode_close():
    """
    11:00 AM ET: Time stop — force-close all open positions.
    """
    print(f"\n{'='*60}")
    print(f"  EARNINGS FADE — TIME STOP — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    state  = _load_state()
    trades = _load_trades()

    if not trades["positions"]:
        print("  No open positions to close.")
    else:
        print(f"  Force-closing {len(trades['positions'])} position(s)...")
        _check_exits(trades, state, force_close=True)
        _save_trades(trades)
        _save_state(state)

    print("  ✅ All positions closed.")


def mode_summary():
    """
    11:30 AM ET: Send daily P&L summary via Telegram.
    """
    print(f"\n{'='*60}")
    print(f"  EARNINGS FADE — DAILY SUMMARY — {_fmt_et_sgt()}")
    print(f"{'='*60}")

    trades = _load_trades()
    closed = trades.get("closed_trades", [])
    total_pnl  = sum(t.get("pnl_usd", 0) for t in closed)
    win_count  = sum(1 for t in closed if t.get("pnl_usd", 0) > 0)

    print(f"  Trades today: {len(closed)}  Wins: {win_count}  Total P&L: ${total_pnl:+.2f}")
    for t in closed:
        pnl   = t.get("pnl_usd", 0)
        emoji = "✅" if pnl > 0 else "❌"
        print(f"  {emoji} {t['ticker']}  P&L: ${pnl:+.2f}  ({t.get('exit_reason','')})")

    trades["summary"] = {
        "total_pnl":  total_pnl,
        "win_count":  win_count,
        "loss_count": len(closed) - win_count,
        "trade_count": len(closed),
        "generated_at": _fmt_et_sgt(),
    }
    _save_trades(trades)
    _send_summary(trades)


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Earnings Fade Scanner")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["scan", "open", "monitor", "close", "summary"],
        help="scan | open | monitor | close | summary",
    )
    args = parser.parse_args()

    modes = {
        "scan":    mode_scan,
        "open":    mode_open,
        "monitor": mode_monitor,
        "close":   mode_close,
        "summary": mode_summary,
    }
    modes[args.mode]()


if __name__ == "__main__":
    main()
