from __future__ import annotations

import sys
from typing import Any

import pandas as pd

from sats.data.akshare_datasets import AkShareDatasetSpec, get_akshare_dataset, list_akshare_datasets
from sats.symbols import normalize_symbols, normalize_ts_code

_MINI_RACER_UNRAISABLE_FILTER_INSTALLED = False
_MAX_AKSHARE_LIMIT = 1000
_MAX_AKSHARE_FIELDS = 30
_BLOCKED_AKSHARE_PARAM_PARTS = ("path", "file", "callback", "func", "code", "cmd", "command", "cookie", "token")


class AkShareDataProvider:
    """Optional AkShare supplements for native DSA analysis.

    AkShare is intentionally imported lazily so SATS can run without the
    dependency installed. Every public method returns empty data on import or
    endpoint failure.
    """

    def __init__(self, *, ak_module: Any | None = None) -> None:
        _install_mini_racer_unraisable_filter()
        self._ak = ak_module

    def load_realtime_quotes(self, symbols: list[str]) -> pd.DataFrame:
        clean_symbols = normalize_symbols(symbols, required=False)
        if not clean_symbols:
            return _empty_quote_frame()
        data = self.load_a_share_realtime_quotes()
        if data.empty:
            return _empty_quote_frame()
        data = data[data["ts_code"].isin(clean_symbols)].copy()
        if data.empty:
            return _empty_quote_frame()
        return data.reset_index(drop=True)

    def load_a_share_realtime_quotes(self) -> pd.DataFrame:
        ak = self._akshare()
        if ak is None:
            return _empty_quote_frame()
        try:
            frame = ak.stock_zh_a_spot_em()
        except Exception:
            return _empty_quote_frame()
        return _adapt_spot_frame(frame)

    def load_chip_context(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        ak = self._akshare()
        if ak is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for ts_code in normalize_symbols(symbols, required=False):
            plain_code = ts_code.split(".", 1)[0]
            try:
                frame = ak.stock_cyq_em(symbol=plain_code)
            except Exception:
                continue
            if frame is None or frame.empty:
                continue
            row = frame.iloc[-1]
            result[ts_code] = {
                "date": str(row.get("日期") or ""),
                "profit_ratio": _safe_float(row.get("获利比例")),
                "avg_cost": _safe_float(row.get("平均成本")),
                "concentration_90": _safe_float(row.get("90集中度")),
                "concentration_70": _safe_float(row.get("70集中度")),
                "data_source": "akshare_stock_cyq_em",
            }
        return result

    def load_fundamental_context(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        ak = self._akshare()
        if ak is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for ts_code in normalize_symbols(symbols, required=False):
            plain_code = ts_code.split(".", 1)[0]
            try:
                frame = ak.stock_individual_info_em(symbol=plain_code)
            except Exception:
                continue
            if frame is None or frame.empty:
                continue
            payload = _individual_info_payload(frame)
            if payload:
                payload["data_source"] = "akshare_stock_individual_info_em"
                result[ts_code] = payload
        return result

    def list_akshare_datasets(
        self,
        *,
        domain: str | None = None,
        category: str | None = None,
        tags: list[str] | tuple[str, ...] | str | None = None,
        query: str | None = None,
        realtime: bool | None = None,
        compact: bool = False,
    ) -> list[dict[str, Any]]:
        return list_akshare_datasets(
            domain=domain,
            category=category,
            tags=tags,
            query=query,
            realtime=realtime,
            compact=compact,
        )

    def describe_akshare_dataset(self, dataset: str) -> dict[str, Any]:
        return get_akshare_dataset(dataset).to_dict(compact=False)

    def fetch_akshare_dataset(
        self,
        dataset: str,
        params: dict[str, Any] | None = None,
        *,
        fields: list[str] | str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        spec = get_akshare_dataset(dataset)
        call_params = _safe_akshare_params(spec, params or {})
        safe_fields = _safe_fields(fields)
        safe_limit = _safe_limit(limit, default=spec.default_limit)
        ak = self._akshare()
        if ak is None:
            return _akshare_unavailable_payload(spec, call_params, reason="akshare:unavailable")
        func = getattr(ak, spec.function_name, None)
        if not callable(func):
            return _akshare_unavailable_payload(spec, call_params, reason="akshare:function_unavailable")
        try:
            value = func(**call_params)
        except Exception as exc:
            payload = _akshare_unavailable_payload(spec, call_params, reason="akshare:fetch_failed")
            payload["error"] = str(exc)
            return payload
        return _akshare_result_payload(spec, value, call_params, fields=safe_fields, limit=safe_limit)

    def _akshare(self):
        if self._ak is not None:
            return self._ak
        try:
            import akshare as ak  # type: ignore
        except Exception:
            return None
        self._ak = ak
        return ak


def _safe_akshare_params(spec: AkShareDatasetSpec, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("AkShare params must be an object")
    allowed = set(spec.input_fields)
    clean: dict[str, Any] = {}
    for key, value in params.items():
        name = str(key or "").strip()
        if not name:
            continue
        if allowed and name not in allowed:
            raise ValueError(f"unsupported AkShare param for {spec.dataset}: {name}")
        lowered = name.lower()
        if any(part in lowered for part in _BLOCKED_AKSHARE_PARAM_PARTS):
            raise ValueError(f"blocked AkShare param for readonly agent access: {name}")
        if not _json_safe(value):
            raise ValueError(f"AkShare param is not JSON safe: {name}")
        clean[name] = value
    return clean


def _safe_fields(fields: list[str] | str | None) -> list[str]:
    if fields is None:
        return []
    raw = [fields] if isinstance(fields, str) else list(fields)
    result = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in result:
            result.append(name)
        if len(result) >= _MAX_AKSHARE_FIELDS:
            break
    return result


def _safe_limit(limit: Any, *, default: int = 200) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = int(default or 200)
    return max(1, min(_MAX_AKSHARE_LIMIT, value))


def _json_safe(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_safe(item) for key, item in value.items())
    return False


def _akshare_result_payload(
    spec: AkShareDatasetSpec,
    value: Any,
    params: dict[str, Any],
    *,
    fields: list[str],
    limit: int,
) -> dict[str, Any]:
    if isinstance(value, pd.DataFrame):
        return _akshare_frame_payload(spec, value, params, fields=fields, limit=limit)
    rows, columns = _records_from_value(value, limit=limit)
    return {
        **_akshare_base_payload(spec, params),
        "columns": columns,
        "rows": rows,
        "head": rows[: min(5, len(rows))],
        "tail": rows[-min(5, len(rows)) :] if rows else [],
        "latest": rows[-1] if rows else {},
        "row_count": len(rows),
        "returned_row_count": len(rows),
        "data_source": f"akshare_{spec.function_name}",
        "missing_fields": [],
        "market_data_provenance": [_akshare_provenance(spec)],
    }


def _akshare_frame_payload(
    spec: AkShareDatasetSpec,
    frame: pd.DataFrame,
    params: dict[str, Any],
    *,
    fields: list[str],
    limit: int,
) -> dict[str, Any]:
    data = frame.copy()
    if fields:
        keep = [field for field in fields if field in data.columns]
        data = data[keep] if keep else data.iloc[:, 0:0]
    limited = data.head(limit)
    rows = _frame_records(limited)
    return {
        **_akshare_base_payload(spec, params),
        "columns": [str(column) for column in data.columns],
        "rows": rows,
        "head": _frame_records(data.head(min(5, limit))),
        "tail": _frame_records(data.tail(min(5, limit))),
        "latest": _frame_records(data.tail(1))[0] if not data.empty else {},
        "row_count": int(len(data)),
        "returned_row_count": int(len(limited)),
        "data_source": f"akshare_{spec.function_name}",
        "missing_fields": [],
        "market_data_provenance": [_akshare_provenance(spec)],
    }


def _akshare_unavailable_payload(spec: AkShareDatasetSpec, params: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        **_akshare_base_payload(spec, params),
        "columns": [],
        "rows": [],
        "head": [],
        "tail": [],
        "latest": {},
        "row_count": 0,
        "returned_row_count": 0,
        "data_source": "unavailable",
        "missing_fields": [reason],
        "market_data_provenance": [],
    }


def _akshare_base_payload(spec: AkShareDatasetSpec, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset": spec.dataset,
        "function_name": spec.function_name,
        "title": spec.title,
        "domain": spec.domain,
        "category": spec.category,
        "tags": list(spec.tags),
        "params": dict(params),
        "doc_url": spec.doc_url,
        "realtime": spec.realtime,
    }


def _akshare_provenance(spec: AkShareDatasetSpec) -> dict[str, Any]:
    return {"dataset": spec.dataset, "source": "akshare", "function": spec.function_name}


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _records_from_value(value: Any, *, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    safe = _json_value(value)
    if isinstance(safe, list):
        rows = [_row_from_value(item) for item in safe[:limit]]
    elif isinstance(safe, dict):
        rows = [safe]
    else:
        rows = [{"value": safe}]
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return rows, columns


def _row_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {"value": value}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _adapt_spot_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return _empty_quote_frame()
    code_col = _first_column(frame, ["代码", "code", "symbol"])
    if code_col is None:
        return _empty_quote_frame()
    data = frame.copy()
    data["ts_code"] = data[code_col].astype(str).map(normalize_ts_code)
    data = data[data["ts_code"].astype(str).str.strip() != ""].copy()
    if data.empty:
        return _empty_quote_frame()
    result = pd.DataFrame(
        {
            "ts_code": data["ts_code"],
            "name": _column_or_blank(data, ["名称", "name"]),
            "close": _numeric_column(data, ["最新价", "close", "price"]),
            "pct_chg": _numeric_column(data, ["涨跌幅", "pct_chg"]),
            "vol": _numeric_column(data, ["成交量", "volume", "vol"]),
            "amount": _numeric_column(data, ["成交额", "amount"]),
            "volume_ratio": _numeric_column(data, ["量比", "volume_ratio"]),
            "turnover_rate": _numeric_column(data, ["换手率", "turnover_rate"]),
            "pe": _numeric_column(data, ["市盈率-动态", "市盈率", "pe"]),
            "pb": _numeric_column(data, ["市净率", "pb"]),
            "total_mv": _numeric_column(data, ["总市值", "total_mv"]),
            "circ_mv": _numeric_column(data, ["流通市值", "circ_mv"]),
            "data_source": "akshare_spot_em",
        }
    )
    return result.drop_duplicates(subset=["ts_code"], keep="last").reset_index(drop=True)


def _install_mini_racer_unraisable_filter() -> None:
    """Hide a known py_mini_racer destructor warning emitted by AkShare."""
    global _MINI_RACER_UNRAISABLE_FILTER_INSTALLED
    if getattr(sys.unraisablehook, "_sats_mini_racer_filter", False):
        _MINI_RACER_UNRAISABLE_FILTER_INSTALLED = True
        return
    previous_hook = sys.unraisablehook

    def hook(unraisable: Any) -> None:
        if _is_mini_racer_unraisable(unraisable):
            return
        previous_hook(unraisable)

    setattr(hook, "_sats_mini_racer_filter", True)
    sys.unraisablehook = hook
    _MINI_RACER_UNRAISABLE_FILTER_INSTALLED = True


def _is_mini_racer_unraisable(unraisable: Any) -> bool:
    exc_value = getattr(unraisable, "exc_value", None)
    exc_type = getattr(unraisable, "exc_type", None)
    if exc_type is not None:
        try:
            if not issubclass(exc_type, AttributeError):
                return False
        except TypeError:
            return False
    elif not isinstance(exc_value, AttributeError):
        return False
    exc_text = str(exc_value or "")
    if "mr_free_context" not in exc_text or "NoneType" not in exc_text:
        return False
    obj = getattr(unraisable, "object", None)
    obj_text = " ".join(
        str(part)
        for part in (
            repr(obj),
            getattr(obj, "__qualname__", ""),
            getattr(obj, "__module__", ""),
        )
        if part
    )
    return "MiniRacer.__del__" in obj_text and "py_mini_racer" in obj_text


def _empty_quote_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "ts_code",
            "name",
            "close",
            "pct_chg",
            "vol",
            "amount",
            "volume_ratio",
            "turnover_rate",
            "pe",
            "pb",
            "total_mv",
            "circ_mv",
            "data_source",
        ]
    )


def _individual_info_payload(frame: pd.DataFrame) -> dict[str, Any]:
    item_col = _first_column(frame, ["item", "项目", "指标"])
    value_col = _first_column(frame, ["value", "值", "内容"])
    if item_col is None or value_col is None:
        return {}
    payload: dict[str, Any] = {}
    for _, row in frame.iterrows():
        key = str(row.get(item_col) or "").strip()
        value = row.get(value_col)
        if not key:
            continue
        if "行业" in key:
            payload["industry"] = str(value or "").strip()
        elif "总市值" in key:
            payload["total_mv"] = _safe_float(value)
        elif "流通市值" in key:
            payload["circ_mv"] = _safe_float(value)
        elif "上市时间" in key or "上市日期" in key:
            payload["list_date"] = str(value or "").strip()
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _first_column(frame: pd.DataFrame, names: list[str]) -> str | None:
    lookup = {str(column).lower(): str(column) for column in frame.columns}
    for name in names:
        match = lookup.get(name.lower())
        if match is not None:
            return match
    return None


def _numeric_column(frame: pd.DataFrame, names: list[str]) -> pd.Series:
    column = _first_column(frame, names)
    if column is None:
        return pd.Series([None] * len(frame), index=frame.index)
    return pd.to_numeric(frame[column], errors="coerce")


def _column_or_blank(frame: pd.DataFrame, names: list[str]) -> pd.Series:
    column = _first_column(frame, names)
    if column is None:
        return pd.Series([""] * len(frame), index=frame.index)
    return frame[column].fillna("").astype(str)


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
