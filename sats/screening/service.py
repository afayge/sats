from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sats.screening.base import ScreeningInput, ScreeningResult
from sats.screening.registry import get_rule
from sats.storage.duckdb import DuckDBStorage


def evaluate_inputs(
    inputs: Iterable[ScreeningInput],
    *,
    rule_name: str,
    progress: Any | None = None,
) -> list[ScreeningResult]:
    rule = get_rule(rule_name)
    items = rule.prepare_inputs(list(inputs))
    if progress is None:
        return [rule.evaluate(item) for item in items]
    results = []
    with progress.step("规则计算", total=len(items)) as step:
        for index, item in enumerate(items, start=1):
            results.append(rule.evaluate(item))
            step.update(index)
    return results


def evaluate_and_store(
    inputs: Iterable[ScreeningInput],
    *,
    rule_name: str,
    storage: DuckDBStorage,
    progress: Any | None = None,
) -> list[ScreeningResult]:
    results = evaluate_inputs(inputs, rule_name=rule_name, progress=progress)
    if progress is None:
        storage.upsert_screening_results(results)
    else:
        with progress.step("写入筛选结果", total=1) as step:
            storage.upsert_screening_results(results)
            step.update(1)
    return results
