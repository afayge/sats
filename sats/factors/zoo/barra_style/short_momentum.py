from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, finite, require_panel

__alpha_meta__ = {
    "id": "barra_style_short_momentum",
    "display_name": "Barra-style Short Momentum",
    "theme": ["short_momentum", "momentum"],
    "formula_latex": "close / close.shift(20) - 1",
    "columns_required": ["close"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 20,
    "min_warmup_bars": 20,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Short-horizon momentum proxy for A-share tactical ranking.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (close,) = require_panel(panel, "close")
    return finite(close / close.shift(20) - 1.0)
