from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, require_panel

__alpha_meta__ = {
    "id": "barra_style_state_ownership_proxy",
    "display_name": "Barra-style State Ownership Proxy",
    "theme": ["ownership"],
    "formula_latex": "state_ownership",
    "columns_required": ["state_ownership"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "neutral",
    "decay_horizon": 60,
    "min_warmup_bars": 1,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Reserved for verified SOE/central-SOE ownership tags. SATS skips this factor when the field is unavailable.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    (state_ownership,) = require_panel(panel, "state_ownership")
    return state_ownership
