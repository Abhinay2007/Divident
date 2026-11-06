"""Request IDs and the LLD's one-megabyte request limit."""

import json
from uuid import UUID, uuid4


MAX_REQUEST_BYTES = 1_000_000


class RequestContextMiddleware:
    def __init__(self, app, max_body_bytes: int = MAX_REQUEST_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_request_id = headers.get(b"x-request-id", b"").decode("latin-1")
        try:
            request_id = str(UUID(raw_request_id))
        except (ValueError, AttributeError):
            request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > self.max_body_bytes
            except ValueError:
                too_large = True
            if too_large:
                await self._too_large(send_with_request_id, request_id)
                return

        received_bytes = 0
        too_large = False
        sent_error_body = False

        async def limited_receive():
            nonlocal received_bytes, too_large
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    too_large = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def enforce_size(send_message):
            nonlocal sent_error_body
            if not too_large:
                await send_with_request_id(send_message)
            elif send_message["type"] == "http.response.start":
                await send_with_request_id({
                    **send_message,
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                })
            elif send_message["type"] == "http.response.body" and not sent_error_body:
                sent_error_body = True
                payload = json.dumps({
                    "error": "validation_error",
                    "code": "request_too_large",
                    "request_id": request_id,
                }, separators=(",", ":")).encode("utf-8")
                await send_with_request_id({
                    "type": "http.response.body", "body": payload, "more_body": False,
                })

        await self.app(scope, limited_receive, enforce_size)

    @staticmethod
    async def _too_large(send, request_id: str) -> None:
        payload = json.dumps({
            "error": "validation_error",
            "code": "request_too_large",
            "request_id": request_id,
        }, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
        })
        await send({"type": "http.response.body", "body": payload})
