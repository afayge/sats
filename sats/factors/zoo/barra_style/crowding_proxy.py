from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, finite, require_panel, zscore

__alpha_meta__ = {
    "id": "barra_style_crowding_proxy",
    "display_name": "Barra-style Crowding Proxy",
    "theme": ["crowding", "liquidity"],
    "formula_latex": "z(turnover_rate) + z(amount / mean(amount,20)) + optional z(main_net_amount/amount)",
    "columns_required": ["amount", "turnover_rate"],
    "extras_required": [],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "negative",
    "decay_horizon": 20,
    "min_warmup_bars": 20,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Approximate crowding pressure; main_net_amount is used only when present.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    amount, turnover = require_panel(panel, "amount", "turnover_rate")
    amount_expansion = amount.div(amount.rolling(20, min_periods=10).mean())
    score = zscore(turnover) + zscore(amount_expansion)
    main_net = panel.get("main_net_amount")
    if main_net is not None:
        score = score + zscore(main_net.div(amount.where(amount > 0)))
    return finite(score)
