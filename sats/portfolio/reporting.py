from __future__ import annotations

from pathlib import Path
from typing import Any


def write_portfolio_daily_report(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(render_portfolio_daily_report(payload), encoding="utf-8")
    tmp_path.replace(path)
    return path


def render_portfolio_daily_report(payload: dict[str, Any]) -> str:
    trade_date = str(payload.get("trade_date") or "")
    mode = str(payload.get("trading_mode") or "")
    account = payload.get("account") or {}
    regime = payload.get("market_regime") or {}
    performance = payload.get("performance") or {}
    lines = [
        f"# SATS Portfolio 每日总结 {trade_date}",
        "",
        f"- 模式：{mode}",
        f"- 账户：{payload.get('account_id') or account.get('account_id') or 'default'}",
        f"- 大盘评分：{_fmt(regime.get('score'))}",
        f"- 仓位上限：{_pct(regime.get('exposure_limit'))}",
        f"- 是否允许新增买入：{'是' if regime.get('buy_allowed') else '否'}",
        "",
        "## 账户概览",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 总资产 | {_money(account.get('total_asset'))} |",
        f"| 现金 | {_money(account.get('cash'))} |",
        f"| 市值 | {_money(account.get('market_value'))} |",
        f"| 已实现盈亏 | {_money(account.get('realized_pnl'))} |",
        f"| 最大回撤 | {_pct(account.get('max_drawdown_pct'), raw_percent=True)} |",
        "",
        "## 当日成交",
        "",
    ]
    lines.extend(_trade_table(payload.get("trades") or []))
    lines.extend(["", "## 当日订单", ""])
    lines.extend(_order_table(payload.get("orders") or []))
    lines.extend(["", "## 收盘持仓与计划", ""])
    lines.extend(_position_table(payload.get("positions") or [], payload.get("plans_by_id") or {}))
    lines.extend(["", "## 10选5与换榜记录", ""])
    lines.extend(_candidate_table(payload.get("candidates") or []))
    lines.extend(["", "## 实盘待确认/拒绝委托", ""])
    lines.extend(_intent_table(payload.get("pending_intents") or []))
    lines.extend(["", "## 复核请求", ""])
    lines.extend(_review_request_table(payload.get("review_requests") or []))
    lines.extend(
        [
            "",
            "## 统计",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 已平仓交易数 | {_fmt(performance.get('closed_trade_count'), digits=0)} |",
            f"| 已平仓胜率 | {_pct(performance.get('closed_trade_win_rate'))} |",
            f"| 当前最大不利波动 | {_pct(performance.get('current_max_adverse_excursion_pct'), raw_percent=True)} |",
            f"| 1日平均收益 | {_pct(performance.get('average_return_1d_pct'), raw_percent=True)} |",
            f"| 1日命中率 | {_pct(performance.get('hit_rate_1d'))} |",
            f"| 2日平均收益 | {_pct(performance.get('average_return_2d_pct'), raw_percent=True)} |",
            f"| 2日命中率 | {_pct(performance.get('hit_rate_2d'))} |",
            f"| 4日平均收益 | {_pct(performance.get('average_return_4d_pct'), raw_percent=True)} |",
            f"| 4日命中率 | {_pct(performance.get('hit_rate_4d'))} |",
            "",
            "## 数据与风险提示",
            "",
        ]
    )
    warnings = list(payload.get("warnings") or [])
    missing = list((regime.get("details") or {}).get("missing_fields") or [])
    warnings.extend(f"大盘上下文缺失字段：{item}" for item in missing)
    if warnings:
        lines.extend(f"- {item}" for item in warnings)
    else:
        lines.append("- 无显著数据缺失或执行异常。")
    lines.append("")
    return "\n".join(lines)


def _trade_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["当日无成交。"]
    lines = [
        "| 时间 | 代码 | 名称 | 方向 | 数量 | 价格 | 实现盈亏 | 原因 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {time} | {code} | {name} | {side} | {qty} | {price} | {pnl} | {reason} |".format(
                time=str(row.get("trade_time") or ""),
                code=str(row.get("ts_code") or ""),
                name=str(row.get("name") or ""),
                side=str(row.get("side") or ""),
                qty=_fmt(row.get("quantity"), digits=0),
                price=_money(row.get("price")),
                pnl=_money(row.get("realized_pnl")),
                reason=_cell(row.get("reason")),
            )
        )
    return lines


def _order_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["当日无订单。"]
    lines = [
        "| 时间 | 代码 | 名称 | 方向 | 数量 | 价格 | 状态 | 原因 |",
        "|---|---|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {time} | {code} | {name} | {side} | {qty} | {price} | {status} | {reason} |".format(
                time=str(row.get("trade_time") or row.get("created_at") or ""),
                code=str(row.get("ts_code") or ""),
                name=str(row.get("name") or ""),
                side=str(row.get("side") or ""),
                qty=_fmt(row.get("quantity"), digits=0),
                price=_money(row.get("price")),
                status=str(row.get("status") or ""),
                reason=_cell(row.get("reason")),
            )
        )
    return lines


def _position_table(rows: list[dict[str, Any]], plans_by_id: dict[str, dict[str, Any]]) -> list[str]:
    if not rows:
        return ["收盘无持仓。"]
    lines = [
        "| 代码 | 名称 | 数量 | 可用 | 成本 | 现价 | 盈亏 | 止损 | 止盈1 | 止盈2 | 计划有效期 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        plan = plans_by_id.get(str(row.get("plan_id") or "")) or {}
        lines.append(
            "| {code} | {name} | {qty} | {available} | {cost} | {price} | {pnl} | {stop} | {tp1} | {tp2} | {valid} |".format(
                code=str(row.get("ts_code") or ""),
                name=str(row.get("name") or ""),
                qty=_fmt(row.get("quantity"), digits=0),
                available=_fmt(row.get("available_quantity"), digits=0),
                cost=_money(row.get("cost_price")),
                price=_money(row.get("price")),
                pnl=_money(row.get("pnl")),
                stop=_money(plan.get("stop_loss")),
                tp1=_money(plan.get("take_profit_1")),
                tp2=_money(plan.get("take_profit_2")),
                valid=str(plan.get("valid_until") or ""),
            )
        )
    return lines


def _candidate_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["当日无候选记录。"]
    lines = [
        "| 排名 | 入选 | 代码 | 名称 | 行业 | 评分 | 入场 | 止损 | 生效日 | 有效期 |",
        "|---:|---|---|---|---|---:|---:|---:|---|---|",
    ]
    for row in sorted(rows, key=lambda item: int(item.get("rank_no") or 0)):
        lines.append(
            "| {rank} | {selected} | {code} | {name} | {industry} | {score} | {entry} | {stop} | {effective} | {valid} |".format(
                rank=int(row.get("rank_no") or 0),
                selected="是" if row.get("selected") else "否",
                code=str(row.get("ts_code") or ""),
                name=str(row.get("name") or ""),
                industry=str(row.get("industry") or ""),
                score=_fmt(row.get("total_score")),
                entry=_money(row.get("entry_price")),
                stop=_money(row.get("stop_loss")),
                effective=str(row.get("effective_trade_date") or ""),
                valid=str(row.get("valid_until") or ""),
            )
        )
    return lines


def _intent_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["无实盘待确认或近期拒绝委托。"]
    lines = [
        "| 创建时间 | 代码 | 名称 | 方向 | 数量 | 参考价 | 状态 | 原因 |",
        "|---|---|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {time} | {code} | {name} | {side} | {qty} | {price} | {status} | {reason} |".format(
                time=str(row.get("created_at") or ""),
                code=str(row.get("ts_code") or ""),
                name=str(row.get("name") or ""),
                side=str(row.get("side") or ""),
                qty=_fmt(row.get("quantity"), digits=0),
                price=_money(row.get("reference_price")),
                status=str(row.get("status") or ""),
                reason=_cell(row.get("reason")),
            )
        )
    return lines


def _review_request_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["无 Portfolio 复核请求。"]
    lines = [
        "| 请求时间 | 代码 | 名称 | 类型 | 状态 | 价格 | 原因 |",
        "|---|---|---|---|---|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {time} | {code} | {name} | {kind} | {status} | {price} | {reason} |".format(
                time=str(row.get("requested_at") or ""),
                code=str(row.get("ts_code") or ""),
                name=str(row.get("name") or ""),
                kind=str(row.get("trigger_type") or ""),
                status=str(row.get("status") or ""),
                price=_money(row.get("price")),
                reason=_cell(row.get("reason")),
            )
        )
    return lines


def _fmt(value: Any, *, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if digits <= 0:
        return f"{number:.0f}"
    return f"{number:.{digits}f}"


def _money(value: Any) -> str:
    return _fmt(value, digits=2)


def _pct(value: Any, *, raw_percent: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not raw_percent:
        number *= 100.0
    return f"{number:.2f}%"


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
