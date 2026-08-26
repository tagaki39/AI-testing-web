"""Optional AI visual locator and candidate ranker."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import threading
from dataclasses import dataclass
from io import BytesIO
from time import monotonic
from typing import Any, Literal
from urllib import request

from app.core.config import get_settings


ModelFamily = Literal["qwen-vl", "gemini", "gpt-4o", "qwen2.5-vl", "glm"]

SYSTEM_PROMPT = """You are an AI assistant that locates a UI element in a screenshot.
Return JSON only in the shape {"bbox":[xmin,ymin,xmax,ymax],"errors":["..."]?}."""
SECTION_SYSTEM_PROMPT = """You are an AI assistant that finds the broad page area that contains a UI element.
Return JSON only in the shape {"bbox":[xmin,ymin,xmax,ymax],"errors":["..."]?}."""
RANK_CANDIDATE_SYSTEM_PROMPT = """You rank numbered UI candidates in a screenshot.
Return JSON only in the shape {"candidate_index": number, "errors":["..."]?}."""

logger = logging.getLogger(__name__)
DEEP_LOCATE_SECTION_TIMEOUT_RATIO = 0.4
MIN_STAGE_TIMEOUT_SECONDS = 0.5


@dataclass
class AIVisualRuntimeState:
    window_started_at: float = 0.0
    window_request_count: int = 0
    consecutive_failures: int = 0
    opened_until: float = 0.0
    last_failure_reason: str = ""


@dataclass
class AIVisualRuntimeStatsState:
    locate_requests: int = 0
    locate_success_count: int = 0
    locate_failure_count: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    cache_invalidated_count: int = 0
    breaker_skip_count: int = 0
    rate_limited_skip_count: int = 0
    disabled_skip_count: int = 0
    total_locate_latency_ms: float = 0.0
    max_locate_latency_ms: float = 0.0


@dataclass(frozen=True)
class AIVisualRuntimeStatsSnapshot:
    locate_requests: int
    locate_success_count: int
    locate_failure_count: int
    cache_hit_count: int
    cache_miss_count: int
    cache_invalidated_count: int
    breaker_skip_count: int
    rate_limited_skip_count: int
    disabled_skip_count: int
    avg_locate_latency_ms: float
    max_locate_latency_ms: float


RUNTIME_STATE = AIVisualRuntimeState()
RUNTIME_STATS = AIVisualRuntimeStatsState()
_STATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class AILocateResult:
    center: tuple[int, int]
    bbox: tuple[int, int, int, int]
    confidence: float
    raw_response: str


@dataclass(frozen=True)
class AIVisionCandidateBox:
    index: int
    label: str
    bbox: tuple[int, int, int, int]


VLM_FALLBACK_MODELS_HARDCODED = ["glm-4.6v-flash", "glm-4.6v"]

# Models that do NOT support base64 image input (require URL only)
VLM_MODELS_NO_BASE64 = set()


def _get_vlm_fallback_models() -> list[str]:
    try:
        from app.core.config import get_settings
        return get_settings().vlm_fallback_models or VLM_FALLBACK_MODELS_HARDCODED
    except Exception:
        return list(VLM_FALLBACK_MODELS_HARDCODED)


def _model_supports_base64(model_name: str) -> bool:
    """Check if a model supports base64 image input."""
    return model_name not in VLM_MODELS_NO_BASE64


def _is_rate_limited_error(exc: Exception) -> bool:
    """Check if an exception indicates a rate-limit (429) or server overload."""
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg or "rate" in msg:
        return True
    # Check urllib.error.HTTPError (has .code attribute)
    status = getattr(exc, "code", None)
    if status == 429:
        return True
    # Check requests-style response (has .response.status_code)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429


def locate_element_by_vision(
    *,
    screenshot_base64: str,
    target_description: str,
    image_width: int,
    image_height: int,
    model_family: ModelFamily | None = None,
    deep_locate: bool = False,
) -> AILocateResult | None:
    settings = get_settings()
    if not settings.enable_ai_visual_locate:
        _record_disabled_skip()
        with _STATE_LOCK:
            RUNTIME_STATE.last_failure_reason = "VLM 视觉定位未启用（enable_ai_visual_locate=False）"
        return None
    if not settings.vlm_api_key:
        with _STATE_LOCK:
            RUNTIME_STATE.last_failure_reason = "VLM API 凭证未配置（缺少 vlm_api_key 环境变量），所有 VLM 视觉定位功能不可用"
        logger.error("VLM locate skipped: vlm_api_key is not configured. Set VLM_API_KEY in .env to enable visual locating.")
        return None
    if not _can_attempt_request(track_stats=True):
        with _STATE_LOCK:
            if RUNTIME_STATE.opened_until > monotonic():
                RUNTIME_STATE.last_failure_reason = (
                    f"VLM 断路器已打开（连续失败 {RUNTIME_STATE.consecutive_failures} 次），"
                    f"冷却中（剩余 {round(RUNTIME_STATE.opened_until - monotonic(), 1)} 秒）"
                )
            else:
                RUNTIME_STATE.last_failure_reason = "VLM 请求频率超限"
        return None

    family = model_family or settings.vlm_model_family
    _record_attempt()
    _record_locate_request()
    started_at = monotonic()

    models_to_try = list(_get_vlm_fallback_models())
    failed_models: list[str] = []

    logger.info("[VLM-FALLBACK] Starting fallback loop with models: %s", models_to_try)

    for model_name in models_to_try:
        logger.info("[VLM-FALLBACK] === Trying model: %s ===", model_name)

        # Skip models that don't support base64 when using base64 input
        if not _model_supports_base64(model_name):
            failed_models.append(f"{model_name}: 不支持 Base64 图片格式")
            logger.info("[VLM-FALLBACK] Model %s skipped: does not support base64 image input", model_name)
            continue

        try:
            if deep_locate:
                logger.info("[VLM-FALLBACK] Calling _deep_locate with model=%s", model_name)
                result = _deep_locate(
                    screenshot_base64=screenshot_base64,
                    target_description=target_description,
                    image_width=image_width,
                    image_height=image_height,
                    model_family=family,
                    api_key=settings.vlm_api_key,
                    model=model_name,
                    base_url=settings.vlm_base_url,
                    timeout_seconds=max(1.0, settings.ai_visual_timeout_ms / 1000),
                )
            else:
                logger.info("[VLM-FALLBACK] Calling _single_locate with model=%s", model_name)
                result = _single_locate(
                    screenshot_base64=screenshot_base64,
                    target_description=target_description,
                    image_width=image_width,
                    image_height=image_height,
                    model_family=family,
                    api_key=settings.vlm_api_key,
                    model=model_name,
                    base_url=settings.vlm_base_url,
                    timeout_seconds=max(1.0, settings.ai_visual_timeout_ms / 1000),
                )
        except Exception as exc:
            failed_models.append(f"{model_name}: {type(exc).__name__}: {exc}")
            is_rate_limited = _is_rate_limited_error(exc)
            logger.error(
                "[VLM-FALLBACK] Model %s EXCEPTION: type=%s, msg=%s, is_rate_limited=%s",
                model_name, type(exc).__name__, exc, is_rate_limited
            )
            if is_rate_limited:
                logger.warning("[VLM-FALLBACK] Model %s rate limited (429), will try next fallback model", model_name)
            else:
                logger.warning("[VLM-FALLBACK] Model %s failed, will try next fallback model", model_name)
            logger.info("[VLM-FALLBACK] Continuing to next model in list...")
            continue

        if result is None:
            failed_models.append(f"{model_name}: 未定位到目标")
            logger.warning("[VLM-FALLBACK] Model %s returned None, will try next fallback model", model_name)
            continue

        logger.info("[VLM-FALLBACK] Model %s succeeded!", model_name)
        _record_locate_result(success=True, latency_ms=_elapsed_milliseconds(started_at))
        _record_success()
        return result

    # All models failed
    _record_locate_result(success=False, latency_ms=_elapsed_milliseconds(started_at))
    _record_failure()
    reason = f"所有 VLM 模型均失败：{'; '.join(failed_models)}"
    with _STATE_LOCK:
        RUNTIME_STATE.last_failure_reason = reason
    logger.warning("All VLM fallback models failed: %s", reason)
    return None


def rank_candidates_by_vision(
    *,
    screenshot_base64: str,
    target_description: str,
    candidates: list[AIVisionCandidateBox],
    model_family: ModelFamily | None = None,
) -> int | None:
    if not candidates:
        return None

    settings = get_settings()
    if not settings.enable_ai_visual_locate:
        return None
    if not settings.vlm_api_key or not settings.vlm_model:
        return None
    if not _can_attempt_request():
        return None

    family = model_family or settings.vlm_model_family
    _record_attempt()
    try:
        response_text = _call_candidate_ranker(
            screenshot_base64=screenshot_base64,
            target_description=target_description,
            candidates=candidates,
            api_key=settings.vlm_api_key,
            model=settings.vlm_model,
            base_url=settings.vlm_base_url,
            timeout_seconds=max(1.0, settings.ai_visual_timeout_ms / 1000),
        )
        ranked_index = _parse_candidate_index_response(
            response_text=response_text,
            candidate_indexes={candidate.index for candidate in candidates},
            model_family=family,
        )
    except Exception as exc:
        _record_failure()
        logger.warning("AI visual candidate rank request failed: %s", exc)
        return None

    if ranked_index is None:
        _record_failure()
        return None

    _record_success()
    return ranked_index


def _single_locate(
    *,
    screenshot_base64: str,
    target_description: str,
    image_width: int,
    image_height: int,
    model_family: ModelFamily,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
    prompt_text: str | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> AILocateResult | None:
    response_text = _call_vlm(
        screenshot_base64=screenshot_base64,
        prompt_text=prompt_text or f"Find: {target_description}",
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        model_family=model_family,
        system_prompt=system_prompt,
    )
    return _parse_bbox_response(
        response_text=response_text,
        image_width=image_width,
        image_height=image_height,
        model_family=model_family,
    )


def _deep_locate(
    *,
    screenshot_base64: str,
    target_description: str,
    image_width: int,
    image_height: int,
    model_family: ModelFamily,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
) -> AILocateResult | None:
    deadline = monotonic() + max(timeout_seconds, MIN_STAGE_TIMEOUT_SECONDS)
    section_timeout = _compute_stage_timeout(
        deadline=deadline,
        requested_seconds=max(MIN_STAGE_TIMEOUT_SECONDS, timeout_seconds * DEEP_LOCATE_SECTION_TIMEOUT_RATIO),
    )
    if section_timeout is None:
        return None
    section_result = _locate_section(
        screenshot_base64=screenshot_base64,
        target_description=target_description,
        image_width=image_width,
        image_height=image_height,
        model_family=model_family,
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=section_timeout,
    )
    remaining_timeout = _remaining_timeout_seconds(deadline)
    if remaining_timeout is None:
        return None

    if section_result is None:
        return _single_locate(
            screenshot_base64=screenshot_base64,
            target_description=target_description,
            image_width=image_width,
            image_height=image_height,
            model_family=model_family,
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=remaining_timeout,
        )

    search_area = _expand_search_area(
        bbox=section_result.bbox,
        image_width=image_width,
        image_height=image_height,
        min_size=400,
    )
    cropped_base64, crop_offset = _crop_and_scale(screenshot_base64=screenshot_base64, area=search_area, scale=2)
    cropped_width = max(1, (search_area[2] - search_area[0]) * 2)
    cropped_height = max(1, (search_area[3] - search_area[1]) * 2)

    local_result = _single_locate(
        screenshot_base64=cropped_base64,
        target_description=target_description,
        image_width=cropped_width,
        image_height=cropped_height,
        model_family=model_family,
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=remaining_timeout,
    )
    if local_result is None:
        return None

    return AILocateResult(
        center=(
            int(local_result.center[0] / 2 + crop_offset[0]),
            int(local_result.center[1] / 2 + crop_offset[1]),
        ),
        bbox=(
            int(local_result.bbox[0] / 2 + crop_offset[0]),
            int(local_result.bbox[1] / 2 + crop_offset[1]),
            int(local_result.bbox[2] / 2 + crop_offset[0]),
            int(local_result.bbox[3] / 2 + crop_offset[1]),
        ),
        confidence=local_result.confidence,
        raw_response=local_result.raw_response,
    )


def _locate_section(
    *,
    screenshot_base64: str,
    target_description: str,
    image_width: int,
    image_height: int,
    model_family: ModelFamily,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
) -> AILocateResult | None:
    return _single_locate(
        screenshot_base64=screenshot_base64,
        target_description=target_description,
        image_width=image_width,
        image_height=image_height,
        model_family=model_family,
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        prompt_text=f"Find the section/area that contains: {target_description}",
        system_prompt=SECTION_SYSTEM_PROMPT,
    )


def _expand_search_area(
    *,
    bbox: tuple[int, int, int, int],
    image_width: int,
    image_height: int,
    min_size: int = 400,
) -> tuple[int, int, int, int]:
    xmin, ymin, xmax, ymax = bbox
    width = max(1, xmax - xmin)
    height = max(1, ymax - ymin)
    center_x = xmin + width / 2
    center_y = ymin + height / 2

    target_width = max(width, min_size)
    target_height = max(height, min_size)
    target_width = min(image_width, int(round(target_width * 1.2)))
    target_height = min(image_height, int(round(target_height * 1.2)))

    new_xmin = max(0, int(round(center_x - target_width / 2)))
    new_ymin = max(0, int(round(center_y - target_height / 2)))
    new_xmax = min(image_width, new_xmin + target_width)
    new_ymax = min(image_height, new_ymin + target_height)
    return (new_xmin, new_ymin, new_xmax, new_ymax)


def _crop_and_scale(
    *,
    screenshot_base64: str,
    area: tuple[int, int, int, int],
    scale: int = 2,
) -> tuple[str, tuple[int, int]]:
    from PIL import Image

    image = Image.open(BytesIO(_decode_base64_image(screenshot_base64)))
    cropped = image.crop(area)
    scaled = cropped.resize((max(1, cropped.width * scale), max(1, cropped.height * scale)))
    buffer = BytesIO()
    scaled.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8"), (area[0], area[1])


def _decode_base64_image(value: str) -> bytes:
    payload = value.split(",", 1)[1] if "," in value else value
    try:
        return base64.b64decode(payload, validate=True)
    except binascii.Error as exc:
        raise ValueError("Invalid base64 image payload.") from exc


def _call_vlm(
    *,
    screenshot_base64: str,
    prompt_text: str,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
    model_family: ModelFamily,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    return _call_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_base64}", "detail": "high"},
                    },
                    {"type": "text", "text": prompt_text},
                ],
            },
        ],
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        model_family=model_family,
    )


def _call_candidate_ranker(
    *,
    screenshot_base64: str,
    target_description: str,
    candidates: list[AIVisionCandidateBox],
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
) -> str:
    candidate_lines = [
        f"{candidate.index}: {candidate.label} @ {list(candidate.bbox)}"
        for candidate in candidates
    ]
    return _call_chat_completion(
        messages=[
            {"role": "system", "content": RANK_CANDIDATE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{screenshot_base64}", "detail": "high"},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Target: {target_description}\n"
                            "Choose the best candidate index from the list below.\n"
                            + "\n".join(candidate_lines)
                        ),
                    },
                ],
            },
        ],
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        model_family="glm" if model_family == "glm" else model_family,
    )


def _call_chat_completion(
    *,
    messages: list[dict[str, Any]],
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
    model_family: ModelFamily,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
    }
    if model_family == "glm":
        payload["thinking"] = {"type": "enabled"}
    else:
        payload["response_format"] = {"type": "json_object"}
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    http_request = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(http_request, timeout=timeout_seconds) as response:
        raw_payload = json.loads(response.read().decode("utf-8"))

    return (
        raw_payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )


def _parse_bbox_response(
    *,
    response_text: str,
    image_width: int,
    image_height: int,
    model_family: ModelFamily,
) -> AILocateResult | None:
    if not response_text:
        return None
    payload = _extract_json_payload(response_text)
    bbox = _extract_bbox(response_text)
    if bbox is None:
        logger.warning("AI visual locate response missing valid bbox: %s", response_text)
        return None
    normalized_bbox = _normalize_bbox(
        bbox=bbox,
        image_width=image_width,
        image_height=image_height,
        model_family=model_family,
    )
    xmin, ymin, xmax, ymax = normalized_bbox
    center = ((xmin + xmax) // 2, (ymin + ymax) // 2)
    errors = payload.get("errors") if isinstance(payload, dict) else []
    if not isinstance(errors, list):
        errors = []
    confidence = 0.35 if errors else 0.7
    return AILocateResult(
        center=center,
        bbox=normalized_bbox,
        confidence=confidence,
        raw_response=response_text,
    )


def _extract_bbox(response_text: str) -> list[Any] | None:
    payload = _extract_json_payload(response_text)
    if isinstance(payload, dict):
        bbox = payload.get("bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            return bbox

    match = re.search(
        r"\[\s*\[?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]?\s*\]",
        response_text,
    )
    if not match:
        return None
    return [float(match.group(index)) for index in range(1, 5)]


def _extract_json_payload(response_text: str) -> Any | None:
    try:
        return json.loads(_extract_json_object(response_text))
    except json.JSONDecodeError:
        return None


def _parse_candidate_index_response(
    *,
    response_text: str,
    candidate_indexes: set[int],
    model_family: ModelFamily,
) -> int | None:
    del model_family  # Reserved for future model-specific response quirks.
    if not response_text:
        return None
    try:
        payload = json.loads(_extract_json_object(response_text))
    except json.JSONDecodeError as exc:
        logger.warning("AI visual candidate rank returned invalid JSON: %s", exc)
        return None

    candidate_index = payload.get("candidate_index")
    if isinstance(candidate_index, str) and candidate_index.isdigit():
        candidate_index = int(candidate_index)
    if not isinstance(candidate_index, int):
        logger.warning("AI visual candidate rank missing valid candidate_index: %s", response_text)
        return None
    if candidate_index not in candidate_indexes:
        logger.warning("AI visual candidate rank returned unknown candidate_index=%s", candidate_index)
        return None
    return candidate_index


def _extract_json_object(response_text: str) -> str:
    stripped = response_text.strip()
    if not stripped:
        return stripped

    # Strip markdown code fences.
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            end_fence = stripped.rfind("```", first_newline)
            if end_fence > first_newline:
                stripped = stripped[first_newline + 1 : end_fence].strip()

    # Find the first brace-balanced JSON object.
    in_string = False
    escape_next = False
    depth = 0
    start = -1
    for i, ch in enumerate(stripped):
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"' and depth > 0:
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return stripped[start : i + 1]

    # Fallback: return original text for downstream JSON parsing/logging.
    return stripped


def _remaining_timeout_seconds(deadline: float) -> float | None:
    remaining = deadline - monotonic()
    if remaining <= 0:
        return None
    return remaining


def _compute_stage_timeout(*, deadline: float, requested_seconds: float) -> float | None:
    remaining = _remaining_timeout_seconds(deadline)
    if remaining is None:
        return None
    return min(remaining, max(MIN_STAGE_TIMEOUT_SECONDS, requested_seconds))


def _normalize_bbox(
    *,
    bbox: list[Any],
    image_width: int,
    image_height: int,
    model_family: ModelFamily,
) -> tuple[int, int, int, int]:
    numbers = [float(value) for value in bbox]
    if model_family == "gemini":
        ymin, xmin, ymax, xmax = numbers
        numbers = [xmin, ymin, xmax, ymax]
    if model_family not in {"qwen2.5-vl", "glm"}:
        xmin, ymin, xmax, ymax = numbers
        numbers = [
            xmin / 1000 * image_width,
            ymin / 1000 * image_height,
            xmax / 1000 * image_width,
            ymax / 1000 * image_height,
        ]

    xmin, ymin, xmax, ymax = numbers
    return (
        max(0, min(image_width, int(round(xmin)))),
        max(0, min(image_height, int(round(ymin)))),
        max(0, min(image_width, int(round(xmax)))),
        max(0, min(image_height, int(round(ymax)))),
    )


def _can_attempt_request(*, track_stats: bool = False) -> bool:
    settings = get_settings()
    now = monotonic()
    with _STATE_LOCK:
        _maybe_reset_window(now)
        if RUNTIME_STATE.opened_until > now:
            if track_stats:
                RUNTIME_STATS.breaker_skip_count += 1
            logger.warning(
                "AI visual locate breaker open until=%s",
                round(RUNTIME_STATE.opened_until, 2),
            )
            return False

        if (
            RUNTIME_STATE.window_started_at > 0
            and RUNTIME_STATE.window_request_count >= settings.ai_visual_rate_limit_per_minute
        ):
            if track_stats:
                RUNTIME_STATS.rate_limited_skip_count += 1
            logger.warning(
                "AI visual locate rate limited count=%s window_started_at=%s",
                RUNTIME_STATE.window_request_count,
                round(RUNTIME_STATE.window_started_at, 2),
            )
            return False

    return True


def _record_attempt() -> None:
    now = monotonic()
    with _STATE_LOCK:
        _maybe_reset_window(now)
        RUNTIME_STATE.window_request_count += 1


def _record_locate_request() -> None:
    with _STATE_LOCK:
        RUNTIME_STATS.locate_requests += 1


def _record_locate_result(*, success: bool, latency_ms: float) -> None:
    with _STATE_LOCK:
        if success:
            RUNTIME_STATS.locate_success_count += 1
        else:
            RUNTIME_STATS.locate_failure_count += 1
        RUNTIME_STATS.total_locate_latency_ms += latency_ms
        RUNTIME_STATS.max_locate_latency_ms = max(RUNTIME_STATS.max_locate_latency_ms, latency_ms)


def _record_disabled_skip() -> None:
    with _STATE_LOCK:
        RUNTIME_STATS.disabled_skip_count += 1


def record_ai_visual_cache_hit() -> None:
    with _STATE_LOCK:
        RUNTIME_STATS.cache_hit_count += 1


def record_ai_visual_cache_miss() -> None:
    with _STATE_LOCK:
        RUNTIME_STATS.cache_miss_count += 1


def record_ai_visual_cache_invalidation() -> None:
    with _STATE_LOCK:
        RUNTIME_STATS.cache_invalidated_count += 1


def get_ai_visual_runtime_stats() -> AIVisualRuntimeStatsSnapshot:
    with _STATE_LOCK:
        average_latency = (
            RUNTIME_STATS.total_locate_latency_ms / RUNTIME_STATS.locate_requests
            if RUNTIME_STATS.locate_requests
            else 0.0
        )
        return AIVisualRuntimeStatsSnapshot(
            locate_requests=RUNTIME_STATS.locate_requests,
            locate_success_count=RUNTIME_STATS.locate_success_count,
            locate_failure_count=RUNTIME_STATS.locate_failure_count,
            cache_hit_count=RUNTIME_STATS.cache_hit_count,
            cache_miss_count=RUNTIME_STATS.cache_miss_count,
            cache_invalidated_count=RUNTIME_STATS.cache_invalidated_count,
            breaker_skip_count=RUNTIME_STATS.breaker_skip_count,
            rate_limited_skip_count=RUNTIME_STATS.rate_limited_skip_count,
            disabled_skip_count=RUNTIME_STATS.disabled_skip_count,
            avg_locate_latency_ms=average_latency,
            max_locate_latency_ms=RUNTIME_STATS.max_locate_latency_ms,
        )


def _maybe_reset_window(now: float) -> None:
    if now - RUNTIME_STATE.window_started_at >= 60 or RUNTIME_STATE.window_started_at == 0:
        RUNTIME_STATE.window_started_at = now
        RUNTIME_STATE.window_request_count = 0


def _record_success() -> None:
    with _STATE_LOCK:
        RUNTIME_STATE.consecutive_failures = 0
        RUNTIME_STATE.opened_until = 0.0


def _record_failure() -> None:
    settings = get_settings()
    with _STATE_LOCK:
        RUNTIME_STATE.consecutive_failures += 1
        if RUNTIME_STATE.consecutive_failures >= settings.ai_visual_failure_threshold:
            RUNTIME_STATE.opened_until = monotonic() + settings.ai_visual_cooldown_seconds


def reset_ai_visual_runtime_state() -> None:
    with _STATE_LOCK:
        RUNTIME_STATE.window_started_at = 0.0
        RUNTIME_STATE.window_request_count = 0
        RUNTIME_STATE.consecutive_failures = 0
        RUNTIME_STATE.opened_until = 0.0
        RUNTIME_STATS.locate_requests = 0
        RUNTIME_STATS.locate_success_count = 0
        RUNTIME_STATS.locate_failure_count = 0
        RUNTIME_STATS.cache_hit_count = 0
        RUNTIME_STATS.cache_miss_count = 0
        RUNTIME_STATS.cache_invalidated_count = 0
        RUNTIME_STATS.breaker_skip_count = 0
        RUNTIME_STATS.rate_limited_skip_count = 0
        RUNTIME_STATS.disabled_skip_count = 0
        RUNTIME_STATS.total_locate_latency_ms = 0.0
        RUNTIME_STATS.max_locate_latency_ms = 0.0


def _elapsed_milliseconds(started_at: float) -> float:
    return max(0.0, (monotonic() - started_at) * 1000)


# ---------------------------------------------------------------------------
# VLM page layout annotation (used by planning phase)
# ---------------------------------------------------------------------------

_PAGE_ANNOTATION_SYSTEM_PROMPT = (
    "You are an AI assistant that describes the layout structure of a web page screenshot.\n"
    "Return a concise text description (not JSON) covering:\n"
    "1. Overall page layout (header, navigation, main content, sidebar, footer)\n"
    "2. Form sections and their purpose\n"
    "3. Key interactive regions (buttons, links, inputs)\n"
    "4. Any modal or overlay elements\n"
    "Keep the description under 200 words. Focus on spatial layout and element relationships, "
    "not individual element details."
)


def describe_page_layout(
    *,
    screenshot_base64: str,
    page_url: str,
) -> str | None:
    """Use VLM to describe page layout for planning context.

    This is separate from the element-locating pipeline and does NOT affect
    the circuit breaker or rate limiter used during execution.
    """
    settings = get_settings()
    if not settings.enable_vlm_page_annotation:
        return None
    if not settings.vlm_api_key or not settings.vlm_model:
        return None

    try:
        return _call_vlm(
            screenshot_base64=screenshot_base64,
            prompt_text=f"Describe the layout structure of this web page (URL: {page_url}).",
            api_key=settings.vlm_api_key,
            model=settings.vlm_model,
            base_url=settings.vlm_base_url,
            timeout_seconds=max(1.0, settings.ai_visual_timeout_ms / 1000),
            model_family=settings.vlm_model_family,
            system_prompt=_PAGE_ANNOTATION_SYSTEM_PROMPT,
        )
    except Exception as exc:
        logger.warning("VLM page annotation failed for %s: %s", page_url, exc)
        return None


__all__ = [
    "AILocateResult",
    "AIVisionCandidateBox",
    "AIVisualRuntimeStatsSnapshot",
    "ModelFamily",
    "describe_page_layout",
    "get_ai_visual_runtime_stats",
    "locate_element_by_vision",
    "record_ai_visual_cache_hit",
    "record_ai_visual_cache_invalidation",
    "record_ai_visual_cache_miss",
    "rank_candidates_by_vision",
    "reset_ai_visual_runtime_state",
]
