from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from sats.backtesting.strategy_spec import StrategySpec, validate_strategy_spec
from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.storage.duckdb import DuckDBStorage


@dataclass(frozen=True, slots=True)
class BacktestResult:
    spec: StrategySpec
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    data_source: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "metrics": self.metrics,
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "data_source": self.data_source,
            "message": self.message,
        }


def run_strategy_backtest(
    spec: StrategySpec | dict[str, Any],
    *,
    settings: Settings,
    storage: DuckDBStorage | None = None,
    provider: AStockDataProvider | None = None,
) -> BacktestResult:
    clean_spec = validate_strategy_spec(spec)
    storage = storage or DuckDBStorage(settings.db_path)
    provider = provider or AStockDataProvider(settings)
    daily = provider.load_historical_daily_klines(
        list(clean_spec.symbols),
        start_date=clean_spec.start_date,
        end_date=clean_spec.end_date,
        storage=storage,
    )
    if daily.empty:
        raise ValueError("无法获取回测所需 A 股日线数据")
    source = str(daily.attrs.get("data_source") or "unknown")
    frame = _prepare_daily(daily, list(clean_spec.symbols), clean_spec.start_date, clean_spec.end_date)
    if frame.empty:
        raise ValueError("回测日线数据为空")
    result = _run_moving_average(clean_spec, frame)
    return BacktestResult(
        spec=clean_spec,
        metrics=result["metrics"],
        equity_curve=result["equity_curve"],
        trades=result["trades"],
        data_source=source,
        message="SATS-native 轻量研究回测完成；结果不构成投资建议。",
    )


def format_backtest_report(result: BacktestResult) -> str:
    metrics = result.metrics
    spec = result.spec
    lines = [
        f"# {spec.name} 回测报告",
        "",
        "## 策略",
        f"- 类型: {spec.strategy_type}",
        f"- 股票池: {', '.join(spec.symbols)}",
        f"- 区间: {spec.start_date} - {spec.end_date}",
        f"- 规则: close > MA{spec.short_window} 且 MA{spec.short_window} > MA{spec.long_window}",
        f"- top_n: {spec.top_n}",
        "",
        "## 指标",
        f"- 累计收益: {_pct(metrics.get('total_return'))}",
        f"- 年化收益: {_pct(metrics.get('annual_return'))}",
        f"- 最大回撤: {_pct(metrics.get('max_drawdown'))}",
        f"- 年化波动: {_pct(metrics.get('annual_volatility'))}",
        f"- 胜率: {_pct(metrics.get('win_rate'))}",
        f"- 换手次数: {metrics.get('turnover_count', 0)}",
        f"- 交易日数: {metrics.get('trading_days', 0)}",
        "",
        "## 边界",
        "- 该回测为 SATS-native 轻量研究回测，仅使用日线和受限策略 spec。",
        "- 不执行 LLM 生成 Python，不自动交易，不构成投资建议。",
    ]
    return "\n".join(lines)


def write_backtest_json(result: BacktestResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def _prepare_daily(daily: pd.DataFrame, symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    frame = daily.copy()
    required = {"ts_code", "trade_date", "close"}
    if not required.issubset(frame.columns):
        raise ValueError("daily data must include ts_code, trade_date and close")
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame[frame["ts_code"].isin(symbols)]
    frame = frame[(frame["trade_date"] >= str(start_date)) & (frame["trade_date"] <= str(end_date))]
    frame = frame.dropna(subset=["close"])
    frame = frame.sort_values(["trade_date", "ts_code"])
    return frame


def _run_moving_average(spec: StrategySpec, daily: pd.DataFrame) -> dict[str, Any]:
    close = daily.pivot(index="trade_date", columns="ts_code", values="close").sort_index()
    returns = close.pct_change().fillna(0.0)
    ma_short = close.rolling(spec.short_window, min_periods=spec.short_window).mean()
    ma_long = close.rolling(spec.long_window, min_periods=spec.long_window).mean()
    signal = (close > ma_short) & (ma_short > ma_long)
    score = (ma_short / ma_long - 1.0).where(signal)
    positions = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for trade_date in score.index:
        row = score.loc[trade_date].dropna().sort_values(ascending=False).head(spec.top_n)
        if not row.empty:
            positions.loc[trade_date, row.index] = 1.0 / len(row)
    shifted = positions.shift(1).fillna(0.0)
    turnover = shifted.diff().abs().sum(axis=1).fillna(0.0)
    gross = (shifted * returns).sum(axis=1)
    cost = turnover * ((spec.fee_bps + spec.slippage_bps) / 10000.0)
    net = gross - cost
    equity = (1.0 + net).cumprod()
    if equity.empty:
        raise ValueError("回测没有可计算的权益曲线")
    total_return = float(equity.iloc[-1] - 1.0)
    annual_return = float(equity.iloc[-1] ** (252 / max(1, len(equity))) - 1.0)
    max_drawdown = float((equity / equity.cummax() - 1.0).min())
    annual_volatility = float(net.std(ddof=0) * (252 ** 0.5)) if len(net) > 1 else 0.0
    win_rate = float((net > 0).sum() / max(1, (net != 0).sum()))
    trades = _position_changes(shifted)
    return {
        "metrics": {
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "annual_volatility": annual_volatility,
            "win_rate": win_rate,
            "turnover_count": int((turnover > 0).sum()),
            "trading_days": int(len(equity)),
            "final_nav": float(equity.iloc[-1]),
        },
        "equity_curve": [
            {"trade_date": str(idx), "nav": float(value), "return": float(net.loc[idx])}
            for idx, value in equity.items()
        ],
        "trades": trades,
    }


def _position_changes(positions: pd.DataFrame) -> list[dict[str, Any]]:
    changes = positions.diff().fillna(positions)
    rows: list[dict[str, Any]] = []
    for trade_date, row in changes.iterrows():
        changed = row[row.abs() > 1e-12]
        for symbol, weight_delta in changed.items():
            rows.append({"trade_date": str(trade_date), "ts_code": str(symbol), "weight_delta": float(weight_delta)})
    return rows[:500]


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "N/A"
