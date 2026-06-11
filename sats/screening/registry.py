from __future__ import annotations

import importlib
import pkgutil

from sats.screening.base import ScreeningRule
from sats.screening.rules.chan_composite import ChanCompositeRule
from sats.screening.rules.chan_signals import ChanSignalsRule
from sats.screening.rules.chan_third_buy import ChanThirdBuyRule
from sats.screening.rules.ma_volume_relative_strength import MaVolumeRelativeStrengthRule
from sats.screening.rules.monthly_base_breakout import MonthlyBaseBreakoutRule
from sats.screening.rules.price_volume_ma import PriceVolumeMaRule
from sats.screening.rules.sequoia_x import (
    HighTightFlagRule,
    LimitUpShakeoutRule,
    MaVolumeRule,
    RpsBreakoutRule,
    TurtleTradeRule,
    UptrendLimitDownRule,
)
from sats.screening.rules.signal_composite import SignalCompositeRule

_RULES: dict[str, type[ScreeningRule]] = {
    ChanCompositeRule.name: ChanCompositeRule,
    ChanSignalsRule.name: ChanSignalsRule,
    ChanThirdBuyRule.name: ChanThirdBuyRule,
    HighTightFlagRule.name: HighTightFlagRule,
    LimitUpShakeoutRule.name: LimitUpShakeoutRule,
    MaVolumeRule.name: MaVolumeRule,
    MaVolumeRelativeStrengthRule.name: MaVolumeRelativeStrengthRule,
    MonthlyBaseBreakoutRule.name: MonthlyBaseBreakoutRule,
    PriceVolumeMaRule.name: PriceVolumeMaRule,
    RpsBreakoutRule.name: RpsBreakoutRule,
    SignalCompositeRule.name: SignalCompositeRule,
    TurtleTradeRule.name: TurtleTradeRule,
    UptrendLimitDownRule.name: UptrendLimitDownRule,
}

_ALIASES = {
    "HighTightFlag": HighTightFlagRule.name,
    "HighTightFlagStrategy": HighTightFlagRule.name,
    "LimitUpShakeout": LimitUpShakeoutRule.name,
    "LimitUpShakeoutStrategy": LimitUpShakeoutRule.name,
    "MaVolume": MaVolumeRule.name,
    "MaVolumeStrategy": MaVolumeRule.name,
    "RpsBreakout": RpsBreakoutRule.name,
    "RpsBreakoutStrategy": RpsBreakoutRule.name,
    "TurtleTrade": TurtleTradeRule.name,
    "TurtleTradeStrategy": TurtleTradeRule.name,
    "UptrendLimitDown": UptrendLimitDownRule.name,
    "UptrendLimitDownStrategy": UptrendLimitDownRule.name,
    "chan-composite": ChanCompositeRule.name,
    "chan-ai-select": ChanSignalsRule.name,
    "chan-signals": ChanSignalsRule.name,
    "chan-stock-select": ChanCompositeRule.name,
    "chan-third-buy": ChanThirdBuyRule.name,
    "high-tight-flag": HighTightFlagRule.name,
    "hightightflag": HighTightFlagRule.name,
    "limit-up-shakeout": LimitUpShakeoutRule.name,
    "limitupshakeout": LimitUpShakeoutRule.name,
    "ma-volume": MaVolumeRule.name,
    "mavolume": MaVolumeRule.name,
    "ma-volume-relative-strength": MaVolumeRelativeStrengthRule.name,
    "monthly-base-breakout": MonthlyBaseBreakoutRule.name,
    "price-volume-ma": PriceVolumeMaRule.name,
    "rps-breakout": RpsBreakoutRule.name,
    "rpsbreakout": RpsBreakoutRule.name,
    "signal-composite": SignalCompositeRule.name,
    "abu-signals": SignalCompositeRule.name,
    "turtle-trade": TurtleTradeRule.name,
    "turtletrade": TurtleTradeRule.name,
    "uptrend-limit-down": UptrendLimitDownRule.name,
    "uptrendlimitdown": UptrendLimitDownRule.name,
}


def list_rules() -> list[str]:
    return sorted(_all_rules())


def canonical_rule_name(name: str) -> str:
    value = str(name or "").strip()
    return _ALIASES.get(value, value.replace("-", "_"))


def get_rule(name: str) -> ScreeningRule:
    normalized = canonical_rule_name(name)
    rules = _all_rules()
    try:
        return rules[normalized]()
    except KeyError as exc:
        available = ", ".join(list_rules())
        raise ValueError(f"Unknown screening rule: {name}. Available: {available}") from exc


def _all_rules() -> dict[str, type[ScreeningRule]]:
    rules = dict(_RULES)
    for name, rule_cls in _generated_rules().items():
        rules.setdefault(name, rule_cls)
    return rules


def _generated_rules() -> dict[str, type[ScreeningRule]]:
    try:
        package = importlib.import_module("sats.screening.rules.generated")
    except ModuleNotFoundError:
        return {}
    result: dict[str, type[ScreeningRule]] = {}
    for item in pkgutil.iter_modules(getattr(package, "__path__", [])):
        if item.name.startswith("_"):
            continue
        module_name = f"{package.__name__}.{item.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for value in vars(module).values():
            if not isinstance(value, type) or value is ScreeningRule:
                continue
            if not issubclass(value, ScreeningRule):
                continue
            rule_name = str(getattr(value, "name", "") or "").strip()
            if rule_name:
                result[rule_name] = value
    return result
