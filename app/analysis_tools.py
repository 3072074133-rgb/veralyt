from __future__ import annotations

import re
import logging
import threading
import time
import uuid
import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb
from sqlglot import exp, parse

from .config import settings
from .ingestion import task_dir
from .models import DatasetInfo, EvidenceRecord, ToolExecutionResult
from .observability import duration_ms, log_event
from .repository import repository


FORBIDDEN_FUNCTIONS = {
    "http_get", "httpfs", "glob", "read_blob", "read_csv", "read_csv_auto",
    "read_json", "read_json_auto", "read_ndjson", "read_parquet", "read_text",
    "url_decode", "create_secret",
}
NONDETERMINISTIC_FUNCTIONS = {
    "rand", "random", "gen_random_uuid", "uuid", "current_setting", "setseed",
}
logger = logging.getLogger(__name__)


class ToolError(ValueError):
    pass


def execute_sql(
    task_id: str,
    datasets: list[DatasetInfo],
    sql: str,
    title: str = "查询结果",
    run_id: str | None = None,
) -> ToolExecutionResult:
    started_at = time.perf_counter()
    allowed = {item.table_name for item in datasets}
    physical_tables = _validate_sql(sql, allowed)
    query_hash = hashlib.sha256(re.sub(r"\s+", " ", sql.strip()).encode("utf-8")).hexdigest()
    log_event(
        logger,
        "query.started",
        task_id=task_id,
        run_id=run_id,
        query_hash=query_hash,
        table_count=len(physical_tables),
    )
    db_path = task_dir(task_id) / "work" / "analysis.duckdb"
    result: list[dict[str, Any]] | None = None
    error: Exception | None = None
    active_connection: duckdb.DuckDBPyConnection | None = None

    def execute() -> None:
        nonlocal result, error, active_connection
        try:
            with duckdb.connect(str(db_path), read_only=True) as connection:
                active_connection = connection
                connection.execute("SET memory_limit='1GB'")
                limited_sql = f"SELECT * FROM ({sql.rstrip(';')}) AS safe_query LIMIT {settings.max_query_rows + 1}"
                cursor = connection.execute(limited_sql)
                names = [item[0] for item in cursor.description]
                result = [dict(zip(names, map(_json_value, row), strict=True)) for row in cursor.fetchall()]
        except Exception as exc:
            error = exc
        finally:
            active_connection = None

    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    thread.join(settings.query_timeout_seconds)
    if thread.is_alive():
        if active_connection is not None:
            active_connection.interrupt()
        thread.join(2)
        log_event(
            logger,
            "query.timed_out",
            task_id=task_id,
            run_id=run_id,
            query_hash=query_hash,
            duration_ms=duration_ms(started_at),
        )
        raise ToolError(f"查询超过 {settings.query_timeout_seconds} 秒限制")
    if error:
        log_event(
            logger,
            "query.failed",
            task_id=task_id,
            run_id=run_id,
            query_hash=query_hash,
            duration_ms=duration_ms(started_at),
            error_type=type(error).__name__,
        )
        raise ToolError(f"查询执行失败：{error}")
    source_ids = [item.id for item in datasets if item.table_name in physical_tables]
    truncated = len(result or []) > settings.max_query_rows
    result = (result or [])[:settings.max_query_rows]
    persisted = _persist_result(
        task_id, "execute_sql", {"sql": sql}, title, result, run_id,
        source_dataset_ids=source_ids, query=sql,
    )
    log_event(
        logger,
        "query.completed",
        task_id=task_id,
        run_id=run_id,
        query_hash=query_hash,
        row_count=len(result or []),
        duration_ms=duration_ms(started_at),
    )
    persisted.execution_metadata = {'returned_rows': len(result), 'truncated': truncated,
                                    'row_limit': settings.max_query_rows,
                                    'null_counts': {key: sum(row.get(key) is None for row in result)
                                                    for key in (result[0] if result else {})}}
    return persisted


def _validate_sql(sql: str, allowed_tables: set[str]) -> set[str]:
    if not sql.strip():
        raise ToolError("查询包含禁止的 SQL 操作")
    try:
        expressions = parse(sql, read="duckdb")
    except Exception as exc:
        raise ToolError(f"SQL 语法无效：{exc}") from exc
    if len(expressions) != 1 or not isinstance(expressions[0], (exp.Select, exp.Union, exp.Subquery)):
        raise ToolError("仅允许一条只读 SELECT 查询")
    function_names = {
        (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
        for node in expressions[0].find_all(exp.Func)
    }
    if function_names & (FORBIDDEN_FUNCTIONS | NONDETERMINISTIC_FUNCTIONS):
        raise ToolError("查询包含禁止的 SQL 函数")
    tables = {table.name for table in expressions[0].find_all(exp.Table)}
    cte_names = {cte.alias_or_name for cte in expressions[0].find_all(exp.CTE)}
    unknown = tables - allowed_tables - cte_names
    if unknown:
        raise ToolError(f"查询引用了未授权的数据表：{', '.join(sorted(unknown))}")
    physical_tables = tables & allowed_tables
    if not physical_tables:
        raise ToolError("查询必须引用当前任务中的数据表")
    return physical_tables


def _persist_result(
    task_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    title: str,
    rows: list[dict[str, Any]],
    run_id: str | None = None,
    *,
    source_dataset_ids: list[str] | None = None,
    query: str | None = None,
) -> ToolExecutionResult:
    evidence_id = f"ev_{uuid.uuid4().hex[:12]}"
    clean_rows = [{key: _json_value(value) for key, value in row.items()} for row in rows]
    columns = list(clean_rows[0]) if clean_rows else []
    data_revision = (
        repository.get_run_by_id(run_id).data_revision
        if run_id
        else repository.get_task(task_id).data_revision
    )
    normalized_query = re.sub(r"\s+", " ", query.strip()) if query else None
    provenance = repository.source_revision_provenance(task_id, source_dataset_ids or [])
    query_fingerprint = (
        "|".join([normalized_query or "", *(content_hash for _, content_hash in provenance)])
        if normalized_query else None
    )
    repository.add_evidence(
        EvidenceRecord(
            id=evidence_id,
            task_id=task_id,
            run_id=run_id,
            title=title,
            source=tool_name,
            columns=columns,
            rows=clean_rows,
            data_revision=data_revision,
            source_dataset_ids=source_dataset_ids or [],
            source_revision_ids=[revision_id for revision_id, _ in provenance],
            query=normalized_query,
            query_hash=(
                hashlib.sha256(query_fingerprint.encode("utf-8")).hexdigest()
                if query_fingerprint else None
            ),
        )
    )
    return ToolExecutionResult(
        result_id=f"res_{uuid.uuid4().hex[:12]}",
        tool_name=tool_name,
        arguments=arguments,
        status="success",
        summary=f"{title}，共 {len(clean_rows)} 行结果。",
        rows=clean_rows,
        evidence_ids=[evidence_id],
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return format(value, ".12g")
    return value
