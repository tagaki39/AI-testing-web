"""Middleware that logs every API request and response with details."""

from __future__ import annotations

import json
import logging
import time
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

# Paths that are noisy and should be logged at DEBUG level
_NOISY_PATHS = {"/docs", "/openapi.json", "/redoc", "/"}

# Max body bytes to log (avoid dumping huge payloads like screenshots)
_MAX_BODY_LOG = 2048


def _truncate(text: str, limit: int = _MAX_BODY_LOG) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... (truncated, total {len(text)} chars)"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs method, path, query params, request body, status code, and response body for every API call."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        method = request.method

        # Skip static noise
        log_fn = logger.debug if path in _NOISY_PATHS else logger.info

        # --- Read request body ---
        body_bytes = await request.body()
        body_str = ""
        content_type = request.headers.get("content-type", "")
        if body_bytes and "application/json" in content_type:
            try:
                body_str = json.dumps(json.loads(body_bytes), ensure_ascii=False)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body_str = body_bytes[:_MAX_BODY_LOG].decode("utf-8", errors="replace")
        elif body_bytes and "multipart/form-data" in content_type:
            body_str = f"<multipart, {len(body_bytes)} bytes>"
        elif body_bytes:
            body_str = body_bytes[:_MAX_BODY_LOG].decode("utf-8", errors="replace")

        query = str(request.query_params)
        log_fn("--> %s %s%s %s",
               method, path,
               f"?{query}" if query else "",
               _truncate(body_str) if body_str else "(no body)")

        # --- Call next ---
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000

        # --- Read response body ---
        resp_content_type = response.headers.get("content-type", "")
        resp_body_str = ""

        # Don't consume streaming responses (SSE, large downloads)
        if isinstance(response, StreamingResponse) or "text/event-stream" in resp_content_type:
            resp_body_str = f"<streaming, content-type={resp_content_type}>"
        else:
            body_chunks: list[bytes] = []
            async for chunk in response.body_iterator:
                body_chunks.append(chunk)
            resp_body = b"".join(body_chunks)

            if resp_body and "application/json" in resp_content_type:
                try:
                    resp_body_str = json.dumps(json.loads(resp_body), ensure_ascii=False)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    resp_body_str = resp_body[:_MAX_BODY_LOG].decode("utf-8", errors="replace")
            elif resp_body:
                resp_body_str = f"<{resp_content_type}, {len(resp_body)} bytes>"

            # Rebuild response so it can still be sent to client
            response = Response(
                content=resp_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        log_fn("<-- %s %s %d %.1fms %s",
               method, path, response.status_code, elapsed_ms,
               _truncate(resp_body_str) if resp_body_str else "(empty)")

        return response
