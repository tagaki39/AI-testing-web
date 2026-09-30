"""In-memory idempotency key store and FastAPI middleware."""

import threading
import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

DEFAULT_TTL_SECONDS = 3600  # 1 hour


@dataclass
class _CacheEntry:
    status_code: int
    headers: dict[str, str]
    body: bytes
    expires_at: float


class IdempotencyStore:
    """Thread-safe in-memory store with lazy TTL eviction."""

    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def lookup(self, key: str) -> _CacheEntry | None:
        with self._lock:
            self._sweep()
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                del self._store[key]
                return None
            return entry

    def store(self, key: str, status_code: int, headers: dict[str, str], body: bytes) -> None:
        with self._lock:
            self._sweep()
            self._store[key] = _CacheEntry(
                status_code=status_code,
                headers=headers,
                body=body,
                expires_at=time.monotonic() + self._ttl,
            )

    def _sweep(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._store.items() if now > v.expires_at]
        for k in expired:
            del self._store[k]


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Caches POST responses when an Idempotency-Key header is present."""

    def __init__(self, app, store: IdempotencyStore | None = None) -> None:
        super().__init__(app)
        self._store = store or IdempotencyStore()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        key = request.headers.get("Idempotency-Key", "").strip()
        if request.method != "POST" or not key:
            return await call_next(request)

        cached = self._store.lookup(key)
        if cached is not None:
            return Response(
                content=cached.body,
                status_code=cached.status_code,
                headers=cached.headers,
            )

        response = await call_next(request)

        body_chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            body_chunks.append(chunk)
        body = b"".join(body_chunks)

        resp_headers = dict(response.headers)

        content_type = resp_headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return Response(content=body, status_code=response.status_code, headers=resp_headers)

        if response.status_code >= 500:
            return Response(content=body, status_code=response.status_code, headers=resp_headers)

        cacheable_headers = {
            k: v for k, v in resp_headers.items()
            if k.lower() not in ("content-length", "transfer-encoding")
        }
        self._store.store(key, response.status_code, cacheable_headers, body)

        return Response(content=body, status_code=response.status_code, headers=cacheable_headers)
