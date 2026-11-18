# Alpha Scanner

Multi-chain (Solana, Ethereum, Base, BSC) on-chain scanner, security analyzer and signal platform.

## Directory structure

```
alpha-scanner/
├── README.md
├── docker-compose.yml               # Phase 2: postgres + redis + api + worker
├── backend/
│   ├── requirements.txt
│   ├── .env.example
│   ├── data/
│   │   └── smart_wallets.example.json   # copy to smart_wallets.json
│   ├── app/
│   │   ├── __init__.py
│   │   ├── config.py               # pydantic-settings, all env vars
│   │   ├── db.py                   # async engine, session factory, init_db
│   │   ├── cache.py                # Redis/in-memory cache, pub/sub, token-bucket rate limiter
│   │   ├── models.py               # SQLAlchemy 2.0 ORM
│   │   ├── schemas.py              # Pydantic v2 domain + API contracts
│   │   ├── scoring_engine.py       # Alpha Score (pure, deterministic)
│   │   ├── scanner_worker.py       # discovery → enrich → filter → audit → score → persist → signal
│   │   ├── repository.py           # read-side queries + ORM→schema conversion
│   │   ├── main.py                 # FastAPI app, lifespan, /healthz, /ws/live-signals
│   │   ├── api/
│   │   │   ├── tokens.py           # /api/tokens, detail, chart, /api/signals, /api/status
│   │   │   └── ai.py               # POST /api/ai-analyze
│   │   ├── ws.py                   # SignalHub: Redis pub/sub or DB polling, per-client filters
│   │   ├── ai_analyst.py           # OpenAI-compatible LLM client + deterministic rules fallback
│   │   └── telegram_alerts.py      # alerts + /top /token /status commands
│   ├── Dockerfile
│   └── tests/
│       ├── test_scoring_engine.py
│       └── test_api.py
└── frontend/                        # Next.js 16 App Router, Tailwind v4 (theme tokens in globals.css)
    ├── Dockerfile
    ├── app/
    │   ├── layout.tsx               # Inter + JetBrains Mono, dark theme
    │   ├── globals.css              # @theme tokens: carbon #0A0E17, surface #111827, emerald, amber
    │   ├── page.tsx                 # dashboard
    │   └── token/[chain]/[address]/page.tsx   # deep link: dashboard with inspector open
    ├── components/
    │   ├── Dashboard.tsx            # state, data loading, live wiring, toasts
    │   ├── KpiBar.tsx               # active tokens, high-alpha 24h, top gainer, live pulse
    │   ├── ScannerTable.tsx         # filters, sortable columns, pagination
    │   ├── ScoreGauge.tsx           # 0-100 dial
    │   ├── Badges.tsx               # chain pill, alpha badge, security pills, avatar
    │   ├── LiveFeed.tsx             # WebSocket signal stream
    │   ├── TokenInspector.tsx       # slide-over: score breakdown, stats, security checklist
    │   ├── PriceChart.tsx           # TradingView Lightweight Charts v5
    │   └── AIAnalystCard.tsx        # 3-bullet AI summary
    └── lib/ (api.ts, useLiveSignals.ts, format.ts, types.ts)
```

## Run the worker (Phase 1)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m app.scanner_worker --once   # one cycle
.venv\Scripts\python -m app.scanner_worker          # loop every 60s
```

Windows note: if the project lives under a very long path, SQLAlchemy's compiled extension can fail to load
("The filename or extension is too long"). Put the venv at a short path such as `C:\venvs\alpha`.

## Run the API (Phase 2)

```powershell
cd backend
# API + worker in one process (local MVP):
$env:RUN_SCANNER_IN_API="true"; .venv\Scripts\uvicorn app.main:app --port 8000
# or production-style, two processes:
.venv\Scripts\uvicorn app.main:app --port 8000
.venv\Scripts\python -m app.scanner_worker
```

Or with Docker (Postgres + Redis + API + worker), after creating `backend/.env`:

```powershell
docker compose up --build
```

Interactive docs: http://localhost:8000/docs

| Endpoint | Purpose |
|---|---|
| `GET /api/tokens` | Scanner table. Query: `chain`, `min_score`, `min_liquidity`, `badge`, `q`, `sort` (`score`/`volume`/`liquidity`/`age`/`change_1h`), `order`, `page`, `page_size` |
| `GET /api/tokens/{chain}/{address}` | Inspector: metrics, security audit, full score breakdown, score history |
| `GET /api/tokens/{chain}/{address}/chart?interval=5m` | OHLCV for Lightweight Charts (GeckoTerminal, falls back to scanner snapshots) |
| `POST /api/ai-analyze` | `{chain, address}` or inline `metrics`; 3-bullet analysis. Without `LLM_API_KEY`, returns the `rules-v1` analysis |
| `GET /api/signals` | Recent signals with gain since signal |
| `GET /api/status` | KPI bar: active tokens, 24h signals, high-alpha count, top gainer, live pulse |
| `WS /ws/live-signals` | `hello` (recent signals), `signal`, `scan_complete`, `heartbeat`. Send `{"type":"subscribe","chains":["solana"],"min_score":80}` to filter |

Signal ordering: the worker commits each signal before publishing it, so the token detail endpoint always
resolves for a token you just received, and a cycle's signals always arrive before its `scan_complete`.

### Telegram

Create a bot with @BotFather, then set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (a channel, group or user id).
Signals with score ≥ `TELEGRAM_MIN_SCORE` (default 80) are posted once each, even with several API replicas.
Commands: `/top [chain]`, `/token <address>`, `/status`, `/help`. Telegram runs inside the API process; if you
scale the API to several replicas, run `python -m app.telegram_alerts` as one separate process instead.

## Run the dashboard (Phase 3)

```powershell
cd frontend
npm install
copy .env.example .env.local      # NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev                       # http://localhost:3000
```

Production: `npm run build` then `npm run start`. `NEXT_PUBLIC_API_URL` is baked in at build time.
`docker compose up --build` also builds and serves the dashboard on port 3000.

The dashboard reloads the table and KPIs on every `scan_complete` WebSocket event and falls back to polling
every 60s if the socket is down. Opening a token rewrites the URL to `/token/{chain}/{address}`, so the URL
can be shared; Telegram alerts link there when `PUBLIC_DASHBOARD_URL` is set.

## Smart wallets

A token whose top holders include an enabled tracked wallet earns the 15-point smart-wallet component of the
Alpha Score. Wallets come from two sources:

- **manual**: yours. Either list them in `backend/data/smart_wallets.json` (format in
  `smart_wallets.example.json`; the worker re-imports the file whenever it changes, and the folder is mounted
  into the containers, so no rebuild) or use the CLI below.
- **birdeye**: verified profitable traders (needs a free `BIRDEYE_API_KEY`). Every 2 hours the worker pulls the
  most profitable traders of each token that rallied 50%+ after a signal (dev / sniper / bundler / insider tags
  dropped), plus once a day the week's top PnL wallets per chain (bot-like trade counts dropped), then checks each
  candidate's last 30 days with Birdeye's wallet PnL summary: win rate >= 55%, realized profit >= $5,000 and
  20–5,000 trades. Calls are logged and capped at 30 per 24h to stay inside the free plan (30 CU each, 30k CU/month).
  `python -m app.smart_money candidates` shows every candidate and why it was rejected.
- **auto**: discovered every 30 minutes from the scanner's own history. A wallet qualifies when, at signal time,
  it was a top holder of at least 2 tokens that later rallied 50%+, with a win rate of at least 50% across the
  signalled tokens it held. Pools, burn addresses, contracts, RugCheck "insiders" and holders above 20% are
  ignored, and so are wallets seen in more than 40 tokens. Tune with the `SMART_AUTO_*` settings in `config.py`.

```powershell
ssh -i $HOME\.ssh\claude_vps root@148.230.113.18 "cd /opt/alpha-scanner && docker compose exec api python -m app.smart_money list --all"
```

Other commands: `birdeye` (run Birdeye discovery now), `candidates`, `add <chain> <address> [label]`, `remove <chain> <address>` (deletes a manual wallet;
permanently disables an auto one), `enable <chain> <address>`, `discover`, `import <file.json>`.
The dashboard shows the list read-only (`GET /api/smart-wallets`).

**Matching.** Every scan checks each tracked wallet against each scanned token directly on-chain, not just the
top 10-20 holders: on Solana by deriving the wallet's associated token accounts (SPL Token and Token-2022) and
reading them in batches of 100 with `getMultipleAccounts`; on Ethereum, Base and BSC with batched ERC-20
`balanceOf` calls. A position counts when it is worth at least `HOLDINGS_MIN_USD` ($100). Free public RPCs
are used by default; set `SOLANA_RPC_URL` / `EVM_RPC_URLS` to your own endpoints for heavier use.

## Wallet buy alerts

Pick any wallets (smart or not) and get a Telegram message when they buy. From the owner chat
(`TELEGRAM_CHAT_ID` only):

```
/wallets                      smart wallets, each with a 👀 Track / ✖ Untrack button
/track <address> [name]       chain auto-detected: base58 = Solana, 0x = Ethereum + Base + BSC
/untrack <address>            /tracked
```

The slash is optional in the owner chat (`track <address>` works). The dashboard's Smart wallets panel has a
Track button per wallet: it opens the bot with `?start=track_sol_<address>` / `track_evm_<address>`, and pressing
Start adds it (accepted from the owner chat only, since the site itself has no login).

The worker polls every 30s. Solana: new transactions *signed by the wallet* (getSignaturesForAddress →
getTransaction, `maxSupportedTransactionVersion: 1`), token balance increases excluding SOL/USDC/USDT.
EVM: ERC-20 transfers to the wallet in transactions *the wallet sent* (address-less `eth_getLogs` on dRPC's
free tier, split into 100-block chunks). A buy alerts when the token has a DEX pair with ≥ $5K liquidity and the
buy is ≥ $100; repeat buys within 60 minutes are grouped; history from before `/track` is never replayed.
Bought tokens are also added to the scanner's next cycle. Tuning: `TRACKER_*` settings in `config.py`.

**Sells.** A decrease in a tracked wallet's token balance, in a transaction it signed/sent, is a sale when the
wallet received SOL / a quote token (Solana) or the transaction went through a router (EVM); otherwise it is a
transfer out (recorded, no alert). Sell alerts show the share of the position sold (from the remaining balance,
"FULL EXIT" at ≥ 99%) and, when this wallet's buys of the token were recorded, the estimated P&L against their
average cost. Partial sells within 60 minutes are grouped; a full exit always alerts. On a Solana 429 the
tracker keeps its place at the last transaction read and resumes on the next poll, so trades are not lost.

## Data sources and limits

| Source | Used for | Keyless limit |
|---|---|---|
| DexScreener `token-boosts`, `token-profiles` | discovery | 60/min |
| DexScreener `tokens/v1/{chain}/{addrs}` | pair metrics (30 tokens/call) | 300/min |
| GeckoTerminal `trending_pools`, `new_pools` | discovery | 30/min |
| RugCheck `tokens/{mint}/report` | Solana mint/freeze, LP lock, holders, risks | ~ |
| GoPlus `token_security` | EVM security (Solana fallback) | ~30/min, returns `code 4029` when exceeded |
| GeckoTerminal `pools/{pool}/ohlcv` | inspector chart (shares the quota with discovery) | 30/min |

GoPlus is the bottleneck on EVM chains. Security results are cached for 30 minutes, and audits that miss the
per-cycle deadline (70% of the interval) are retried next cycle, so coverage fills in over a few cycles. Set
`GOPLUS_ACCESS_TOKEN` for higher limits.

## Alpha Score

See the docstring at the top of `backend/app/scoring_engine.py` for every formula and threshold. Missing data
never earns points. Honeypots, `cannot_sell_all`, sell tax ≥ 50% and RugCheck "rugged" force the score to 0.
