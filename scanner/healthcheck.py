#!/usr/bin/env python3
"""
Daily Data Source Health Check
--------------------------------
Pings all data sources used by the trading scanners and sends a Telegram
summary. Runs before the morning scanner so you know immediately if silence
is due to a data outage vs no qualifying setups.

Sources checked:
  - TradingView Scanner API   (scanner.py, orf_scanner.py, earnings_scanner.py)
  - Barchart.com              (scanner.py)
  - StockAnalysis.com         (scanner.py)
  - iborrowdesk.com           (scanner.py, earnings_scanner.py)
  - Finviz.com                (scanner.py - weekly watchlist)
  - Nasdaq Earnings API       (earnings_scanner.py)
  - Alpaca Paper API          (alpaca_agent.py)

Usage:
  python healthcheck.py
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

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

def _fmt_et_sgt() -> str:
    now = _now_utc()
    et_label = "EDT" if 3 <= now.month <= 11 else "EST"
    et = _utc_to_et(now).strftime("%Y-%m-%d %H:%M")
    sgt = _utc_to_sgt(now).strftime("%H:%M")
    return f"{et} {et_label} / {sgt} SGT"

def _today_et() -> str:
    return _utc_to_et(_now_utc()).strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR  = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR.parent / "config.yaml"

def _load_alpaca_keys() -> tuple:
    if CONFIG_FILE.exists():
        try:
            import yaml
            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("alpaca_api_key", ""), cfg.get("alpaca_secret_key", "")
        except Exception:
            pass
    return os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 15  # seconds per check

# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_tradingview() -> dict:
    """POST to TradingView scanner — expect data array back."""
    name = "TradingView Scanner"
    url  = "https://scanner.tradingview.com/america/scan"
    payload = {
        "markets": ["america"],
        "symbols": {"query": {"types": ["stock"]}, "tickers": []},
        "options": {"lang": "en"},
        "columns": ["name", "close", "premarket_change_percent", "premarket_volume"],
        "filter": [
            {"left": "is_primary",               "operation": "equal",   "right": True},
            {"left": "premarket_change_percent",  "operation": "greater", "right": 20},
        ],
        "sort":  {"sortBy": "premarket_change_percent", "sortOrder": "desc"},
        "range": [0, 5],
    }
    headers = {
        **HEADERS,
        "Origin":       "https://www.tradingview.com",
        "Referer":      "https://www.tradingview.com/",
        "Content-Type": "application/json",
    }
    start = time.time()
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        ms = int((time.time() - start) * 1000)
        if r.status_code == 200:
            data  = r.json()
            count = data.get("totalCount", len(data.get("data", [])))
            return {"name": name, "ok": True,  "ms": ms, "detail": f"{count} pre-market gappers >20%"}
        return {"name": name, "ok": False, "ms": ms, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        return {"name": name, "ok": False, "ms": ms, "detail": str(e)[:80]}


def check_barchart() -> dict:
    """GET Barchart pre-market movers page — expect HTML with table data."""
    name = "Barchart.com"
    url  = "https://www.barchart.com/stocks/quotes/percent_change/greater_than/10?reportPage=1"
    start = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        ms = int((time.time() - start) * 1000)
        if r.status_code == 200 and len(r.text) > 1000:
            return {"name": name, "ok": True,  "ms": ms, "detail": f"{len(r.text)//1024}KB received"}
        return {"name": name, "ok": False, "ms": ms, "detail": f"HTTP {r.status_code} or empty response"}
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        return {"name": name, "ok": False, "ms": ms, "detail": str(e)[:80]}


def check_stockanalysis() -> dict:
    """GET StockAnalysis pre-market page."""
    name = "StockAnalysis.com"
    url  = "https://stockanalysis.com/markets/pre-market/"
    start = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        ms = int((time.time() - start) * 1000)
        if r.status_code == 200 and len(r.text) > 1000:
            return {"name": name, "ok": True,  "ms": ms, "detail": f"{len(r.text)//1024}KB received"}
        return {"name": name, "ok": False, "ms": ms, "detail": f"HTTP {r.status_code} or empty response"}
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        return {"name": name, "ok": False, "ms": ms, "detail": str(e)[:80]}


def check_iborrowdesk() -> dict:
    """GET iborrowdesk API for a known ticker (TSLA)."""
    name = "iborrowdesk.com"
    url  = "https://iborrowdesk.com/api/ticker/TSLA"
    start = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        ms = int((time.time() - start) * 1000)
        if r.status_code == 200:
            data = r.json()
            has_data = bool(data.get("ibkr") or data.get("schwab"))
            return {"name": name, "ok": True,  "ms": ms, "detail": "TSLA borrow data received" if has_data else "Connected (no data)"}
        return {"name": name, "ok": False, "ms": ms, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        return {"name": name, "ok": False, "ms": ms, "detail": str(e)[:80]}


def check_finviz() -> dict:
    """GET Finviz screener — used weekly for watchlist build."""
    name = "Finviz.com (weekly watchlist)"
    url  = "https://finviz.com/screener.ashx?v=111&f=sh_price_u10,sh_short_u10&o=-shortfloat"
    start = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        ms = int((time.time() - start) * 1000)
        if r.status_code == 200 and len(r.text) > 1000:
            return {"name": name, "ok": True,  "ms": ms, "detail": f"{len(r.text)//1024}KB received"}
        return {"name": name, "ok": False, "ms": ms, "detail": f"HTTP {r.status_code} or empty response"}
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        return {"name": name, "ok": False, "ms": ms, "detail": str(e)[:80]}


def check_nasdaq_earnings() -> dict:
    """GET Nasdaq earnings calendar API for today."""
    name = "Nasdaq Earnings API"
    today = _today_et()
    url   = f"https://api.nasdaq.com/api/calendar/earnings?date={today}"
    start = time.time()
    try:
        r = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
        ms = int((time.time() - start) * 1000)
        if r.status_code == 200:
            data  = r.json()
            rows  = (
                data.get("data", {}).get("rows", [])
                or data.get("data", {}).get("earning", {}).get("rows", [])
                or []
            )
            return {"name": name, "ok": True,  "ms": ms, "detail": f"{len(rows)} earnings today ({today})"}
        return {"name": name, "ok": False, "ms": ms, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        return {"name": name, "ok": False, "ms": ms, "detail": str(e)[:80]}


def check_alpaca() -> dict:
    """GET Alpaca paper account — confirm API keys are valid."""
    name = "Alpaca Paper API"
    api_key, secret_key = _load_alpaca_keys()
    if not api_key or not secret_key:
        return {"name": name, "ok": False, "ms": 0, "detail": "API keys not configured"}
    url   = "https://paper-api.alpaca.markets/v2/account"
    start = time.time()
    try:
        r = requests.get(
            url,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
            timeout=TIMEOUT,
        )
        ms = int((time.time() - start) * 1000)
        if r.status_code == 200:
            data   = r.json()
            equity = float(data.get("equity", 0))
            buying = float(data.get("buying_power", 0))
            return {"name": name, "ok": True,  "ms": ms,
                    "detail": f"Equity: ${equity:,.2f}  |  Buying power: ${buying:,.2f}"}
        return {"name": name, "ok": False, "ms": ms, "detail": f"HTTP {r.status_code}: {r.text[:60]}"}
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        return {"name": name, "ok": False, "ms": ms, "detail": str(e)[:80]}


# ---------------------------------------------------------------------------
# Watchlist freshness check (local file)
# ---------------------------------------------------------------------------

def check_watchlist_freshness() -> dict:
    """Check how old the local watchlist.json is."""
    name = "Watchlist (local)"
    wl_file = SCRIPT_DIR / "data" / "watchlist.json"
    if not wl_file.exists():
        return {"name": name, "ok": False, "ms": 0, "detail": "watchlist.json not found"}
    try:
        with open(wl_file) as f:
            wl = json.load(f)
        stocks    = wl.get("stocks", [])
        gen_utc   = wl.get("generated_at_utc", "")
        run_status = wl.get("run_status", "")
        if "FAILED" in run_status:
            return {"name": name, "ok": False, "ms": 0,
                    "detail": f"Last build FAILED — {run_status[:60]}"}
        if not stocks:
            return {"name": name, "ok": False, "ms": 0, "detail": "0 stocks in watchlist"}
        if gen_utc:
            try:
                gen_dt  = datetime.fromisoformat(gen_utc.replace("Z", "+00:00"))
                age_days = (_now_utc() - gen_dt).days
                if age_days > 7:
                    return {"name": name, "ok": False, "ms": 0,
                            "detail": f"{len(stocks)} stocks but {age_days} days old — needs refresh"}
                return {"name": name, "ok": True, "ms": 0,
                        "detail": f"{len(stocks)} stocks, {age_days}d old"}
            except Exception:
                pass
        return {"name": name, "ok": True, "ms": 0, "detail": f"{len(stocks)} stocks (age unknown)"}
    except Exception as e:
        return {"name": name, "ok": False, "ms": 0, "detail": str(e)[:80]}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_healthcheck() -> bool:
    """
    Run all checks, print results, send Telegram report.
    Returns True if all critical sources are reachable, False otherwise.
    """
    print(f"\n{'='*60}")
    print(f"  DAILY HEALTH CHECK - {_fmt_et_sgt()}")
    print(f"{'='*60}\n")

    # Run all checks
    checks = [
        check_tradingview,
        check_barchart,
        check_stockanalysis,
        check_iborrowdesk,
        check_finviz,
        check_nasdaq_earnings,
        check_alpaca,
        check_watchlist_freshness,
    ]

    results = []
    for fn in checks:
        print(f"  Checking {fn.__name__.replace('check_', '')}...", end=" ", flush=True)
        result = fn()
        results.append(result)
        status = "OK" if result["ok"] else "FAIL"
        ms_str = f" ({result['ms']}ms)" if result["ms"] else ""
        print(f"[{status}]{ms_str} {result['detail']}")

    # Summary
    ok_count   = sum(1 for r in results if r["ok"])
    fail_count = len(results) - ok_count
    all_ok     = fail_count == 0

    # Critical sources (scanner won't work without these)
    critical = {"TradingView Scanner", "iborrowdesk.com", "Alpaca Paper API"}
    critical_failures = [r for r in results if not r["ok"] and r["name"] in critical]

    print(f"\n  Summary: {ok_count} OK / {fail_count} FAIL")
    if critical_failures:
        print(f"  CRITICAL failures: {[r['name'] for r in critical_failures]}")

    # Build Telegram message
    now_str = _fmt_et_sgt()
    if all_ok:
        header = f"*Health Check* - {now_str}\nAll {ok_count} sources reachable."
    elif critical_failures:
        header = f"*Health Check ALERT* - {now_str}\n{fail_count} source(s) DOWN - scanner may be impaired."
    else:
        header = f"*Health Check* - {now_str}\n{ok_count} OK / {fail_count} non-critical issue(s)."

    lines = [header, ""]
    for r in results:
        icon = "OK" if r["ok"] else "!!"
        ms_str = f" {r['ms']}ms" if r["ms"] else ""
        lines.append(f"[{icon}]{ms_str} *{r['name']}*")
        lines.append(f"      {r['detail']}")

    if not all_ok:
        fail_names = [r["name"] for r in results if not r["ok"]]
        lines += ["", f"Failed: {', '.join(fail_names)}"]
        if critical_failures:
            lines.append("Scanner alerts may not fire today.")

    _send_telegram("\n".join(lines))
    return all_ok


if __name__ == "__main__":
    import sys
    ok = run_healthcheck()
    sys.exit(0 if ok else 1)
