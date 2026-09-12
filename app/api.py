from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from .config import settings
from .dataset_service import correct_dataset, create_task_from_revision, preview_dataset, profile_dataset
from .exports import export_excel, export_html
from .ingestion import IngestionError, ingest_file, task_dir
from .knowledge_service import (
    KnowledgeEmbeddingError,
    archive_knowledge_base,
    create_knowledge_base,
    get_knowledge_base,
    list_knowledge_bases,
    list_run_matches,
    list_task_bindings,
    publish_knowledge_revision,
    replace_task_bindings,
)
from .models import (
    CreateTaskResponse,
    DatasetAssetDetail,
    DatasetAssetList,
    DatasetCorrectionRequest,
    DatasetCorrectionResult,
    DatasetPreview,
    DatasetProfile,
    EvidenceRecord,
    KnowledgeBaseCreate,
    KnowledgeBaseDetail,
    KnowledgeBaseList,
    KnowledgeBindingRequest,
    KnowledgeMatch,
    KnowledgeRevisionCreate,
    MessageRequest,
    NodeExecutionDetail,
    NodeExecutionSummary,
    ReplayNodeRequest,
    ReplayNodeResponse,
    ReportDetail,
    ReportJob,
    ReportSummary,
    RunArtifact,
    TaskListResponse,
    TaskSnapshot,
    DatasetRelationship,
    RelationshipConfirmationRequest,
    TaskKnowledgeBinding,
    TaskStatus,
    UploadBatchResponse,
    UploadFailure,
    UploadedFile,
    WorkflowRun,
)
from .repository import repository
from .worker import worker


router = APIRouter(prefix=settings.api_prefix)


@router.post("/tasks", response_model=CreateTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task() -> CreateTaskResponse:
    task_id = repository.create_task()
    return CreateTaskResponse(id=task_id, status=TaskStatus.READY)


@router.post("/tasks/{task_id}/files", response_model=UploadBatchResponse)
async def upload_files(
    task_id: str,
    response: Response,
    files: list[UploadFile] = File(...),
    publish_to_library: bool = Query(default=False),
) -> UploadBatchResponse:
    snapshot = _snapshot(task_id)
    original_status = snapshot.status
    original_progress = snapshot.progress
    original_message = snapshot.status_message
    original_error = snapshot.error
    existing_ready = sum(file.status == "ready" for file in snapshot.files)
    if existing_ready + len(files) > settings.max_files:
        raise HTTPException(400, f"每个任务最多上传 {settings.max_files} 个文件")
    if not repository.begin_ingestion(task_id):
        raise HTTPException(409, "任务正在上传或分析，请等待当前操作完成")
    rejected: list[UploadFailure] = []
    rejected_statuses: list[int] = []
    accepted: list[str] = []
    for upload in files:
        original_name = Path(upload.filename or "未命名文件").name
        extension = Path(original_name).suffix.lower()
        file_id = str(uuid.uuid4())
        stored_name = f"{file_id}{extension}"
        upload_dir = task_dir(task_id) / "uploads"
        upload_dir.mkdir(exist_ok=True)
        stored_path = upload_dir / stored_name
        size = 0
        record_added = False
        try:
            if extension not in {".xlsx", ".csv"}:
                raise IngestionError(
                    "仅支持 .xlsx 和 .csv 文件", code="unsupported_type", http_status=415
                )
            async with aiofiles.open(stored_path, "wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_file_size:
                        limit_mb = settings.max_file_size // (1024 * 1024)
                        raise IngestionError(
                            f"单个文件不能超过 {limit_mb} MB",
                            code="file_too_large",
                            http_status=413,
                        )
                    await output.write(chunk)
            record = UploadedFile(id=file_id, original_name=original_name, size=size, status="ready")
            repository.add_file(task_id, record, stored_name)
            record_added = True
            await asyncio.to_thread(
                ingest_file,
                task_id,
                record,
                stored_path,
                publish_to_library=publish_to_library,
            )
            accepted.append(original_name)
        except Exception as exc:
            code = exc.code if isinstance(exc, IngestionError) else "ingestion_failed"
            rejected_statuses.append(exc.http_status if isinstance(exc, IngestionError) else 422)
            rejected.append(UploadFailure(name=original_name, code=code, message=str(exc)))
            if record_added:
                repository.update_file(UploadedFile(id=file_id, original_name=original_name, size=size, status="failed", error=str(exc)))
            stored_path.unlink(missing_ok=True)
        finally:
            await upload.close()
    if accepted:
        repository.publish_data_revision(task_id)
        snapshot = _snapshot(task_id)
        message = f"已读取 {len(snapshot.datasets)} 个数据表"
        if rejected:
            message += f"，{len(rejected)} 个文件失败"
        repository.update_task(
            task_id, status=TaskStatus.READY, progress=15, status_message=message,
            error="；".join(f"{item.name}：{item.message}" for item in rejected) or None,
            clear_error=not rejected,
        )
    else:
        repository.update_task(
            task_id, status=original_status, progress=original_progress,
            status_message=original_message,
            error=original_error,
            clear_error=original_error is None,
        )
        response.status_code = (
            status.HTTP_413_CONTENT_TOO_LARGE
            if status.HTTP_413_CONTENT_TOO_LARGE in rejected_statuses
            else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            if rejected and all(item.code == "unsupported_type" for item in rejected)
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    outcome = "success" if not rejected else "partial" if accepted else "failed"
    return UploadBatchResponse(
        outcome=outcome, accepted_files=accepted, rejected_files=rejected, task=_snapshot(task_id)
    )


@router.post("/tasks/{task_id}/messages", response_model=TaskSnapshot, status_code=status.HTTP_202_ACCEPTED)
async def send_message(task_id: str, request: MessageRequest) -> TaskSnapshot:
    snapshot = _snapshot(task_id)
    if not snapshot.datasets:
        raise HTTPException(400, "请先上传可分析的 Excel 或 CSV 文件")
    content = request.content.strip()
    try:
        run_id = repository.queue_analysis(task_id, content)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await worker.enqueue_run(run_id)
    return _snapshot(task_id)


@router.get("/tasks/{task_id}", response_model=TaskSnapshot)
async def get_task(task_id: str) -> TaskSnapshot:
    return _snapshot(task_id)


@router.get("/tasks/{task_id}/relationships", response_model=list[DatasetRelationship])
async def get_task_relationships(task_id: str) -> list[DatasetRelationship]:
    snapshot = _snapshot(task_id)
    from .dataset_retrieval import detect_dataset_relationships
    detected = [DatasetRelationship.model_validate(item) for item in detect_dataset_relationships(snapshot.datasets)]
    saved = {(
        item.left_dataset_id, item.left_field, item.right_dataset_id, item.right_field
    ): item for item in snapshot.relationships}
    merged: list[DatasetRelationship] = []
    for item in detected:
        key = (item.left_dataset_id, item.left_field, item.right_dataset_id, item.right_field)
        merged.append(saved.get(key, item))
    for item in snapshot.relationships:
        key = (item.left_dataset_id, item.left_field, item.right_dataset_id, item.right_field)
        if key not in {(entry.left_dataset_id, entry.left_field, entry.right_dataset_id, entry.right_field) for entry in merged}:
            merged.append(item)
    return merged


@router.post("/tasks/{task_id}/relationships", response_model=list[DatasetRelationship])
async def confirm_task_relationships(task_id: str, request: RelationshipConfirmationRequest) -> list[DatasetRelationship]:
    snapshot = _snapshot(task_id)
    dataset_ids = {item.id for item in snapshot.datasets}
    datasets_by_id = {item.id: item for item in snapshot.datasets}
    for relation in request.relationships:
        if relation.left_dataset_id not in dataset_ids or relation.right_dataset_id not in dataset_ids:
            raise HTTPException(422, "关系引用了当前任务之外的数据表")
        if relation.left_dataset_id == relation.right_dataset_id:
            raise HTTPException(422, "关系必须连接两张不同的数据表")
        left_fields = {column.name for column in datasets_by_id[relation.left_dataset_id].columns}
        right_fields = {column.name for column in datasets_by_id[relation.right_dataset_id].columns}
        if relation.left_field not in left_fields or relation.right_field not in right_fields:
            raise HTTPException(422, "关系引用了不存在的字段")
    return repository.save_task_relationships(task_id, request.relationships)


@router.get(
    "/tasks/{task_id}/datasets/{dataset_id}/preview", response_model=DatasetPreview
)
async def get_dataset_preview(
    task_id: str,
    dataset_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort_by: str | None = None,
    sort_direction: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> DatasetPreview:
    _snapshot(task_id)
    try:
        return await asyncio.to_thread(
            preview_dataset,
            task_id,
            dataset_id,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_direction=sort_direction,
        )
    except KeyError as exc:
        raise HTTPException(404, "数据表不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get(
    "/tasks/{task_id}/datasets/{dataset_id}/profile", response_model=DatasetProfile
)
async def get_dataset_profile(task_id: str, dataset_id: str) -> DatasetProfile:
    _snapshot(task_id)
    try:
        return await asyncio.to_thread(profile_dataset, task_id, dataset_id)
    except KeyError as exc:
        raise HTTPException(404, "数据表不存在") from exc


@router.post(
    "/tasks/{task_id}/datasets/{dataset_id}/corrections",
    response_model=DatasetCorrectionResult,
)
async def apply_dataset_corrections(
    task_id: str, dataset_id: str, request: DatasetCorrectionRequest
) -> DatasetCorrectionResult:
    snapshot = _snapshot(task_id)
    if snapshot.pending_run_id:
        raise HTTPException(409, "分析运行中，暂不能发布数据修改")
    try:
        return await asyncio.to_thread(correct_dataset, task_id, dataset_id, request)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "数据表不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/datasets", response_model=DatasetAssetList)
async def list_datasets(include_archived: bool = False) -> DatasetAssetList:
    return repository.list_data_assets(include_archived)


@router.get("/datasets/{dataset_id}", response_model=DatasetAssetDetail)
async def get_dataset(dataset_id: str) -> DatasetAssetDetail:
    try:
        return repository.get_data_asset(dataset_id)
    except KeyError as exc:
        raise HTTPException(404, "数据集不存在") from exc


@router.delete("/datasets/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_dataset(dataset_id: str) -> Response:
    try:
        repository.archive_data_asset(dataset_id)
    except KeyError as exc:
        raise HTTPException(404, "数据集不存在") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/datasets/{dataset_id}/revisions/{revision_id}/tasks",
    response_model=CreateTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def analyze_dataset_revision(dataset_id: str, revision_id: str) -> CreateTaskResponse:
    try:
        task_id = await asyncio.to_thread(create_task_from_revision, dataset_id, revision_id)
    except KeyError as exc:
        raise HTTPException(404, "数据集版本不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return CreateTaskResponse(id=task_id, status=TaskStatus.READY)


@router.get("/knowledge-bases", response_model=KnowledgeBaseList)
async def get_knowledge_bases(include_archived: bool = False) -> KnowledgeBaseList:
    return list_knowledge_bases(include_archived)


@router.post(
    "/knowledge-bases", response_model=KnowledgeBaseDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_private_knowledge_base(request: KnowledgeBaseCreate) -> KnowledgeBaseDetail:
    try:
        return await asyncio.to_thread(create_knowledge_base, request)
    except KnowledgeEmbeddingError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseDetail)
async def get_private_knowledge_base(knowledge_base_id: str) -> KnowledgeBaseDetail:
    try:
        return get_knowledge_base(knowledge_base_id)
    except KeyError as exc:
        raise HTTPException(404, "知识库不存在") from exc


@router.post("/knowledge-bases/{knowledge_base_id}/revisions", response_model=KnowledgeBaseDetail)
async def create_knowledge_revision(
    knowledge_base_id: str, request: KnowledgeRevisionCreate
) -> KnowledgeBaseDetail:
    try:
        return await asyncio.to_thread(publish_knowledge_revision, knowledge_base_id, request)
    except KnowledgeEmbeddingError as exc:
        raise HTTPException(503, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "知识库不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/knowledge-bases/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(knowledge_base_id: str) -> Response:
    try:
        archive_knowledge_base(knowledge_base_id)
    except KeyError as exc:
        raise HTTPException(404, "知识库不存在") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/tasks/{task_id}/knowledge-bases", response_model=list[TaskKnowledgeBinding]
)
async def get_task_knowledge_bases(task_id: str) -> list[TaskKnowledgeBinding]:
    _snapshot(task_id)
    return list_task_bindings(task_id)


@router.post(
    "/tasks/{task_id}/knowledge-bases", response_model=list[TaskKnowledgeBinding]
)
async def set_task_knowledge_bases(
    task_id: str, request: KnowledgeBindingRequest
) -> list[TaskKnowledgeBinding]:
    snapshot = _snapshot(task_id)
    if snapshot.pending_run_id:
        raise HTTPException(409, "分析运行中，暂不能修改知识库")
    try:
        return replace_task_bindings(task_id, request.bindings)
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get(
    "/tasks/{task_id}/runs/{run_id}/knowledge", response_model=list[KnowledgeMatch]
)
async def get_run_knowledge(task_id: str, run_id: str) -> list[KnowledgeMatch]:
    _snapshot(task_id)
    try:
        repository.get_run(task_id, run_id)
    except KeyError as exc:
        raise HTTPException(404, "分析运行不存在") from exc
    return list_run_matches(run_id)


@router.get("/tasks/{task_id}/runs", response_model=list[WorkflowRun])
async def list_runs(task_id: str) -> list[WorkflowRun]:
    _snapshot(task_id)
    return repository.list_runs(task_id)


@router.get(
    "/tasks/{task_id}/runs/{run_id}/artifacts", response_model=list[RunArtifact]
)
async def list_run_artifacts(task_id: str, run_id: str) -> list[RunArtifact]:
    _snapshot(task_id)
    try:
        repository.get_run(task_id, run_id)
        return repository.list_artifacts(task_id, run_id)
    except KeyError as exc:
        raise HTTPException(404, "分析运行不存在") from exc


@router.get("/tasks/{task_id}/runs/{run_id}/nodes", response_model=list[NodeExecutionSummary])
async def list_run_nodes(task_id: str, run_id: str) -> list[NodeExecutionSummary]:
    _snapshot(task_id)
    try:
        return repository.list_node_executions(task_id, run_id)
    except KeyError as exc:
        raise HTTPException(404, "分析分支不存在") from exc


@router.get("/tasks/{task_id}/nodes/{execution_id}", response_model=NodeExecutionDetail)
async def get_node_execution(task_id: str, execution_id: str) -> NodeExecutionDetail:
    _snapshot(task_id)
    try:
        return repository.get_node_execution(task_id, execution_id)
    except KeyError as exc:
        raise HTTPException(404, "节点快照不存在") from exc


@router.post("/tasks/{task_id}/nodes/{execution_id}/replay", response_model=ReplayNodeResponse, status_code=status.HTTP_202_ACCEPTED)
async def replay_node(task_id: str, execution_id: str, request: ReplayNodeRequest) -> ReplayNodeResponse:
    snapshot = _snapshot(task_id)
    if snapshot.status not in {
        TaskStatus.COMPLETED, TaskStatus.COMPLETED_WITH_WARNINGS, TaskStatus.NEEDS_REVIEW,
        TaskStatus.FAILED, TaskStatus.OFF_TOPIC, TaskStatus.NEEDS_CLARIFICATION,
    }:
        raise HTTPException(409, "当前分析尚未结束，不能创建重跑分支")
    if repository.has_pending_run(task_id):
        raise HTTPException(409, "已有分析分支正在运行")
    try:
        source = repository.get_node_execution(task_id, execution_id)
        source_run = repository.get_run(task_id, source.run_id)
    except KeyError as exc:
        raise HTTPException(404, "节点快照不存在") from exc
    if source.status != "completed":
        raise HTTPException(409, "只能从已经完成的节点重新运行")
    if not source.prompt_replay_supported:
        raise HTTPException(409, source.replay_unavailable_reason)
    try:
        repository.assert_run_context_current(task_id, source.run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    source_input_snapshot = repository.get_run_input_snapshot(source.run_id)
    content = request.prompt_content.strip()
    if content == source.prompt_version.content.strip():
        raise HTTPException(400, "提示词没有发生变化")
    prompt = repository.ensure_prompt_version(
        source.node_name,
        f"custom-{source.prompt_version.version}",
        content,
        source.prompt_version.id,
    )
    try:
        run_id = repository.start_execution(
            task_id,
            source_run.question,
            status="queued",
            parent_run_id=source.run_id,
            forked_from_node_execution_id=source.id,
            entry_node=source.node_name,
            prompt_version_id=prompt.id,
            data_revision=source_run.data_revision,
            message_sequence=source_run.message_sequence,
            input_snapshot=source_input_snapshot,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    repository.update_task(
        task_id,
        status=TaskStatus.CLASSIFYING,
        progress=18,
        status_message="提示词重跑已进入队列",
        clear_error=True,
        event_type="run.replay_queued",
        payload={"run_id": run_id, "source_node_execution_id": source.id},
    )
    await worker.enqueue_replay(run_id)
    return ReplayNodeResponse(run_id=run_id, status="queued")


@router.post("/tasks/{task_id}/runs/{run_id}/activate", response_model=WorkflowRun)
async def activate_run(task_id: str, run_id: str) -> WorkflowRun:
    _snapshot(task_id)
    if repository.has_pending_run(task_id):
        raise HTTPException(409, "分析运行期间不能切换分支")
    try:
        return repository.activate_run(task_id, run_id)
    except KeyError as exc:
        raise HTTPException(404, "分析分支不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/tasks/{task_id}/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_run(task_id: str, run_id: str) -> None:
    _snapshot(task_id)
    try:
        repository.cancel_queued_execution(task_id, run_id)
    except KeyError as exc:
        raise HTTPException(404, "分析分支不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    query: str = "",
    task_status: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> TaskListResponse:
    if task_status and task_status not in {item.value for item in TaskStatus}:
        raise HTTPException(400, "状态筛选值无效")
    return repository.list_tasks(query.strip(), task_status, page, page_size)


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, after: int = 0) -> StreamingResponse:
    _snapshot(task_id)
    header_cursor = request.headers.get("last-event-id")
    if header_cursor and header_cursor.isdigit():
        after = max(after, int(header_cursor))

    async def stream():
        cursor = after
        idle = 0
        while not await request.is_disconnected():
            events = repository.events_after(task_id, cursor)
            for event in events:
                cursor = event.id
                yield f"id: {event.id}\nevent: {event.event_type}\ndata: {event.model_dump_json()}\n\n"
                idle = 0
            if not events:
                idle += 1
                if idle % 30 == 0:
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/tasks/{task_id}/evidence/{evidence_id}", response_model=EvidenceRecord)
async def get_evidence(task_id: str, evidence_id: str) -> EvidenceRecord:
    snapshot = _snapshot(task_id)
    evidence = repository.get_evidence(task_id, evidence_id)
    if evidence is None:
        raise HTTPException(404, "证据不存在")
    lineage = repository.run_lineage_ids(task_id, snapshot.active_run_id) if snapshot.active_run_id else set()
    if (
        evidence.data_revision != snapshot.data_revision
        or snapshot.active_run_id is None
        or (evidence.run_id is not None and evidence.run_id not in lineage)
    ):
        raise HTTPException(409, "该证据不属于当前数据版本的活动分析结果")
    return evidence


@router.get("/tasks/{task_id}/exports/excel")
async def download_excel(task_id: str) -> FileResponse:
    snapshot = _snapshot(task_id)
    _require_current_result(snapshot)
    try:
        path = await asyncio.to_thread(export_excel, snapshot)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    repository.record_export(task_id, "excel", path, snapshot.active_run_id)
    return FileResponse(path, filename=f"{_download_name(snapshot.title)}.xlsx")


@router.get("/tasks/{task_id}/exports/report")
async def download_report(task_id: str) -> FileResponse:
    snapshot = _snapshot(task_id)
    _require_current_result(snapshot)
    try:
        path = await asyncio.to_thread(export_html, snapshot)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    repository.record_export(task_id, "report", path, snapshot.active_run_id)
    return FileResponse(path, filename=f"{_download_name(snapshot.title)}.html", media_type="text/html")


@router.post("/tasks/{task_id}/reports", response_model=ReportJob, status_code=status.HTTP_202_ACCEPTED)
async def publish_report(task_id: str) -> ReportJob:
    snapshot = _snapshot(task_id)
    _require_current_result(snapshot)
    try:
        job = repository.queue_report(task_id, snapshot.active_run_id or "")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await worker.enqueue_report(job.id)
    return job


@router.get("/report-jobs/{job_id}", response_model=ReportJob)
async def get_report_job(job_id: str) -> ReportJob:
    try:
        return repository.get_report_job(job_id)
    except KeyError as exc:
        raise HTTPException(404, "报告任务不存在") from exc


@router.get("/reports", response_model=list[ReportSummary])
async def list_reports() -> list[ReportSummary]:
    return repository.list_reports()


@router.get("/reports/{report_id}", response_model=ReportDetail)
async def get_report(report_id: str) -> ReportDetail:
    try:
        return repository.get_report(report_id)
    except KeyError as exc:
        raise HTTPException(404, "报告不存在") from exc


@router.get("/reports/{report_id}/versions/{version_id}/download")
async def download_report_version(report_id: str, version_id: str) -> FileResponse:
    try:
        report = repository.get_report(report_id)
    except KeyError as exc:
        raise HTTPException(404, "报告不存在") from exc
    version = next((item for item in report.versions if item.id == version_id), None)
    if version is None:
        raise HTTPException(404, "报告版本不存在")
    if not version.html_path or not Path(version.html_path).is_file():
        raise HTTPException(410, "该报告版本的文件不可用")
    return FileResponse(
        version.html_path,
        filename=f"{_download_name(report.title)}-v{version.version_number}.html",
        media_type="text/html",
    )


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: str) -> None:
    snapshot = _snapshot(task_id)
    if snapshot.status not in {
        TaskStatus.READY, TaskStatus.OFF_TOPIC, TaskStatus.NEEDS_CLARIFICATION,
        TaskStatus.NEEDS_REVIEW, TaskStatus.COMPLETED_WITH_WARNINGS,
        TaskStatus.COMPLETED, TaskStatus.FAILED,
    }:
        raise HTTPException(409, "任务正在运行，完成后才能删除")
    directory = task_dir(task_id)
    hard_deleted = repository.delete_task(task_id)
    root = (settings.data_dir / "tasks").resolve()
    resolved = directory.resolve()
    if hard_deleted and root in resolved.parents and resolved.exists():
        shutil.rmtree(resolved)


def _snapshot(task_id: str) -> TaskSnapshot:
    try:
        snapshot = repository.get_task(task_id)
        return snapshot.model_copy(update={"knowledge_bases": list_task_bindings(task_id)})
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc


def _download_name(title: str) -> str:
    safe = "".join(char for char in title if char not in '<>:"/\\|?*').strip()
    return safe[:60] or "分析结果"


def _require_current_result(snapshot: TaskSnapshot) -> None:
    if snapshot.status == TaskStatus.INGESTING:
        raise HTTPException(409, "文件上传处理中，完成后才能导出结果")
    if snapshot.result is None or snapshot.active_run_id is None:
        raise HTTPException(409, "当前任务没有可导出的已验证结果")
    try:
        run = repository.get_run(snapshot.id, snapshot.active_run_id)
    except KeyError as exc:
        raise HTTPException(409, "当前结果对应的分析分支不存在") from exc
    if run.data_revision != snapshot.data_revision or run.status not in {
        TaskStatus.COMPLETED.value,
        TaskStatus.COMPLETED_WITH_WARNINGS.value,
    }:
        raise HTTPException(409, "当前结果不是基于最新数据生成的已验证结果")
