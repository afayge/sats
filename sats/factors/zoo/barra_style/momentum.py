from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, finite, require_panel

__alpha_meta__ = {
    "id": "barra_style_momentum",
    "display_name": "Barra-style Momentum",
    "theme": ["momentum"],
    "formula_latex": "close.shift(21) / close.shift(252) - 1",
    "columns_required": ["close"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 60,
    "min_warmup_bars": 252,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Approximate 12-1 month momentum using 252-day and 21-day lags.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (close,) = require_panel(panel, "close")
    return finite(close.shift(21) / close.shift(252) - 1.0)
