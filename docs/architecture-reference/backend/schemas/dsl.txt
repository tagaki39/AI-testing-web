"""Structured DSL schemas for runnable test cases."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DSLModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# Commonly recognized locator strategies.  The DSL generator may emit
# variant names (e.g. "css_selector" for "css", "href" as a heuristic).
# These sets are used for runtime normalization rather than compile-time
# rejection, so the pipeline stays robust against AI-generated names.
_KNOWN_STRATEGIES: set[str] = {
    "css", "css_selector",
    "xpath",
    "data-testid", "data_testid",
    "element_id", "elementId",
    "role", "role_fuzzy", "link_role", "link_role_fuzzy",
    "label", "label_fuzzy",
    "placeholder", "placeholder_fuzzy",
    "text", "text_fuzzy",
    "tag", "semantic", "vlm",
    "verified_role", "verified_role_fuzzy",
    "verified_css", "verified_xpath",
    "verified_placeholder", "verified_placeholder_fuzzy",
    "verified_label", "verified_label_fuzzy",
    "verified_text", "verified_element_id",
    "verified_name",
    # AI-generated variants
    "href", "link", "button", "aria",
    "id",
}

TargetStrategy = Literal[
    "css", "css_selector", "xpath", "data-testid", "data_testid",
    "element_id", "elementId", "tag",
    "role", "role_fuzzy", "link_role", "link_role_fuzzy",
    "label", "label_fuzzy", "placeholder", "placeholder_fuzzy",
    "text", "text_fuzzy", "semantic", "vlm",
    "verified_role", "verified_role_fuzzy", "verified_css", "verified_xpath",
    "verified_placeholder", "verified_placeholder_fuzzy",
    "verified_label", "verified_label_fuzzy",
    "verified_text", "verified_element_id", "verified_name",
    "href", "link", "button", "aria", "id",
]
LocatorConfidence = Literal["high", "medium", "low"]


# Strategy name normalization map: AI-generated variant -> canonical name.
_STRATEGY_NORMALIZE: dict[str, str] = {
    "css_selector": "css",
    "data_testid": "data-testid",
    "elementId": "element_id",
    "href": "css",
    "link": "role",
    "button": "role",
    "aria": "role",
    "id": "element_id",
    "name": "tag",
}


class LocatorCandidate(BaseModel):
    """Pre-scored candidate locator strategy for a DSL step."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    strategy: str = Field(description="Locator strategy name (normalized at runtime)")

    @field_validator("strategy", mode="before")
    @classmethod
    def _normalize_strategy(cls, v: str) -> str:
        return _STRATEGY_NORMALIZE.get(v, v)
    selector: str | None = Field(default=None, description="Explicit selector value (for css/xpath/data-testid/etc).")
    semantic_value: str | None = Field(default=None, description="Semantic value (role name, label text, etc).")
    pre_score: float = Field(ge=0.0, le=1.0, description="Generation-time pre-score 0.0-1.0.")
    pre_features: dict | None = Field(default=None, description="Pre-score feature breakdown for debugging.")


class Postcondition(BaseModel):
    """Post-action verification condition."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    type: Literal[
        "url_contains", "url_changes", "text_visible",
        "text_gone", "element_visible", "element_gone",
        "network_request", "dom_changed", "value_changed",
    ]
    value: str | None = Field(default=None, description="Expected value (URL fragment, text, selector).")
    timeout_ms: int = Field(default=3000, ge=100, le=30000)


class GotoStep(DSLModel):
    action: Literal["goto"]
    value: str = Field(min_length=1, description="Target URL or path.")


class ClickStep(DSLModel):
    action: Literal["click"]
    target: str = Field(min_length=1, description="Semantic or explicit locator.")
    page_state: str | None = Field(default=None, description="Page state this step belongs to (S0, S1, …).")
    target_strategy: TargetStrategy | None = Field(default=None, description="Locator strategy hint.")
    locator_confidence: LocatorConfidence | None = Field(
        default=None, description="AI-assessed locator confidence. low triggers VLM pre-verification.",
    )
    candidates: list[LocatorCandidate] = Field(default_factory=list, description="Pre-scored candidate locators.")
    postconditions: list[Postcondition] = Field(default_factory=list, description="Post-action verification conditions.")


class InputStep(DSLModel):
    action: Literal["input"]
    target: str = Field(min_length=1, description="Semantic or explicit locator.")
    value: str = Field(description="Input text.")
    trigger: str | None = Field(default=None, description="Key to press after input (e.g. Enter for quantity/search fields).")
    page_state: str | None = Field(default=None, description="Page state this step belongs to (S0, S1, …).")
    target_strategy: TargetStrategy | None = Field(default=None, description="Locator strategy hint.")
    locator_confidence: LocatorConfidence | None = Field(
        default=None, description="AI-assessed locator confidence. low triggers VLM pre-verification.",
    )
    candidates: list[LocatorCandidate] = Field(default_factory=list, description="Pre-scored candidate locators.")
    postconditions: list[Postcondition] = Field(default_factory=list, description="Post-action verification conditions.")


class WaitForStep(DSLModel):
    action: Literal["wait_for"]
    target: str = Field(min_length=1, description="Target to wait for.")
    timeout_ms: int = Field(default=5000, ge=1, le=60000)
    page_state: str | None = Field(default=None, description="Page state this step belongs to (S0, S1, …).")
    target_strategy: TargetStrategy | None = Field(default=None, description="Locator strategy hint.")
    locator_confidence: LocatorConfidence | None = Field(
        default=None, description="AI-assessed locator confidence. low triggers VLM pre-verification.",
    )
    candidates: list[LocatorCandidate] = Field(default_factory=list, description="Pre-scored candidate locators.")
    postconditions: list[Postcondition] = Field(default_factory=list, description="Post-action verification conditions.")


class AssertTextStep(DSLModel):
    action: Literal["assert_text"]
    target: str = Field(min_length=1, description="Target to assert against.")
    value: str = Field(min_length=1, description="Expected text.")
    page_state: str | None = Field(default=None, description="Page state this step belongs to (S0, S1, …).")
    target_strategy: TargetStrategy | None = Field(default=None, description="Locator strategy hint.")
    locator_confidence: LocatorConfidence | None = Field(
        default=None, description="AI-assessed locator confidence. low triggers VLM pre-verification.",
    )
    candidates: list[LocatorCandidate] = Field(default_factory=list, description="Pre-scored candidate locators.")
    postconditions: list[Postcondition] = Field(default_factory=list, description="Post-action verification conditions.")


class AssertUrlContainsStep(DSLModel):
    action: Literal["assert_url_contains"]
    value: str = Field(min_length=1, description="Expected URL fragment.")


class CaptureTextStep(DSLModel):
    action: Literal["capture_text"]
    target: str = Field(min_length=1, description="Element to capture text from.")
    context_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Runtime variable name to store the captured text.",
    )
    page_state: str | None = Field(default=None, description="Page state this step belongs to (S0, S1, …).")
    target_strategy: TargetStrategy | None = Field(default=None, description="Locator strategy hint.")
    locator_confidence: LocatorConfidence | None = Field(
        default=None, description="AI-assessed locator confidence. low triggers VLM pre-verification.",
    )
    candidates: list[LocatorCandidate] = Field(default_factory=list, description="Pre-scored candidate locators.")
    postconditions: list[Postcondition] = Field(default_factory=list, description="Post-action verification conditions.")


DSLVariableType = Literal["string", "number", "boolean", "object", "array"]
DSLVariableSource = Literal[
    "latest_url",
    "error_message",
    "status",
    "last_step_url",
    "last_step_page_title",
    "last_step_target",
    "last_step_value",
    "last_step_error_message",
]


class DSLCaseInputContract(DSLModel):
    name: str = Field(min_length=1, max_length=100)
    context_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value_type: DSLVariableType
    required: bool = True
    description: str | None = Field(default=None, max_length=500)
    value: str | None = Field(default=None, description="Default value for this variable.")


class DSLCaseOutputContract(DSLModel):
    name: str = Field(min_length=1, max_length=100)
    context_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value_type: DSLVariableType
    source: DSLVariableSource | None = None
    description: str | None = Field(default=None, max_length=500)


DSLStep = Annotated[
    GotoStep
    | ClickStep
    | InputStep
    | WaitForStep
    | AssertTextStep
    | AssertUrlContainsStep
    | CaptureTextStep,
    Field(discriminator="action"),
]


class DSLCase(DSLModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    input_contract: list[DSLCaseInputContract] = Field(default_factory=list)
    output_contract: list[DSLCaseOutputContract] = Field(default_factory=list)
    steps: list[DSLStep] = Field(min_length=1)


GenerateDslMode = Literal["draft", "strict_steps_only"]
GenerateDslImportMode = Literal["replace", "steps_only", "contracts_only"]
GenerateDslBaseUrlSource = Literal["ai_output", "request", "current_case", "none"]
DslGenerationRunStatus = Literal["success", "failed"]
DslGenerationFeedbackStatus = Literal["pending", "accepted", "rejected"]
DslGenerationFeedbackDecision = Literal["accepted", "rejected"]
DslGenerationPromptVariant = Literal["baseline_draft", "rewrite_from_case", "repair_steps", "contracts_focus"]
DslGenerationContextProfile = Literal["blank_request", "rewrite_from_case", "repair_steps", "contracts_focus"]
DslGenerationRiskFlag = Literal[
    "missing_name_fallback",
    "base_url_backfilled",
    "invalid_actions_repaired",
    "invalid_steps_removed",
    "invalid_contracts_removed",
    "contracts_preserved_fallback",
]
DslGenerationRejectionReasonCode = Literal[
    "wrong_actions",
    "invalid_structure",
    "context_mismatch",
    "bad_contracts",
    "other",
]


class GenerateDslRequest(DSLModel):
    prompt: str = Field(min_length=1, max_length=50000)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    actor_user_id: int = Field(ge=1)
    project_id: int | None = Field(default=None, ge=1)
    case_id: int | None = Field(default=None, ge=1)
    generation_mode: GenerateDslMode | None = None
    import_mode: GenerateDslImportMode = "replace"
    current_case: DSLCase | None = None
    current_steps: list[DSLStep] | None = None
    current_input_contract: list[DSLCaseInputContract] | None = None
    current_output_contract: list[DSLCaseOutputContract] | None = None
    retry_from_generation_id: int | None = Field(default=None, ge=1)
    retry_reason_code: DslGenerationRejectionReasonCode | None = None
    retry_note: str | None = Field(default=None, max_length=1000)
    preserve_contracts: bool = False
    page_elements: str | None = Field(default=None, description="Formatted DOM elements for grounding DSL generation.")
    flow_steps: list[dict[str, Any]] | None = Field(default=None, description="Structured flow steps for step-level element filtering and segmented DSL generation.")
    a11y_nodes_by_state: dict[str, list[dict[str, Any]]] | None = Field(
        default=None,
        description="A11y nodes grouped by page_state, used as element context when flow_steps is empty (Bug A fallback).",
    )
    scenario_variables: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Naming authority for cross-segment variables. Each entry has shape "
            "{context_key, description, source ('input'|'captured'), capture_in_state}. "
            "All segments receive the same list so generated capture_text/assert_text "
            "agree on ${context_key} spelling. Bypassing this risks unresolved ${} at runtime."
        ),
    )
    user_context: str | None = Field(
        default=None,
        max_length=5000,
        description=(
            "Original user requirements summary injected into every segment prompt "
            "so the DSL generator sees the full business intent, not just the "
            "planning agent's draft_prompt derivative."
        ),
    )

    @model_validator(mode="after")
    def validate_retry_context(self) -> "GenerateDslRequest":
        if self.retry_from_generation_id is not None and self.retry_reason_code is None:
            raise ValueError("retry_from_generation_id 存在时必须提供 retry_reason_code。")
        return self


class DSLValidationResult(DSLModel):
    valid: bool = True
    case: DSLCase
    supported_actions: list[str]


class GenerateDslMeta(DSLModel):
    model: str | None = Field(default=None, max_length=200)
    generation_mode: GenerateDslMode
    import_mode: GenerateDslImportMode
    prompt_variant: DslGenerationPromptVariant
    context_profile: DslGenerationContextProfile
    active_governance_focus_reasons: list[DslGenerationRejectionReasonCode] = Field(default_factory=list)
    risk_flags: list[DslGenerationRiskFlag] = Field(default_factory=list)
    base_url_source: GenerateDslBaseUrlSource
    base_url_backfilled: bool = False
    repaired_invalid_actions: int = Field(default=0, ge=0)
    removed_invalid_steps: int = Field(default=0, ge=0)
    removed_invalid_contracts: int = Field(default=0, ge=0)
    preserve_contracts_applied: bool = False
    used_current_case_context: bool = False
    used_current_steps_context: bool = False


class GenerateDslResponse(DSLModel):
    generation_id: int = Field(ge=1)
    case: DSLCase
    supported_actions: list[str]
    warnings: list[str] = Field(default_factory=list)
    normalization_notes: list[str] = Field(default_factory=list)
    generation_meta: GenerateDslMeta


class DslGenerationFeedbackRequest(DSLModel):
    actor_user_id: int = Field(ge=1)
    feedback_status: DslGenerationFeedbackDecision
    feedback_import_mode: GenerateDslImportMode | None = None
    rejection_reason_code: DslGenerationRejectionReasonCode | None = None
    feedback_note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_feedback_import_mode(self) -> "DslGenerationFeedbackRequest":
        if self.feedback_status == "accepted" and self.feedback_import_mode is None:
            raise ValueError("accepted 反馈必须提供 feedback_import_mode。")
        if self.feedback_status == "rejected" and self.feedback_import_mode is not None:
            raise ValueError("rejected 反馈不能提供 feedback_import_mode。")
        if self.feedback_status == "rejected" and self.rejection_reason_code is None:
            raise ValueError("rejected 反馈必须提供 rejection_reason_code。")
        if self.feedback_status == "accepted" and self.rejection_reason_code is not None:
            raise ValueError("accepted 反馈不能提供 rejection_reason_code。")
        return self


class StoredDslGenerationRunSummary(DSLModel):
    id: int = Field(ge=1)
    created_at: datetime
    success: bool
    model_name: str | None = Field(default=None, max_length=200)
    generation_mode: GenerateDslMode
    import_mode: GenerateDslImportMode
    prompt_variant: DslGenerationPromptVariant
    project_id: int | None = Field(default=None, ge=1)
    case_id: int | None = Field(default=None, ge=1)
    prompt_version: str = Field(min_length=1, max_length=100)
    retry_from_generation_id: int | None = Field(default=None, ge=1)
    retry_reason_code: DslGenerationRejectionReasonCode | None = None
    retry_note: str | None = Field(default=None, max_length=1000)
    error_type: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=2000)
    repaired_invalid_actions: int = Field(ge=0)
    removed_invalid_steps: int = Field(ge=0)
    removed_invalid_contracts: int = Field(ge=0)
    warnings_count: int = Field(ge=0)
    normalization_notes_count: int = Field(ge=0)
    prompt_preview: str = Field(min_length=1, max_length=200)
    governance_focus_reasons: list[DslGenerationRejectionReasonCode] = Field(default_factory=list)
    risk_flags: list[DslGenerationRiskFlag] = Field(default_factory=list)
    feedback_status: DslGenerationFeedbackStatus
    feedback_import_mode: GenerateDslImportMode | None = None
    rejection_reason_code: DslGenerationRejectionReasonCode | None = None
    feedback_recorded_at: datetime | None = None


class StoredDslGenerationRunDetail(StoredDslGenerationRunSummary):
    request_base_url: str | None = Field(default=None, max_length=500)
    generated_case_json: DSLCase | None = None
    warnings_json: list[str] = Field(default_factory=list)
    normalization_notes_json: list[str] = Field(default_factory=list)
    feedback_note: str | None = Field(default=None, max_length=1000)
    context_profile: DslGenerationContextProfile
    used_current_case_context: bool = False
    used_current_steps_context: bool = False
    preserve_contracts_requested: bool = False
    preserve_contracts_applied: bool = False
