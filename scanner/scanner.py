#!/usr/bin/env python3
"""
Bagholder Exit Liquidity Trading Scanner
-----------------------------------------
Identifies pre-market gap-up stocks primed for a fade (price reversal) at market open.

Strategy: Stocks in prolonged decline (6ÃÂ¢ÃÂÃÂ12 months, 50%+ drawdown) that suddenly
gap up 30%+ in pre-market on no real fundamental catalyst. Trapped holders sell
aggressively at open, overwhelming speculative buyers. Stock fades within 60 minutes.

Scoring (each /10, total /60):
  1. Prior decline ÃÂ¢ÃÂÃÂ¥50% over ÃÂ¢ÃÂÃÂ¥3 months
  2. Price <$10, market cap <$2B
  3. Short interest ÃÂ¢ÃÂÃÂ¥10% of float
  4. Pre-market spike ÃÂ¢ÃÂÃÂ¥30%
  5. No real fundamental catalyst (manual check ÃÂ¢ÃÂÃÂ always scored 5/10 as placeholder)
  6. Pre-market volume ÃÂ¢ÃÂÃÂ¥5x average daily volume

Tiers:
  ÃÂ¢ÃÂÃÂ¥35 ÃÂ¢ÃÂÃÂ A+ setup (full trade parameters)
  20ÃÂ¢ÃÂÃÂ34 ÃÂ¢ÃÂÃÂ Monitor
  <20 ÃÂ¢ÃÂÃÂ Skip

Usage:
  python scanner.py --mode watchlist   # Build/refresh watchlist from Finviz
  python scanner.py --mode morning     # Morning pre-market scan
  python scanner.py --mode full        # Refresh watchlist if >7 days old, then morning scan
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = SCRIPT_DIR / "data"
HISTORY_DIR = DATA_DIR / "history"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
LATEST_SCAN_FILE = DATA_DIR / "latest_scan.json"
CONFIG_FILE = SCRIPT_DIR.parent / "config.yaml"

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
            print(f"  [!] Could not load config.yaml: {e} ÃÂ¢ÃÂÃÂ using defaults")
    return {}

_CFG = _load_config()
_BH = _CFG.get("bagholder", {})

def _g(key, default):
    """Get a top-level config value with fallback."""
    return _CFG.get(key, default)

def _b(key, default):
    """Get a bagholder-section config value with fallback."""
    return _BH.get(key, default)

# ---------------------------------------------------------------------------
# Config / constants  (read from config.yaml, fall back to hardcoded defaults)
# ---------------------------------------------------------------------------
WATCHLIST_MAX_STOCKS = _b("watchlist_max_stocks", 80)
WATCHLIST_STALE_DAYS = _b("watchlist_stale_days", 7)
FINVIZ_RATE_MIN      = _g("finviz_rate_min_s", 0.8)
FINVIZ_RATE_MAX      = _g("finviz_rate_max_s", 1.5)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Trade risk parameters
CAPITAL        = _g("capital_usd", 10_000)
RISK_PER_TRADE = CAPITAL * _g("risk_per_trade_pct", 0.01)
STOP_PCT       = _b("stop_loss_pct", 0.10)
TIME_STOP_ET   = "10:30 AM ET"
TIME_STOP_SGT  = "10:30 PM SGT"


# ===========================================================================
# SCORING HELPERS
# ===========================================================================

def score_condition_1(perf_6m_pct: Optional[float], months_declining: Optional[int]) -> int:
    """
    Prior decline ÃÂ¢ÃÂÃÂ¥50% over ÃÂ¢ÃÂÃÂ¥3 months.
    Sweet spot: 60ÃÂ¢ÃÂÃÂ80% over 6ÃÂ¢ÃÂÃÂ12 months = 10/10.
    Thresholds read from config.yaml ÃÂ¢ÃÂÃÂ bagholder.c1_decline_*
    """
    if perf_6m_pct is None:
        return 0
    decline = abs(perf_6m_pct) if perf_6m_pct < 0 else 0
    t1 = _b("c1_decline_10", 80)
    t2 = _b("c1_decline_9",  60)
    t3 = _b("c1_decline_7",  50)
    t4 = _b("c1_decline_4",  40)
    t5 = _b("c1_decline_2",  30)
    if decline >= t1:   return 10
    elif decline >= t2: return 9
    elif decline >= t3: return 7
    elif decline >= t4: return 4
    elif decline >= t5: return 2
    return 0


def score_condition_2(price: Optional[float], market_cap_m: Optional[float]) -> int:
    """
    Price <$10, market cap <$2B. Lower = more retail-dominated = more predictable.
    Thresholds read from config.yaml ÃÂ¢ÃÂÃÂ bagholder.c2_price_* / c2_mktcap_*
    """
    score = 0
    if price is None or market_cap_m is None:
        return 0
    # Price component (5 pts)
    p1 = _b("c2_price_5", 1)
    p2 = _b("c2_price_4", 3)
    p3 = _b("c2_price_3", 5)
    p4 = _b("c2_price_2", 10)
    if price < p1:        score += 5
    elif price < p2:      score += 4
    elif price < p3:      score += 3
    elif price < p4:      score += 2
    # Market cap component (5 pts)
    m1 = _b("c2_mktcap_5", 50)
    m2 = _b("c2_mktcap_4", 200)
    m3 = _b("c2_mktcap_3", 500)
    m4 = _b("c2_mktcap_2", 1000)
    m5 = _b("c2_mktcap_1", 2000)
    if market_cap_m < m1:        score += 5
    elif market_cap_m < m2:      score += 4
    elif market_cap_m < m3:      score += 3
    elif market_cap_m < m4:      score += 2
    elif market_cap_m < m5:      score += 1
    return min(score, 10)


def score_condition_3(short_float_pct: Optional[float]) -> int:
    """
    Short interest ÃÂ¢ÃÂÃÂ¥10% of float. Higher = more bearish conviction.
    Thresholds read from config.yaml ÃÂ¢ÃÂÃÂ bagholder.c3_short_*
    """
    if short_float_pct is None:
        return 0
    t1 = _b("c3_short_10", 30)
    t2 = _b("c3_short_8",  20)
    t3 = _b("c3_short_7",  15)
    t4 = _b("c3_short_6",  10)
    t5 = _b("c3_short_3",   5)
    if short_float_pct >= t1:   return 10
    elif short_float_pct >= t2: return 8
    elif short_float_pct >= t3: return 7
    elif short_float_pct >= t4: return 6
    elif short_float_pct >= t5: return 3
    return 0


def score_condition_4(premarket_gap_pct: Optional[float]) -> int:
    """
    Pre-market spike ÃÂ¢ÃÂÃÂ¥30%. Optimal 50ÃÂ¢ÃÂÃÂ100%+ = max score.
    Thresholds read from config.yaml ÃÂ¢ÃÂÃÂ bagholder.c4_gap_*
    """
    if premarket_gap_pct is None:
        return 0
    t1 = _b("c4_gap_10", 100)
    t2 = _b("c4_gap_9",   75)
    t3 = _b("c4_gap_8",   50)
    t4 = _b("c4_gap_7",   40)
    t5 = _b("c4_gap_6",   30)
    t6 = _b("c4_gap_3",   20)
    if premarket_gap_pct >= t1:   return 10
    elif premarket_gap_pct >= t2: return 9
    elif premarket_gap_pct >= t3: return 8
    elif premarket_gap_pct >= t4: return 7
    elif premarket_gap_pct >= t5: return 6
    elif premarket_gap_pct >= t6: return 3
    return 0


def score_condition_5_placeholder() -> int:
    """
    Catalyst check ÃÂ¢ÃÂÃÂ ALWAYS manual. Returns 5/10 as neutral placeholder.
    User MUST verify via Reddit/Stocktwits before trading.
    """
    return 5  # Neutral ÃÂ¢ÃÂÃÂ pending manual verification


def score_condition_6(premarket_vol: Optional[float], avg_daily_vol: Optional[float]) -> int:
    """
    Pre-market volume ÃÂ¢ÃÂÃÂ¥5x average daily volume. Confirms retail FOMO surge.
    Thresholds read from config.yaml ÃÂ¢ÃÂÃÂ bagholder.c6_vol_*
    """
    if premarket_vol is None or avg_daily_vol is None or avg_daily_vol == 0:
        return 0
    ratio = premarket_vol / avg_daily_vol
    t1 = _b("c6_vol_10", 20)
    t2 = _b("c6_vol_9",  15)
    t3 = _b("c6_vol_8",  10)
    t4 = _b("c6_vol_7",   7)
    t5 = _b("c6_vol_6",   5)
    t6 = _b("c6_vol_4",   3)
    t7 = _b("c6_vol_2",   2)
    if ratio >= t1:   return 10
    elif ratio >= t2: return 9
    elif ratio >= t3: return 8
    elif ratio >= t4: return 7
    elif ratio >= t5: return 6
    elif ratio >= t6: return 4
    elif ratio >= t7: return 2
    return 0


def compute_total_score(c1, c2, c3, c4, c5, c6) -> int:
    return c1 + c2 + c3 + c4 + c5 + c6


def score_tier(total: int) -> str:
    aplus   = _b("tier_a_plus_min",  35)
    monitor = _b("tier_monitor_min", 20)
    if total >= aplus:   return "A+"
    elif total >= monitor: return "Monitor"
    return "Skip"


# ===========================================================================
# TRADE PARAMETER COMPUTATION
# ===========================================================================

def compute_trade_params(ticker: str, pm_price: float, prev_close: float) -> dict:
    """Compute entry, stop, targets, and position size for an A+ setup."""
    gap_amount = pm_price - prev_close
    stop = pm_price * (1 + STOP_PCT)
    stop_distance = stop - pm_price
    shares = int(RISK_PER_TRADE / stop_distance) if stop_distance > 0 else 0
    target1 = pm_price - (gap_amount * 0.5)
    target2 = prev_close  # Full gap fill

    return {
        "entry": round(pm_price, 4),
        "stop": round(stop, 4),
        "stop_distance": round(stop_distance, 4),
        "target1": round(target1, 4),
        "target2": round(target2, 4),
        "shares": shares,
        "risk_usd": RISK_PER_TRADE,
        "reward_t1": round((pm_price - target1) * shares, 2),
        "reward_t2": round((pm_price - target2) * shares, 2),
        "time_stop_et": TIME_STOP_ET,
        "time_stop_sgt": TIME_STOP_SGT,
    }


# ===========================================================================
# LAYER 1 ÃÂ¢ÃÂÃÂ WEEKLY WATCHLIST BUILDER (Finviz)
# ===========================================================================

def parse_float(s: str) -> Optional[float]:
    """Parse a string like '12.34%' or '-45.6%' or '1.2B' into a float."""
    if not s or s in ("-", "N/A", ""):
        return None
    s = s.strip()
    multiplier = 1.0
    if s.endswith("B"):
        multiplier = 1000.0
        s = s[:-1]
    elif s.endswith("M"):
        multiplier = 1.0
        s = s[:-1]
    elif s.endswith("K"):
        multiplier = 0.001
        s = s[:-1]
    s = s.replace("%", "").replace(",", "").replace("$", "")
    try:
        return float(s) * multiplier
    except ValueError:
        return None


def finviz_screener_page(page: int = 1) -> list[dict]:
    """
    Scrape one page of Finviz screener results.
    Filters are driven by config.yaml ÃÂ¢ÃÂÃÂ bagholder.watchlist_* values.
    Uses v=111 (overview). Tickers are extracted from quote.ashx?t=TICKER links,
    validated to be pure uppercase letters (1ÃÂ¢ÃÂÃÂ5 chars) to avoid picking up
    price/percentage values that also appear as link text.
    """
    max_price  = int(_b("watchlist_max_price",       10))
    min_short  = int(_b("watchlist_min_short_float", 10))
    max_perf   = int(abs(_b("watchlist_max_perf_6m", -30)))
    min_vol_k  = int(_b("watchlist_min_avg_vol",    100_000) / 1000)

    r = (page - 1) * 20 + 1
    url = (
        f"https://finviz.com/screener.ashx?v=111"
        f"&f=sh_avgvol_o{min_vol_k},sh_price_u{max_price},sh_short_o{min_short},ta_perf2_-{max_perf}to0"
        f"&r={r}"
        "&o=-volume"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [!] Finviz screener page {page} error: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = []
    seen = set()

    # Extract tickers from href="quote.ashx?t=TICKER" links.
    # Pull the ticker from the URL parameter (not the link text) to avoid
    # accidentally grabbing price/percentage values that share the same link.
    ticker_pattern = re.compile(r"quote\.ashx\?t=([A-Z]{1,5})(?:&|$)")
    for a in soup.find_all("a", href=ticker_pattern):
        m = ticker_pattern.search(a.get("href", ""))
        if m:
            ticker = m.group(1)
            if ticker not in seen:
                seen.add(ticker)
                rows.append({"ticker": ticker})

    if not rows:
        print(f"  [!] Could not parse Finviz screener page {page} ÃÂ¢ÃÂÃÂ structure may have changed")

    return rows


def finviz_quote(ticker: str) -> dict:
    """Scrape the Finviz quote page for a single ticker for detailed data."""
    url = f"https://finviz.com/quote.ashx?t={ticker}&ty=c&ta=1&p=d"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [!] {ticker} quote error: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    data = {"ticker": ticker}

    # Finviz quote page: all snapshot cells share class "snapshot-td2" inside
    # "snapshot-table2". They alternate label / value (even index = label,
    # odd index = value). The old "snapshot-td2-cp" label class no longer exists.
    label_value_pairs = {}
    table = soup.find("table", class_="snapshot-table2")
    if table:
        cells = table.find_all("td", class_="snapshot-td2")
        for i in range(0, len(cells) - 1, 2):
            lbl = cells[i].text.strip()
            val = cells[i + 1].text.strip()
            if lbl:
                label_value_pairs[lbl] = val

    # Map known fields
    field_map = {
        "Price": "price",
        "Short Float": "short_float_pct_str",
        "Perf Half Y": "perf_6m_str",
        "Market Cap": "market_cap_str",
        "Avg Volume": "avg_volume_str",
        "52W High": "week52_high_str",
        "52W Low": "week52_low_str",
        "Shs Float": "float_str",
    }
    for finviz_lbl, our_key in field_map.items():
        if finviz_lbl in label_value_pairs:
            data[our_key] = label_value_pairs[finviz_lbl]

    # Parse numerics
    data["price"] = parse_float(data.get("price"))
    data["short_float_pct"] = parse_float(data.get("short_float_pct_str"))
    data["perf_6m_pct"] = parse_float(data.get("perf_6m_str"))
    data["market_cap_m"] = parse_float(data.get("market_cap_str"))
    # Avg Volume comes as e.g. "39.38M" ÃÂ¢ÃÂÃÂ parse_float already handles M suffix
    # and returns value in millions, so multiply by 1,000,000 to get actual shares
    data["avg_volume"] = parse_float(data.get("avg_volume_str"))
    if data["avg_volume"] is not None:
        data["avg_volume"] = int(data["avg_volume"] * 1_000_000)

    # Score conditions 1ÃÂ¢ÃÂÃÂ3
    c1 = score_condition_1(data.get("perf_6m_pct"), None)
    c2 = score_condition_2(data.get("price"), data.get("market_cap_m"))
    c3 = score_condition_3(data.get("short_float_pct"))
    data["score_c1"] = c1
    data["score_c2"] = c2
    data["score_c3"] = c3
    data["watchlist_score"] = c1 + c2 + c3

    return data


def build_watchlist() -> list[dict]:
    """Build weekly watchlist from Finviz screener."""
    print("\nÃÂ°ÃÂÃÂÃÂ LAYER 1 ÃÂ¢ÃÂÃÂ Building weekly watchlist from Finviz...")
    tickers_found = []

    # Scrape up to 4 pages (20 results each = 80 max)
    for page in range(1, 5):
        print(f"  Scraping Finviz screener page {page}...")
        rows = finviz_screener_page(page)
        if not rows:
            print(f"  No results on page {page}, stopping.")
            break
        tickers_found.extend([r["ticker"] for r in rows])
        print(f"  Found {len(rows)} tickers on page {page}")
        if len(rows) < 20:
            break  # Last page
        time.sleep(random.uniform(FINVIZ_RATE_MIN, FINVIZ_RATE_MAX))

    # Final safety filter: only keep strings that look like real tickers
    # (1ÃÂ¢ÃÂÃÂ5 uppercase letters, no digits, no punctuation)
    ticker_re = re.compile(r"^[A-Z]{1,5}$")
    tickers_found = [t for t in dict.fromkeys(tickers_found) if ticker_re.match(t)]
    tickers_found = tickers_found[:WATCHLIST_MAX_STOCKS]
    print(f"  Total unique tickers: {len(tickers_found)}")

    if not tickers_found:
        print("  [!] No tickers found. Finviz may be blocking or structure changed.")
        print("  Using empty watchlist.")
        return []

    # Fetch detailed data for each ticker
    stocks = []
    for i, ticker in enumerate(tickers_found, 1):
        print(f"  [{i}/{len(tickers_found)}] Fetching quote for {ticker}...")
        data = finviz_quote(ticker)
        if data and data.get("price"):
            stocks.append(data)
        sleep_time = random.uniform(FINVIZ_RATE_MIN, FINVIZ_RATE_MAX)
        time.sleep(sleep_time)

    # Sort by watchlist score descending
    stocks.sort(key=lambda x: x.get("watchlist_score", 0), reverse=True)
    return stocks


def save_watchlist(stocks: list[dict]) -> None:
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_at_et": _utc_to_et_str(),
        "generated_at_sgt": _utc_to_sgt_str(),
        "count": len(stocks),
        "stocks": stocks,
    }
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\nÃÂ¢ÃÂÃÂ Watchlist saved ÃÂ¢ÃÂÃÂ {WATCHLIST_FILE}  ({len(stocks)} stocks)")


def load_watchlist() -> dict:
    if not WATCHLIST_FILE.exists():
        return {}
    with open(WATCHLIST_FILE) as f:
        return json.load(f)


def watchlist_is_stale() -> bool:
    if not WATCHLIST_FILE.exists():
        return True
    wl = load_watchlist()
    ts_str = wl.get("generated_at_utc")
    if not ts_str:
        return True
    try:
        ts = datetime.fromisoformat(ts_str)
        age_days = (datetime.now(timezone.utc) - ts).days
        return age_days >= WATCHLIST_STALE_DAYS
    except Exception:
        return True


# ===========================================================================
# LAYER 2 ÃÂ¢ÃÂÃÂ MORNING PRE-MARKET SCANNER
# ===========================================================================

def scan_tradingview() -> list[dict]:
    """
    Fetch pre-market gappers from TradingView Scanner API.
    Returns list of dicts with ticker, premarket_gap_pct, price, prev_close.

    TradingView's current API (2025+) requires:
    - "markets" array instead of inline exchange filter
    - "filter2" with operator/operands syntax for complex filters
    - "filter" array still works for simple field filters
    - Referer header to avoid 400s
    """
    print("  ÃÂ°ÃÂÃÂÃÂ¡ TradingView Scanner API...")
    url = "https://scanner.tradingview.com/america/scan"

    # Updated payload format for current TradingView API
    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "options": {"lang": "en"},
        "columns": [
            "name",
            "close",
            "premarket_close",
            "premarket_change",
            "premarket_volume",
            "average_volume_10d_calc",
            "market_cap_basic",
            "type",
            "subtype",
        ],
        "filter": [
            {"left": "is_primary", "operation": "equal", "right": True},
            {"left": "premarket_change", "operation": "greater", "right": 0.20},
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
        "sort": {"sortBy": "premarket_change", "sortOrder": "desc"},
        "range": [0, 50],
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
        if len(d) < 7:
            continue
        ticker, prev_close, pm_price, pm_change, pm_vol, avg_vol, mkt_cap = d[:7]
        if not ticker or pm_price is None or prev_close is None:
            continue
        # pm_change from TV is already a percentage (e.g. 35.0 = 35%)
        gap_pct = float(pm_change) if pm_change else 0
        results.append({
            "ticker": ticker.split(":")[1] if ":" in ticker else ticker,
            "source": "tradingview",
            "prev_close": prev_close,
            "pm_price": pm_price,
            "premarket_gap_pct": round(gap_pct, 2),
            "pm_volume": pm_vol,
            "avg_daily_volume": avg_vol,
            "market_cap_m": round(mkt_cap / 1_000_000, 2) if mkt_cap else None,
        })
    print(f"    Found {len(results)} gappers from TradingView")
    return results


def scan_barchart() -> list[dict]:
    """
    Fetch pre-market gappers from Barchart API.
    """
    print("  ÃÂ°ÃÂÃÂÃÂ¡ Barchart API...")
    url = (
        "https://www.barchart.com/proxies/core-api/v1/quotes/get"
        "?list=stocks.us.gap_up.pre_market&fields=symbol,lastPrice,priceChange,"
        "percentChange,previousClose,volume,avgVolume&orderBy=percentChange"
        "&orderDir=desc&startRow=1&numRows=50&raw=1"
    )
    headers = {**HEADERS, "Referer": "https://www.barchart.com/"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code == 401:
            print("    [!] Barchart: authentication required (skipping)")
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    [!] Barchart error: {type(e).__name__}")
        return []

    results = []
    for item in data.get("data", []):
        q = item.get("raw", item)
        ticker = q.get("symbol")
        if not ticker:
            continue
        pm_price = q.get("lastPrice")
        prev_close = q.get("previousClose")
        pct_change = q.get("percentChange")
        pm_vol = q.get("volume")
        avg_vol = q.get("avgVolume")
        if pm_price is None or prev_close is None:
            continue
        gap_pct = float(pct_change) if pct_change else (
            ((pm_price - prev_close) / prev_close * 100) if prev_close else 0
        )
        results.append({
            "ticker": ticker,
            "source": "barchart",
            "prev_close": prev_close,
            "pm_price": pm_price,
            "premarket_gap_pct": round(gap_pct, 2),
            "pm_volume": pm_vol,
            "avg_daily_volume": avg_vol,
            "market_cap_m": None,
        })
    print(f"    Found {len(results)} gappers from Barchart")
    return results


def scan_stockanalysis() -> list[dict]:
    """
    Scrape pre-market gainers from StockAnalysis.com as backup.
    """
    print("  ÃÂ°ÃÂÃÂÃÂ¡ StockAnalysis.com (backup scrape)...")
    url = "https://stockanalysis.com/markets/premarket/gainers/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [!] StockAnalysis error: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    # Find the main table
    table = soup.find("table")
    if not table:
        print("    [!] StockAnalysis: no table found")
        return []

    rows = table.find_all("tr")[1:]  # Skip header
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 5:
            continue
        try:
            ticker = cols[0].text.strip().split("\n")[0].strip()
            pm_price_str = cols[2].text.strip().replace(",", "")
            gap_str = cols[3].text.strip().replace("%", "").replace("+", "")
            prev_close_str = cols[4].text.strip().replace(",", "") if len(cols) > 4 else ""

            pm_price = parse_float(pm_price_str)
            gap_pct = parse_float(gap_str)
            prev_close = parse_float(prev_close_str)

            if not ticker or pm_price is None or gap_pct is None:
                continue
            if prev_close is None and pm_price and gap_pct:
                prev_close = pm_price / (1 + gap_pct / 100)

            results.append({
                "ticker": ticker,
                "source": "stockanalysis",
                "prev_close": prev_close,
                "pm_price": pm_price,
                "premarket_gap_pct": round(gap_pct, 2),
                "pm_volume": None,
                "avg_daily_volume": None,
                "market_cap_m": None,
            })
        except Exception:
            continue

    print(f"    Found {len(results)} gappers from StockAnalysis")
    return results


def deduplicate_gappers(sources: list[list[dict]]) -> list[dict]:
    """
    Merge results from multiple sources, deduplicating by ticker.
    Prefer TradingView data, then Barchart, then StockAnalysis.
    """
    merged: dict[str, dict] = {}
    priority = {"tradingview": 0, "barchart": 1, "stockanalysis": 2}
    for batch in sources:
        for item in batch:
            ticker = item["ticker"].upper()
            if ticker not in merged:
                merged[ticker] = item
            else:
                existing_prio = priority.get(merged[ticker].get("source", ""), 99)
                new_prio = priority.get(item.get("source", ""), 99)
                if new_prio < existing_prio:
                    # Better source ÃÂ¢ÃÂÃÂ merge, filling in missing fields
                    for k, v in item.items():
                        if v is not None and merged[ticker].get(k) is None:
                            merged[ticker][k] = v
    return list(merged.values())


def cross_reference_watchlist(gappers: list[dict], watchlist: dict) -> list[dict]:
    """Enrich gappers with watchlist data (conditions 1ÃÂ¢ÃÂÃÂ3 scores)."""
    wl_stocks = {s["ticker"]: s for s in watchlist.get("stocks", [])}
    for g in gappers:
        ticker = g["ticker"]
        if ticker in wl_stocks:
            wl = wl_stocks[ticker]
            g["on_watchlist"] = True
            g["perf_6m_pct"] = wl.get("perf_6m_pct")
            g["short_float_pct"] = wl.get("short_float_pct")
            if g.get("market_cap_m") is None:
                g["market_cap_m"] = wl.get("market_cap_m")
            if g.get("avg_daily_volume") is None:
                g["avg_daily_volume"] = wl.get("avg_volume")
            if g.get("pm_price") and g.get("market_cap_m") is None:
                g["price_from_watchlist"] = wl.get("price")
        else:
            g["on_watchlist"] = False
            g["perf_6m_pct"] = None
            g["short_float_pct"] = None
    return gappers


def score_gapper(g: dict) -> dict:
    """Score a gapper on all 6 conditions."""
    c1 = score_condition_1(g.get("perf_6m_pct"), None)
    c2 = score_condition_2(g.get("pm_price"), g.get("market_cap_m"))
    c3 = score_condition_3(g.get("short_float_pct"))
    c4 = score_condition_4(g.get("premarket_gap_pct"))
    c5 = score_condition_5_placeholder()
    c6 = score_condition_6(g.get("pm_volume"), g.get("avg_daily_volume"))

    total = compute_total_score(c1, c2, c3, c4, c5, c6)
    tier = score_tier(total)

    g["scores"] = {
        "c1_prior_decline": c1,
        "c2_price_cap": c2,
        "c3_short_interest": c3,
        "c4_pm_spike": c4,
        "c5_catalyst_MANUAL": c5,
        "c6_pm_volume": c6,
        "total": total,
        "tier": tier,
    }
    g["total_score"] = total
    g["tier"] = tier

    # Trade parameters for A+ and Monitor setups
    if tier in ("A+", "Monitor") and g.get("pm_price") and g.get("prev_close"):
        trade = compute_trade_params(
            g["ticker"],
            float(g["pm_price"]),
            float(g["prev_close"]),
        )
        g["trade"] = trade

        # R/R sanity check: if reward at T1 is ÃÂ¢ÃÂÃÂ¤0 or R/R < 1:1, downgrade to Monitor
        # This catches extreme penny-stock gaps where stop > retracement distance
        if tier == "A+" and (trade["reward_t1"] <= 0 or trade.get("reward_t1", 0) < trade["risk_usd"]):
            g["tier"] = "Monitor"
            g["scores"]["tier"] = "Monitor"
            g["scores"]["downgrade_reason"] = "Poor R/R ÃÂ¢ÃÂÃÂ stop distance exceeds T1 reward"
            print(f"  ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ  [{g['ticker']}] Downgraded A+ ÃÂ¢ÃÂÃÂ Monitor: negative/poor R/R at T1")

    return g


# ===========================================================================
# SHORTABILITY CHECK
# ===========================================================================

def check_shortability(tickers: list[str]) -> dict[str, dict]:
    """
    Check whether each ticker has shares available to borrow.

    Uses iborrowdesk.com (free, no auth, scrapes IBKR + Schwab borrow data).
    Returns a dict: { "TICKER": { "shortable": bool|None, "fee_pct": float|None, "source": str } }

    shortable=True  \u2192 shares available to borrow (fee_pct = annualised borrow rate %)
    shortable=False \u2192 explicitly listed as "no shares available" (HTB / no locate)
    shortable=None  \u2192 iborrowdesk unreachable or ticker not found (unknown)
    """
    results = {}
    if not tickers:
        return results

    print(f"\n  \U0001f50d Shortability check ({len(tickers)} tickers via iborrowdesk.com)...")
    session = requests.Session()
    session.headers.update(HEADERS)

    for ticker in tickers:
        try:
            url = f"https://iborrowdesk.com/api/ticker/{ticker}"
            resp = session.get(url, timeout=10)
            if resp.status_code == 404:
                results[ticker] = {"shortable": None, "fee_pct": None, "source": "iborrowdesk_404"}
                print(f"    {ticker}: not found on iborrowdesk (unknown)")
                continue
            if not resp.ok:
                results[ticker] = {"shortable": None, "fee_pct": None, "source": f"iborrowdesk_err_{resp.status_code}"}
                print(f"    {ticker}: iborrowdesk error {resp.status_code} (unknown)")
                continue

            data = resp.json()
            ibkr = data.get("ibkr", [])
            schwab = data.get("schwab", [])

            for broker_data in [ibkr, schwab]:
                if broker_data:
                    latest = broker_data[-1]
                    available = latest.get("available", 0)
                    fee = latest.get("rate")
                    if available > 0:
                        results[ticker] = {"shortable": True, "fee_pct": fee, "source": "iborrowdesk_ibkr"}
                        fee_str = f"{fee:.2f}%" if fee is not None else "?"
                        print(f"    {ticker}: \u2705 shortable  fee={fee_str}  avail={available:,}")
                        break
                    else:
                        results[ticker] = {"shortable": False, "fee_pct": fee, "source": "iborrowdesk_ibkr"}
                        print(f"    {ticker}: \u274c NOT shortable (0 shares available at IBKR/Schwab)")
                        break
            else:
                results[ticker] = {"shortable": None, "fee_pct": None, "source": "iborrowdesk_nodata"}
                print(f"    {ticker}: no data (unknown)")

        except requests.exceptions.ConnectionError:
            print(f"    [{ticker}] iborrowdesk unreachable \u2014 skipping shortability check")
            results[ticker] = {"shortable": None, "fee_pct": None, "source": "unreachable"}
        except Exception as e:
            print(f"    [{ticker}] shortability check error: {type(e).__name__}")
            results[ticker] = {"shortable": None, "fee_pct": None, "source": "error"}

        time.sleep(0.3)

    return results


# ===========================================================================
# TELEGRAM NOTIFICATIONS
# ===========================================================================

def send_telegram_alert(candidates: list[dict], total_gappers: int = 0) -> None:
    """
    Send formatted Telegram alert after every morning scan.
    - A+ setups get full trade parameters.
    - Monitor setups get a brief mention.
    - If no gappers at all, sends a "clear market" summary.
    Requires env vars: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  [Telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set ÃÂ¢ÃÂÃÂ skipping.")
        return

    a_plus = [c for c in candidates if c.get("tier") == "A+"]
    monitor = [c for c in candidates if c.get("tier") == "Monitor"]
    now_str = f"{_utc_to_et_str()} / {_utc_to_sgt_str()}"

    # --- Case 1: No gappers found at all ÃÂ¢ÃÂÃÂ silent, no notification ---
    if total_gappers == 0:
        print("  [Telegram] No gappers found ÃÂ¢ÃÂÃÂ skipping notification.")
        return

    # --- Case 2: Gappers found but none qualify ---
    if not a_plus and not monitor:
        message = (
            f"ÃÂ°ÃÂÃÂÃÂ *Scanner ran ÃÂ¢ÃÂÃÂ {now_str}*\n"
            f"Found *{total_gappers} gapper(s)* ÃÂ¢ÃÂÃÂ none meet the 20/60 threshold.\n"
            f"_No setups today. Stand down._"
        )
        _send_telegram_message(token, chat_id, message)
        print("  [Telegram] ÃÂ¢ÃÂÃÂ Sent 'no qualifying setups' summary.")
        return

    # --- Case 3: Monitor only (no A+) ---
    if not a_plus:
        lines = [
            f"ÃÂ°ÃÂÃÂÃÂ *Scanner ran ÃÂ¢ÃÂÃÂ {now_str}*",
            f"Found *{total_gappers} gapper(s)* ÃÂ¢ÃÂÃÂ {len(monitor)} Monitor setup(s), no A+.",
            "",
        ]
        for c in monitor[:3]:
            lines.append(
                f"  ÃÂ¢ÃÂÃÂ¢ *{c['ticker']}*  {c.get('premarket_gap_pct', 0):.1f}% gap  "
                f"Score: {c.get('total_score', 0)}/60"
            )
        lines += ["", "_No high-conviction setup today. Stand down._"]
        _send_telegram_message(token, chat_id, "\n".join(lines))
        print(f"  [Telegram] ÃÂ¢ÃÂÃÂ Sent monitor-only summary ({len(monitor)} setups).")
        return

    # --- Case 4: A+ setups found ---
    lines = [
        f"ÃÂ°ÃÂÃÂÃÂ¨ *TRADING SCANNER ALERT* ÃÂ¢ÃÂÃÂ {now_str}",
        f"Found *{len(a_plus)} A+ setup(s)* ÃÂ¢ÃÂÃÂ Bagholder Exit Liquidity",
        "",
    ]
    for c in a_plus:
        t = c.get("trade", {})
        lines += [
            f"ÃÂ°ÃÂÃÂÃÂ *{c['ticker']}*  |  Score: {c['total_score']}/60  |  Tier: A+",
            f"  Gap: +{c.get('premarket_gap_pct', '?')}%  |  PM Price: ${t.get('entry', '?')}",
            f"  Stop: ${t.get('stop', '?')} (+{STOP_PCT*100:.0f}% above PM high)",
            f"  Target 1: ${t.get('target1', '?')} (50% retracement)",
            f"  Target 2: ${t.get('target2', '?')} (full gap fill)",
            f"  Shares: {t.get('shares', '?')}  |  Risk: ${t.get('risk_usd', '?')}",
            f"  Reward T1: ${t.get('reward_t1', '?')}  |  Reward T2: ${t.get('reward_t2', '?')}",
            f"  ÃÂ°ÃÂÃÂÃÂ Reddit: https://www.reddit.com/search/?q={c['ticker']}",
            f"  ÃÂ°ÃÂÃÂÃÂ Stocktwits: https://stocktwits.com/symbol/{c['ticker']}",
            "",
        ]
    if monitor:
        lines.append(f"ÃÂ°ÃÂÃÂÃÂ Also monitoring: {', '.join(c['ticker'] for c in monitor[:5])}")
        lines.append("")
    lines += [
        "ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ *Short at 9:30 PM SGT (9:30 AM ET) open.*",
        "ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ *Verify catalyst is hollow (check Reddit/Stocktwits).*",
        "ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ *Close by 10:30 PM SGT (10:30 AM ET) regardless.*",
        "ÃÂ°ÃÂÃÂÃÂ« *If catalyst is M&A / FDA / earnings beat ÃÂ¢ÃÂÃÂ DO NOT TRADE.*",
    ]

    _send_telegram_message(token, chat_id, "\n".join(lines))
    print(f"  [Telegram] ÃÂ¢ÃÂÃÂ Alert sent for {len(a_plus)} A+ setup(s).")


def _send_telegram_message(token: str, chat_id: str, message: str) -> None:
    """Send a single message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [Telegram] ÃÂ¢ÃÂÃÂ Failed to send: {e}")


# ===========================================================================
# TIME HELPERS
# ===========================================================================

def _utc_to_et_str() -> str:
    """Return current time as ET string (approximate, no pytz dependency)."""
    now_utc = datetime.now(timezone.utc)
    # EDT is UTC-4, EST is UTC-5. Use -4 (EDT) MarchÃÂ¢ÃÂÃÂNov, -5 (EST) otherwise.
    month = now_utc.month
    offset_hours = -4 if 3 <= month <= 11 else -5
    from datetime import timedelta
    et = now_utc + timedelta(hours=offset_hours)
    tz_label = "EDT" if offset_hours == -4 else "EST"
    return et.strftime(f"%Y-%m-%d %H:%M {tz_label}")


def _utc_to_sgt_str() -> str:
    """Return current time as SGT string (UTC+8)."""
    from datetime import timedelta
    sgt = datetime.now(timezone.utc) + timedelta(hours=8)
    return sgt.strftime("%Y-%m-%d %H:%M SGT")


# ===========================================================================
# MAIN MODES
# ===========================================================================

def run_watchlist_mode():
    """Build or refresh the weekly watchlist."""
    print("\n" + "=" * 60)
    print("  WATCHLIST BUILD MODE")
    print("=" * 60)
    stocks = build_watchlist()
    save_watchlist(stocks)
    if stocks:
        print("\nÃÂ°ÃÂÃÂÃÂ Top 5 stocks by watchlist score (conditions 1ÃÂ¢ÃÂÃÂ3):")
        print("-" * 70)
        for s in stocks[:5]:
            print(
                f"  {s['ticker']:6s}  "
                f"Score:{s.get('watchlist_score',0):2d}/30  "
                f"C1(decline):{s.get('score_c1',0):2d}  "
                f"C2(price/cap):{s.get('score_c2',0):2d}  "
                f"C3(short):{s.get('score_c3',0):2d}  "
                f"Price:${s.get('price','?')}  "
                f"6mPerf:{s.get('perf_6m_pct','?')}%  "
                f"SI:{s.get('short_float_pct','?')}%"
            )
    return stocks


def run_morning_mode():
    """Run the morning pre-market scan."""
    print("\n" + "=" * 60)
    print(f"  MORNING PRE-MARKET SCAN ÃÂ¢ÃÂÃÂ {_utc_to_et_str()} / {_utc_to_sgt_str()}")
    print("=" * 60)

    # Load watchlist
    watchlist = load_watchlist()
    if not watchlist:
        print("ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ  Watchlist is empty ÃÂ¢ÃÂÃÂ run --mode watchlist first.")
        print("   Proceeding with gap data only (conditions 1ÃÂ¢ÃÂÃÂ3 will score 0).")

    # Fetch gappers from all 3 sources in sequence (parallel via threading optional)
    tv_results = scan_tradingview()
    time.sleep(1)
    bc_results = scan_barchart()
    time.sleep(1)
    sa_results = scan_stockanalysis()

    # Deduplicate
    all_gappers = deduplicate_gappers([tv_results, bc_results, sa_results])
    print(f"\n  Total unique gappers after dedup: {len(all_gappers)}")

    if not all_gappers:
        print("  No pre-market gappers found. Market may not be open yet or sources are down.")
        _save_scan([], "morning")
        send_telegram_alert([], total_gappers=0)
        return []

    # Filter: gap ÃÂ¢ÃÂÃÂ¥20% minimum to be worth scoring
    gappers_filtered = [g for g in all_gappers if (g.get("premarket_gap_pct") or 0) >= 20]
    print(f"  After ÃÂ¢ÃÂÃÂ¥20% gap filter: {len(gappers_filtered)}")

    # Filter: PM price must be ÃÂ¢ÃÂÃÂ¥$0.50 ÃÂ¢ÃÂÃÂ penny stocks have poor R/R and unreliable data
    gappers_filtered = [g for g in gappers_filtered if (g.get("pm_price") or 0) >= 0.50]
    print(f"  After ÃÂ¢ÃÂÃÂ¥$0.50 PM price filter: {len(gappers_filtered)}")

    # Filter: prev_close sanity check
    # If implied prev_close = pm_price / (1 + gap%/100) is <$0.10, the gap figure
    # is almost certainly a data artifact (TradingView returning split-adjusted
    # historical close instead of prior day's close). Discard these.
    def _implied_prev_close(g: dict) -> float:
        pm = g.get("pm_price") or 0
        gap = g.get("premarket_gap_pct") or 0
        if gap <= 0:
            return pm
        return pm / (1 + gap / 100)

    before = len(gappers_filtered)
    gappers_filtered = [g for g in gappers_filtered if _implied_prev_close(g) >= 0.10]
    removed = before - len(gappers_filtered)
    if removed:
        print(f"  Removed {removed} ticker(s) with implausible prev_close (<$0.10) ÃÂ¢ÃÂÃÂ likely bad gap data")
    print(f"  After prev_close sanity filter: {len(gappers_filtered)}")

    # Filter: gap ÃÂ¢ÃÂÃÂ¤300% cap ÃÂ¢ÃÂÃÂ gaps beyond this are almost always pump-and-dumps
    # or data errors. The bagholder thesis requires a credible gap, not a 10x spike.
    gappers_filtered = [g for g in gappers_filtered if (g.get("premarket_gap_pct") or 0) <= 300]
    print(f"  After ÃÂ¢ÃÂÃÂ¤300% gap cap: {len(gappers_filtered)}")

    # Cross-reference with watchlist
    gappers_filtered = cross_reference_watchlist(gappers_filtered, watchlist)

    # Score all candidates
    scored = [score_gapper(g) for g in gappers_filtered]
    scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    # Shortability check — only bother with A+ and Monitor candidates
    actionable = [s for s in scored if s.get("tier") in ("A+", "Monitor")]
    if actionable:
        short_map = check_shortability([s["ticker"] for s in actionable])
        for s in scored:
            info = short_map.get(s["ticker"], {"shortable": None, "fee_pct": None})
            s["shortable"] = info.get("shortable")       # True / False / None
            s["borrow_fee_pct"] = info.get("fee_pct")    # annualised %, or None
            # Downgrade to "No Borrow" if confirmed not shortable — no point trading
            if s.get("shortable") is False and s.get("tier") in ("A+", "Monitor"):
                s["tier_original"] = s["tier"]
                s["tier"] = "No Borrow"
    else:
        for s in scored:
            s["shortable"] = None
            s["borrow_fee_pct"] = None

    # Print results
    a_plus  = [s for s in scored if s.get("tier") == "A+"]
    monitor = [s for s in scored if s.get("tier") == "Monitor"]
    no_borrow = [s for s in scored if s.get("tier") == "No Borrow"]

    print(f"\n{'='*60}")
    print(f"  RESULTS: {len(a_plus)} A+ | {len(monitor)} Monitor | {len(no_borrow)} No-Borrow | {len(scored)-len(a_plus)-len(monitor)-len(no_borrow)} Skip")
    print(f"{'='*60}")

    if a_plus:
        print("\nÃÂ°ÃÂÃÂÃÂ¥ A+ SETUPS (ÃÂ¢ÃÂÃÂ¥35/60):")
        for c in a_plus:
            sc = c.get("scores", {})
            t = c.get("trade", {})
            print(f"\n  {'='*50}")
            print(f"  {c['ticker']}  |  {c.get('premarket_gap_pct',0):.1f}% gap  |  Score: {c['total_score']}/60")
            print(f"  C1:{sc.get('c1_prior_decline',0)} C2:{sc.get('c2_price_cap',0)} C3:{sc.get('c3_short_interest',0)} "
                  f"C4:{sc.get('c4_pm_spike',0)} C5:{sc.get('c5_catalyst_MANUAL',0)}(manual) C6:{sc.get('c6_pm_volume',0)}")
            if t:
                print(f"  Entry: ${t.get('entry')} | Stop: ${t.get('stop')} | T1: ${t.get('target1')} | T2: ${t.get('target2')}")
                print(f"  Shares: {t.get('shares')} | Risk: ${t.get('risk_usd')} | Reward T1: ${t.get('reward_t1')}")
            print(f"  ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ  VERIFY CATALYST MANUALLY before trading!")
            print(f"  ÃÂ°ÃÂÃÂÃÂ Reddit: https://www.reddit.com/search/?q={c['ticker']}")
            print(f"  ÃÂ°ÃÂÃÂÃÂ Stocktwits: https://stocktwits.com/symbol/{c['ticker']}")

    if monitor:
        print("\nÃÂ°ÃÂÃÂÃÂ MONITOR (20ÃÂ¢ÃÂÃÂ34/60):")
        for c in monitor[:5]:
            print(f"  {c['ticker']:6s}  {c.get('premarket_gap_pct',0):.1f}% gap  Score:{c['total_score']}/60")

    # Save results
    _save_scan(scored, "morning")

    # Telegram alert ÃÂ¢ÃÂÃÂ always fires, passes total gapper count for context
    send_telegram_alert(scored, total_gappers=len(all_gappers))

    return scored


def _save_scan(candidates: list[dict], mode: str) -> None:
    """Save scan results to latest_scan.json and history."""
    now_utc = datetime.now(timezone.utc)
    payload = {
        "scan_at_utc": now_utc.isoformat(),
        "scan_at_et": _utc_to_et_str(),
        "scan_at_sgt": _utc_to_sgt_str(),
        "mode": mode,
        "count": len(candidates),
        "a_plus_count": sum(1 for c in candidates if c.get("tier") == "A+"),
        "candidates": candidates,
    }
    with open(LATEST_SCAN_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    ts = now_utc.strftime("%Y%m%d_%H%M%S")
    hist_file = HISTORY_DIR / f"scan_{ts}.json"
    with open(hist_file, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\nÃÂ¢ÃÂÃÂ Scan saved ÃÂ¢ÃÂÃÂ {LATEST_SCAN_FILE}")
    print(f"   History  ÃÂ¢ÃÂÃÂ {hist_file}")


def run_full_mode():
    """Refresh watchlist if stale, then run morning scan."""
    print("\n" + "=" * 60)
    print("  FULL MODE")
    print("=" * 60)
    if watchlist_is_stale():
        print("  Watchlist is stale (>7 days or missing) ÃÂ¢ÃÂÃÂ rebuilding...")
        run_watchlist_mode()
    else:
        wl = load_watchlist()
        print(f"  Watchlist is fresh ({wl.get('count', 0)} stocks, "
              f"generated {wl.get('generated_at_et', '?')})")
    run_morning_mode()


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Bagholder Exit Liquidity Trading Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["watchlist", "morning", "full"],
        default="full",
        help="watchlist: build watchlist only | morning: scan only | full: both (default)",
    )
    args = parser.parse_args()

    print(f"\nÃÂ°ÃÂÃÂÃÂ Trading Scanner starting ÃÂ¢ÃÂÃÂ {_utc_to_et_str()} / {_utc_to_sgt_str()}")

    if args.mode == "watchlist":
        run_watchlist_mode()
    elif args.mode == "morning":
        run_morning_mode()
    elif args.mode == "full":
        run_full_mode()


if __name__ == "__main__":
    main()
