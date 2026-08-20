"""
Real-time (WebSocket-driven) market-data collector.

This is the fast path: DeltaWebSocketClient pushes MarketSnapshot objects as
they arrive (sub-second, driven by the exchange's own update rate, not a
polling interval), and this collector buffers them in memory and flushes to
SQLite on a short timer -- default 0.5s, hard-configured below 1s per the
"maximum 1 second, not more than that" requirement.

Relationship to collectors/market_data_collector.py (the REST-polling
collector built earlier):
  - Instrument discovery (which contracts exist, their specs) still goes
    through REST -- get_option_chain()/get_instruments() -- because that
    doesn't need to be real-time and Delta's WS feed doesn't replace the
    contract-specification lookups Section C's matching engine needs.
  - Ticker/price data now goes through this WebSocket path instead of REST
    polling. The REST poll_tickers() method in MarketDataCollector still
    exists and still works (e.g. as a fallback, or for a one-off --once
    smoke test), but the default collection mode is this one.

Flush latency budget: SQLite executemany() for a batch of a few hundred rows
is sub-millisecond on local disk; the dominant term is the flush *interval*
itself, which is hard-capped below 1s here. Actual end-to-end latency from
"exchange sends ticker update" to "row committed in market_data" is
therefore bounded by (network + parse time) + flush_interval_sec, and with
flush_interval_sec=0.5 that comfortably meets the 1s requirement with margin
for jitter.
"""

from __future__ import annotations

import logging
import queue
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import COLLECTOR, DB
from exchange_adapters.delta import DeltaAdapter
from exchange_adapters.delta_ws import DeltaWebSocketClient
from normalization.schemas import MarketSnapshot, OptionContract

logger = logging.getLogger("realtime_collector")

# Hard ceiling per explicit requirement: never let the flush interval exceed
# 1 second. This is intentionally not read from an environment variable --
# unlike most tunables in this repo, "speed is the main factor, max 1 sec"
# was an explicit instruction, so it's enforced in code rather than left
# adjustable to something slower by a config typo.
_MAX_FLUSH_INTERVAL_SEC = 1.0
_DEFAULT_FLUSH_INTERVAL_SEC = 0.5


def _configure_logging() -> None:
    COLLECTOR.log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(COLLECTOR.log_path), logging.StreamHandler()],
    )


class GracefulShutdown:
    def __init__(self) -> None:
        self.should_stop = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum: int, frame: Any) -> None:
        logger.info("Received shutdown signal (%s) -- flushing and stopping.", signum)
        self.should_stop = True


class RealtimeCollector:
    def __init__(
        self,
        adapter: DeltaAdapter,
        db_path: Path | None = None,
        flush_interval_sec: float = _DEFAULT_FLUSH_INTERVAL_SEC,
    ) -> None:
        if flush_interval_sec > _MAX_FLUSH_INTERVAL_SEC:
            raise ValueError(
                f"flush_interval_sec={flush_interval_sec} exceeds the hard "
                f"ceiling of {_MAX_FLUSH_INTERVAL_SEC}s. Speed is a hard "
                f"requirement here, not a tunable default -- see module docstring."
            )

        self._adapter = adapter
        self._db_path = db_path or DB.sqlite_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON;")
        # WAL mode: readers (e.g. gap_report.py running concurrently) don't
        # block the writer, and the writer doesn't block on readers either --
        # important for a sub-second flush loop that must not stall.
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")

        self._flush_interval_sec = flush_interval_sec
        self._queue: queue.Queue[MarketSnapshot] = queue.Queue()
        self._tracked_instruments: dict[str, OptionContract] = {}

        self._ws_client = DeltaWebSocketClient(on_snapshot=self._queue.put)

        self._stats = {
            "snapshots_received": 0,
            "snapshots_written": 0,
            "flush_cycles": 0,
            "max_observed_flush_latency_sec": 0.0,
            "started_at": None,
        }

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    # -- instrument discovery (REST, infrequent) -----------------------------

    def discover_and_subscribe(self) -> None:
        """
        Pulls the option chain via REST (as before), upserts into
        `instruments`, and subscribes the WebSocket client to every
        instrument's symbol so real-time ticks start flowing.
        """
        all_contracts: list[OptionContract] = []
        for underlying in COLLECTOR.underlyings:
            contracts = self._adapter.get_option_chain(underlying=underlying)
            if COLLECTOR.max_instruments_per_underlying > 0:
                contracts = contracts[: COLLECTOR.max_instruments_per_underlying]
            all_contracts.extend(contracts)

        if not all_contracts:
            logger.warning("discover_and_subscribe: zero contracts fetched.")
            return

        now = datetime.now(timezone.utc).isoformat()
        rows = []
        symbols = []
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
            symbols.append(c.contract_symbol)

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
        logger.info("discover_and_subscribe: upserted %d contracts, subscribing WS to %d symbols.",
                    len(rows), len(symbols))

        self._ws_client.subscribe(symbols)

    # -- flush loop -----------------------------------------------------------

    def _drain_queue(self, max_items: int = 5000) -> list[MarketSnapshot]:
        batch: list[MarketSnapshot] = []
        for _ in range(max_items):
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _write_batch(self, batch: list[MarketSnapshot]) -> None:
        if not batch:
            return
        now = datetime.now(timezone.utc).isoformat()
        rows = [
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
                now,
            )
            for snap in batch
        ]
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
        self._stats["snapshots_written"] += len(rows)

    def run_forever(self, duration_hours: float | None = None) -> None:
        shutdown = GracefulShutdown()
        start_time = time.monotonic()
        self._stats["started_at"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            "Starting realtime collector: underlyings=%s flush_interval=%.2fs (ceiling=%.1fs) duration_hours=%s",
            COLLECTOR.underlyings, self._flush_interval_sec, _MAX_FLUSH_INTERVAL_SEC, duration_hours,
        )

        self._ws_client.start()
        connected = self._ws_client.wait_until_connected(timeout_sec=15.0)
        if not connected:
            logger.error("WebSocket did not connect within 15s -- aborting startup.")
            self._ws_client.stop()
            self.close()
            return

        self.discover_and_subscribe()
        last_instrument_refresh = time.monotonic()

        while not shutdown.should_stop:
            if duration_hours is not None and (time.monotonic() - start_time) > duration_hours * 3600:
                logger.info("Reached configured duration_hours=%.2f -- stopping.", duration_hours)
                break

            if (time.monotonic() - last_instrument_refresh) >= COLLECTOR.instrument_refresh_interval_sec:
                self.discover_and_subscribe()
                last_instrument_refresh = time.monotonic()

            cycle_start = time.monotonic()
            batch = self._drain_queue()
            self._stats["snapshots_received"] += len(batch)
            self._write_batch(batch)
            self._stats["flush_cycles"] += 1

            flush_latency = time.monotonic() - cycle_start
            self._stats["max_observed_flush_latency_sec"] = max(
                self._stats["max_observed_flush_latency_sec"], flush_latency
            )
            if flush_latency > _MAX_FLUSH_INTERVAL_SEC:
                logger.warning(
                    "Flush cycle took %.2fs, exceeding the %.1fs ceiling -- "
                    "investigate disk/DB contention if this recurs.",
                    flush_latency, _MAX_FLUSH_INTERVAL_SEC,
                )

            sleep_remaining = self._flush_interval_sec - flush_latency
            if sleep_remaining > 0:
                time.sleep(sleep_remaining)

        logger.info("Realtime collector stopping. Final stats: %s", self._stats)
        self._ws_client.stop()
        self.close()
