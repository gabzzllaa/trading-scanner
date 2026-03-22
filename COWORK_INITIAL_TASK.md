# Initial Task Prompt for Cowork

Open Cowork, create a project called "Trading Scanner" pointed at ~/trading-scanner, paste the project instructions from the other file, then paste this as your first task:

---

Set up a complete trading scanner system in this project folder. Work through these phases in order, pausing between each so I can verify before continuing.

## Phase 1: Prerequisites & OpenClaw

1. Check that Node.js 22+ and Python 3.10+ are installed. If Node is too old, tell me and I'll upgrade manually.
2. Run `npm install -g @openclaw/cli`
3. Run `openclaw onboard` — PAUSE here so I can enter my Anthropic API key when prompted
4. After onboarding completes, start the gateway: `openclaw gateway run`
5. Verify with `openclaw status`

Tell me the results before moving on.

## Phase 2: Install ClawPort

1. Run `npm install -g clawport-ui`
2. Run `clawport setup` (auto-detects OpenClaw config)
3. Run `clawport dev` to start the dashboard
4. Confirm http://localhost:3000 is accessible

Tell me the results before moving on.

## Phase 3: Create the scanner engine

Create `scanner/scanner.py` in this project folder. This is a pure Python script (no LLM calls) that:

**Layer 1 — Weekly watchlist builder:**
- Scrapes Finviz screener with filters: price <$10, short interest >10%, 6-month perf <-30%, avg volume >100K
- For each result (up to 80 stocks), scrapes the individual Finviz quote page for detailed data: exact short float %, 6-month performance, 52-week range, market cap, avg volume
- Scores each stock on conditions 1-3 from the project instructions (prior decline, price/cap, short interest)
- Saves to `scanner/data/watchlist.json` with timestamp
- Includes rate limiting (0.8-1.5s between Finviz requests to avoid blocks)

**Layer 2 — Morning pre-market scanner:**
- Scrapes 3 sources in parallel:
  - TradingView Scanner API (POST to https://scanner.tradingview.com/america/scan) — filter premarket_gap > 0.20, stock type, primary listing
  - Barchart API (GET https://www.barchart.com/proxies/core-api/v1/quotes/get) — list stocks.us.gap_up.pre_market
  - StockAnalysis.com (HTML scrape https://stockanalysis.com/markets/premarket/gainers/)
- Deduplicates by ticker
- Cross-references against watchlist
- Scores each candidate using all 6 conditions from the project instructions
- For A+ candidates (score ≥35), computes trade parameters:
  - Entry: pre-market price
  - Stop: 10% above pre-market price
  - Target 1: 50% retracement of gap (pm_price - (gap_amount * 0.5))
  - Target 2: previous close (full gap fill)
  - Position size: $100 risk / stop_distance = number of shares
  - Time stop: 10:30 AM ET
- Saves to `scanner/data/latest_scan.json` and `scanner/data/history/scan_YYYYMMDD_HHMMSS.json`

**CLI interface:**
```
python scanner/scanner.py --mode watchlist   # Build watchlist only
python scanner/scanner.py --mode morning     # Morning scan only  
python scanner/scanner.py --mode full        # Refresh watchlist if >7 days old, then morning scan
```

Also create `scanner/requirements.txt` with: requests, beautifulsoup4

Install the dependencies: `pip install -r scanner/requirements.txt`

Test the watchlist build: `python scanner/scanner.py --mode watchlist`

Confirm the watchlist.json file was created and show me the top 5 stocks with their scores.

## Phase 4: Register agents in ClawPort

Create `~/.openclaw/workspace/clawport/agents.json` with 5 agents:

1. **Orchestrator** (id: orchestrator) — purple, emoji 🎯, coordinates all scanners
2. **Bagholder Scanner** (id: bagholder-scanner) — red, emoji 📉, reports to orchestrator, description: "Scans for bagholder exit liquidity setups"
3. **Watchlist Builder** (id: watchlist-builder) — amber, emoji 📋, reports to orchestrator, description: "Weekly Finviz screener"
4. **ORF Scanner** (id: orf-scanner) — cyan, emoji 📊, reports to orchestrator, description: "Opening range panic fade (planned)"
5. **Earnings Scanner** (id: earnings-scanner) — blue, emoji 💰, reports to orchestrator, description: "Earnings gap strategies (planned)"

Restart ClawPort and verify the agents appear in the org chart at localhost:3000.

## Phase 5: Set up scheduled scans

Use Cowork's /schedule feature to create these recurring tasks:

1. **Weekly watchlist build** — every Sunday at 8:00 PM SGT:
   `cd ~/trading-scanner && python scanner/scanner.py --mode watchlist`

2. **Morning scan 1** — Mon-Fri at 6:00 PM SGT:
   `cd ~/trading-scanner && python scanner/scanner.py --mode full`

3. **Morning scan 2** — Mon-Fri at 7:00 PM SGT:
   `cd ~/trading-scanner && python scanner/scanner.py --mode morning`

4. **Morning scan 3** — Mon-Fri at 8:00 PM SGT:
   `cd ~/trading-scanner && python scanner/scanner.py --mode morning`

5. **Morning scan 4** — Mon-Fri at 9:00 PM SGT:
   `cd ~/trading-scanner && python scanner/scanner.py --mode morning`

If Cowork's /schedule doesn't support cron-style scheduling for these, fall back to OpenClaw cron:
```
openclaw cron add --name "watchlist-build" --schedule "0 12 * * 0" --command "python ~/trading-scanner/scanner/scanner.py --mode watchlist"
openclaw cron add --name "morning-scan-6pm" --schedule "0 10 * * 1-5" --command "python ~/trading-scanner/scanner/scanner.py --mode full"
openclaw cron add --name "morning-scan-7pm" --schedule "0 11 * * 1-5" --command "python ~/trading-scanner/scanner/scanner.py --mode morning"
openclaw cron add --name "morning-scan-8pm" --schedule "0 12 * * 1-5" --command "python ~/trading-scanner/scanner/scanner.py --mode morning"
openclaw cron add --name "morning-scan-9pm" --schedule "0 13 * * 1-5" --command "python ~/trading-scanner/scanner/scanner.py --mode morning"
```

(Cron times are UTC: 10 UTC = 6 AM ET = 6 PM SGT during EDT)

## Phase 6: Telegram notifications (optional)

I'll create the Telegram bot myself via @BotFather. Once I have the token, add a `send_telegram_alert()` function to scanner.py that sends a formatted message when A+ candidates are found. The message should include:
- Number of A+ setups
- For each: ticker, gap %, score, entry price, stop, target, shares
- Reminder: "Short at 9:30 PM SGT open. Verify catalyst is hollow."

Use env vars TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

---

Start with Phase 1 now. Show me what you find for Node.js and Python versions, then proceed with the OpenClaw install.
