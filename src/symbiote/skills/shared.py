"""A skill library shared by replicas that do not share a disk.

By default the writable skill library is a directory: the kernel reads and
writes ``SKILL.md`` files there, and that is all. With several replicas each
one has its own disk, so a skill learned on one would never reach the others.

With a ``SkillsStoragePort`` injected (``SymbioteKernel(...,
skills_storage=...)``), the shared library becomes the source of truth and
the directory of each replica a local copy of it:

* every change made through the kernel (``skill_manage``, the background
  review, dream, promotion, deletion) is pushed to the shared library;
* at the start of every turn the replica checks the library's version and,
  when it changed, brings its local copy up to date. So a skill written on
  one replica is seen by the next turn of any other, and a turn whose
  context was already assembled is not changed under its feet.

Everything else (validation, protected roots, quarantine, per-symbiote
isolation) keeps working on the local copy, unchanged.

``StorageSkillsLibrary`` is the implementation on the kernel's database.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from symbiote.core.ports import SkillsStoragePort, StoragePort
from symbiote.skills.usage import SIDECAR_NAME

logger = logging.getLogger(__name__)

GLOBAL_SCOPE = "global"
_TMP_PREFIX = ".tmp-"


def digest(content: str) -> str:
    """The digest ``SkillsStoragePort.list_files`` reports for a file."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class StorageSkillsLibrary:
    """``SkillsStoragePort`` on the kernel's database (tables ``skill_files``
    and ``skill_scopes``).

        storage = PostgresAdapter(dsn, schema="symbiote")
        kernel = SymbioteKernel(config, storage=storage,
                                skills_storage=StorageSkillsLibrary(storage))
    """

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def version(self, scope: str) -> int:
        row = self._storage.fetch_one(
            "SELECT version FROM skill_scopes WHERE scope = ?", (scope,)
        )
        return int(row["version"]) if row else 0

    def list_files(self, scope: str) -> dict[str, str]:
        rows = self._storage.fetch_all(
            "SELECT path, digest FROM skill_files WHERE scope = ?", (scope,)
        )
        return {row["path"]: row["digest"] for row in rows}

    def read_files(self, scope: str, paths: Sequence[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for start in range(0, len(paths), 200):
            chunk = tuple(paths[start : start + 200])
            marks = ", ".join("?" for _ in chunk)
            rows = self._storage.fetch_all(
                f"SELECT path, content FROM skill_files WHERE scope = ? AND path IN ({marks})",  # noqa: S608
                (scope, *chunk),
            )
            found.update({row["path"]: row["content"] for row in rows})
        return found

    def write_files(self, scope: str, files: Mapping[str, str | None]) -> int:
        now = datetime.now(tz=UTC).isoformat()
        with self._storage.transaction():
            for path, content in files.items():
                if content is None:
                    self._storage.execute(
                        "DELETE FROM skill_files WHERE scope = ? AND path = ?", (scope, path)
                    )
                else:
                    self._storage.execute(
                        "INSERT INTO skill_files (scope, path, content, digest, updated_at) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(scope, path) DO UPDATE SET "
                        "content = excluded.content, digest = excluded.digest, "
                        "updated_at = excluded.updated_at",
                        (scope, path, content, digest(content), now),
                    )
            self._storage.execute(
                "INSERT INTO skill_scopes (scope, version, updated_at) VALUES (?, 1, ?) "
                "ON CONFLICT(scope) DO UPDATE SET "
                "version = skill_scopes.version + 1, updated_at = excluded.updated_at",
                (scope, now),
            )
            return self.version(scope)

    def delete_scope(self, scope: str) -> int:
        with self._storage.transaction():
            removed = self._storage.execute(
                "DELETE FROM skill_files WHERE scope = ?", (scope,)
            ).rowcount
            self._storage.execute("DELETE FROM skill_scopes WHERE scope = ?", (scope,))
        return removed


class _ScopeState:
    """What this replica knows about one scope."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.seen: int | None = None  # library version of the last sync
        self.known: dict[str, str] = {}  # path -> digest, as of the last sync
        self.stat: dict[str, tuple[int, int, str]] = {}  # path -> (mtime_ns, size, digest)
        self.lazy_since: float | None = None  # oldest unpushed usage-only change


class SkillsSync:
    """Keeps local skill directories in step with a ``SkillsStoragePort``.

    One instance per kernel. ``scope`` is ``"global"`` for the single shared
    library or a symbiote id in the per-symbiote scope; ``root`` is the
    directory that holds that scope's skill directories.
    """

    def __init__(
        self,
        port: SkillsStoragePort,
        *,
        redact: Callable[[str], str] | None = None,
        writable: bool = True,
        usage_flush_interval: float = 30.0,
    ) -> None:
        self._port = port
        self._redact = redact
        self._writable = writable
        self._usage_flush_interval = usage_flush_interval
        self._states: dict[str, _ScopeState] = {}
        self._roots: dict[Path, str] = {}  # resolved root -> scope
        self._guard = threading.Lock()

    # ── registration ───────────────────────────────────────────────────

    def attach(self, scope: str, root: Path) -> None:
        """Declare that ``root`` is the local copy of ``scope``."""
        with self._guard:
            self._roots[Path(root).resolve()] = scope
            self._states.setdefault(scope, _ScopeState())

    def forget(self, scope: str) -> None:
        """Drop what is known about ``scope`` (its symbiote was purged)."""
        with self._guard:
            self._states.pop(scope, None)
            for root in [r for r, s in self._roots.items() if s == scope]:
                del self._roots[root]

    def _state(self, scope: str) -> _ScopeState:
        with self._guard:
            return self._states.setdefault(scope, _ScopeState())

    # ── the two directions ─────────────────────────────────────────────

    def pull(self, scope: str, root: Path) -> bool:
        """Bring the local copy of ``scope`` up to date; True when files
        under ``root`` changed (the caller refreshes its loader)."""
        state = self._state(scope)
        with state.lock:
            if state.seen is None:  # first sync of this scope in this process
                version = self._port.version(scope)
                if version == 0:
                    # Nothing was ever shared for this scope: what this
                    # replica has on disk becomes the library.
                    state.seen = 0
                    self._push(scope, root, state, include_usage=True)
                    return False
                return self._apply_remote(scope, root, state, version, first=True)
            # Local changes go up first, then the others' changes come down.
            self._push(scope, root, state, include_usage=self._usage_due(state))
            version = self._port.version(scope)
            if version == state.seen:
                return False
            return self._apply_remote(scope, root, state, version, first=False)

    def push(self, scope: str, root: Path) -> None:
        """Send the local changes of ``scope`` to the shared library."""
        state = self._state(scope)
        with state.lock:
            if state.seen is None:
                self.pull(scope, root)  # learn what is shared before writing to it
                return
            self._push(scope, root, state, include_usage=True)

    def on_sidecar_written(self, skill_dir: Path, urgent: bool) -> None:
        """A usage sidecar changed. Status changes are pushed at once; plain
        usage counters wait for the next push of the scope."""
        found = self._scope_of(skill_dir)
        if found is None:
            return
        scope, root = found
        state = self._state(scope)
        if urgent:
            try:
                self.push(scope, root)
            except Exception:
                logger.warning(
                    "could not push the skills of scope %s; will retry on the next turn",
                    scope,
                    exc_info=True,
                )
            return
        with state.lock:
            if state.lazy_since is None:
                state.lazy_since = time.monotonic()

    def flush(self) -> None:
        """Push every pending change (called at shutdown)."""
        with self._guard:
            attached = list(self._roots.items())
        for root, scope in attached:
            state = self._state(scope)
            if state.seen is None:
                continue
            try:
                with state.lock:
                    self._push(scope, root, state, include_usage=True)
            except Exception:
                logger.warning("could not flush the skills of scope %s", scope, exc_info=True)

    # ── internals ──────────────────────────────────────────────────────

    def _scope_of(self, skill_dir: Path) -> tuple[str, Path] | None:
        try:
            resolved = Path(skill_dir).resolve()
        except OSError:
            return None
        with self._guard:
            for candidate in (resolved, *resolved.parents):
                scope = self._roots.get(candidate)
                if scope is not None:
                    return scope, candidate
        return None

    def _usage_due(self, state: _ScopeState) -> bool:
        return (
            state.lazy_since is not None
            and time.monotonic() - state.lazy_since >= self._usage_flush_interval
        )

    def _scan(self, root: Path, state: _ScopeState) -> dict[str, str]:
        """Text files under ``root`` as ``relative path -> digest``."""
        found: dict[str, str] = {}
        stat_cache: dict[str, tuple[int, int, str]] = {}
        if root.is_dir():
            for path in root.rglob("*"):
                if path.name.startswith(_TMP_PREFIX) or path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                try:
                    info = path.stat()
                    cached = state.stat.get(rel)
                    if cached is not None and cached[:2] == (info.st_mtime_ns, info.st_size):
                        file_digest = cached[2]
                    else:
                        text = _read_text(path)
                        if text is None:
                            continue  # binary: not part of the shared library
                        file_digest = digest(text)
                    stat_cache[rel] = (info.st_mtime_ns, info.st_size, file_digest)
                    found[rel] = file_digest
                except OSError:
                    continue
        state.stat = stat_cache
        return found

    def _push(self, scope: str, root: Path, state: _ScopeState, *, include_usage: bool) -> None:
        if not self._writable:
            return
        local = self._scan(root, state)
        changes: dict[str, str | None] = {}
        for rel, file_digest in local.items():
            if state.known.get(rel) == file_digest:
                continue
            path = root / rel
            text = _read_text(path)
            if text is None:
                continue
            if self._redact is not None and path.name != SIDECAR_NAME:
                redacted = self._redact(text)
                if redacted != text:
                    _write_text(path, redacted)
                    text = redacted
            changes[rel] = text
        for rel in state.known:
            if rel not in local:
                changes[rel] = None
        if not changes:
            state.lazy_since = None
            return
        if not include_usage and all(Path(rel).name == SIDECAR_NAME for rel in changes):
            return  # usage counters only: they ride along with the next push
        version = self._port.write_files(scope, changes)
        for rel, text in changes.items():
            if text is None:
                state.known.pop(rel, None)
            else:
                state.known[rel] = digest(text)
        state.lazy_since = None
        # Someone else wrote in between: leave ``seen`` behind so the next
        # pull fetches their changes.
        if state.seen is not None and version == state.seen + 1:
            state.seen = version

    def _apply_remote(
        self, scope: str, root: Path, state: _ScopeState, version: int, first: bool
    ) -> bool:
        remote = self._port.list_files(scope)
        local = self._scan(root, state)
        if first:
            # The shared library wins over whatever an older run left here.
            wanted = [rel for rel, d in remote.items() if local.get(rel) != d]
            stale = [rel for rel in local if rel not in remote]
            if stale:
                logger.warning(
                    "skills of scope %s: removing %d local file(s) that are not in "
                    "the shared library",
                    scope,
                    len(stale),
                )
        else:
            wanted = [rel for rel, d in remote.items() if state.known.get(rel) != d]
            # Removed from the library since the last sync; files this replica
            # created and has not pushed yet stay.
            stale = [rel for rel in local if rel not in remote and rel in state.known]
        contents = self._port.read_files(scope, wanted) if wanted else {}
        changed = False
        for rel, text in contents.items():
            if not _is_inside(root, rel):
                logger.warning("skills of scope %s: ignoring unsafe path %r", scope, rel)
                continue
            _write_text(root / rel, text)
            changed = True
        for rel in stale:
            with contextlib.suppress(OSError):
                (root / rel).unlink()
                changed = True
        if stale:
            _remove_empty_dirs(root)
        state.known = dict(remote)
        state.seen = version
        return changed


def _read_text(path: Path) -> str | None:
    """The file as text, or None when it is not UTF-8 text."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return None if "\x00" in text else text


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=_TMP_PREFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _is_inside(root: Path, rel: str) -> bool:
    """``rel`` is a plain relative path that stays under ``root``."""
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        return False
    try:
        (root / candidate).resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _remove_empty_dirs(root: Path) -> None:
    for directory in sorted(
        (p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        with contextlib.suppress(OSError):
            directory.rmdir()  # only succeeds when empty


__all__ = [
    "GLOBAL_SCOPE",
    "SkillsSync",
    "StorageSkillsLibrary",
    "digest",
]
