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
        if self.use_testnet:
            return "wss://testnet-socket.india.delta.exchange"
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


@dataclass(frozen=True)
class DBConfig:
    sqlite_path: Path = Path(os.getenv("SQLITE_PATH", "data/options_arb.db"))


DELTA = DeltaConfig()
RISK = RiskLimits()
DB = DBConfig()
