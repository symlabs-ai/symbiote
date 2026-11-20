"""ReflectionEngine — extract durable facts from session messages.

Two extraction paths, selected by ``mode``:

* ``keyword`` — legacy regex/keyword scan. Zero LLM cost. Default for
  backward compatibility. Cheap but coarse: misses anything that doesn't
  literally contain ``prefer / always / never / rule / procedure``.

* ``llm`` / ``llm_main`` — engineered prompt from ``_review_prompts.py``
  with anti-patterns and PATCH>CREATE hierarchy. Requires an LLM. Uses
  the host-provided evolver LLM (``llm``) or main LLM (``llm_main``).

* ``hybrid`` — runs both, persists ONLY the keyword facts (preserving
  legacy behaviour), and logs the diff to ``reflection_audit`` for offline
  comparison before promoting LLM mode to default.

All modes fall back to keyword on LLM failure (parse error, timeout, etc.)
so a flaky model never silently drops the close_session signal.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field

from symbiote.core._review_prompts import (
    REFLECTION_PROMPT,
    SESSION_SUMMARY_PROMPT,
    render_prompt,
)
from symbiote.core.llm_usage import llm_call
from symbiote.core.models import MemoryEntry, MemoryType
from symbiote.core.origin import speaker
from symbiote.core.ports import LLMPort, MemoryPort, MessagePort, StoragePort
from symbiote.core.subjects import subject_kwargs

_T = TypeVar("_T")

_log = logging.getLogger(__name__)


def _clamped_importance(value: object) -> float | None:
    """Importance from LLM output as a float in [0, 1], or None if unusable."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return min(1.0, max(0.0, number))


class ReflectionResult(BaseModel):
    """Result of a reflection pass over session messages."""

    session_id: str
    summary: str
    extracted_facts: list[dict] = Field(default_factory=list)
    discarded_count: int = 0
    persisted_count: int = 0
    mode_used: str = "keyword"
    llm_error: str | None = None


# ── Keyword path (legacy) ──────────────────────────────────────────────────

# Whole words that signal a durable fact stated by the user (English and
# Portuguese), mapped to the fact type. Checked in this order; one fact per
# message.
_KEYWORD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"\b(prefer|prefers|preferred|preference|prefiro|prefere|preferimos|"
            r"preferem|prefer[eê]ncia)\b",
            re.IGNORECASE,
        ),
        "preference",
    ),
    (
        re.compile(
            r"\b(decided|decision|decidi|decidimos|decidiu|decidiram|decis[aã]o)\b",
            re.IGNORECASE,
        ),
        "decision",
    ),
    (
        re.compile(
            r"\b(always|never|sempre|nunca|jamais|rules?|regras?|constraints?|"
            r"restri[cç][aã]o|restri[cç][oõ]es)\b",
            re.IGNORECASE,
        ),
        "constraint",
    ),
    (
        re.compile(
            r"\b(conventions?|procedures?|conven[cç][aã]o|conven[cç][oõ]es|procedimentos?)\b",
            re.IGNORECASE,
        ),
        "procedural",
    ),
]

# Longer messages are not stored whole as a fact (the keyword path keeps the
# entire message as the memory text).
_MAX_KEYWORD_FACT_CHARS = 500

# Kept for callers that imported the old map; no longer used for matching.
_KEYWORD_MAP: dict[str, str] = {
    "prefer": "preference",
    "always": "constraint",
    "never": "constraint",
    "rule": "constraint",
    "convention": "procedural",
    "procedure": "procedural",
    "constraint": "constraint",
}

# Noise patterns — short or common acknowledgments
_NOISE_PATTERNS: set[str] = {
    "ok",
    "okay",
    "yes",
    "no",
    "thanks",
    "thank you",
    "got it",
    "sure",
    "right",
    "yep",
    "nope",
    "cool",
    "nice",
    "great",
    "done",
    "alright",
    "understood",
    "noted",
    "ack",
    "k",
}

_NOISE_RE = re.compile(
    r"^(" + "|".join(re.escape(p) for p in _NOISE_PATTERNS) + r")[.!?]*$",
    re.IGNORECASE,
)

# ── Defense-in-depth: anti-pattern guard for LLM output ────────────────────

# If LLM output content matches any of these, drop entries typed `constraint`
# (most dangerous — hardens into permanent self-imposed refusal). Lower-risk
# types are downgraded in importance instead of dropped.
_BLOCK_PATTERNS = [
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bbroken\b.*\btool\b|\btool\b.*\bbroken\b", re.IGNORECASE),
    re.compile(r"does\s?n['’]?t\s+work", re.IGNORECASE),
    re.compile(r"missing\s+(binary|package|dependency)", re.IGNORECASE),
    re.compile(r"no\s+such\s+file", re.IGNORECASE),
    re.compile(r"not\s+installed", re.IGNORECASE),
]


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class ReflectionEngine:
    """Extracts durable knowledge from session messages and persists it.

    Modes:
        keyword  — heuristic, no LLM (default, backward compatible)
        llm      — engineered LLM prompt using ``evolver_llm`` (cheap aux)
        llm_main — engineered LLM prompt using main ``llm`` (opt-in cost)
        hybrid   — run both, persist keyword only, log diff to audit

    Summary modes (``EnvironmentConfig.session_summary_mode``), independent
    of the mode above:
        concat — the non-noise messages joined into one paragraph (default)
        llm    — written by ``summary_llm``; ``concat`` when there is no
                 model, the call fails or it answers nothing
        off    — no summary
    """

    def __init__(
        self,
        memory_store: MemoryPort,
        messages: MessagePort,
        *,
        llm: LLMPort | None = None,
        mode: str = "keyword",
        storage: StoragePort | None = None,
        max_tokens: int = 4000,
        summary_mode: str = "concat",
        summary_llm: LLMPort | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._messages = messages
        self._llm = llm
        self._storage = storage  # for reflection_audit writes; optional
        self._max_tokens = max_tokens
        self._summary_mode = summary_mode if summary_mode in {"concat", "llm", "off"} else "concat"
        self._summary_llm = summary_llm

        # Validate mode; fall back to keyword if no LLM is available.
        if mode not in {"keyword", "llm", "llm_main", "hybrid"}:
            _log.warning("Unknown reflection mode %r; falling back to keyword", mode)
            mode = "keyword"
        if mode in {"llm", "llm_main", "hybrid"} and llm is None:
            _log.warning(
                "Reflection mode %r requested but no LLM provided; falling back to keyword",
                mode,
            )
            mode = "keyword"
        self._mode = mode

    # ── public API ─────────────────────────────────────────────────────

    def session_summary(self, session_id: str, *, limit: int = 50) -> str | None:
        """The summary of a whole session in this engine's summary mode:
        ``None`` in mode ``off``, else the text (``""`` for no messages)."""
        if self._summary_mode == "off":
            return None
        with llm_call("session_summary", session_id=session_id):
            return self._generate_summary(self._fetch_messages(session_id, limit))

    def reflect_session(
        self,
        session_id: str,
        symbiote_id: str,
        *,
        limit: int = 50,
        summary: str | None = None,
    ) -> ReflectionResult:
        """Run reflection on a session's messages, dispatching by mode.

        Only messages not reflected before are considered (when the message
        store tracks a watermark), so closing a reopened session does not
        learn the same messages twice. ``summary``, when given, is the
        session's summary already written by the caller (``session_summary``)
        and is returned as it is instead of being generated again.
        """
        get_mark = getattr(self._messages, "get_reflection_watermark", None)
        since = get_mark(session_id) if get_mark is not None else None
        messages = self._fetch_messages(session_id, limit, since=since)
        mark: Callable[[], None] | None = None
        set_mark = getattr(self._messages, "set_reflection_watermark", None)
        if set_mark is not None and messages and messages[-1].get("created_at"):
            last = messages[-1]["created_at"]

            def mark() -> None:
                set_mark(session_id, last)

        with llm_call("reflection", symbiote_id, session_id):
            return self._reflect_messages(messages, session_id, symbiote_id, mark, summary)

    def _commit(self, persist: Callable[[], _T], mark: Callable[[], None] | None) -> _T:
        """Store the facts and move the watermark as one unit: the same
        messages are never learned twice, nor skipped without being learned."""
        transaction = (
            self._storage.transaction()
            if self._storage is not None
            else contextlib.nullcontext()
        )
        with transaction:
            out = persist()
            if mark is not None:
                mark()
        return out

    def _reflect_messages(
        self,
        messages: list[dict],
        session_id: str,
        symbiote_id: str,
        mark: Callable[[], None] | None = None,
        summary: str | None = None,
    ) -> ReflectionResult:
        discarded_count = sum(1 for m in messages if self._is_noise(m["content"]))
        if summary is None:
            summary = self._generate_summary(messages)
        # The session of an end user reflects inside that user's partition:
        # the facts it stores belong to the user (``MemoryStore.store``), and
        # the memories offered to the LLM as PATCH targets are only theirs.
        subject = subject_kwargs(self._memory_store, session_id)

        if self._mode == "keyword":
            facts = self._extract_keyword(messages)
            persisted = self._commit(
                lambda: self._persist_facts(facts, symbiote_id, session_id), mark
            )
            return ReflectionResult(
                session_id=session_id,
                summary=summary,
                extracted_facts=facts,
                discarded_count=discarded_count,
                persisted_count=persisted,
                mode_used="keyword",
            )

        if self._mode == "hybrid":
            keyword_facts = self._extract_keyword(messages)
            llm_facts, llm_error = self._extract_llm_safe(messages, symbiote_id, subject)
            # Persist keyword only (safer migration); log diff for audit.
            persisted = self._commit(
                lambda: self._persist_facts(keyword_facts, symbiote_id, session_id), mark
            )
            self._audit_log(
                session_id, symbiote_id, "hybrid",
                keyword_facts, llm_facts, llm_error,
            )
            return ReflectionResult(
                session_id=session_id,
                summary=summary,
                extracted_facts=keyword_facts,
                discarded_count=discarded_count,
                persisted_count=persisted,
                mode_used="hybrid",
                llm_error=llm_error,
            )

        # llm / llm_main — engineered LLM extraction
        llm_facts, llm_error = self._extract_llm_safe(messages, symbiote_id, subject)
        if llm_error is not None:
            # Hard fallback to keyword on LLM failure so we never produce
            # zero output silently on the close_session path.
            _log.warning(
                "Reflection LLM failed (%s); falling back to keyword for session %s",
                llm_error, session_id,
            )
            keyword_facts = self._extract_keyword(messages)
            persisted = self._commit(
                lambda: self._persist_facts(keyword_facts, symbiote_id, session_id), mark
            )
            self._audit_log(
                session_id, symbiote_id, self._mode,
                keyword_facts, [], llm_error,
            )
            return ReflectionResult(
                session_id=session_id,
                summary=summary,
                extracted_facts=keyword_facts,
                discarded_count=discarded_count,
                persisted_count=persisted,
                mode_used=self._mode,
                llm_error=llm_error,
            )

        persisted, skipped = self._commit(
            lambda: self._apply_llm_facts(llm_facts, symbiote_id, session_id, subject),
            mark,
        )
        skipped_note = (
            f"skipped {skipped} invalid fact(s) from the LLM" if skipped else None
        )
        self._audit_log(session_id, symbiote_id, self._mode, [], llm_facts, skipped_note)
        return ReflectionResult(
            session_id=session_id,
            summary=summary,
            extracted_facts=llm_facts,
            discarded_count=discarded_count,
            persisted_count=persisted,
            mode_used=self._mode,
        )

    def reflect_task(
        self,
        session_id: str,
        symbiote_id: str,
        task_description: str,
        *,
        limit: int = 20,
    ) -> ReflectionResult:
        """Lighter reflection scoped to task context.

        Currently uses the keyword path regardless of ``mode`` because task
        scope is too narrow for the LLM hierarchy to pay off. Revisit if a
        concrete use case shows up.
        """
        messages = self._fetch_messages(session_id, limit)
        facts = self._extract_keyword(messages)
        with llm_call("reflection", symbiote_id, session_id):
            summary = f"Task: {task_description}\n{self._generate_summary(messages)}"
        discarded_count = sum(1 for m in messages if self._is_noise(m["content"]))

        persisted = 0
        for fact in facts:
            mem_type: MemoryType = fact["type"] if fact["type"] in (
                "preference", "constraint", "procedural", "factual", "decision",
            ) else "reflection"
            entry = MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type=mem_type,
                scope="global",
                content=fact["content"],
                tags=[fact["type"], "task"],
                importance=fact["importance"],
                source="reflection",
            )
            self._memory_store.store(entry)
            persisted += 1

        return ReflectionResult(
            session_id=session_id,
            summary=summary,
            extracted_facts=facts,
            discarded_count=discarded_count,
            persisted_count=persisted,
            mode_used="keyword",
        )

    # ── keyword extraction (legacy) ────────────────────────────────────

    def _extract_keyword(self, messages: list[dict]) -> list[dict]:
        """Heuristic extraction: user messages that state a durable fact.

        Only the user's own messages count (an assistant reply saying "I
        always double-check" is not a fact about the user), keywords must be
        whole words, and long messages are skipped.
        """
        facts: list[dict] = []
        for msg in messages:
            if msg.get("role", "user") != "user" or msg.get("origin"):
                continue  # nor an input the host wrote (a wake, a webhook)
            content = msg["content"]
            if self._is_noise(content) or len(content) > _MAX_KEYWORD_FACT_CHARS:
                continue
            for pattern, fact_type in _KEYWORD_PATTERNS:
                if pattern.search(content):
                    importance = 0.8 if fact_type == "constraint" else 0.6
                    facts.append(
                        {
                            "content": content,
                            "type": fact_type,
                            "importance": importance,
                        }
                    )
                    break  # one fact per message
        return facts

    # Back-compat alias for any external caller / test that used the old name.
    _extract_durable_facts = _extract_keyword

    def _is_noise(self, content: str) -> bool:
        """Return True if message is too short or matches noise patterns."""
        stripped = content.strip()
        if len(stripped) < 10:
            return True
        return bool(_NOISE_RE.match(stripped))

    def _generate_summary(self, messages: list[dict]) -> str:
        """The summary of ``messages`` in this engine's summary mode."""
        if self._summary_mode == "off":
            return ""
        if self._summary_mode == "llm":
            written = self._summarize_llm(messages)
            if written:
                return written
        return self._concat_summary(messages)

    def _concat_summary(self, messages: list[dict]) -> str:
        """Concatenate non-noise messages into a summary paragraph."""
        parts = [
            msg["content"]
            for msg in messages
            if not self._is_noise(msg["content"])
        ]
        return " ".join(parts)

    def _summarize_llm(self, messages: list[dict]) -> str | None:
        """A summary written by the summary model, or ``None`` when there is
        no model, nothing worth summarizing, or the call fails (the caller
        then falls back to the concatenation)."""
        if self._summary_llm is None:
            return None
        kept = [m for m in messages if not self._is_noise(m["content"])]
        if not kept:
            return None
        prompt = render_prompt(
            SESSION_SUMMARY_PROMPT, messages=self._format_messages_for_prompt(kept)
        )
        try:
            with llm_call("session_summary"):
                response = self._summary_llm.complete([{"role": "user", "content": prompt}])
        except Exception as exc:
            _log.warning("Session summary LLM failed (%s); using the concatenation", exc)
            return None
        text = response if isinstance(response, str) else getattr(response, "content", "")
        return (text or "").strip() or None

    # ── LLM extraction (engineered prompt) ─────────────────────────────

    def _extract_llm_safe(
        self,
        messages: list[dict],
        symbiote_id: str,
        subject: dict[str, Any] | None = None,
    ) -> tuple[list[dict], str | None]:
        """Extract via LLM; return (facts, error_string_or_None).

        Never raises — all exceptions are caught and returned as the error
        string so the caller can decide whether to fall back to keyword.
        """
        if self._llm is None:
            return [], "no LLM configured"
        try:
            facts = self._extract_llm(messages, symbiote_id, subject)
            return facts, None
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"

    def _extract_llm(
        self,
        messages: list[dict],
        symbiote_id: str,
        subject: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Run the engineered prompt and return validated, guarded facts."""
        msg_text = self._format_messages_for_prompt(messages)
        existing = self._format_existing_memories(symbiote_id, subject=subject)
        prompt = render_prompt(
            REFLECTION_PROMPT,
            messages=msg_text,
            existing_memories=existing,
        )

        if self._llm is None:
            raise RuntimeError("LLM reflection needs an LLM adapter")
        response = self._llm.complete([{"role": "user", "content": prompt}])
        text = response if isinstance(response, str) else getattr(response, "content", "")
        return self._parse_and_guard(text)

    def _format_messages_for_prompt(self, messages: list[dict]) -> str:
        """Render messages within the configured token budget (chars/4 heuristic)."""
        budget_chars = self._max_tokens * 4
        lines: list[str] = []
        total = 0
        # Prefer most recent messages if we have to truncate
        for msg in reversed(messages):
            who = speaker(msg.get("role", "?"), msg.get("origin"))
            line = f"[{who}]: {msg.get('content', '')}"
            if total + len(line) > budget_chars:
                break
            lines.append(line)
            total += len(line)
        lines.reverse()
        return "\n".join(lines)

    # Types worth offering as PATCH candidates. Excludes ephemeral
    # (working/session_summary), meta (reflection/semantic_note), and
    # handoff — these aren't durable knowledge the LLM should overwrite.
    _PATCH_CANDIDATE_TYPES = (
        "preference",
        "constraint",
        "procedural",
        "decision",
        "factual",
    )

    @staticmethod
    def _own_partition(subject: dict[str, Any] | None) -> dict[str, Any]:
        """Keywords that restrict a ``get_by_type`` to the partition the
        session may write to: the end user's own (``subject`` set) or, with
        none, the shared one — which is what a port reads by default."""
        return {**subject, "include_shared": False} if subject else {}

    def _format_existing_memories(
        self,
        symbiote_id: str,
        per_type_limit: int = 5,
        *,
        subject: dict[str, Any] | None = None,
    ) -> str:
        """Fetch top-N relevant memories so the LLM can pick PATCH targets.

        Returns a compact "ID :: type :: content" rendering keyed for short
        token cost. IDs are shortened to first 8 chars for prompt economy;
        the parser accepts both short and full IDs when looking up target_id.

        Only memories of the session's own partition are offered: a PATCH
        from the session of an end user must not reach a shared memory.
        """
        try:
            recent: list = []
            own = self._own_partition(subject)
            for t in self._PATCH_CANDIDATE_TYPES:
                recent.extend(
                    self._memory_store.get_by_type(
                        symbiote_id, t, limit=per_type_limit, **own
                    )
                )
        except Exception:
            return "(none available)"
        if not recent:
            return "(none)"

        # Sort by importance desc, then last_used_at desc (recent + important first).
        def _key(m):
            return (
                -float(getattr(m, "importance", 0.0)),
                -(getattr(m, "last_used_at", None).timestamp() if getattr(m, "last_used_at", None) else 0),
            )
        with contextlib.suppress(Exception):
            recent.sort(key=_key)

        lines: list[str] = []
        for m in recent:
            full_id = str(getattr(m, "id", ""))
            short_id = full_id[:8] if full_id else "?"
            mtype = getattr(m, "type", "?")
            content = (getattr(m, "content", "") or "")[:160]
            lines.append(f"- {short_id} :: {mtype} :: {content}")
        return "\n".join(lines)

    def _resolve_target_id(
        self,
        short_or_full: str,
        symbiote_id: str,
        subject: dict[str, Any] | None = None,
    ) -> str | None:
        """Resolve a target_id from LLM output (may be short 8-char prefix) to a full id.

        Returns the full id if exactly one active memory matches, else None.
        A memory outside the session's own partition (``subject``) never
        matches.
        """
        if not short_or_full:
            return None
        own_subject = (subject or {}).get("subject_key")
        # If it looks like a full UUID, use it directly.
        if len(short_or_full) >= 32:
            # Ownership is checked here rather than via a ``symbiote_id``
            # kwarg so host MemoryPort implementations keep working: an LLM
            # must never be able to patch another symbiote's memory, nor a
            # memory of another partition of the same symbiote.
            existing = self._memory_store.get(short_or_full)
            if existing is None or getattr(existing, "symbiote_id", None) != symbiote_id:
                return None
            if getattr(existing, "subject_key", None) != own_subject:
                return None
            return short_or_full
        # Short prefix — look up the unique active match.
        try:
            own = self._own_partition(subject)
            for t in self._PATCH_CANDIDATE_TYPES:
                for m in self._memory_store.get_by_type(symbiote_id, t, limit=50, **own):
                    if str(getattr(m, "id", "")).startswith(short_or_full):
                        return m.id
        except Exception:
            return None
        return None

    def _parse_and_guard(self, text: str) -> list[dict]:
        """Parse JSON array from LLM, apply defense-in-depth guard."""
        cleaned = text.strip()
        # Strip markdown fences if present
        if "```" in cleaned:
            start = cleaned.find("[")
            end = cleaned.rfind("]") + 1
            if start >= 0 and end > start:
                cleaned = cleaned[start:end]

        try:
            raw = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM did not return valid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError("LLM did not return a JSON array")

        guarded: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict) or "content" not in entry:
                continue
            if not self._passes_guard(entry):
                _log.info(
                    "Reflection guard dropped entry: %s",
                    entry.get("content", "")[:80],
                )
                continue
            guarded.append(entry)
        return guarded

    @staticmethod
    def _passes_guard(entry: dict) -> bool:
        """Defense-in-depth: drop dangerous constraints, downgrade overconfident
        entries, normalize tags.

        Tags normalization rationale: the prompt declares `tags` REQUIRED and
        non-empty, but LLMs ignore that ~5-15% of the time even on good models.
        Rather than reject the whole entry over a missing tag (and lose a
        genuinely useful preference / fix), we auto-fill ``[type]`` as a
        last-resort tag and log a warning so the audit shows prompt drift.
        Empty / type-only tag lists are visible in ``reflection_audit`` for
        prompt tuning.
        """
        content = str(entry.get("content", "")).lower()
        etype = entry.get("type", "factual")
        if etype == "constraint" and any(p.search(content) for p in _BLOCK_PATTERNS):
            return False
        importance = entry.get("importance")
        if isinstance(importance, int | float) and importance > 0.9 and etype != "constraint":
            entry["importance"] = 0.7

        # Normalize tags: must be list[str], lowercased, deduplicated, non-empty.
        raw_tags = entry.get("tags")
        tags: list[str] = []
        if isinstance(raw_tags, list):
            seen: set[str] = set()
            for t in raw_tags:
                if not isinstance(t, str):
                    continue
                norm = t.strip().lower()
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                tags.append(norm)
        if not tags:
            _log.warning(
                "LLM returned no usable tags for %s entry; falling back to [%s]",
                etype, etype,
            )
            tags = [etype]
        entry["tags"] = tags
        return True

    # ── persistence ────────────────────────────────────────────────────

    def _persist_facts(
        self, facts: list[dict], symbiote_id: str, session_id: str
    ) -> int:
        """Persist keyword-extracted facts (legacy shape: type/content/importance)."""
        persisted = 0
        for fact in facts:
            mem_type: MemoryType = fact["type"] if fact["type"] in (
                "preference", "constraint", "procedural", "factual", "decision",
            ) else "reflection"
            entry = MemoryEntry(
                symbiote_id=symbiote_id,
                session_id=session_id,
                type=mem_type,
                scope="global",
                content=fact["content"],
                tags=[fact["type"]],
                importance=fact["importance"],
                source="reflection",
            )
            self._memory_store.store(entry)
            persisted += 1
        return persisted

    def _apply_llm_facts(
        self,
        facts: list[dict],
        symbiote_id: str,
        session_id: str,
        subject: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        """Persist LLM-extracted facts honouring action=create|patch.

        PATCH path (Sprint 2 first-class):
          - target_id is resolved against active memories (full UUID or 8-char prefix).
          - If unique active match: MemoryPort.update() patches in place. No new row.
          - If no match or update returns False (id gone / inactive): falls back to
            CREATE so the lesson is not lost.
          - Adapters that don't implement update() also fall back to CREATE.
        """
        persisted = 0
        skipped = 0
        has_update = hasattr(self._memory_store, "update")
        for fact in facts:
            if not isinstance(fact, dict):
                skipped += 1
                continue
            content = fact.get("content")
            if not isinstance(content, str) or not content.strip():
                skipped += 1
                continue
            mem_type = fact.get("type", "factual")
            if mem_type not in {"preference", "constraint", "procedural", "factual", "decision"}:
                mem_type = "reflection"
            importance = _clamped_importance(fact.get("importance", 0.5))
            if importance is None:
                _log.info("Skipping reflected fact with invalid importance %r", fact.get("importance"))
                skipped += 1
                continue
            tags = fact.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            tags = [str(t) for t in tags]

            action = fact.get("action", "create")
            raw_target = fact.get("target_id")

            if action == "patch" and raw_target and has_update:
                resolved = self._resolve_target_id(str(raw_target), symbiote_id, subject)
                if resolved is not None:
                    try:
                        ok = self._memory_store.update(  # type: ignore[attr-defined]
                            resolved,
                            content=content,
                            importance=importance,
                            tags=tags,
                            # For an end user's session the store itself
                            # refuses a target of another partition.
                            **(subject or {}),
                        )
                        if ok:
                            persisted += 1
                            continue
                        _log.info(
                            "PATCH target %s resolved but update returned False; "
                            "falling back to CREATE", resolved,
                        )
                    except Exception as exc:
                        _log.warning(
                            "PATCH on memory %s failed (%s); falling back to CREATE",
                            resolved, exc,
                        )
                else:
                    _log.info(
                        "PATCH target_id %r did not resolve to an active memory; "
                        "falling back to CREATE", raw_target,
                    )

            try:
                entry = MemoryEntry(
                    symbiote_id=symbiote_id,
                    session_id=session_id,
                    type=mem_type,
                    scope="global",
                    content=content,
                    tags=tags,
                    importance=importance,
                    source="reflection",
                )
            except ValueError as exc:  # pydantic ValidationError is a ValueError
                _log.info("Skipping reflected fact that does not validate: %s", exc)
                skipped += 1
                continue
            self._memory_store.store(entry)
            persisted += 1
        return persisted, skipped

    # ── audit ──────────────────────────────────────────────────────────

    def _audit_log(
        self,
        session_id: str,
        symbiote_id: str,
        mode: str,
        keyword_facts: list[dict],
        llm_facts: list[dict],
        llm_error: str | None,
    ) -> None:
        """Best-effort write to reflection_audit. Failures are logged, not raised."""
        if self._storage is None:
            return
        try:
            self._storage.execute(
                "INSERT INTO reflection_audit "
                "(id, session_id, symbiote_id, mode, keyword_facts_json, "
                "llm_facts_json, llm_error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    session_id,
                    symbiote_id,
                    mode,
                    json.dumps(keyword_facts, ensure_ascii=False),
                    json.dumps(llm_facts, ensure_ascii=False),
                    llm_error,
                    _utcnow_iso(),
                ),
            )
        except Exception as exc:
            _log.warning("reflection_audit write failed: %s", exc)

    # ── private helpers ────────────────────────────────────────────────

    def _fetch_messages(
        self, session_id: str, limit: int, *, since: str | None = None
    ) -> list[dict]:
        """Fetch last N messages for a session via MessagePort."""
        if since is None:
            return self._messages.get_messages(session_id, limit)
        return self._messages.get_messages(session_id, limit, since=since)
