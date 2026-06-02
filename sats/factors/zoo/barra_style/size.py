from __future__ import annotations

import numpy as np
import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, finite, positive, require_panel

__alpha_meta__ = {
    "id": "barra_style_size",
    "display_name": "Barra-style Size",
    "theme": ["size"],
    "formula_latex": "log(total_mv)",
    "columns_required": ["total_mv"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 20,
    "min_warmup_bars": 1,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Public approximation of size exposure using total market value.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (total_mv,) = require_panel(panel, "total_mv")
    return finite(np.log(positive(total_mv)))
