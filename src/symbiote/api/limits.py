"""Size limits for the HTTP API.

Every bound the API enforces lives here so operators can find (and tests can
reference) them in one place.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

# Whole request body, enforced before the route runs (Content-Length and
# chunked bodies alike). Leaves room for ``extra_context`` next to a message.
MAX_BODY_BYTES = 5 * 1024 * 1024

# One chat/session message.
MAX_MESSAGE_CHARS = 1_000_000

# Identifiers and short labels: symbiote name/role, tool ids and names,
# API key names, tenant ids, external session keys.
MAX_NAME_CHARS = 200
MAX_KEY_CHARS = 500

# Free text that is not a message: session goal, tool descriptions.
MAX_TEXT_CHARS = 10_000


class RequestBodyTooLarge(HTTPException):
    """Raised while streaming a body past ``MAX_BODY_BYTES``.

    An ``HTTPException`` on purpose: FastAPI turns any other exception raised
    while reading the body into a generic 400.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413, detail=f"Request body exceeds {limit} bytes"
        )


class BodySizeLimitMiddleware:
    """Pure ASGI middleware rejecting oversized request bodies with 413."""

    def __init__(self, app: Any, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    await self._reject(send)
                    return
                break

        received = 0
        limit = self.max_bytes

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge(limit)
            return message

        started = False

        async def tracking_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge:
            if not started:
                await self._reject(send)
            else:
                raise

    async def _reject(self, send: Any) -> None:
        body = (
            b'{"detail": "Request body exceeds ' + str(self.max_bytes).encode() + b' bytes"}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
