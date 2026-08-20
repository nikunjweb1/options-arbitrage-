"""
CLI entry point for the real-time (WebSocket-driven) collector.

Usage:
    python -m collectors.run_realtime
    python -m collectors.run_realtime --duration-hours 24
    python -m collectors.run_realtime --flush-interval 0.5

Requires config/.env with real Delta testnet credentials, same as
collectors/run.py.
"""

from __future__ import annotations

import argparse
import logging
import sys

from collectors.realtime_collector import RealtimeCollector, _configure_logging, _MAX_FLUSH_INTERVAL_SEC
from config.settings import DELTA
from exchange_adapters.delta import DeltaAdapter

logger = logging.getLogger("collector.run_realtime")


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-time WebSocket-driven market-data collector")
    parser.add_argument("--duration-hours", type=float, default=None,
                         help="Stop automatically after this many hours (omit to run until Ctrl+C).")
    parser.add_argument("--flush-interval", type=float, default=0.5,
                         help=f"Seconds between DB flushes. Must be <= {_MAX_FLUSH_INTERVAL_SEC} (default: 0.5).")
    args = parser.parse_args()

    _configure_logging()

    if args.flush_interval > _MAX_FLUSH_INTERVAL_SEC:
        logger.error(
            "--flush-interval %.2f exceeds the hard ceiling of %.1fs. "
            "This is enforced in code, not adjustable past that point.",
            args.flush_interval, _MAX_FLUSH_INTERVAL_SEC,
        )
        return 1

    if not DELTA.use_testnet:
        logger.error(
            "DELTA_USE_TESTNET is false. Refusing to start against production "
            "by default -- see collectors/run.py for the same guard."
        )
        return 1

    if not (DELTA.api_key and DELTA.api_secret):
        logger.error(
            "DELTA_API_KEY / DELTA_API_SECRET not set. Copy config/.env.example "
            "to config/.env and fill in testnet credentials first."
        )
        return 1

    adapter = DeltaAdapter()
    collector = RealtimeCollector(adapter=adapter, flush_interval_sec=args.flush_interval)
    collector.run_forever(duration_hours=args.duration_hours)
    return 0


if __name__ == "__main__":
    sys.exit(main())
