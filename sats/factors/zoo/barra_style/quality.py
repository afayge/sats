from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, require_panel, zscore

__alpha_meta__ = {
    "id": "barra_style_quality",
    "display_name": "Barra-style Quality",
    "theme": ["quality"],
    "formula_latex": "z(roe) - z(debt_to_assets)",
    "columns_required": ["roe", "debt_to_assets"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 60,
    "min_warmup_bars": 1,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Public quality proxy using profitability minus leverage pressure.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    roe, debt_to_assets = require_panel(panel, "roe", "debt_to_assets")
    return zscore(roe) - zscore(debt_to_assets)
