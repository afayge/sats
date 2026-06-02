from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, inverse, require_panel

__alpha_meta__ = {
    "id": "barra_style_earnings_yield",
    "display_name": "Barra-style Earnings Yield",
    "theme": ["earnings_yield", "value"],
    "formula_latex": "1 / pe",
    "columns_required": ["pe"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 60,
    "min_warmup_bars": 1,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Public approximation using inverse PE; negative or zero PE is treated as missing.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (pe,) = require_panel(panel, "pe")
    return inverse(pe)
