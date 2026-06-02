from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import (
    LICENSE_NOTE,
    SOURCE,
    benchmark_returns,
    close_returns,
    finite,
    require_panel,
    row_mean_returns,
)

__alpha_meta__ = {
    "id": "barra_style_beta",
    "display_name": "Barra-style Beta",
    "theme": ["beta", "volatility"],
    "formula_latex": "cov(stock_ret, benchmark_ret, 60) / var(benchmark_ret, 60)",
    "columns_required": ["close"],
    "extras_required": [],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "neutral",
    "decay_horizon": 60,
    "min_warmup_bars": 80,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Uses benchmark_close when available; otherwise degrades to cross-sectional mean return benchmark.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (close,) = require_panel(panel, "close")
    returns = close_returns(close)
    benchmark = benchmark_returns(panel)
    if benchmark is None:
        benchmark = row_mean_returns(close)
    bench = benchmark.iloc[:, 0]
    beta = returns.rolling(60, min_periods=40).cov(bench).div(
        bench.rolling(60, min_periods=40).var(),
        axis=0,
    )
    return finite(beta)
