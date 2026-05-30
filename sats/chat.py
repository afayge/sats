from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from sats.config import Settings, load_settings
from sats.llm import ChatLLM, LLMResponse
from sats.memory import ChatMemoryStore, MemoryExtractor, MemoryRecord, MemoryRetriever
from sats.screening.registry import list_rules
from sats.screening.rule_composer import (
    RuleGenerationPlan,
    compose_rule_generation_plan,
    format_generated_rule_result,
    format_rule_generation_plan,
    generate_rule_code,
    is_rule_generation_request,
    parse_rule_generation_confirmation,
    revise_rule_generation_plan,
)
from sats.skills import Skill, default_skills_dir, find_skill, load_skills, match_skills, skill_summaries
from sats.storage.duckdb import DuckDBStorage
from sats.analysis.chan_chat_context import build_chan_chat_context
from sats.analysis.dsa_native import run_dsa_analysis
from sats.analysis.market_llm_context import build_market_llm_context, get_a_share_market_context
from sats.analysis.opportunity_discovery import format_opportunity_discovery, run_opportunity_discovery
from sats.analysis.quote_llm_context import build_stock_quote_llm_context
from sats.analysis.stock_llm_context import build_stock_llm_context, ensure_stock_analysis_data
from sats.analysis.stock_research_context import StockResearchContext, build_stock_research_context
from sats.chat_planner import build_chat_plan, skills_for_plan
from sats.chat_preprocessor import ChatPreprocessResult, preprocess_chat_message
from sats.chat_reference import ChatReferenceContext
from sats.data.astock_provider import AStockDataProvider
from sats.signals import SignalInput, analyze_signal_inputs
from sats.stock_question import StockQuestion, extract_intraday_time, extract_trade_date, parse_stock_question
from sats.symbols import normalize_symbols

SYSTEM_PROMPT = (
    "你是 SATS CLI 助手。你可以解释 SATS 的功能、筛选规则、数据源、命令用法和实现思路，"
    "也可以建议用户运行哪些命令。不要声称已经执行任何命令；只有斜杠命令、一次性 CLI 命令或已注入的只读研究上下文才代表真实执行过数据获取/分析。"
    "不得编造实时行情、价格、涨跌幅、均线、成交量、财务数据、新闻、公告或题材；"
    "没有 SATS 命令或结构化数据结果时，只能说明无法获取真实行情并建议具体命令。"
    "对自然语言短线选股问题，SATS 会先用 Analyze 中短期上涨信号做临时全市场筛选，再注入候选上下文；"
    "热点板块只做优先加权，不能把热点或题材当作上涨保证；"
    "只能把候选表达为观察名单、触发条件和失效条件，不能保证上涨。"
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
        self._light_llm: Any | None = None
        self._memory_lock = threading.Lock()
        self._last_stock_question: StockQuestion | None = None
        self._pending_rule_plan: RuleGenerationPlan | None = None

    def ask(
        self,
        message: str,
        *,
        use_memory: bool | None = None,
        progress: Any | None = None,
        reference_context: ChatReferenceContext | None = None,
        defer_memory_updates: bool = False,
    ) -> ChatResult:
        active_progress = progress if progress is not None else self.progress
        text = str(message or "").strip()
        if not text:
            raise ValueError("chat message is required")
        rule_result = self._maybe_handle_rule_generation(text)
        if rule_result is not None:
            return rule_result
        effective_memory = self.memory_enabled if use_memory is None else use_memory
        store = self._ensure_memory_store() if effective_memory else None
        if store is not None:
            try:
                self._load_persisted_history(store)
            except Exception:
                store = None
        try:
            memories = MemoryRetriever(store).retrieve(text) if store is not None else []
            summary = store.get_session_summary(self.session_id) if store is not None else ""
        except Exception:
            store = None
            memories = []
            summary = ""
        matched = match_skills(text, self.skills)
        preprocess: ChatPreprocessResult | None = None
        if active_progress is None:
            preprocess = self._preprocess_chat(text, reference_context=reference_context)
            if preprocess.missing_questions:
                return self._direct_result(text, _format_preprocess_questions(preprocess.missing_questions))
            resolved_stock_question = self._resolved_stock_question_from_preprocess(text, preprocess, reference_context)
            plan = build_chat_plan(
                text,
                skills=self.skills,
                stock_question=resolved_stock_question,
                preprocess=preprocess,
            )
        else:
            with active_progress.step("输入分析") as step:
                preprocess = self._preprocess_chat(text, reference_context=reference_context)
                if preprocess.missing_questions:
                    step.complete(message="需要澄清")
                    return self._direct_result(text, _format_preprocess_questions(preprocess.missing_questions))
                resolved_stock_question = self._resolved_stock_question_from_preprocess(text, preprocess, reference_context)
                plan = build_chat_plan(
                    text,
                    skills=self.skills,
                    stock_question=resolved_stock_question,
                    preprocess=preprocess,
                )
                step.complete(message=plan.intent)
        if active_progress is None:
            matched = skills_for_plan(plan, self.skills, matched)
        else:
            with active_progress.step("加载 skill") as step:
                matched = skills_for_plan(plan, self.skills, matched)
                step.complete(message=",".join(skill.name for skill in matched) or "none")
        if resolved_stock_question is None and _is_stock_followup(text):
            return ChatResult(
                content="请先提供明确股票代码，例如：用缠论分析 002436。",
                skill_names=tuple(skill.name for skill in matched),
                memory_count=len(memories),
            )
        stock_context = None
        data_names: list[str] = []
        if reference_context is not None:
            data_names.append(reference_context.data_name)
        quote_context = None
        if (
            preprocess is not None
            and getattr(preprocess, "needs_realtime_quote_context", False)
            and resolved_stock_question is not None
            and resolved_stock_question.has_stock_question
        ):
            if active_progress is None:
                quote_context = build_stock_quote_llm_context(
                    text,
                    settings=self.settings,
                    symbols=list(resolved_stock_question.symbols),
                )
            else:
                with active_progress.step("实盘数据") as step:
                    quote_context = build_stock_quote_llm_context(
                        text,
                        settings=self.settings,
                        symbols=list(resolved_stock_question.symbols),
                    )
                    step.complete(message="报价")
            if quote_context is not None:
                data_names.append("实时报价")
        if plan.needs_stock_context and resolved_stock_question is not None and resolved_stock_question.has_stock_question:
            if active_progress is None:
                stock_context = build_stock_llm_context(text, settings=self.settings, question=resolved_stock_question)
            else:
                with active_progress.step("实盘数据") as step:
                    stock_context = build_stock_llm_context(text, settings=self.settings, question=resolved_stock_question)
                    step.complete(message="个股")
            if stock_context is not None:
                data_names.append("个股")
        if stock_context is not None:
            context_question = getattr(stock_context, "question", resolved_stock_question)
            context_trade_date = str(getattr(stock_context, "trade_date", "") or context_question.trade_date or "")
            self._last_stock_question = StockQuestion(
                symbols=list(context_question.symbols),
                trade_date=context_trade_date or None,
                as_of_time=context_question.as_of_time,
                has_stock_question=True,
            )
        show_market_progress = active_progress is not None and plan.needs_market_context
        market_indices = tuple(getattr(preprocess, "market_indices", ()) or ()) if preprocess is not None else ()
        market_dimensions = tuple(getattr(preprocess, "market_dimensions", ()) or ()) if preprocess is not None else ()
        market_horizons = tuple(getattr(preprocess, "market_horizons", ()) or ()) if preprocess is not None else ()
        market_plan_source = str(getattr(preprocess, "market_plan_source", "") or "") if preprocess is not None else ""
        market_context = None
        if not plan.needs_market_context:
            market_context = None
        elif active_progress is None or not show_market_progress:
            market_context = build_market_llm_context(
                text,
                settings=self.settings,
                trade_date=getattr(stock_context, "trade_date", None) if stock_context is not None else None,
                indices=market_indices or None,
                dimensions=market_dimensions or None,
                horizons=market_horizons or None,
                market_plan_source=market_plan_source or None,
                force=plan.needs_market_context,
            )
        else:
            with active_progress.step("实盘数据") as step:
                market_context = build_market_llm_context(
                    text,
                    settings=self.settings,
                    trade_date=getattr(stock_context, "trade_date", None) if stock_context is not None else None,
                    indices=market_indices or None,
                    dimensions=market_dimensions or None,
                    horizons=market_horizons or None,
                    market_plan_source=market_plan_source or None,
                    force=plan.needs_market_context,
                )
                step.complete(message="大盘")
        if market_context is not None:
            data_names.append("大盘")
        opportunity_context = None
        if resolved_stock_question is None and plan.needs_opportunity_discovery:
            storage = DuckDBStorage(self.settings.db_path)
            if active_progress is None:
                opportunity_context = run_opportunity_discovery(
                    settings=self.settings,
                    storage=storage,
                    trade_date=extract_trade_date(text),
                    hot_sector_enabled=True,
                    hot_sector_days=5,
                    market_indices=market_indices or None,
                    market_dimensions=market_dimensions or None,
                    market_horizons=market_horizons or None,
                    market_plan_source=market_plan_source or None,
                    reports_dir=Path(getattr(self.settings, "project_root", ".")) / "reports",
                    report=True,
                )
            else:
                with active_progress.step("内部分析") as step:
                    opportunity_context = run_opportunity_discovery(
                        settings=self.settings,
                        storage=storage,
                        trade_date=extract_trade_date(text),
                        hot_sector_enabled=True,
                        hot_sector_days=5,
                        market_indices=market_indices or None,
                        market_dimensions=market_dimensions or None,
                        market_horizons=market_horizons or None,
                        market_plan_source=market_plan_source or None,
                        reports_dir=Path(getattr(self.settings, "project_root", ".")) / "reports",
                        report=True,
                        progress=active_progress,
                    )
                    step.complete(message="机会发现")
            data_names.extend(["热点板块", "机会发现"])
        chan_context = None
        if plan.needs_chan_context:
            chan_context = build_chan_chat_context(text, skills=self.skills)
            if chan_context is not None:
                data_names.append("缠论RAG")
        research_context = self._build_research_context(
            text,
            plan_collections=_collections_for_plan(plan),
            explicit_knowledge=self.knowledge,
        )
        if research_context is not None:
            data_names.append("知识库RAG")
        conversation = ConversationContextBuilder().build(
            message=text,
            history=self.history,
            skills=matched,
            plan_context=plan.system_message(),
            memories=memories,
            session_summary=summary,
            stock_context=_stock_context_message(stock_context, market_context),
            market_context=market_context.system_message if stock_context is None and market_context is not None else "",
            opportunity_context=opportunity_context.system_message if opportunity_context is not None else "",
            chan_context=chan_context.system_message if chan_context is not None else "",
            research_context=research_context.system_message if research_context is not None else "",
            quote_context=quote_context.system_message if quote_context is not None else "",
            reference_context=reference_context.system_message if reference_context is not None else "",
            preprocess_context=preprocess.system_message()
            if preprocess is not None and _should_include_preprocess_context(preprocess)
            else "",
            sources=research_context.sources if research_context is not None else (),
        )
        messages = conversation.messages
        response, tool_call_count = self._chat_with_optional_tools(
            messages,
            progress=active_progress,
        )
        content = str(response.content or "").strip()
        if opportunity_context is not None:
            content = _opportunity_discovery_content_or_fallback(content, opportunity_context)
        content = content or "无响应"
        if store is not None:
            try:
                store.touch_memories([memory.memory_id for memory in memories])
                store.add_message(self.session_id, "user", text)
                store.add_message(self.session_id, "assistant", content)
            except Exception:
                store = None
        self._append_history("user", text)
        self._append_history("assistant", content)
        if store is not None:
            if defer_memory_updates:
                self._defer_memory_maintenance(store, text, content)
            else:
                self._maintain_memory(store, text, content)
        return ChatResult(
            content=content,
            skill_names=tuple(skill.name for skill in matched),
            memory_count=len(memories),
            tool_call_count=tool_call_count,
            data_names=tuple(_dedupe(data_names)),
            sources=conversation.sources,
        )

    def _maybe_handle_rule_generation(self, text: str) -> ChatResult | None:
        confirmed_rule = parse_rule_generation_confirmation(text)
        if confirmed_rule is not None:
            return self._confirm_rule_generation(text, confirmed_rule)
        if is_rule_generation_request(text):
            plan = compose_rule_generation_plan(text, existing_rule_names=list_rules())
            self._pending_rule_plan = plan
            return self._direct_result(text, format_rule_generation_plan(plan), data_names=("规则计划",))
        if self._pending_rule_plan is not None and _looks_like_rule_plan_revision(text):
            plan = revise_rule_generation_plan(self._pending_rule_plan, text, existing_rule_names=list_rules())
            self._pending_rule_plan = plan
            return self._direct_result(text, format_rule_generation_plan(plan), data_names=("规则计划",))
        return None

    def _confirm_rule_generation(self, text: str, confirmed_rule: str) -> ChatResult:
        plan = self._pending_rule_plan
        if plan is None:
            return self._direct_result(text, "当前没有待生成规则。请先描述要新增的筛选规则。")
        if confirmed_rule != plan.rule_name:
            return self._direct_result(text, f"确认的规则名 {confirmed_rule} 与当前计划 {plan.rule_name} 不一致，请重新确认。")
        if plan.questions:
            return self._direct_result(
                text,
                "规则计划仍有待确认问题，暂不能生成代码。\n\n" + format_rule_generation_plan(plan),
                data_names=("规则计划",),
            )
        if confirmed_rule in list_rules():
            return self._direct_result(text, f"规则名 {confirmed_rule} 已存在，请换一个 rule_name 后重新生成计划。")
        result = generate_rule_code(plan)
        self._pending_rule_plan = None
        return self._direct_result(text, format_generated_rule_result(result), data_names=("生成规则",))

    def _direct_result(self, user_text: str, content: str, *, data_names: tuple[str, ...] = ()) -> ChatResult:
        self._append_history("user", user_text)
        self._append_history("assistant", content)
        return ChatResult(content=content, skill_names=(), data_names=data_names)

    def _preprocess_chat(
        self,
        message: str,
        *,
        reference_context: ChatReferenceContext | None = None,
    ) -> ChatPreprocessResult:
        return preprocess_chat_message(
            message,
            settings=self.settings,
            reference_context=reference_context,
            llm_factory=self.llm_factory,
            llm_enabled=self.preprocess_enabled and self.llm_factory is ChatLLM,
        )

    def _resolved_stock_question_from_preprocess(
        self,
        message: str,
        preprocess: ChatPreprocessResult,
        reference_context: ChatReferenceContext | None,
    ) -> StockQuestion | None:
        parsed = parse_stock_question(message)
        if parsed.has_stock_question:
            return self._resolve_stock_question(message, parsed)
        symbols = list(preprocess.symbols)
        if symbols:
            return self._resolve_stock_question(
                message,
                StockQuestion(
                    symbols=symbols,
                    trade_date=preprocess.trade_date or (reference_context.trade_date if reference_context else None),
                    as_of_time=preprocess.as_of_time,
                    has_stock_question=True,
                ),
            )
        if reference_context is not None and reference_context.symbols:
            return self._resolve_stock_question(
                message,
                StockQuestion(
                    symbols=list(reference_context.symbols),
                    trade_date=preprocess.trade_date or reference_context.trade_date,
                    as_of_time=preprocess.as_of_time,
                    has_stock_question=True,
                ),
            )
        return self._resolve_stock_question(message, parsed)

    def _resolve_stock_question(self, message: str, parsed: StockQuestion) -> StockQuestion | None:
        if parsed.has_stock_question:
            return parsed
        if self._last_stock_question is None or not _is_stock_followup(message):
            return None
        return StockQuestion(
            symbols=list(self._last_stock_question.symbols),
            trade_date=extract_trade_date(message) or self._last_stock_question.trade_date,
            as_of_time=extract_intraday_time(message) or self._last_stock_question.as_of_time,
            has_stock_question=True,
        )

    def _llm_client(self) -> Any:
        if self._llm is None:
            self._llm = _create_llm(
                self.llm_factory,
                _main_model_name(self.settings),
                profile="default",
            )
        return self._llm

    def _light_llm_client(self) -> Any:
        if self._light_llm is None:
            self._light_llm = _create_llm(
                self.llm_factory,
                _light_model_name(self.settings),
                profile="light",
                timeout_seconds=6,
            )
        return self._light_llm

    def _chat_with_optional_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        progress: Any | None = None,
    ) -> tuple[Any, int]:
        llm = self._llm_client()
        model_name = str(getattr(self.settings, "openai_model", "") or "LLM")
        if not self.tools_enabled:
            try:
                if progress is None:
                    return llm.chat(messages), 0
                with progress.step(f"{model_name} LLM") as step:
                    response = llm.chat(messages)
                    step.complete()
                return response, 0
            except Exception as exc:
                if _is_timeout_exception(exc):
                    return LLMResponse(content=_timeout_message(model_name)), 0
                raise
        registry = _ChatSkillToolRegistry(self.skills, self.settings)
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
                return llm.chat(messages), 0
            with progress.step(f"{model_name} LLM") as step:
                response = llm.chat(messages)
                step.complete()
            return response, 0
        except Exception as exc:
            if _is_timeout_exception(exc):
                return LLMResponse(content=_timeout_message(model_name)), 0
            raise
        tool_call_count = 0
        for _ in range(self.max_tool_iterations):
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                return response, tool_call_count
            messages.append(_assistant_tool_call_message(response))
            for call in tool_calls:
                tool_call_count += 1
                if progress is None:
                    tool_result = registry.execute(call.name, call.arguments)
                else:
                    with progress.step(f"工具 {call.name}") as step:
                        tool_result = registry.execute(call.name, call.arguments)
                        step.complete()
                messages.append(_tool_result_message(call, tool_result))
            if progress is None:
                try:
                    response = llm.chat(messages, tools=definitions)
                except Exception as exc:
                    if _is_timeout_exception(exc):
                        return LLMResponse(content=_timeout_message(model_name)), tool_call_count
                    raise
            else:
                with progress.step(f"{model_name} LLM") as step:
                    try:
                        response = llm.chat(messages, tools=definitions)
                    except Exception as exc:
                        if _is_timeout_exception(exc):
                            step.complete(message="Request timed out.")
                            return LLMResponse(content=_timeout_message(model_name)), tool_call_count
                        raise
                    step.complete()
        if not str(getattr(response, "content", "") or "").strip():
            response.content = "工具调用已达到上限，请缩小问题范围后重试。"
        return response, tool_call_count

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
) -> ChatResult:
    session = ChatSession(
        settings=settings,
        skills=skills,
        llm_factory=llm_factory,
        memory_enabled=memory_enabled,
        max_history_messages=0,
        progress=progress,
        knowledge=knowledge,
    )
    return session.ask(message)


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


def format_chat_result(result: ChatResult) -> str:
    lines = []
    if result.skill_names:
        lines.append(f"使用 skill: {', '.join(result.skill_names)}")
    if result.data_names:
        lines.append(f"数据: {', '.join(result.data_names)}")
    lines.append(result.content)
    return "\n".join(lines)


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
    if "chan-theory" in skills or "chan_context" in requirements or intent == "chan_analysis":
        collections.append("chan")
    if "technical-basic" in skills or "stock_context" in requirements:
        collections.extend(["stock-basic", "technical"])
    if "opportunity_discovery" in actions or "signals" in skills or {"quant-factor-screener", "small-cap-growth-identifier"} & skills:
        collections.append("signals")
    if "market_context" in requirements or "sats-market-assistant" in skills:
        collections.extend(["market", "sentiment"])
    if {"sector-rotation"} & skills:
        collections.extend(["market", "sentiment"])
    if {"financial-statement", "valuation-model", "fundamental-filter", "quant-factor-screener", "high-dividend-strategy", "undervalued-stock-screener", "small-cap-growth-identifier", "esg-screener", "tech-hype-vs-fundamentals"} & skills:
        collections.append("fundamental")
    if {"event-driven-detector", "insider-trading-analyzer", "sentiment-reality-gap"} & skills:
        collections.append("sentiment")
    if {"risk-analysis", "portfolio-health-check", "risk-adjusted-return-optimizer", "suitability-report-generator"} & skills:
        collections.append("risk")
    return tuple(_dedupe(collections))


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


class _ChatSkillToolRegistry:
    def __init__(self, skills: list[Skill], settings: Settings) -> None:
        self.skills = skills
        self.settings = settings

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_skills",
                    "description": "列出 SATS 本地 skills 的分类摘要。只读，不执行任何交易或数据写入。",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "load_skill",
                    "description": "按 skill 名称或 id 加载完整 SKILL.md 内容。只读，用于获得更完整的分析指引。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Skill name or id, e.g. valuation-model"}
                        },
                        "required": ["name"],
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
            {
                "type": "function",
                "function": {
                    "name": "get_a_share_market_context",
                    "description": "获取真实 A 股大盘指数和市场宽度上下文。只读，不写库、不交易。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "trade_date": {"type": "string", "description": "交易日 YYYYMMDD，可省略为今天"},
                            "horizon": {
                                "type": "string",
                                "enum": ["today", "tomorrow", "day_after_tomorrow", "next_week"],
                                "description": "分析视角，默认 today",
                            },
                            "horizons": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["today", "tomorrow", "day_after_tomorrow", "next_week"],
                                },
                                "description": "多个分析视角，可替代 horizon",
                            },
                            "indices": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "指数代码或名称，可省略使用默认 A 股指数池",
                            },
                            "dimensions": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors"],
                                },
                                "description": "需要拉取的市场维度，默认核心指数+市场宽度+涨跌停情绪",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_a_share_opportunities",
                    "description": "基于 Analyze 中短期上涨信号做临时全 A 机会发现，并返回候选股票排序上下文。只读研究，不交易。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "trade_date": {"type": "string", "description": "交易日 YYYYMMDD，可省略为最近交易日"},
                            "signals": {"type": "string", "description": "信号组或信号 id，默认 short_up"},
                            "limit": {"type": "integer", "description": "返回股票数量，默认 5"},
                            "candidate_limit": {"type": "integer", "description": "本地候选池数量，默认 30"},
                            "hot_sector": {"type": "boolean", "description": "是否启用热点板块优先，默认 true"},
                            "hot_sector_days": {
                                "type": "integer",
                                "enum": [3, 4, 5],
                                "description": "热点板块持续性参考天数，默认 5",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_stock_research_context",
                    "description": "获取指定 A 股个股真实研究上下文。只读，不写库、不交易。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "股票代码列表，支持裸代码",
                            },
                            "trade_date": {"type": "string", "description": "交易日 YYYYMMDD，可省略"},
                        },
                        "required": ["symbols"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_internal_analysis",
                    "description": "运行 SATS 白名单内部分析能力。只读研究，不接受自由 shell 命令。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["indicators", "analyze_signals", "native_dsa"],
                                "description": "内部分析类型",
                            },
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "股票代码列表，支持裸代码",
                            },
                            "trade_date": {"type": "string", "description": "交易日 YYYYMMDD，可省略为最近交易日"},
                            "signals": {"type": "string", "description": "analyze_signals 使用的信号组，默认 short_up"},
                        },
                        "required": ["kind", "symbols"],
                    },
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            return self._execute(name, arguments)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

    def _execute(self, name: str, arguments: dict[str, Any]) -> str:
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
                result = run_opportunity_discovery(
                    settings=self.settings,
                    storage=storage,
                    trade_date=str(arguments.get("trade_date") or "").strip() or None,
                    signals=str(arguments.get("signals") or "short_up").strip() or "short_up",
                    limit=int(arguments.get("limit") or 5),
                    candidate_limit=int(arguments.get("candidate_limit") or 30),
                    hot_sector_enabled=bool(arguments.get("hot_sector", True)),
                    hot_sector_days=int(arguments.get("hot_sector_days") or 5),
                    reports_dir=Path(getattr(self.settings, "project_root", ".")) / "reports",
                    report=True,
                )
            except Exception as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
            return json.dumps({"status": "ok", "opportunity_discovery": result.to_dict()}, ensure_ascii=False)
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


def _run_internal_analysis_tool(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    kind = str(arguments.get("kind") or "").strip()
    if kind not in {"indicators", "analyze_signals", "native_dsa"}:
        raise ValueError(f"unsupported internal analysis kind: {kind}")
    symbols = normalize_symbols(arguments.get("symbols") if isinstance(arguments.get("symbols"), list) else [], required=True)
    trade_date = str(arguments.get("trade_date") or "").strip() or extract_trade_date(" ".join(symbols))
    storage = DuckDBStorage(settings.db_path)
    if kind == "native_dsa":
        result = run_dsa_analysis(
            symbols,
            settings=settings,
            storage=storage,
            trade_date=trade_date,
            reports_dir=Path(getattr(settings, "project_root", ".")) / "reports",
            report=False,
            llm_enabled=False,
        )
        return {
            "kind": kind,
            "trade_date": result.trade_date,
            "rankings": [asdict(ranking) for ranking in result.rankings],
            "message": result.message,
        }
    contexts = ensure_stock_analysis_data(
        symbols,
        trade_date or _today_yyyymmdd(),
        settings=settings,
        storage=storage,
    )
    inputs = [_signal_input_from_context(context) for context in contexts.values()]
    if kind == "indicators":
        return {
            "kind": kind,
            "trade_date": trade_date or _today_yyyymmdd(),
            "indicators": [context.get("indicator_result", {}) for context in contexts.values()],
        }
    run = analyze_signal_inputs(
        inputs,
        selected_signals=str(arguments.get("signals") or "short_up"),
        trade_date=trade_date or _today_yyyymmdd(),
        report=False,
    )
    return {
        "kind": kind,
        "trade_date": run.trade_date,
        "results": [result.to_dict() for result in run.results],
    }


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
