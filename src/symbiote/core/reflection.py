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
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from symbiote.core._review_prompts import REFLECTION_PROMPT, render_prompt
from symbiote.core.models import MemoryEntry
from symbiote.core.ports import LLMPort, MemoryPort, MessagePort, StoragePort

_log = logging.getLogger(__name__)


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

# Keywords that signal durable facts, mapped to fact type
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
    ) -> None:
        self._memory_store = memory_store
        self._messages = messages
        self._llm = llm
        self._storage = storage  # for reflection_audit writes; optional
        self._max_tokens = max_tokens

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

    def reflect_session(
        self, session_id: str, symbiote_id: str, *, limit: int = 50
    ) -> ReflectionResult:
        """Run reflection on a session's messages, dispatching by mode."""
        messages = self._fetch_messages(session_id, limit)
        discarded_count = sum(1 for m in messages if self._is_noise(m["content"]))
        summary = self._generate_summary(messages)

        if self._mode == "keyword":
            facts = self._extract_keyword(messages)
            persisted = self._persist_facts(facts, symbiote_id, session_id)
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
            llm_facts, llm_error = self._extract_llm_safe(messages, symbiote_id)
            # Persist keyword only (safer migration); log diff for audit.
            persisted = self._persist_facts(keyword_facts, symbiote_id, session_id)
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
        llm_facts, llm_error = self._extract_llm_safe(messages, symbiote_id)
        if llm_error is not None:
            # Hard fallback to keyword on LLM failure so we never produce
            # zero output silently on the close_session path.
            _log.warning(
                "Reflection LLM failed (%s); falling back to keyword for session %s",
                llm_error, session_id,
            )
            keyword_facts = self._extract_keyword(messages)
            persisted = self._persist_facts(keyword_facts, symbiote_id, session_id)
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

        persisted = self._apply_llm_facts(llm_facts, symbiote_id, session_id)
        self._audit_log(session_id, symbiote_id, self._mode, [], llm_facts, None)
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
        summary = f"Task: {task_description}\n{self._generate_summary(messages)}"
        discarded_count = sum(1 for m in messages if self._is_noise(m["content"]))

        persisted = 0
        for fact in facts:
            mem_type = fact["type"] if fact["type"] in (
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
        """Heuristic extraction: look for keyword signals in message content."""
        facts: list[dict] = []
        for msg in messages:
            content = msg["content"]
            if self._is_noise(content):
                continue
            content_lower = content.lower()
            for keyword, fact_type in _KEYWORD_MAP.items():
                if keyword in content_lower:
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
        """Concatenate non-noise messages into a summary paragraph."""
        parts = [
            msg["content"]
            for msg in messages
            if not self._is_noise(msg["content"])
        ]
        return " ".join(parts)

    # ── LLM extraction (engineered prompt) ─────────────────────────────

    def _extract_llm_safe(
        self, messages: list[dict], symbiote_id: str
    ) -> tuple[list[dict], str | None]:
        """Extract via LLM; return (facts, error_string_or_None).

        Never raises — all exceptions are caught and returned as the error
        string so the caller can decide whether to fall back to keyword.
        """
        if self._llm is None:
            return [], "no LLM configured"
        try:
            facts = self._extract_llm(messages, symbiote_id)
            return facts, None
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"

    def _extract_llm(self, messages: list[dict], symbiote_id: str) -> list[dict]:
        """Run the engineered prompt and return validated, guarded facts."""
        msg_text = self._format_messages_for_prompt(messages)
        existing = self._format_existing_memories(symbiote_id)
        prompt = render_prompt(
            REFLECTION_PROMPT,
            messages=msg_text,
            existing_memories=existing,
        )

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
            line = f"[{msg.get('role', '?')}]: {msg.get('content', '')}"
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

    def _format_existing_memories(self, symbiote_id: str, per_type_limit: int = 5) -> str:
        """Fetch top-N relevant memories so the LLM can pick PATCH targets.

        Returns a compact "ID :: type :: content" rendering keyed for short
        token cost. IDs are shortened to first 8 chars for prompt economy;
        the parser accepts both short and full IDs when looking up target_id.
        """
        try:
            recent: list = []
            for t in self._PATCH_CANDIDATE_TYPES:
                recent.extend(
                    self._memory_store.get_by_type(symbiote_id, t, limit=per_type_limit)
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

    def _resolve_target_id(self, short_or_full: str, symbiote_id: str) -> str | None:
        """Resolve a target_id from LLM output (may be short 8-char prefix) to a full id.

        Returns the full id if exactly one active memory matches, else None.
        """
        if not short_or_full:
            return None
        # If it looks like a full UUID, use it directly.
        if len(short_or_full) >= 32:
            existing = self._memory_store.get(short_or_full)
            return short_or_full if existing is not None else None
        # Short prefix — look up the unique active match.
        try:
            for t in self._PATCH_CANDIDATE_TYPES:
                for m in self._memory_store.get_by_type(symbiote_id, t, limit=50):
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
            mem_type = fact["type"] if fact["type"] in (
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
        self, facts: list[dict], symbiote_id: str, session_id: str
    ) -> int:
        """Persist LLM-extracted facts honouring action=create|patch.

        PATCH path (Sprint 2 first-class):
          - target_id is resolved against active memories (full UUID or 8-char prefix).
          - If unique active match: MemoryPort.update() patches in place. No new row.
          - If no match or update returns False (id gone / inactive): falls back to
            CREATE so the lesson is not lost.
          - Adapters that don't implement update() also fall back to CREATE.
        """
        persisted = 0
        has_update = hasattr(self._memory_store, "update")
        for fact in facts:
            content = fact.get("content")
            if not content:
                continue
            mem_type = fact.get("type", "factual")
            if mem_type not in {"preference", "constraint", "procedural", "factual", "decision"}:
                mem_type = "reflection"
            importance = float(fact.get("importance", 0.5))
            tags = fact.get("tags") or []
            if not isinstance(tags, list):
                tags = []

            action = fact.get("action", "create")
            raw_target = fact.get("target_id")

            if action == "patch" and raw_target and has_update:
                resolved = self._resolve_target_id(str(raw_target), symbiote_id)
                if resolved is not None:
                    try:
                        ok = self._memory_store.update(  # type: ignore[attr-defined]
                            resolved,
                            content=content,
                            importance=importance,
                            tags=tags,
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
            self._memory_store.store(entry)
            persisted += 1
        return persisted

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

    def _fetch_messages(self, session_id: str, limit: int) -> list[dict]:
        """Fetch last N messages for a session via MessagePort."""
        return self._messages.get_messages(session_id, limit)
