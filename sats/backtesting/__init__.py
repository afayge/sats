"""SATS-native lightweight research backtesting."""

from sats.backtesting.service import BacktestResult, run_strategy_backtest
from sats.backtesting.strategy_spec import StrategySpec, strategy_spec_from_request, validate_strategy_spec

__all__ = [
    "BacktestResult",
    "StrategySpec",
    "run_strategy_backtest",
    "strategy_spec_from_request",
    "validate_strategy_spec",
]
