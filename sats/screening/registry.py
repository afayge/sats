from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class ScreeningRuleMetadata:
    description: str
    semantic_tags: tuple[str, ...]
    condition_summary: str
    data_dependencies: tuple[str, ...] = ("日线 OHLCV", "stock_basic")


_RULE_METADATA: dict[str, ScreeningRuleMetadata] = {
    "chan_composite": ScreeningRuleMetadata("缠论一二三类买点综合筛选", ("缠论", "买点", "一买", "二买", "三买"), "缠论结构与多周期信号综合", ("日线 OHLCV", "分钟 K", "缠论信号")),
    "chan_signals": ScreeningRuleMetadata("缠论信号组合筛选", ("缠论信号", "缠论选股"), "注册缠论信号命中", ("日线 OHLCV", "分钟 K", "缠论信号")),
    "chan_third_buy": ScreeningRuleMetadata("缠论三买回踩确认筛选", ("缠论三买", "三买", "三买回踩"), "突破中枢后回踩不破并重新转强", ("日线 OHLCV", "分钟 K")),
    "high_tight_flag": ScreeningRuleMetadata("高位紧旗形整理筛选", ("高位紧旗形", "紧旗形", "high tight flag"), "强势上涨后的窄幅紧凑整理"),
    "limit_up_shakeout": ScreeningRuleMetadata("涨停后缩量洗盘筛选", ("涨停洗盘", "涨停后缩量", "limit up shakeout"), "涨停后回调缩量且结构未破"),
    "ma_volume": ScreeningRuleMetadata("均线金叉放量筛选", ("均线金叉放量", "ma volume"), "MA5 上穿 MA20 且成交量放大"),
    "ma_volume_relative_strength": ScreeningRuleMetadata("温和上涨、量价配合和完整均线多头筛选", ("均线量能相对强度", "ma volume relative strength"), "连续站上 MA5、四日三阳、量比与 MA5>MA10>MA20>MA60"),
    "monthly_base_breakout": ScreeningRuleMetadata("月线底部形态突破筛选", ("月线底部突破", "月线平台突破", "monthly base breakout"), "月线底部、颈线突破和月均线约束", ("日线 OHLCV", "月线 OHLCV", "stock_basic")),
    "price_volume_ma": ScreeningRuleMetadata("价格、成交量和均线综合筛选", ("量价均线", "price volume ma"), "量价确认、均线排列和扩展市场字段", ("日线 OHLCV", "daily_basic", "stock_basic")),
    "rps_breakout": ScreeningRuleMetadata("RPS 相对强度突破筛选", ("rps突破", "相对强度突破", "rps breakout"), "长期相对强度排名与价格突破"),
    "signal_composite": ScreeningRuleMetadata("注册技术信号组合筛选", ("信号组合", "综合信号", "signal composite"), "多个 SATS 技术信号综合"),
    "turtle_trade": ScreeningRuleMetadata("海龟通道突破筛选", ("海龟交易", "唐奇安突破", "turtle trade"), "唐奇安通道突破与波动率约束"),
    "uptrend_limit_down": ScreeningRuleMetadata("上升趋势中的跌停反转筛选", ("上升趋势跌停", "趋势跌停", "uptrend limit down"), "中期上升趋势、跌停与放量条件"),
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


def rule_metadata(name: str) -> ScreeningRuleMetadata:
    normalized = canonical_rule_name(name)
    metadata = _RULE_METADATA.get(normalized)
    if metadata is not None:
        return metadata
    return ScreeningRuleMetadata(
        description="用户确认后生成的声明式筛选规则",
        semantic_tags=(normalized,),
        condition_summary="条件见生成规则 spec",
    )


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
