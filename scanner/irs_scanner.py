#!/usr/bin/env python3
"""
Institutional Reversal Signal (IRS) Scanner
---------------------------------------------
SCAN ONLY - no trading. Trading handled by irs_agent.py.

Detects stocks where institutional distribution has exhausted and insider
buying has emerged - positioning between two institutional phases:
  - Enter after institutions finish selling
  - Exit when institutions start buying again

Signal chain:
  1. 13F net reduction 2+ consecutive quarters  (distribution happened)
  2. Price 20%+ below 52W high                  (distribution suppressed price)
  3. 2+ insiders bought within 30 days          (exhaustion / insider conviction)
  4. Volume contracting (20d avg < 60d avg)     (sellers dried up)
  5. Sector ETF above 10-week MA                (macro tailwind)
  6. Profitable + revenue growing               (business intact)

Scoring (each /10, total /60):
  C1: 13F distribution depth  - quarters of net reduction
  C2: Price suppression       - % below 52W high
  C3: Insider conviction      - cluster size + dollar amount
  C4: Volume exhaustion       - 20d/60d volume ratio
  C5: Sector momentum         - ETF vs 10W MA + slope
  C6: Business quality        - revenue growth + operating margin

Tiers:
  >=40 - Enter (alert + queue for agent)
  25-39 - Watch (alert only)
  <25 - Skip

Usage:
  python irs_scanner.py --mode weekly   # Sunday: full scan, build candidate list
  python irs_scanner.py --mode daily    # Weekdays: Form 4 check, fire entry triggers
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
SCRIPT_DIR   = Path(__file__).parent.resolve()
DATA_DIR     = SCRIPT_DIR / "data"
HISTORY_DIR  = DATA_DIR / "history"
CONFIG_FILE  = SCRIPT_DIR.parent / "config.yaml"

IRS_WATCHLIST_FILE = DATA_DIR / "irs_watchlist.json"
IRS_STATE_FILE     = DATA_DIR / "irs_state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            import yaml
            with open(CONFIG_FILE) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"  [!] config.yaml load error: {e} - using defaults")
    return {}

_CFG = _load_config()
_IRS = _CFG.get("irs", {})

def _g(key, default):
    return _CFG.get(key, default)

def _i(key, default):
    return _IRS.get(key, default)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CAPITAL         = _g("capital_usd", 10_000)
RISK_PER_TRADE  = CAPITAL * _g("risk_per_trade_pct", 0.01)
STOP_PCT        = _i("stop_loss_pct",   0.10)   # 10% stop loss
TIME_STOP_DAYS  = _i("time_stop_days",  90)     # 90-day time stop
MIN_INSIDER_CLUSTER = _i("min_insider_cluster", 2)  # 2+ insiders = cluster
INSIDER_WINDOW_DAYS = _i("insider_window_days", 30) # look back 30 days
SECTOR_MA_WEEKS = _i("sector_ma_weeks", 10)     # 10-week MA for sector ETF

TIER_ENTER = _i("tier_enter_min", 40)
TIER_WATCH = _i("tier_watch_min", 25)

HEADERS = {
    "User-Agent": "trading-scanner/1.0 contact@trading-scanner.local",
    "Accept":     "application/json",
}

TV_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://www.tradingview.com",
    "Referer":         "https://www.tradingview.com/",
    "Content-Type":    "application/json",
}

# Sector ETF map: Finviz sector name -> ETF ticker
SECTOR_ETFS = {
    "Technology":           "XLK",
    "Financial":            "XLF",
    "Healthcare":           "XLV",
    "Consumer Cyclical":    "XLY",
    "Consumer Defensive":   "XLP",
    "Industrials":          "XLI",
    "Energy":               "XLE",
    "Materials":            "XLB",
    "Real Estate":          "XLRE",
    "Communication Services": "XLC",
    "Utilities":            "XLU",
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

def _today_str() -> str:
    return _now_utc().strftime("%Y-%m-%d")

def _days_ago(n: int) -> str:
    return (_now_utc() - timedelta(days=n)).strftime("%Y-%m-%d")


# ===========================================================================
# SEC EDGAR - Form 4 (Insider Transactions)
# ===========================================================================

def fetch_insider_buys(ticker: str) -> list:
    """
    Fetch recent Form 4 filings for a ticker from SEC EDGAR.
    Returns list of insider buy transactions in the last INSIDER_WINDOW_DAYS days.

    SEC EDGAR full-text search endpoint (free, no auth):
    https://efts.sec.gov/LATEST/search-index?q=%22TICKER%22&dateRange=custom&startdt=DATE&enddt=DATE&forms=4
    """
    cutoff = _days_ago(INSIDER_WINDOW_DAYS)
    today  = _today_str()

    # Step 1: get CIK for ticker
    cik = _get_cik(ticker)
    if not cik:
        return []

    # Step 2: fetch recent Form 4 filings for this company
    url = (
        f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
        f"&dateRange=custom&startdt={cutoff}&enddt={today}"
        f"&forms=4&hits.hits._source=period_of_report,display_names,file_date"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if not r.ok:
            return []
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
    except Exception as e:
        print(f"    [{ticker}] Form 4 search error: {e}")
        return []

    buys = []
    for hit in hits[:20]:  # cap at 20 filings to avoid rate limits
        src = hit.get("_source", {})
        filer_names = src.get("display_names", "")
        file_date   = src.get("file_date", "")
        accession   = hit.get("_id", "")

        if not accession or not file_date:
            continue

        # Step 3: fetch the actual filing to get transaction details
        txns = _parse_form4(accession, ticker)
        for txn in txns:
            if txn.get("transaction_type") == "P":  # P = Purchase (open market buy)
                txn["filer"]     = filer_names
                txn["file_date"] = file_date
                buys.append(txn)

        time.sleep(0.1)  # respect EDGAR rate limits

    return buys


def _get_cik(ticker: str) -> Optional[str]:
    """Resolve ticker to SEC CIK number."""
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=&CIK={ticker}&type=4&dateb=&owner=include&count=1&search_text=&output=atom"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if not r.ok:
            return None
        # Parse CIK from response
        import re
        match = re.search(r'CIK=(\d+)', r.text)
        if match:
            return match.group(1).lstrip("0")
    except Exception:
        pass

    # Fallback: company tickers JSON
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=HEADERS, timeout=15
        )
        if r.ok:
            data = r.json()
            for entry in data.values():
                if entry.get("ticker", "").upper() == ticker.upper():
                    return str(entry.get("cik_str", ""))
    except Exception:
        pass
    return None


def _parse_form4(accession_no: str, ticker: str) -> list:
    """
    Fetch and parse a Form 4 filing XML to extract transaction details.
    Returns list of transactions with type, shares, price.
    """
    # Convert accession number to filing URL
    acc_clean = accession_no.replace("-", "")
    # accession format: 0001234567-24-000001 -> cik from first 10 digits
    cik_part  = acc_clean[:10].lstrip("0")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_part}/"
        f"{acc_clean}/{accession_no}.xml"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if not r.ok:
            # Try alternate URL pattern
            url2 = f"https://www.sec.gov/Archives/edgar/data/{cik_part}/{acc_clean}/form4.xml"
            r = requests.get(url2, headers=HEADERS, timeout=10)
            if not r.ok:
                return []

        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)

        txns = []
        for txn in root.findall(".//nonDerivativeTransaction"):
            txn_code = ""
            elem = txn.find(".//transactionCode")
            if elem is not None:
                txn_code = elem.text or ""

            shares_elem = txn.find(".//transactionShares/value")
            price_elem  = txn.find(".//transactionPricePerShare/value")
            shares = float(shares_elem.text) if shares_elem is not None and shares_elem.text else 0
            price  = float(price_elem.text)  if price_elem  is not None and price_elem.text  else 0

            if shares > 0:
                txns.append({
                    "transaction_type": txn_code,
                    "shares":           shares,
                    "price_per_share":  price,
                    "total_value":      round(shares * price, 2),
                })
        return txns
    except Exception:
        return []


# ===========================================================================
# SEC EDGAR - 13F (Institutional Holdings)
# ===========================================================================

def fetch_13f_net_change(ticker: str) -> dict:
    """
    Estimate net institutional position change over last 2-4 quarters.

    Approach: query SEC EDGAR company facts for the ticker's CIK,
    then look at 13F-HR filings that mention this ticker as a holding.

    Returns:
      {
        "quarters_of_reduction": int,   # consecutive quarters net sold
        "quarters_of_data":      int,   # how many quarters we have
        "net_direction":         str,   # "reducing" | "accumulating" | "mixed" | "unknown"
        "detail":                str,   # human-readable summary
      }

    Note: Full 13F parsing across all filers is computationally expensive.
    We use the EDGAR full-text search to find filings that mention this ticker,
    sample the top 10 most recent filers, and compute directional change.
    This is an approximation — directionally reliable, not precise share counts.
    """
    result = {
        "quarters_of_reduction": 0,
        "quarters_of_data":      0,
        "net_direction":         "unknown",
        "detail":                "Could not fetch 13F data",
    }

    # Search for 13F filings mentioning this ticker
    url = (
        f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
        f"&forms=13F-HR&dateRange=custom"
        f"&startdt={_days_ago(365)}&enddt={_today_str()}"
        f"&hits.hits.total.value=true"
        f"&hits.hits._source=period_of_report,display_names,file_date"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if not r.ok:
            return result
        data   = r.json()
        hits   = data.get("hits", {}).get("hits", [])
        total  = data.get("hits", {}).get("total", {}).get("value", 0)

        if not hits:
            result["detail"] = "No 13F filings found mentioning this ticker"
            return result

        # Group by period_of_report to get quarterly snapshots
        by_quarter = {}
        for hit in hits[:50]:
            src     = hit.get("_source", {})
            period  = src.get("period_of_report", "")[:7]  # YYYY-MM
            if period:
                by_quarter.setdefault(period, 0)
                by_quarter[period] += 1

        quarters = sorted(by_quarter.keys(), reverse=True)
        result["quarters_of_data"] = len(quarters)

        if len(quarters) < 2:
            result["detail"] = f"Only {len(quarters)} quarter(s) of data — insufficient"
            return result

        # Directional signal: is filing count declining (fewer funds holding)?
        # This is a proxy — fewer 13F mentions = funds exiting
        counts = [by_quarter[q] for q in quarters[:4]]  # last 4 quarters

        # Count consecutive quarters of decline
        consecutive_decline = 0
        for i in range(len(counts) - 1):
            if counts[i] < counts[i + 1]:  # current < prior = declining
                consecutive_decline += 1
            else:
                break

        result["quarters_of_reduction"] = consecutive_decline
        result["quarters_of_data"]      = len(quarters)

        if consecutive_decline >= 2:
            result["net_direction"] = "reducing"
        elif counts[0] > counts[1] if len(counts) >= 2 else False:
            result["net_direction"] = "accumulating"
        else:
            result["net_direction"] = "mixed"

        result["detail"] = (
            f"{total} total 13F mentions | "
            f"Last 4Q filing counts: {counts[:4]} | "
            f"{consecutive_decline} consecutive Q decline"
        )

    except Exception as e:
        result["detail"] = f"13F fetch error: {str(e)[:60]}"

    return result


# ===========================================================================
# Finviz - Fundamentals
# ===========================================================================

def fetch_finviz_fundamentals(tickers: list) -> dict:
    """
    Scrape Finviz for fundamental data on a list of tickers.
    Returns { TICKER: { eps, revenue_growth_pct, operating_margin, pe_ratio,
                        sector, industry, price, week52_high, week52_low } }
    Uses Finviz screener export (CSV) for batch efficiency.
    """
    if not tickers:
        return {}

    print(f"  Finviz - fetching fundamentals for {len(tickers)} tickers...")
    results = {}

    # Finviz screener: query specific tickers
    # Use the export URL which returns CSV
    ticker_str = ",".join(tickers[:100])  # cap at 100 per request
    url = (
        "https://finviz.com/screener.ashx?v=152&t=" + ticker_str +
        "&o=-marketcap"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if not r.ok:
            print(f"  [!] Finviz error: {r.status_code}")
            return results

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")

        # Find the screener results table
        table = soup.find("table", {"id": "screener-views-table"})
        if not table:
            # Try alternate selector
            tables = soup.find_all("table", class_="table-light")
            table = tables[0] if tables else None

        if not table:
            print("  [!] Finviz: could not find results table")
            return results

        rows = table.find_all("tr")
        headers_row = rows[0] if rows else None
        if not headers_row:
            return results

        col_names = [th.get_text(strip=True) for th in headers_row.find_all("td")]

        def _col(row_cells, name):
            try:
                idx = col_names.index(name)
                return row_cells[idx].get_text(strip=True)
            except (ValueError, IndexError):
                return None

        def _pct(val):
            if not val or val == "-":
                return None
            try:
                return float(val.replace("%", "").replace(",", ""))
            except Exception:
                return None

        def _flt(val):
            if not val or val == "-":
                return None
            try:
                return float(val.replace(",", "").replace("B", "e9").replace("M", "e6"))
            except Exception:
                return None

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            ticker = _col(cells, "Ticker")
            if not ticker:
                # First cell is usually ticker in some layouts
                ticker = cells[1].get_text(strip=True) if len(cells) > 1 else None
            if not ticker:
                continue

            results[ticker.upper()] = {
                "ticker":             ticker.upper(),
                "sector":             _col(cells, "Sector"),
                "industry":           _col(cells, "Industry"),
                "price":              _flt(_col(cells, "Price")),
                "pe_ratio":           _flt(_col(cells, "P/E")),
                "eps_ttm":            _flt(_col(cells, "EPS (ttm)")),
                "revenue_growth_pct": _pct(_col(cells, "Sales Q/Q")),  # quarterly revenue growth
                "eps_growth_pct":     _pct(_col(cells, "EPS Q/Q")),
                "operating_margin":   _pct(_col(cells, "Oper. Margin")),
                "gross_margin":       _pct(_col(cells, "Gross Margin")),
                "week52_high":        _flt(_col(cells, "52W High")),
                "week52_low":         _flt(_col(cells, "52W Low")),
                "avg_volume":         _flt(_col(cells, "Avg Volume")),
                "market_cap_m":       _flt(_col(cells, "Market Cap")),
                "short_float_pct":    _pct(_col(cells, "Short Float")),
            }

    except Exception as e:
        print(f"  [!] Finviz scrape error: {e}")

    print(f"  Finviz: got data for {len(results)} tickers")
    return results


def fetch_finviz_screener() -> list:
    """
    Fetch a broad universe of stocks from Finviz screener.
    Filters: EPS > 0, price > $5, market cap > $500M, avg volume > 200K.
    Returns list of tickers to evaluate further.
    """
    print("  Finviz screener - fetching candidate universe...")
    # Screener URL: profitable (EPS>0), price $5+, mktcap $500M+, avg vol 200K+
    url = (
        "https://finviz.com/screener.ashx?v=111"
        "&f=fa_eps_pos,sh_price_o5,cap_midover,sh_avgvol_o200"
        "&o=-marketcap"
        "&r=1"  # start from row 1
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    tickers = []
    page = 1
    max_pages = 5  # cap at 500 stocks to keep runtime reasonable

    while page <= max_pages:
        row_start = (page - 1) * 100 + 1
        paged_url = url + f"&r={row_start}"
        try:
            r = requests.get(paged_url, headers=headers, timeout=20)
            if not r.ok:
                break

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")

            # Find ticker links
            ticker_links = soup.find_all("a", class_="screener-link-primary")
            if not ticker_links:
                break

            page_tickers = [a.get_text(strip=True) for a in ticker_links]
            tickers.extend(page_tickers)

            if len(page_tickers) < 100:
                break  # last page

            page += 1
            time.sleep(0.8)

        except Exception as e:
            print(f"  [!] Finviz screener page {page} error: {e}")
            break

    print(f"  Finviz screener: found {len(tickers)} candidates")
    return list(set(tickers))  # deduplicate


# ===========================================================================
# TradingView - Price + Volume
# ===========================================================================

def fetch_tv_price_volume(tickers: list) -> dict:
    """
    Fetch current price, volume (20d + 60d avg), and 52W high from TradingView.
    Returns { TICKER: { price, volume_20d, volume_60d, week52_high } }
    """
    if not tickers:
        return {}

    print(f"  TradingView - fetching price/volume for {len(tickers)} tickers...")
    url = "https://scanner.tradingview.com/america/scan"
    results = {}

    # Batch into groups of 100
    tv_tickers = []
    for t in tickers:
        tv_tickers += [f"NASDAQ:{t}", f"NYSE:{t}", f"AMEX:{t}"]

    for i in range(0, len(tv_tickers), 150):
        batch = tv_tickers[i:i + 150]
        payload = {
            "markets": ["america"],
            "symbols": {"query": {"types": ["stock"]}, "tickers": batch},
            "options": {"lang": "en"},
            "columns": [
                "name",
                "close",
                "volume",
                "average_volume_10d_calc",
                "average_volume_60d_calc",
                "High.All",             # 52W high
                "Low.All",              # 52W low
            ],
            "filter": [{"left": "is_primary", "operation": "equal", "right": True}],
            "sort":   {"sortBy": "market_cap_basic", "sortOrder": "desc"},
            "range":  [0, 500],
        }
        try:
            r = requests.post(url, json=payload, headers=TV_HEADERS, timeout=20)
            if not r.ok:
                continue
            data = r.json()
        except Exception as e:
            print(f"  [!] TradingView batch error: {e}")
            continue

        for item in data.get("data", []):
            d = item.get("d", [])
            if len(d) < 7:
                continue
            name, price, vol, avg10, avg60, hi52, lo52 = d[:7]
            if not name:
                continue
            ticker = name.split(":")[1] if ":" in name else name
            if ticker in results:
                continue  # take first match (usually right exchange)
            results[ticker.upper()] = {
                "price":      float(price)  if price  else None,
                "volume":     float(vol)    if vol    else None,
                "avg_vol_10": float(avg10)  if avg10  else None,
                "avg_vol_60": float(avg60)  if avg60  else None,
                "week52_high": float(hi52)  if hi52   else None,
                "week52_low":  float(lo52)  if lo52   else None,
            }

        time.sleep(0.3)

    print(f"  TradingView: got data for {len(results)} tickers")
    return results


def fetch_sector_etf_momentum(sector: str) -> dict:
    """
    Check if a sector ETF is above its 10-week MA and trending up.
    Returns { "etf": str, "above_ma": bool, "pct_above_ma": float,
              "slope_positive": bool, "detail": str }
    """
    etf = SECTOR_ETFS.get(sector)
    result = {
        "etf":            etf or "unknown",
        "above_ma":       False,
        "pct_above_ma":   0.0,
        "slope_positive": False,
        "detail":         "No ETF mapping for sector",
    }
    if not etf:
        return result

    # Fetch weekly bars for the ETF (70 weeks = enough for 10W MA + slope)
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": ["fund"]}, "tickers": [f"AMEX:{etf}"]},
        "options": {"lang": "en"},
        "columns": [
            "name",
            "close",
            "SMA10",          # TradingView built-in: 10-period SMA (weekly chart = 10W)
            "change",         # % change
            "Perf.3M",        # 3-month performance
        ],
        "filter": [],
        "range": [0, 1],
    }
    try:
        r = requests.post(url, json=payload, headers=TV_HEADERS, timeout=15)
        if not r.ok:
            result["detail"] = f"TradingView error {r.status_code}"
            return result
        data   = r.json()
        items  = data.get("data", [])
        if not items:
            result["detail"] = f"No data for {etf}"
            return result

        d = items[0].get("d", [])
        if len(d) < 5:
            result["detail"] = "Insufficient data fields"
            return result

        name, price, sma10, change_pct, perf_3m = d[:5]
        price  = float(price)  if price  else None
        sma10  = float(sma10)  if sma10  else None
        perf3m = float(perf_3m) if perf_3m else None

        if price and sma10:
            pct_above = (price - sma10) / sma10 * 100
            result["above_ma"]       = price > sma10
            result["pct_above_ma"]   = round(pct_above, 2)
            result["slope_positive"] = (perf3m or 0) > 0
            result["detail"] = (
                f"{etf} @ ${price:.2f} | 10W MA: ${sma10:.2f} "
                f"({'above' if price > sma10 else 'below'} by {abs(pct_above):.1f}%) | "
                f"3M perf: {perf3m:.1f}%" if perf3m else f"{etf} @ ${price:.2f}"
            )
        else:
            result["detail"] = f"{etf}: price or SMA not available"

    except Exception as e:
        result["detail"] = f"Sector ETF error: {str(e)[:60]}"

    return result


# ===========================================================================
# Scoring Engine
# ===========================================================================

def score_candidate(candidate: dict) -> dict:
    """
    Score a candidate stock on all 6 IRS conditions.
    candidate must have keys from fetch_finviz_fundamentals +
    fetch_tv_price_volume + fetch_insider_buys + fetch_13f_net_change.
    """

    # ------------------------------------------------------------------
    # C1: 13F distribution depth - how many consecutive quarters of net reduction
    # ------------------------------------------------------------------
    q_decline = candidate.get("13f_quarters_of_reduction", 0)
    if q_decline >= 4:      c1 = 10
    elif q_decline == 3:    c1 = 8
    elif q_decline == 2:    c1 = 6
    elif q_decline == 1:    c1 = 3
    else:                   c1 = 1  # no data or accumulating

    # ------------------------------------------------------------------
    # C2: Price suppression - % below 52W high
    # ------------------------------------------------------------------
    price    = candidate.get("price") or 0
    hi52     = candidate.get("week52_high") or 0
    if price > 0 and hi52 > 0:
        pct_below_hi = (hi52 - price) / hi52 * 100
    else:
        pct_below_hi = 0

    if pct_below_hi >= 50:      c2 = 10
    elif pct_below_hi >= 35:    c2 = 7
    elif pct_below_hi >= 20:    c2 = 4
    elif pct_below_hi >= 10:    c2 = 2
    else:                        c2 = 0  # near high - no suppression

    # ------------------------------------------------------------------
    # C3: Insider conviction - cluster size + dollar amount
    # ------------------------------------------------------------------
    insider_buys   = candidate.get("insider_buys", [])
    cluster_size   = len(insider_buys)
    total_value    = sum(b.get("total_value", 0) for b in insider_buys)

    if cluster_size >= 4:       c3_size = 10
    elif cluster_size == 3:     c3_size = 8
    elif cluster_size == 2:     c3_size = 6
    elif cluster_size == 1:     c3_size = 3
    else:                        c3_size = 0

    # Bonus for dollar amount
    if total_value >= 1_000_000:    c3_val = 2
    elif total_value >= 500_000:    c3_val = 1
    else:                            c3_val = 0

    c3 = min(10, c3_size + c3_val)

    # ------------------------------------------------------------------
    # C4: Volume exhaustion - 20d avg vs 60d avg (sellers drying up)
    # ------------------------------------------------------------------
    vol_20 = candidate.get("avg_vol_10") or 0   # using 10d as proxy for recent
    vol_60 = candidate.get("avg_vol_60") or 0

    if vol_20 > 0 and vol_60 > 0:
        vol_ratio = vol_20 / vol_60  # <1 = volume contracting = exhaustion
        if vol_ratio <= 0.50:       c4 = 10  # volume halved
        elif vol_ratio <= 0.65:     c4 = 8
        elif vol_ratio <= 0.80:     c4 = 6
        elif vol_ratio <= 0.90:     c4 = 4
        elif vol_ratio <= 1.00:     c4 = 2
        else:                        c4 = 0  # volume expanding = sellers still active
    else:
        c4 = 3  # unknown

    # ------------------------------------------------------------------
    # C5: Sector momentum - ETF above 10W MA
    # ------------------------------------------------------------------
    sector_data = candidate.get("sector_momentum", {})
    above_ma    = sector_data.get("above_ma",       False)
    pct_above   = sector_data.get("pct_above_ma",   0)
    slope_pos   = sector_data.get("slope_positive", False)

    if above_ma and slope_pos:
        if pct_above >= 10:     c5 = 10
        elif pct_above >= 5:    c5 = 8
        elif pct_above >= 2:    c5 = 6
        else:                    c5 = 4
    elif above_ma:
        c5 = 3  # above MA but momentum flat
    else:
        c5 = 0  # below MA - sector headwind

    # ------------------------------------------------------------------
    # C6: Business quality - revenue growth + operating margin
    # ------------------------------------------------------------------
    rev_growth = candidate.get("revenue_growth_pct") or 0
    op_margin  = candidate.get("operating_margin")   or 0

    # Revenue growth component (0-5)
    if rev_growth >= 20:        c6_rev = 5
    elif rev_growth >= 10:      c6_rev = 4
    elif rev_growth >= 5:       c6_rev = 3
    elif rev_growth >= 0:       c6_rev = 2
    else:                        c6_rev = 0  # declining revenue

    # Operating margin component (0-5)
    if op_margin >= 20:         c6_mar = 5
    elif op_margin >= 10:       c6_mar = 4
    elif op_margin >= 5:        c6_mar = 3
    elif op_margin >= 0:        c6_mar = 2
    else:                        c6_mar = 0  # operating at a loss

    c6 = c6_rev + c6_mar

    # ------------------------------------------------------------------
    # Total
    # ------------------------------------------------------------------
    total = c1 + c2 + c3 + c4 + c5 + c6
    tier  = "Enter" if total >= TIER_ENTER else "Watch" if total >= TIER_WATCH else "Skip"

    candidate["scores"] = {
        "c1_13f_distribution":  c1,
        "c2_price_suppression": c2,
        "c3_insider_conviction": c3,
        "c4_volume_exhaustion": c4,
        "c5_sector_momentum":   c5,
        "c6_business_quality":  c6,
        "total":                total,
        "tier":                 tier,
    }
    candidate["total_score"] = total
    candidate["tier"]        = tier

    # Trade parameters (for alert + agent consumption)
    if tier in ("Enter", "Watch") and price > 0:
        stop          = round(price * (1 - STOP_PCT), 4)
        stop_distance = price - stop
        shares        = int(RISK_PER_TRADE / stop_distance) if stop_distance > 0 else 0
        target        = round(hi52, 4) if hi52 else round(price * 1.20, 4)  # 52W high or +20%
        reward        = round(shares * (target - price), 2) if shares else 0
        rr            = round(reward / RISK_PER_TRADE, 2) if RISK_PER_TRADE > 0 and shares else 0

        candidate["trade"] = {
            "entry":          price,
            "stop":           stop,
            "stop_distance":  round(stop_distance, 4),
            "target":         target,
            "shares":         shares,
            "risk_usd":       RISK_PER_TRADE,
            "reward_usd":     reward,
            "rr_ratio":       rr,
            "time_stop_days": TIME_STOP_DAYS,
        }

    return candidate


# ===========================================================================
# State management
# ===========================================================================

def _load_watchlist() -> dict:
    if IRS_WATCHLIST_FILE.exists():
        with open(IRS_WATCHLIST_FILE) as f:
            return json.load(f)
    return {"generated_at": None, "candidates": {}}


def _save_watchlist(data: dict) -> None:
    with open(IRS_WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _load_state() -> dict:
    if IRS_STATE_FILE.exists():
        with open(IRS_STATE_FILE) as f:
            return json.load(f)
    return {"date": _today_str(), "triggered": {}}


def _save_state(state: dict) -> None:
    with open(IRS_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ===========================================================================
# Telegram
# ===========================================================================

def _send_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  [Telegram] Env vars not set - skipping.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        payload = json.dumps(
            {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            ensure_ascii=False
        ).encode("utf-8")
        r = requests.post(
            url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        r.raise_for_status()
        print("  [Telegram] Message sent.")
    except Exception as e:
        print(f"  [Telegram] Failed: {e}")


def _send_weekly_alert(enter: list, watch: list) -> None:
    if not enter and not watch:
        _send_telegram(
            f"*IRS Weekly Scan* - {_fmt_et_sgt()}\n"
            "No qualifying candidates this week."
        )
        return

    now_str = _fmt_et_sgt()
    lines = [f"*IRS Weekly Scan* - {now_str}", ""]

    if enter:
        lines.append(f"*{len(enter)} ENTER candidate(s)* - awaiting Form 4 trigger")
        lines.append("")
        for c in enter:
            sc = c.get("scores", {})
            t  = c.get("trade", {})
            insider_count = len(c.get("insider_buys", []))
            lines += [
                f"*{c['ticker']}*  Score: {c['total_score']}/60  [{c['sector']}]",
                f"  Price: ${c.get('price','?')}  |  52W High: ${c.get('week52_high','?')}  "
                f"({c.get('pct_below_52wh','?'):.1f}% below)",
                f"  C1(13F):{sc.get('c1_13f_distribution')} "
                f"C2(price):{sc.get('c2_price_suppression')} "
                f"C3(insider):{sc.get('c3_insider_conviction')} "
                f"C4(vol):{sc.get('c4_volume_exhaustion')} "
                f"C5(sector):{sc.get('c5_sector_momentum')} "
                f"C6(biz):{sc.get('c6_business_quality')}",
                f"  Insiders buying: {insider_count} in last 30d  |  "
                f"13F: {c.get('13f_detail','?')}",
                f"  Entry: ${t.get('entry','?')}  Stop: ${t.get('stop','?')} (-{STOP_PCT*100:.0f}%)  "
                f"Target: ${t.get('target','?')}  R/R: {t.get('rr_ratio','?')}:1",
                "",
            ]

    if watch:
        lines.append(f"*{len(watch)} WATCH candidate(s)* - need more signals")
        for c in watch[:5]:
            sc = c.get("scores", {})
            lines.append(
                f"  - *{c['ticker']}*  {c['total_score']}/60  [{c.get('sector','')}]  "
                f"C3(insider):{sc.get('c3_insider_conviction')} "
                f"C1(13F):{sc.get('c1_13f_distribution')}"
            )

    lines += [
        "",
        "_Entry orders placed Monday pre-market by IRS agent._",
        "_Stop: 10% below entry. Target: 52W high. Time stop: 90 days._",
        "_C2 (Earnings quality) always verify manually._",
    ]
    _send_telegram("\n".join(lines))


def _send_entry_trigger_alert(candidate: dict) -> None:
    sc = candidate.get("scores", {})
    t  = candidate.get("trade", {})
    buys = candidate.get("insider_buys", [])
    insider_lines = []
    for b in buys[:4]:
        val_str = f"${b.get('total_value', 0):,.0f}" if b.get("total_value") else "?"
        insider_lines.append(
            f"  - {b.get('filer','?')}  {b.get('shares',0):,.0f} shares @ "
            f"${b.get('price_per_share','?')}  ({val_str})  [{b.get('file_date','')}]"
        )

    lines = [
        f"*IRS ENTRY TRIGGER* - {_fmt_et_sgt()}",
        f"*{candidate['ticker']}*  Score: {candidate['total_score']}/60  Tier: Enter",
        "",
        f"*Insider cluster detected ({len(buys)} buys in 30d):*",
    ] + insider_lines + [
        "",
        f"  Price: ${candidate.get('price','?')}  |  52W High: ${candidate.get('week52_high','?')}",
        f"  13F: {candidate.get('13f_detail','?')}",
        f"  Sector: {candidate.get('sector','')} ({candidate.get('sector_momentum',{}).get('detail','')})",
        f"  Rev growth: {candidate.get('revenue_growth_pct','?')}%  |  Op margin: {candidate.get('operating_margin','?')}%",
        "",
        f"  Entry: ${t.get('entry','?')}  |  Stop: ${t.get('stop','?')} (-{STOP_PCT*100:.0f}%)",
        f"  Target: ${t.get('target','?')} (52W high)  |  R/R: {t.get('rr_ratio','?')}:1",
        f"  Shares: {t.get('shares','?')}  |  Risk: ${t.get('risk_usd','?')}",
        f"  Time stop: {TIME_STOP_DAYS} days",
        "",
        "_IRS agent will place limit buy order at Monday open._",
        "*Verify no adverse news before market open.*",
    ]
    _send_telegram("\n".join(lines))


# ===========================================================================
# Run modes
# ===========================================================================

def mode_weekly():
    """
    Sunday: full scan.
    1. Fetch candidate universe from Finviz screener
    2. Fetch fundamentals + price/volume data
    3. Check 13F distribution + sector momentum
    4. Score all candidates
    5. Save watchlist of Watch+ candidates for daily Form 4 monitoring
    6. Alert
    """
    print(f"\n{'='*60}")
    print(f"  IRS SCANNER - WEEKLY SCAN - {_fmt_et_sgt()}")
    print(f"{'='*60}")

    # Step 1: get universe
    tickers = fetch_finviz_screener()
    if not tickers:
        print("  No candidates from Finviz screener.")
        _send_telegram(f"*IRS Weekly Scan* - {_fmt_et_sgt()}\nFinviz screener returned 0 results.")
        return

    # Step 2: fundamentals filter - must be profitable + revenue growing
    print(f"\n  Fetching fundamentals for {len(tickers)} tickers...")
    fundamentals = fetch_finviz_fundamentals(tickers[:200])

    # Apply hard filters
    qualified = []
    for ticker, f in fundamentals.items():
        eps = f.get("eps_ttm") or 0
        rev = f.get("revenue_growth_pct")
        op  = f.get("operating_margin") or 0
        hi  = f.get("week52_high") or 0
        pr  = f.get("price") or 0

        # Must be profitable
        if eps <= 0:
            continue
        # Revenue must be growing (or unknown)
        if rev is not None and rev < 0:
            continue
        # Operating margin must be positive
        if op <= 0:
            continue
        # Must be at least 15% below 52W high (needs some suppression)
        if hi > 0 and pr > 0:
            pct_below = (hi - pr) / hi * 100
            if pct_below < 15:
                continue
            f["pct_below_52wh"] = round(pct_below, 2)
        else:
            f["pct_below_52wh"] = 0

        qualified.append(f)

    print(f"  After fundamental filters: {len(qualified)} candidates")

    if not qualified:
        _send_telegram(f"*IRS Weekly Scan* - {_fmt_et_sgt()}\nNo candidates passed fundamental filters.")
        return

    # Step 3: price/volume from TradingView
    qual_tickers = [c["ticker"] for c in qualified]
    tv_data = fetch_tv_price_volume(qual_tickers)

    # Merge TV data
    for c in qualified:
        tv = tv_data.get(c["ticker"], {})
        c.update({k: v for k, v in tv.items() if v is not None})

    # Step 4: 13F distribution check (rate-limited — only top 50 candidates by price suppression)
    qualified.sort(key=lambda x: x.get("pct_below_52wh", 0), reverse=True)
    top_candidates = qualified[:50]

    print(f"\n  Checking 13F distribution for top {len(top_candidates)} candidates...")
    for c in top_candidates:
        print(f"  [{c['ticker']}] 13F check...", end=" ", flush=True)
        result = fetch_13f_net_change(c["ticker"])
        c["13f_quarters_of_reduction"] = result["quarters_of_reduction"]
        c["13f_net_direction"]         = result["net_direction"]
        c["13f_detail"]                = result["detail"]
        print(result["net_direction"])
        time.sleep(0.5)

    # Step 5: sector ETF momentum
    print(f"\n  Checking sector momentum...")
    sector_cache = {}
    for c in top_candidates:
        sector = c.get("sector", "")
        if sector not in sector_cache:
            sector_cache[sector] = fetch_sector_etf_momentum(sector)
            time.sleep(0.3)
        c["sector_momentum"] = sector_cache[sector]

    # Step 6: check insider buys for candidates with 13F reduction
    candidates_with_distribution = [
        c for c in top_candidates if c.get("13f_quarters_of_reduction", 0) >= 2
    ]
    print(f"\n  Checking insider buys for {len(candidates_with_distribution)} candidates with 13F distribution...")
    for c in candidates_with_distribution:
        print(f"  [{c['ticker']}] Form 4 check...", end=" ", flush=True)
        buys = fetch_insider_buys(c["ticker"])
        c["insider_buys"] = buys
        print(f"{len(buys)} buy(s)")
        time.sleep(0.5)

    # Fill in empty insider buys for others
    for c in top_candidates:
        if "insider_buys" not in c:
            c["insider_buys"] = []

    # Step 7: score all
    scored = [score_candidate(c) for c in top_candidates]
    scored.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    enter = [c for c in scored if c.get("tier") == "Enter"]
    watch = [c for c in scored if c.get("tier") == "Watch"]
    skip  = [c for c in scored if c.get("tier") == "Skip"]

    print(f"\n  RESULTS: {len(enter)} Enter | {len(watch)} Watch | {len(skip)} Skip")
    for c in enter + watch[:3]:
        sc = c.get("scores", {})
        print(f"  {c['ticker']:6s}  {c['total_score']}/60  [{c.get('tier')}]  "
              f"C1:{sc.get('c1_13f_distribution')} C2:{sc.get('c2_price_suppression')} "
              f"C3:{sc.get('c3_insider_conviction')} C4:{sc.get('c4_volume_exhaustion')} "
              f"C5:{sc.get('c5_sector_momentum')} C6:{sc.get('c6_business_quality')}")

    # Save watchlist (Watch+ candidates for daily monitoring)
    watchlist = {
        "generated_at": _fmt_et_sgt(),
        "candidates": {c["ticker"]: c for c in enter + watch},
    }
    _save_watchlist(watchlist)

    # Archive
    hist = HISTORY_DIR / f"irs_weekly_{_now_et().strftime('%Y%m%d')}.json"
    with open(hist, "w") as f:
        json.dump({"scanned_at": _fmt_et_sgt(), "scored": scored}, f, indent=2, default=str)

    _send_weekly_alert(enter, watch)


def mode_daily():
    """
    Weekdays: check Form 4 filings for candidates in watchlist.
    If a candidate now has 2+ insider buys in 30 days → fire entry trigger.
    """
    print(f"\n{'='*60}")
    print(f"  IRS SCANNER - DAILY FORM 4 CHECK - {_fmt_et_sgt()}")
    print(f"{'='*60}")

    watchlist = _load_watchlist()
    candidates = watchlist.get("candidates", {})

    if not candidates:
        print("  No IRS watchlist candidates — run weekly scan first.")
        return

    state    = _load_state()
    triggered = state.get("triggered", {})
    today    = _today_str()

    print(f"  Monitoring {len(candidates)} candidates for insider cluster...")

    newly_triggered = []
    for ticker, candidate in candidates.items():
        # Skip if already triggered today
        if triggered.get(ticker, {}).get("date") == today:
            print(f"  [{ticker}] Already triggered today - skipping.")
            continue

        print(f"  [{ticker}] Form 4 check...", end=" ", flush=True)
        buys = fetch_insider_buys(ticker)
        candidate["insider_buys"] = buys
        print(f"{len(buys)} buy(s)")

        # Re-score with fresh insider data
        candidate = score_candidate(candidate)

        # Check if cluster threshold met
        cluster_met = len(buys) >= MIN_INSIDER_CLUSTER

        if cluster_met and candidate.get("tier") in ("Enter", "Watch"):
            print(f"  [{ticker}] ENTRY TRIGGER - {len(buys)} insiders bought in 30 days!")
            triggered[ticker] = {"date": today, "score": candidate["total_score"]}
            newly_triggered.append(candidate)
            # Update watchlist with fresh data
            candidates[ticker] = candidate

        time.sleep(0.5)

    # Save updated state + watchlist
    state["triggered"] = triggered
    _save_state(state)
    watchlist["candidates"] = candidates
    _save_watchlist(watchlist)

    if newly_triggered:
        print(f"\n  {len(newly_triggered)} entry trigger(s) fired!")
        for c in newly_triggered:
            _send_entry_trigger_alert(c)
    else:
        print(f"\n  No new entry triggers today.")


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="IRS Scanner")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["weekly", "daily"],
        help="weekly: full scan (Sundays) | daily: Form 4 monitor (weekdays)",
    )
    args = parser.parse_args()
    {"weekly": mode_weekly, "daily": mode_daily}[args.mode]()


if __name__ == "__main__":
    main()
