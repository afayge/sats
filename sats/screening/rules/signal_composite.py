from __future__ import annotations

from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule
from sats.signals import SignalInput, screening_result_from_signal_input


class SignalCompositeRule(ScreeningRule):
    name = "signal_composite"

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        return screening_result_from_signal_input(
            SignalInput(
                ts_code=data.ts_code,
                trade_date=data.trade_date,
                daily=data.daily,
                stock_basic=data.stock_basic,
                metadata=data.metadata,
            ),
            selected_signals="all",
        )
