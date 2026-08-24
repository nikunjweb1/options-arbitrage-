"""
Central runtime configuration.

RECONSTRUCTED 2026-08-24: this file was found as an 11-byte placeholder stub
(literal contents "PLACEHOLDER") despite being imported by matching/run_matcher.py,
pricing/diagnose_pair.py, and (as of this commit) pricing/run_pricing.py -- all of
which were therefore non-functional. Rebuilt from those three files' actual import
usage (`from config.settings import DB`, `from config.settings import DB, DELTA`)
plus the documented env var contract in config/.env.example, which was NOT itself
a placeholder and is treated here as the source of truth for what belongs in this
module. If the other working session has a different real version of this file
locally, diff before assuming this reconstruction is authoritative -- the goal
here is "unblock the pipeline correctly", not "win a merge conflict".

Loads from config/.env if present (via python-dotenv, already a dependency per
requirements.txt), falling back to hardcoded defaults below -- never raises on
a missing .env file, since SCAN_ONLY mode should work with zero configuration.

Nothing in this file performs I/O beyond reading env vars and one dotenv file
at import time. No network calls, no DB connections opened here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

from normalization.schemas import FeeSchedule

# config/.env is gitignored (see config/.env.example's own header comment).
# Loading it here means every entry point (collectors, matcher, pricing,
# dashboard) gets consistent config without each one calling load_dotenv
# itself. Silent no-op if the file doesn't exist -- SCAN_ONLY/dry-run usage
# should never require real credentials to be present.
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_decimal(key: str, default: str) -> Decimal:
    val = os.environ.get(key, default)
    try:
        return Decimal(val)
    except Exception:
        return Decimal(default)


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


class AppMode(str, Enum):
    """Per config/.env.example: SCAN_ONLY | PAPER | LIVE. LIVE is refused at
    startup regardless of what's in .env -- see require_non_live() below.
    This is a deliberate hard gate, not a suggestion: nothing in this
    codebase should be able to place a real order just because an .env file
    says APP_MODE=LIVE. Flipping that on is a decision that needs to happen
    somewhere more deliberate than a config file, per architecture.md's
    explicit-approval principle for Phase 8+."""

    SCAN_ONLY = "SCAN_ONLY"
    PAPER = "PAPER"
    LIVE = "LIVE"


APP_MODE: AppMode = AppMode(_env_str("APP_MODE", "SCAN_ONLY"))


def require_non_live() -> None:
    """Call this at the top of any entry point that can place real orders.
    Raises rather than silently downgrading -- a script that wanted LIVE and
    got refused should stop, not quietly run in a different mode than the
    operator asked for."""
    if APP_MODE is AppMode.LIVE:
        raise RuntimeError(
            "APP_MODE=LIVE is refused at startup. This codebase has no path "
            "that is allowed to place real orders yet (see docs/architecture.md's "
            "explicit-approval principle for Phase 8+). If live trading is "
            "genuinely ready, that gate needs to be removed deliberately, in "
            "its own reviewed change -- not bypassed via a different .env value."
        )


@dataclass(frozen=True)
class DBConfig:
    sqlite_path: Path


DB = DBConfig(sqlite_path=Path(_env_str("SQLITE_PATH", "data/options_arb.db")))


@dataclass(frozen=True)
class DeltaConfig:
    """Delta Exchange India connection config + documented fee schedule.

    FEE SCHEDULE NOTE: the taker_fee_pct/settlement figures below are a
    conservative placeholder pending verification against Delta India's
    currently-published options fee schedule -- per normalization.schemas.
    FeeSchedule's own docstring ("as documented, never guessed") this
    should be replaced with the exact figures from Delta's fee page before
    this number is trusted for real net_entry_cost math, and source_url
    below should point at that exact page once verified. Flagging loudly
    here rather than presenting an unverified number as authoritative.
    """

    use_testnet: bool
    api_key: str
    api_secret: str
    fee_schedule: FeeSchedule = field(
        default_factory=lambda: FeeSchedule(
            exchange="delta_india",
            maker_fee_pct=Decimal("0.0003"),
            taker_fee_pct=Decimal("0.0006"),
            settlement_fee_pct=Decimal("0.0006"),
            fee_cap_pct_of_premium=Decimal("0.125"),
            zero_fee_on_otm_settlement=True,
            additional_tax_pct=Decimal("0.18"),  # 18% GST on Delta India accounts
            source_url="UNVERIFIED -- replace with exact Delta India fee-schedule URL before live use",
        )
    )

    @property
    def base_url(self) -> str:
        return (
            "https://cdn-ind.testnet.deltaex.org"
            if self.use_testnet
            else "https://api.india.delta.exchange"
        )


DELTA = DeltaConfig(
    use_testnet=_env_bool("DELTA_USE_TESTNET", True),
    api_key=_env_str("DELTA_API_KEY", ""),
    api_secret=_env_str("DELTA_API_SECRET", ""),
)


@dataclass(frozen=True)
class RiskLimits:
    """Presence here does not by itself constitute enforcement -- each of
    these must be explicitly wired into whatever code path it's meant to
    gate (see pricing/ev_engine.py's GAP #1 note on min_liquidity, and
    pricing/run_pricing.py's MIN_NET_CREDIT gate, both of which take these
    values as explicit constructor/CLI arguments rather than reading this
    dataclass implicitly deep inside business logic)."""

    max_total_capital: Decimal
    max_margin_per_trade: Decimal
    max_open_trades: int
    max_daily_loss: Decimal
    min_liquidity: Decimal
    min_expected_profit: Decimal
    # Per docs/architecture.md Section M.2: net_entry_cost must exceed this
    # to be entry_eligible. Not in config/.env.example yet (added alongside
    # the M.2 gate implementation) -- defaults to 0 (any genuine net credit
    # qualifies) until a real minimum is set via MIN_NET_CREDIT env var.
    min_net_credit: Decimal


RISK = RiskLimits(
    max_total_capital=_env_decimal("MAX_TOTAL_CAPITAL", "0"),
    max_margin_per_trade=_env_decimal("MAX_MARGIN_PER_TRADE", "0"),
    max_open_trades=_env_int("MAX_OPEN_TRADES", 0),
    max_daily_loss=_env_decimal("MAX_DAILY_LOSS", "0"),
    min_liquidity=_env_decimal("MIN_LIQUIDITY", "0"),
    min_expected_profit=_env_decimal("MIN_EXPECTED_PROFIT", "0"),
    min_net_credit=_env_decimal("MIN_NET_CREDIT", "0"),
)


@dataclass(frozen=True)
class CollectorConfig:
    underlyings: tuple[str, ...]
    instrument_refresh_interval_sec: int
    ticker_poll_interval_sec: int
    request_throttle_sec: float
    max_instruments_per_underlying: int
    max_retries: int
    retry_backoff_sec: float
    log_path: Path


COLLECTOR = CollectorConfig(
    underlyings=tuple(
        u.strip() for u in _env_str("COLLECT_UNDERLYINGS", "BTC").split(",") if u.strip()
    ),
    instrument_refresh_interval_sec=_env_int("INSTRUMENT_REFRESH_INTERVAL_SEC", 300),
    ticker_poll_interval_sec=_env_int("TICKER_POLL_INTERVAL_SEC", 30),
    request_throttle_sec=_env_float("REQUEST_THROTTLE_SEC", 0.05),
    max_instruments_per_underlying=_env_int("MAX_INSTRUMENTS_PER_UNDERLYING", 0),
    max_retries=_env_int("COLLECTOR_MAX_RETRIES", 3),
    retry_backoff_sec=_env_float("COLLECTOR_RETRY_BACKOFF_SEC", 1.0),
    log_path=Path(_env_str("COLLECTOR_LOG_PATH", "logs/collector.log")),
)

RUN_INTEGRATION_TESTS: bool = _env_bool("RUN_INTEGRATION_TESTS", False)
