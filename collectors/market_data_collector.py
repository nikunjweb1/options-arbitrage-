"""
Phase 2 market-data collector.

Continuously pulls instruments + tickers from an ExchangeAdapter and writes
them into the SQLite tables defined in db/schema.sql. This is the thing that
needs to run gap-free for 24h to satisfy docs/architecture.md's Phase 2 exit
criterion:

    "24h of continuous, gap-free Delta options + underlying data captured
    and queryable."

Design principles carried over from the rest of this repo:
  - No trading logic anywhere in this file. It only ever calls read-only
    ExchangeAdapter methods (get_instruments, get_option_chain, get_ticker).
  - Fail-loud-per-instrument, fail-soft-overall: a single instrument's ticker
    call failing (rate limit blip, transient network error, testnet hiccup)
    is logged and retried with backoff, but does not crash the whole
    collector run. A crash-looping collector would produce exactly the
    "gaps" this phase is trying to rule out.
  - Every write is append-only to market_data (never UPDATE/DELETE) and
    upsert-only to instruments (INSERT OR REPLACE, since instrument specs
    can legitimately change -- e.g. a strike gets relisted).
  - Uses only the exchange-agnostic ExchangeAdapter Protocol, so pointing
    this at CoinSwitch or Deribit later is a config change, not a rewrite.
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import COLLECTOR, DB
from exchange_adapters.base import ExchangeAdapter
from normalization.schemas import MarketSnapshot, OptionContract

logger = logging.getLogger("collector")


def _configure_logging() -> None:
    COLLECTOR.log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(COLLECTOR.log_path),
            logging.StreamHandler(),
        ],
    )


class GracefulShutdown:
    """
    SIGINT/SIGTERM handler so a 24h unattended run can be stopped cleanly
    (finishes the in-flight write, closes the DB connection) instead of being
    killed mid-transaction.
    """

    def __init__(self) -> None:
        self.should_stop = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum: int, frame: Any) -> None:
        logger.info("Received shutdown signal (%s) -- finishing current cycle then stopping.", signum)
        self.should_stop = True


class MarketDataCollector:
    def __init__(self, adapter: ExchangeAdapter, db_path: Path | None = None) -> None:
        self._adapter = adapter
        self._db_path = db_path or DB.sqlite_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._tracked_instruments: dict[str, OptionContract] = {}
        self._stats = {
            "instrument_refresh_count": 0,
            "ticker_poll_count": 0,
            "ticker_success_count": 0,
            "ticker_failure_count": 0,
            "started_at": None,
        }

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    # -- retry wrapper --------------------------------------------------

    def _with_retries(self, description: str, fn, *args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(1, COLLECTOR.max_retries_per_call + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 -- intentionally broad: network/API errors of any shape must not crash the loop
                last_exc = exc
                backoff = COLLECTOR.retry_backoff_base_sec * (2 ** (attempt - 1))
                logger.warning(
                    "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                    description, attempt, COLLECTOR.max_retries_per_call, exc, backoff,
                )
                time.sleep(backoff)
        logger.error("%s failed after %d attempts, giving up for this cycle: %s",
                      description, COLLECTOR.max_retries_per_call, last_exc)
        return None

    # -- instrument refresh -----------------------------------------------

    def refresh_instruments(self) -> None:
        """
        Pulls the full option chain for every configured underlying and
        upserts into `instruments`. Also updates the in-memory set of
        instruments that `poll_tickers` will subsequently poll.
        """
        all_contracts: list[OptionContract] = []
        for underlying in COLLECTOR.underlyings:
            contracts = self._with_retries(
                f"get_option_chain({underlying})",
                self._adapter.get_option_chain,
                underlying=underlying,
            )
            if contracts is None:
                continue  # already logged; skip this underlying this cycle

            if COLLECTOR.max_instruments_per_underlying > 0:
                contracts = contracts[: COLLECTOR.max_instruments_per_underlying]

            all_contracts.extend(contracts)

        if not all_contracts:
            logger.warning("refresh_instruments: zero contracts fetched this cycle across all underlyings.")
            return

        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for c in all_contracts:
            rows.append(
                (
                    c.instrument_id, c.exchange, c.contract_symbol, c.underlying,
                    c.option_type.value, c.option_variant.value, str(c.strike),
                    c.expiry_timestamp.isoformat(), c.settlement_timestamp.isoformat(),
                    c.settlement_method.value, c.settlement_price_formula,
                    str(c.contract_multiplier), str(c.lot_size), str(c.tick_size),
                    c.quote_currency, c.settlement_currency, int(c.is_european), now,
                )
            )
            self._tracked_instruments[c.instrument_id] = c

        self._conn.executemany(
            """
            INSERT INTO instruments (
                instrument_id, exchange, symbol, underlying, option_type, option_variant,
                strike, expiry_ts, settlement_ts, settlement_method, settlement_price_formula,
                contract_multiplier, lot_size, tick_size, quote_currency, settlement_currency,
                is_european, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (exchange, instrument_id) DO UPDATE SET
                symbol=excluded.symbol, underlying=excluded.underlying,
                option_type=excluded.option_type, option_variant=excluded.option_variant,
                strike=excluded.strike, expiry_ts=excluded.expiry_ts,
                settlement_ts=excluded.settlement_ts, settlement_method=excluded.settlement_method,
                settlement_price_formula=excluded.settlement_price_formula,
                contract_multiplier=excluded.contract_multiplier, lot_size=excluded.lot_size,
                tick_size=excluded.tick_size, quote_currency=excluded.quote_currency,
                settlement_currency=excluded.settlement_currency, is_european=excluded.is_european,
                last_synced_at=excluded.last_synced_at
            """,
            rows,
        )
        self._conn.commit()
        self._stats["instrument_refresh_count"] += 1
        logger.info("refresh_instruments: upserted %d contracts across %s.",
                    len(rows), ", ".join(COLLECTOR.underlyings))

    # -- ticker polling -----------------------------------------------------

    def poll_tickers(self) -> None:
        """
        Polls get_ticker() for every currently-tracked instrument and appends
        a row per instrument to `market_data`. Never expires/filters out
        instruments here based on expiry_timestamp -- keeping a settled
        contract's last known state is useful for backtest gap analysis, and
        refresh_instruments() is the only place instrument tracking changes.
        """
        if not self._tracked_instruments:
            logger.warning("poll_tickers: no tracked instruments yet -- call refresh_instruments() first.")
            return

        now = datetime.now(timezone.utc)
        rows = []
        success, failure = 0, 0

        for instrument_id in list(self._tracked_instruments.keys()):
            self._stats["ticker_poll_count"] += 1
            ticker = self._with_retries(
                f"get_ticker({instrument_id})",
                self._adapter.get_ticker,
                instrument_id=instrument_id,
            )
            if ticker is None:
                failure += 1
                continue

            snap: MarketSnapshot = ticker.snapshot
            rows.append(
                (
                    snap.timestamp.isoformat(), snap.exchange, snap.instrument_id,
                    str(snap.best_bid) if snap.best_bid is not None else None,
                    str(snap.best_ask) if snap.best_ask is not None else None,
                    str(snap.bid_size) if snap.bid_size is not None else None,
                    str(snap.ask_size) if snap.ask_size is not None else None,
                    str(snap.mark_price) if snap.mark_price is not None else None,
                    str(snap.index_price) if snap.index_price is not None else None,
                    str(snap.iv) if snap.iv is not None else None,
                    str(snap.delta) if snap.delta is not None else None,
                    str(snap.gamma) if snap.gamma is not None else None,
                    str(snap.theta) if snap.theta is not None else None,
                    str(snap.vega) if snap.vega is not None else None,
                    str(snap.open_interest) if snap.open_interest is not None else None,
                    str(snap.volume_24h) if snap.volume_24h is not None else None,
                    now.isoformat(),
                )
            )
            success += 1
            if COLLECTOR.request_throttle_sec > 0:
                time.sleep(COLLECTOR.request_throttle_sec)

        if rows:
            self._conn.executemany(
                """
                INSERT INTO market_data (
                    ts, exchange, instrument_id, best_bid, best_ask, bid_size, ask_size,
                    mark_price, index_price, iv, delta, gamma, theta, vega,
                    open_interest, volume_24h, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._conn.commit()

        self._stats["ticker_success_count"] += success
        self._stats["ticker_failure_count"] += failure
        logger.info("poll_tickers: %d succeeded, %d failed out of %d tracked instruments.",
                    success, failure, len(self._tracked_instruments))

    # -- main loop --------------------------------------------------------

    def run_forever(self, duration_hours: float | None = None) -> None:
        """
        Runs the collector loop until stopped (SIGINT/SIGTERM) or, if
        duration_hours is given, until that much time has elapsed -- useful
        for a bounded 24h validation run rather than a truly infinite
        unattended process.
        """
        shutdown = GracefulShutdown()
        start_time = time.monotonic()
        self._stats["started_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            "Starting collector: underlyings=%s instrument_refresh=%ds ticker_poll=%ds duration_hours=%s",
            COLLECTOR.underlyings, COLLECTOR.instrument_refresh_interval_sec,
            COLLECTOR.ticker_poll_interval_sec, duration_hours,
        )

        self.refresh_instruments()
        last_instrument_refresh = time.monotonic()

        while not shutdown.should_stop:
            if duration_hours is not None and (time.monotonic() - start_time) > duration_hours * 3600:
                logger.info("Reached configured duration_hours=%.2f -- stopping.", duration_hours)
                break

            if (time.monotonic() - last_instrument_refresh) >= COLLECTOR.instrument_refresh_interval_sec:
                self.refresh_instruments()
                last_instrument_refresh = time.monotonic()

            self.poll_tickers()

            for _ in range(int(COLLECTOR.ticker_poll_interval_sec)):
                if shutdown.should_stop:
                    break
                time.sleep(1)

        logger.info("Collector stopped. Final stats: %s", self._stats)
        self.close()
