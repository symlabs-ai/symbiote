"""Regression: configure() never loses concurrent updates, keeps one row per
(symbiote, workspace), reads 0.0 back as 0.0, and a new workspace config
starts from the symbiote's config (report item T-4)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.core.identity import IdentityManager
from tests.support import ScriptedLLM, make_kernel


def _rows(kernel, sid: str) -> int:
    return kernel._storage.fetch_one(
        "SELECT COUNT(*) c FROM environment_configs WHERE symbiote_id = ?", (sid,)
    )["c"]


def test_concurrent_add_tools_loses_nothing(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="b", role="r").id
    env = kernel.environment
    n = 16
    barrier = threading.Barrier(n)

    def add(i: int) -> None:
        barrier.wait()
        env.add_tools(sid, [f"tool_{i}"])

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(env.list_tools(sid)) == sorted(f"tool_{i}" for i in range(n))
    assert _rows(kernel, sid) == 1
    kernel.shutdown()


def test_remove_tools_keeps_the_others(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="b", role="r").id
    env = kernel.environment
    env.add_tools(sid, ["a", "b", "c"])
    env.add_tools(sid, ["b"])  # no duplicate

    env.remove_tools(sid, ["b", "missing"])

    assert env.list_tools(sid) == ["a", "c"]
    kernel.shutdown()


def test_zero_is_a_valid_share(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="b", role="r").id
    kernel.environment.configure(symbiote_id=sid, memory_share=0.0, knowledge_share=0.0)

    assert kernel.environment.get_memory_share(sid) == 0.0
    assert kernel.environment.get_knowledge_share(sid) == 0.0
    kernel.shutdown()


def test_new_workspace_config_starts_from_the_symbiote_config(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("ok"))
    sid = kernel.create_symbiote(name="b", role="r").id
    env = kernel.environment
    env.configure(symbiote_id=sid, tools=["fs_read", "bash"], memory_share=0.1)

    env.configure(symbiote_id=sid, workspace_id="ws1", prompt_caching=True)

    assert env.list_tools(sid, "ws1") == ["fs_read", "bash"]
    assert env.get_memory_share(sid, "ws1") == 0.1
    assert env.get_prompt_caching(sid, "ws1") is True
    assert env.get_prompt_caching(sid) is False
    kernel.shutdown()


def test_migration_merges_duplicate_rows(tmp_path: Path) -> None:
    db = tmp_path / "dup.db"
    adapter = SQLiteAdapter(db_path=db)
    adapter.init_schema()
    sid = IdentityManager(storage=adapter).create(name="s", role="r").id
    adapter.execute("DROP INDEX IF EXISTS idx_environment_configs_scope")
    for i, tools in enumerate((["a", "b"], ["b", "c"])):
        adapter.execute(
            "INSERT INTO environment_configs (id, symbiote_id, workspace_id, tools_json, "
            "services_json, humans_json, policies_json, resources_json, memory_share) "
            "VALUES (?, ?, NULL, ?, '[]', '[]', '{}', '{}', ?)",
            (f"row{i}", sid, json.dumps(tools), 0.1 * (i + 1)),
        )
    adapter.close()

    reopened = SQLiteAdapter(db_path=db)
    reopened.init_schema()
    rows = reopened.fetch_all("SELECT id, tools_json, memory_share FROM environment_configs")

    assert len(rows) == 1
    assert rows[0]["id"] == "row1"  # the most recent one wins
    assert sorted(json.loads(rows[0]["tools_json"])) == ["a", "b", "c"]
    try:
        reopened.execute(
            "INSERT INTO environment_configs (id, symbiote_id, workspace_id, tools_json, "
            "services_json, humans_json, policies_json, resources_json) "
            "VALUES ('x', ?, NULL, '[]', '[]', '[]', '{}', '{}')",
            (sid,),
        )
        duplicated = True
    except sqlite3.IntegrityError:
        duplicated = False
    assert duplicated is False
    reopened.close()
