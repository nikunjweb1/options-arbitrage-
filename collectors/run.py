"""
CLI entry point for the Phase 2 market-data collector.

Usage:
    # Run indefinitely (stop with Ctrl+C)
    python -m collectors.run

    # Run for a bounded 24h validation window, per the Phase 2 exit criterion
    python -m collectors.run --duration-hours 24

    # Single pass, useful for smoke-testing the wiring before a long run
    python -m collectors.run --once

Requires config/.env to be set up (see config/.env.example) with real Delta
testnet credentials -- copy the example and fill it in before running this.
"""

from __future__ import annotations

import argparse
import logging
import sys

from collectors.market_data_collector import MarketDataCollector, _configure_logging
from config.settings import DELTA
from exchange_adapters.delta import DeltaAdapter

logger = logging.getLogger("collector.run")


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 market-data collector")
    parser.add_argument(
        "--duration-hours", type=float, default=None,
        help="Stop automatically after this many hours (omit to run until Ctrl+C).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single refresh+poll cycle and exit -- for smoke-testing wiring.",
    )
    args = parser.parse_args()

    _configure_logging()

    if not DELTA.use_testnet:
        logger.error(
            "DELTA_USE_TESTNET is false. Refusing to start a long-running "
            "collector against production by default -- this script is meant "
            "for Phase 2 validation. If you really mean to collect from "
            "production, that's a deliberate decision to make explicitly in "
            "code, not something this CLI should do silently."
        )
        return 1

    if not (DELTA.api_key and DELTA.api_secret):
        logger.error(
            "DELTA_API_KEY / DELTA_API_SECRET not set. Copy config/.env.example "
            "to config/.env and fill in testnet credentials first."
        )
        return 1

    adapter = DeltaAdapter()
    collector = MarketDataCollector(adapter=adapter)

    if args.once:
        logger.info("Running single collector cycle (--once).")
        collector.refresh_instruments()
        collector.poll_tickers()
        collector.close()
        logger.info("Single cycle complete.")
        return 0

    collector.run_forever(duration_hours=args.duration_hours)
    return 0


if __name__ == "__main__":
    sys.exit(main())
