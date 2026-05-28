from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule


def get_rule(name: str) -> ScreeningRule:
    from sats.screening.registry import get_rule as _get_rule

    return _get_rule(name)


def list_rules() -> list[str]:
    from sats.screening.registry import list_rules as _list_rules

    return _list_rules()

__all__ = ["ScreeningInput", "ScreeningResult", "ScreeningRule", "get_rule", "list_rules"]
