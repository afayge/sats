from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, inverse, require_panel, zscore

__alpha_meta__ = {
    "id": "barra_style_value",
    "display_name": "Barra-style Value",
    "theme": ["value"],
    "formula_latex": "z(1/pb) + z(1/pe) + z(1/ps)",
    "columns_required": ["pb", "pe", "ps"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 60,
    "min_warmup_bars": 1,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Public value composite; each valuation leg is cross-sectionally standardized per date.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    pb, pe, ps = require_panel(panel, "pb", "pe", "ps")
    return zscore(inverse(pb)) + zscore(inverse(pe)) + zscore(inverse(ps))
