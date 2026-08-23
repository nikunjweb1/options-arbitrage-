"""
Central configuration for the cross-exchange options arbitrage system.

SAFETY-CRITICAL: LIVE_TRADING defaults to False and there is no environment
variable, CLI flag, or code path anywhere in this module that flips it to True
automatically. Enabling live trading is a manual, explicit, one-line code change
that must be reviewed and approved outside of normal config loading -- it is
deliberately NOT read from .env, so a leaked/misconfigured .env file can never
turn live trading on by accident.

See docs/architecture.md Section 29 for the rationale.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)


class Mode(str, Enum):
    SCAN_ONLY = "SCAN_ONLY"
    PAPER = "PAPER"
    LIVE = "LIVE"


# ---------------------------------------------------------------------------
# HARD SAFETY DEFAULT -- do not wire this to an environment variable.
# Flipping this requires a deliberate code change and review, not a config edit.
# ---------------------------------------------------------------------------
LIVE_TRADING: bool = False

# Mode IS environment-driven, but LIVE mode still requires LIVE_TRADING=True
# above to actually place orders -- Mode alone can never enable live trading.
_MODE_RAW = os.getenv("APP_MODE", "SCAN_ONLY").upper()
try:
    MODE: Mode = Mode(_MODE_RAW)
except ValueError:
    raise ValueError(
        f"Invalid APP_MODE '{_MODE_RAW}' in environment. "
        f"Must be one of: {', '.join(m.value for m in Mode)}"
    )

if MODE == Mode.LIVE and not LIVE_TRADING:
    raise RuntimeError(
        "APP_MODE=LIVE was requested but LIVE_TRADING is hardcoded to False "
        "in config/settings.py. This is intentional. Live trading requires an "
        "explicit code change (not a config change) and separate sign-off. "
        "See docs/architecture.md Section 29."
    )


@dataclass(frozen=True)
class DeltaConfig:
    """Delta Exchange India connection settings. Testnet by default."""

    use_testnet: bool = os.getenv("DELTA_USE_TESTNET", "true").lower() == "true"
    api_key: str = field(default_factory=lambda: os.getenv("DELTA_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("DELTA_API_SECRET", ""))

    @property
    def rest_base_url(self) -> str:
        if self.use_testnet:
            return "https://cdn-ind.testnet.deltaex.org"
        return "https://api.india.delta.exchange"

    @property
    def ws_base_url(self) -> str:
        # Fixed 2026-08-21: the previous testnet value
        # ("wss://testnet-socket.india.delta.exchange") does not resolve --
        # confirmed via getaddrinfo failure on a live testnet run, then
        # confirmed the replacement below via a direct successful
        # websocket.create_connection() against it (unlike the old value,
        # which was carried over from Phase 1 research and never actually
        # dialed). This host also matches the same domain family
        # (deltaex.org) that the REST testnet URL above already uses,
        # unlike the old value which mixed in delta.exchange -- that
        # mismatch itself was a signal worth catching earlier.
        if self.use_testnet:
            return "wss://socket-ind.testnet.deltaex.org"
        return "wss://socket.india.delta.exchange"


@dataclass(frozen=True)
class RiskLimits:
    """
    Hard limits per docs/architecture.md Section H / master-prompt Section 24.
    These are read at startup; the risk engine (Phase 9, not yet built) is
    responsible for actually enforcing them. Their presence here does not by
    itself constitute enforcement -- Phase 2/3/4 code must not skip this.
    """

    max_total_capital: float = float(os.getenv("MAX_TOTAL_CAPITAL", "0"))
    max_margin_per_trade: float = float(os.getenv("MAX_MARGIN_PER_TRADE", "0"))
    max_open_trades: int = int(os.getenv("MAX_OPEN_TRADES", "0"))
    max_daily_loss: float = float(os.getenv("MAX_DAILY_LOSS", "0"))
    min_liquidity: float = float(os.getenv("MIN_LIQUIDITY", "0"))
    min_expected_profit: float = float(os.getenv("MIN_EXPECTED_PROFIT", "0"))

    # Added 2026-08-23 per docs/architecture.md Section M.2: a candidate whose
    # net_entry_cost (== gross entry credit after fees) is at or below this
    # value is a hard DO_NOT_ENTER, not merely a lower-ranked candidate. The
    # video-derived math this section documents shows a net-debit entry can
    # lose its FULL debit in the worst-case (high-momentum) scenario, while a
    # net-credit entry is worst-case bounded at the credit received -- these
    # are not symmetric risks, so "rank lower" is not an adequate treatment.
    # Defaults to 0.0 (the strict mathematical floor: any real credit, however
    # thin, changes the worst-case shape). Set MIN_NET_CREDIT above 0 via env
    # to add an explicit safety margin once real fee/slippage variance is
    # better understood (Section M.2 calls for "a safety margin" without
    # pinning a specific number yet -- that's a deliberate choice to leave
    # tunable here rather than guess a margin with no empirical basis).
    min_net_credit: float = float(os.getenv("MIN_NET_CREDIT", "0"))


@dataclass(frozen=True)
class DBConfig:
    sqlite_path: Path = Path(os.getenv("SQLITE_PATH", "data/options_arb.db"))


@dataclass(frozen=True)
class CollectorConfig:
    """
    Phase 2 market-data collector settings. See collectors/market_data_collector.py.

    Defaults are conservative on purpose: Delta's documented rate limit is
    500 ops/sec/product, which is nowhere near a binding constraint at these
    intervals, but a continuous 24h+ unattended collector should still be
    polite to a shared testnet rather than push limits just because it can.
    """

    # Comma-separated underlyings to track, e.g. "BTC,ETH"
    underlyings: tuple[str, ...] = tuple(
        u.strip().upper() for u in os.getenv("COLLECT_UNDERLYINGS", "BTC").split(",") if u.strip()
    )

    # How often to re-pull the full instrument list (new strikes/maturities
    # get added intraday per architecture.md Section E.3).
    instrument_refresh_interval_sec: int = int(
        os.getenv("INSTRUMENT_REFRESH_INTERVAL_SEC", str(5 * 60))
    )

    # How often to poll tickers for every tracked instrument.
    ticker_poll_interval_sec: int = int(os.getenv("TICKER_POLL_INTERVAL_SEC", "30"))

    # Small delay between individual per-instrument ticker requests within a
    # single poll pass, to avoid bursting the API even though the documented
    # limit is generous.
    request_throttle_sec: float = float(os.getenv("REQUEST_THROTTLE_SEC", "0.05"))

    # Safety cap on how many instruments get polled per underlying per pass,
    # in case an underlying's chain is unexpectedly huge. 0 = no cap.
    max_instruments_per_underlying: int = int(
        os.getenv("MAX_INSTRUMENTS_PER_UNDERLYING", "0")
    )

    # Retry behavior for transient network/API errors during collection.
    max_retries_per_call: int = int(os.getenv("COLLECTOR_MAX_RETRIES", "3"))
    retry_backoff_base_sec: float = float(os.getenv("COLLECTOR_RETRY_BACKOFF_SEC", "1.0"))

    log_path: Path = Path(os.getenv("COLLECTOR_LOG_PATH", "logs/collector.log"))


DELTA = DeltaConfig()
RISK = RiskLimits()
DB = DBConfig()
COLLECTOR = CollectorConfig()
