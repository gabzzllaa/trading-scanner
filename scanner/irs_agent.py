#!/usr/bin/env python3
"""
Institutional Reversal Signal (IRS) Trading Agent
---------------------------------------------------
Handles all Alpaca order placement and exit monitoring for IRS setups.
Reads triggered candidates from irs_scanner.py output.

Modes:
  entry   - Monday pre-market: place limit buy orders for triggered candidates
  monitor - Weekly (Sunday): check exit conditions for all open IRS positions

Exit conditions (first triggered wins):
  1. Price reaches 52W high target        -> sell
  2. Form 4 insider SELLING detected      -> sell immediately
  3. Stop loss 10% below entry            -> sell
  4. Time stop 90 days                    -> sell

Usage:
  python irs_agent.py --mode entry    # place limit buy orders
  python irs_agent.py --mode monitor  # check exits for open positions
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
SCRIPT_DIR      = Path(__file__).parent.resolve()
DATA_DIR        = SCRIPT_DIR / "data"
HISTORY_DIR     = DATA_DIR / "history"
CONFIG_FILE     = SCRIPT_DIR.parent / "config.yaml"

IRS_WATCHLIST_FILE  = DATA_DIR / "irs_watchlist.json"
IRS_STATE_FILE      = DATA_DIR / "irs_state.json"
IRS_POSITIONS_FILE  = DATA_DIR / "irs_positions.json"

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
CAPITAL          = _g("capital_usd", 10_000)
RISK_PER_TRADE   = CAPITAL * _g("risk_per_trade_pct", 0.01)
STOP_PCT         = _i("stop_loss_pct",  0.10)
TIME_STOP_DAYS   = _i("time_stop_days", 90)

ALPACA_API_KEY    = _g("alpaca_api_key",    os.environ.get("ALPACA_API_KEY",    ""))
ALPACA_SECRET_KEY = _g("alpaca_secret_key", os.environ.get("ALPACA_SECRET_KEY", ""))
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL   = "https://data.alpaca.markets"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://www.tradingview.com",
    "Referer":         "https://www.tradingview.com/",
    "Content-Type":    "application/json",
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


# ---------------------------------------------------------------------------
# Alpaca API helpers
# ---------------------------------------------------------------------------

def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }


def _alpaca_get(path: str) -> Optional[dict]:
    try:
        r = requests.get(
            f"{ALPACA_BASE_URL}{path}",
            headers=_alpaca_headers(),
            timeout=15,
        )
        if r.ok:
            return r.json()
        print(f"  [Alpaca] GET {path} -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  [Alpaca] GET {path} error: {e}")
    return None


def _alpaca_post(path: str, body: dict) -> Optional[dict]:
    try:
        r = requests.post(
            f"{ALPACA_BASE_URL}{path}",
            headers=_alpaca_headers(),
            json=body,
            timeout=15,
        )
        if r.ok:
            return r.json()
        print(f"  [Alpaca] POST {path} -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  [Alpaca] POST {path} error: {e}")
    return None


def _alpaca_delete(path: str) -> bool:
    try:
        r = requests.delete(
            f"{ALPACA_BASE_URL}{path}",
            headers=_alpaca_headers(),
            timeout=15,
        )
        return r.ok
    except Exception as e:
        print(f"  [Alpaca] DELETE {path} error: {e}")
        return False


def get_account() -> Optional[dict]:
    return _alpaca_get("/v2/account")


def get_position(ticker: str) -> Optional[dict]:
    return _alpaca_get(f"/v2/positions/{ticker}")


def get_all_positions() -> list:
    result = _alpaca_get("/v2/positions")
    return result if isinstance(result, list) else []


def get_latest_price(ticker: str) -> Optional[float]:
    """Fetch latest trade price from Alpaca data API."""
    try:
        r = requests.get(
            f"{ALPACA_DATA_URL}/v2/stocks/{ticker}/trades/latest",
            headers=_alpaca_headers(),
            timeout=10,
        )
        if r.ok:
            return float(r.json().get("trade", {}).get("p", 0)) or None
    except Exception:
        pass
    # Fallback: TradingView
    return _tv_price(ticker)


def _tv_price(ticker: str) -> Optional[float]:
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "markets": ["america"],
        "symbols": {
            "query": {"types": ["stock"]},
            "tickers": [f"NASDAQ:{ticker}", f"NYSE:{ticker}", f"AMEX:{ticker}"],
        },
        "options": {"lang": "en"},
        "columns": ["name", "close"],
        "range": [0, 5],
    }
    try:
        r = requests.post(url, json=payload, headers=HEADERS, timeout=10)
        if r.ok:
            for item in r.json().get("data", []):
                d = item.get("d", [])
                if len(d) >= 2:
                    name, price = d[0], d[1]
                    t = name.split(":")[1] if name and ":" in name else name
                    if t and t.upper() == ticker.upper() and price:
                        return float(price)
    except Exception:
        pass
    return None


def place_limit_buy(ticker: str, shares: int, limit_price: float,
                    stop_price: float, take_profit: float) -> Optional[dict]:
    """
    Place a bracket limit buy order via Alpaca.
    Bracket = entry limit + stop loss + take profit in one order.
    """
    if shares <= 0:
        print(f"  [{ticker}] Skipping - 0 shares calculated.")
        return None

    body = {
        "symbol":        ticker,
        "qty":           str(shares),
        "side":          "buy",
        "type":          "limit",
        "time_in_force": "day",
        "limit_price":   str(round(limit_price, 2)),
        "order_class":   "bracket",
        "stop_loss": {
            "stop_price": str(round(stop_price, 2)),
        },
        "take_profit": {
            "limit_price": str(round(take_profit, 2)),
        },
    }
    print(f"  [{ticker}] Placing bracket buy: {shares} shares @ ${limit_price:.2f} "
          f"| Stop: ${stop_price:.2f} | Target: ${take_profit:.2f}")
    return _alpaca_post("/v2/orders", body)


def close_position(ticker: str) -> Optional[dict]:
    """Market sell all shares of a position."""
    print(f"  [{ticker}] Closing position (market sell)...")
    return _alpaca_delete(f"/v2/positions/{ticker}")


# ---------------------------------------------------------------------------
# Form 4 insider SELL check
# ---------------------------------------------------------------------------

def check_insider_selling(ticker: str) -> list:
    """
    Check for recent Form 4 insider SELL transactions (transaction code S).
    Returns list of sell transactions in the last 14 days.
    Any insider selling = exit signal.
    """
    cutoff = _days_ago(14)
    today  = _today_str()

    url = (
        f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
        f"&dateRange=custom&startdt={cutoff}&enddt={today}"
        f"&forms=4&hits.hits._source=period_of_report,display_names,file_date"
    )
    sells = []
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "trading-scanner/1.0", "Accept": "application/json"},
            timeout=15,
        )
        if not r.ok:
            return []
        hits = r.json().get("hits", {}).get("hits", [])

        for hit in hits[:10]:
            src        = hit.get("_source", {})
            accession  = hit.get("_id", "")
            file_date  = src.get("file_date", "")
            filer      = src.get("display_names", "")
            if not accession:
                continue

            # Parse the filing for sell transactions
            acc_clean = accession.replace("-", "")
            cik_part  = acc_clean[:10].lstrip("0")
            xml_url   = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_part}/"
                f"{acc_clean}/{accession}.xml"
            )
            try:
                xr = requests.get(
                    xml_url,
                    headers={"User-Agent": "trading-scanner/1.0"},
                    timeout=10,
                )
                if not xr.ok:
                    continue
                import xml.etree.ElementTree as ET
                root = ET.fromstring(xr.text)
                for txn in root.findall(".//nonDerivativeTransaction"):
                    code_elem = txn.find(".//transactionCode")
                    if code_elem is None:
                        continue
                    code = code_elem.text or ""
                    if code == "S":  # S = open market sale
                        shares_elem = txn.find(".//transactionShares/value")
                        price_elem  = txn.find(".//transactionPricePerShare/value")
                        shares = float(shares_elem.text) if shares_elem is not None and shares_elem.text else 0
                        price  = float(price_elem.text)  if price_elem  is not None and price_elem.text  else 0
                        if shares > 0:
                            sells.append({
                                "filer":      filer,
                                "shares":     shares,
                                "price":      price,
                                "file_date":  file_date,
                            })
            except Exception:
                pass
            time.sleep(0.1)

    except Exception as e:
        print(f"  [{ticker}] Insider sell check error: {e}")

    return sells


# ---------------------------------------------------------------------------
# Positions state
# ---------------------------------------------------------------------------

def _load_positions() -> dict:
    if IRS_POSITIONS_FILE.exists():
        with open(IRS_POSITIONS_FILE) as f:
            return json.load(f)
    return {}


def _save_positions(data: dict) -> None:
    with open(IRS_POSITIONS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _load_state() -> dict:
    if IRS_STATE_FILE.exists():
        with open(IRS_STATE_FILE) as f:
            return json.load(f)
    return {"date": _today_str(), "triggered": {}}


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
# Mode: entry
# ---------------------------------------------------------------------------

def mode_entry():
    """
    Monday pre-market: place limit buy orders for IRS-triggered candidates.
    Reads triggered candidates from irs_state.json.
    """
    print(f"\n{'='*60}")
    print(f"  IRS AGENT - ENTRY ORDERS - {_fmt_et_sgt()}")
    print(f"{'='*60}")

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("  [!] Alpaca API keys not configured.")
        _send_telegram(f"*IRS Agent ERROR* - {_fmt_et_sgt()}\nAlpaca API keys not configured.")
        return

    # Check account
    account = get_account()
    if not account:
        print("  [!] Cannot reach Alpaca account.")
        return
    equity    = float(account.get("equity", 0))
    buying_pw = float(account.get("buying_power", 0))
    print(f"  Account equity: ${equity:,.2f}  |  Buying power: ${buying_pw:,.2f}")

    # Load triggered candidates from scanner state
    state      = _load_state()
    triggered  = state.get("triggered", {})
    watchlist  = {}
    if IRS_WATCHLIST_FILE.exists():
        with open(IRS_WATCHLIST_FILE) as f:
            wl_data  = json.load(f)
        watchlist = wl_data.get("candidates", {})

    if not triggered:
        print("  No IRS triggered candidates to place orders for.")
        return

    positions = _load_positions()
    orders_placed = []

    for ticker, trigger_info in triggered.items():
        # Skip if already have an open position
        if ticker in positions:
            print(f"  [{ticker}] Already have open position - skipping.")
            continue

        # Get candidate data
        candidate = watchlist.get(ticker, {})
        trade     = candidate.get("trade", {})

        # Fetch latest price
        price = get_latest_price(ticker)
        if not price:
            print(f"  [{ticker}] Cannot fetch price - skipping.")
            continue

        # Recalculate using current price
        stop_price   = round(price * (1 - STOP_PCT), 2)
        stop_dist    = price - stop_price
        shares       = int(RISK_PER_TRADE / stop_dist) if stop_dist > 0 else 0
        target_price = candidate.get("week52_high") or round(price * 1.20, 2)

        if shares <= 0:
            print(f"  [{ticker}] 0 shares calculated - skipping.")
            continue

        position_value = shares * price
        if position_value > buying_pw * 0.20:  # max 20% of buying power per position
            shares = int(buying_pw * 0.20 / price)
            print(f"  [{ticker}] Reduced to {shares} shares (buying power cap)")

        if shares <= 0:
            print(f"  [{ticker}] Insufficient buying power - skipping.")
            continue

        # Place bracket limit buy at current price
        order = place_limit_buy(ticker, shares, price, stop_price, target_price)

        if order:
            entry_date = _today_str()
            time_stop_date = (
                datetime.fromisoformat(entry_date) + timedelta(days=TIME_STOP_DAYS)
            ).strftime("%Y-%m-%d")

            position_record = {
                "ticker":           ticker,
                "strategy":         "IRS",
                "entry_price":      price,
                "entry_date":       entry_date,
                "entry_et":         _fmt(_now_et()),
                "shares":           shares,
                "stop_price":       stop_price,
                "target_price":     target_price,
                "risk_usd":         round(shares * stop_dist, 2),
                "time_stop_date":   time_stop_date,
                "alpaca_order_id":  order.get("id", ""),
                "score":            trigger_info.get("score", 0),
                "sector":           candidate.get("sector", ""),
                "week52_high":      candidate.get("week52_high"),
            }
            positions[ticker] = position_record
            orders_placed.append(position_record)

            print(f"  [{ticker}] Order placed - {shares} shares @ ${price:.2f} "
                  f"| Stop: ${stop_price:.2f} | Target: ${target_price:.2f} "
                  f"| Time stop: {time_stop_date}")
        else:
            print(f"  [{ticker}] Order placement failed.")

        time.sleep(0.5)

    _save_positions(positions)

    # Telegram summary
    if orders_placed:
        lines = [
            f"*IRS Entry Orders* - {_fmt_et_sgt()}",
            f"{len(orders_placed)} bracket buy order(s) placed.",
            "",
        ]
        for p in orders_placed:
            reward   = round(p["shares"] * (p["target_price"] - p["entry_price"]), 2)
            rr       = round(reward / p["risk_usd"], 2) if p["risk_usd"] else 0
            lines += [
                f"*{p['ticker']}*  [{p.get('sector','')}]",
                f"  Buy: {p['shares']} shares @ ${p['entry_price']:.2f}",
                f"  Stop: ${p['stop_price']:.2f} (-{STOP_PCT*100:.0f}%)  "
                f"Target: ${p['target_price']:.2f}  R/R: {rr}:1",
                f"  Risk: ${p['risk_usd']:.2f}  |  Time stop: {p['time_stop_date']}",
                "",
            ]
        lines.append("_Stop: 10% below entry. Target: 52W high. Max hold: 90 days._")
        _send_telegram("\n".join(lines))
    else:
        print("  No orders placed.")


# ---------------------------------------------------------------------------
# Mode: monitor
# ---------------------------------------------------------------------------

def mode_monitor():
    """
    Weekly (Sunday): check all open IRS positions against exit conditions.
    Exit conditions checked in order:
      1. Insider selling (Form 4)
      2. Stop loss hit
      3. Target reached (52W high)
      4. Time stop (90 days)
    """
    print(f"\n{'='*60}")
    print(f"  IRS AGENT - WEEKLY MONITOR - {_fmt_et_sgt()}")
    print(f"{'='*60}")

    positions = _load_positions()
    if not positions:
        print("  No open IRS positions to monitor.")
        _send_telegram(
            f"*IRS Weekly Monitor* - {_fmt_et_sgt()}\nNo open positions."
        )
        return

    print(f"  Monitoring {len(positions)} open IRS position(s)...")

    # Get all current Alpaca positions for unrealized P&L
    alpaca_positions = {p["symbol"]: p for p in get_all_positions()}

    exits     = []
    holds     = []
    today_str = _today_str()

    for ticker, pos in list(positions.items()):
        print(f"\n  [{ticker}] Checking exit conditions...")
        entry_price = pos["entry_price"]
        stop_price  = pos["stop_price"]
        target      = pos["target_price"]
        time_stop_d = pos.get("time_stop_date", "")
        entry_date  = pos.get("entry_date", "")

        # Get current price
        price = get_latest_price(ticker)
        if not price:
            print(f"  [{ticker}] Cannot fetch price - skipping exit check.")
            holds.append({**pos, "current_price": None, "unrealized_pnl": None})
            continue

        # Unrealized P&L
        shares         = pos["shares"]
        unrealized_pnl = round(shares * (price - entry_price), 2)
        unrealized_pct = round((price - entry_price) / entry_price * 100, 2)

        # Calculate days held
        try:
            days_held = (_now_utc().date() - datetime.fromisoformat(entry_date).date()).days
        except Exception:
            days_held = 0

        print(f"  [{ticker}] Price: ${price:.2f} | Entry: ${entry_price:.2f} | "
              f"Unrealized: ${unrealized_pnl:+.2f} ({unrealized_pct:+.1f}%) | "
              f"Days held: {days_held}")

        exit_reason = None

        # 1. Insider selling (highest priority)
        print(f"  [{ticker}] Checking insider selling...", end=" ", flush=True)
        insider_sells = check_insider_selling(ticker)
        if insider_sells:
            exit_reason = "INSIDER_SELLING"
            print(f"SELL SIGNAL - {len(insider_sells)} insider sell(s) detected!")
        else:
            print("none")

        # 2. Stop loss
        if not exit_reason and price <= stop_price:
            exit_reason = "STOP_LOSS"
            print(f"  [{ticker}] Stop loss hit: ${price:.2f} <= ${stop_price:.2f}")

        # 3. Target reached
        if not exit_reason and price >= target:
            exit_reason = "TARGET_HIT"
            print(f"  [{ticker}] Target reached: ${price:.2f} >= ${target:.2f}")

        # 4. Time stop
        if not exit_reason and time_stop_d and today_str >= time_stop_d:
            exit_reason = "TIME_STOP"
            print(f"  [{ticker}] Time stop: {days_held} days held (limit: {TIME_STOP_DAYS})")

        if exit_reason:
            # Execute exit
            result = close_position(ticker)
            actual_exit_price = price  # best estimate; actual fill may differ slightly

            pnl = round(shares * (actual_exit_price - entry_price), 2)
            pnl_pct = round((actual_exit_price - entry_price) / entry_price * 100, 2)

            exit_record = {
                **pos,
                "exit_price":   actual_exit_price,
                "exit_date":    today_str,
                "exit_reason":  exit_reason,
                "pnl_usd":      pnl,
                "pnl_pct":      pnl_pct,
                "days_held":    days_held,
            }
            exits.append(exit_record)

            # Archive
            hist = HISTORY_DIR / f"irs_exit_{ticker}_{today_str}.json"
            with open(hist, "w") as f:
                json.dump(exit_record, f, indent=2, default=str)

            # Remove from active positions
            del positions[ticker]

            emoji = "+" if pnl > 0 else "-"
            print(f"  [{ticker}] EXITED - {exit_reason} | P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")

            # Individual exit Telegram alert
            reason_label = {
                "INSIDER_SELLING": "Insider selling detected",
                "STOP_LOSS":       "Stop loss hit",
                "TARGET_HIT":      "Target (52W high) reached",
                "TIME_STOP":       "90-day time stop",
            }.get(exit_reason, exit_reason)

            exit_emoji = "+" if pnl > 0 else "-"
            _send_telegram(
                f"*IRS EXIT* - {_fmt_et_sgt()}\n"
                f"*{ticker}*  Reason: {reason_label}\n"
                f"  Entry: ${entry_price:.2f}  Exit: ${actual_exit_price:.2f}\n"
                f"  P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)  |  Held: {days_held} days\n"
                f"  Shares: {shares}  |  Risk was: ${pos.get('risk_usd','?')}"
            )

        else:
            # No exit - update with current data
            alpaca_pos = alpaca_positions.get(ticker, {})
            unrealized = float(alpaca_pos.get("unrealized_pl", unrealized_pnl))
            holds.append({
                **pos,
                "current_price":   price,
                "unrealized_pnl":  round(unrealized, 2),
                "unrealized_pct":  unrealized_pct,
                "days_held":       days_held,
            })
            print(f"  [{ticker}] Holding - no exit condition met.")

        time.sleep(0.5)

    _save_positions(positions)

    # Weekly monitor summary Telegram
    now_str = _fmt_et_sgt()
    lines   = [f"*IRS Weekly Monitor* - {now_str}", ""]

    if exits:
        total_pnl = sum(e["pnl_usd"] for e in exits)
        lines.append(f"*{len(exits)} position(s) closed this week:*")
        for e in exits:
            pnl_str = f"${e['pnl_usd']:+.2f} ({e['pnl_pct']:+.1f}%)"
            lines.append(
                f"  {'WIN' if e['pnl_usd'] > 0 else 'LOSS'} *{e['ticker']}*  "
                f"{e['exit_reason']}  {pnl_str}  ({e['days_held']}d held)"
            )
        lines.append(f"  Week closed P&L: ${total_pnl:+.2f}")
        lines.append("")

    if holds:
        total_unrealized = sum(h.get("unrealized_pnl", 0) or 0 for h in holds)
        lines.append(f"*{len(holds)} position(s) still open:*")
        for h in holds:
            upnl = h.get("unrealized_pnl")
            upct = h.get("unrealized_pct")
            days = h.get("days_held", 0)
            time_left = TIME_STOP_DAYS - days
            price_str = f"${h.get('current_price','?'):.2f}" if h.get("current_price") else "?"
            pnl_str   = f"${upnl:+.2f} ({upct:+.1f}%)" if upnl is not None else "?"
            lines.append(
                f"  *{h['ticker']}*  {price_str}  "
                f"Unrealized: {pnl_str}  "
                f"Day {days}/{TIME_STOP_DAYS} ({time_left}d left)"
            )
        lines.append(f"  Total unrealized: ${total_unrealized:+.2f}")

    if not exits and not holds:
        lines.append("No open positions and no exits this week.")

    _send_telegram("\n".join(lines))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="IRS Trading Agent")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["entry", "monitor"],
        help="entry: place buy orders | monitor: weekly exit checks",
    )
    args = parser.parse_args()
    {"entry": mode_entry, "monitor": mode_monitor}[args.mode]()


if __name__ == "__main__":
    main()
