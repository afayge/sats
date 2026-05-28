__all__ = [
    "ChanCompositeRule",
    "ChanSignalsRule",
    "ChanThirdBuyRule",
    "MaVolumeRelativeStrengthRule",
    "PriceVolumeMaRule",
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
    if name == "MaVolumeRelativeStrengthRule":
        from sats.screening.rules.ma_volume_relative_strength import MaVolumeRelativeStrengthRule

        return MaVolumeRelativeStrengthRule
    if name == "PriceVolumeMaRule":
        from sats.screening.rules.price_volume_ma import PriceVolumeMaRule

        return PriceVolumeMaRule
    raise AttributeError(name)
