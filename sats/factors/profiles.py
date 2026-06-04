from __future__ import annotations

from dataclasses import dataclass


DEFAULT_FACTOR_PROFILE = "balanced"


@dataclass(frozen=True, slots=True)
class FactorProfile:
    name: str
    display_name: str
    factor_ids: tuple[str, ...]
    description: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "factor_ids": list(self.factor_ids),
            "description": self.description,
        }


FACTOR_PROFILES: dict[str, FactorProfile] = {
    "balanced": FactorProfile(
        name="balanced",
        display_name="平衡画像",
        factor_ids=(
            "barra_style_value",
            "barra_style_quality",
            "barra_style_momentum",
            "barra_style_liquidity",
            "barra_style_crowding_proxy",
        ),
        description="价值、质量、中期动量、流动性和拥挤度的平衡组合。",
    ),
    "short_term": FactorProfile(
        name="short_term",
        display_name="短线量价",
        factor_ids=(
            "gtja191_001",
            "gtja191_018",
            "gtja191_020",
            "alpha101_001",
            "barra_style_short_momentum",
        ),
        description="偏短周期量价、反转/动量和短动量。",
    ),
    "fundamental_quality": FactorProfile(
        name="fundamental_quality",
        display_name="基本面稳健",
        factor_ids=(
            "barra_style_value",
            "barra_style_quality",
            "barra_style_low_size",
            "barra_style_earnings_yield",
            "barra_style_book_to_price",
        ),
        description="低估值、盈利收益、账面价值、质量和小市值风格。",
    ),
}

FACTOR_PROFILE_CHOICES = tuple(FACTOR_PROFILES)


def get_factor_profile(name: str | None = None) -> FactorProfile:
    profile_name = str(name or DEFAULT_FACTOR_PROFILE).strip() or DEFAULT_FACTOR_PROFILE
    try:
        return FACTOR_PROFILES[profile_name]
    except KeyError as exc:
        known = ", ".join(FACTOR_PROFILE_CHOICES)
        raise ValueError(f"unknown factor profile: {profile_name}; known profiles: {known}") from exc


def resolve_factor_ids(*, profile: str | None = None, factor_ids: list[str] | tuple[str, ...] | None = None) -> list[str]:
    explicit = [str(item).strip() for item in (factor_ids or []) if str(item).strip()]
    if explicit:
        return explicit
    return list(get_factor_profile(profile).factor_ids)
