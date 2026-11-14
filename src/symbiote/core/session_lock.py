"""Per-session concurrency control: one turn at a time on a session.

Different sessions run in parallel; the turns of one session are serialized
so they do not race on its history, memory and context.

* ``SessionLockPort`` (``symbiote.core.ports``) is the contract the kernel
  uses; hosts may inject their own implementation.
* ``InProcessSessionLock`` is the default: locks inside this process.
* ``StorageSessionLock`` keeps the locks in the kernel's database, for
  several replicas sharing it.
* ``SessionLease`` renews a held lock in the background while a turn runs.
* ``SessionLock`` is the original in-process primitive (context managers),
  still used by ``InProcessSessionLock``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager

from symbiote.core.ports import SessionLockPort, StoragePort

logger = logging.getLogger(__name__)


class SessionLock:
    """Per-session lock manager.

    Usage::

        lock = SessionLock()

        # Sync
        with lock.acquire(session_id):
            ...

        # Async
        async with lock.acquire_async(session_id):
            ...

    Locks are tracked via weak references and garbage-collected when no
    longer in use, preventing unbounded memory growth.
    """

    def __init__(self) -> None:
        self._sync_locks: weakref.WeakValueDictionary[str, threading.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._async_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._meta_lock = threading.Lock()

    @contextmanager
    def acquire(self, session_id: str) -> Iterator[None]:
        """Acquire a sync lock for the given session."""
        lock = self._get_sync_lock(session_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    @asynccontextmanager
    async def acquire_async(self, session_id: str) -> AsyncIterator[None]:
        """Acquire an async lock for the given session."""
        lock = self._get_async_lock(session_id)
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def _get_sync_lock(self, session_id: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._sync_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._sync_locks[session_id] = lock
            return lock

    def _get_async_lock(self, session_id: str) -> asyncio.Lock:
        with self._meta_lock:
            lock = self._async_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._async_locks[session_id] = lock
            return lock


class InProcessSessionLock:
    """Default ``SessionLockPort``: serializes the turns of one process.

    Same behaviour as the kernel always had: a ``threading.Lock`` per session
    for ``message()`` and an ``asyncio.Lock`` per session for
    ``message_async()``. The locks never expire (``renew`` always succeeds),
    and other processes are not excluded: with several replicas use
    ``StorageSessionLock`` or the host's own lock.
    """

    default_ttl: float | None = None

    def __init__(self) -> None:
        self._locks = SessionLock()
        self._held: dict[str, threading.Lock | asyncio.Lock] = {}
        self._held_guard = threading.Lock()

    def _hold(self, lock: threading.Lock | asyncio.Lock) -> str:
        token = uuid.uuid4().hex
        with self._held_guard:
            self._held[token] = lock  # strong reference while held
        return token

    def acquire(
        self, session_id: str, *, wait: float | None = None, ttl: float | None = None
    ) -> str | None:
        lock = self._locks._get_sync_lock(session_id)
        if not lock.acquire(timeout=-1 if wait is None else wait):
            return None
        return self._hold(lock)

    async def acquire_async(
        self, session_id: str, *, wait: float | None = None, ttl: float | None = None
    ) -> str | None:
        lock = self._locks._get_async_lock(session_id)
        if wait is None:
            await lock.acquire()
        else:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=wait)
            except TimeoutError:
                return None
        return self._hold(lock)

    def renew(self, session_id: str, token: str, *, ttl: float | None = None) -> bool:
        with self._held_guard:
            return token in self._held

    def release(self, session_id: str, token: str) -> None:
        with self._held_guard:
            lock = self._held.pop(token, None)
        if lock is not None:
            lock.release()

    async def release_async(self, session_id: str, token: str) -> None:
        self.release(session_id, token)


class StorageSessionLock:
    """``SessionLockPort`` kept in the kernel's database: one row of
    ``session_locks`` per busy session, so every replica sharing the
    database sees it.

        storage = PostgresAdapter(dsn, schema="symbiote")
        kernel = SymbioteKernel(config, storage=storage,
                                session_lock=StorageSessionLock(storage))

    A waiting turn polls every ``poll_interval`` seconds. A lock always
    expires (``default_ttl`` when the kernel gives no ttl): if the replica
    holding it dies, the session is free again after that long. Expiry is
    judged by each replica's own clock, so keep the ttl well above the clock
    skew between replicas.
    """

    def __init__(
        self,
        storage: StoragePort,
        *,
        default_ttl: float = 60.0,
        poll_interval: float = 0.1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if default_ttl <= 0 or poll_interval <= 0:
            raise ValueError("default_ttl and poll_interval must be positive")
        self._storage = storage
        self.default_ttl: float | None = default_ttl
        self._poll_interval = poll_interval
        self._clock = clock

    def _ttl(self, ttl: float | None) -> float:
        return ttl if ttl is not None else float(self.default_ttl or 60.0)

    def _try(self, session_id: str, token: str, ttl: float | None) -> bool:
        """Insert our row, or take over a row that expired; one statement."""
        now = self._clock()
        result = self._storage.execute(
            "INSERT INTO session_locks (session_id, owner, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "owner = excluded.owner, expires_at = excluded.expires_at "
            "WHERE session_locks.expires_at <= ?",
            (session_id, token, now + self._ttl(ttl), now),
        )
        return result.rowcount == 1

    def acquire(
        self, session_id: str, *, wait: float | None = None, ttl: float | None = None
    ) -> str | None:
        token = uuid.uuid4().hex
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            if self._try(session_id, token, ttl):
                return token
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                time.sleep(min(self._poll_interval, remaining))
            else:
                time.sleep(self._poll_interval)

    async def acquire_async(
        self, session_id: str, *, wait: float | None = None, ttl: float | None = None
    ) -> str | None:
        token = uuid.uuid4().hex
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            if await asyncio.to_thread(self._try, session_id, token, ttl):
                return token
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                await asyncio.sleep(min(self._poll_interval, remaining))
            else:
                await asyncio.sleep(self._poll_interval)

    def renew(self, session_id: str, token: str, *, ttl: float | None = None) -> bool:
        result = self._storage.execute(
            "UPDATE session_locks SET expires_at = ? WHERE session_id = ? AND owner = ?",
            (self._clock() + self._ttl(ttl), session_id, token),
        )
        return result.rowcount == 1

    def release(self, session_id: str, token: str) -> None:
        self._storage.execute(
            "DELETE FROM session_locks WHERE session_id = ? AND owner = ?",
            (session_id, token),
        )

    async def release_async(self, session_id: str, token: str) -> None:
        await asyncio.to_thread(self.release, session_id, token)


class SessionLease:
    """A held session lock, renewed in the background while a turn runs.

    ``on_lost`` is called (once, from the renewal thread) when the lock is
    no longer ours: ``renew`` said so, or it kept failing until the ttl of
    the last successful renewal ran out.
    """

    def __init__(
        self,
        port: SessionLockPort,
        session_id: str,
        token: str,
        *,
        ttl: float | None,
        interval: float | None,
        on_lost: Callable[[], None],
    ) -> None:
        self._port = port
        self._session_id = session_id
        self._token = token
        self._ttl = ttl
        self._interval = interval
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._interval is None:
            return  # a lock that does not expire needs no renewal
        self._thread = threading.Thread(
            target=self._run, name="symbiote-session-lease", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        assert self._interval is not None
        interval = self._interval
        valid_until = None if self._ttl is None else time.monotonic() + self._ttl
        delay = interval
        while not self._stop.wait(delay):
            try:
                renewed = self._port.renew(self._session_id, self._token, ttl=self._ttl)
            except Exception:
                if self._stop.is_set():
                    return
                expired = valid_until is None or time.monotonic() >= valid_until
                logger.warning(
                    "could not renew the lock of session %s%s",
                    self._session_id,
                    "; giving the turn up" if expired else "; retrying",
                    exc_info=True,
                )
                if expired:
                    break
                delay = min(interval, max(0.05, interval / 4))
                continue
            if self._stop.is_set():
                return
            if not renewed:
                logger.warning(
                    "the lock of session %s is no longer held; stopping the turn",
                    self._session_id,
                )
                break
            if self._ttl is not None:
                valid_until = time.monotonic() + self._ttl
            delay = interval
        else:
            return
        self._on_lost()


def renew_interval(ttl: float | None, configured: float | None) -> float | None:
    """How often a held lock is renewed: the configured interval, or a third
    of the ttl; None when the lock does not expire."""
    if configured is not None:
        return configured
    return None if ttl is None else ttl / 3


__all__ = [
    "InProcessSessionLock",
    "SessionLease",
    "SessionLock",
    "StorageSessionLock",
    "renew_interval",
]
