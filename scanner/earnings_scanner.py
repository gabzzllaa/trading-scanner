#!/usr/bin/env python3
"""
Earnings Fade Scanner
----------------------
SCAN ONLY - no paper trading, no position management.
Trading is handled exclusively by alpaca_agent.py.

Identifies stocks that gap up on earnings releases but show "sell the news"
dynamics - beat on weak guidance, one-time items, or already-priced-in results.

Strategy Logic:
  1. Pre-market (6-9 AM ET): Pull today's earnings calendar, cross-reference
     with pre-market gappers. Score each qualifying stock.
  2. Alert via Telegram for A+ and Monitor setups.
  3. Save scan state for the trading agent to consume.

Scoring (each /10, total /60):
  1. Pre-market gap size (5-20% sweet spot; >25% = overextended)
  2. Earnings quality - ALWAYS MANUAL (fixed 5/10 placeholder)
  3. Pre-market volume vs ADV (>=3x confirms retail over-reaction)
  4. Stock profile - price $5-$50, market cap $500M-$10B
  5. Prior trend - near 52-week high (more sellers ready to exit)
  6. Open reaction - placeholder at scan time, updated at monitor phase

Tiers:
  >=35 - A+ setup
  20-34 - Monitor
  <20 - Skip

Hard skip rule: EPS beat + revenue beat + raised guidance = DO NOT TRADE.

Usage:
  python earnings_scanner.py --mode scan    # 6-9 AM ET (6-9 PM SGT): scan + alert
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

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            import yaml
            with open(CONFIG_FILE) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"  [!] Could not load config.yaml: {e} - using defaults")
    return {}

_CFG = _load_config()
_EF  = _CFG.get("earnings_fade", {})

def _g(key, default):
    return _CFG.get(key, default)

def _e(key, default):
    return _EF.get(key, default)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CAPITAL        = _g("capital_usd",        10_000)
RISK_PER_TRADE = CAPITAL * _g("risk_per_trade_pct", 0.01)
STOP_PCT       = _e("stop_loss_pct",      0.05)
MIN_GAP_PCT    = _e("min_gap_pct",        5.0)
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

def fetch_earnings_calendar() -> list:
    """
    Pull today's earnings announcements from the Nasdaq earnings calendar API.
    Returns tickers that reported after-hours yesterday or before-hours today.
    """
    today     = _today_et()
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
                report_time = (row.get("time") or row.get("eps_time") or "").lower()
                ticker = (row.get("symbol") or row.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                if date_str == yesterday and "after" in report_time:
                    tickers.add(ticker)
                elif date_str == today and ("before" in report_time or "pre" in report_time):
                    tickers.add(ticker)
        except Exception as e:
            print(f"  [!] Earnings calendar fetch error ({date_str}): {e}")

    print(f"  Earnings calendar: {len(tickers)} stocks reporting today")
    return sorted(tickers)


def fetch_premarket_data(tickers: list) -> list:
    """
    Fetch pre-market prices for earnings tickers from TradingView.
    Returns only stocks gapping up >= MIN_GAP_PCT.
    """
    if not tickers:
        return []

    print(f"  TradingView - fetching pre-market prices for {len(tickers)} earnings tickers...")
    url = "https://scanner.tradingview.com/america/scan"
    headers = {
        **HEADERS,
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "Content-Type": "application/json",
    }

    results = []
    tv_tickers = []
    for t in tickers:
        tv_tickers += [f"NASDAQ:{t}", f"NYSE:{t}", f"AMEX:{t}"]

    batch_size = 150
    for i in range(0, len(tv_tickers), batch_size):
        batch = tv_tickers[i:i + batch_size]
        payload = {
            "markets": ["america"],
            "symbols": {"query": {"types": ["stock"]}, "tickers": batch},
            "options": {"lang": "en"},
            "columns": [
                "name",
                "close",
                "premarket_close",
                "premarket_volume",
                "average_volume_10d_calc",
                "market_cap_basic",
                "52_week_high",
                "close",
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
            name, prev_close, pm_price, pm_vol, avg_vol, mkt_cap, w52_high, last_price = d[:8]

            if not name:
                continue
            ticker = name.split(":")[1] if ":" in name else name

            pm_p = float(pm_price) if pm_price else (float(last_price) if last_price else None)
            prev_c = float(prev_close) if prev_close else None

            if not pm_p or not prev_c or prev_c <= 0:
                continue

            gap_pct = (pm_p - prev_c) / prev_c * 100

            if gap_pct < MIN_GAP_PCT:
                continue

            if ticker.upper() not in [t.upper() for t in tickers]:
                continue

            mkt_cap_m = float(mkt_cap) / 1_000_000 if mkt_cap else None
            w52h      = float(w52_high) if w52_high else None
            adv       = float(avg_vol)  if avg_vol  else None
            pm_volume = float(pm_vol)   if pm_vol   else None

            near_high_pct = None
            if w52h and w52h > 0:
                near_high_pct = (w52h - prev_c) / w52h * 100

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
                "open_price":     None,
                "open_reaction":  None,
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

    print(f"  Found {len(results)} earnings gapper(s) with gap >= {MIN_GAP_PCT}%")
    return results


# ===========================================================================
# SCORING
# ===========================================================================

def score_earnings(candidate: dict) -> dict:
    """Score a candidate on all 6 Earnings Fade conditions."""

    # C1: Pre-market gap size
    gap = candidate.get("gap_pct", 0)
    if gap >= _e("c1_gap_10", 20):       c1 = 10
    elif gap >= _e("c1_gap_8",  15):     c1 = 8
    elif gap >= _e("c1_gap_7",  10):     c1 = 7
    elif gap >= _e("c1_gap_5",   7):     c1 = 5
    elif gap >= _e("c1_gap_3",   5):     c1 = 3
    else:                                 c1 = 1

    # C2: Earnings quality - always manual
    c2 = 5

    # C3: Pre-market volume vs ADV
    pm_vol  = candidate.get("pm_volume")  or 0
    avg_vol = candidate.get("avg_daily_vol") or 0
    if avg_vol > 0:
        vol_ratio = pm_vol / avg_vol
        if vol_ratio >= _e("c3_vol_10", 10):       c3 = 10
        elif vol_ratio >= _e("c3_vol_8",   7):     c3 = 8
        elif vol_ratio >= _e("c3_vol_7",   5):     c3 = 7
        elif vol_ratio >= _e("c3_vol_5",   3):     c3 = 5
        elif vol_ratio >= _e("c3_vol_3",   2):     c3 = 3
        else:                                       c3 = 1
    else:
        c3 = 3

    # C4: Stock profile
    price     = candidate.get("pm_price")    or 0
    mkt_cap_m = candidate.get("market_cap_m") or 0
    price_ok  = _e("c4_min_price",    5) <= price     <= _e("c4_max_price",    50)
    mktcap_ok = _e("c4_min_mktcap", 500) <= mkt_cap_m <= _e("c4_max_mktcap", 10000) if mkt_cap_m else False
    if price_ok and mktcap_ok:    c4 = 10
    elif price_ok or mktcap_ok:   c4 = 5
    else:                          c4 = 1

    # C5: Prior trend - near 52-week high
    near_h = candidate.get("near_high_pct")
    if near_h is None:                          c5 = 3
    elif near_h <= _e("c5_near_high_10",  5):  c5 = 10
    elif near_h <= _e("c5_near_high_8",  10):  c5 = 8
    elif near_h <= _e("c5_near_high_6",  20):  c5 = 6
    elif near_h <= _e("c5_near_high_3",  30):  c5 = 3
    else:                                        c5 = 1

    # C6: Open reaction - placeholder at scan time
    c6 = 5

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

    # Trade parameters (for alert display only - NOT for paper trading)
    if tier in ("A+", "Monitor"):
        pm_p  = candidate["pm_price"]
        prev_c = candidate["prev_close"]

        stop          = round(pm_p * (1 + STOP_PCT), 4)
        stop_distance = stop - pm_p
        shares        = int(RISK_PER_TRADE / stop_distance) if stop_distance > 0 else 0
        gap_amount    = pm_p - prev_c
        target1       = round(pm_p - gap_amount * 0.5, 4)
        target2       = round(prev_c, 4)

        reward_t1 = round(shares * (pm_p - target1), 2) if shares else 0
        rr        = round(reward_t1 / RISK_PER_TRADE, 2) if RISK_PER_TRADE > 0 else 0

        candidate["trade"] = {
            "entry":         pm_p,
            "stop":          stop,
            "stop_distance": round(stop_distance, 4),
            "target1":       target1,
            "target2":       target2,
            "shares":        shares,
            "risk_usd":      RISK_PER_TRADE,
            "reward_t1":     reward_t1,
            "rr_ratio":      rr,
            "time_stop_et":  TIME_STOP_ET,
            "time_stop_sgt": TIME_STOP_SGT,
        }

    return candidate


# ===========================================================================
# SHORTABILITY CHECK
# ===========================================================================

def check_shortability(tickers: list) -> dict:
    """
    Check borrow availability via iborrowdesk.com (free, no auth).
    Returns { "TICKER": { "shortable": bool|None, "fee_pct": float|None } }
    shortable=True -> shares available
    shortable=False -> no borrow
    None -> unknown
    """
    results = {}
    if not tickers:
        return results
    print(f"\n  Shortability check ({len(tickers)} tickers via iborrowdesk.com)...")
    session = requests.Session()
    session.headers.update(HEADERS)
    for ticker in tickers:
        try:
            resp = session.get(f"https://iborrowdesk.com/api/ticker/{ticker}", timeout=10)
            if resp.status_code == 404:
                results[ticker] = {"shortable": None, "fee_pct": None}
                print(f"    {ticker}: not found (unknown)")
                continue
            if not resp.ok:
                results[ticker] = {"shortable": None, "fee_pct": None}
                print(f"    {ticker}: iborrowdesk error {resp.status_code}")
                continue
            data = resp.json()
            for broker_data in [data.get("ibkr", []), data.get("schwab", [])]:
                if broker_data:
                    latest    = broker_data[-1]
                    available = latest.get("available", 0)
                    fee       = latest.get("rate")
                    shortable = available > 0
                    results[ticker] = {"shortable": shortable, "fee_pct": fee}
                    icon = "OK" if shortable else "NO"
                    fee_str = f"{fee:.2f}%" if fee is not None else "?"
                    print(f"    {ticker}: [{icon}] {'shortable fee=' + fee_str if shortable else 'NOT shortable'}")
                    break
            else:
                results[ticker] = {"shortable": None, "fee_pct": None}
                print(f"    {ticker}: no data (unknown)")
        except requests.exceptions.ConnectionError:
            print(f"    [{ticker}] iborrowdesk unreachable - skipping shortability check")
            results[ticker] = {"shortable": None, "fee_pct": None}
        except Exception as e:
            results[ticker] = {"shortable": None, "fee_pct": None}
            print(f"    [{ticker}] shortability error: {type(e).__name__}")
        time.sleep(0.3)
    return results


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
        dest = HISTORY_DIR / f"ef_state_{state.get('date', 'unknown')}.json"
        with open(dest, "w") as f:
            json.dump(state, f, indent=2, default=str)
    return {"date": today, "candidates": {}, "scanned_at": None}

def _save_state(state: dict) -> None:
    with open(EF_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ===========================================================================
# TELEGRAM
# ===========================================================================

def _send_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  [Telegram] Env vars not set - skipping.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        import json as _json
        payload = _json.dumps(
            {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            ensure_ascii=False
        ).encode("utf-8")
        resp = requests.post(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        resp.raise_for_status()
        print("  [Telegram] Message sent.")
    except Exception as e:
        print(f"  [Telegram] Failed: {e}")


def _send_scan_alert(a_plus: list, monitor: list, no_borrow: list = None) -> None:
    no_borrow = no_borrow or []
    if not a_plus and not monitor:
        print("  [Telegram] No qualifying Earnings Fade setups - skipping.")
        return

    now_str = _fmt_et_sgt()

    if not a_plus:
        lines = [
            f"*Earnings Fade - {now_str}*",
            f"{len(monitor)} Monitor setup(s), no A+.",
            "",
        ]
        for c in monitor[:3]:
            borrow_icon = "OK" if c.get("shortable") else ("NO" if c.get("shortable") is False else "?")
            lines.append(
                f"  - *{c['ticker']}* [{borrow_icon}]  Gap: +{c['gap_pct']}%  "
                f"PM: ${c['pm_price']}  Score: {c['total_score']}/60"
            )
        if no_borrow:
            lines.append(f"\nExcluded (no borrow): {', '.join(c['ticker'] for c in no_borrow)}")
        lines.append("\n_Watch at open. Verify earnings quality manually._")
        _send_telegram("\n".join(lines))
        return

    lines = [
        f"*EARNINGS FADE ALERT* - {now_str}",
        f"*{len(a_plus)} A+ setup(s)* - Sell The News",
        "",
    ]
    for c in a_plus:
        t  = c.get("trade", {})
        sc = c.get("scores", {})
        if c.get("shortable") is True:
            fee_str = f"{c['borrow_fee_pct']:.2f}%" if c.get("borrow_fee_pct") is not None else "?"
            borrow_line = f"  Shortable  |  Borrow fee: {fee_str}/yr"
        elif c.get("shortable") is False:
            borrow_line = "  NO BORROW - do not trade"
        else:
            borrow_line = "  Borrow status unknown - verify with broker"
        lines += [
            f"*{c['ticker']}*  |  Score: {c['total_score']}/60  |  Tier: A+",
            borrow_line,
            f"  Gap: +{c['gap_pct']}%  |  PM Price: ${c['pm_price']}  |  Prev Close: ${c['prev_close']}",
            f"  Mkt Cap: ${c.get('market_cap_m', '?')}M  |  Near 52W High: {c.get('near_high_pct', '?')}% below",
            f"  C1(gap):{sc.get('c1_gap_size')} C2(earnings):{sc.get('c2_earnings_MANUAL')} (manual) "
            f"C3(vol):{sc.get('c3_pm_volume')} C4(profile):{sc.get('c4_stock_profile')} "
            f"C5(trend):{sc.get('c5_prior_trend')} C6(open):{sc.get('c6_open_reaction')} (pending)",
            f"  Entry (short near open): ${t.get('entry', '?')}",
            f"  Stop: ${t.get('stop', '?')} (+{STOP_PCT*100:.0f}% above PM high)",
            f"  Target 1 (50% fill): ${t.get('target1', '?')}  |  Target 2 (full fill): ${t.get('target2', '?')}",
            f"  Shares: {t.get('shares', '?')}  |  Risk: ${t.get('risk_usd', '?')}  |  R/R: {t.get('rr_ratio', '?')}:1",
            f"  Earnings: https://stockanalysis.com/stocks/{c['ticker'].lower()}/financials/",
            f"  Stocktwits: https://stocktwits.com/symbol/{c['ticker']}",
            f"  Reddit: https://www.reddit.com/search/?q={c['ticker']}",
            "",
        ]
    if monitor:
        monitor_parts = []
        for c in monitor[:5]:
            borrow_icon = "OK" if c.get("shortable") else ("NO" if c.get("shortable") is False else "?")
            monitor_parts.append(f"{c['ticker']}[{borrow_icon}]")
        lines.append(f"Also watching: {', '.join(monitor_parts)}")
        lines.append("")
    if no_borrow:
        lines.append(f"Excluded (no borrow): {', '.join(c['ticker'] for c in no_borrow)}")
        lines.append("")
    lines += [
        "*C2 (Earnings Quality) MUST be verified manually before trading.*",
        "*Clean beat (EPS + revenue + raised guidance) -> DO NOT TRADE.*",
        f"*Time stop: {TIME_STOP_SGT} / {TIME_STOP_ET}.*",
    ]
    _send_telegram("\n".join(lines))


# ===========================================================================
# SCAN MODE (the only mode)
# ===========================================================================

def mode_scan():
    """
    6-9 AM ET: Pull earnings calendar, fetch pre-market prices, score gappers, alert.
    NO paper trading. Trading agent handles all order management.
    """
    print(f"\n{'='*60}")
    print(f"  EARNINGS FADE - PRE-MARKET SCAN - {_fmt_et_sgt()}")
    print(f"{'='*60}")

    tickers = fetch_earnings_calendar()
    if not tickers:
        print("  No earnings tickers found for today.")
        return

    candidates = fetch_premarket_data(tickers)
    if not candidates:
        print(f"  No earnings gappers >= {MIN_GAP_PCT}% found.")
        return

    scored = [score_earnings(c) for c in candidates]
    scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    a_plus  = [s for s in scored if s.get("tier") == "A+"]
    monitor = [s for s in scored if s.get("tier") == "Monitor"]

    # Shortability check - only query actionable candidates
    actionable = [s for s in scored if s.get("tier") in ("A+", "Monitor")]
    if actionable:
        short_map = check_shortability([s["ticker"] for s in actionable])
        for s in scored:
            info = short_map.get(s["ticker"], {"shortable": None, "fee_pct": None})
            s["shortable"]      = info.get("shortable")
            s["borrow_fee_pct"] = info.get("fee_pct")
            # Strict filter: only confirmed shortable passes through
            if s.get("shortable") is not True and s.get("tier") in ("A+", "Monitor"):
                s["tier_original"] = s["tier"]
                s["tier"] = "No Borrow"
    else:
        for s in scored:
            s["shortable"]      = None
            s["borrow_fee_pct"] = None

    a_plus    = [s for s in scored if s.get("tier") == "A+"]
    monitor   = [s for s in scored if s.get("tier") == "Monitor"]
    no_borrow = [s for s in scored if s.get("tier") == "No Borrow"]

    print(f"\n  RESULTS: {len(a_plus)} A+ | {len(monitor)} Monitor | {len(no_borrow)} No-Borrow | "
          f"{len(scored)-len(a_plus)-len(monitor)-len(no_borrow)} Skip")

    if a_plus:
        print("\nA+ SETUPS:")
        for c in a_plus:
            sc = c.get("scores", {})
            t  = c.get("trade", {})
            borrow = "shortable" if c.get("shortable") else ("borrow unknown" if c.get("shortable") is None else "NO BORROW")
            print(f"\n  {c['ticker']}  Gap:+{c['gap_pct']}%  PM:${c['pm_price']}  Score:{c['total_score']}/60  [{borrow}]")
            print(f"  C1:{sc.get('c1_gap_size')} C2:{sc.get('c2_earnings_MANUAL')}(manual) "
                  f"C3:{sc.get('c3_pm_volume')} C4:{sc.get('c4_stock_profile')} "
                  f"C5:{sc.get('c5_prior_trend')} C6:{sc.get('c6_open_reaction')}(pending open)")
            print(f"  Entry:${t.get('entry')} Stop:${t.get('stop')} "
                  f"T1:${t.get('target1')} T2:${t.get('target2')}")
            print(f"  Verify: https://stockanalysis.com/stocks/{c['ticker'].lower()}/financials/")

    if monitor:
        print("\nMONITOR:")
        for c in monitor[:5]:
            borrow = "OK" if c.get("shortable") else ("NO" if c.get("shortable") is False else "?")
            print(f"  {c['ticker']:6s}  Gap:+{c['gap_pct']}%  PM:${c['pm_price']}  Score:{c['total_score']}/60  Borrow:[{borrow}]")

    if no_borrow:
        print(f"\nNO BORROW ({len(no_borrow)} excluded):")
        for c in no_borrow:
            print(f"  {c['ticker']:6s}  Was:{c.get('tier_original', '?')} - no shares available to short")

    # Save state (for trading agent and ClawPort dashboard)
    state = _load_state()
    state["scanned_at"]        = _fmt_et_sgt()
    state["candidates"]        = {c["ticker"]: c for c in scored}
    state["earnings_tickers"]  = tickers
    _save_state(state)

    # Archive
    hist_file = HISTORY_DIR / f"ef_scan_{_now_et().strftime('%Y%m%d_%H%M%S')}.json"
    with open(hist_file, "w") as f:
        json.dump({"scanned_at": state["scanned_at"], "candidates": scored}, f, indent=2, default=str)

    # Alert (only confirmed shortable setups)
    _send_scan_alert(a_plus, monitor, no_borrow)


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Earnings Fade Scanner (scan only)")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["scan"],
        help="scan: 6-9 AM ET - pull earnings, score gappers, alert",
    )
    args = parser.parse_args()
    mode_scan()


if __name__ == "__main__":
    main()
