__all__ = [
    "ChanCompositeRule",
    "ChanSignalsRule",
    "ChanThirdBuyRule",
    "HighTightFlagRule",
    "LimitUpShakeoutRule",
    "MaVolumeRule",
    "MaVolumeRelativeStrengthRule",
    "PriceVolumeMaRule",
    "RpsBreakoutRule",
    "TurtleTradeRule",
    "UptrendLimitDownRule",
]


def __getattr__(name: str):
    if name == "ChanCompositeRule":
        from sats.screening.rules.chan_composite import ChanCompositeRule

        return ChanCompositeRule
    if name == "ChanSignalsRule":
        from sats.screening.rules.chan_signals import ChanSignalsRule

        return ChanSignalsRule
    if name == "ChanThirdBuyRule":
        from sats.screening.rules.chan_third_buy import ChanThirdBuyRule

        return ChanThirdBuyRule
    if name == "HighTightFlagRule":
        from sats.screening.rules.sequoia_x import HighTightFlagRule

        return HighTightFlagRule
    if name == "LimitUpShakeoutRule":
        from sats.screening.rules.sequoia_x import LimitUpShakeoutRule

        return LimitUpShakeoutRule
    if name == "MaVolumeRule":
        from sats.screening.rules.sequoia_x import MaVolumeRule

        return MaVolumeRule
    if name == "MaVolumeRelativeStrengthRule":
        from sats.screening.rules.ma_volume_relative_strength import MaVolumeRelativeStrengthRule

        return MaVolumeRelativeStrengthRule
    if name == "PriceVolumeMaRule":
        from sats.screening.rules.price_volume_ma import PriceVolumeMaRule

        return PriceVolumeMaRule
    if name == "RpsBreakoutRule":
        from sats.screening.rules.sequoia_x import RpsBreakoutRule

        return RpsBreakoutRule
    if name == "TurtleTradeRule":
        from sats.screening.rules.sequoia_x import TurtleTradeRule

        return TurtleTradeRule
    if name == "UptrendLimitDownRule":
        from sats.screening.rules.sequoia_x import UptrendLimitDownRule

        return UptrendLimitDownRule
    raise AttributeError(name)
