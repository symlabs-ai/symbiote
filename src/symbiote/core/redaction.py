"""Redaction of secrets in everything the kernel persists.

The redactor recognizes a secret in two ways:

* **Annotated parameters.** A tool descriptor lists the parameters that carry
  secrets (``ToolDescriptor.secret_params``). When such a tool is called, the
  values are masked in the records of the call (audit log, execution trace)
  and remembered as literals, so the same text is also removed wherever else
  it would be stored.
* **The host's function.** ``fn(text) -> text`` installed with
  ``kernel.set_redactor`` sees every piece of text before it is stored and
  returns it without the secrets it recognizes (patterns, known values).

Redaction happens at the storage boundary (``RedactingStorage`` wraps the
kernel's storage adapter) and where skill files are written. The model still
sees the real value during the turn that uses it; later turns read the
redacted history.

What is covered: the tables that hold conversation-derived content (see
``REDACTED_TABLES``). Configuration the host wrote on purpose (HTTP tool
configs, personas, knowledge, API keys, environment configs) is left alone, as
are identifiers and timestamps.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from typing import Any

from symbiote.core.ports import StoragePort, WriteResult

logger = logging.getLogger(__name__)

DEFAULT_REPLACEMENT = "[REDACTED]"
REDACTION_FAILED = "[REDACTED: the host redactor failed]"
# A literal shorter than this is masked in the tool call's own records but
# not learned: replacing "42" everywhere would shred unrelated text.
MIN_LITERAL_LENGTH = 4
_MAX_LITERALS = 10_000

# Tables whose text comes from conversations, and the statements redacted on
# each. ``sessions`` is redacted on UPDATE only (the summary): its INSERT
# carries ``external_key``, the host's own lookup key.
REDACTED_TABLES: dict[str, frozenset[str]] = {
    "messages": frozenset({"INSERT", "UPDATE"}),
    "sessions": frozenset({"UPDATE"}),
    "memory_entries": frozenset({"INSERT", "UPDATE"}),
    "audit_log": frozenset({"INSERT", "UPDATE"}),
    "execution_traces": frozenset({"INSERT", "UPDATE"}),
    "reflection_audit": frozenset({"INSERT", "UPDATE"}),
    "skill_review_audit": frozenset({"INSERT", "UPDATE"}),
    "dream_reports": frozenset({"INSERT", "UPDATE"}),
    "decisions": frozenset({"INSERT", "UPDATE"}),
    "process_instances": frozenset({"INSERT", "UPDATE"}),
    "artifacts": frozenset({"INSERT", "UPDATE"}),
    "harness_versions": frozenset({"INSERT", "UPDATE"}),
}
# Where a secret learned in the middle of a session may already be stored
# (the user's message is saved before the tool call that reveals the secret).
_SESSION_TEXT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("messages", "content"),
    ("execution_traces", "steps_json"),
    ("audit_log", "params_json"),
    ("memory_entries", "content"),
)

_STATEMENT = re.compile(
    r"\s*(INSERT)\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_]\w*)|\s*(UPDATE)\s+([A-Za-z_]\w*)",
    re.IGNORECASE,
)
# Identifiers and timestamps are never rewritten, whatever the secret is.
_OPAQUE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


class Redactor:
    """Knows the secrets and removes them from text and from tool parameters."""

    def __init__(self, replacement: str = DEFAULT_REPLACEMENT) -> None:
        self.replacement = replacement
        self._fn: Callable[[str], str] | None = None
        self._literals: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    # ── configuration ──────────────────────────────────────────────────

    def set_function(self, fn: Callable[[str], str] | None) -> None:
        """Install (or remove) the host's ``fn(text) -> text``."""
        self._fn = fn

    def add_secret(self, value: Any) -> bool:
        """Remember a literal to remove from everything stored from now on.

        Returns True when the value is new and long enough to be learned.
        """
        text = value if isinstance(value, str) else None
        if text is None and isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value)
        if text is None or len(text) < MIN_LITERAL_LENGTH or text == self.replacement:
            return False
        with self._lock:
            known = text in self._literals
            self._literals[text] = None
            self._literals.move_to_end(text)
            while len(self._literals) > _MAX_LITERALS:
                self._literals.popitem(last=False)
        return not known

    @property
    def active(self) -> bool:
        return self._fn is not None or bool(self._literals)

    # ── text ───────────────────────────────────────────────────────────

    def redact_text(self, text: str) -> str:
        """``text`` without the secrets this redactor recognizes."""
        if not text or not self.active:
            return text
        with self._lock:
            literals = sorted(self._literals, key=len, reverse=True)
        for literal in literals:
            if literal in text:
                text = text.replace(literal, self.replacement)
        if self._fn is not None:
            try:
                result = self._fn(text)
            except Exception:
                # Fail closed: what could not be checked is not stored.
                logger.exception("host redactor failed; text withheld")
                return REDACTION_FAILED
            if not isinstance(result, str):
                logger.error("host redactor returned %s; text withheld", type(result).__name__)
                return REDACTION_FAILED
            text = result
        return text

    def redact_value(self, value: Any) -> Any:
        """Redact every string inside a JSON-like value (dict keys excluded)."""
        if isinstance(value, str):
            return value if _OPAQUE.fullmatch(value) else self.redact_text(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        return value

    def redact_param(self, value: Any) -> Any:
        """Redact one SQL parameter. Non-strings, identifiers and timestamps
        pass through; JSON documents are redacted leaf by leaf so they stay
        valid JSON."""
        if not isinstance(value, str) or not value or _OPAQUE.fullmatch(value):
            return value
        if value[0] in "{[":
            try:
                document = json.loads(value)
            except ValueError:
                return self.redact_text(value)
            redacted = self.redact_value(document)
            return value if redacted == document else json.dumps(redacted)
        return self.redact_text(value)

    # ── annotated tool parameters ──────────────────────────────────────

    def learn(self, params: Any, secret_paths: Iterable[str]) -> list[str]:
        """Remember the values at ``secret_paths``; returns the new literals."""
        new: list[str] = []
        for path in secret_paths:
            for value in _values_at(params, path.split(".")):
                for leaf in _leaves(value):
                    if self.add_secret(leaf):
                        new.append(leaf if isinstance(leaf, str) else str(leaf))
        return new

    def mask_params(self, params: Any, secret_paths: Iterable[str]) -> Any:
        """A copy of ``params`` with the values at ``secret_paths`` replaced,
        whatever their length or type."""
        paths = [p.split(".") for p in secret_paths]
        if not paths or not isinstance(params, dict):
            return params
        masked = json.loads(json.dumps(params, default=str))
        for path in paths:
            _mask_at(masked, path, self.replacement)
        return masked


def _values_at(node: Any, path: list[str]) -> list[Any]:
    if not path:
        return [node]
    if isinstance(node, list):
        return [v for item in node for v in _values_at(item, path)]
    if isinstance(node, dict) and path[0] in node:
        return _values_at(node[path[0]], path[1:])
    return []


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _leaves(v)]
    if isinstance(value, list):
        return [leaf for v in value for leaf in _leaves(v)]
    return [value]


def _mask_at(node: Any, path: list[str], replacement: str) -> None:
    if isinstance(node, list):
        for item in node:
            _mask_at(item, path, replacement)
        return
    if not isinstance(node, dict) or path[0] not in node:
        return
    if len(path) == 1:
        node[path[0]] = replacement
    else:
        _mask_at(node[path[0]], path[1:], replacement)


class RedactingStorage:
    """``StoragePort`` that redacts what is written to the conversation tables.

    Everything else is passed to the wrapped adapter untouched, including
    attribute access, so code that reaches for adapter internals keeps working.
    """

    _OWN = frozenset({"_inner", "_redactor"})

    def __init__(self, inner: StoragePort, redactor: Redactor) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_redactor", redactor)

    @property
    def inner(self) -> StoragePort:
        """The wrapped adapter (writes through it are not redacted)."""
        return self._inner

    def init_schema(self) -> None:
        self._inner.init_schema()

    def execute(self, sql: str, params: tuple | None = None) -> WriteResult:
        if params and self._redactor.active and _is_redacted_statement(sql):
            params = tuple(self._redactor.redact_param(p) for p in params)
        return self._inner.execute(sql, params)

    def fetch_one(self, sql: str, params: tuple | None = None) -> dict | None:
        return self._inner.fetch_one(sql, params)

    def fetch_all(self, sql: str, params: tuple | None = None) -> list[dict]:
        return self._inner.fetch_all(sql, params)

    def transaction(self) -> AbstractContextManager[None]:
        return self._inner.transaction()

    def close(self) -> None:
        self._inner.close()

    def scrub_session(self, session_id: str, literals: Iterable[str], replacement: str) -> None:
        """Remove ``literals`` from what this session already stored.

        Used when a secret is recognized in the middle of a session: the
        message that carried it was saved before the tool call that revealed
        it. JSON columns hold the JSON-escaped form, so both are replaced.
        """
        forms: set[str] = set()
        for literal in literals:
            forms.add(literal)
            forms.add(json.dumps(literal)[1:-1])
            forms.add(json.dumps(literal, ensure_ascii=False)[1:-1])
        with self._inner.transaction():
            for table, column in _SESSION_TEXT_COLUMNS:
                for form in sorted(forms, key=len, reverse=True):
                    self._inner.execute(
                        f"UPDATE {table} SET {column} = REPLACE({column}, ?, ?) "  # noqa: S608
                        "WHERE session_id = ?",
                        (form, replacement, session_id),
                    )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


def _is_redacted_statement(sql: str) -> bool:
    match = _STATEMENT.match(sql)
    if match is None:
        return False
    verb = (match.group(1) or match.group(3)).upper()
    table = (match.group(2) or match.group(4)).lower()
    return verb in REDACTED_TABLES.get(table, frozenset())


__all__ = [
    "DEFAULT_REPLACEMENT",
    "REDACTED_TABLES",
    "RedactingStorage",
    "Redactor",
]
