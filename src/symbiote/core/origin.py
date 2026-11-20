"""Who wrote the input of a turn, when it was not the person.

A turn usually starts with something the person on the other side wrote.
A host may also start one by itself: a scheduled check came due, a webhook
arrived, a job the symbiote was waiting for finished. The input of such a
turn goes to the model in the user position (providers alternate user and
assistant messages), but nobody typed it.

``origin`` says so. It is a short label the host chooses (``"wake"``,
``"webhook"``, ``"schedule"``); ``None`` means the person. The kernel stores
it with the message, returns it wherever messages are read, tells the model
that the text was not written by the user, and keeps such messages out of
what it learns about the user.
"""

from __future__ import annotations

import re

from symbiote.core.exceptions import ValidationError

# What ``None`` stands for; accepted as a value and stored as ``None``.
USER_ORIGIN = "user"

_ORIGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


def normalize_origin(origin: object) -> str | None:
    """``origin`` as it is stored: ``None`` for the person, or the label.

    Raises ``ValidationError`` for anything that is not a label of 1 to 64
    letters, digits, ``_``, ``.``, ``:`` or ``-`` starting with a letter or a
    digit. The label is shown to the model, so it carries no free text.
    """
    if origin is None or origin == USER_ORIGIN:
        return None
    if not isinstance(origin, str) or not _ORIGIN.fullmatch(origin):
        raise ValidationError(
            "origin must be a label of 1 to 64 letters, digits, '_', '.', ':' "
            f"or '-', starting with a letter or a digit; got {origin!r}"
        )
    return origin


def label_origin(content: str, origin: str | None) -> str:
    """``content`` as the model reads it: unchanged when the person wrote it,
    otherwise under one line saying that the user did not."""
    if not origin:
        return content
    return f"[Not written by the user. Source: {origin}]\n{content}"


def speaker(role: str, origin: str | None) -> str:
    """The name of who wrote a message in a transcript: its role, or its
    origin when the message is an input the person did not write."""
    return origin if origin and role == "user" else role


__all__ = ["USER_ORIGIN", "label_origin", "normalize_origin", "speaker"]
