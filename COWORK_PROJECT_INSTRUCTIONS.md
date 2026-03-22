# Trading Scanner — Cowork Project Instructions

Paste this entire block into the "Project Instructions" field when creating your Cowork project.

---

## What this project is

A trading scanner system that identifies pre-market gap-up stocks primed for a fade (price reversal) at market open. It runs daily during US pre-market hours (6–9 AM ET = 6–9 PM Singapore time) and alerts me to high-probability short setups.

## The strategy: Bagholder Exit Liquidity Pattern

Stocks that have been in prolonged decline (6–12 months, 50%+ drawdown) create trapped holders. When these stocks suddenly gap up 30%+ in pre-market on no real fundamental catalyst (just social media hype, meme activity, or vague news), the trapped holders sell aggressively at the open, overwhelming the speculative buyers. The stock fades back down within 60 minutes.

### 6 scoring conditions (each /10, total /60):

1. **Prior decline ≥50% over ≥3 months** — sweet spot 60–80% over 6–12 months
2. **Price <$10, market cap <$2B** — lower = more retail-dominated = more predictable
3. **Short interest ≥10% of float** — indicates widespread bearish conviction
4. **Pre-market spike ≥30%** — optimal 50–100%+, must be sudden
5. **No real fundamental catalyst** — social media/meme = max score; M&A/FDA approval = DO NOT TRADE
6. **Pre-market volume ≥5x average daily volume** — confirms retail FOMO surge

### Scoring tiers:
- Score ≥35: A+ setup — full trade parameters, high conviction
- Score 20–34: Good setup — monitor, verify catalyst
- Score <20: Skip

### Trade execution:
- Short at or near 9:30 AM ET open
- Stop loss: 5–10% above pre-market high
- Target 1: 50% retracement of spike (primary)
- Target 2: Full gap fill to prior close (extended)
- Time stop: Close position by 10:30 AM ET regardless
- Position sizing: Risk max 1% of capital per trade, max 2% daily loss

## Architecture

- **OpenClaw** — agent runtime (gateway process running locally)
- **ClawPort** — dashboard UI (Next.js app at localhost:3000)
- **Python scanner** — the actual scraping + scoring logic (no LLM needed for this)
- **Telegram bot** — push notifications for A+ setups
- **Scheduled tasks** — Cowork scheduled tasks or OpenClaw cron

## Data sources (all free, no API keys needed):
- **Finviz** — weekly watchlist screening (HTML scrape)
- **TradingView Scanner API** — pre-market gappers (public JSON API)
- **Barchart API** — pre-market gappers (public JSON API)
- **StockAnalysis.com** — pre-market gainers (HTML scrape backup)

## File structure in this project folder:

```
~/trading-scanner/
├── scanner/
│   ├── scanner.py          # Core scanning + scoring engine
│   ├── requirements.txt    # Python deps: requests, beautifulsoup4
│   └── data/
│       ├── watchlist.json   # Weekly watchlist (auto-generated)
│       ├── latest_scan.json # Most recent scan results
│       └── history/         # Historical scans
├── clawport/
│   └── agents.json         # Agent registry for ClawPort dashboard
├── config.yaml             # Trading parameters (capital, risk %)
└── CLAUDE.md               # This file
```

## My timezone
Singapore (SGT, UTC+8). US pre-market 6–9 AM ET = 6–9 PM SGT.

## My trading capital
$10,000 USD starting. Risk 1% ($100) per trade max.

## Important rules
- NEVER auto-execute trades. Alert only. I make the final decision.
- Condition 5 (catalyst check) is ALWAYS manual — I verify via Reddit/Stocktwits links.
- If the catalyst is real (M&A, FDA approval, earnings beat), DO NOT TRADE regardless of score.
- All times displayed should show both ET and SGT.
