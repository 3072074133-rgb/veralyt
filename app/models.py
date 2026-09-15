from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(StrEnum):
    INGESTING = "ingesting"
    READY = "ready"
    CLASSIFYING = "classifying"
    OFF_TOPIC = "off_topic"
    PLANNING = "planning"
    NEEDS_CLARIFICATION = "needs_clarification"
    EXECUTING = "executing"
    VALIDATING = "validating"
    REFLECTING = "reflecting"
    NEEDS_REVIEW = "needs_review"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ModelSettings(StrictModel):
    mode: Literal["ollama", "openai_compatible"] = "ollama"
    provider: str = Field(default="ollama", min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", max_length=4096)
    base_url: str = Field(min_length=1, max_length=1000)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=3072, ge=128, le=65536)
    context_window: int = Field(default=32768, ge=512, le=1048576)


class ModelSettingsPublic(ModelSettings):
    api_key_configured: bool = False


class ModelSettingsUpdate(StrictModel):
    mode: Literal["ollama", "openai_compatible"] | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=60)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    base_url: str | None = Field(default=None, min_length=1, max_length=1000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=128, le=65536)
    context_window: int | None = Field(default=None, ge=512, le=1048576)


class ModelConnectionResult(StrictModel):
    success: bool
    provider: str
    model: str
    message: str
    latency_ms: int


class ModelSettingsEnvelope(StrictModel):
    llm: ModelSettingsPublic


class ModelSettingsRequest(StrictModel):
    llm: ModelSettingsUpdate


class UploadedFile(StrictModel):
    id: str
    original_name: str
    size: int
    status: Literal["ready", "failed"]
    sheet_count: int = 0
    detected_sheet_count: int = 0
    skipped_sheet_count: int = 0
    row_count: int = 0
    error: str | None = None


class DatasetColumn(StrictModel):
    name: str
    display_name: str
    data_type: str
    null_count: int
    sample_values: list[Any] = Field(default_factory=list)
    semantic_type: Literal[
        "id", "name", "date", "amount", "percentage", "category", "metric", "unknown"
    ] = "unknown"
    role: Literal["dimension", "measure", "identifier", "unknown"] = "unknown"
    unit: str | None = None
    currency: str | None = None
    default_aggregation: Literal["sum", "average", "count", "none"] = "none"
    semantic_confidence: float = Field(default=0, ge=0, le=1)


class DatasetRegion(StrictModel):
    sheet_name: str
    region_index: int = Field(ge=1)
    source_range: str
    header_start_row: int = Field(ge=1)
    header_end_row: int = Field(ge=1)
    data_start_row: int = Field(ge=1)
    data_end_row: int = Field(ge=1)


class DatasetInfo(StrictModel):
    id: str
    file_id: str
    table_name: str
    display_name: str
    row_count: int
    columns: list[DatasetColumn]
    source_region: DatasetRegion | None = None


class DatasetRelationship(StrictModel):
    left_dataset_id: str
    left_dataset: str = ""
    left_field: str
    right_dataset_id: str
    right_dataset: str = ""
    right_field: str
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""
    status: Literal["candidate", "confirmed", "rejected"] = "candidate"


class RelationshipConfirmationRequest(StrictModel):
    relationships: list[DatasetRelationship] = Field(default_factory=list, max_length=50)


class DatasetAsset(StrictModel):
    id: str
    name: str
    description: str = ""
    status: Literal["active", "archived"] = "active"
    owner_id: str = "local"
    latest_revision: int = 1
    created_at: str
    updated_at: str


class DatasetRevision(StrictModel):
    id: str
    dataset_id: str
    revision_number: int = Field(ge=1)
    parent_revision_id: str | None = None
    source_type: Literal["upload", "correction", "migration"]
    status: Literal["published", "archived"] = "published"
    content_hash: str
    change_summary: str
    tables: list[DatasetInfo] = Field(default_factory=list)
    created_at: str


class DatasetAssetDetail(DatasetAsset):
    revisions: list[DatasetRevision] = Field(default_factory=list)
    permission: Literal["owner", "editor", "viewer"] = "owner"


class DatasetAssetList(StrictModel):
    items: list[DatasetAsset]
    total: int


class DatasetPreview(StrictModel):
    dataset_id: str
    display_name: str
    columns: list[DatasetColumn]
    rows: list[dict[str, Any]]
    page: int
    page_size: int
    total: int
    data_revision: int


class DatasetColumnProfile(StrictModel):
    name: str
    display_name: str
    data_type: str
    null_count: int
    null_ratio: float
    distinct_count: int
    minimum: Any | None = None
    maximum: Any | None = None
    sample_values: list[Any] = Field(default_factory=list)


class DatasetProfile(StrictModel):
    dataset_id: str
    row_count: int
    duplicate_count: int
    columns: list[DatasetColumnProfile]
    data_revision: int


class CellCorrection(StrictModel):
    row_id: int = Field(ge=1)
    column: str
    value: Any | None = None


class ColumnMetadataCorrection(StrictModel):
    column: str
    display_name: str | None = None
    semantic_type: Literal[
        "id", "name", "date", "amount", "percentage", "category", "metric", "unknown"
    ] | None = None
    role: Literal["dimension", "measure", "identifier", "unknown"] | None = None
    unit: str | None = None
    currency: str | None = None
    default_aggregation: Literal["sum", "average", "count", "none"] | None = None


class DatasetCorrectionRequest(StrictModel):
    expected_data_revision: int = Field(ge=0)
    cell_updates: list[CellCorrection] = Field(default_factory=list, max_length=500)
    metadata_updates: list[ColumnMetadataCorrection] = Field(default_factory=list, max_length=100)
    deleted_row_ids: list[int] = Field(default_factory=list, max_length=500)
    added_rows: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    change_summary: str = Field(default="数据纠错", min_length=1, max_length=200)


class DatasetCorrectionResult(StrictModel):
    dataset: DatasetInfo
    data_revision: int
    dataset_asset_id: str
    dataset_revision_id: str
    revision_number: int


class IntentDecision(StrictModel):
    delivery: Literal['answer', 'report', 'clarify'] = 'answer'
    data_action: Literal['reuse', 'query'] = 'reuse'
    route: Literal['analysis', 'clarification', 'conversation', 'explanation']
    reason: str = ''
    reply: str | None

    @model_validator(mode='after')
    def validate_route_fields(self):
        if self.route in {'clarification', 'conversation', 'explanation'} and not (self.reply or '').strip():
            raise ValueError(f'{self.route} requires a nonempty reply answering the user')
        return self


class PlanStep(StrictModel):
    id: str
    purpose: str
    dataset_ids: list[str] = Field(min_length=1)
    sql: str = Field(min_length=1)


class PlanDecision(StrictModel):
    """Minimal planning decision returned by the analysis planner."""
    action: Literal["analyze", "clarify"]
    goal: str = Field(min_length=1)
    clarification: str | None
    clarification_options: list[str]
    steps: list[PlanStep]

    @model_validator(mode="after")
    def validate_clarification(self):
        if self.action == "clarify" and not (self.clarification or "").strip():
            raise ValueError("clarify requires a nonempty clarification")
        return self


class EvidencePointer(StrictModel):
    citation_id: str | None = None
    evidence_id: str = ""
    row_index: int = Field(default=0, ge=0)
    field: str = ""
    raw_value: str = ""
    unit: str | None = None
    # ``cell`` points at an original value; ``derived`` points at a value
    # calculated from the listed input pointers.  Keeping this metadata in
    # the result makes derived claims auditable without adding another table.
    source_type: Literal["cell", "derived"] = "cell"
    formula: str | None = None
    input_pointers: list["EvidencePointer"] = Field(default_factory=list)


class AnalysisPlan(StrictModel):
    goal: str
    can_execute: bool
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    time_basis: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    clarification_question: str | None = None
    clarification_options: list[str] = Field(default_factory=list)


class ToolExecutionResult(StrictModel):
    result_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "error"]
    summary: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    execution_metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Metric(StrictModel):
    label: str
    value: str
    change: str | None = None
    direction: Literal["up", "down", "flat", "neutral"] = "neutral"
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_pointers: list[EvidencePointer] = Field(default_factory=list)


class Finding(StrictModel):
    title: str = "分析发现"
    detail: str
    severity: Severity = Severity.INFO
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_pointers: list[EvidencePointer] = Field(default_factory=list)


class ChartSeries(StrictModel):
    name: str
    field: str


class ChartSpec(StrictModel):
    id: str
    title: str = "分析图表"
    chart_type: Literal["line", "bar", "stacked_bar", "pie", "waterfall", "scatter", "table"]
    dataset_ref: str
    category_field: str
    series: list[ChartSeries]
    unit: str | None = None
    orientation: Literal["vertical", "horizontal"] = "vertical"
    sort_order: Literal["source", "category_asc", "value_desc"] = "source"
    max_items: int = Field(default=20, ge=1, le=200)
    x_field: str | None = None
    y_field: str | None = None
    label_field: str | None = None


class CalculationDetail(StrictModel):
    id: str
    tool_name: str
    title: str
    status: Literal["success", "error"]
    row_count: int = 0
    evidence_refs: list[str] = Field(default_factory=list)
    query: str | None = None
    warnings: list[str] = Field(default_factory=list)


class Insight(StrictModel):
    id: str
    type: Literal[
        "ratio", "change", "comparison", "ranking",
        "contribution", "anomaly", "risk", "reconciliation",
    ]
    title: str
    conclusion: str
    significance: str
    action: str | None = None
    value: str | None = None
    unit: str | None = None
    severity: Severity = Severity.INFO
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_pointers: list[EvidencePointer] = Field(default_factory=list)
    formula: str | None = None
    input_labels: list[str] = Field(default_factory=list)


class DeliveryCheck(StrictModel):
    code: str
    label: str
    passed: bool
    severity: Severity
    message: str


class DeliveryGate(StrictModel):
    status: Literal[
        "completed", "completed_with_warnings", "needs_clarification",
        "needs_review", "failed", "cancelled",
    ]
    checks: list[DeliveryCheck] = Field(default_factory=list)
    new_information_count: int = Field(default=0, ge=0)
    unresolved_error_count: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0)
    issues: list[dict[str, Any]] = Field(default_factory=list)


class ReportText(StrictModel):
    text: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_pointers: list[EvidencePointer] = Field(default_factory=list)


class ReportBlock(StrictModel):
    id: str = Field(min_length=1)
    kind: Literal["paragraph", "list", "metrics", "table", "chart"]
    text: str = ""
    items: list[ReportText] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    chart: ChartSpec | None = None
    dataset_ref: str | None = None
    columns: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_pointers: list[EvidencePointer] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_content(self):
        if self.kind == "paragraph" and not self.text.strip():
            raise ValueError("paragraph requires text")
        if self.kind == "list" and not self.items:
            raise ValueError("list requires items")
        if self.kind == "metrics" and not self.metrics:
            raise ValueError("metrics requires metrics")
        if self.kind == "chart" and self.chart is None:
            raise ValueError("chart requires chart specification")
        if self.kind == "table" and not self.dataset_ref:
            raise ValueError("table requires persisted dataset_ref")
        allowed = {"paragraph": {"text"}, "list": {"items"}, "metrics": {"metrics"},
                   "chart": {"chart"}, "table": {"dataset_ref", "columns"}}[self.kind]
        for field in {"text", "items", "metrics", "chart", "dataset_ref", "columns"} - allowed:
            if getattr(self, field):
                raise ValueError(f"{self.kind} does not render {field}; use a separate block")
        return self


class ReportSection(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    blocks: list[ReportBlock] = Field(min_length=1)


class OutlineSection(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    purpose: str


class ReportOutline(StrictModel):
    sections: list[OutlineSection] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({section.id for section in self.sections}) != len(self.sections):
            raise ValueError("outline section IDs must be unique")
        return self


class AnalysisDraft(StrictModel):
    report_schema_version: Literal[1, 2] = 1
    sections: list[ReportSection] = Field(default_factory=list)
    title: str = "数据分析结果"
    summary: str
    summary_evidence_refs: list[str] = Field(default_factory=list)
    summary_evidence_pointers: list[EvidencePointer] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    charts: list[ChartSpec] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)
    calculation_details: list[CalculationDetail] = Field(default_factory=list)
    insights: list[Insight] = Field(default_factory=list)
    delivery: DeliveryGate | None = None
    verification_level: Literal["legacy", "evidence", "cell"] = "evidence"

    @model_validator(mode="after")
    def valid_sections(self):
        if self.report_schema_version == 2:
            if not self.sections:
                raise ValueError("v2 report requires sections")
            if self.metrics or self.findings or self.insights or self.charts:
                raise ValueError("v2 body belongs exclusively in sections")
            ids = [section.id for section in self.sections]
            ids += [block.id for section in self.sections for block in section.blocks]
            if len(set(ids)) != len(ids):
                raise ValueError("section and block IDs must be unique")
        elif self.sections:
            raise ValueError("sections require report_schema_version=2")
        return self


class GeneratedAnalysisDraft(AnalysisDraft):
    """Draft returned directly by the writing model."""


class ChapterAnalysisDraft(AnalysisDraft):
    """New reports must use the chapter protocol; v1 remains readable."""
    report_schema_version: Literal[2] = 2
    sections: list[ReportSection] = Field(min_length=1)


class ValidationIssue(StrictModel):
    code: str
    message: str
    severity: Severity
    target: str | None = None


class ValidationReport(StrictModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    checked_evidence_ids: list[str] = Field(default_factory=list)


class ReviewIssue(StrictModel):
    target: str
    problem: str
    required_action: str
    severity: Severity


class ReflectionDecision(StrictModel):
    verdict: Literal["pass", "revise"]
    route: Literal["finish", "replan", "rewrite", "ask_user"]
    reason: str
    issues: list[ReviewIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_route(self):
        if self.verdict == "pass" and self.route != "finish":
            raise ValueError("pass requires finish")
        return self


class ResultDecision(StrictModel):
    action: Literal["continue", "replan", "draft", "ask_user", "answer"]
    reason: str
    question: str | None = None
    answer: str | None = None

    @model_validator(mode='after')
    def require_answer(self):
        if self.action == 'answer' and not (self.answer or '').strip():
            raise ValueError('answer action requires a nonempty answer')
        return self


class ConvergenceDecision(ResultDecision):
    """Model decision used when replanning produced no executable work."""

    action: Literal["draft", "ask_user", "answer"]
    reason: str
    question: str | None = None


class EvidenceRecord(StrictModel):
    id: str
    task_id: str
    run_id: str | None = None
    data_revision: int = 0
    title: str
    source: str
    columns: list[str]
    rows: list[dict[str, Any]]
    source_dataset_ids: list[str] = Field(default_factory=list)
    source_revision_ids: list[str] = Field(default_factory=list)
    query: str | None = None
    query_hash: str | None = None
    created_at: str = Field(default_factory=utc_now)


class MessageRecord(StrictModel):
    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: str
    sequence: int = 0


class AnalysisMemory(StrictModel):
    question: str
    conclusion: str
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ExactMemoryFact(StrictModel):
    subject: str
    metric: str
    value: str
    unit: str
    period: str = ""
    source_sequence: int
    source_quote: str


class ConversationMemory(StrictModel):
    task_goal: str = ""
    exact_facts: list[ExactMemoryFact] = Field(default_factory=list)
    confirmed_requirements: list[str] = Field(default_factory=list)
    analysis_scope: list[str] = Field(default_factory=list)
    metric_definitions: list[str] = Field(default_factory=list)
    user_preferences: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    completed_analyses: list[AnalysisMemory] = Field(default_factory=list)
    referenced_evidence_ids: list[str] = Field(default_factory=list)
    last_updated_at: str = Field(default_factory=utc_now)


class ConversationMemoryRecord(StrictModel):
    task_id: str
    memory: ConversationMemory
    covered_until_sequence: int = 0
    version: int
    created_at: str
    updated_at: str


class ConversationContext(StrictModel):
    memory: ConversationMemory | None = None
    recent_messages: list[MessageRecord] = Field(default_factory=list)


class TaskSnapshot(StrictModel):
    id: str
    title: str
    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    status_message: str
    files: list[UploadedFile] = Field(default_factory=list)
    messages: list[MessageRecord] = Field(default_factory=list)
    datasets: list[DatasetInfo] = Field(default_factory=list)
    relationships: list[DatasetRelationship] = Field(default_factory=list)
    result: AnalysisDraft | None = None
    clarification_question: str | None = None
    error: str | None = None
    active_run_id: str | None = None
    pending_run_id: str | None = None
    queue_position: int | None = None
    data_revision: int = 0
    created_at: str
    updated_at: str


class TaskListItem(StrictModel):
    id: str
    title: str
    status: TaskStatus
    status_message: str
    file_names: list[str]
    created_at: str
    updated_at: str


class TaskListResponse(StrictModel):
    items: list[TaskListItem]
    total: int
    page: int
    page_size: int


class TaskEvent(StrictModel):
    schema_version: int = 1
    id: int
    task_id: str
    event_type: str
    status: TaskStatus
    progress: int
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RunArtifact(StrictModel):
    id: str
    task_id: str
    run_id: str
    message_id: str | None = None
    sequence: int = Field(ge=1)
    artifact_type: Literal["query", "table", "chart", "validation", "error"]
    status: Literal["pending", "running", "ready", "failed"]
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class ReportSummary(StrictModel):
    id: str
    task_id: str
    title: str
    status: Literal["draft", "ready", "failed", "archived"]
    latest_version: int
    created_at: str
    updated_at: str


class ReportVersion(StrictModel):
    id: str
    report_id: str
    version_number: int = Field(ge=1)
    run_id: str
    status: Literal["generating", "ready", "failed"]
    content: dict[str, Any]
    source_revision_ids: list[str] = Field(default_factory=list)
    content_hash: str
    html_path: str | None = None
    created_at: str


class ReportDetail(ReportSummary):
    versions: list[ReportVersion] = Field(default_factory=list)


class ReportJob(StrictModel):
    id: str
    task_id: str
    run_id: str
    status: Literal["queued", "generating", "ready", "failed"]
    report_id: str | None = None
    report_version_id: str | None = None
    error: str | None = None
    attempt_count: int = 0
    claimed_at: str | None = None
    created_at: str
    updated_at: str


class CreateTaskResponse(StrictModel):
    id: str
    status: TaskStatus


class UploadFailure(StrictModel):
    name: str
    code: str
    message: str


class UploadBatchResponse(StrictModel):
    outcome: Literal["success", "partial", "failed"]
    accepted_files: list[str] = Field(default_factory=list)
    rejected_files: list[UploadFailure] = Field(default_factory=list)
    task: TaskSnapshot


class MessageRequest(StrictModel):
    content: str = Field(min_length=1, max_length=4000)


class ErrorResponse(StrictModel):
    code: str
    message: str
    retryable: bool = False


class WorkflowRun(StrictModel):
    id: str
    task_id: str
    question: str
    status: str
    parent_run_id: str | None = None
    entry_node: str = "classify"
    result: AnalysisDraft | None = None
    is_active: bool = False
    data_revision: int = 0
    message_sequence: int = 0
    claimed_at: str | None = None
    heartbeat_at: str | None = None
    attempt_count: int = 0
    error: str | None = None
    started_at: str
    finished_at: str | None = None


class AnalysisState(StrictModel):
    schema_version: int = 3
    task_id: str
    run_id: str
    user_question: str
    # The first request that entered the analysis workflow remains stable
    # across clarification turns. The user's latest answer is passed
    # separately so downstream nodes do not classify it as a new request.
    analysis_question: str | None = None
    clarification_answer: str | None = None
    entry_node: Literal["classify", "plan", "execute", "draft", "reflect"] = "classify"
    is_replay: bool = False
    conversation_summary: dict[str, Any] | None = None
    conversation_messages: list[dict[str, Any]] = Field(default_factory=list)
    previous_result: dict[str, Any] | None = None
    inherited_evidence_ids: list[str] = Field(default_factory=list)
    data_revision: int = 0
    result_decision: dict[str, Any] | None = None
    decision_history: list[dict[str, Any]] = Field(default_factory=list)
    context_prepared: bool = False
    datasets: list[dict[str, Any]] = Field(default_factory=list)
    confirmed_relationships: list[dict[str, Any]] = Field(default_factory=list)
    dataset_candidates: list[dict[str, Any]] = Field(default_factory=list)
    intent: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    completed_step_ids: list[str] = Field(default_factory=list)
    tool_call_count: int = 0
    query_failures: list[dict[str, Any]] = Field(default_factory=list)
    draft: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    reflection: dict[str, Any] | None = None
    revision_round: int = 0
    citation_repair_round: int = 0
    final_status: str | None = None
    error: str | None = None
