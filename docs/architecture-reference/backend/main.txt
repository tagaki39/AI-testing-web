"""FastAPI application entrypoint."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from app.api.routes.artifacts import router as artifacts_router
from app.api.router import build_api_router
from app.core.config import get_settings
from app.core.idempotency import IdempotencyMiddleware
from app.core.logging_config import get_uvicorn_log_config, setup_logging
from app.core.rate_limit import RateLimitMiddleware
from app.core.request_logging import RequestLoggingMiddleware
from app.db import verify_database_connection


ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    verify_database_connection()
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_STATES_DIR = Path(settings.storage_state_dir)
    STORAGE_STATES_DIR.mkdir(parents=True, exist_ok=True)
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
    )
    app.state.artifacts_dir = ARTIFACTS_DIR
    app.state.storage_states_dir = STORAGE_STATES_DIR
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.auth_session_secret,
        session_cookie=settings.auth_session_cookie_name,
        max_age=settings.auth_session_max_age_seconds,
        same_site=settings.auth_session_same_site,
        https_only=settings.auth_session_https_only,
    )
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(build_api_router())
    app.include_router(artifacts_router)

    @app.get("/", tags=["meta"], summary="Service metadata")
    def read_root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "environment": settings.app_env,
            "docs_url": "/docs",
        }

    return app

def main() -> None:
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8000"))
    uvicorn.run("app.main:create_app", host=host, port=port, reload=True, factory=True, log_config=get_uvicorn_log_config())


if __name__ == "__main__":
    main()
