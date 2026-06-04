from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from sats.stock_question import extract_trade_date
from sats.symbols import normalize_symbols


@dataclass(frozen=True, slots=True)
class StrategySpec:
    name: str
    strategy_type: str = "moving_average"
    symbols: tuple[str, ...] = ()
    start_date: str = ""
    end_date: str = ""
    short_window: int = 5
    long_window: int = 20
    top_n: int = 5
    fee_bps: float = 3.0
    slippage_bps: float = 2.0
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    notes: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbols"] = list(self.symbols)
        return payload


def strategy_spec_from_request(message: str, *, symbols: list[str] | tuple[str, ...] = ()) -> StrategySpec:
    text = str(message or "")
    clean_symbols = tuple(normalize_symbols(list(symbols) or _symbols_from_text(text), required=False))
    end_date = extract_trade_date(text) or _today_yyyymmdd()
    start_date = _date_days_before(end_date, _lookback_days(text))
    short_window, long_window = _moving_average_windows(text)
    return StrategySpec(
        name=_strategy_name(text),
        strategy_type="moving_average",
        symbols=clean_symbols or ("000001.SZ",),
        start_date=start_date,
        end_date=end_date,
        short_window=short_window,
        long_window=long_window,
        top_n=_top_n(text),
        fee_bps=_number_after(text, ("手续费", "fee"), default=3.0),
        slippage_bps=_number_after(text, ("滑点", "slippage"), default=2.0),
        stop_loss_pct=_optional_pct_after(text, ("止损", "stop loss")),
        take_profit_pct=_optional_pct_after(text, ("止盈", "take profit")),
        notes="SATS-native runtime 生成的受限策略 spec；仅用于研究回测，不构成投资建议。",
        meta={"source": "chat_runtime"},
    )


def validate_strategy_spec(spec: StrategySpec | Mapping[str, Any]) -> StrategySpec:
    if not isinstance(spec, StrategySpec):
        spec = StrategySpec(
            name=str(spec.get("name") or "SATS 策略"),
            strategy_type=str(spec.get("strategy_type") or "moving_average"),
            symbols=tuple(normalize_symbols(spec.get("symbols") if isinstance(spec.get("symbols"), list) else [], required=False)),
            start_date=str(spec.get("start_date") or ""),
            end_date=str(spec.get("end_date") or ""),
            short_window=int(spec.get("short_window") or 5),
            long_window=int(spec.get("long_window") or 20),
            top_n=int(spec.get("top_n") or 5),
            fee_bps=float(spec.get("fee_bps") or 0.0),
            slippage_bps=float(spec.get("slippage_bps") or 0.0),
            stop_loss_pct=_clean_optional_float(spec.get("stop_loss_pct")),
            take_profit_pct=_clean_optional_float(spec.get("take_profit_pct")),
            notes=str(spec.get("notes") or ""),
            meta=dict(spec.get("meta") or {}) if isinstance(spec.get("meta"), dict) else {},
        )
    if spec.strategy_type != "moving_average":
        raise ValueError("v1 backtest only supports moving_average strategy specs")
    if not spec.symbols:
        raise ValueError("strategy spec requires at least one A-share symbol")
    if spec.short_window <= 0 or spec.long_window <= 0:
        raise ValueError("moving average windows must be positive")
    if spec.short_window >= spec.long_window:
        raise ValueError("short_window must be smaller than long_window")
    if spec.top_n <= 0:
        raise ValueError("top_n must be positive")
    if spec.fee_bps < 0 or spec.slippage_bps < 0:
        raise ValueError("fee and slippage must be non-negative")
    if not spec.start_date or not spec.end_date:
        raise ValueError("strategy spec requires start_date and end_date")
    return spec


def strategy_draft_python(spec: StrategySpec) -> str:
    return (
        '"""Readable SATS strategy draft. This file is not executed by runtime v1."""\n\n'
        f"STRATEGY_SPEC = {spec.to_dict()!r}\n\n"
        "def describe():\n"
        "    return (\n"
        "        f\"{STRATEGY_SPEC['name']}: close > MA{STRATEGY_SPEC['short_window']} \"\n"
        "        f\"and MA{STRATEGY_SPEC['short_window']} > MA{STRATEGY_SPEC['long_window']}\"\n"
        "    )\n"
    )


def _symbols_from_text(text: str) -> list[str]:
    return re.findall(r"(?<!\d)(?:[036]\d{5})(?!\d)(?:\.(?:SH|SZ|BJ))?", text, flags=re.IGNORECASE)


def _strategy_name(text: str) -> str:
    if "均线" in text or "ma" in text.lower():
        return "均线研究策略"
    if "突破" in text:
        return "突破研究策略"
    return "SATS 研究策略"


def _moving_average_windows(text: str) -> tuple[int, int]:
    values = [int(item) for item in re.findall(r"(?<!\d)(\d{1,3})\s*(?:日|天)?\s*(?:均线|MA|ma)", text)]
    if len(values) >= 2:
        short, long = sorted(values[:2])
        return max(1, short), max(short + 1, long)
    return 5, 20


def _top_n(text: str) -> int:
    match = re.search(r"(?:top|前)\s*(\d{1,3})", text, flags=re.IGNORECASE)
    return max(1, min(200, int(match.group(1)))) if match else 5


def _lookback_days(text: str) -> int:
    match = re.search(r"近\s*(\d{1,4})\s*(?:天|日)", text)
    return max(30, min(1500, int(match.group(1)))) if match else 260


def _number_after(text: str, labels: tuple[str, ...], *, default: float) -> float:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*(\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return default


def _optional_pct_after(text: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*(\d+(?:\.\d+)?)\s*%?", text, flags=re.IGNORECASE)
        if match:
            value = float(match.group(1))
            return value / 100.0 if value > 1 else value
    return None


def _clean_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def _date_days_before(end_date: str, days: int) -> str:
    try:
        end = datetime.strptime(str(end_date), "%Y%m%d")
    except ValueError:
        end = datetime.now()
    return (end - timedelta(days=max(1, int(days)))).strftime("%Y%m%d")
