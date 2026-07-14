from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

CHAT_EVENT_TYPES = (
    "turn_started",
    "plan_ready",
    "context_started",
    "context_completed",
    "tool_started",
    "tool_completed",
    "assistant_completed",
    "turn_completed",
    "turn_failed",
    "clarification_required",
    "turn_interrupted",
    "runtime_started",
    "runtime_iteration_started",
    "llm_completed",
    "action_recovered",
    "failure_detected",
    "recovery_started",
    "recovery_completed",
    "repair_proposed",
    "repair_applied",
    "repair_failed",
    "tool_batch_started",
    "tool_batch_completed",
    "artifact_created",
    "approval_required",
    "compact_started",
    "compact_completed",
    "runtime_completed",
)

CHAT_EVENT_STATUSES = ("running", "done", "error", "interrupted")


@dataclass(frozen=True, slots=True)
class ChatTurn:
    turn_id: str
    session_id: str
    request: str
    status: str = "running"


@dataclass(frozen=True, slots=True)
class ChatTurnEvent:
    event_id: str
    turn_id: str
    seq: int
    event_type: str
    item_type: str = ""
    item_name: str = ""
    status: str = "running"
    content: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    started_at: str = ""
    completed_at: str | None = None
    duration_seconds: float | None = None


ChatEventSink = Callable[[ChatTurnEvent], None]


class ChatTurnRecorder:
    def __init__(
        self,
        *,
        session_id: str,
        request: str,
        store: Any | None = None,
        event_sink: ChatEventSink | None = None,
        enabled: bool = True,
    ) -> None:
        self.turn = ChatTurn(turn_id=_new_id("turn"), session_id=str(session_id or "default"), request=str(request or ""))
        self.store = store
        self.event_sink = event_sink
        self.enabled = enabled
        self.seq = 0
        self.item_seq = 0
        self.started = False

    @property
    def turn_id(self) -> str:
        return self.turn.turn_id

    def start(self, *, payload: Mapping[str, Any] | None = None) -> None:
        if not self.enabled or self.started:
            return
        self.started = True
        if self.store is not None:
            try:
                self.store.start_chat_turn(
                    turn_id=self.turn.turn_id,
                    session_id=self.turn.session_id,
                    request=self.turn.request,
                    meta=dict(payload or {}),
                )
            except Exception:
                self.store = None
        self.emit("turn_started", item_type="turn", status="running", payload=payload or {})

    def emit(
        self,
        event_type: str,
        *,
        item_type: str = "",
        item_name: str = "",
        status: str = "done",
        content: str = "",
        payload: Mapping[str, Any] | None = None,
        duration_seconds: float | None = None,
    ) -> ChatTurnEvent | None:
        if not self.enabled:
            return None
        self.seq += 1
        event = ChatTurnEvent(
            event_id=_new_id("evt"),
            turn_id=self.turn.turn_id,
            seq=self.seq,
            event_type=_clean_choice(event_type, CHAT_EVENT_TYPES, "turn_started"),
            item_type=str(item_type or ""),
            item_name=str(item_name or ""),
            status=_clean_choice(status, CHAT_EVENT_STATUSES, "done"),
            content=str(content or ""),
            payload=dict(payload or {}),
            started_at=_now(),
            completed_at=_now() if status != "running" else None,
            duration_seconds=duration_seconds,
        )
        if self.store is not None:
            try:
                self.store.add_chat_turn_event(event)
            except Exception:
                self.store = None
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:
                pass
        return event

    def start_item(
        self,
        item_type: str,
        item_name: str = "",
        *,
        input_payload: Mapping[str, Any] | None = None,
        status: str = "running",
    ) -> str:
        item_id = _new_id("item")
        if not self.enabled:
            return item_id
        self.item_seq += 1
        if self.store is not None:
            try:
                self.store.add_chat_turn_item(
                    item_id=item_id,
                    turn_id=self.turn.turn_id,
                    seq=self.item_seq,
                    item_type=item_type,
                    item_name=item_name,
                    status=status,
                    input_payload=dict(input_payload or {}),
                )
            except Exception:
                self.store = None
        return item_id

    def complete_item(
        self,
        item_id: str,
        *,
        status: str = "done",
        output_payload: Mapping[str, Any] | None = None,
        artifact_paths: list[str] | tuple[str, ...] = (),
        duration_seconds: float | None = None,
    ) -> None:
        if not self.enabled or self.store is None:
            return
        try:
            self.store.complete_chat_turn_item(
                item_id,
                status=status,
                output_payload=dict(output_payload or {}),
                artifact_paths=artifact_paths,
                duration_seconds=duration_seconds,
            )
        except Exception:
            self.store = None

    def fail_item(
        self,
        item_id: str,
        exc: Exception | str,
        *,
        duration_seconds: float | None = None,
    ) -> None:
        self.complete_item(
            item_id,
            status="error",
            output_payload={"error": str(exc)},
            duration_seconds=duration_seconds,
        )

    def create_artifact(
        self,
        *,
        kind: str,
        path: str,
        title: str = "",
        mime_type: str = "",
        summary: str = "",
        meta: Mapping[str, Any] | None = None,
    ) -> str:
        artifact_id = _new_id("art")
        payload = {
            "artifact_id": artifact_id,
            "kind": kind,
            "title": title,
            "path": path,
            "mime_type": mime_type,
            "summary": summary,
            **dict(meta or {}),
        }
        if self.store is not None:
            try:
                self.store.add_chat_artifact(
                    artifact_id=artifact_id,
                    session_id=self.turn.session_id,
                    turn_id=self.turn.turn_id,
                    kind=kind,
                    path=path,
                    title=title,
                    mime_type=mime_type,
                    summary=summary,
                    meta=dict(meta or {}),
                )
            except Exception:
                self.store = None
        self.emit(
            "artifact_created",
            item_type="artifact",
            item_name=kind,
            status="done",
            content=path,
            payload=payload,
        )
        return artifact_id

    def require_approval(
        self,
        *,
        action_type: str,
        title: str,
        payload: Mapping[str, Any],
        expires_at: str | None = None,
    ) -> str:
        action_id = _new_id("act")
        stored_payload = dict(payload or {})
        if self.store is not None:
            try:
                action_id = self.store.create_pending_action(
                    action_id=action_id,
                    session_id=self.turn.session_id,
                    turn_id=self.turn.turn_id,
                    action_type=action_type,
                    title=title,
                    payload=stored_payload,
                    expires_at=expires_at,
                )
            except Exception:
                self.store = None
        self.emit(
            "approval_required",
            item_type="approval",
            item_name=action_type,
            status="done",
            content=title,
            payload={"action_id": action_id, "action_type": action_type, "title": title},
        )
        return action_id

    def complete(
        self,
        *,
        content: str = "",
        status: str = "done",
        intent: str = "",
        symbols: list[str] | tuple[str, ...] = (),
        trade_date: str | None = None,
        data_names: list[str] | tuple[str, ...] = (),
        skill_names: list[str] | tuple[str, ...] = (),
        model_name: str = "",
        tool_call_count: int = 0,
        user_message_id: str = "",
        assistant_message_id: str = "",
        duration_seconds: float | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        payload = {
            "intent": intent,
            "symbols": list(symbols),
            "trade_date": trade_date or "",
            "data_names": list(data_names),
            "skill_names": list(skill_names),
            "tool_call_count": tool_call_count,
            **dict(meta or {}),
        }
        self.emit(
            "assistant_completed",
            item_type="assistant",
            status="done",
            content=content,
            payload=payload,
        )
        if self.store is not None:
            try:
                self.store.complete_chat_turn(
                    self.turn.turn_id,
                    status=_clean_choice(status, CHAT_EVENT_STATUSES, "done"),
                    intent=intent,
                    symbols=symbols,
                    trade_date=trade_date,
                    data_names=data_names,
                    skill_names=skill_names,
                    model_name=model_name,
                    tool_call_count=tool_call_count,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    duration_seconds=duration_seconds,
                    meta=meta or {},
                )
            except Exception:
                self.store = None
        self.emit(
            "turn_completed",
            item_type="turn",
            status=_clean_choice(status, CHAT_EVENT_STATUSES, "done"),
            payload=payload,
            duration_seconds=duration_seconds,
        )

    def fail(
        self,
        exc: Exception | str,
        *,
        status: str = "error",
        duration_seconds: float | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        message = str(exc)
        event_type = "turn_interrupted" if status == "interrupted" else "turn_failed"
        payload = {"error": message, **dict(meta or {})}
        if self.store is not None:
            try:
                self.store.fail_chat_turn(
                    self.turn.turn_id,
                    status=status,
                    error={"message": message},
                    duration_seconds=duration_seconds,
                    meta=meta or {},
                )
            except Exception:
                self.store = None
        self.emit(
            event_type,
            item_type="turn",
            status=status,
            content=message,
            payload=payload,
            duration_seconds=duration_seconds,
        )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_choice(value: str, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default
