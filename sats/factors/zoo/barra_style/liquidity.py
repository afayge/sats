from __future__ import annotations

import pandas as pd

from sats.factors.zoo.barra_style._common import LICENSE_NOTE, SOURCE, close_returns, finite, require_panel, zscore

__alpha_meta__ = {
    "id": "barra_style_liquidity",
    "display_name": "Barra-style Liquidity",
    "theme": ["liquidity"],
    "formula_latex": "z(turnover_rate) + z(amount) - z(abs(ret)/(amount+eps))",
    "columns_required": ["close", "amount", "turnover_rate"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "direction": "positive",
    "decay_horizon": 20,
    "min_warmup_bars": 20,
    "source": "SATS public Barra-style approximation; not MSCI Barra proprietary exposure data.",
    "license_note": "SATS original implementation. MSCI Barra names are used only as style-factor references.",
    "notes": "Public liquidity proxy combining turnover, traded amount, and inverse Amihud illiquidity.",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    close, amount, turnover = require_panel(panel, "close", "amount", "turnover_rate")
    returns = close_returns(close).abs()
    amihud = returns.div(amount.where(amount > 0))
    return finite(zscore(turnover) + zscore(amount) - zscore(amihud))
