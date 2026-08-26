"""Services for runtime-manageable AI settings."""

from __future__ import annotations

import os

import app.core.config as config_module
from sqlalchemy.orm import Session

from app.locators.ai_visual import get_ai_visual_runtime_stats
from app.schemas.settings import (
    AISettingsOverviewResponse,
    AISettingsResponse,
    AISettingsUpdateRequest,
    AIVisualStats,
)
from app.services.dsl import get_dsl_generation_durable_stats


def get_ai_settings() -> AISettingsResponse:
    settings = config_module.get_settings()
    return AISettingsResponse(
        enable_ai_dsl_generate=settings.enable_ai_dsl_generate,
        ai_dsl_timeout_ms=settings.ai_dsl_timeout_ms,
        ai_dsl_base_url=settings.ai_dsl_base_url,
        ai_dsl_model=settings.ai_dsl_model,
        ai_dsl_strict_mode=settings.ai_dsl_strict_mode,
        ai_dsl_allow_auto_repair=settings.ai_dsl_allow_auto_repair,
        has_ai_dsl_api_key=bool(settings.ai_dsl_api_key),
        enable_ai_visual_locate=settings.enable_ai_visual_locate,
        ai_visual_timeout_ms=settings.ai_visual_timeout_ms,
        ai_visual_failure_threshold=settings.ai_visual_failure_threshold,
        ai_visual_cooldown_seconds=settings.ai_visual_cooldown_seconds,
        ai_visual_rate_limit_per_minute=settings.ai_visual_rate_limit_per_minute,
        vlm_base_url=settings.vlm_base_url,
        vlm_model=settings.vlm_model,
        vlm_model_family=settings.vlm_model_family,
        has_vlm_api_key=bool(settings.vlm_api_key),
        enable_ai_planning=settings.enable_ai_planning,
        ai_planning_model=settings.ai_planning_model,
        ai_planning_base_url=settings.ai_planning_base_url,
        ai_planning_timeout_ms=settings.ai_planning_timeout_ms,
        has_ai_planning_api_key=bool(settings.ai_planning_api_key),
    )


def update_ai_settings(payload: AISettingsUpdateRequest) -> AISettingsResponse:
    env_updates = {
        "ENABLE_AI_DSL_GENERATE": _format_bool(payload.enable_ai_dsl_generate),
        "AI_DSL_TIMEOUT_MS": str(payload.ai_dsl_timeout_ms),
        "AI_DSL_BASE_URL": payload.ai_dsl_base_url,
        "AI_DSL_MODEL": payload.ai_dsl_model or "",
        "AI_DSL_STRICT_MODE": _format_bool(payload.ai_dsl_strict_mode),
        "AI_DSL_ALLOW_AUTO_REPAIR": _format_bool(payload.ai_dsl_allow_auto_repair),
        "ENABLE_AI_VISUAL_LOCATE": _format_bool(payload.enable_ai_visual_locate),
        "AI_VISUAL_TIMEOUT_MS": str(payload.ai_visual_timeout_ms),
        "AI_VISUAL_FAILURE_THRESHOLD": str(payload.ai_visual_failure_threshold),
        "AI_VISUAL_COOLDOWN_SECONDS": str(payload.ai_visual_cooldown_seconds),
        "AI_VISUAL_RATE_LIMIT_PER_MINUTE": str(payload.ai_visual_rate_limit_per_minute),
        "VLM_BASE_URL": payload.vlm_base_url,
        "VLM_MODEL": payload.vlm_model or "",
        "VLM_MODEL_FAMILY": payload.vlm_model_family,
        "ENABLE_AI_PLANNING": _format_bool(payload.enable_ai_planning),
        "AI_PLANNING_MODEL": payload.ai_planning_model or "",
        "AI_PLANNING_BASE_URL": payload.ai_planning_base_url,
        "AI_PLANNING_TIMEOUT_MS": str(payload.ai_planning_timeout_ms),
    }

    if payload.clear_ai_dsl_api_key:
        env_updates["AI_DSL_API_KEY"] = ""
    elif payload.ai_dsl_api_key is not None:
        env_updates["AI_DSL_API_KEY"] = payload.ai_dsl_api_key

    if payload.clear_vlm_api_key:
        env_updates["VLM_API_KEY"] = ""
    elif payload.vlm_api_key is not None:
        env_updates["VLM_API_KEY"] = payload.vlm_api_key

    if payload.clear_ai_planning_api_key:
        env_updates["AI_PLANNING_API_KEY"] = ""
    elif payload.ai_planning_api_key is not None:
        env_updates["AI_PLANNING_API_KEY"] = payload.ai_planning_api_key

    _persist_env_updates(env_updates)
    for key, value in env_updates.items():
        os.environ[key] = value
    config_module.get_settings.cache_clear()
    return get_ai_settings()


def get_ai_settings_overview(session: Session) -> AISettingsOverviewResponse:
    settings = config_module.get_settings()
    ai_visual_stats = get_ai_visual_runtime_stats()
    return AISettingsOverviewResponse(
        ai_dsl_enabled=settings.enable_ai_dsl_generate,
        ai_dsl_model=settings.ai_dsl_model,
        ai_dsl_strict_mode=settings.ai_dsl_strict_mode,
        ai_dsl_allow_auto_repair=settings.ai_dsl_allow_auto_repair,
        generation_stats=get_dsl_generation_durable_stats(session),
        ai_visual_stats=AIVisualStats(**ai_visual_stats.__dict__),
    )


def _persist_env_updates(updates: dict[str, str]) -> None:
    env_path = config_module.ENV_FILE_PATH
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    key_to_index: dict[str, int] = {}
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _ = stripped.split("=", 1)
        key_to_index[key.strip()] = index

    for key, value in updates.items():
        line = f"{key}={value}"
        if key in key_to_index:
            lines[key_to_index[key]] = line
        else:
            lines.append(line)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_bool(value: bool) -> str:
    return "true" if value else "false"
