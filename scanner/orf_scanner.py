#!/usr/bin/env python3
"""
Opening Range Fade (ORF) Scanner
----------------------------------
SCAN ONLY - no paper trading, no position management.
Trading is handled exclusively by alpaca_agent.py.

Identifies stocks that spike above their 9:30-9:45 AM ET opening range high,
then break back below it - a high-probability short setup as trapped buyers
from the initial spike are forced to sell.

Strategy Logic:
  1. At 9:45 AM ET: scan for stocks that moved >2% in the first 15 minutes.
     Record the Opening Range (OR) high and low for each.
  2. Score each setup on 6 conditions.
  3. Alert via Telegram for A+ and Monitor setups.
  4. Save scan state for the trading agent to consume.

Scoring (each /10, total /60):
  1. Prior trend (declining stock) - weak stocks fade harder
  2. OR size (range as % of price) - larger OR = more trapped buyers
  3. Gap direction - gapped up into OR high = more fuel for fade
  4. OR volume vs average - confirms conviction of initial spike
  5. Catalyst check - manual (M&A/FDA = do not trade)
  6. Rejection quality - how cleanly price rejected OR high

Tiers:
  >=35 - A+ setup
  20-34 - Monitor
  <20 - Skip

Usage:
  python orf_scanner.py --mode scan    # 9:45 AM ET (9:45 PM SGT): scan + alert
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
CONFIG_FILE = SCRIPT_DIR.parent / "config.yaml"

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
_ORF = _CFG.get("orf", {})

def _g(key, default):
    return _CFG.get(key, default)

def _o(key, default):
    return _ORF.get(key, default)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CAPITAL        = _g("capital_usd", 10_000)
RISK_PER_TRADE = CAPITAL * _g("risk_per_trade_pct", 0.01)
STOP_PCT       = _o("stop_loss_pct", 0.03)
MIN_MOVE_PCT   = _o("min_move_pct",  2.0)
OR_WINDOW_MINUTES = 15
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


# ---------------------------------------------------------------------------
# TradingView price fetching
# ---------------------------------------------------------------------------

def fetch_opening_range_movers() -> list:
    """
    Fetch stocks that have moved >2% since the open using TradingView Scanner.
    Called at 9:45 AM ET - 15 min after open.
    """
    print("  TradingView - fetching opening range movers...")
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "options": {"lang": "en"},
        "columns": [
            "name",
            "close",
            "open",
            "high",
            "low",
            "close",
            "volume",
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
        if not resp.ok:
            print(f"    [!] TradingView error {resp.status_code}: {resp.text[:500]}")
            return []
        data = resp.json()
    except Exception as e:
        print(f"    [!] TradingView error: {e}")
        return []

    results = []
    for item in data.get("data", []):
        d = item.get("d", [])
        if len(d) < 9:
            continue
        name, prev_close, open_p, high, low, last_price, volume, avg_vol, mkt_cap = d[:9]

        if not name or not open_p or not last_price:
            continue

        ticker = name.split(":")[1] if ":" in name else name

        or_high = float(high) if high else float(last_price)
        or_low = float(low) if low else float(last_price)
        current_price = float(last_price)
        open_price = float(open_p)
        prev_close_f = float(prev_close) if prev_close else None

        move_pct = (
            (current_price - open_price) / open_price * 100 if open_price else 0
        )

        if current_price < 0.50:
            continue
        if mkt_cap and float(mkt_cap) < 1_000_000:
            continue
        if move_pct < MIN_MOVE_PCT:
            continue

        gap_pct = ((open_price - prev_close_f) / prev_close_f * 100) if prev_close_f and prev_close_f > 0 else 0
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
        })

    print(f"    Found {len(results)} opening range movers (>{MIN_MOVE_PCT}% from open)")
    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_orf(candidate: dict) -> dict:
    """Score a candidate on all 6 ORF conditions."""

    # C1: Prior trend - default 5 (neutral)
    c1 = 5

    # C2: OR size
    or_size = candidate.get("or_size_pct", 0)
    if or_size >= _o("c2_or_size_10", 10):       c2 = 10
    elif or_size >= _o("c2_or_size_8",   7):      c2 = 8
    elif or_size >= _o("c2_or_size_7",   5):      c2 = 7
    elif or_size >= _o("c2_or_size_5",   3):      c2 = 5
    elif or_size >= _o("c2_or_size_3",   2):      c2 = 3
    else:                                          c2 = 1

    # C3: Gap direction
    gap_pct = candidate.get("gap_pct", 0)
    if gap_pct >= _o("c3_gap_10", 30):       c3 = 10
    elif gap_pct >= _o("c3_gap_9",  20):     c3 = 9
    elif gap_pct >= _o("c3_gap_7",  10):     c3 = 7
    elif gap_pct >= _o("c3_gap_5",   5):     c3 = 5
    elif gap_pct >= _o("c3_gap_3",   0):     c3 = 3
    else:                                     c3 = 1

    # C4: OR volume vs average
    vol = candidate.get("volume") or 0
    avg_vol = candidate.get("avg_daily_volume") or 0
    if avg_vol > 0:
        vol_ratio = vol / avg_vol
        if vol_ratio >= _o("c4_vol_pct_10", 30) / 100:    c4 = 10
        elif vol_ratio >= _o("c4_vol_pct_8",  20) / 100:  c4 = 8
        elif vol_ratio >= _o("c4_vol_pct_6",  10) / 100:  c4 = 6
        elif vol_ratio >= _o("c4_vol_pct_4",   5) / 100:  c4 = 4
        else:                                               c4 = 2
    else:
        c4 = 3

    # C5: Catalyst check - manual placeholder
    c5 = 5

    # C6: Rejection quality
    move_pct = candidate.get("move_from_open_pct", 0)
    or_size_p = candidate.get("or_size_pct", 1) or 1
    rejection_ratio = move_pct / or_size_p if or_size_p > 0 else 0
    if rejection_ratio >= 2.0:       c6 = 10
    elif rejection_ratio >= 1.5:     c6 = 8
    elif rejection_ratio >= 1.0:     c6 = 6
    elif rejection_ratio >= 0.5:     c6 = 4
    else:                             c6 = 2

    total = c1 + c2 + c3 + c4 + c5 + c6
    _aplus   = _o("tier_a_plus_min",  35)
    _monitor = _o("tier_monitor_min", 20)
    tier = "A+" if total >= _aplus else "Monitor" if total >= _monitor else "Skip"

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

    # Trade parameters (for alert display only - NOT for paper trading)
    if tier in ("A+", "Monitor"):
        or_high = candidate["or_high"]
        or_low = candidate["or_low"]
        prev_close = candidate.get("prev_close") or or_low

        stop = round(or_high * (1 + STOP_PCT), 4)
        stop_distance = stop - or_high
        shares = int(RISK_PER_TRADE / stop_distance) if stop_distance > 0 else 0
        target1 = round(or_low, 4)
        target2 = round(prev_close, 4)

        reward_t1 = round(shares * (or_high - target1), 2) if shares else 0
        rr = round(reward_t1 / RISK_PER_TRADE, 2) if RISK_PER_TRADE > 0 else 0

        candidate["trade"] = {
            "entry": or_high,
            "stop": stop,
            "stop_distance": round(stop_distance, 4),
            "target1": target1,
            "target2": target2,
            "shares": shares,
            "risk_usd": RISK_PER_TRADE,
            "reward_t1": reward_t1,
            "rr_ratio": rr,
            "time_stop_et": TIME_STOP_ET,
            "time_stop_sgt": TIME_STOP_SGT,
        }

    return candidate


# ---------------------------------------------------------------------------
# State management (scan results only)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    today = _today_et()
    if ORF_STATE_FILE.exists():
        with open(ORF_STATE_FILE) as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
        # Archive previous day
        dest = HISTORY_DIR / f"orf_state_{state.get('date', 'unknown')}.json"
        with open(dest, "w") as f:
            json.dump(state, f, indent=2, default=str)
    return {"date": today, "candidates": {}, "scanned_at": None}


def _save_state(state: dict) -> None:
    with open(ORF_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
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


def _send_scan_alert(a_plus: list, monitor: list) -> None:
    """Send Telegram alert for scan results. Silent if nothing qualifies."""
    if not a_plus and not monitor:
        print("  [Telegram] No qualifying ORF setups - skipping notification.")
        return

    now_str = _fmt_et_sgt()

    if not a_plus:
        lines = [
            f"ORF Scanner - {now_str}",
            f"{len(monitor)} Monitor setup(s), no A+.",
            "",
        ]
        for c in monitor[:3]:
            lines.append(
                f"  - *{c['ticker']}*  OR: ${c['or_low']}-${c['or_high']} "
                f"({c['or_size_pct']}%)  Score: {c['total_score']}/60"
            )
        lines.append("\n_Watch for breakdown below OR high. No entry yet._")
        _send_telegram("\n".join(lines))
        return

    lines = [
        f"*ORF ALERT* - {now_str}",
        f"*{len(a_plus)} A+ setup(s)* - Opening Range Fade",
        "",
    ]
    for c in a_plus:
        t = c.get("trade", {})
        sc = c.get("scores", {})
        lines += [
            f"*{c['ticker']}*  |  Score: {c['total_score']}/60  |  Tier: A+",
            f"  OR: ${c['or_low']} - ${c['or_high']} ({c['or_size_pct']}% range)",
            f"  Gap from prev close: +{c.get('gap_pct', 0):.1f}%  |  Move from open: +{c.get('move_from_open_pct', 0):.1f}%",
            f"  Entry (short at OR high breakdown): ${t.get('entry', '?')}",
            f"  Stop: ${t.get('stop', '?')} (+{STOP_PCT*100:.0f}% above OR high)",
            f"  Target 1 (OR low): ${t.get('target1', '?')}  |  Target 2 (prev close): ${t.get('target2', '?')}",
            f"  Shares: {t.get('shares', '?')}  |  Risk: ${t.get('risk_usd', '?')}  |  R/R: {t.get('rr_ratio', '?')}:1",
            f"  Reddit: https://www.reddit.com/search/?q={c['ticker']}",
            f"  Stocktwits: https://stocktwits.com/symbol/{c['ticker']}",
            "",
        ]
    if monitor:
        lines.append(f"Also watching: {', '.join(c['ticker'] for c in monitor[:5])}")
        lines.append("")
    lines += [
        "*Enter SHORT only when price closes back BELOW OR high.*",
        "*Verify catalyst is hollow (Reddit/Stocktwits).*",
        f"*Time stop: {TIME_STOP_SGT} / {TIME_STOP_ET}.*",
        "*If catalyst is M&A / FDA / earnings - DO NOT TRADE.*",
    ]
    _send_telegram("\n".join(lines))


# ---------------------------------------------------------------------------
# Scan mode (the only mode)
# ---------------------------------------------------------------------------

def mode_scan():
    """
    9:45 AM ET: Scan for opening range movers, score them, save state, alert.
    NO paper trading. Trading agent handles all order management.
    """
    print(f"\n{'='*60}")
    print(f"  ORF SCANNER - OPENING RANGE SCAN - {_fmt_et_sgt()}")
    print(f"{'='*60}")

    movers = fetch_opening_range_movers()

    if not movers:
        print("  No opening range movers found.")
        return

    movers = [m for m in movers if (m.get("current_price") or 0) >= 0.50]
    movers = [m for m in movers if (m.get("or_size_pct") or 0) >= 1.0]
    print(f"  After quality filters: {len(movers)} candidates")

    scored = [score_orf(m) for m in movers]
    scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    a_plus = [s for s in scored if s.get("tier") == "A+"]
    monitor = [s for s in scored if s.get("tier") == "Monitor"]

    print(f"\n  RESULTS: {len(a_plus)} A+ | {len(monitor)} Monitor | {len(scored)-len(a_plus)-len(monitor)} Skip")

    if a_plus:
        print("\nA+ SETUPS:")
        for c in a_plus:
            sc = c.get("scores", {})
            t = c.get("trade", {})
            print(f"\n  {c['ticker']}  Score:{c['total_score']}/60  OR:${c['or_low']}-${c['or_high']} ({c['or_size_pct']}%)")
            print(f"  C1:{sc.get('c1_prior_trend')} C2:{sc.get('c2_or_size')} C3:{sc.get('c3_gap_direction')} "
                  f"C4:{sc.get('c4_or_volume')} C5:{sc.get('c5_catalyst_MANUAL')}(manual) C6:{sc.get('c6_rejection_quality')}")
            print(f"  Entry:${t.get('entry')} Stop:${t.get('stop')} T1:${t.get('target1')} T2:${t.get('target2')}")
            print(f"  Shares:{t.get('shares')} Risk:${t.get('risk_usd')} R/R:{t.get('rr_ratio')}:1")

    if monitor:
        print("\nMONITOR:")
        for c in monitor[:5]:
            print(f"  {c['ticker']:6s}  OR:${c['or_low']}-${c['or_high']} ({c['or_size_pct']}%)  Score:{c['total_score']}/60")

    # Save state (for trading agent and ClawPort dashboard)
    state = _load_state()
    state["scanned_at"] = _fmt_et_sgt()
    state["candidates"] = {c["ticker"]: c for c in scored}
    _save_state(state)

    # Save history snapshot
    hist_file = HISTORY_DIR / f"orf_scan_{_now_et().strftime('%Y%m%d_%H%M%S')}.json"
    with open(hist_file, "w") as f:
        json.dump({"scanned_at": state["scanned_at"], "candidates": scored}, f, indent=2, default=str)

    # Telegram alert
    _send_scan_alert(a_plus, monitor)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ORF Scanner (scan only)")
    parser.add_argument(
        "--mode",
        choices=["scan"],
        required=True,
        help="scan: 9:45 AM ET - scan for opening range movers and alert",
    )
    args = parser.parse_args()
    mode_scan()


if __name__ == "__main__":
    main()
