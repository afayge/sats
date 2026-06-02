from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, inverse, require_panel

__alpha_meta__ = {
    "id": "barra_style_book_to_price",
    "display_name": "Barra-style Book To Price",
    "theme": ["value"],
    "formula_latex": "1 / pb",
    "columns_required": ["pb"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 60,
    "min_warmup_bars": 1,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Public approximation using inverse PB; negative or zero PB is treated as missing.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (pb,) = require_panel(panel, "pb")
    return inverse(pb)
