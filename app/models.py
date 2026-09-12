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


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


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


class KnowledgeDocumentInput(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=200000)
    source_name: str | None = Field(default=None, max_length=260)


class KnowledgeBaseCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    documents: list[KnowledgeDocumentInput] = Field(min_length=1, max_length=50)
    change_summary: str = Field(default="创建知识库", min_length=1, max_length=200)


class KnowledgeRevisionCreate(StrictModel):
    documents: list[KnowledgeDocumentInput] = Field(min_length=1, max_length=50)
    change_summary: str = Field(default="更新知识内容", min_length=1, max_length=200)


class KnowledgeDocument(StrictModel):
    id: str
    revision_id: str
    title: str
    content: str
    source_name: str | None = None
    chunk_count: int = 0
    created_at: str


class KnowledgeBaseRevision(StrictModel):
    id: str
    knowledge_base_id: str
    revision_number: int = Field(ge=1)
    parent_revision_id: str | None = None
    status: Literal["published", "archived"] = "published"
    embedding_model: str
    embedding_dimensions: int = Field(gt=0)
    content_hash: str
    change_summary: str
    documents: list[KnowledgeDocument] = Field(default_factory=list)
    created_at: str


class KnowledgeBaseSummary(StrictModel):
    id: str
    name: str
    description: str = ""
    status: Literal["active", "archived"] = "active"
    owner_id: str = "local"
    latest_revision: int = 1
    document_count: int = 0
    chunk_count: int = 0
    created_at: str
    updated_at: str


class KnowledgeBaseDetail(KnowledgeBaseSummary):
    revisions: list[KnowledgeBaseRevision] = Field(default_factory=list)
    permission: Literal["owner", "editor", "viewer"] = "owner"


class KnowledgeBaseList(StrictModel):
    items: list[KnowledgeBaseSummary]
    total: int


class KnowledgeBindingItem(StrictModel):
    knowledge_base_id: str
    revision_id: str


class KnowledgeBindingRequest(StrictModel):
    bindings: list[KnowledgeBindingItem] = Field(default_factory=list, max_length=20)


class TaskKnowledgeBinding(StrictModel):
    knowledge_base_id: str
    knowledge_base_name: str
    revision_id: str
    revision_number: int
    embedding_model: str
    bound_at: str


class KnowledgeMatch(StrictModel):
    chunk_id: str
    knowledge_base_id: str
    knowledge_base_name: str
    revision_id: str
    revision_number: int
    document_id: str
    document_title: str
    content: str
    score: float
    rank: int


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
    route: Literal['analysis', 'derived_metric', 'clarification', 'conversation', 'explanation']
    reason: str = ''
    reply: str | None = None
    metric: str | None = None

    @model_validator(mode='before')
    @classmethod
    def read_legacy(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        legacy = value.pop('is_analysis', None)
        value.pop('confidence', None)
        if 'suggested_response' in value:
            value.setdefault('reply', value.pop('suggested_response'))
        if 'route' not in value and isinstance(legacy, bool):
            value['route'] = 'analysis' if legacy else 'conversation'
        if value.get('route') == 'off_topic':
            value['route'] = 'conversation'
        if legacy is False and value.get('route') == 'conversation' and not value.get('reply'):
            value['reply'] = '请告诉我你想了解的问题。'
        if isinstance(legacy, bool) and legacy != (value.get('route') != 'conversation'):
            raise ValueError('conflicting legacy intent and route')
        return value

    @property
    def is_analysis(self) -> bool:
        return self.route != 'conversation'

    @property
    def suggested_response(self) -> str | None:
        return self.reply

    @model_validator(mode='after')
    def validate_route_fields(self):
        if self.route == 'derived_metric' and not self.metric:
            raise ValueError('derived_metric requires metric')
        if self.route == 'conversation' and not (self.reply or '').strip():
            raise ValueError('conversation requires a nonempty reply answering the user')
        return self


class PlanStep(StrictModel):
    id: str
    purpose: str
    tool: Literal["auto_analyze", "profile_table", "query_data"]
    dataset_id: str | None = None
    dataset_ids: list[str] = Field(default_factory=list, max_length=6)
    joins: list["QueryJoin"] = Field(default_factory=list, max_length=3)


class QueryMeasure(StrictModel):
    field: str
    aggregation: Literal["sum", "average", "min", "max", "count"] = "sum"
    alias: str | None = None


class QueryFilter(StrictModel):
    field: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains"]
    value: str | int | float


class QueryJoin(StrictModel):
    right_dataset_id: str
    left_field: str
    right_field: str
    join_type: Literal["inner", "left"] = "left"


class QuerySpec(StrictModel):
    dataset_id: str
    dimensions: list[str] = Field(default_factory=list, max_length=3)
    measures: list[QueryMeasure] = Field(default_factory=list, min_length=1, max_length=5)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=10)
    order_by: str | None = None
    descending: bool = True
    limit: int = Field(default=100, ge=1, le=5000)
    joins: list[QueryJoin] = Field(default_factory=list, max_length=3)


class DatasetQuerySpec(StrictModel):
    dimensions: list[str] = Field(default_factory=list, max_length=3)
    measures: list[QueryMeasure] = Field(default_factory=list, min_length=1, max_length=5)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=10)
    order_by: str | None = None
    descending: bool = True
    limit: int = Field(default=100, ge=1, le=5000)
    joins: list[QueryJoin] = Field(default_factory=list, max_length=3)


class QueryRequest(StrictModel):
    query: DatasetQuerySpec
    title: str = "查询结果"


class EvidencePointer(StrictModel):
    evidence_id: str
    row_index: int = Field(ge=0)
    field: str
    raw_value: str
    unit: str | None = None


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


class ToolExecutionResult(StrictModel):
    result_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "error"]
    summary: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
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


class AnalysisDraft(StrictModel):
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
    verification_level: Literal["legacy", "evidence", "cell"] = "evidence"


class GeneratedAnalysisDraft(AnalysisDraft):
    """Bound model output size without invalidating larger historical reports."""

    metrics: list[Metric] = Field(default_factory=list, max_length=8)
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    charts: list[ChartSpec] = Field(default_factory=list, max_length=4)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    warnings: list[str] = Field(default_factory=list, max_length=10)
    suggested_questions: list[str] = Field(default_factory=list, max_length=5)


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
    route: Literal["finish", "replan", "execute", "rewrite"]
    reason: str
    issues: list[ReviewIssue] = Field(default_factory=list)


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


class ConversationMemory(StrictModel):
    task_goal: str = ""
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
    knowledge_bases: list[TaskKnowledgeBinding] = Field(default_factory=list)
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


class PromptVersion(StrictModel):
    id: str
    node_name: str
    version: str
    content: str
    content_hash: str
    parent_version_id: str | None = None
    created_at: str


class WorkflowRun(StrictModel):
    id: str
    task_id: str
    question: str
    status: str
    parent_run_id: str | None = None
    forked_from_node_execution_id: str | None = None
    entry_node: str = "classify"
    prompt_version_id: str | None = None
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


class NodeExecutionSummary(StrictModel):
    id: str
    run_id: str
    node_name: str
    occurrence: int
    prompt_version: PromptVersion
    status: Literal["running", "completed", "failed"]
    error: str | None = None
    started_at: str
    finished_at: str | None = None


class NodeExecutionDetail(NodeExecutionSummary):
    input_state: dict[str, Any]
    output_state: dict[str, Any] | None = None
    state_schema_version: int
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class ReplayNodeRequest(StrictModel):
    prompt_content: str = Field(min_length=20, max_length=30000)


class ReplayNodeResponse(StrictModel):
    run_id: str
    status: str


class AnalysisState(StrictModel):
    schema_version: int = 3
    task_id: str
    run_id: str
    user_question: str
    entry_node: Literal["classify", "plan", "execute", "draft", "reflect"] = "classify"
    is_replay: bool = False
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    conversation_summary: dict[str, Any] | None = None
    context_prepared: bool = False
    datasets: list[dict[str, Any]] = Field(default_factory=list)
    confirmed_relationships: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_context: list[dict[str, Any]] = Field(default_factory=list)
    dataset_candidates: list[dict[str, Any]] = Field(default_factory=list)
    intent: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    completed_step_ids: list[str] = Field(default_factory=list)
    tool_call_count: int = 0
    step_attempts: dict[str, int] = Field(default_factory=dict)
    draft: dict[str, Any] | None = None
    draft_execution_mode: Literal["model", "deterministic", "unknown"] = "unknown"
    validation: dict[str, Any] | None = None
    reflection: dict[str, Any] | None = None
    revision_round: int = 0
    final_status: str | None = None
    error: str | None = None
