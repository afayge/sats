from __future__ import annotations

import pickle
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.factors.composite import FactorPickCandidate, FactorPickResult
from sats.factors.panel import build_factor_panel
from sats.factors.profiles import DEFAULT_FACTOR_PROFILE, get_factor_profile, resolve_factor_ids
from sats.factors.service import compute_factor_snapshot
from sats.storage.duckdb import DuckDBStorage


@dataclass(slots=True)
class FactorMLTrainResult:
    run_id: str
    model_type: str
    profile: str
    factor_ids: list[str]
    horizon: int
    model_path: str
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model_type": self.model_type,
            "profile": self.profile,
            "factor_ids": list(self.factor_ids),
            "horizon": self.horizon,
            "model_path": self.model_path,
            "metrics": dict(self.metrics),
        }


def train_factor_ml_model(
    *,
    settings: Settings,
    storage: DuckDBStorage,
    provider: AStockDataProvider,
    model_type: str,
    profile: str = DEFAULT_FACTOR_PROFILE,
    factor_ids: list[str] | None = None,
    train_start: str | None = None,
    train_end: str | None = None,
    valid_end: str | None = None,
    horizon: int = 1,
    lookback_days: int = 520,
    symbols: list[str] | None = None,
    model_factory: Callable[[str], Any] | None = None,
) -> FactorMLTrainResult:
    profile_name = get_factor_profile(profile).name
    ids = resolve_factor_ids(profile=profile_name, factor_ids=factor_ids)
    end_date = str(valid_end or train_end or "").strip()
    if not end_date:
        raise ValueError("factor ml train requires --train-end or --valid-end")
    panel_result = build_factor_panel(
        provider=provider,
        storage=storage,
        trade_date=end_date,
        lookback_days=lookback_days,
        symbols=symbols,
    )
    snapshot = compute_factor_snapshot(
        panel_result.panel,
        trade_date=end_date,
        profile=profile_name,
        factor_ids=ids,
        names=panel_result.names,
        warnings=panel_result.warnings,
    )
    data = _training_frame(snapshot.panels, panel_result.panel["close"], horizon=max(1, int(horizon)))
    train = _slice_training(data, start=train_start, end=train_end or end_date)
    valid = _slice_validation(data, train_end=train_end or end_date, valid_end=valid_end)
    if train.empty:
        raise ValueError("factor ml train has no usable training samples")
    model = (model_factory or _make_model)(model_type)
    feature_columns = [factor_id for factor_id in ids if factor_id in snapshot.panels]
    if not feature_columns:
        raise ValueError("factor ml train has no usable factor features")
    model.fit(train[feature_columns], train["label"])
    metrics = {
        "train_rows": int(len(train)),
        "valid_rows": int(len(valid)),
        "train_start": str(train["trade_date"].min()) if not train.empty else "",
        "train_end": str(train["trade_date"].max()) if not train.empty else "",
        "warnings": list(snapshot.warnings),
    }
    if not valid.empty:
        pred = np.asarray(model.predict(valid[feature_columns]), dtype=float)
        label = valid["label"].to_numpy(dtype=float)
        metrics.update(_prediction_metrics(pred, label, prefix="valid"))
    run_id = f"factor_ml_{uuid.uuid4().hex[:12]}"
    model_dir = Path(settings.project_root) / "models" / "factors" / run_id
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / "model.pkl"
    payload = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "model_type": model_type,
        "profile": profile_name,
        "factor_ids": feature_columns,
        "horizon": max(1, int(horizon)),
        "model": model,
        "metrics": metrics,
    }
    with model_path.open("wb") as handle:
        pickle.dump(payload, handle)
    metrics["model_path"] = str(model_path)
    storage.upsert_factor_run(
        {
            "run_id": run_id,
            "kind": "ml_train",
            "trade_date": end_date,
            "universe": "symbols" if symbols else "a_share",
            "factor_ids": feature_columns,
            "params": {
                "profile": profile_name,
                "model": model_type,
                "train_start": train_start or "",
                "train_end": train_end or end_date,
                "valid_end": valid_end or "",
                "horizon": max(1, int(horizon)),
                "lookback_days": int(lookback_days),
                "symbols": list(symbols or []),
            },
            "metrics": metrics,
            "report_path": str(model_path),
        }
    )
    return FactorMLTrainResult(
        run_id=run_id,
        model_type=model_type,
        profile=profile_name,
        factor_ids=feature_columns,
        horizon=max(1, int(horizon)),
        model_path=str(model_path),
        metrics=metrics,
    )


def predict_factor_ml_model(
    *,
    settings: Settings,
    storage: DuckDBStorage,
    provider: AStockDataProvider,
    model_run: str,
    trade_date: str,
    profile: str | None = None,
    factor_ids: list[str] | None = None,
    top: int = 20,
    lookback_days: int = 260,
    symbols: list[str] | None = None,
) -> FactorPickResult:
    payload = _load_model_payload(settings, storage, model_run)
    trained_factor_ids = [str(item) for item in payload.get("factor_ids") or [] if str(item).strip()]
    trained_profile = str(payload.get("profile") or DEFAULT_FACTOR_PROFILE)
    if profile:
        requested_profile = get_factor_profile(profile).name
        if requested_profile != trained_profile:
            raise ValueError(f"factor ml predict profile mismatch: model uses {trained_profile}, got {requested_profile}")
    if factor_ids:
        requested_factor_ids = resolve_factor_ids(profile=profile or trained_profile, factor_ids=factor_ids)
        if requested_factor_ids != trained_factor_ids:
            raise ValueError(
                "factor ml predict factors mismatch: "
                f"model uses {','.join(trained_factor_ids)}, got {','.join(requested_factor_ids)}"
            )
    model = payload["model"]
    panel_result = build_factor_panel(
        provider=provider,
        storage=storage,
        trade_date=trade_date,
        lookback_days=lookback_days,
        symbols=symbols,
    )
    snapshot = compute_factor_snapshot(
        panel_result.panel,
        trade_date=trade_date,
        profile=trained_profile,
        factor_ids=trained_factor_ids,
        names=panel_result.names,
        warnings=panel_result.warnings,
    )
    features = _prediction_frame(snapshot.panels, factor_ids=trained_factor_ids, trade_date=trade_date)
    if features.empty:
        raise ValueError("factor ml predict has no usable feature rows")
    predictions = np.asarray(model.predict(features[factor_ids]), dtype=float)
    ranked = pd.Series(predictions, index=features.index.astype(str)).sort_values(ascending=False).head(max(1, int(top)))
    candidates: list[FactorPickCandidate] = []
    for rank, (ts_code, score) in enumerate(ranked.items(), start=1):
        factors = snapshot.factor_values_for(str(ts_code))
        candidates.append(
            FactorPickCandidate(
                ts_code=str(ts_code),
                name=panel_result.names.get(str(ts_code), ""),
                rank=rank,
                score=round(float(score), 6),
                factors=factors,
                metrics={
                    "model_run": model_run,
                    "profile": trained_profile,
                    "score_date": str(_resolve_score_date(snapshot.score, trade_date) or trade_date),
                    "factor_score": snapshot.score_for(str(ts_code)),
                },
            )
        )
    result = FactorPickResult(
        run_id=f"factor_ml_pred_{uuid.uuid4().hex[:12]}",
        trade_date=str(trade_date),
        factors=trained_factor_ids,
        weighting="ml",
        neutralization="none",
        candidates=candidates,
        warnings=list(snapshot.warnings),
    )
    storage.upsert_factor_run(
        {
            "run_id": result.run_id,
            "kind": "ml_predict",
            "trade_date": trade_date,
            "universe": "symbols" if symbols else "a_share",
            "factor_ids": trained_factor_ids,
            "params": {
                "model_run": model_run,
                "profile": trained_profile,
                "top": int(top),
                "lookback_days": int(lookback_days),
                "symbols": list(symbols or []),
            },
            "metrics": {"warnings": result.warnings, "candidate_count": len(candidates)},
            "report_path": "",
        }
    )
    storage.upsert_factor_candidates(result.run_id, trade_date, [candidate.to_dict() for candidate in candidates])
    return result


def load_factor_ml_run(settings: Settings, storage: DuckDBStorage, model_run: str) -> dict[str, Any]:
    payload = _load_model_payload(settings, storage, model_run)
    return {
        "run_id": payload.get("run_id") or model_run,
        "model_type": payload.get("model_type") or "",
        "profile": payload.get("profile") or "",
        "factor_ids": list(payload.get("factor_ids") or []),
        "horizon": payload.get("horizon") or 1,
        "metrics": dict(payload.get("metrics") or {}),
    }


def _training_frame(panels: dict[str, pd.DataFrame], close: pd.DataFrame, *, horizon: int) -> pd.DataFrame:
    if not panels:
        return pd.DataFrame()
    features = pd.concat(
        {factor_id: frame.stack(future_stack=True) for factor_id, frame in panels.items()},
        axis=1,
    )
    label = close.shift(-horizon).div(close).sub(1.0).stack(future_stack=True).rename("label")
    data = features.join(label, how="inner").replace([np.inf, -np.inf], np.nan).dropna()
    if data.empty:
        return data
    data = data.reset_index()
    data.columns = ["trade_date", "ts_code", *list(features.columns), "label"]
    data["trade_date"] = data["trade_date"].astype(str)
    data["ts_code"] = data["ts_code"].astype(str)
    return data


def _slice_training(data: pd.DataFrame, *, start: str | None, end: str | None) -> pd.DataFrame:
    if data.empty:
        return data
    mask = pd.Series(True, index=data.index)
    if start:
        mask &= data["trade_date"].astype(str) >= str(start)
    if end:
        mask &= data["trade_date"].astype(str) <= str(end)
    return data.loc[mask].copy()


def _slice_validation(data: pd.DataFrame, *, train_end: str, valid_end: str | None) -> pd.DataFrame:
    if data.empty or not valid_end:
        return data.iloc[0:0].copy()
    mask = (data["trade_date"].astype(str) > str(train_end)) & (data["trade_date"].astype(str) <= str(valid_end))
    return data.loc[mask].copy()


def _prediction_frame(panels: dict[str, pd.DataFrame], *, factor_ids: list[str], trade_date: str) -> pd.DataFrame:
    date = None
    for frame in panels.values():
        date = _resolve_score_date(frame, trade_date)
        if date is not None:
            break
    if date is None:
        return pd.DataFrame(columns=factor_ids)
    data = {}
    for factor_id in factor_ids:
        frame = panels.get(factor_id)
        if frame is None or date not in frame.index:
            continue
        data[factor_id] = frame.loc[date]
    features = pd.DataFrame(data).replace([np.inf, -np.inf], np.nan).dropna()
    return features


def _prediction_metrics(pred: np.ndarray, label: np.ndarray, *, prefix: str) -> dict[str, float]:
    if len(pred) == 0:
        return {}
    error = pred - label
    metrics = {
        f"{prefix}_mae": round(float(np.mean(np.abs(error))), 8),
        f"{prefix}_rmse": round(float(np.sqrt(np.mean(error * error))), 8),
    }
    if len(pred) > 1 and np.std(pred) > 1e-12 and np.std(label) > 1e-12:
        metrics[f"{prefix}_corr"] = round(float(np.corrcoef(pred, label)[0, 1]), 8)
    return metrics


def _make_model(model_type: str) -> Any:
    model = str(model_type or "").strip().lower()
    if model == "lightgbm":
        from lightgbm import LGBMRegressor

        return LGBMRegressor(n_estimators=80, learning_rate=0.05, random_state=7)
    if model == "xgboost":
        from xgboost import XGBRegressor

        return XGBRegressor(n_estimators=80, learning_rate=0.05, max_depth=4, random_state=7, objective="reg:squarederror")
    raise ValueError(f"unsupported factor ML model: {model_type}")


def _load_model_payload(settings: Settings, storage: DuckDBStorage, model_run: str) -> dict[str, Any]:
    run = storage.get_factor_run(model_run)
    model_path = ""
    if run:
        metrics = run.get("metrics") if isinstance(run.get("metrics"), dict) else {}
        model_path = str(metrics.get("model_path") or run.get("report_path") or "").strip()
    if not model_path:
        model_path = str(Path(settings.project_root) / "models" / "factors" / model_run / "model.pkl")
    path = Path(model_path)
    if not path.exists():
        raise ValueError(f"factor ML model not found: {model_run}")
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    if not isinstance(payload, dict) or "model" not in payload:
        raise ValueError(f"invalid factor ML model file: {path}")
    return payload


def _resolve_score_date(score: pd.DataFrame, trade_date: str) -> Any | None:
    if score.empty:
        return None
    if trade_date in score.index:
        return trade_date
    values = [idx for idx in score.index if str(idx) <= str(trade_date)]
    return values[-1] if values else None
