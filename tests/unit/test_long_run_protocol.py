"""B-74: the long-run blocks run in the kernel's agent protocol and nudge its
background skill review, like a plain chat turn."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import symbiote.runners.chat as chat_module
from symbiote.core.context import AssembledContext
from symbiote.runners.base import RunResult
from symbiote.runners.long_run import LongRunRunner
from tests.support import ScriptedLLM, make_kernel

PLAN = json.dumps(
    [
        {"name": "A", "description": "d", "success_criteria": "c"},
        {"name": "B", "description": "d", "success_criteria": "c"},
    ]
)


def _ctx() -> AssembledContext:
    return AssembledContext(symbiote_id="s", session_id="x", user_input="build")


class _FakeBlockRunner:
    """Stands in for the block ChatRunner; records how it was built."""

    built: list[dict[str, Any]] = []
    iterations_per_block = 3

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._iters_since_skill_review = 0
        _FakeBlockRunner.built.append(kwargs)

    def run(self, context: AssembledContext, **_: Any) -> RunResult:
        _FakeBlockRunner.built[-1]["counter_in"] = self._iters_since_skill_review
        self._iters_since_skill_review += self.iterations_per_block
        return RunResult(success=True, output="done", runner_type="chat")


@pytest.fixture
def fake_block_runner(monkeypatch: pytest.MonkeyPatch) -> type[_FakeBlockRunner]:
    _FakeBlockRunner.built = []
    monkeypatch.setattr(chat_module, "ChatRunner", _FakeBlockRunner)
    return _FakeBlockRunner


def test_blocks_get_protocol_and_background_review(
    fake_block_runner: type[_FakeBlockRunner],
) -> None:
    def review(symbiote_id: str) -> None:
        return None

    runner = LongRunRunner(
        ScriptedLLM(PLAN),
        agent_protocol="poolside_laguna_v1",
        background_review=review,
        skill_nudge_interval=7,
    )

    runner.run(_ctx())

    assert len(fake_block_runner.built) == 2
    for kwargs in fake_block_runner.built:
        assert kwargs["agent_protocol"].protocol_id == "poolside_laguna_v1"
        assert kwargs["background_review"] is review
        assert kwargs["skill_nudge_interval"] == 7


def test_skill_review_counter_carries_across_blocks(
    fake_block_runner: type[_FakeBlockRunner],
) -> None:
    runner = LongRunRunner(ScriptedLLM(PLAN), background_review=lambda _sid: None)

    runner.run(_ctx())

    # Block B starts where block A stopped, so a nudge interval longer than
    # one block still fires during a long run.
    assert [k["counter_in"] for k in fake_block_runner.built] == [0, 3]


def test_default_protocol_is_legacy() -> None:
    assert LongRunRunner(ScriptedLLM(PLAN)).agent_protocol_id == "legacy_text_v1"


def test_kernel_hands_its_protocol_to_long_run(tmp_path: Path) -> None:
    kernel = make_kernel(tmp_path, ScriptedLLM("x"), agent_protocol="poolside_laguna_v1")
    long_run = kernel._runner_registry.get("long_run")
    assert isinstance(long_run, LongRunRunner)
    assert long_run.agent_protocol_id == "poolside_laguna_v1"
    assert long_run._background_review == kernel._background_review_for

    kernel.set_agent_protocol(None)
    assert long_run.agent_protocol_id == "legacy_text_v1"
    kernel.set_agent_protocol("poolside_laguna_v1")
    assert long_run.agent_protocol_id == "poolside_laguna_v1"
    kernel.shutdown()
