# Alphard service — current state (Phase 1.6)

> ⚠️ **LEGACY DOCUMENT** — Snapshot from Phase 1.6 (before Macro Agent,
> before observability metrics, before Postgres audit). The diagrams in
> this file reflect Phase 1.6 topology only.
>
> Current architecture (Phase 2.x with Macro Agent, RiskGate, observability
> stack):
>
> - [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — canonical current architecture
> - [`docs/PHASE2-8-METRICS.md`](PHASE2-8-METRICS.md) — current runtime diagrams
> - [`DOCS-INDEX.md`](../DOCS-INDEX.md) — top-level navigation, including the legacy table
>
> Do **not** make decisions based on this file. Preserved for audit trail only.
> See issue #292.

---

Three diagrams: stack, runtime, fallback chain. Render in any mermaid viewer (GitHub, VSCode, mermaid.live).

## 1. Service stack

```mermaid
graph TB
    subgraph NET["alphard-net (bridge, 192.168.0.0/16)"]
        BOT["alphard-bot<br/>ghcr.io/m0rtal/alphard:sha-c120eaf<br/>restart: unless-stopped<br/>memory: 2G, cpus: 1.0"]
        PG["postgres:16-alpine<br/>volume: /mnt/appdata/alphard/postgres<br/>healthcheck: psql SELECT 1"]
        RD["redis:7-alpine<br/>volume: /mnt/appdata/alphard/redis<br/>healthcheck: redis-cli ping"]
        INIT["pg-init (one-shot)<br/>alpine+psql<br/>injects 192.168.0.0/16 trust<br/>+ pg_reload_conf()"]
    end

    INIT -.waits for.-> PG
    BOT -.depends_on healthy.-> PG
    BOT -.depends_on completed.-> INIT

    classDef active fill:#cdf,stroke:#333,color:#000
    classDef oneshot fill:#fdc,stroke:#333,color:#000
    classDef db fill:#dfc,stroke:#333,color:#000
    class BOT active
    class INIT oneshot
    class PG,RD db
```

## 2. Runtime — what's running inside alphard-bot

```mermaid
graph TB
    subgraph ENTRY["entrypoint.sh (Bash)"]
        E1["source .env (3 candidates)"]
        E2["Tinkoff token sanity"]
        E3["wait postgres TCP ≤60s"]
        E4["DSN password fingerprint vs /tmp/alphard-dsn-fp"]
        E5["auth_probe → INSERT _auth_probe"]
        E6["init_schema (ADD COLUMN IF NOT EXISTS)"]
        E7["setsid backfill_history_md.py &"]
    end

    subgraph MAIN["PID 1 — python -m src.main (heartbeat 60s)"]
        H1["while not _shutdown_event:<br/>  log('Heartbeat')<br/>  time.sleep(60)"]
    end

    subgraph DAEMON["Thread 'alphard-daily-sync' (daemon)"]
        D1["ms_to_20msk = seconds_until(20:00 MSK)"]
        D2["_sleep_interruptible(ms_to_20msk)"]
        D3["subprocess.run('daily_sync.py --days 5',<br/> timeout=600, cwd=/app)"]
        D4["_sleep_interruptible(24*3600)"]
        D3 --> D4 --> D3
    end

    subgraph BACKFILL["PID ~19 — backfill_history_md.py (background)"]
        B1["loaders: MD + gRPC + MOEX<br/>→ FallbackDataLoader"]
        B2["auth_probe('backfill_pre_run')"]
        B3["upsert_tickers()"]
        B4["for each ticker:<br/>  if _is_complete: skip<br/>  else: _backfill_one<br/>    ├ signal.alarm(900s)<br/>    ├ for b in loader.iter_ohlcv:<br/>    │   loader=FallbackDataLoader<br/>    ├ store.upsert_ohlcv<br/>    └ _set_complete_flag if done"]
        B5["if 5 consecutive fails:<br/>  return 3 (circuit breaker)"]
        B4 --> B5
    end

    E7 --> BACKFILL
    E6 --> MAIN
    E6 --> DAEMON

    MAIN -.starts thread.-> DAEMON

    classDef bash fill:#fed,stroke:#333
    classDef python fill:#dfd,stroke:#333
    classDef thread fill:#ddf,stroke:#333
    classDef bg fill:#fdd,stroke:#333
    class E1,E2,E3,E4,E5,E6,E7 bash
    class H1 python
    class D1,D2,D3,D4 thread
    class B1,B2,B3,B4,B5 bg
```

## 3. Fallback chain (one ticker fetch)

```mermaid
flowchart LR
    REQ["iter_ohlcv(ticker, start, end)"]
    S1["1. tinkoff_md<br/>(history-data HTTP)<br/>yearly ZIP → agg to daily"]
    S2["2. tinkoff_grpc<br/>(GetCandles)<br/>1-year chunks × N"]
    S3["3. moex_iss<br/>(free REST)<br/>500-bar pagination"]
    OK["yield OHLCVRow"]
    FAIL["ALL sources returned 0 bars"]

    REQ --> S1
    S1 -- "rows>0<br/>or raise" --> S2
    S1 -- "empty" --> S2
    S2 -- "rows>0<br/>or raise" --> S3
    S2 -- "empty" --> S3
    S3 -- "rows>0" --> OK
    S3 -- "empty" --> FAIL

    classDef src fill:#eef,stroke:#333
    class S1,S2,S3 src
```

## 4. Schedule (MSK wall-clock)

```mermaid
gantt
    title Daily pipeline (anchored to 20:00 MSK)
    dateFormat HH:mm
    axisFormat %H:%M
    section Container
    Heartbeat 60s    :active, h1, 00:00, 24h
    Backfill (setsid)  :active, b1, 00:00, 24h
    section Markets
    MOEX daily close :milestone, m1, 18:40, 0m
    daily_sync 5d pull :crit, s1, 20:00, 5m
```

## 5. Postgres schema (7 tables)

```mermaid
erDiagram
    ticker_universe ||--o{ ohlcv_daily : "FK ticker"
    ticker_universe ||--o{ corporate_actions : ""
    ticker_universe ||--o{ delisting_log : ""
    ticker_universe ||--o{ news_embedding : ""
    ticker_universe ||--o{ decision_log : ""

    ticker_universe {
        VARCHAR ticker PK
        VARCHAR figi
        TEXT name
        INTEGER lot
        VARCHAR isin
        VARCHAR currency
        VARCHAR class_code
        BOOLEAN delisted
        DATE delisted_at
        DATE listed_at
        VARCHAR source
        BOOLEAN backfill_complete
        TIMESTAMPTZ backfill_complete_at
        TIMESTAMPTZ updated_at
    }

    ohlcv_daily {
        VARCHAR ticker FK
        DATE ts
        DECIMAL open
        DECIMAL high
        DECIMAL low
        DECIMAL close
        DECIMAL volume
        DECIMAL adj_close
        TIMESTAMPTZ updated_at
    }

    corporate_actions {
        VARCHAR ticker FK
        DATE ts
        VARCHAR kind
        DECIMAL value
        VARCHAR source
    }

    delisting_log {
        SERIAL id PK
        VARCHAR ticker FK
        DATE delisted_at
        TEXT reason
        VARCHAR source
    }

    _auth_probe {
        SMALLINT id PK
        TIMESTAMPTZ probed_at
        VARCHAR source
    }

    news_embedding {
        BIGSERIAL id PK
        VARCHAR ticker FK
        TIMESTAMPTZ ts
        TEXT headline
        VARCHAR source
    }

    decision_log {
        BIGSERIAL id PK
        VARCHAR kind
        VARCHAR ticker FK
        JSONB decision
        VARCHAR source
    }
```

## 6. Defense-in-depth (5 layers)

```mermaid
flowchart TB
    L1["L1: depends_on<br/>postgres healthy + pg-init completed"]
    L2["L2: entrypoint.sh<br/>TCP probe + DSN fingerprint + auth_probe"]
    L3["L3: backfill pre-run guard<br/>auth_probe + ticker_universe SELECT"]
    L4["L4: pg-init one-shot<br/>192.168.0.0/16 trust line"]
    L5["L5: per-ticker SIGALRM(900s)<br/>+ circuit breaker 5 fails → abort"]

    L1 --> L2 --> L3 --> L4
    L2 --> L5

    classDef layer fill:#ffd,stroke:#333
    class L1,L2,L3,L4,L5 layer
```

## 7. Profiles (lazy, not deployed)

```mermaid
graph LR
    PROFILES["docker-compose profiles"]
    O["[observability]<br/>prometheus:9090<br/>grafana:3000"]
    S["[scheduled]<br/>supercronic<br/>19:00 daily_sync<br/>+ freshness 5m<br/>+ db_health 1m<br/>⚠️ daily_sync уже в main-loop"]
    C["[default]<br/>alphard-bot + postgres + redis"]

    PROFILES --> C
    PROFILES -.-> O
    PROFILES -.-> S

    classDef default fill:#dfd
    classDef lazy fill:#eee
    class C default
    class O,S lazy
```
