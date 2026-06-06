from __future__ import annotations

from typing import Any

from sats.agent.models import TradeIntent
from sats.agent.tools.base import AgentToolContext, AgentToolResult, AgentToolSpec, object_schema


def trade_tool_specs() -> list[AgentToolSpec]:
    return [
        AgentToolSpec(
            name="trade.submit_intent",
            description="把交易意图转为风控校验后的订单；实盘必须显式 --auto-trade、--broker qmt、--live-trading。",
            category="trade",
            side_effect="live_trade",
            requires_trade_permission=True,
            timeout=60,
            input_schema=object_schema(
                {
                    "ts_code": {"type": "string"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "quantity": {"type": "integer"},
                    "price_type": {"type": "string"},
                    "price": {"type": "number"},
                    "reason": {"type": "string"},
                },
                ["ts_code", "side"],
            ),
            executor=_submit_intent,
        ),
        AgentToolSpec(
            name="trade.asset",
            description="查询 QMT 资金资产。",
            category="trade",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(),
            executor=_qmt_command(["qmt", "asset"], "QMT 资产"),
        ),
        AgentToolSpec(
            name="trade.positions",
            description="查询 QMT 持仓。",
            category="trade",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema(),
            executor=_qmt_command(["qmt", "positions"], "QMT 持仓"),
        ),
        AgentToolSpec(
            name="trade.orders",
            description="查询 QMT 委托。",
            category="trade",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema({"limit": {"type": "integer"}}),
            executor=_qmt_orders,
        ),
        AgentToolSpec(
            name="trade.trades",
            description="查询 QMT 成交。",
            category="trade",
            side_effect="readonly",
            timeout=30,
            input_schema=object_schema({"limit": {"type": "integer"}}),
            executor=_qmt_trades,
        ),
    ]


def _submit_intent(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    intent = TradeIntent(
        ts_code=str(arguments.get("ts_code") or ""),
        side=str(arguments.get("side") or ""),
        quantity=int(arguments["quantity"]) if arguments.get("quantity") not in (None, "") else None,
        price_type=str(arguments.get("price_type") or "latest"),
        price=float(arguments["price"]) if arguments.get("price") not in (None, "") else None,
        reason=str(arguments.get("reason") or context.message or ""),
        source_step_id=str(arguments.get("source_step_id") or "agent_tool"),
    )
    audit = context.trader.execute(intent)
    return AgentToolResult(
        status="done" if audit.status in {"submitted", "dry_run", "done"} else "error",
        content=audit.message,
        payload=audit.to_dict(),
        data_names=("交易审计",),
    )


def _qmt_command(argv: list[str], data_name: str):
    def execute(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
        result = context.command_runner.run(argv)
        return AgentToolResult(
            status="done" if result.returncode == 0 else "error",
            content=result.output,
            payload={"argv": list(result.argv), "returncode": result.returncode, "status": result.status},
            data_names=(data_name,),
        )

    return execute


def _qmt_orders(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    argv = ["qmt", "orders"]
    if arguments.get("limit") not in (None, ""):
        argv.extend(["--limit", str(arguments.get("limit"))])
    return _qmt_command(argv, "QMT 委托")(context, arguments)


def _qmt_trades(context: AgentToolContext, arguments: dict[str, Any]) -> AgentToolResult:
    argv = ["qmt", "trades"]
    if arguments.get("limit") not in (None, ""):
        argv.extend(["--limit", str(arguments.get("limit"))])
    return _qmt_command(argv, "QMT 成交")(context, arguments)
