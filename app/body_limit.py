"""Refuse request bodies over MAX_BODY_BYTES with 413 `payload_too_large`.

FastAPI reads and parses a JSON body before any dependency runs, so without
this an anonymous caller could make the server buffer and parse a body of any
size before authentication refuses it. Two cases:

- A declared `Content-Length` over the cap is refused before the app runs,
  whatever the route (including ones that never read a body).
- A body without a length (chunked) is counted as it is received; the read
  that crosses the cap raises, so the app never holds more than the cap.
"""

import json

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOO_LARGE_MESSAGE = "The request body is too large."


class BodyLimitMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware) so it can wrap `receive`."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _send_too_large(send)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # An HTTPException is what FastAPI re-raises from a body
                    # read (anything else becomes 400), so it reaches the app's
                    # handler in main.py and leaves in the contract's shape.
                    raise HTTPException(413, TOO_LARGE_MESSAGE)
            return message

        await self.app(scope, counting_receive, send)


async def _send_too_large(send: Send) -> None:
    body = json.dumps({"error": {"code": "payload_too_large", "message": TOO_LARGE_MESSAGE}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # The unread body is still on the socket; do not reuse it.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
