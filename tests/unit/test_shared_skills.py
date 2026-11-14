"""The shared skill library (SkillsStoragePort): replicas with separate
disks see each other's skills from their next turn on.

Each "replica" here is a kernel with its own skills directory; they share
one database (SQLite or PostgreSQL, whichever the suite runs on).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.kernel import SymbioteKernel
from symbiote.skills import usage
from symbiote.skills.shared import StorageSkillsLibrary, digest
from tests.support import ScriptedLLM

SKILL = "---\nname: {name}\ndescription: how to {name}\n---\n\n# {name}\n\n{body}\n"


class Replica:
    def __init__(
        self,
        tmp_path: Path,
        name: str,
        *,
        scope: str = "per_symbiote",
        llm: Any = None,
        library: Any = "storage",
        **config: Any,
    ) -> None:
        self.storage = SQLiteAdapter(tmp_path / "shared.db", check_same_thread=False)
        self.root = tmp_path / name / "skills"
        if library == "storage":
            library = StorageSkillsLibrary(self.storage)
        self.kernel = SymbioteKernel(
            KernelConfig(
                db_path=tmp_path / f"unused-{name}.db",
                skills_root=self.root,
                skill_scope=scope,
                **config,
            ),
            llm=llm or ScriptedLLM("ok"),
            storage=self.storage,
            skills_storage=library,
        )

    def store(self, symbiote_id: str):
        return self.kernel.skills_store_for(symbiote_id)

    def loader(self, symbiote_id: str):
        return self.kernel.skills_loader_for(symbiote_id)

    def names(self, symbiote_id: str) -> list[str]:
        return sorted(s.name for s in self.loader(symbiote_id).list_skills())

    def turn(self, session_id: str, text: str = "hi") -> Any:
        return self.kernel.message(session_id, text)

    def close(self) -> None:
        self.kernel.shutdown()
        self.storage.close()


@pytest.fixture()
def replicas(tmp_path: Path):
    made: list[Replica] = []

    def make(name: str, **kwargs: Any) -> Replica:
        replica = Replica(tmp_path, name, **kwargs)
        made.append(replica)
        return replica

    yield make
    for replica in made:
        replica.close()


def _user(replica: Replica, name: str = "user") -> tuple[str, str]:
    sym = replica.kernel.create_symbiote(name=name, role="assistant")
    return sym.id, replica.kernel.start_session(sym.id).id


def _rows(replica: Replica, scope: str) -> dict[str, str]:
    rows = replica.storage.fetch_all(
        "SELECT path, content FROM skill_files WHERE scope = ?", (scope,)
    )
    return {r["path"]: r["content"] for r in rows}


# ── the acceptance criterion ────────────────────────────────────────────


def test_a_skill_written_on_one_replica_is_seen_by_the_next_turn_of_the_other(replicas) -> None:
    one, two = replicas("one"), replicas("two")
    sym, session = _user(one)
    two.turn(session)  # replica two already served this user
    assert two.names(sym) == []

    one.store(sym).create("deploy", SKILL.format(name="deploy", body="run the pipeline"))
    usage.set_status(one.store(sym).find_skill_dir("deploy"), usage.STATUS_QUARANTINE)

    assert two.names(sym) == []  # not before its next turn
    two.turn(session)
    assert two.names(sym) == ["deploy"]
    skill_dir = two.store(sym).find_skill_dir("deploy")
    assert "run the pipeline" in (skill_dir / "SKILL.md").read_text()
    # The lifecycle state travels with it: quarantined there, quarantined here.
    assert usage.get_effective_status(skill_dir) == usage.STATUS_QUARANTINE


def test_a_turn_already_assembled_does_not_change(replicas, tmp_path: Path) -> None:
    in_model = threading.Event()
    go_on = threading.Event()
    seen_during_turn: list[list[str]] = []

    def model(_i: int, _m: list[dict]) -> str:
        in_model.set()
        go_on.wait(10)
        seen_during_turn.append(two.names(sym))
        return "ok"

    one = replicas("one")
    two = replicas("two", llm=ScriptedLLM(model))
    sym, session = _user(one)

    turn = threading.Thread(target=two.turn, args=(session,))
    turn.start()
    assert in_model.wait(10)  # replica two assembled its context and is waiting on the model
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    go_on.set()
    turn.join(10)

    assert seen_during_turn == [[]]  # the running turn kept the library it started with
    in_model.clear()
    two.turn(session)
    assert seen_during_turn[-1] == ["deploy"]  # the next one has it


def test_edits_deletes_and_supporting_files_propagate(replicas) -> None:
    one, two = replicas("one"), replicas("two")
    sym, session = _user(one)
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="v1"))
    one.store(sym).write_file("deploy", "references/notes.md", "first notes")
    two.turn(session)
    two_dir = two.store(sym).find_skill_dir("deploy")
    assert (two_dir / "references" / "notes.md").read_text() == "first notes"

    # Replica two edits; replica one gets the edit.
    two.store(sym).edit("deploy", SKILL.format(name="deploy", body="v2 from two"))
    two.store(sym).remove_file("deploy", "references/notes.md")
    one.turn(session)
    one_dir = one.store(sym).find_skill_dir("deploy")
    assert "v2 from two" in (one_dir / "SKILL.md").read_text()
    assert not (one_dir / "references" / "notes.md").exists()

    # Replica one deletes; it disappears from replica two.
    one.store(sym).delete("deploy")
    two.turn(session)
    assert two.names(sym) == []
    assert not two_dir.exists()
    assert _rows(one, sym) == {}


def test_both_replicas_can_write_different_skills(replicas) -> None:
    one, two = replicas("one"), replicas("two")
    sym, session = _user(one)
    one.turn(session)
    two.turn(session)

    one.store(sym).create("alpha", SKILL.format(name="alpha", body="a"))
    two.store(sym).create("beta", SKILL.format(name="beta", body="b"))
    one.turn(session)
    two.turn(session)

    assert one.names(sym) == two.names(sym) == ["alpha", "beta"]


def test_async_turns_sync_too(replicas) -> None:
    one, two = replicas("one"), replicas("two")
    sym, session = _user(one)
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    asyncio.run(two.kernel.message_async(session, "hi"))
    assert two.names(sym) == ["deploy"]


# ── lifecycle state and usage counters ──────────────────────────────────


def test_a_status_change_is_shared_at_once(replicas) -> None:
    one, two = replicas("one"), replicas("two")
    sym, session = _user(one)
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    two.turn(session)

    usage.set_status(one.store(sym).find_skill_dir("deploy"), usage.STATUS_ACTIVE)

    two.turn(session)
    assert usage.get_effective_status(two.store(sym).find_skill_dir("deploy")) == usage.STATUS_ACTIVE


def test_usage_counters_wait_for_the_next_push(replicas) -> None:
    one, two = replicas("one"), replicas("two")
    sym, session = _user(one)
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    library = StorageSkillsLibrary(one.storage)
    version = library.version(sym)

    for _ in range(3):
        assert one.loader(sym).get_skill("deploy") is not None  # bumps use_count

    assert library.version(sym) == version  # three loads, no write to the library
    one.kernel.shutdown()  # pending counters are flushed
    assert library.version(sym) == version + 1
    two.turn(session)
    meta = usage.read_meta(two.store(sym).find_skill_dir("deploy"))
    assert meta["use_count"] == 3


# ── isolation, scopes, roots ────────────────────────────────────────────


def test_per_symbiote_isolation_is_kept(replicas) -> None:
    one, two = replicas("one"), replicas("two")
    alice, alice_session = _user(one, "alice")
    bob, bob_session = _user(one, "bob")
    one.store(alice).create("alice-skill", SKILL.format(name="alice-skill", body="a"))
    one.store(bob).create("bob-skill", SKILL.format(name="bob-skill", body="b"))

    two.turn(alice_session)
    two.turn(bob_session)

    assert two.names(alice) == ["alice-skill"]
    assert two.names(bob) == ["bob-skill"]
    scopes = {r["scope"] for r in one.storage.fetch_all("SELECT DISTINCT scope FROM skill_files")}
    assert scopes == {alice, bob}
    assert set(_rows(one, alice)) == {"alice-skill/SKILL.md", "alice-skill/.skill_meta.json"}


def test_global_scope(replicas) -> None:
    one, two = replicas("one", scope="global"), replicas("two", scope="global")
    sym, session = _user(one)
    one.kernel.skills_store.create("deploy", SKILL.format(name="deploy", body="b"))
    two.turn(session)
    assert [s.name for s in two.kernel.skills_loader.list_skills()] == ["deploy"]
    assert set(_rows(one, "global")) == {"deploy/SKILL.md", "deploy/.skill_meta.json"}


def test_protected_roots_are_not_uploaded(replicas, tmp_path: Path) -> None:
    catalogue = tmp_path / "catalogue" / "skills"
    (catalogue / "house-style").mkdir(parents=True)
    (catalogue / "house-style" / "SKILL.md").write_text(
        SKILL.format(name="house-style", body="shipped with the image")
    )
    one = replicas("one", skills_protected_roots=[catalogue])
    sym, session = _user(one)
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    one.turn(session)

    assert one.names(sym) == ["deploy", "house-style"]
    assert set(_rows(one, sym)) == {"deploy/SKILL.md", "deploy/.skill_meta.json"}


# ── first sync of a replica ─────────────────────────────────────────────


def _write_local_skill(root: Path, symbiote_id: str, name: str) -> None:
    skill_dir = root / symbiote_id / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL.format(name=name, body="written before"))


def test_local_skills_seed_an_empty_library(replicas) -> None:
    one = replicas("one")
    sym, session = _user(one)
    _write_local_skill(one.root, sym, "legacy")  # existed before the library was shared
    one.turn(session)
    assert set(_rows(one, sym)) == {"legacy/SKILL.md"}

    two = replicas("two")
    two.turn(session)
    assert two.names(sym) == ["legacy"]


def test_the_library_wins_over_local_leftovers(replicas, caplog) -> None:
    one, two = replicas("one"), replicas("two")
    sym, session = _user(one)
    one.store(sym).create("current", SKILL.format(name="current", body="b"))
    _write_local_skill(two.root, sym, "leftover")  # e.g. deleted while two was down

    with caplog.at_level(logging.WARNING, logger="symbiote.skills.shared"):
        two.turn(session)

    assert two.names(sym) == ["current"]
    assert not (two.root / sym / "skills" / "leftover").exists()
    assert "not in the shared library" in caplog.text
    assert "leftover/SKILL.md" not in _rows(one, sym)  # and it was not resurrected


def test_a_read_only_replica_reads_but_never_writes(replicas) -> None:
    one = replicas("one")
    reader = replicas("reader", skill_writes=False)
    sym, session = _user(one)
    _write_local_skill(reader.root, sym, "stray")
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    before = _rows(one, sym)

    reader.turn(session)

    assert reader.names(sym) == ["deploy"]
    assert _rows(one, sym) == before


# ── erasure and redaction ───────────────────────────────────────────────


def test_purge_erases_the_library_and_other_replicas_sweep_their_copy(replicas) -> None:
    one, two = replicas("one"), replicas("two")
    alice, alice_session = _user(one, "alice")
    bob, bob_session = _user(one, "bob")
    one.store(alice).create("alice-skill", SKILL.format(name="alice-skill", body="a"))
    one.store(bob).create("bob-skill", SKILL.format(name="bob-skill", body="b"))
    two.turn(alice_session)
    two.turn(bob_session)
    assert (two.root / alice).is_dir()

    counts = one.kernel.purge_symbiote(alice)

    assert counts["skill_files"] == 2 and counts["skill_scopes"] == 1
    dump = "\n".join(
        json.dumps(row, default=str)
        for table in one.storage.table_names()
        for row in one.storage.fetch_all(f"SELECT * FROM {table}")  # noqa: S608
    )
    assert alice not in dump and "alice-skill" not in dump
    assert not (one.root / alice).exists()
    # Replica two still has its local copy until it sweeps.
    assert (two.root / alice).is_dir()
    assert two.kernel.sweep_skill_cache() == 1
    assert not (two.root / alice).exists()
    # Bob is untouched everywhere.
    assert two.names(bob) == ["bob-skill"]
    assert set(_rows(one, bob)) == {"bob-skill/SKILL.md", "bob-skill/.skill_meta.json"}


def test_a_new_replica_sweeps_stale_copies_when_it_starts(replicas, tmp_path: Path) -> None:
    one = replicas("one")
    sym, _session = _user(one)
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    ghost = tmp_path / "two" / "skills" / "00000000-dead-beef-0000-000000000000" / "skills" / "old"
    ghost.mkdir(parents=True)
    (ghost / "SKILL.md").write_text(SKILL.format(name="old", body="of a purged user"))
    unrelated = tmp_path / "two" / "skills" / "notes"
    unrelated.mkdir(parents=True)
    (unrelated / "readme.txt").write_text("not a symbiote directory")

    replicas("two")  # sweeps at start

    assert not ghost.exists()
    assert (unrelated / "readme.txt").exists()


def test_secrets_never_reach_the_library(replicas) -> None:
    key = "sk-live-9f8e7d6c5b4a"
    one, two = replicas("one"), replicas("two")
    for replica in (one, two):
        replica.kernel.set_redactor(lambda text: text.replace(key, "[REDACTED]"))
    sym, session = _user(one)
    one.store(sym).create("register", SKILL.format(name="register", body=f"use {key}"))
    one.store(sym).write_file("register", "references/keys.md", f"the key is {key}")
    two.turn(session)

    dump = json.dumps(
        [
            row
            for table in one.storage.table_names()
            for row in one.storage.fetch_all(f"SELECT * FROM {table}")  # noqa: S608
        ],
        default=str,
    )
    assert key not in dump
    for root in (one.root, two.root):
        for path in root.rglob("*"):
            if path.is_file():
                assert key not in path.read_text(), path
    assert "[REDACTED]" in _rows(one, sym)["register/references/keys.md"]


# ── the port itself ─────────────────────────────────────────────────────


class MemoryLibrary:
    """A SkillsStoragePort kept in memory (what a host's own would look like)."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, str]] = {}
        self.versions: dict[str, int] = {}
        self.deleted: list[str] = []
        self.fail = False
        self._guard = threading.Lock()

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("library unreachable")

    def version(self, scope: str) -> int:
        self._check()
        return self.versions.get(scope, 0)

    def list_files(self, scope: str) -> dict[str, str]:
        self._check()
        return {path: digest(text) for path, text in self.files.get(scope, {}).items()}

    def read_files(self, scope: str, paths: Sequence[str]) -> dict[str, str]:
        self._check()
        files = self.files.get(scope, {})
        return {path: files[path] for path in paths if path in files}

    def write_files(self, scope: str, files: Mapping[str, str | None]) -> int:
        self._check()
        with self._guard:
            current = self.files.setdefault(scope, {})
            for path, text in files.items():
                if text is None:
                    current.pop(path, None)
                else:
                    current[path] = text
            self.versions[scope] = self.versions.get(scope, 0) + 1
            return self.versions[scope]

    def delete_scope(self, scope: str) -> int:
        self.deleted.append(scope)
        self.versions.pop(scope, None)
        return len(self.files.pop(scope, {}))


def test_a_host_supplied_library(replicas) -> None:
    library = MemoryLibrary()
    one, two = replicas("one", library=library), replicas("two", library=library)
    sym, session = _user(one)
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    assert set(library.files[sym]) == {"deploy/SKILL.md", "deploy/.skill_meta.json"}

    two.turn(session)
    assert two.names(sym) == ["deploy"]
    assert one.storage.fetch_all("SELECT * FROM skill_files") == []  # not in the database

    one.kernel.purge_symbiote(sym)
    assert library.deleted == [sym] and sym not in library.files


def test_a_turn_survives_an_unreachable_library(replicas, caplog) -> None:
    library = MemoryLibrary()
    one = replicas("one", library=library)
    sym, session = _user(one)
    one.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    one.turn(session)

    library.fail = True
    with caplog.at_level(logging.ERROR, logger="symbiote.core.kernel"):
        assert one.turn(session) == "ok"  # runs with the local copy
    assert "could not sync the skills" in caplog.text
    assert one.names(sym) == ["deploy"]

    # A write made while the library is down is pushed once it is back.
    one.store(sym).create("later", SKILL.format(name="later", body="b"))
    assert "later/SKILL.md" not in library.files[sym]
    library.fail = False
    one.turn(session)
    assert "later/SKILL.md" in library.files[sym]


def test_storage_library_contract(tmp_path: Path) -> None:
    storage = SQLiteAdapter(tmp_path / "lib.db")
    storage.init_schema()
    library = StorageSkillsLibrary(storage)

    assert library.version("s") == 0 and library.list_files("s") == {}
    assert library.write_files("s", {"a/SKILL.md": "one", "a/x.md": "two"}) == 1
    assert library.write_files("s", {"a/x.md": None, "a/SKILL.md": "one!"}) == 2
    assert library.write_files("other", {"b/SKILL.md": "três"}) == 1
    assert library.list_files("s") == {"a/SKILL.md": digest("one!")}
    assert library.read_files("s", ["a/SKILL.md", "a/x.md", "missing"]) == {"a/SKILL.md": "one!"}
    assert library.read_files("other", ["b/SKILL.md"]) == {"b/SKILL.md": "três"}
    assert library.delete_scope("s") == 1
    assert library.version("s") == 0 and library.version("other") == 1
    storage.close()


def test_without_a_library_nothing_is_stored_in_the_database(tmp_path: Path) -> None:
    replica = Replica(tmp_path, "solo", library=None)
    sym, session = _user(replica)
    replica.store(sym).create("deploy", SKILL.format(name="deploy", body="b"))
    replica.turn(session)
    assert replica.names(sym) == ["deploy"]
    assert replica.storage.fetch_all("SELECT * FROM skill_files") == []
    assert replica.storage.fetch_all("SELECT * FROM skill_scopes") == []
    replica.close()
