from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd


def make_trade_dates(count: int, *, end: str = "20260430") -> list[str]:
    end_dt = datetime.strptime(end, "%Y%m%d")
    dates = []
    cursor = end_dt
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    return sorted(dates)


def make_passing_daily(*, latest_volume: float = 1200.0, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(70, end=end)
    closes = [10.0 + i * 0.04 for i in range(70)]
    rows = []
    for index, (trade_date, close) in enumerate(zip(dates, closes)):
        open_price = close - 0.02
        high = close + 0.03
        low = close - 0.17
        volume = 1000.0
        if index == 20:
            volume = max(2500.0, latest_volume + 100)
        if index == len(dates) - 1:
            volume = latest_volume
        prev = closes[index - 1] if index > 0 else close
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(open_price, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "vol": volume,
                "amount": volume * close,
                "pct_chg": (close / prev - 1.0) * 100 if index > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)


def make_price_volume_daily(
    *,
    pct_chg: float = 4.0,
    latest_volume: float = 1200.0,
    ma_stack: bool = True,
    end: str = "20260430",
) -> pd.DataFrame:
    dates = make_trade_dates(70, end=end)
    if ma_stack:
        closes = [10.0 + i * 0.05 for i in range(70)]
    else:
        closes = [20.0 - i * 0.04 for i in range(70)]
    closes[-1] = closes[-2] * (1.0 + pct_chg / 100.0)

    rows = []
    for index, (trade_date, close) in enumerate(zip(dates, closes)):
        volume = latest_volume if index == len(dates) - 1 else 1000.0
        prev = closes[index - 1] if index > 0 else close
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(close - 0.05, 4),
                "high": round(close + 0.1, 4),
                "low": round(close - 0.1, 4),
                "close": round(close, 4),
                "vol": volume,
                "amount": volume * close,
                "pct_chg": pct_chg if index == len(dates) - 1 else (close / prev - 1.0) * 100 if index > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)


def make_daily_basic(*, turnover_rate: float = 8.0, circ_mv: float = 1_200_000.0, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(70, end=end)
    return pd.DataFrame(
        {
            "ts_code": ["000001.SZ"] * len(dates),
            "trade_date": dates,
            "turnover_rate": [turnover_rate] * len(dates),
            "circ_mv": [circ_mv] * len(dates),
        }
    )


def make_benchmark(*, strong: bool = False, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(70, end=end)
    step = 0.01 if not strong else 0.12
    closes = [10.0 + i * step for i in range(70)]
    return pd.DataFrame({"trade_date": dates, "close": closes})


def make_chan_daily(
    *,
    end: str = "20260430",
    breakout_volume: float = 1600.0,
    pullback_breaks_box: bool = False,
    no_pullback: bool = False,
    hot_latest: bool = False,
) -> pd.DataFrame:
    dates = make_trade_dates(45, end=end)
    breakout_index = len(dates) - 1 if no_pullback else len(dates) - 4
    rows = []
    for index, trade_date in enumerate(dates):
        close = 10.0 + (index % 5) * 0.02
        high = close + 0.10
        low = close - 0.10
        volume = 1000.0
        if index == breakout_index:
            close = 10.45
            high = 10.55
            low = 10.30
            volume = breakout_volume
        elif index > breakout_index:
            close = 10.34 + (index - breakout_index) * 0.02
            high = close + 0.08
            low = close - 0.10
            if pullback_breaks_box and index == breakout_index + 1:
                low = 9.90
            if hot_latest and index == len(dates) - 1:
                close = 12.80
                high = 12.95
                low = 12.50
        prev_close = rows[-1]["close"] if rows else close
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(close - 0.03, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "vol": volume,
                "amount": volume * close,
                "pct_chg": (close / prev_close - 1.0) * 100 if rows else 0.0,
            }
        )
    return pd.DataFrame(rows)


def make_chan_minute_30m(
    *,
    end: str = "20260430",
    box_high: float = 10.18,
    breaks_box: bool = False,
    macd_improving: bool = True,
) -> pd.DataFrame:
    dates = make_trade_dates(5, end=end)
    rows = []
    for day_index, trade_date in enumerate(dates):
        for slot in range(8):
            index = day_index * 8 + slot
            if macd_improving:
                close = 10.60 - min(index, 24) * 0.012 + max(index - 24, 0) * 0.018
            else:
                close = 10.55 - index * 0.004
            low = close - 0.04
            if breaks_box and index == 18:
                low = box_high * 0.97
            if not macd_improving and index == 39:
                low = box_high * 0.99
                close = max(close, 10.45)
            rows.append(
                {
                    "ts_code": "000001.SZ",
                    "period": "30m",
                    "trade_date": trade_date,
                    "trade_time": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} {9 + slot // 2:02d}:{30 if slot % 2 == 0 else 0:02d}:00",
                    "open": round(close - 0.02, 4),
                    "high": round(close + 0.05, 4),
                    "low": round(low, 4),
                    "close": round(close, 4),
                    "vol": 1000.0,
                    "amount": 1000.0 * close,
                    "data_source": "tickflow",
                }
            )
    return pd.DataFrame(rows)


def make_chan_sell_minute_30m(*, end: str = "20260430", base: float = 10.20) -> pd.DataFrame:
    dates = make_trade_dates(5, end=end)
    rows = []
    for day_index, trade_date in enumerate(dates):
        for slot in range(8):
            index = day_index * 8 + slot
            close = base + min(index, 24) * 0.018 - max(index - 24, 0) * 0.020
            rows.append(
                {
                    "ts_code": "000001.SZ",
                    "period": "30m",
                    "trade_date": trade_date,
                    "trade_time": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} {9 + slot // 2:02d}:{30 if slot % 2 == 0 else 0:02d}:00",
                    "open": round(close + 0.02, 4),
                    "high": round(close + 0.05, 4),
                    "low": round(close - 0.05, 4),
                    "close": round(close, 4),
                    "vol": 1000.0,
                    "amount": 1000.0 * close,
                    "data_source": "tickflow",
                }
            )
    return pd.DataFrame(rows)


def make_chan_first_buy_daily(*, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(80, end=end)
    rows = []
    closes = []
    for index in range(80):
        if index < 25:
            close = 12.0 - index * 0.13
        elif index < 55:
            close = 9.05 + (index % 6) * 0.08
        else:
            close = 9.35 - (index - 55) * 0.025
            if index >= 76:
                close = 8.72 + (index - 76) * 0.08
        closes.append(close)
    for index, (trade_date, close) in enumerate(zip(dates, closes)):
        prev = closes[index - 1] if index > 0 else close
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(close - 0.03, 4),
                "high": round(close + 0.08, 4),
                "low": round(close - 0.08, 4),
                "close": round(close, 4),
                "vol": 1400.0 if index < 25 else 900.0,
                "amount": close * 1000.0,
                "pct_chg": (close / prev - 1.0) * 100 if index > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)


def make_chan_second_buy_daily(*, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(80, end=end)
    rows = []
    closes = []
    for index in range(80):
        if index < 35:
            close = 11.2 - index * 0.06
        elif index < 58:
            close = 9.10 + (index - 35) * 0.085
        else:
            close = 11.05 - (index - 58) * 0.035
            if index >= 76:
                close = 10.08 + (index - 76) * 0.06
        closes.append(close)
    for index, (trade_date, close) in enumerate(zip(dates, closes)):
        prev = closes[index - 1] if index > 0 else close
        low = close - 0.08
        if index == 35:
            low = 8.95
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(close - 0.03, 4),
                "high": round(close + 0.10, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "vol": 1000.0,
                "amount": close * 1000.0,
                "pct_chg": (close / prev - 1.0) * 100 if index > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)


def make_chan_center_low_daily(*, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(70, end=end)
    rows = []
    for index, trade_date in enumerate(dates):
        close = 10.10 + (index % 8) * 0.035
        high = close + 0.10
        low = close - 0.10
        if index == len(dates) - 12:
            close = 9.88
            high = 10.02
            low = 9.70
        if index == len(dates) - 1:
            close = 9.95
            high = 10.08
            low = 9.72
        prev = rows[-1]["close"] if rows else close
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(close - 0.03, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "vol": 900.0,
                "amount": close * 900.0,
                "pct_chg": (close / prev - 1.0) * 100 if rows else 0.0,
            }
        )
    return pd.DataFrame(rows)


def make_chan_first_sell_daily(*, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(80, end=end)
    closes = []
    for index in range(80):
        if index < 25:
            close = 8.0 + index * 0.16
        elif index < 55:
            close = 11.55 + (index % 6) * 0.08
        else:
            close = 11.25 + (index - 55) * 0.055
            if index >= 76:
                close = 12.75 - (index - 76) * 0.10
        closes.append(close)
    return _daily_from_closes(dates, closes, high_pad=0.10, low_pad=0.08)


def make_chan_second_sell_daily(*, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(80, end=end)
    closes = []
    for index in range(80):
        if index < 35:
            close = 9.0 + index * 0.08
        elif index < 58:
            close = 12.20 - (index - 35) * 0.09
        else:
            close = 10.10 + (index - 58) * 0.035
            if index >= 76:
                close = 10.95 - (index - 76) * 0.08
        closes.append(close)
    frame = _daily_from_closes(dates, closes, high_pad=0.10, low_pad=0.08)
    frame.loc[35, "high"] = 12.40
    return frame


def make_chan_third_sell_daily(*, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(45, end=end)
    rows = []
    breakdown_index = len(dates) - 4
    for index, trade_date in enumerate(dates):
        close = 10.20 + (index % 5) * 0.02
        high = close + 0.10
        low = close - 0.10
        volume = 1000.0
        if index == breakdown_index:
            close = 9.72
            high = 9.88
            low = 9.62
            volume = 1700.0
        elif index > breakdown_index:
            close = 9.73 - (index - breakdown_index) * 0.02
            high = 9.83
            low = close - 0.08
        prev = rows[-1]["close"] if rows else close
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(close + 0.03, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "vol": volume,
                "amount": volume * close,
                "pct_chg": (close / prev - 1.0) * 100 if rows else 0.0,
            }
        )
    return pd.DataFrame(rows)


def make_chan_center_high_daily(*, end: str = "20260430") -> pd.DataFrame:
    dates = make_trade_dates(70, end=end)
    rows = []
    for index, trade_date in enumerate(dates):
        close = 10.00 + (index % 8) * 0.035
        high = close + 0.10
        low = close - 0.10
        if index == len(dates) - 12:
            close = 10.38
            high = 10.62
            low = 10.20
        if index == len(dates) - 1:
            close = 10.34
            high = 10.60
            low = 10.18
        prev = rows[-1]["close"] if rows else close
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(close + 0.03, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "vol": 900.0,
                "amount": close * 900.0,
                "pct_chg": (close / prev - 1.0) * 100 if rows else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _daily_from_closes(
    dates: list[str],
    closes: list[float],
    *,
    high_pad: float,
    low_pad: float,
) -> pd.DataFrame:
    rows = []
    for index, (trade_date, close) in enumerate(zip(dates, closes)):
        prev = closes[index - 1] if index > 0 else close
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": round(close, 4),
                "high": round(close + high_pad, 4),
                "low": round(close - low_pad, 4),
                "close": round(close, 4),
                "vol": 1200.0,
                "amount": close * 1200.0,
                "pct_chg": (close / prev - 1.0) * 100 if index > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)
