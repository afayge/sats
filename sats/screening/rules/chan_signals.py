from __future__ import annotations

from sats.chan.engine import evaluate_chan_signals
from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule
from sats.screening.rules.chan_third_buy import _latest_trade_date, _prepare_daily


class ChanSignalsRule(ScreeningRule):
    name = "chan_signals"

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        signals = evaluate_chan_signals(data)
        passed_signals = [signal for signal in signals if signal.passed]
        buy_signals = [signal for signal in passed_signals if signal.side == "buy"]
        sell_signals = [signal for signal in passed_signals if signal.side == "sell"]
        conflict_flags = []
        if buy_signals and sell_signals:
            conflict_flags.append("buy_sell_signal_conflict")

        daily = _prepare_daily(data.daily, trade_date=data.trade_date)
        metrics = {
            "data_source": data.metadata.get("data_source", "unknown"),
            "daily_basic_source": data.metadata.get("daily_basic_source", ""),
            "minute_30m_source": data.metadata.get("minute_30m_source", ""),
            "daily_rows": len(daily),
            "latest_daily_trade_date": _latest_trade_date(daily),
            "chan_daily_candidates": data.metadata.get("chan_daily_candidates", []),
            "chan_signals": [signal.to_dict() for signal in signals],
            "matched_chan_rules": [signal.label for signal in passed_signals],
            "matched_chan_rule_names": [signal.signal_name for signal in passed_signals],
            "chan_signal_labels": [signal.label for signal in passed_signals],
            "chan_signal_sides": {signal.signal_name: signal.side for signal in passed_signals},
            "conflict_flags": conflict_flags,
            "watch_levels": {
                signal.signal_name: signal.watch_levels for signal in passed_signals if signal.watch_levels
            },
            "risk_flags": _risk_flags(signals, conflict_flags),
            "evidence_refs": _evidence_refs(passed_signals),
        }
        failed = [signal.signal_name for signal in signals if not signal.passed]
        return ScreeningResult(
            trade_date=data.trade_date,
            ts_code=data.ts_code,
            rule_name=self.name,
            passed=bool(passed_signals),
            score=_score(passed_signals, conflict_flags),
            matched_conditions=[signal.signal_name for signal in passed_signals],
            failed_conditions=[] if passed_signals else failed,
            metrics=metrics,
        )


def _score(passed_signals, conflict_flags: list[str]) -> float:
    if not passed_signals:
        return 0.0
    score = max(float(signal.score) for signal in passed_signals)
    score += max(0, len([signal for signal in passed_signals if signal.side in {"buy", "sell"}]) - 1) * 3.0
    if conflict_flags:
        score -= 12.0
    return round(max(0.0, min(score, 100.0)), 2)


def _risk_flags(signals, conflict_flags: list[str]) -> list[str]:
    flags = list(conflict_flags)
    seen = set(flags)
    for signal in signals:
        if signal.passed:
            continue
        for flag in signal.risk_flags[:2]:
            text = f"{signal.label}: {flag}"
            if text not in seen:
                seen.add(text)
                flags.append(text)
    return flags[:16]


def _evidence_refs(passed_signals) -> list[dict[str, object]]:
    refs = []
    seen = set()
    for signal in passed_signals:
        for ref in signal.evidence_refs:
            key = str(ref.get("rule_id"))
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    return refs[:8]
