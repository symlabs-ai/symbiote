"""The injectable session lock (SessionLockPort): acquisition with a maximum
wait, guaranteed release, validity period, renewal while the turn runs and
stopping the turn before its next step when the lock is lost.

The database-backed lock runs on whatever database the suite uses.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import SessionBusyError, SessionLockLostError
from symbiote.core.kernel import SymbioteKernel
from symbiote.core.session_lock import InProcessSessionLock, StorageSessionLock
from tests.support import ScriptedLLM, add_tool, make_kernel, tool_call_block


class RecordingLock:
    """A SessionLockPort that records what the kernel asks of it."""

    default_ttl: float | None = None

    def __init__(self) -> None:
        self._inner = InProcessSessionLock()
        self.acquired: list[dict[str, Any]] = []
        self.renewed: list[tuple[str, str, float | None]] = []
        self.released: list[tuple[str, str]] = []
        self.renew_result: Any = True  # bool, or an exception to raise
        self.fail_renew_after = 0  # successful renewals before renew_result applies

    def acquire(self, session_id, *, wait=None, ttl=None):
        token = self._inner.acquire(session_id, wait=wait, ttl=ttl)
        self.acquired.append({"session": session_id, "wait": wait, "ttl": ttl, "token": token})
        return token

    def renew(self, session_id, token, *, ttl=None):
        self.renewed.append((session_id, token, ttl))
        if len(self.renewed) <= self.fail_renew_after:
            return True
        if isinstance(self.renew_result, Exception):
            raise self.renew_result
        return self.renew_result

    def release(self, session_id, token):
        self.released.append((session_id, token))
        self._inner.release(session_id, token)


def _kernel(tmp_path: Path, llm: Any, lock: Any, **config: Any) -> SymbioteKernel:
    config.setdefault("db_path", tmp_path / "symbiote.db")
    config.setdefault("skills_root", tmp_path / "skills")
    return SymbioteKernel(KernelConfig(**config), llm=llm, session_lock=lock)


def _session(kernel: SymbioteKernel) -> tuple[str, str]:
    sym = kernel.create_symbiote(name="a", role="r")
    return sym.id, kernel.start_session(sym.id).id


def _messages(kernel: SymbioteKernel, session_id: str) -> list[str]:
    rows = kernel._storage.fetch_all(
        "SELECT role FROM messages WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
    )
    return [r["role"] for r in rows]


# ── injection, wait, release ────────────────────────────────────────────


def test_the_injected_lock_is_taken_and_released_around_each_turn(tmp_path: Path) -> None:
    lock = RecordingLock()
    kernel = _kernel(tmp_path, ScriptedLLM("ok"), lock, session_lock_wait=2.5, session_lock_ttl=30)
    _sym, session = _session(kernel)

    kernel.message(session, "hi")

    assert [(a["session"], a["wait"], a["ttl"]) for a in lock.acquired] == [(session, 2.5, 30)]
    assert lock.released == [(session, lock.acquired[0]["token"])]
    kernel.shutdown()


def test_the_lock_is_released_when_the_turn_fails(tmp_path: Path) -> None:
    def explode(_i: int, _m: list[dict]) -> str:
        raise ValueError("model is down")

    lock = RecordingLock()
    kernel = _kernel(tmp_path, ScriptedLLM(explode), lock)
    _sym, session = _session(kernel)

    with pytest.raises(Exception, match="model is down"):
        kernel.message(session, "hi")
    with pytest.raises(Exception, match="model is down"):
        asyncio.run(kernel.message_async(session, "hi"))

    assert len(lock.acquired) == 2
    assert lock.released == [(session, a["token"]) for a in lock.acquired]
    kernel.shutdown()


def test_async_turns_use_the_blocking_port_in_a_thread(tmp_path: Path) -> None:
    lock = RecordingLock()  # has no acquire_async / release_async
    threads: list[str] = []
    original = lock.acquire

    def acquire(session_id, *, wait=None, ttl=None):
        threads.append(threading.current_thread().name)
        return original(session_id, wait=wait, ttl=ttl)

    lock.acquire = acquire  # type: ignore[method-assign]
    kernel = _kernel(tmp_path, ScriptedLLM("ok"), lock)
    _sym, session = _session(kernel)

    async def run() -> str:
        loop_thread = threading.current_thread().name
        reply = await kernel.message_async(session, "hi")
        assert threads and threads[0] != loop_thread  # the loop was not blocked
        return reply

    assert asyncio.run(run()) == "ok"
    assert len(lock.released) == 1
    kernel.shutdown()


def test_a_busy_session_raises_after_the_maximum_wait(tmp_path: Path) -> None:
    started = threading.Event()
    finish = threading.Event()

    def slow(_i: int, _m: list[dict]) -> str:
        started.set()
        finish.wait(5)
        return "first"

    kernel = make_kernel(tmp_path, ScriptedLLM(slow), session_lock_wait=0.2)
    _sym, session = _session(kernel)
    first = threading.Thread(target=lambda: kernel.message(session, "one"))
    first.start()
    assert started.wait(5)

    began = time.monotonic()
    with pytest.raises(SessionBusyError) as raised:
        kernel.message(session, "two")
    waited = time.monotonic() - began
    assert 0.15 <= waited < 2.0
    assert raised.value.session_id == session
    # The refused turn stored nothing.
    assert _messages(kernel, session) == ["user"]

    finish.set()
    first.join(5)
    assert _messages(kernel, session) == ["user", "assistant"]
    kernel.shutdown()


def test_a_busy_session_raises_for_async_turns_too(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"), session_lock_wait=0.1)
    _sym, session = _session(kernel)

    async def run() -> None:
        lock = kernel._session_lock
        token = await lock.acquire_async(session)
        with pytest.raises(SessionBusyError):
            await kernel.message_async(session, "two")
        await lock.release_async(session, token)
        assert await kernel.message_async(session, "three") == "ok"

    asyncio.run(run())
    kernel.shutdown()


def test_default_waits_forever_and_never_renews(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    assert isinstance(kernel._session_lock, InProcessSessionLock)
    assert kernel._config.session_lock_wait is None
    assert kernel._lock_ttl() is None
    before = {t.name for t in threading.enumerate()}
    _sym, session = _session(kernel)
    kernel.message(session, "hi")
    assert "symbiote-session-lease" not in {t.name for t in threading.enumerate()} - before
    kernel.shutdown()


def test_renew_interval_must_be_shorter_than_the_ttl() -> None:
    with pytest.raises(ValidationError, match="shorter than session_lock_ttl"):
        KernelConfig(session_lock_ttl=10, session_lock_renew_interval=10)
    assert KernelConfig(session_lock_ttl=10, session_lock_renew_interval=3)


# ── renewal while the turn runs ─────────────────────────────────────────


def test_the_lock_is_renewed_while_the_turn_runs(tmp_path: Path) -> None:
    def slow(_i: int, _m: list[dict]) -> str:
        time.sleep(0.35)
        return "done"

    lock = RecordingLock()
    kernel = _kernel(
        tmp_path, ScriptedLLM(slow), lock, session_lock_ttl=5, session_lock_renew_interval=0.05
    )
    _sym, session = _session(kernel)

    assert kernel.message(session, "hi") == "done"

    token = lock.acquired[0]["token"]
    assert len(lock.renewed) >= 3
    assert set(lock.renewed) == {(session, token, 5)}
    count = len(lock.renewed)
    time.sleep(0.2)
    assert len(lock.renewed) <= count + 1  # renewal stops with the turn
    kernel.shutdown()


def test_renewal_defaults_to_a_third_of_the_ttl(tmp_path: Path) -> None:
    def slow(_i: int, _m: list[dict]) -> str:
        time.sleep(0.5)
        return "done"

    lock = RecordingLock()
    kernel = _kernel(tmp_path, ScriptedLLM(slow), lock, session_lock_ttl=0.45)
    _sym, session = _session(kernel)
    kernel.message(session, "hi")
    assert 2 <= len(lock.renewed) <= 4  # every 0.15s for 0.5s
    kernel.shutdown()


def _tool_then_reply(calls: list[str], model_delay: float = 0.0):
    def script(i: int, _m: list[dict]) -> str:
        calls.append(f"model-{i}")
        time.sleep(model_delay)
        return tool_call_block("work") if i == 0 else "final answer"

    return script


@pytest.mark.parametrize("failure", [False, RuntimeError("lock server unreachable")])
def test_a_lost_lock_stops_the_turn_before_the_next_model_call(tmp_path: Path, failure) -> None:
    calls: list[str] = []
    lock = RecordingLock()
    lock.renew_result = failure
    kernel = _kernel(
        tmp_path, ScriptedLLM(_tool_then_reply(calls)), lock,
        session_lock_ttl=0.2, session_lock_renew_interval=0.05,
    )
    sym, session = _session(kernel)

    def work(_params: dict) -> str:
        calls.append("tool")
        time.sleep(0.6)  # the lock is lost while this runs
        calls.append("tool-finished")
        return "worked"

    add_tool(kernel, sym, "work", work)

    with pytest.raises(SessionLockLostError) as raised:
        kernel.message(session, "do it")

    assert raised.value.session_id == session
    # The tool that was running finished; the second model call never started.
    assert calls == ["model-0", "tool", "tool-finished"]
    assert "assistant" not in _messages(kernel, session)
    assert lock.released == [(session, lock.acquired[0]["token"])]
    kernel.shutdown()


def test_a_lost_lock_stops_the_turn_before_the_next_tool_call(tmp_path: Path) -> None:
    calls: list[str] = []
    lock = RecordingLock()
    lock.renew_result = False
    kernel = _kernel(
        tmp_path, ScriptedLLM(_tool_then_reply(calls, model_delay=0.3)), lock,
        session_lock_ttl=5, session_lock_renew_interval=0.05,
    )
    sym, session = _session(kernel)
    add_tool(kernel, sym, "work", lambda _p: calls.append("tool") or "worked")

    with pytest.raises(SessionLockLostError):
        kernel.message(session, "do it")

    assert calls == ["model-0"]  # the tool the model asked for never ran
    kernel.shutdown()


def test_a_lost_lock_stops_an_async_turn(tmp_path: Path) -> None:
    calls: list[str] = []
    lock = RecordingLock()
    lock.renew_result = False
    kernel = _kernel(
        tmp_path, ScriptedLLM(_tool_then_reply(calls)), lock,
        session_lock_ttl=5, session_lock_renew_interval=0.05,
    )
    sym, session = _session(kernel)

    async def work(_params: dict) -> str:
        calls.append("tool")
        await asyncio.sleep(0.3)
        return "worked"

    add_tool(kernel, sym, "work", work)

    with pytest.raises(SessionLockLostError):
        asyncio.run(kernel.message_async(session, "do it"))

    assert calls == ["model-0", "tool"]
    assert "assistant" not in _messages(kernel, session)
    assert len(lock.released) == 1
    kernel.shutdown()


def test_renewal_errors_are_tolerated_while_the_lock_is_still_valid(tmp_path: Path) -> None:
    calls: list[str] = []
    lock = RecordingLock()
    lock.renew_result = RuntimeError("blip")
    kernel = _kernel(
        tmp_path, ScriptedLLM(_tool_then_reply(calls, model_delay=0.15)), lock,
        session_lock_ttl=30, session_lock_renew_interval=0.05,
    )
    sym, session = _session(kernel)
    add_tool(kernel, sym, "work", lambda _p: "worked")

    reply = kernel.message(session, "do it")

    assert (reply["text"] if isinstance(reply, dict) else reply) == "final answer"
    assert len(lock.renewed) >= 2  # it kept trying
    kernel.shutdown()


# ── the database-backed lock ────────────────────────────────────────────


@pytest.fixture()
def storage(tmp_path: Path):
    adapter = SQLiteAdapter(tmp_path / "locks.db", check_same_thread=False)
    adapter.init_schema()
    yield adapter
    adapter.close()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_storage_lock_excludes_another_replica(storage) -> None:
    one, other = StorageSessionLock(storage), StorageSessionLock(storage)
    token = one.acquire("s1", wait=0)
    assert token is not None
    assert other.acquire("s1", wait=0) is None
    assert other.acquire("s2", wait=0) is not None  # another session is free

    began = time.monotonic()
    assert other.acquire("s1", wait=0.25) is None
    assert 0.2 <= time.monotonic() - began < 1.5

    one.release("s1", token)
    assert other.acquire("s1", wait=0) is not None


def test_storage_lock_waiter_gets_the_lock_when_it_is_released(storage) -> None:
    one = StorageSessionLock(storage, poll_interval=0.02)
    other = StorageSessionLock(storage, poll_interval=0.02)
    token = one.acquire("s1", wait=0)
    got: list[str | None] = []
    waiter = threading.Thread(target=lambda: got.append(other.acquire("s1", wait=5)))
    waiter.start()
    time.sleep(0.15)
    assert got == []
    one.release("s1", token)
    waiter.join(5)
    assert got and got[0] is not None and got[0] != token


def test_storage_lock_expires_and_the_old_owner_loses_it(storage) -> None:
    clock = Clock()
    one = StorageSessionLock(storage, clock=clock)
    other = StorageSessionLock(storage, clock=clock)
    token = one.acquire("s1", wait=0, ttl=30)

    clock.now += 29
    assert other.acquire("s1", wait=0) is None  # still valid
    assert one.renew("s1", token, ttl=30) is True  # valid until now + 30
    clock.now += 29
    assert other.acquire("s1", wait=0) is None
    clock.now += 2  # the replica holding it died: nobody renewed

    taken = other.acquire("s1", wait=0, ttl=30)
    assert taken is not None
    assert one.renew("s1", token, ttl=30) is False  # the old owner finds out
    one.release("s1", token)  # and its release does not free the new owner's lock
    assert one.acquire("s1", wait=0) is None
    other.release("s1", taken)
    assert storage.fetch_all("SELECT * FROM session_locks") == []


def test_storage_lock_uses_its_default_ttl(storage) -> None:
    clock = Clock()
    lock = StorageSessionLock(storage, default_ttl=10, clock=clock)
    lock.acquire("s1", wait=0)
    row = storage.fetch_one("SELECT expires_at FROM session_locks WHERE session_id = ?", ("s1",))
    assert row["expires_at"] == clock.now + 10
    with pytest.raises(ValueError, match="must be positive"):
        StorageSessionLock(storage, default_ttl=0)


def test_storage_lock_async(storage) -> None:
    lock = StorageSessionLock(storage, poll_interval=0.02)

    async def run() -> None:
        token = await lock.acquire_async("s1", wait=0)
        assert token is not None
        assert await lock.acquire_async("s1", wait=0.1) is None
        await lock.release_async("s1", token)
        assert await lock.acquire_async("s1", wait=0) is not None

    asyncio.run(run())


def test_only_one_of_many_concurrent_acquirers_wins(storage) -> None:
    locks = [StorageSessionLock(storage) for _ in range(12)]
    start = threading.Barrier(len(locks))
    won: list[str] = []

    def attempt(lock: StorageSessionLock) -> None:
        start.wait(5)
        token = lock.acquire("s1", wait=0)
        if token is not None:
            won.append(token)

    threads = [threading.Thread(target=attempt, args=(lock,)) for lock in locks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(won) == 1


# ── two replicas, one database ──────────────────────────────────────────


class Overlap:
    """Counts how many model calls run at the same time."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.running = 0
        self.peak = 0

    def __call__(self, _i: int, _m: list[dict]) -> str:
        with self._guard:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(0.1)
        with self._guard:
            self.running -= 1
        return "ok"


def _two_replicas(tmp_path: Path, overlap: Overlap, *, shared_lock: bool):
    replicas = []
    for name in ("one", "two"):
        storage = SQLiteAdapter(tmp_path / "shared.db", check_same_thread=False)
        lock = StorageSessionLock(storage, poll_interval=0.01) if shared_lock else None
        kernel = SymbioteKernel(
            KernelConfig(db_path=tmp_path / f"unused-{name}.db", skills_root=tmp_path / name),
            llm=ScriptedLLM(overlap),
            storage=storage,
            session_lock=lock,
        )
        replicas.append((kernel, storage))
    return replicas


def _run_turns(replicas, session: str) -> None:
    threads = [
        threading.Thread(target=kernel.message, args=(session, f"turn {i}"))
        for i in range(3)
        for kernel, _storage in replicas
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)


def test_two_replicas_take_turns_on_a_session_with_the_storage_lock(tmp_path: Path) -> None:
    overlap = Overlap()
    replicas = _two_replicas(tmp_path, overlap, shared_lock=True)
    _sym, session = _session(replicas[0][0])

    _run_turns(replicas, session)

    assert overlap.peak == 1
    assert _messages(replicas[0][0], session) == ["user", "assistant"] * 6
    assert replicas[0][1].fetch_all("SELECT * FROM session_locks") == []
    for kernel, storage in replicas:
        kernel.shutdown()
        storage.close()


def test_without_a_shared_lock_two_replicas_overlap(tmp_path: Path) -> None:
    """Control for the test above: the default lock is per process."""
    overlap = Overlap()
    replicas = _two_replicas(tmp_path, overlap, shared_lock=False)
    _sym, session = _session(replicas[0][0])

    _run_turns(replicas, session)

    assert overlap.peak == 2
    for kernel, storage in replicas:
        kernel.shutdown()
        storage.close()


def test_storage_lock_is_renewed_by_default(tmp_path: Path) -> None:
    """With the storage lock and no ttl configured, the kernel still renews:
    the lock's own default ttl applies."""
    storage = SQLiteAdapter(tmp_path / "db.db", check_same_thread=False)
    lock = StorageSessionLock(storage, default_ttl=0.3)
    expiries: list[float] = []

    def slow(_i: int, _m: list[dict]) -> str:
        for _ in range(4):
            time.sleep(0.12)
            row = storage.fetch_one("SELECT expires_at FROM session_locks")
            expiries.append(row["expires_at"])
        return "done"

    kernel = SymbioteKernel(
        KernelConfig(db_path=tmp_path / "unused.db", skills_root=tmp_path / "skills"),
        llm=ScriptedLLM(slow), storage=storage, session_lock=lock,
    )
    _sym, session = _session(kernel)

    assert kernel.message(session, "hi") == "done"  # 0.48s, longer than the ttl

    assert expiries[-1] > expiries[0]  # it was extended while the turn ran
    assert storage.fetch_all("SELECT * FROM session_locks") == []
    kernel.shutdown()
    storage.close()


def test_purge_removes_the_locks_of_the_symbiote(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sym, session = _session(kernel)
    lock = StorageSessionLock(kernel._storage)
    lock.acquire(session, wait=0)
    assert kernel.purge_symbiote(sym)["session_locks"] == 1
    assert kernel._storage.fetch_all("SELECT * FROM session_locks") == []
    kernel.shutdown()
