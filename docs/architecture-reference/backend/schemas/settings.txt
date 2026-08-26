"""Schemas for runtime-manageable AI settings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


VLMModelFamily = Literal["qwen-vl", "gemini", "gpt-4o", "qwen2.5-vl", "glm"]


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AISettingsResponse(SettingsModel):
    enable_ai_dsl_generate: bool
    ai_dsl_timeout_ms: int = Field(ge=1000)
    ai_dsl_base_url: str = Field(min_length=1, max_length=500)
    ai_dsl_model: str | None = Field(default=None, max_length=200)
    ai_dsl_strict_mode: bool
    ai_dsl_allow_auto_repair: bool
    has_ai_dsl_api_key: bool
    enable_ai_visual_locate: bool
    ai_visual_timeout_ms: int = Field(ge=1000)
    ai_visual_failure_threshold: int = Field(ge=1)
    ai_visual_cooldown_seconds: int = Field(ge=1)
    ai_visual_rate_limit_per_minute: int = Field(ge=1)
    vlm_base_url: str = Field(min_length=1, max_length=500)
    vlm_model: str | None = Field(default=None, max_length=200)
    vlm_model_family: VLMModelFamily
    has_vlm_api_key: bool
    enable_ai_planning: bool
    ai_planning_model: str | None = Field(default=None, max_length=200)
    ai_planning_base_url: str = Field(min_length=1, max_length=500)
    ai_planning_timeout_ms: int = Field(ge=1000)
    has_ai_planning_api_key: bool


class AISettingsUpdateRequest(SettingsModel):
    enable_ai_dsl_generate: bool
    ai_dsl_timeout_ms: int = Field(ge=1000)
    ai_dsl_base_url: str = Field(min_length=1, max_length=500)
    ai_dsl_model: str | None = Field(default=None, max_length=200)
    ai_dsl_strict_mode: bool = False
    ai_dsl_allow_auto_repair: bool = True
    ai_dsl_api_key: str | None = Field(default=None, max_length=1000)
    clear_ai_dsl_api_key: bool = False
    enable_ai_visual_locate: bool
    ai_visual_timeout_ms: int = Field(ge=1000)
    ai_visual_failure_threshold: int = Field(ge=1)
    ai_visual_cooldown_seconds: int = Field(ge=1)
    ai_visual_rate_limit_per_minute: int = Field(ge=1)
    vlm_base_url: str = Field(min_length=1, max_length=500)
    vlm_model: str | None = Field(default=None, max_length=200)
    vlm_model_family: VLMModelFamily
    vlm_api_key: str | None = Field(default=None, max_length=1000)
    clear_vlm_api_key: bool = False
    enable_ai_planning: bool
    ai_planning_model: str | None = Field(default=None, max_length=200)
    ai_planning_base_url: str = Field(min_length=1, max_length=500)
    ai_planning_timeout_ms: int = Field(ge=1000)
    ai_planning_api_key: str | None = Field(default=None, max_length=1000)
    clear_ai_planning_api_key: bool = False

    @model_validator(mode="after")
    def validate_secret_update_flags(self) -> "AISettingsUpdateRequest":
        if self.clear_ai_dsl_api_key and self.ai_dsl_api_key:
            raise ValueError("ai_dsl_api_key 不能和 clear_ai_dsl_api_key 同时提交。")
        if self.clear_vlm_api_key and self.vlm_api_key:
            raise ValueError("vlm_api_key 不能和 clear_vlm_api_key 同时提交。")
        if self.clear_ai_planning_api_key and self.ai_planning_api_key:
            raise ValueError("ai_planning_api_key 不能和 clear_ai_planning_api_key 同时提交。")
        return self


class AIDslGenerationErrorTypeCount(SettingsModel):
    error_type: str = Field(min_length=1, max_length=200)
    count: int = Field(ge=1)


class AIDslGenerationImportModeCount(SettingsModel):
    import_mode: Literal["replace", "steps_only", "contracts_only"]
    count: int = Field(ge=1)


class AIDslGenerationRejectionReasonCount(SettingsModel):
    rejection_reason_code: Literal["wrong_actions", "invalid_structure", "context_mismatch", "bad_contracts", "other"]
    count: int = Field(ge=1)


class AIDslGenerationPromptVariantBreakdown(SettingsModel):
    prompt_variant: Literal["baseline_draft", "rewrite_from_case", "repair_steps", "contracts_focus"]
    total_requests: int = Field(ge=1)
    success_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)


class AIDslGenerationPromptVersionBreakdown(SettingsModel):
    prompt_version: str = Field(min_length=1, max_length=100)
    total_requests: int = Field(ge=1)
    success_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    retry_requests: int = Field(ge=0)
    retry_accepted_count: int = Field(ge=0)


class AIDslGenerationContextProfileBreakdown(SettingsModel):
    context_profile: Literal["blank_request", "rewrite_from_case", "repair_steps", "contracts_focus"]
    total_requests: int = Field(ge=1)
    success_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)


class AIDslGenerationRejectionReasonByVariant(SettingsModel):
    prompt_variant: Literal["baseline_draft", "rewrite_from_case", "repair_steps", "contracts_focus"]
    rejection_reason_code: Literal["wrong_actions", "invalid_structure", "context_mismatch", "bad_contracts", "other"]
    count: int = Field(ge=1)


class AIDslGenerationModelOutcome(SettingsModel):
    model_name: str | None = Field(default=None, max_length=200)
    total_requests: int = Field(ge=1)
    success_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)


class AIDslGenerationModeBreakdown(SettingsModel):
    generation_mode: Literal["draft", "strict_steps_only"]
    total_requests: int = Field(ge=1)
    success_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)


class AIDslGenerationRetryAcceptanceByReason(SettingsModel):
    rejection_reason_code: Literal["wrong_actions", "invalid_structure", "context_mismatch", "bad_contracts", "other"]
    retry_requests: int = Field(ge=1)
    accepted_count: int = Field(ge=0)
    acceptance_rate: float = Field(ge=0, le=1)


class AIDslGenerationGovernanceFocusSummary(SettingsModel):
    rejection_reason_code: Literal["wrong_actions", "invalid_structure", "context_mismatch", "bad_contracts", "other"]
    rejected_count: int = Field(default=0, ge=0)
    affected_prompt_variants: int = Field(default=0, ge=0)
    retry_requests: int = Field(default=0, ge=0)
    retry_accepted_count: int = Field(default=0, ge=0)
    retry_acceptance_rate: float = Field(default=0.0, ge=0, le=1)


class AIDslGenerationStats(SettingsModel):
    current_prompt_version: str = Field(min_length=1, max_length=100)
    current_governance_focus_reasons: list[
        Literal["wrong_actions", "invalid_structure", "context_mismatch", "bad_contracts", "other"]
    ] = Field(default_factory=list)
    prompt_version_observation_note: str = Field(min_length=1, max_length=200)
    governance_focus_selection_note: str = Field(min_length=1, max_length=300)
    total_requests: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    accepted_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    pending_count: int = Field(default=0, ge=0)
    decision_coverage_rate: float = Field(default=0.0, ge=0, le=1)
    last_model: str | None = Field(default=None, max_length=200)
    last_error_type: str | None = Field(default=None, max_length=200)
    last_error_message: str | None = Field(default=None, max_length=2000)
    last_24h_requests: int = Field(default=0, ge=0)
    last_24h_success_count: int = Field(default=0, ge=0)
    last_24h_failure_count: int = Field(default=0, ge=0)
    last_24h_auto_repair_rate: float = Field(default=0.0, ge=0, le=1)
    retry_requests: int = Field(default=0, ge=0)
    retry_accepted_count: int = Field(default=0, ge=0)
    retry_rejected_count: int = Field(default=0, ge=0)
    top_error_types: list[AIDslGenerationErrorTypeCount] = Field(default_factory=list)
    accepted_import_mode_breakdown: list[AIDslGenerationImportModeCount] = Field(default_factory=list)
    top_rejection_reasons: list[AIDslGenerationRejectionReasonCount] = Field(default_factory=list)
    prompt_variant_breakdown: list[AIDslGenerationPromptVariantBreakdown] = Field(default_factory=list)
    prompt_version_breakdown: list[AIDslGenerationPromptVersionBreakdown] = Field(default_factory=list)
    context_profile_breakdown: list[AIDslGenerationContextProfileBreakdown] = Field(default_factory=list)
    rejection_reason_by_variant: list[AIDslGenerationRejectionReasonByVariant] = Field(default_factory=list)
    model_outcome_breakdown: list[AIDslGenerationModelOutcome] = Field(default_factory=list)
    generation_mode_breakdown: list[AIDslGenerationModeBreakdown] = Field(default_factory=list)
    retry_acceptance_by_reason: list[AIDslGenerationRetryAcceptanceByReason] = Field(default_factory=list)
    current_governance_focus_breakdown: list[AIDslGenerationGovernanceFocusSummary] = Field(default_factory=list)


class AIVisualStats(SettingsModel):
    locate_requests: int = Field(default=0, ge=0)
    locate_success_count: int = Field(default=0, ge=0)
    locate_failure_count: int = Field(default=0, ge=0)
    cache_hit_count: int = Field(default=0, ge=0)
    cache_miss_count: int = Field(default=0, ge=0)
    cache_invalidated_count: int = Field(default=0, ge=0)
    breaker_skip_count: int = Field(default=0, ge=0)
    rate_limited_skip_count: int = Field(default=0, ge=0)
    disabled_skip_count: int = Field(default=0, ge=0)
    avg_locate_latency_ms: float = Field(default=0.0, ge=0)
    max_locate_latency_ms: float = Field(default=0.0, ge=0)


class AISettingsOverviewResponse(SettingsModel):
    ai_dsl_enabled: bool
    ai_dsl_model: str | None = Field(default=None, max_length=200)
    ai_dsl_strict_mode: bool
    ai_dsl_allow_auto_repair: bool
    generation_stats: AIDslGenerationStats
    ai_visual_stats: AIVisualStats
