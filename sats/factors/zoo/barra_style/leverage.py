from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, require_panel

__alpha_meta__ = {
    "id": "barra_style_leverage",
    "display_name": "Barra-style Leverage",
    "theme": ["leverage"],
    "formula_latex": "debt_to_assets",
    "columns_required": ["debt_to_assets"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "negative",
    "decay_horizon": 60,
    "min_warmup_bars": 1,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Higher leverage is generally a risk exposure; composite pickers should invert when seeking quality.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (debt_to_assets,) = require_panel(panel, "debt_to_assets")
    return debt_to_assets
