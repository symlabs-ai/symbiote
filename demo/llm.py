"""RecordingLLM — an LLMPort wrapper that shows every model call.

It forwards to the real adapter and publishes ``llm_start`` / ``llm_delta`` /
``llm_end`` events, keeping the last calls (with the exact messages sent) for
the Context tab. The kernel only sees an ordinary LLMPort.
"""

from __future__ import annotations

import copy
import itertools
import time
from collections import deque
from collections.abc import Iterator
from typing import Any

from demo.events import TURN, EventBus
from symbiote.environment.descriptors import LLMResponse

_MAX_STORED_CHARS = 20_000


def _trim_messages(messages: list[dict]) -> list[dict]:
    out = []
    for message in copy.deepcopy(messages):
        content = message.get("content")
        if isinstance(content, str) and len(content) > _MAX_STORED_CHARS:
            message["content"] = content[:_MAX_STORED_CHARS] + "\n…[cortado pelo demo]"
        out.append(message)
    return out


def _purpose(messages: list[dict], role: str) -> str:
    """Best-effort label for what the kernel is asking the model."""
    system = str(messages[0].get("content", "")) if messages else ""
    head = system[:400].lower()
    if role == "main":
        if "planner" in head or "planejador" in head:
            return "planejador (long-run)"
        if "evaluator" in head or "avaliador" in head:
            return "avaliador (long-run)"
        return "turno do chat"
    for needle, label in (
        ("reflect", "reflexão"),
        ("fact", "reflexão"),
        ("skill", "review de skills"),
        ("dream", "dream"),
        ("generaliz", "dream"),
        ("tag", "seleção semântica de tools"),
        ("propos", "evolução do harness"),
        ("instruction", "evolução do harness"),
    ):
        if needle in head:
            return label
    return "tarefa auxiliar"


class RecordingLLM:
    def __init__(self, inner: Any, bus: EventBus, *, role: str, model: str) -> None:
        self._inner = inner
        self._bus = bus
        self.role = role
        self.model = model
        self._ids = itertools.count(1)
        self.calls: deque[dict] = deque(maxlen=60)

    # ── LLMPort ────────────────────────────────────────────────────────

    def complete(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> str | LLMResponse:
        record = self._start(messages, tools, streaming=False)
        try:
            result = self._inner.complete(messages, config=config, tools=tools)
        except Exception as exc:
            self._end(record, error=f"{type(exc).__name__}: {exc}")
            raise
        self._end(record, result=result)
        return result

    def stream(
        self,
        messages: list[dict],
        config: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Iterator[str | LLMResponse]:
        if not hasattr(self._inner, "stream"):
            # Same contract as ForgeLLMAdapter's fallback: text, then the response.
            result = self.complete(messages, config=config, tools=tools)
            if isinstance(result, LLMResponse):
                if result.content:
                    yield result.content
                yield result
            else:
                yield result
            return
        record = self._start(messages, tools, streaming=True)
        text: list[str] = []
        final: LLMResponse | None = None
        try:
            for chunk in self._inner.stream(messages, config=config, tools=tools):
                if isinstance(chunk, LLMResponse):
                    final = chunk
                else:
                    text.append(chunk)
                    self._bus.publish("llm_delta", call_id=record["id"], text=chunk)
                yield chunk
        except Exception as exc:
            self._end(record, error=f"{type(exc).__name__}: {exc}")
            raise
        self._end(record, result=final if final is not None else "".join(text))

    # ── recording ──────────────────────────────────────────────────────

    def _start(self, messages: list[dict], tools: list[dict] | None, *, streaming: bool) -> dict:
        turn = TURN.get()
        record = {
            "id": f"{self.role}-{next(self._ids)}",
            "role": self.role,
            "model": self.model,
            "purpose": _purpose(messages, self.role),
            "turn_id": turn.turn_id if turn else None,
            "session_id": turn.session_id if turn else None,
            "started": time.time(),
            "streaming": streaming,
            "messages": _trim_messages(messages),
            "tools": [t.get("function", t).get("name") for t in tools or []],
            "elapsed_ms": None,
            "text": None,
            "tool_calls": [],
            "usage": None,
            "error": None,
        }
        self.calls.append(record)
        self._bus.publish(
            "llm_start",
            call_id=record["id"],
            role=self.role,
            model=self.model,
            purpose=record["purpose"],
            n_messages=len(messages),
            n_tools=len(record["tools"]),
            streaming=streaming,
            prompt_chars=sum(len(str(m.get("content") or "")) for m in messages),
        )
        return record

    def _end(self, record: dict, *, result: Any = None, error: str | None = None) -> None:
        record["elapsed_ms"] = int((time.time() - record["started"]) * 1000)
        if error is not None:
            record["error"] = error
        elif isinstance(result, LLMResponse):
            record["text"] = result.content
            record["tool_calls"] = [
                {"tool": c.tool_id, "params": c.params} for c in result.tool_calls
            ]
            if result.usage is not None:
                record["usage"] = result.usage.model_dump(exclude={"metadata"})
        else:
            record["text"] = str(result) if result is not None else ""
        self._bus.publish(
            "llm_end",
            call_id=record["id"],
            role=self.role,
            purpose=record["purpose"],
            elapsed_ms=record["elapsed_ms"],
            text=(record["text"] or "")[:4000],
            tool_calls=record["tool_calls"],
            usage=record["usage"],
            error=record["error"],
        )

    def find(self, *, turn_id: str | None = None, call_id: str | None = None) -> list[dict]:
        calls = list(self.calls)
        if call_id is not None:
            return [c for c in calls if c["id"] == call_id]
        if turn_id is not None:
            return [c for c in calls if c["turn_id"] == turn_id]
        return calls
