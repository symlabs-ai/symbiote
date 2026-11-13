"""Per-symbiote skill scoping (Option A).

When ``KernelConfig.skill_scope == "per_symbiote"``, each symbiote gets its
own read-write skill root under ``{skills_root}/<symbiote_id>/skills/`` plus a
shared, read-only "factory" catalogue (``skills_protected_roots``). This module
provides:

* ``active_symbiote`` — a ContextVar holding the symbiote_id driving the
  current turn. Set by the kernel at the start of ``message``/``message_async``
  so foreground tool handlers (``skill_manage`` / ``skill_view``), which receive
  only ``params`` from the gateway, can resolve the right per-symbiote store /
  loader. Mirrors ``core.provenance``'s contextvar pattern. The gateway's
  parallel path already does ``contextvars.copy_context()`` so the value
  propagates into worker threads.

* ``SkillScopeManager`` — a small factory + LRU cache of
  ``(SkillsLoader, SkillsStore)`` keyed by symbiote_id. Lazily built; cheap to
  rebuild (loaders just scan a directory).

Global mode does not use this module — the kernel keeps its single
store/loader exactly as before.
"""

from __future__ import annotations

import contextvars
import shutil
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from symbiote.skills.loader import SkillsLoader
from symbiote.skills.store import SkillsStore

# The symbiote whose turn is currently executing. ``None`` outside a turn (or
# in global mode). Tool handlers read this to scope themselves.
active_symbiote: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_symbiote",
    default=None,
)


def set_active_symbiote(symbiote_id: str | None) -> contextvars.Token:
    """Bind the active symbiote for the current context. Returns a reset Token."""
    return active_symbiote.set(symbiote_id)


def reset_active_symbiote(token: contextvars.Token) -> None:
    """Restore the previous active symbiote (call in a finally block)."""
    active_symbiote.reset(token)


def get_active_symbiote() -> str | None:
    """Return the symbiote_id driving the current turn, or None."""
    return active_symbiote.get()


# Default cap on how many per-symbiote (loader, store) pairs we keep warm.
# Loaders are cheap (idle dict + a directory scan on build), so this only
# bounds memory for hosts with many concurrent users.
_DEFAULT_MAX_CACHED = 256


class SkillScopeManager:
    """Factory + LRU cache of per-symbiote SkillsLoader / SkillsStore.

    Layout per symbiote::

        {base_root}/<symbiote_id>/skills/<name>/SKILL.md   (read-write)
        {protected_roots...}                               (read-only, shared)

    The write root is the symbiote's own directory; ``protected_roots`` are the
    shared factory catalogue (same for every symbiote, never written).
    """

    def __init__(
        self,
        base_root: Path,
        *,
        protected_roots: list[Path] | None = None,
        extra_roots: list[Path] | None = None,
        auto_promote_threshold: int = 0,
        max_cached: int = _DEFAULT_MAX_CACHED,
        read_only: bool = False,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self._read_only = read_only
        self._redact = redact
        self._base_root = Path(base_root)
        self._protected_roots = list(protected_roots or [])
        self._extra_roots = list(extra_roots or [])
        self._auto_promote_threshold = auto_promote_threshold
        self._max_cached = max(1, int(max_cached))
        self._cache: OrderedDict[str, tuple[SkillsLoader, SkillsStore]] = OrderedDict()
        self._lock = threading.Lock()

    # ── public API ─────────────────────────────────────────────────────

    def set_auto_promote_threshold(self, value: int) -> None:
        """Update the threshold for future-built loaders and live cached ones."""
        self._auto_promote_threshold = max(0, int(value))
        with self._lock:
            for loader, _store in self._cache.values():
                loader.set_auto_promote_threshold(self._auto_promote_threshold)

    def loader_for(self, symbiote_id: str) -> SkillsLoader:
        return self._get(symbiote_id)[0]

    def store_for(self, symbiote_id: str) -> SkillsStore:
        return self._get(symbiote_id)[1]

    def has_directory(self, symbiote_id: str) -> bool:
        """True when the symbiote has a skill directory on disk."""
        return bool(symbiote_id) and (self._base_root / symbiote_id).is_dir()

    def purge(self, symbiote_id: str) -> bool:
        """Delete everything the symbiote wrote: ``{base_root}/<symbiote_id>``.

        Returns True when a directory was removed. The shared catalogue
        (protected and extra roots) is never touched.
        """
        if not symbiote_id or Path(symbiote_id).name != symbiote_id:
            raise ValueError(f"not a symbiote id: {symbiote_id!r}")
        with self._lock:
            self._cache.pop(symbiote_id, None)
        base = self._base_root.resolve()
        target = (base / symbiote_id).resolve()
        if target.parent != base or not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    # ── internal ───────────────────────────────────────────────────────

    def _get(self, symbiote_id: str) -> tuple[SkillsLoader, SkillsStore]:
        if not symbiote_id:
            raise ValueError("symbiote_id is required for per-symbiote skill scope.")
        with self._lock:
            hit = self._cache.get(symbiote_id)
            if hit is not None:
                self._cache.move_to_end(symbiote_id)  # LRU touch
                return hit
            pair = self._build(symbiote_id)
            self._cache[symbiote_id] = pair
            # Evict least-recently-used beyond the cap. Evicted loaders/stores
            # are stateless wrappers over disk — dropping them loses nothing.
            while len(self._cache) > self._max_cached:
                self._cache.popitem(last=False)
            return pair

    def _build(self, symbiote_id: str) -> tuple[SkillsLoader, SkillsStore]:
        # Per-symbiote read-write agent root: {base}/<sid>/skills
        agent_root = self._base_root / symbiote_id / "skills"
        # The store writes ONLY to the symbiote's own root. Extra roots are
        # shared between symbiotes, so they are read-only here: writing to
        # them let one tenant change a skill another tenant reads.
        store = SkillsStore(
            roots=[agent_root],
            protected_roots=self._protected_roots,
            read_only=self._read_only,
        )
        # Loader scans {root}/skills/*/SKILL.md, so pass the PARENT of each
        # "skills" dir. Own root + extra roots + shared protected catalogue
        # are all discoverable (read).
        loader_roots: list[Path] = [agent_root.parent]
        loader_roots += [r.parent for r in self._extra_roots]
        loader_roots += [r.parent for r in self._protected_roots]
        loader = SkillsLoader(
            *loader_roots,
            auto_promote_threshold=self._auto_promote_threshold,
            read_only=self._read_only,
        )
        store.on_change = loader.refresh
        store.redact = self._redact
        return loader, store


__all__ = [
    "SkillScopeManager",
    "active_symbiote",
    "get_active_symbiote",
    "reset_active_symbiote",
    "set_active_symbiote",
]
