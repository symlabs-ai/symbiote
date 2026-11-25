"""Ready messages can be appended without running a model or tools."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from symbiote.adapters.storage.sqlite import SQLiteAdapter
from symbiote.config.models import KernelConfig
from symbiote.core.exceptions import (
    EntityNotFoundError,
    IdempotencyConflictError,
    ValidationError,
)
from symbiote.core.kernel import SymbioteKernel


@pytest.fixture()
def kernel(tmp_path: Path):
    instance = SymbioteKernel(KernelConfig(db_path=tmp_path / "append.db"))
    symbiote = instance.create_symbiote("ready", "assistant")
    session = instance.start_session(symbiote.id)
    yield instance, session
    instance.shutdown()


def test_append_message_persists_without_a_model_or_turn_artifacts(kernel) -> None:
    instance, session = kernel

    message = instance.append_message(
        session.id,
        "assistant",
        "Your report is ready.",
        origin="symbios.task",
    )

    assert message.role == "assistant"
    assert message.content == "Your report is ready."
    assert message.origin == "symbios.task"
    assert message.idempotency_key is None
    assert instance.inspector.list_messages(session.id) == [
        {
            "id": message.id,
            "role": "assistant",
            "content": "Your report is ready.",
            "created_at": message.created_at.isoformat(),
            "origin": "symbios.task",
            "idempotency_key": None,
        }
    ]
    assert instance.inspector.list_traces(session_id=session.id) == []
    assert instance.inspector.list_turn_contexts(session_id=session.id) == []
    assert instance.inspector.list_scores(session_id=session.id) == []
    assert instance.inspector.usage_totals(session_id=session.id)["llm_calls"] == 0


def test_identical_retry_returns_the_original_message(kernel) -> None:
    instance, session = kernel

    first = instance.append_message(
        session.id, "assistant", "Done", "symbios.task", "task:42:done"
    )
    retry = instance.append_message(
        session.id, "assistant", "Done", "symbios.task", "task:42:done"
    )

    assert retry == first
    assert retry.idempotency_key == "task:42:done"
    assert len(instance.inspector.list_messages(session.id)) == 1


def test_idempotency_survives_kernel_restart(tmp_path: Path) -> None:
    db = tmp_path / "durable.db"
    first_kernel = SymbioteKernel(KernelConfig(db_path=db))
    symbiote = first_kernel.create_symbiote("ready", "assistant")
    session = first_kernel.start_session(symbiote.id)
    first = first_kernel.append_message(
        session.id, "system", "Imported", idempotency_key="import:7"
    )
    first_kernel.shutdown()

    second_kernel = SymbioteKernel(KernelConfig(db_path=db))
    retry = second_kernel.append_message(
        session.id, "system", "Imported", idempotency_key="import:7"
    )

    assert retry == first
    assert second_kernel.inspector.list_messages(session.id)[0]["id"] == first.id
    second_kernel.shutdown()


def test_idempotency_compares_the_original_payload_when_storage_redacts(
    tmp_path: Path,
) -> None:
    instance = SymbioteKernel(KernelConfig(db_path=tmp_path / "redacted.db"))
    instance.set_redactor(lambda text: text.replace("secret", "[hidden]"))
    symbiote = instance.create_symbiote("ready", "assistant")
    session = instance.start_session(symbiote.id)

    first = instance.append_message(
        session.id, "assistant", "secret", idempotency_key="event:1"
    )
    retry = instance.append_message(
        session.id, "assistant", "secret", idempotency_key="event:1"
    )

    assert first == retry
    assert first.content == "[hidden]"
    with pytest.raises(IdempotencyConflictError):
        instance.append_message(
            session.id, "assistant", "different", idempotency_key="event:1"
        )
    instance.shutdown()


def test_same_key_with_different_payload_conflicts_without_an_extra_message(kernel) -> None:
    instance, session = kernel
    original = instance.append_message(
        session.id, "assistant", "Done", idempotency_key="task:42"
    )

    for role, content, origin in (
        ("user", "Done", None),
        ("assistant", "Different", None),
        ("assistant", "Done", "symbios.task"),
    ):
        with pytest.raises(IdempotencyConflictError) as error:
            instance.append_message(
                session.id,
                role,
                content,
                origin=origin,
                idempotency_key="task:42",
            )
        assert error.value.session_id == session.id
        assert error.value.idempotency_key == "task:42"

    assert [row["id"] for row in instance.inspector.list_messages(session.id)] == [
        original.id
    ]


def test_same_key_is_independent_between_sessions(kernel) -> None:
    instance, first_session = kernel
    symbiote_id = instance.get_session(first_session.id).symbiote_id
    second_session = instance.start_session(symbiote_id)

    first = instance.append_message(
        first_session.id, "assistant", "One", idempotency_key="same"
    )
    second = instance.append_message(
        second_session.id, "assistant", "Two", idempotency_key="same"
    )

    assert first.id != second.id


def test_concurrent_retries_across_adapters_create_one_message(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.db"
    bootstrap = SymbioteKernel(KernelConfig(db_path=db))
    symbiote = bootstrap.create_symbiote("ready", "assistant")
    session = bootstrap.start_session(symbiote.id)
    bootstrap.shutdown()

    first_storage = SQLiteAdapter(db, check_same_thread=False)
    second_storage = SQLiteAdapter(db, check_same_thread=False)
    first = SymbioteKernel(KernelConfig(db_path=db), storage=first_storage)
    second = SymbioteKernel(KernelConfig(db_path=db), storage=second_storage)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda active: active.append_message(
                        session.id,
                        "assistant",
                        "Done",
                        idempotency_key="concurrent",
                    ),
                    (first, second),
                )
            )
        assert results[0] == results[1]
        assert len(first.inspector.list_messages(session.id)) == 1
    finally:
        first.shutdown()
        second.shutdown()
        first_storage.close()
        second_storage.close()


@pytest.mark.parametrize("role", ["tool", "", None, 7])
def test_invalid_role_is_rejected_without_a_write(kernel, role) -> None:
    instance, session = kernel
    with pytest.raises(ValidationError, match="role"):
        instance.append_message(session.id, role, "text")
    assert instance.inspector.list_messages(session.id) == []


@pytest.mark.parametrize("content", ["", "   ", None, 7])
def test_invalid_content_is_rejected_without_a_write(kernel, content) -> None:
    instance, session = kernel
    with pytest.raises(ValidationError, match="content"):
        instance.append_message(session.id, "assistant", content)
    assert instance.inspector.list_messages(session.id) == []


@pytest.mark.parametrize("key", ["", "   ", 7, "x" * 501])
def test_invalid_idempotency_key_is_rejected_without_a_write(kernel, key) -> None:
    instance, session = kernel
    with pytest.raises(ValidationError, match="idempotency_key"):
        instance.append_message(
            session.id, "assistant", "text", idempotency_key=key
        )
    assert instance.inspector.list_messages(session.id) == []


def test_invalid_origin_and_unknown_session_are_rejected(kernel) -> None:
    instance, session = kernel
    with pytest.raises(ValidationError, match="origin"):
        instance.append_message(session.id, "assistant", "text", origin="two words")
    with pytest.raises(EntityNotFoundError):
        instance.append_message("missing", "assistant", "text")
    assert instance.inspector.list_messages(session.id) == []
