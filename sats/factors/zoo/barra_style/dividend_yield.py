from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, require_panel

__alpha_meta__ = {
    "id": "barra_style_dividend_yield",
    "display_name": "Barra-style Dividend Yield",
    "theme": ["dividend_yield", "value"],
    "formula_latex": "dividend_yield",
    "columns_required": ["dividend_yield"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 60,
    "min_warmup_bars": 1,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Reserved for real dividend-yield data. SATS skips this factor when dividend_yield is unavailable.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (dividend_yield,) = require_panel(panel, "dividend_yield")
    return dividend_yield
