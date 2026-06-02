from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, finite, require_panel, zscore

__alpha_meta__ = {
    "id": "barra_style_growth",
    "display_name": "Barra-style Growth",
    "theme": ["growth"],
    "formula_latex": "z(pct_change(revenue, 252)) + z(pct_change(net_profit, 252))",
    "columns_required": ["revenue", "net_profit"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 60,
    "min_warmup_bars": 252,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Daily panel approximation of recent report-period growth; requires revenue and net_profit histories.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    revenue, net_profit = require_panel(panel, "revenue", "net_profit")
    revenue_growth = revenue.pct_change(252, fill_method=None)
    profit_growth = net_profit.pct_change(252, fill_method=None)
    return finite(zscore(revenue_growth) + zscore(profit_growth))
