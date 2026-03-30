"""SessionLock — per-session concurrency control.

Provides both sync (threading.Lock) and async (asyncio.Lock) per-session
locking.  Different sessions can process in parallel; the same session
serializes requests to prevent race conditions in memory and context.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager


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
