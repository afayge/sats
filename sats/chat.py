from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from sats.chat_components import (
    STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS,
    STOCK_ANALYSIS_DEFAULT_RAG_LIMIT,
    ChatEvidenceBundle,
    ChatRequestRoute,
    build_plain_chat_answer,
    chat_tool_definitions,
    collect_chat_evidence,
    execute_chat_tool,
    match_skills_for_route,
    preprocess_chat_route,
    run_internal_analysis_component,
    skill_context,
    synthesize_chat_response,
)
from sats.config import Settings, load_settings
from sats.llm import ChatLLM, LLMResponse, build_light_fallback_llm, build_standard_llm
from sats.memory import ChatMemoryStore, MemoryExtractor, MemoryRecord, MemoryRetriever
from sats.natural_output import build_output_semantic_lexicon, normalize_natural_markdown, render_natural_output
from sats.skills import Skill, default_skills_dir, find_skill, load_skills, match_skills, skill_summaries
from sats.storage.duckdb import DuckDBStorage
from sats.analysis.opportunity_discovery import (
    estimate_llm_message_tokens,
    format_opportunity_discovery,
    is_llm_context_length_error,
    llm_context_input_budget_tokens,
)
from sats.analysis.chan_chat_context import build_chan_chat_context
from sats.analysis.market_llm_context import build_market_llm_context, get_a_share_market_context
from sats.analysis.quote_llm_context import build_stock_quote_llm_context
from sats.analysis.stock_llm_context import build_stock_llm_context
from sats.analysis.stock_picking_agent import run_stock_picking_agent
from sats.analysis.stock_research_context import build_stock_research_context
from sats.chat_planner import build_chat_plan, skills_for_plan
from sats.chat_preprocessor import ChatPreprocessResult, preprocess_chat_message
from sats.chat_events import ChatEventSink, ChatTurnRecorder
from sats.chat_reference import ChatReferenceContext
from sats.chat_runtime import (
    ChatResearchRuntime,
    RuntimeContext,
    RuntimeResult,
    build_research_plan,
    is_runtime_request,
)
from sats.data.astock_provider import AStockDataProvider
from sats.factors.profiles import DEFAULT_FACTOR_PROFILE, FACTOR_PROFILE_CHOICES
from sats.factors.service import snapshot_from_screening_inputs, summarize_factor_exposure
from sats.screening.rule_composer import generate_rule_code
from sats.signals import SignalInput
from sats.skill_routing import collections_for_skill_ids
from sats.stock_question import StockQuestion, extract_trade_date
from sats.symbols import normalize_symbols

SYSTEM_PROMPT = (
    "你是 SATS CLI 助手。你可以解释 SATS 的功能、筛选规则、数据源、命令用法和实现思路，"
    "也可以建议用户运行哪些命令。不要声称已经执行任何命令；只有斜杠命令、一次性 CLI 命令或已注入的只读研究上下文才代表真实执行过数据获取/分析。"
    "不得编造实时行情、价格、涨跌幅、均线、成交量、财务数据、新闻、公告或题材；"
    "market_breadth.total_amount 若 amount_basis=intraday_cumulative，只能表述为截至当前累计成交额；"
    "只有 turnover_comparison.status=ok 时，才能写放量、缩量、成交萎缩或成交放大，且不得把盘中累计成交额直接与前一交易日全天成交额比较。"
    "没有 SATS 命令或结构化数据结果时，只能说明无法获取真实行情并建议具体命令。"
    "对自然语言选股问题，SATS 会先运行本地选股 Agent，用 skills/RAG 理解约束，再用 Analyze 中短期上涨信号做临时全市场筛选并注入候选上下文；"
    "skills/RAG 只提供方法论，真实候选必须来自结构化行情；热点板块只做优先加权，不能把热点或题材当作上涨保证；"
    "只能把候选表达为观察名单、触发条件和失效条件，不能保证上涨。"
    "因子分数、因子画像和 ML 预测只作为研究证据和排序辅助，不能解释为确定收益或买卖指令；"
    "当用户要求新增、生成或创建筛选规则时，必须先展示规则生成计划和确认口令；"
    "只有用户明确回复“确认生成规则 <rule_name>”后，SATS 才会写入 generated 目录并注册规则。"
    "涉及股票、交易或投资判断时，必须说明内容不构成投资建议。回答保持简洁、直接、可操作。"
)

@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    skill_names: tuple[str, ...]
    memory_count: int = 0
    tool_call_count: int = 0
    data_names: tuple[str, ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    requires_confirmation: bool = False
    pending_action_id: str | None = None
    turn_id: str | None = None
    session_id: str = ""


class ChatSession:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        skills: list[Skill] | None = None,
        llm_factory: Callable[..., Any] | None = None,
        memory_store: ChatMemoryStore | None = None,
        memory_extractor: MemoryExtractor | None = None,
        memory_enabled: bool = True,
        session_id: str = "default",
        max_history_messages: int = 12,
        summary_threshold_messages: int = 24,
        summary_refresh_messages: int = 8,
        tools_enabled: bool = True,
        max_tool_iterations: int = 4,
        progress: Any | None = None,
        preprocess_enabled: bool = True,
        knowledge: str | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        project_root = Path(getattr(self.settings, "project_root", "."))
        self.skills = skills if skills is not None else load_skills(default_skills_dir(project_root))
        self.llm_factory = llm_factory or ChatLLM
        self.memory_enabled = memory_enabled
        self._memory_store = memory_store
        self.memory_extractor = memory_extractor or MemoryExtractor()
        self.session_id = session_id
        self.max_history_messages = max_history_messages
        self.summary_threshold_messages = summary_threshold_messages
        self.summary_refresh_messages = max(1, summary_refresh_messages)
        self.tools_enabled = tools_enabled
        self.max_tool_iterations = max(1, max_tool_iterations)
        self.progress = progress
        self.preprocess_enabled = preprocess_enabled
        self.knowledge = knowledge
        self.history: list[dict[str, str]] = []
        self._history_loaded = False
        self._llm: Any | None = None
        self._standard_llm: Any | None = None
        self._light_llm: Any | None = None
        self._memory_lock = threading.Lock()
        self._last_stock_question: StockQuestion | None = None

    def ask(
        self,
        message: str,
        *,
        use_memory: bool | None = None,
        progress: Any | None = None,
        reference_context: ChatReferenceContext | None = None,
        defer_memory_updates: bool = False,
        event_sink: ChatEventSink | None = None,
    ) -> ChatResult:
        active_progress = progress if progress is not None else self.progress
        text = str(message or "").strip()
        if not text:
            raise ValueError("chat message is required")
        effective_memory = self.memory_enabled if use_memory is None else use_memory
        store = self._ensure_memory_store() if effective_memory else None
        recorder = ChatTurnRecorder(
            session_id=self.session_id,
            request=text,
            store=store,
            event_sink=event_sink,
        )
        started_at = time.monotonic()
        recorder.start(payload={"memory_enabled": bool(store), "session_id": self.session_id})
        try:
            if store is not None:
                try:
                    self._load_persisted_history(store)
                except Exception:
                    store = None
                    recorder.store = None
            try:
                memories = MemoryRetriever(store).retrieve(text) if store is not None else []
                summary = store.get_session_summary(self.session_id) if store is not None else ""
            except Exception:
                store = None
                recorder.store = None
                memories = []
                summary = ""
            matched = match_skills(text, self.skills)
            if active_progress is None:
                preprocess, resolved_stock_question, plan, route = preprocess_chat_route(
                    text,
                    settings=self.settings,
                    skills=self.skills,
                    llm_factory=self.llm_factory,
                    preprocess_enabled=self.preprocess_enabled,
                    reference_context=reference_context,
                    explicit_knowledge=self.knowledge,
                    last_stock_question=self._last_stock_question,
                )
            else:
                with active_progress.step("输入分析") as step:
                    preprocess, resolved_stock_question, plan, route = preprocess_chat_route(
                        text,
                        settings=self.settings,
                        skills=self.skills,
                        llm_factory=self.llm_factory,
                        preprocess_enabled=self.preprocess_enabled,
                        reference_context=reference_context,
                        explicit_knowledge=self.knowledge,
                        last_stock_question=self._last_stock_question,
                    )
                    step.complete(message=route.route_kind)
            if preprocess.missing_questions:
                content = _format_preprocess_questions(preprocess.missing_questions)
                recorder.emit(
                    "clarification_required",
                    item_type="plan",
                    item_name="preprocess",
                    status="done",
                    content=content,
                    payload={"questions": list(preprocess.missing_questions)},
                )
                result = replace(self._direct_result(text, content), turn_id=recorder.turn_id, session_id=self.session_id)
                recorder.complete(
                    content=result.content,
                    intent="clarification_required",
                    model_name=_main_model_name(self.settings),
                    duration_seconds=max(0.0, time.monotonic() - started_at),
                )
                return result
            route = replace(route, requires_runtime=is_runtime_request(text))
            plan_symbols = list(route.symbols)
            plan_trade_date = route.trade_date
            recorder.emit(
                "plan_ready",
                item_type="plan",
                item_name=route.route_kind,
                status="done",
                content=route.reason,
                payload={
                    "route": route.to_dict(),
                    "intent": plan.intent,
                    "preprocess": _preprocess_event_payload(preprocess),
                },
            )
            if active_progress is None:
                matched = match_skills_for_route(route, self.skills, matched=matched)
            else:
                with active_progress.step("加载 skill") as step:
                    matched = match_skills_for_route(route, self.skills, matched=matched)
                    step.complete(message=",".join(skill.name for skill in matched) or "none")
            recorder.emit(
                "context_completed",
                item_type="skills",
                item_name="matched_skills",
                status="done",
                payload={"skills": [skill.name for skill in matched]},
            )
            if resolved_stock_question is None and _is_stock_followup(text):
                content = "请先提供明确股票代码，例如：用缠论分析 002436。"
                recorder.emit(
                    "clarification_required",
                    item_type="plan",
                    item_name="stock_followup",
                    status="done",
                    content=content,
                    payload={"reason": "missing_stock_for_followup"},
                )
                result = ChatResult(
                    content=content,
                    skill_names=tuple(skill.name for skill in matched),
                    memory_count=len(memories),
                    turn_id=recorder.turn_id,
                    session_id=self.session_id,
                )
                recorder.complete(
                    content=result.content,
                    intent=plan.intent,
                    skill_names=result.skill_names,
                    model_name=_main_model_name(self.settings),
                    duration_seconds=max(0.0, time.monotonic() - started_at),
                )
                return result
            if route.requires_runtime:
                runtime_plan = build_research_plan(text, symbols=tuple(plan_symbols))
                runtime = ChatResearchRuntime(
                    settings=self.settings,
                    store=store,
                    recorder=recorder,
                    llm=self._llm_client(),
                    tool_registry=ChatToolRegistry(self.skills, self.settings),
                    max_iterations=self.max_tool_iterations,
                )
                runtime_result = runtime.run(
                    RuntimeContext(
                        session_id=self.session_id,
                        turn_id=recorder.turn_id,
                        request=text,
                        project_root=Path(getattr(self.settings, "project_root", ".")),
                        symbols=tuple(plan_symbols),
                        trade_date=str(plan_trade_date or ""),
                        memory_enabled=bool(store),
                    ),
                    plan=runtime_plan,
                )
                user_message_id = ""
                assistant_message_id = ""
                if store is not None:
                    try:
                        store.touch_memories([memory.memory_id for memory in memories])
                        user_message_id = store.add_message(self.session_id, "user", text)
                        assistant_message_id = store.add_message(self.session_id, "assistant", runtime_result.content)
                    except Exception:
                        store = None
                self._append_history("user", text)
                self._append_history("assistant", runtime_result.content)
                if store is not None:
                    if defer_memory_updates:
                        self._defer_memory_maintenance(store, text, runtime_result.content)
                    else:
                        self._maintain_memory(store, text, runtime_result.content)
                result = _chat_result_from_runtime(
                    runtime_result,
                    skill_names=tuple(skill.name for skill in matched),
                    memory_count=len(memories),
                    turn_id=recorder.turn_id,
                    session_id=self.session_id,
                )
                recorder.complete(
                    content=result.content,
                    intent=f"runtime:{runtime_result.plan.workflow_kind}",
                    symbols=plan_symbols,
                    trade_date=plan_trade_date,
                    data_names=result.data_names,
                    skill_names=result.skill_names,
                    model_name=_main_model_name(self.settings),
                    tool_call_count=result.tool_call_count,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    duration_seconds=max(0.0, time.monotonic() - started_at),
                    meta={
                        "runtime_plan": runtime_result.plan.to_dict(),
                        "phase": "runtime",
                        "model_policy": "standard",
                        "model_profile": "default",
                        "model_name": _main_model_name(self.settings),
                    },
                )
                return result
            evidence = collect_chat_evidence(
                text,
                route=route,
                settings=self.settings,
                skills=self.skills,
                explicit_knowledge=self.knowledge,
                store=store,
                session_id=self.session_id,
                reference_context=reference_context,
                progress=active_progress,
                recorder=recorder,
            )
            if evidence.stock_context is not None:
                context_question = getattr(evidence.stock_context, "question", resolved_stock_question)
                context_trade_date = str(getattr(evidence.stock_context, "trade_date", "") or getattr(context_question, "trade_date", "") or "")
                self._last_stock_question = StockQuestion(
                    symbols=list(getattr(context_question, "symbols", ()) or route.symbols),
                    trade_date=context_trade_date or None,
                    as_of_time=getattr(context_question, "as_of_time", None),
                    has_stock_question=True,
                )
            synthesis = synthesize_chat_response(
                text,
                route=route,
                evidence=evidence,
                settings=self.settings,
                llm_factory=self.llm_factory,
                skills=matched,
                history=self.history,
                memories=memories,
                session_summary=summary,
                tool_chat=(
                    lambda messages: self._chat_with_optional_tools(
                        messages,
                        progress=active_progress,
                        event_recorder=recorder,
                    )
                )
                if route.route_kind == "general_qa"
                else None,
                progress=active_progress,
            )
            content = synthesis.content or "无响应"
            tool_call_count = synthesis.tool_call_count
            user_message_id = ""
            assistant_message_id = ""
            if store is not None:
                try:
                    store.touch_memories([memory.memory_id for memory in memories])
                    user_message_id = store.add_message(self.session_id, "user", text)
                    assistant_message_id = store.add_message(self.session_id, "assistant", content)
                except Exception:
                    store = None
            self._append_history("user", text)
            self._append_history("assistant", content)
            if store is not None:
                if defer_memory_updates:
                    self._defer_memory_maintenance(store, text, content)
                else:
                    self._maintain_memory(store, text, content)
            result = ChatResult(
                content=content,
                skill_names=tuple(skill.name for skill in matched),
                memory_count=len(memories),
                tool_call_count=tool_call_count,
                data_names=evidence.data_names,
                sources=evidence.sources,
                artifacts=evidence.artifacts,
                requires_confirmation=evidence.requires_confirmation,
                pending_action_id=evidence.pending_action_id,
                turn_id=recorder.turn_id,
                session_id=self.session_id,
            )
            recorder.complete(
                content=result.content,
                intent=route.intent,
                symbols=plan_symbols,
                trade_date=evidence.completion_trade_date or plan_trade_date,
                data_names=result.data_names,
                skill_names=result.skill_names,
                model_name=synthesis.model_name or _main_model_name(self.settings),
                tool_call_count=tool_call_count,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                duration_seconds=max(0.0, time.monotonic() - started_at),
                meta={
                    "route": route.to_dict(),
                    "evidence": evidence.to_dict(),
                    "phase": synthesis.phase,
                    "model_policy": synthesis.model_policy,
                    "model_profile": synthesis.model_profile,
                    "model_name": synthesis.model_name,
                },
            )
            return result
        except KeyboardInterrupt as exc:
            recorder.fail(exc, status="interrupted", duration_seconds=max(0.0, time.monotonic() - started_at))
            raise
        except Exception as exc:
            recorder.fail(exc, duration_seconds=max(0.0, time.monotonic() - started_at))
            raise

    def _direct_result(self, user_text: str, content: str, *, data_names: tuple[str, ...] = ()) -> ChatResult:
        self._append_history("user", user_text)
        self._append_history("assistant", content)
        return ChatResult(content=content, skill_names=(), data_names=data_names)

    def _llm_client(self) -> Any:
        if self._standard_llm is None:
            self._standard_llm = build_standard_llm(
                self.llm_factory,
                model_name=_main_model_name(self.settings),
                timeout_seconds=_llm_timeout_seconds(self.settings),
            )
        self._llm = self._standard_llm
        return self._standard_llm

    def _light_llm_client(self) -> Any:
        if self._light_llm is None:
            self._light_llm = build_light_fallback_llm(
                self.llm_factory,
                light_model_name=_light_model_name(self.settings),
                default_model_name=_main_model_name(self.settings),
                timeout_seconds=_llm_timeout_seconds(self.settings),
            )
        return self._light_llm

    def _chat_with_optional_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        progress: Any | None = None,
        event_recorder: ChatTurnRecorder | None = None,
    ) -> tuple[Any, int, dict[str, str]]:
        llm = self._light_llm_client()
        self._llm = llm
        model_name = _light_model_name(self.settings)
        if not self.tools_enabled:
            try:
                if progress is None:
                    response = llm.chat(messages)
                    return response, 0, _chat_model_meta(llm, settings=self.settings, policy="light")
                with progress.step(f"{model_name} LLM") as step:
                    response = llm.chat(messages)
                    step.complete()
                return response, 0, _chat_model_meta(llm, settings=self.settings, policy="light")
            except Exception as exc:
                if _is_timeout_exception(exc):
                    return (
                        LLMResponse(content=_timeout_message(_timeout_model_name(self.settings))),
                        0,
                        _chat_model_meta(llm, settings=self.settings, policy="light"),
                    )
                raise
        registry = ChatToolRegistry(self.skills, self.settings)
        definitions = registry.definitions()
        try:
            if progress is None:
                response = llm.chat(messages, tools=definitions)
            else:
                with progress.step(f"{model_name} LLM") as step:
                    response = llm.chat(messages, tools=definitions)
                    step.complete()
        except TypeError:
            if progress is None:
                response = llm.chat(messages)
                return response, 0, _chat_model_meta(llm, settings=self.settings, policy="light")
            with progress.step(f"{model_name} LLM") as step:
                response = llm.chat(messages)
                step.complete()
            return response, 0, _chat_model_meta(llm, settings=self.settings, policy="light")
        except Exception as exc:
            if _is_timeout_exception(exc):
                return (
                    LLMResponse(content=_timeout_message(_timeout_model_name(self.settings))),
                    0,
                    _chat_model_meta(llm, settings=self.settings, policy="light"),
                )
            raise
        tool_call_count = 0
        for _ in range(self.max_tool_iterations):
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                return response, tool_call_count, _chat_model_meta(llm, settings=self.settings, policy="light")
            messages.append(_assistant_tool_call_message(response))
            for call in tool_calls:
                tool_call_count += 1
                tool_started = time.monotonic()
                if event_recorder is not None:
                    event_recorder.emit(
                        "tool_started",
                        item_type="tool",
                        item_name=str(call.name),
                        status="running",
                        payload={
                            "tool": str(call.name),
                            "arguments": _tool_event_arguments(call.arguments),
                            "metadata": registry.metadata(str(call.name)),
                        },
                    )
                if progress is None:
                    tool_result = registry.execute(call.name, call.arguments)
                else:
                    with progress.step(f"工具 {call.name}") as step:
                        tool_result = registry.execute(call.name, call.arguments)
                        step.complete()
                if event_recorder is not None:
                    event_recorder.emit(
                        "tool_completed",
                        item_type="tool",
                        item_name=str(call.name),
                        status=_tool_result_event_status(tool_result),
                        payload={
                            "tool": str(call.name),
                            "result_size": len(str(tool_result or "")),
                        },
                        duration_seconds=max(0.0, time.monotonic() - tool_started),
                    )
                messages.append(_tool_result_message(call, tool_result))
            if progress is None:
                try:
                    response = llm.chat(messages, tools=definitions)
                except Exception as exc:
                    if _is_timeout_exception(exc):
                        return (
                            LLMResponse(content=_timeout_message(_timeout_model_name(self.settings))),
                            tool_call_count,
                            _chat_model_meta(llm, settings=self.settings, policy="light"),
                        )
                    raise
            else:
                with progress.step(f"{model_name} LLM") as step:
                    try:
                        response = llm.chat(messages, tools=definitions)
                    except Exception as exc:
                        if _is_timeout_exception(exc):
                            step.complete(message="Request timed out.")
                            return (
                                LLMResponse(content=_timeout_message(_timeout_model_name(self.settings))),
                                tool_call_count,
                                _chat_model_meta(llm, settings=self.settings, policy="light"),
                            )
                        raise
                    step.complete()
        if not str(getattr(response, "content", "") or "").strip():
            response.content = "工具调用已达到上限，请缩小问题范围后重试。"
        return response, tool_call_count, _chat_model_meta(llm, settings=self.settings, policy="light")

    def _append_history(self, role: str, content: str) -> None:
        if self.max_history_messages <= 0:
            return
        self.history.append({"role": role, "content": content})
        if len(self.history) > self.max_history_messages:
            self.history = self.history[-self.max_history_messages :]

    def _ensure_memory_store(self) -> ChatMemoryStore | None:
        if not self.memory_enabled:
            return None
        if self._memory_store is None:
            db_path = getattr(self.settings, "db_path", None)
            if db_path is None:
                return None
            self._memory_store = ChatMemoryStore(db_path)
        try:
            self._memory_store.ensure_session(self.session_id, model_name=getattr(self.settings, "openai_model", ""))
        except Exception:
            return None
        return self._memory_store

    def _load_persisted_history(self, store: ChatMemoryStore) -> None:
        if self._history_loaded:
            return
        self.history = store.list_recent_messages(self.session_id, limit=self.max_history_messages)
        self._history_loaded = True

    def _extract_memories(self, store: ChatMemoryStore, user_message: str, assistant_message: str) -> None:
        try:
            candidates = self.memory_extractor.extract(user_message, assistant_message, llm=self._light_llm_client())
        except Exception:
            return
        for candidate in candidates:
            try:
                store.add_memory(
                    content=candidate.content,
                    memory_type=candidate.memory_type,
                    tags=candidate.tags,
                    importance=candidate.importance,
                    source_session_id=self.session_id,
                )
            except ValueError:
                continue

    def _maybe_update_summary(self, store: ChatMemoryStore) -> None:
        count = store.count_session_messages(self.session_id)
        if count < self.summary_threshold_messages or count % self.summary_refresh_messages != 0:
            return
        try:
            summary = self.memory_extractor.summarize(
                store.get_session_summary(self.session_id),
                store.list_recent_messages(self.session_id, limit=self.max_history_messages),
                llm=self._light_llm_client(),
            )
        except Exception:
            return
        if summary:
            store.update_session_summary(self.session_id, summary)

    def _maintain_memory(self, store: ChatMemoryStore, user_message: str, assistant_message: str) -> None:
        try:
            with self._memory_lock:
                self._extract_memories(store, user_message, assistant_message)
                self._maybe_update_summary(store)
        except Exception:
            return

    def _defer_memory_maintenance(self, store: ChatMemoryStore, user_message: str, assistant_message: str) -> None:
        thread = threading.Thread(
            target=self._maintain_memory,
            args=(store, user_message, assistant_message),
            daemon=True,
        )
        thread.start()

    def _build_research_context(
        self,
        message: str,
        *,
        plan_collections: tuple[str, ...],
        explicit_knowledge: str | None,
    ) -> StockResearchContext | None:
        if not explicit_knowledge and not plan_collections:
            return None
        if getattr(self.settings, "db_path", None) is None:
            return None
        try:
            return build_stock_research_context(
                message,
                settings=self.settings,
                knowledge=explicit_knowledge,
                collections=plan_collections,
                limit=_research_context_limit(plan_collections, explicit_knowledge=explicit_knowledge),
            )
        except Exception:
            return None


def run_chat_once(
    message: str,
    *,
    settings: Settings | None = None,
    skills: list[Skill] | None = None,
    llm_factory: Callable[..., Any] | None = None,
    memory_enabled: bool = True,
    progress: Any | None = None,
    knowledge: str | None = None,
    tools_enabled: bool = True,
    preprocess_enabled: bool = True,
) -> ChatResult:
    session = ChatSession(
        settings=settings,
        skills=skills,
        llm_factory=llm_factory,
        memory_enabled=memory_enabled,
        max_history_messages=0,
        progress=progress,
        knowledge=knowledge,
        tools_enabled=tools_enabled,
        preprocess_enabled=preprocess_enabled,
    )
    return session.ask(message)


def _chat_result_from_runtime(
    runtime_result: RuntimeResult,
    *,
    skill_names: tuple[str, ...] = (),
    memory_count: int = 0,
    turn_id: str | None = None,
    session_id: str = "",
) -> ChatResult:
    pending = runtime_result.pending_action
    return ChatResult(
        content=runtime_result.content,
        skill_names=skill_names,
        memory_count=memory_count,
        tool_call_count=runtime_result.tool_call_count,
        data_names=runtime_result.data_names,
        artifacts=tuple(artifact.to_dict() for artifact in runtime_result.artifacts),
        requires_confirmation=pending is not None,
        pending_action_id=pending.action_id if pending is not None else None,
        turn_id=turn_id,
        session_id=session_id,
    )


def build_chat_messages(
    message: str,
    *,
    history: list[dict[str, str]] | None = None,
    skills: list[Skill] | None = None,
    plan_context: str = "",
    memories: list[MemoryRecord] | None = None,
    session_summary: str = "",
    stock_context: str = "",
    market_context: str = "",
    opportunity_context: str = "",
    chan_context: str = "",
    research_context: str = "",
    quote_context: str = "",
    reference_context: str = "",
    preprocess_context: str = "",
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if preprocess_context:
        messages.append({"role": "system", "content": preprocess_context})
    if plan_context:
        messages.append({"role": "system", "content": plan_context})
    if skills:
        messages.append({"role": "system", "content": _skills_context(skills)})
    if stock_context:
        messages.append({"role": "system", "content": stock_context})
    if market_context:
        messages.append({"role": "system", "content": market_context})
    if opportunity_context:
        messages.append({"role": "system", "content": opportunity_context})
    if chan_context:
        messages.append({"role": "system", "content": chan_context})
    if research_context:
        messages.append({"role": "system", "content": research_context})
    if quote_context:
        messages.append({"role": "system", "content": quote_context})
    if reference_context:
        messages.append({"role": "system", "content": reference_context})
    if memories:
        messages.append({"role": "system", "content": _memory_context(memories)})
    if session_summary:
        messages.append({"role": "system", "content": f"当前会话摘要：\n{session_summary}"})
    messages.extend(history or [])
    messages.append({"role": "user", "content": message})
    return messages


@dataclass(frozen=True, slots=True)
class ConversationContext:
    messages: list[dict[str, str]]
    sources: tuple[dict[str, Any], ...] = ()


class ConversationContextBuilder:
    def build(
        self,
        *,
        message: str,
        history: list[dict[str, str]] | None = None,
        skills: list[Skill] | None = None,
        plan_context: str = "",
        memories: list[MemoryRecord] | None = None,
        session_summary: str = "",
        stock_context: str = "",
        market_context: str = "",
        opportunity_context: str = "",
        chan_context: str = "",
        research_context: str = "",
        quote_context: str = "",
        reference_context: str = "",
        preprocess_context: str = "",
        sources: tuple[dict[str, Any], ...] = (),
    ) -> ConversationContext:
        messages = build_chat_messages(
            message,
            history=history,
            skills=skills,
            plan_context=plan_context,
            memories=memories,
            session_summary=session_summary,
            stock_context=stock_context,
            market_context=market_context,
            opportunity_context=opportunity_context,
            chan_context=chan_context,
            research_context=research_context,
            quote_context=quote_context,
            reference_context=reference_context,
            preprocess_context=preprocess_context,
        )
        return ConversationContext(messages=messages, sources=sources)


def _stock_context_message(stock_context: Any | None, market_context: Any | None = None) -> str:
    parts = []
    if stock_context is not None:
        parts.append(str(getattr(stock_context, "system_message", "") or ""))
    if market_context is not None:
        parts.append(str(getattr(market_context, "system_message", "") or ""))
    return "\n".join(part for part in parts if part)


def _context_system_message_for_llm(context: Any | None) -> str:
    if context is None:
        return ""
    builder = getattr(context, "system_message_for_llm", None)
    if callable(builder):
        return str(builder() or "")
    return str(getattr(context, "system_message", "") or "")


def _context_payload_for_llm(context: Any) -> Any:
    builder = getattr(context, "to_llm_context", None)
    if callable(builder):
        return builder()
    payload = getattr(context, "payload", None)
    if payload is not None:
        return payload
    to_dict = getattr(context, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {}


def _fit_chat_messages_to_context_budget(
    messages: list[dict[str, Any]],
    *,
    settings: Settings,
    opportunity_context: Any | None = None,
) -> list[dict[str, Any]] | None:
    fitted, _ = _fit_chat_messages_to_context_budget_with_metadata(
        messages,
        settings=settings,
        opportunity_context=opportunity_context,
    )
    return fitted


def _fit_chat_messages_to_context_budget_with_metadata(
    messages: list[dict[str, Any]],
    *,
    settings: Settings,
    opportunity_context: Any | None = None,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    budget = llm_context_input_budget_tokens(settings)
    original_tokens = _estimate_chat_messages_tokens(messages)
    meta: dict[str, Any] = {
        "budget_tokens": budget,
        "input_tokens": original_tokens,
        "message_count": len(messages),
        "compressed": False,
        "skipped_final_llm": False,
        "compressed_context": "",
    }
    if original_tokens <= budget:
        return messages, meta
    if opportunity_context is None:
        meta["over_budget_kept"] = True
        return messages, meta
    compact = [message for index, message in enumerate(messages) if _is_essential_discover_message(message, index=index)]
    compact_tokens = _estimate_chat_messages_tokens(compact)
    meta.update(
        {
            "compressed": True,
            "compressed_context": "opportunity_discovery",
            "compact_tokens": compact_tokens,
            "compact_message_count": len(compact),
        }
    )
    if compact_tokens <= budget:
        return compact, meta
    meta["skipped_final_llm"] = True
    return None, meta


def _estimate_chat_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(
        estimate_llm_message_tokens(str(message.get("role", "")))
        + estimate_llm_message_tokens(str(message.get("content", "")))
        for message in messages
    )


def _is_essential_discover_message(message: dict[str, Any], *, index: int) -> bool:
    content = str(message.get("content", "") or "")
    role = str(message.get("role", "") or "")
    if index == 0 or role == "user":
        return True
    return any(
        marker in content
        for marker in (
            "SATS chat_preprocess",
            "SATS chat_plan",
            "短线机会发现精简上下文",
            "选股 Agent 基于真实本地数据",
        )
    )


def _preprocess_event_payload(preprocess: ChatPreprocessResult | None) -> dict[str, Any]:
    if preprocess is None:
        return {}
    return {
        "source": str(getattr(preprocess, "source", "") or "local"),
        "intent": str(getattr(preprocess, "intent", "") or "general_qa"),
        "symbols": list(getattr(preprocess, "symbols", ()) or ()),
        "stock_names": list(getattr(preprocess, "stock_names", ()) or ()),
        "trade_date": getattr(preprocess, "trade_date", None) or "",
        "as_of_time": getattr(preprocess, "as_of_time", None) or "",
        "reference_needed": bool(getattr(preprocess, "reference_needed", False)),
        "needs_stock_context": bool(getattr(preprocess, "needs_stock_context", False)),
        "needs_market_context": bool(getattr(preprocess, "needs_market_context", False)),
        "needs_opportunity_discovery": bool(getattr(preprocess, "needs_opportunity_discovery", False)),
        "needs_indicators": bool(getattr(preprocess, "needs_indicators", False)),
        "needs_realtime_quote_context": getattr(preprocess, "needs_realtime_quote_context", False),
        "market_indices": list(getattr(preprocess, "market_indices", ()) or ()),
        "market_dimensions": list(getattr(preprocess, "market_dimensions", ()) or ()),
        "market_horizons": list(getattr(preprocess, "market_horizons", ()) or ()),
        "requested_limit": getattr(preprocess, "requested_limit", None),
        "skill_hints": list(getattr(preprocess, "skill_hints", ()) or ()),
        "confidence": float(getattr(preprocess, "confidence", 0.0) or 0.0),
    }


def _tool_event_arguments(arguments: Any) -> Any:
    if isinstance(arguments, (dict, list, tuple)):
        text = json.dumps(arguments, ensure_ascii=False, default=str)
        return arguments if len(text) <= 2000 else {"truncated_json": text[:2000]}
    text = str(arguments or "")
    return text if len(text) <= 2000 else text[:2000]


def _tool_result_event_status(result: str) -> str:
    try:
        payload = json.loads(str(result or ""))
    except Exception:
        return "done"
    if isinstance(payload, dict) and str(payload.get("status") or "").lower() == "error":
        return "error"
    return "done"


def format_chat_result(result: ChatResult) -> str:
    return _append_source_table(
        normalize_natural_markdown(
            result.content,
            data_names=result.data_names,
            skill_names=result.skill_names,
            artifacts=result.artifacts,
            requires_confirmation=result.requires_confirmation,
            pending_action_id=result.pending_action_id,
        ),
        result.sources,
    )


def _append_source_table(content: str, sources: tuple[dict[str, Any], ...]) -> str:
    rows = []
    seen_urls = set()
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        source_id = str(source.get("id") or f"S{index}")
        title = str(source.get("title") or source.get("domain") or url).replace("]", "\\]")
        meta = " · ".join(
            str(value)
            for value in (source.get("domain"), source.get("backend"), source.get("fetched_at"))
            if str(value or "").strip()
        )
        suffix = f" — {meta}" if meta else ""
        rows.append(f"- [{source_id}] [{title}]({url}){suffix}")
    text = str(content or "").rstrip()
    if not rows or "\n## 来源\n" in f"\n{text}\n":
        return text
    return f"{text}\n\n## 来源\n\n" + "\n".join(rows)


def render_chat_result(
    result: ChatResult,
    *,
    channel: str,
    tty: bool,
    width: int,
    db_path: Path | str | None = None,
):
    markdown_text = format_chat_result(result)
    semantic_lexicon = build_output_semantic_lexicon(markdown_text, db_path=db_path) if tty else None
    return render_natural_output(
        markdown_text,
        channel=channel,
        tty=tty,
        width=width,
        semantic_lexicon=semantic_lexicon,
    )


def _format_preprocess_questions(questions: tuple[str, ...]) -> str:
    lines = ["需要先确认以下信息，才能继续获取真实数据分析："]
    lines.extend(f"- {question}" for question in questions)
    return "\n".join(lines)


def _should_include_preprocess_context(preprocess: ChatPreprocessResult) -> bool:
    return bool(
        preprocess.stock_names
        or preprocess.reference_needed
        or preprocess.needs_opportunity_discovery
        or getattr(preprocess, "needs_realtime_quote_context", False)
        or preprocess.source != "local"
    )


_STOCK_FOLLOWUP_REFERENCES = ("继续", "它", "它们", "这只", "这些", "刚才", "上面")
_STOCK_FOLLOWUP_INTENTS = (
    "分析",
    "走势",
    "怎么看",
    "怎么样",
    "缠论",
    "结构",
    "风险",
    "买",
    "卖",
    "背驰",
    "中枢",
    "指标",
    "均线",
    "macd",
    "kdj",
    "rsi",
)
_RULE_PLAN_REVISION_TERMS = ("改", "调整", "补充", "去掉", "忽略", "不需要", "不要", "加上", "规则名", "rule_name")


def _is_stock_followup(message: str) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    return any(term in text for term in _STOCK_FOLLOWUP_REFERENCES) and any(
        term.lower() in text for term in _STOCK_FOLLOWUP_INTENTS
    )


def _looks_like_rule_plan_revision(message: str) -> bool:
    text = str(message or "").strip()
    return bool(text) and any(term in text for term in _RULE_PLAN_REVISION_TERMS)


def _collections_for_plan(plan: Any) -> tuple[str, ...]:
    collections: list[str] = []
    skills = set(getattr(plan, "skills", ()) or ())
    requirements = set(getattr(plan, "data_requirements", ()) or ())
    actions = set(getattr(plan, "internal_actions", ()) or ())
    intent = str(getattr(plan, "intent", "") or "")
    if "stock_context" in requirements or intent == "stock_analysis":
        return STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS
    dsa_technical_skills = {
        "bull-trend",
        "shrink-pullback",
        "ma-golden-cross",
        "volume-breakout",
        "box-oscillation",
        "bottom-volume",
        "one-yang-three-yin",
        "elliott-wave",
    }
    dsa_market_skills = {"dragon-head", "hot-theme", "emotion-cycle"}
    dsa_fundamental_skills = {"expectation-repricing", "growth-quality"}
    if "chan-theory" in skills or "chan_context" in requirements or intent == "chan_analysis":
        collections.append("chan")
    if "technical-basic" in skills or "stock_context" in requirements or dsa_technical_skills & skills:
        collections.extend(["stock-basic", "technical"])
    if "opportunity_discovery" in actions or "signals" in skills or {"quant-factor-screener", "small-cap-growth-identifier"} & skills or dsa_technical_skills & skills:
        collections.append("signals")
    if "market_context" in requirements or "sats-market-assistant" in skills:
        collections.extend(["market", "sentiment"])
    if {"sector-rotation"} & skills or dsa_market_skills & skills:
        collections.extend(["market", "sentiment"])
    if {"financial-statement", "valuation-model", "fundamental-filter", "quant-factor-screener", "high-dividend-strategy", "undervalued-stock-screener", "small-cap-growth-identifier", "esg-screener", "tech-hype-vs-fundamentals"} & skills or dsa_fundamental_skills & skills:
        collections.append("fundamental")
    if {"event-driven-detector", "insider-trading-analyzer", "sentiment-reality-gap"} & skills or {"expectation-repricing", "emotion-cycle", "hot-theme"} & skills:
        collections.append("sentiment")
    if {"risk-analysis", "portfolio-health-check", "risk-adjusted-return-optimizer", "suitability-report-generator"} & skills:
        collections.append("risk")
    collections.extend(collections_for_skill_ids(skills, intent=intent))
    return tuple(_dedupe(collections))


def _research_context_limit(plan_collections: tuple[str, ...], *, explicit_knowledge: str | None) -> int:
    if explicit_knowledge:
        return 6
    if set(STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS).issubset(plan_collections):
        return STOCK_ANALYSIS_DEFAULT_RAG_LIMIT
    return 6


def _ensure_skill(matched: list[Skill], skills: list[Skill], skill_name: str) -> list[Skill]:
    if any(skill.id == skill_name or skill.name == skill_name for skill in matched):
        return matched
    skill = find_skill(skills, skill_name)
    if skill is None:
        return matched
    return [skill, *matched]


def _skills_context(skills: list[Skill]) -> str:
    blocks = [
        "以下是自动匹配到的 SATS skills 摘要。需要完整指引时，可调用只读工具 load_skill(name) 加载；"
        "skill 只提供上下文，不代表已经执行数据查询、筛选或交易。"
    ]
    for skill in skills:
        blocks.append(
            "\n".join(
                [
                    f"Skill: {skill.name}",
                    f"Category: {skill.category}",
                    f"Description: {skill.description}",
                    f"Triggers: {', '.join(skill.triggers) if skill.triggers else '无'}",
                ]
            )
        )
    return "\n\n".join(blocks)


def _memory_context(memories: list[MemoryRecord]) -> str:
    lines = ["以下是 SATS 本地长期记忆，可能与当前问题相关；如与用户当前输入冲突，以当前输入为准："]
    for memory in memories:
        tags = f" tags={','.join(memory.tags)}" if memory.tags else ""
        lines.append(f"- [{memory.memory_type}] {memory.content}{tags}")
    return "\n".join(lines)


def _create_llm(
    factory: Callable[..., Any],
    model_name: str,
    *,
    profile: str,
    timeout_seconds: int | None = None,
) -> Any:
    try:
        return factory(
            model_name=model_name,
            profile=profile,
            timeout_seconds=timeout_seconds,
        )
    except TypeError:
        try:
            return factory(model_name=model_name, timeout_seconds=timeout_seconds)
        except TypeError:
            try:
                return factory(model_name=model_name, profile=profile)
            except TypeError:
                try:
                    return factory(model_name=model_name)
                except TypeError:
                    return factory()


def _main_model_name(settings: Settings) -> str:
    return str(getattr(settings, "openai_model", "") or "LLM")


def _light_model_name(settings: Settings) -> str:
    return str(getattr(settings, "light_model_name", "") or getattr(settings, "openai_model", "") or "LLM")


def _chat_model_meta(llm: Any, *, settings: Settings, policy: str) -> dict[str, str]:
    fallback_profile = "default" if policy == "standard" else "light"
    fallback_model = _main_model_name(settings) if policy == "standard" else _light_model_name(settings)
    return {
        "phase": "synthesis",
        "model_policy": policy,
        "model_profile": str(getattr(llm, "last_profile", "") or getattr(llm, "profile", "") or fallback_profile),
        "model_name": str(getattr(llm, "last_model_name", "") or getattr(llm, "model_name", "") or fallback_model),
    }


def _timeout_model_name(settings: Settings) -> str:
    light = _light_model_name(settings)
    main = _main_model_name(settings)
    return light if light == main else f"{light} / {main}"


def _llm_timeout_seconds(settings: Settings) -> int | None:
    value = getattr(settings, "llm_timeout_seconds", None)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _is_timeout_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc or "").lower()
    return "timeout" in text or "timed out" in text


def _timeout_message(model_name: str) -> str:
    return (
        f"{model_name} 请求超时。请缩小问题范围、切换更快的模型，或稍后重试。"
        "本次未返回可用的大模型分析结果。"
    )


_SHARED_CHAT_TOOL_NAMES = {
    "list_skills",
    "load_skill",
    "get_a_share_market_context",
    "discover_a_share_opportunities",
    "get_stock_research_context",
    "run_internal_analysis",
    "get_chan_context",
    "get_knowledge_context",
    "run_rule_generation",
}


def _tushare_chat_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_provider_capabilities",
                "description": "列出 SATS 已接入的 TickFlow/Tushare/AkShare 数据能力目录，说明可用场景、入参、输出字段和推荐工具。只读，不写库、不交易。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string", "description": "可选 provider：tickflow、tushare 或 akshare"},
                        "category": {"type": "string", "description": "可选能力分类关键词，如 行情、财务、资金流、分钟K、实时"},
                        "realtime": {"type": "boolean", "description": "可选：true 仅实时能力，false 仅非实时能力"},
                        "compact": {"type": "boolean", "description": "是否返回压缩字段摘要，默认 false"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_akshare_datasets",
                "description": "列出 SATS 白名单 AkShare 全量数据字典接口；用于先发现 dataset，再描述或取数。只读，不写库、不交易。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "可选领域，如 股票数据、期货数据、宏观经济、基金数据"},
                        "category": {"type": "string", "description": "可选分类关键词，如 A股、期货、指数、债券"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签过滤，如 stock、futures、macro、realtime"},
                        "query": {"type": "string", "description": "按 dataset、分类、标签、入参关键词搜索"},
                        "realtime": {"type": "boolean", "description": "可选：true 仅实时接口，false 仅非实时接口"},
                        "compact": {"type": "boolean", "description": "是否返回压缩字段摘要，默认 true"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "describe_akshare_dataset",
                "description": "查看一个 AkShare 白名单 dataset 的分类、标签、入参和文档来源。只读，不写库、不交易。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dataset": {"type": "string", "description": "AkShare dataset id，例如 stock_zh_a_spot_em"},
                    },
                    "required": ["dataset"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_akshare_data",
                "description": "按 AkShare 白名单 dataset 获取结构化数据。只读，不写库、不交易；参数必须为 JSON 安全值。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dataset": {"type": "string", "description": "AkShare dataset id，例如 stock_zh_a_spot_em"},
                        "params": {"type": "object", "description": "AkShare 入参，仅允许该 dataset 声明的参数"},
                        "fields": {"type": "array", "items": {"type": "string"}, "description": "可选字段列表，最多保留 30 个"},
                        "limit": {"type": "integer", "description": "最多返回行数，默认 200，最大 1000"},
                    },
                    "required": ["dataset"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_tushare_datasets",
                "description": "列出 SATS 已白名单接入的 Tushare 数据集，覆盖股票和常用跨类数据。只读，不写库、不交易。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "可选领域，如 股票数据、ETF专题、指数专题、宏观经济"},
                        "category": {"type": "string", "description": "可选分类，如 基础数据、行情数据、财务数据"},
                        "include_deprecated": {
                            "type": "boolean",
                            "description": "是否包含 Tushare 标记停用的数据集，默认 true",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选标签过滤，如 etf、fund、index、macro、news、hk、us",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_tushare_data",
                "description": "按需获取 Tushare 白名单数据集的结构化行数据。只读，不写库、不交易；默认限制行数和字段数。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dataset": {
                            "type": "string",
                            "description": "Tushare 数据集名，例如 daily_basic、index_daily、fund_daily、cn_cpi、news",
                        },
                        "params": {
                            "type": "object",
                            "description": "Tushare 入参，如 ts_code、trade_date、start_date、end_date、ann_date、m",
                        },
                        "fields": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选字段列表；最多保留 30 个白名单字段",
                        },
                        "limit": {"type": "integer", "description": "最多返回行数，默认 200，最大 1000"},
                    },
                    "required": ["dataset"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_tushare_stock_datasets",
                "description": "列出 SATS 已白名单接入的 Tushare 股票数据集及字段摘要。只读，不写库、不交易。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "可选分类，如 基础数据、行情数据、财务数据"},
                        "include_deprecated": {
                            "type": "boolean",
                            "description": "是否包含 Tushare 标记停用的数据集，默认 true",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_tushare_stock_data",
                "description": "按需获取 Tushare 股票数据集的结构化行数据。只读，不写库、不交易；仅支持 SATS 白名单数据集。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dataset": {
                            "type": "string",
                            "description": "Tushare 股票数据集名，例如 daily_basic、income、top_list",
                        },
                        "params": {
                            "type": "object",
                            "description": "Tushare 入参，如 ts_code、trade_date、start_date、end_date、ann_date",
                        },
                        "fields": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选字段列表；省略时使用 SATS 默认字段，避免返回过宽表",
                        },
                        "limit": {"type": "integer", "description": "最多返回行数，默认 200，最大 1000"},
                    },
                    "required": ["dataset"],
                },
            },
        },
    ]


class ChatToolRegistry:
    def __init__(self, skills: list[Skill], settings: Settings) -> None:
        self.skills = skills
        self.settings = settings

    def metadata(self, name: str) -> dict[str, Any]:
        known = {str(item.get("function", {}).get("name") or ""): item for item in self.definitions()}
        definition = known.get(str(name or ""))
        function = definition.get("function", {}) if isinstance(definition, dict) else {}
        tool_name = str(name or "")
        metadata = {
            "name": str(name or ""),
            "description": str(function.get("description") or ""),
            "readonly": True,
            "repeatable": False,
            "requires_confirmation": False,
            "writes_artifact": False,
            "category": "research",
            "max_result_chars": 6000,
        }
        if tool_name.startswith("list_"):
            metadata.update({"repeatable": True, "category": "catalog", "max_result_chars": 4000})
        elif tool_name.startswith("load_skill"):
            metadata.update({"repeatable": True, "category": "skill", "max_result_chars": 12000})
        elif tool_name.startswith("get_tushare"):
            metadata.update({"repeatable": True, "category": "market_data", "max_result_chars": 8000})
        elif tool_name.startswith("get_akshare"):
            metadata.update({"repeatable": True, "category": "market_data", "max_result_chars": 8000})
        elif tool_name.startswith("describe_akshare"):
            metadata.update({"repeatable": True, "category": "catalog", "max_result_chars": 5000})
        elif tool_name == "get_a_share_market_context":
            metadata.update({"repeatable": True, "category": "market_context", "max_result_chars": 8000})
        elif tool_name == "get_stock_research_context":
            metadata.update({"repeatable": True, "category": "stock_context", "max_result_chars": 8000})
        elif tool_name == "discover_a_share_opportunities":
            metadata.update(
                {
                    "readonly": False,
                    "repeatable": True,
                    "writes_artifact": True,
                    "category": "research_workflow",
                    "max_result_chars": 10000,
                }
            )
        elif tool_name == "run_internal_analysis":
            metadata.update({"repeatable": True, "category": "internal_analysis", "max_result_chars": 8000})
        elif tool_name == "get_chan_context":
            metadata.update({"repeatable": True, "category": "chan_context", "max_result_chars": 8000})
        elif tool_name == "get_knowledge_context":
            metadata.update({"repeatable": True, "category": "knowledge_context", "max_result_chars": 8000})
        elif tool_name == "run_rule_generation":
            metadata.update(
                {
                    "readonly": False,
                    "repeatable": True,
                    "writes_artifact": True,
                    "requires_confirmation": True,
                    "category": "rule_generation",
                    "max_result_chars": 10000,
                }
            )
        return metadata

    def definitions(self) -> list[dict[str, Any]]:
        return [*chat_tool_definitions(self.skills), *_tushare_chat_tool_definitions()]

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            return self._execute(name, arguments)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

    def _execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name in _SHARED_CHAT_TOOL_NAMES:
            return execute_chat_tool(name, arguments, skills=self.skills, settings=self.settings)
        if name == "list_provider_capabilities":
            provider = AStockDataProvider(self.settings)
            capabilities = provider.load_provider_capabilities(
                provider=str(arguments.get("provider") or "").strip() or None,
                category=str(arguments.get("category") or "").strip() or None,
                realtime=arguments.get("realtime") if isinstance(arguments.get("realtime"), bool) else None,
                compact=bool(arguments.get("compact", False)),
            )
            return json.dumps({"status": "ok", "capabilities": capabilities}, ensure_ascii=False)
        if name == "list_akshare_datasets":
            provider = AStockDataProvider(self.settings)
            datasets = provider.list_akshare_datasets(
                domain=str(arguments.get("domain") or "").strip() or None,
                category=str(arguments.get("category") or "").strip() or None,
                tags=arguments.get("tags") if isinstance(arguments.get("tags"), list) else None,
                query=str(arguments.get("query") or "").strip() or None,
                realtime=arguments.get("realtime") if isinstance(arguments.get("realtime"), bool) else None,
                compact=bool(arguments.get("compact", True)),
            )
            return json.dumps({"status": "ok", "datasets": datasets}, ensure_ascii=False)
        if name == "describe_akshare_dataset":
            try:
                provider = AStockDataProvider(self.settings)
                payload = provider.describe_akshare_dataset(str(arguments.get("dataset") or "").strip())
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            return json.dumps({"status": "ok", "dataset": payload}, ensure_ascii=False)
        if name == "get_akshare_data":
            try:
                provider = AStockDataProvider(self.settings)
                payload = provider.fetch_akshare_dataset(
                    str(arguments.get("dataset") or "").strip(),
                    arguments.get("params") if isinstance(arguments.get("params"), dict) else {},
                    fields=arguments.get("fields") if isinstance(arguments.get("fields"), list) else None,
                    limit=int(arguments.get("limit") or 200),
                )
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            return json.dumps({"status": "ok", "akshare_data": payload}, ensure_ascii=False)
        if name == "list_skills":
            return json.dumps({"status": "ok", "skills": skill_summaries(self.skills)}, ensure_ascii=False)
        if name == "load_skill":
            skill = find_skill(self.skills, str(arguments.get("name") or ""))
            if skill is None:
                return json.dumps(
                    {"status": "error", "error": "unknown skill", "available": [item.name for item in self.skills]},
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "status": "ok",
                    "name": skill.name,
                    "category": skill.category,
                    "description": skill.description,
                    "content": skill.content,
                },
                ensure_ascii=False,
            )
        if name == "list_tushare_datasets":
            provider = AStockDataProvider(self.settings)
            datasets = provider.list_tushare_datasets(
                domain=str(arguments.get("domain") or "").strip() or None,
                category=str(arguments.get("category") or "").strip() or None,
                include_deprecated=bool(arguments.get("include_deprecated", True)),
                tags=arguments.get("tags") if isinstance(arguments.get("tags"), list) else None,
            )
            return json.dumps({"status": "ok", "datasets": datasets}, ensure_ascii=False)
        if name == "get_tushare_data":
            try:
                provider = AStockDataProvider(self.settings)
                payload = provider.fetch_tushare_dataset(
                    str(arguments.get("dataset") or "").strip(),
                    arguments.get("params") if isinstance(arguments.get("params"), dict) else {},
                    fields=arguments.get("fields") if isinstance(arguments.get("fields"), list) else None,
                    limit=int(arguments.get("limit") or 200),
                )
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            return json.dumps({"status": "ok", "tushare_data": payload}, ensure_ascii=False)
        if name == "list_tushare_stock_datasets":
            provider = AStockDataProvider(self.settings)
            datasets = provider.list_tushare_stock_datasets(
                category=str(arguments.get("category") or "").strip() or None,
                include_deprecated=bool(arguments.get("include_deprecated", True)),
            )
            return json.dumps({"status": "ok", "datasets": datasets}, ensure_ascii=False)
        if name == "get_tushare_stock_data":
            try:
                provider = AStockDataProvider(self.settings)
                payload = provider.fetch_tushare_stock_dataset(
                    str(arguments.get("dataset") or "").strip(),
                    arguments.get("params") if isinstance(arguments.get("params"), dict) else {},
                    fields=arguments.get("fields") if isinstance(arguments.get("fields"), list) else None,
                    limit=int(arguments.get("limit") or 200),
                )
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            return json.dumps({"status": "ok", "tushare_stock_data": payload}, ensure_ascii=False)
        if name == "get_a_share_market_context":
            try:
                payload = get_a_share_market_context(
                    settings=self.settings,
                    trade_date=str(arguments.get("trade_date") or "").strip() or None,
                    horizon=str(arguments.get("horizon") or "").strip() or None,
                    horizons=arguments.get("horizons") if isinstance(arguments.get("horizons"), list) else None,
                    indices=arguments.get("indices") if isinstance(arguments.get("indices"), list) else None,
                    dimensions=arguments.get("dimensions") if isinstance(arguments.get("dimensions"), list) else None,
                )
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            return json.dumps({"status": "ok", "market_context": payload}, ensure_ascii=False)
        if name == "discover_a_share_opportunities":
            try:
                storage = DuckDBStorage(self.settings.db_path)
                query = str(arguments.get("query") or "").strip()
                explicit_limit = arguments.get("limit")
                parsed_limit = extract_opportunity_discovery_limit(query)
                result = run_stock_picking_agent(
                    query=query,
                    settings=self.settings,
                    storage=storage,
                    skills=self.skills,
                    trade_date=str(arguments.get("trade_date") or "").strip() or None,
                    signals=str(arguments.get("signals") or "short_up").strip() or "short_up",
                    limit=int(explicit_limit) if explicit_limit is not None else parsed_limit,
                    candidate_limit=int(arguments.get("candidate_limit") or DEFAULT_CANDIDATE_LIMIT),
                    hot_sector_enabled=bool(arguments.get("hot_sector", True)),
                    hot_sector_days=int(arguments.get("hot_sector_days") or 5),
                    reports_dir=Path(getattr(self.settings, "project_root", ".")) / "reports",
                    report=True,
                )
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            payload = _context_payload_for_llm(result)
            legacy = result.discovery.to_llm_context() if hasattr(result, "discovery") else payload
            return json.dumps({"status": "ok", "stock_picking_agent": payload, "opportunity_discovery": legacy}, ensure_ascii=False)
        if name == "get_stock_research_context":
            try:
                symbols = arguments.get("symbols") if isinstance(arguments.get("symbols"), list) else []
                question = StockQuestion(
                    symbols=normalize_symbols(symbols, required=True),
                    trade_date=str(arguments.get("trade_date") or "").strip() or None,
                    has_stock_question=True,
                )
                context = build_stock_llm_context(
                    " ".join(question.symbols),
                    settings=self.settings,
                    question=question,
                )
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            payload = context.payload if context is not None else {}
            return json.dumps({"status": "ok", "stock_context": payload}, ensure_ascii=False)
        if name == "run_internal_analysis":
            try:
                payload = _run_internal_analysis_tool(self.settings, arguments)
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            return json.dumps({"status": "ok", "analysis": payload}, ensure_ascii=False)
        return json.dumps({"status": "error", "error": f"unknown tool: {name}"}, ensure_ascii=False)


_ChatSkillToolRegistry = ChatToolRegistry


def _run_internal_analysis_tool(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    return run_internal_analysis_component(settings, arguments)


def _signal_input_from_context(context: dict[str, Any]) -> SignalInput:
    import pandas as pd

    return SignalInput(
        ts_code=str(context.get("ts_code") or ""),
        trade_date=str(context.get("trade_date") or ""),
        daily=pd.DataFrame(context.get("daily_tail") or []),
        stock_basic={"name": context.get("name") or ""},
    )


def _today_yyyymmdd() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def _opportunity_discovery_content_or_fallback(content: str, result: Any) -> str:
    if _is_substantive_opportunity_answer(content):
        return content
    try:
        if hasattr(result, "discovery"):
            fallback = format_stock_picking_agent_result(result)
        else:
            fallback = format_opportunity_discovery(result)
    except Exception:
        return content
    report_path = str(getattr(result, "report_path", "") or "").strip()
    if report_path and "报告:" not in fallback:
        fallback = f"{fallback}\n报告: {report_path}"
    return fallback


def _is_substantive_opportunity_answer(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    if len(text) < 80 and text.lstrip().startswith("#") and "\n\n" not in text:
        return False
    return any(term in text for term in ("触发", "失效", "风险", "报告:", "不构成投资建议"))


def _assistant_tool_call_message(response: Any) -> dict[str, Any]:
    tool_calls = []
    for call in getattr(response, "tool_calls", None) or []:
        tool_calls.append(
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
        )
    message = {"role": "assistant", "content": response.content or "", "tool_calls": tool_calls}
    reasoning = getattr(response, "reasoning_content", None)
    if reasoning:
        message["additional_kwargs"] = {"reasoning_content": reasoning}
        message["reasoning_content"] = reasoning
    return message


def _tool_result_message(call: Any, result: str) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": result,
    }


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
