-- Phase 2 SQLite schema.
-- SQLite for OLTP-style state now; DuckDB layered on top for analytical/
-- backtest workloads once Phase 6 needs it. Migrate to TimescaleDB/ClickHouse
-- only when tick volume or concurrent-writer load actually requires it --
-- see docs/architecture.md Section F for the rationale.

PRAGMA foreign_keys = ON;

-- Instruments: one row per (exchange, instrument_id). Refreshed on a timer
-- per docs/architecture.md Section E.3 (every 5 minutes minimum, since Delta
-- adds new strikes/maturities intraday).
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id       TEXT NOT NULL,
    exchange             TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    underlying           TEXT NOT NULL,
    option_type          TEXT NOT NULL CHECK (option_type IN ('call', 'put')),
    option_variant       TEXT NOT NULL CHECK (option_variant IN ('vanilla', 'turbo', 'spread', 'other')),
    strike                TEXT NOT NULL,  -- stored as TEXT, parsed as Decimal in app layer -- never float
    expiry_ts             TEXT NOT NULL,  -- ISO8601 UTC
    settlement_ts          TEXT NOT NULL, -- ISO8601 UTC; may differ from expiry_ts
    settlement_method       TEXT NOT NULL CHECK (settlement_method IN ('cash', 'physical')),
    settlement_price_formula TEXT NOT NULL,
    contract_multiplier      TEXT NOT NULL,
    lot_size                  TEXT NOT NULL,
    tick_size                  TEXT NOT NULL,
    quote_currency               TEXT NOT NULL,
    settlement_currency            TEXT NOT NULL,
    is_european                     INTEGER NOT NULL CHECK (is_european IN (0, 1)),
    last_synced_at                  TEXT NOT NULL,
    PRIMARY KEY (exchange, instrument_id)
);

CREATE INDEX IF NOT EXISTS idx_instruments_underlying_expiry
    ON instruments (underlying, expiry_ts);

-- Market data: append-only tick storage. Never overwritten.
-- Per architecture.md Section A.1: best_bid/best_ask are the only fields
-- that may feed entry/exit P&L math. mark_price/index_price are for
-- margin/risk calculations only.
CREATE TABLE IF NOT EXISTS market_data (
    ts                TEXT NOT NULL,       -- exchange-reported timestamp, ISO8601 UTC
    exchange          TEXT NOT NULL,
    instrument_id     TEXT NOT NULL,
    best_bid          TEXT,
    best_ask          TEXT,
    bid_size          TEXT,
    ask_size          TEXT,
    mark_price        TEXT,
    index_price       TEXT,
    iv                TEXT,
    delta             TEXT,
    gamma             TEXT,
    theta             TEXT,
    vega              TEXT,
    open_interest     TEXT,
    volume_24h        TEXT,
    ingested_at       TEXT NOT NULL,       -- our own ingestion timestamp, for clock-skew detection
    FOREIGN KEY (exchange, instrument_id) REFERENCES instruments (exchange, instrument_id)
);

CREATE INDEX IF NOT EXISTS idx_market_data_instrument_ts
    ON market_data (exchange, instrument_id, ts);

-- Matched candidate pairs (Phase 3 output)
CREATE TABLE IF NOT EXISTS candidate_pairs (
    pair_id              TEXT PRIMARY KEY,
    short_exchange        TEXT NOT NULL,
    short_instrument_id    TEXT NOT NULL,
    long_exchange            TEXT NOT NULL,
    long_instrument_id       TEXT NOT NULL,
    match_confidence          TEXT NOT NULL,  -- 1.0 = exact match, <1.0 = interpolated/lower confidence
    classification              TEXT NOT NULL, -- per architecture.md Section D.5
    created_at                   TEXT NOT NULL,
    FOREIGN KEY (short_exchange, short_instrument_id) REFERENCES instruments (exchange, instrument_id),
    FOREIGN KEY (long_exchange, long_instrument_id) REFERENCES instruments (exchange, instrument_id)
);

-- Signals (Phase 4 scanner output)
CREATE TABLE IF NOT EXISTS signals (
    signal_id            TEXT PRIMARY KEY,
    ts                    TEXT NOT NULL,
    pair_id                TEXT NOT NULL REFERENCES candidate_pairs (pair_id),
    net_entry_cost           TEXT NOT NULL,
    expected_value             TEXT NOT NULL,
    expected_profit              TEXT NOT NULL,
    prob_of_profit                 TEXT NOT NULL,
    var_95                          TEXT,
    expected_shortfall                TEXT,
    required_margin                     TEXT,
    score                                 TEXT NOT NULL,
    score_breakdown                        TEXT,  -- JSON blob
    -- Per docs/architecture.md Section M.2: whether net_entry_cost cleared
    -- RISK.min_net_credit (a real net credit, not a net debit) at pricing
    -- time. 0/1, never NULL -- every priced candidate gets a real answer to
    -- this question, persisted alongside the numbers it was computed from,
    -- so "why wasn't this shown as an opportunity" is always answerable
    -- from this table alone, not just from that run's console log.
    entry_eligible                          INTEGER NOT NULL DEFAULT 0 CHECK (entry_eligible IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts);
CREATE INDEX IF NOT EXISTS idx_signals_entry_eligible ON signals (entry_eligible);

-- Manual recommendations (pricing/manual_spread_finder.py output).
-- Added 2026-08-26 alongside the project's pivot from full automation to a
-- manual-trading-assistant model (see docs/architecture.md's latest
-- section): since Shark/CoinSwitch have no reliable automated data feed
-- yet, the short leg's price is manually observed and typed in by a human
-- watching the exchange's own website, run through
-- pricing/manual_spread_finder.py, and the result needs to be visible on
-- the dashboard (not just printed to a terminal) so it's actually usable
-- as a "check this, then go trade it" workflow.
--
-- Distinct from `signals` deliberately, not reusing that table: `signals`
-- rows are produced by a fully-automated pipeline against live-fetched
-- data on both legs (see pricing/run_pricing.py) and its `pair_id` foreign
-- key assumes both legs already exist as rows in `candidate_pairs`/
-- `instruments`. A manual recommendation's short leg is user-typed input
-- that was never fetched from any adapter and doesn't correspond to a
-- `candidate_pairs` row at all -- forcing it into `signals`' shape would
-- mean fabricating a fake candidate_pairs/instruments row for data that
-- was never actually observed via any adapter, which is exactly the kind
-- of silent fabrication this project's fail-closed principle exists to
-- prevent elsewhere (see pricing/ev_engine.py's InsufficientDataError).
CREATE TABLE IF NOT EXISTS manual_recommendations (
    recommendation_id     TEXT PRIMARY KEY,
    ts                     TEXT NOT NULL,
    short_exchange           TEXT NOT NULL,  -- user-typed, e.g. 'shark' or 'coinswitch' -- NOT a candidate_pairs FK
    short_underlying          TEXT NOT NULL,
    short_option_type          TEXT NOT NULL CHECK (short_option_type IN ('call', 'put')),
    short_strike                 TEXT NOT NULL,
    short_expiry_date              TEXT NOT NULL,  -- date only (YYYY-MM-DD) -- user input has no time component
    short_bid_input                  TEXT NOT NULL,  -- exactly what the user typed in, unmodified
    long_exchange                       TEXT NOT NULL,  -- always 'delta_india' currently, but not hardcoded here
    long_instrument_id                    TEXT NOT NULL,
    long_expiry_ts                          TEXT NOT NULL,
    long_ask_live                             TEXT NOT NULL,  -- live-fetched at check time, per architecture.md Section A.1
    long_ask_size_live                          TEXT,          -- for liquidity scoring, see below
    net_entry_cost                                TEXT NOT NULL,
    entry_eligible                                  INTEGER NOT NULL CHECK (entry_eligible IN (0, 1)),
    -- Liquidity scoring added 2026-08-26 alongside tax modeling (see
    -- pricing/tax.py): the short leg's size is whatever the user typed in
    -- (their own read of Shark/CoinSwitch's order book, which this project
    -- cannot independently verify -- see manual_spread_finder.py for the
    -- honesty note on why this is trusted input, not verified data), the
    -- long leg's size is real live data from Delta's own order book.
    -- max_safe_contracts = min(short_size_input, long_ask_size_live) --
    -- the largest size where BOTH legs can actually fill at the quoted
    -- price, not just one of them.
    short_size_input                                  TEXT,
    max_safe_contracts                                  TEXT,
    -- Tax fields, per pricing/tax.py -- see that module's docstring for
    -- the full list of assumptions this is NOT authoritative advice under.
    gross_profit_estimate                                 TEXT,
    tax_owed_estimate                                       TEXT,
    net_profit_after_tax_estimate                             TEXT,
    tds_withheld_estimate                                       TEXT
);

CREATE INDEX IF NOT EXISTS idx_manual_recommendations_ts ON manual_recommendations (ts);
CREATE INDEX IF NOT EXISTS idx_manual_recommendations_eligible ON manual_recommendations (entry_eligible);

-- Trades (Phase 7/8 output -- paper and, eventually and only with explicit
-- approval, live)
CREATE TABLE IF NOT EXISTS trades (
    trade_id             TEXT PRIMARY KEY,
    signal_id             TEXT REFERENCES signals (signal_id),
    exchange_short          TEXT NOT NULL,
    exchange_long             TEXT NOT NULL,
    instrument_short            TEXT NOT NULL,
    instrument_long               TEXT NOT NULL,
    short_qty                       TEXT NOT NULL,
    long_qty                          TEXT NOT NULL,
    entry_price_short                   TEXT,
    entry_price_long                      TEXT,
    entry_ts                                TEXT,
    exit_price_short                          TEXT,
    exit_price_long                             TEXT,
    exit_ts                                       TEXT,
    fees_total                                      TEXT,
    slippage_total                                    TEXT,
    margin_used                                         TEXT,
    realized_pnl                                          TEXT,
    status                                                  TEXT NOT NULL CHECK (
        status IN (
            'SCANNED', 'CANDIDATE', 'APPROVED', 'ENTRY_PENDING', 'PARTIAL_FILL',
            'OPEN', 'FIRST_LEG_EXPIRED', 'EXIT_PENDING', 'CLOSED', 'FAILED', 'ERROR'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);
