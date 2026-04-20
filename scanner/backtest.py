#!/usr/bin/env python3
"""
Backtest Engine — Bagholder Exit Liquidity Strategy
-----------------------------------------------------
Replays the gap-fade strategy against 6 months of historical data
using Alpaca's free market data API.

For each stock in the watchlist:
  1. Fetch 6 months of daily OHLCV bars from Alpaca
  2. Identify gap-up days (open >= prev_close * 1.30 = 30%+ gap)
  3. Score each gap event using the same 6-condition engine
  4. Simulate the trade:
       - Entry:  open price
       - Stop:   open * 1.10  (10% above entry, conservative since we lack intraday data)
       - Target: entry - 0.5 * (entry - prev_close)  (50% retracement)
       - Exit:   whichever of low (target hit) or high (stop hit) occurs first
                 If neither, use close price (time stop proxy)
  5. Record outcome per agent (each agent uses its own stock universe)

Usage:
  python backtest.py --agent 1    # backtest agent 1 (TradingView universe)
  python backtest.py --agent 2    # backtest agent 2 (Barchart universe)
  python backtest.py --agent 3    # backtest agent 3 (StockAnalysis universe)
  python backtest.py --agent all  # backtest all agents sequentially

Output:
  scanner/data/backtest_agent{N}.json   — full results
  Telegram message with summary stats
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
SCRIPT_DIR  = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG_FILE = PROJECT_DIR / "config.yaml"
DATA_DIR    = SCRIPT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

with open(CONFIG_FILE) as f:
    CONFIG = yaml.safe_load(f)

ALPACA_API_KEY    = CONFIG.get("alpaca_api_key", "")
ALPACA_SECRET_KEY = CONFIG.get("alpaca_secret_key", "")
ALPACA_DATA_URL   = "https://data.alpaca.markets"
ALPACA_HEADERS    = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

# ---------------------------------------------------------------------------
# Strategy constants (mirrors alpaca_agent.py)
# ---------------------------------------------------------------------------
CAPITAL        = 100_000
RISK_PER_TRADE = 1_000       # 1% of capital
MAX_POS_PCT    = 0.05        # max 5% of capital per position
MIN_RR_RATIO   = 1.5
STOP_PCT       = 0.10        # 10% above entry
TARGET_RETRACE = 0.50        # 50% retracement
MIN_GAP_PCT    = 30.0        # minimum gap % to consider
LOOKBACK_DAYS  = 183         # ~6 months

# Scoring thresholds (from config)
TIER_A_PLUS  = CONFIG.get("bagholder", {}).get("tier_a_plus_min", 35)
TIER_MONITOR = CONFIG.get("bagholder", {}).get("tier_monitor_min", 20)

# Agent stock universes — in a real run these come from watchlist.json
# Each agent uses the full watchlist but tags its source
AGENT_SOURCES = {1: "tradingview", 2: "barchart", 3: "stockanalysis"}

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _fmt_et_sgt() -> str:
    now = _now_utc()
    et_offset = -4 if 3 <= now.month <= 11 else -5
    et = now + timedelta(hours=et_offset)
    sgt = now + timedelta(hours=8)
    label = "EDT" if et_offset == -4 else "EST"
    return f"{et.strftime('%Y-%m-%d %H:%M')} {label} / {sgt.strftime('%Y-%m-%d %H:%M')} SGT"

# ---------------------------------------------------------------------------
# Alpaca market data
# ---------------------------------------------------------------------------

def _alpaca_data_get(path: str, params: dict = None) -> dict:
    url = f"{ALPACA_DATA_URL}{path}"
    resp = requests.get(url, headers=ALPACA_HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def fetch_daily_bars(ticker: str, start: str, end: str) -> list:
    """
    Fetch daily OHLCV bars for a ticker between start and end dates (YYYY-MM-DD).
    Returns list of bar dicts sorted by date ascending.
    Handles Alpaca pagination automatically.
    """
    bars = []
    params = {
        "start":      start,
        "end":        end,
        "timeframe":  "1Day",
        "adjustment": "raw",
        "limit":      1000,
    }
    path = f"/v2/stocks/{ticker}/bars"
    while True:
        try:
            data = _alpaca_data_get(path, params)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return []   # ticker not found
            raise
        raw_bars = data.get("bars", [])
        if not raw_bars:
            break
        bars.extend(raw_bars)
        next_token = data.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
        time.sleep(0.2)  # rate limit courtesy pause
    return bars

def fetch_watchlist_tickers() -> list:
    """Load tickers from watchlist.json, fall back to a small default set."""
    wl_file = DATA_DIR / "watchlist.json"
    if wl_file.exists():
        try:
            wl = json.loads(wl_file.read_text())
            stocks = wl.get("stocks", [])
            if stocks:
                return [s["ticker"] for s in stocks if s.get("ticker")]
        except Exception:
            pass
    # Fallback: small set of known volatile low-cap stocks for testing
    return [
        "SOUN", "CLOV", "WISH", "WKHS", "RIDE", "NKLA", "SPCE", "BBBY",
        "BYND", "PRTY", "FFIE", "MULN", "XELA", "ATER", "LGVN", "ILUS",
        "VERB", "CTXR", "SNDL", "NAKD", "EXPR", "KOSS", "AMC", "BBIG",
    ]

# ---------------------------------------------------------------------------
# Scoring engine (simplified — mirrors scanner.py conditions 1,2,4,6)
# Note: C3 (short interest) and C5 (catalyst) require live data not available
# in historical bars, so we use conservative fixed scores for those.
# ---------------------------------------------------------------------------

def score_gap_event(gap_pct: float, open_price: float, volume: float, avg_volume: float,
                    prev_close: float, price_52w_high: Optional[float] = None) -> dict:
    """
    Score a historical gap event using available OHLCV data.
    C3 (short interest) = fixed 5 pts (conservative — unknown historically)
    C5 (catalyst) = fixed 5 pts (assumed meme/social — worst case for backtesting)
    """
    scores = {}

    # C1: Prior decline — approximate using distance from 52w high
    # If we don't have 52w high, skip (score 0)
    if price_52w_high and price_52w_high > 0:
        decline_pct = (price_52w_high - prev_close) / price_52w_high * 100
        cfg = CONFIG.get("bagholder", {})
        if decline_pct >= cfg.get("c1_decline_10", 80):   scores["c1"] = 10
        elif decline_pct >= cfg.get("c1_decline_9", 60):  scores["c1"] = 9
        elif decline_pct >= cfg.get("c1_decline_7", 50):  scores["c1"] = 7
        elif decline_pct >= cfg.get("c1_decline_4", 40):  scores["c1"] = 4
        elif decline_pct >= cfg.get("c1_decline_2", 30):  scores["c1"] = 2
        else:                                              scores["c1"] = 0
    else:
        scores["c1"] = 0

    # C2: Price & market cap (price only — no mkt cap in bars)
    cfg = CONFIG.get("bagholder", {})
    if open_price < cfg.get("c2_price_5", 1):    scores["c2"] = 5
    elif open_price < cfg.get("c2_price_4", 3):  scores["c2"] = 4
    elif open_price < cfg.get("c2_price_3", 5):  scores["c2"] = 3
    elif open_price < cfg.get("c2_price_2", 10): scores["c2"] = 2
    else:                                         scores["c2"] = 0

    # C3: Short interest — not available historically, use fixed conservative score
    scores["c3"] = 5

    # C4: Gap size
    if gap_pct >= cfg.get("c4_gap_10", 100):   scores["c4"] = 10
    elif gap_pct >= cfg.get("c4_gap_9", 75):   scores["c4"] = 9
    elif gap_pct >= cfg.get("c4_gap_8", 50):   scores["c4"] = 8
    elif gap_pct >= cfg.get("c4_gap_7", 40):   scores["c4"] = 7
    elif gap_pct >= cfg.get("c4_gap_6", 30):   scores["c4"] = 6
    elif gap_pct >= cfg.get("c4_gap_3", 20):   scores["c4"] = 3
    else:                                       scores["c4"] = 0

    # C5: Catalyst — not verifiable historically, use fixed 5 pts
    scores["c5"] = 5

    # C6: Volume vs average
    if avg_volume > 0:
        vol_ratio = volume / avg_volume
        if vol_ratio >= cfg.get("c6_vol_10", 20):   scores["c6"] = 10
        elif vol_ratio >= cfg.get("c6_vol_9", 15):  scores["c6"] = 9
        elif vol_ratio >= cfg.get("c6_vol_8", 10):  scores["c6"] = 8
        elif vol_ratio >= cfg.get("c6_vol_7", 7):   scores["c6"] = 7
        elif vol_ratio >= cfg.get("c6_vol_6", 5):   scores["c6"] = 6
        elif vol_ratio >= cfg.get("c6_vol_4", 3):   scores["c6"] = 4
        elif vol_ratio >= cfg.get("c6_vol_2", 2):   scores["c6"] = 2
        else:                                        scores["c6"] = 0
    else:
        scores["c6"] = 0

    total = sum(scores.values())
    tier = "A+" if total >= TIER_A_PLUS else "Monitor" if total >= TIER_MONITOR else "Skip"
    return {"scores": scores, "total": total, "tier": tier}

# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_trade(entry: float, high: float, low: float, close: float,
                   prev_close: float) -> dict:
    """
    Simulate a short fade trade using daily OHLCV bar.
    Since we only have daily bars (not intraday), we use this logic:
      - Stop hit: bar high >= stop_price  → exit at stop (worst case)
      - Target hit: bar low <= target1    → exit at target (best case)
      - If both: assume stop hit first (conservative)
      - If neither: exit at close (time stop proxy)

    Returns dict with exit_price, exit_reason, pnl_usd, outcome.
    """
    stop_price = round(entry * (1 + STOP_PCT), 4)
    gap_size   = entry - prev_close
    target1    = round(entry - TARGET_RETRACE * gap_size, 4)

    # Position sizing
    risk_per_share = stop_price - entry
    if risk_per_share <= 0:
        return {"skip": True, "reason": "zero risk per share"}

    reward_per_share = entry - target1
    rr_ratio = round(reward_per_share / risk_per_share, 2)
    if rr_ratio < MIN_RR_RATIO:
        return {"skip": True, "reason": f"R/R {rr_ratio:.2f} < {MIN_RR_RATIO}"}

    shares_by_risk    = int(RISK_PER_TRADE / risk_per_share)
    max_shares_by_cap = int((CAPITAL * MAX_POS_PCT) / entry)
    shares = min(shares_by_risk, max_shares_by_cap)
    if shares < 1:
        return {"skip": True, "reason": "position too small"}

    # Determine exit — conservative: if stop AND target both triggered, assume stop
    stop_hit   = high >= stop_price
    target_hit = low <= target1

    if stop_hit:
        exit_price  = stop_price
        exit_reason = "STOP_LOSS"
    elif target_hit:
        exit_price  = target1
        exit_reason = "TARGET_1"
    else:
        exit_price  = close
        exit_reason = "TIME_STOP"

    pnl_usd = round(shares * (entry - exit_price), 2)
    pnl_pct = round((entry - exit_price) / entry * 100, 2)

    return {
        "skip":        False,
        "entry":       entry,
        "stop_price":  stop_price,
        "target1":     target1,
        "shares":      shares,
        "rr_ratio":    rr_ratio,
        "exit_price":  exit_price,
        "exit_reason": exit_reason,
        "pnl_usd":     pnl_usd,
        "pnl_pct":     pnl_pct,
        "outcome":     "WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "FLAT",
        "risk_usd":    round(shares * risk_per_share, 2),
        "reward_t1":   round(shares * reward_per_share, 2),
    }

# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_backtest(agent_id: int) -> dict:
    source = AGENT_SOURCES[agent_id]
    print(f"\n{'='*60}")
    print(f"  BACKTEST — Agent {agent_id} ({source.upper()}) — {_fmt_et_sgt()}")
    print(f"  Lookback: {LOOKBACK_DAYS} days | Min gap: {MIN_GAP_PCT}%")
    print(f"{'='*60}")

    # Date range
    end_date   = _now_utc().date()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    start_str  = start_date.isoformat()
    end_str    = end_date.isoformat()

    tickers = fetch_watchlist_tickers()
    print(f"  Universe: {len(tickers)} tickers")
    print(f"  Period: {start_str} → {end_str}\n")

    all_events  = []   # all gap events found
    all_trades  = []   # gap events that passed scoring and were traded
    skipped_tickers = []

    for i, ticker in enumerate(tickers):
        print(f"  [{i+1}/{len(tickers)}] {ticker} — fetching bars...", end=" ", flush=True)
        try:
            bars = fetch_daily_bars(ticker, start_str, end_str)
        except Exception as e:
            print(f"ERROR: {e}")
            skipped_tickers.append(ticker)
            continue

        if len(bars) < 10:
            print(f"insufficient data ({len(bars)} bars)")
            skipped_tickers.append(ticker)
            continue

        # Compute rolling 20-day average volume for each bar
        volumes = [b.get("v", 0) for b in bars]

        gap_count = 0
        trade_count = 0

        for idx in range(1, len(bars)):
            bar      = bars[idx]
            prev_bar = bars[idx - 1]

            open_p     = float(bar.get("o", 0))
            high_p     = float(bar.get("h", 0))
            low_p      = float(bar.get("l", 0))
            close_p    = float(bar.get("c", 0))
            volume     = float(bar.get("v", 0))
            prev_close = float(prev_bar.get("c", 0))
            date_str   = bar.get("t", "")[:10]

            if not open_p or not prev_close or prev_close <= 0:
                continue

            # Skip sub-$0.50 stocks
            if open_p < 0.50:
                continue

            # Gap %
            gap_pct = (open_p - prev_close) / prev_close * 100
            if gap_pct < MIN_GAP_PCT:
                continue

            gap_count += 1

            # Average volume (20-day rolling, excluding current bar)
            vol_window = volumes[max(0, idx-20):idx]
            avg_vol    = sum(vol_window) / len(vol_window) if vol_window else 0

            # 52w high proxy: max close in the bar window
            high_window = [float(b.get("h", 0)) for b in bars[max(0, idx-252):idx]]
            price_52w_high = max(high_window) if high_window else None

            # Score this gap event
            scoring = score_gap_event(gap_pct, open_p, volume, avg_vol, prev_close, price_52w_high)

            event = {
                "ticker":      ticker,
                "date":        date_str,
                "source":      source,
                "open":        open_p,
                "high":        high_p,
                "low":         low_p,
                "close":       close_p,
                "prev_close":  prev_close,
                "gap_pct":     round(gap_pct, 2),
                "volume":      int(volume),
                "avg_volume":  int(avg_vol),
                "score":       scoring["total"],
                "tier":        scoring["tier"],
                "scores":      scoring["scores"],
            }
            all_events.append(event)

            # Only trade A+ setups
            if scoring["tier"] != "A+":
                continue

            # Simulate trade
            result = simulate_trade(open_p, high_p, low_p, close_p, prev_close)
            if result.get("skip"):
                event["trade_skipped"] = True
                event["skip_reason"]   = result.get("reason")
                continue

            trade = {**event, **result}
            all_trades.append(trade)
            trade_count += 1

        print(f"{gap_count} gaps found, {trade_count} A+ trades simulated")
        time.sleep(0.3)  # rate limiting

    # ---------------------------------------------------------------------------
    # Aggregate results
    # ---------------------------------------------------------------------------
    wins   = [t for t in all_trades if t["outcome"] == "WIN"]
    losses = [t for t in all_trades if t["outcome"] == "LOSS"]
    flats  = [t for t in all_trades if t["outcome"] == "FLAT"]

    total_pnl   = round(sum(t["pnl_usd"] for t in all_trades), 2)
    win_rate    = round(len(wins) / len(all_trades) * 100, 1) if all_trades else 0
    avg_win     = round(sum(t["pnl_usd"] for t in wins) / len(wins), 2) if wins else 0
    avg_loss    = round(sum(t["pnl_usd"] for t in losses) / len(losses), 2) if losses else 0
    profit_factor = round(
        abs(sum(t["pnl_usd"] for t in wins)) / abs(sum(t["pnl_usd"] for t in losses)), 2
    ) if losses and sum(t["pnl_usd"] for t in losses) != 0 else 0

    # Max drawdown — running cumulative P&L
    running = 0
    peak    = 0
    max_dd  = 0
    for t in all_trades:
        running += t["pnl_usd"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    # Best and worst trades
    best_trade  = max(all_trades, key=lambda t: t["pnl_usd"]) if all_trades else None
    worst_trade = min(all_trades, key=lambda t: t["pnl_usd"]) if all_trades else None

    # Exit reason breakdown
    exit_reasons = {}
    for t in all_trades:
        r = t["exit_reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # Gap size distribution for A+ trades
    gap_buckets = {"30-50%": 0, "50-75%": 0, "75-100%": 0, "100%+": 0}
    for t in all_trades:
        g = t["gap_pct"]
        if g < 50:   gap_buckets["30-50%"] += 1
        elif g < 75: gap_buckets["50-75%"] += 1
        elif g < 100:gap_buckets["75-100%"] += 1
        else:        gap_buckets["100%+"] += 1

    results = {
        "agent_id":       agent_id,
        "source":         source,
        "generated_at":   _now_utc().isoformat(),
        "period":         f"{start_str} to {end_str}",
        "lookback_days":  LOOKBACK_DAYS,
        "universe_size":  len(tickers),
        "skipped_tickers":skipped_tickers,
        "total_gap_events": len(all_events),
        "a_plus_events":  len([e for e in all_events if e["tier"] == "A+"]),
        "monitor_events": len([e for e in all_events if e["tier"] == "Monitor"]),
        "trades_taken":   len(all_trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "flats":          len(flats),
        "win_rate_pct":   win_rate,
        "total_pnl_usd":  total_pnl,
        "avg_win_usd":    avg_win,
        "avg_loss_usd":   avg_loss,
        "profit_factor":  profit_factor,
        "max_drawdown_usd": max_dd,
        "exit_reasons":   exit_reasons,
        "gap_distribution": gap_buckets,
        "best_trade":     best_trade,
        "worst_trade":    worst_trade,
        "all_trades":     all_trades,
        "all_events":     all_events,
    }

    # Save to file
    out_file = DATA_DIR / f"backtest_agent{agent_id}.json"
    out_file.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n  ✅ Results saved → {out_file}")

    return results

# ---------------------------------------------------------------------------
# Print & Telegram summary
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
        print("  [Telegram] ✅ Sent.")
    except Exception as e:
        print(f"  [Telegram] ✗ {e}")

def print_and_notify(results: dict) -> None:
    agent_id = results["agent_id"]
    source   = results["source"]
    pnl      = results["total_pnl_usd"]
    wr       = results["win_rate_pct"]
    pf       = results["profit_factor"]
    dd       = results["max_drawdown_usd"]

    result_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"

    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS — Agent {agent_id} ({source})")
    print(f"{'='*60}")
    print(f"  Period:         {results['period']}")
    print(f"  Universe:       {results['universe_size']} tickers")
    print(f"  Gap events:     {results['total_gap_events']} total | {results['a_plus_events']} A+")
    print(f"  Trades taken:   {results['trades_taken']}")
    print(f"  Win rate:       {wr}%")
    print(f"  Total P&L:      ${pnl}")
    print(f"  Avg win:        ${results['avg_win_usd']}")
    print(f"  Avg loss:       ${results['avg_loss_usd']}")
    print(f"  Profit factor:  {pf}")
    print(f"  Max drawdown:   ${dd}")
    print(f"  Exit reasons:   {results['exit_reasons']}")
    print(f"  Gap buckets:    {results['gap_distribution']}")

    if results["best_trade"]:
        b = results["best_trade"]
        print(f"  Best trade:     {b['ticker']} on {b['date']} +${b['pnl_usd']} ({b['gap_pct']}% gap)")
    if results["worst_trade"]:
        w = results["worst_trade"]
        print(f"  Worst trade:    {w['ticker']} on {w['date']} ${w['pnl_usd']} ({w['gap_pct']}% gap)")

    # Telegram message
    lines = [
        f"📈 *Backtest Complete — Agent {agent_id} ({source})*",
        f"_{_fmt_et_sgt()}_",
        f"Period: {results['period']}",
        "",
        f"━━━ RESULTS ━━━",
        f"{result_emoji} *Total P&L: ${pnl}*  |  Win Rate: {wr}%",
        f"Trades: {results['trades_taken']}  |  Wins: {results['wins']}  |  Losses: {results['losses']}",
        f"Avg Win: ${results['avg_win_usd']}  |  Avg Loss: ${results['avg_loss_usd']}",
        f"Profit Factor: {pf}  |  Max Drawdown: ${dd}",
        "",
        f"━━━ GAP DISTRIBUTION ━━━",
    ]
    for bucket, count in results["gap_distribution"].items():
        lines.append(f"  {bucket}: {count} trades")

    lines += ["", "━━━ EXIT REASONS ━━━"]
    for reason, count in results["exit_reasons"].items():
        lines.append(f"  {reason}: {count}")

    if results["best_trade"]:
        b = results["best_trade"]
        lines += [
            "",
            f"🏆 Best: *{b['ticker']}* {b['date']}  +${b['pnl_usd']}  ({b['gap_pct']}% gap)",
        ]
    if results["worst_trade"]:
        w = results["worst_trade"]
        lines += [
            f"💀 Worst: *{w['ticker']}* {w['date']}  ${w['pnl_usd']}  ({w['gap_pct']}% gap)",
        ]

    lines += ["", "_Paper backtest using Alpaca historical data. Not financial advice._"]
    _send_telegram("\n".join(lines))

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backtest — Bagholder Exit Liquidity Strategy")
    parser.add_argument("--agent", required=True,
                        help="Agent number: 1, 2, 3, or 'all'")
    args = parser.parse_args()

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("❌ Alpaca API keys not found in config.yaml")
        sys.exit(1)

    agent_ids = [1, 2, 3] if args.agent == "all" else [int(args.agent)]

    for agent_id in agent_ids:
        try:
            results = run_backtest(agent_id)
            print_and_notify(results)
        except Exception as e:
            print(f"❌ Agent {agent_id} backtest crashed: {e}")
            import traceback; traceback.print_exc()
            _send_telegram(f"❌ *Backtest Agent {agent_id} failed*\n{e}")

if __name__ == "__main__":
    main()
