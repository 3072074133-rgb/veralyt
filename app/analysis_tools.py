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
from .dataset_retrieval import is_placeholder_column_name
from .ingestion import task_dir
from .models import DatasetInfo, EvidenceRecord, QuerySpec, ToolExecutionResult
from .observability import duration_ms, log_event
from .repository import repository


FORBIDDEN_SQL = re.compile(
    r"\b(attach|copy|install|load|export|import|pragma|call|create|delete|drop|insert|update|alter)\b",
    re.IGNORECASE,
)
FORBIDDEN_EXTERNAL_ACCESS = re.compile(
    r"\b(http_get|httpfs|glob|read_blob|read_csv|read_json|read_parquet|read_text|url_decode|create_secret)\b|https?://|s3://",
    re.IGNORECASE,
)
FORBIDDEN_NONDETERMINISTIC = re.compile(
    r"\b(random|gen_random_uuid|uuid|current_setting|setseed)\s*\(", re.IGNORECASE
)
logger = logging.getLogger(__name__)


class ToolError(ValueError):
    pass


def profile_table(task_id: str, dataset: DatasetInfo, run_id: str | None = None) -> ToolExecutionResult:
    columns = [
        {
            "字段": item.display_name,
            "类型": item.data_type,
            "业务语义": item.semantic_type,
            "分析角色": item.role,
            "默认汇总": item.default_aggregation,
            "单位": item.unit or item.currency or "",
            "空值数": item.null_count,
            "示例": "、".join(map(str, item.sample_values)),
        }
        for item in dataset.columns
    ]
    return _persist_result(
        task_id, "profile_table", {"dataset_id": dataset.id},
        f"{dataset.display_name} 字段画像", columns, run_id,
        source_dataset_ids=[dataset.id],
    )


def query_from_spec(
    task_id: str,
    datasets: list[DatasetInfo],
    spec: QuerySpec,
    title: str = "查询结果",
    run_id: str | None = None,
) -> ToolExecutionResult:
    dataset_by_id = {item.id: item for item in datasets}
    dataset = dataset_by_id.get(spec.dataset_id)
    if dataset is None:
        raise ToolError("查询引用了不存在的数据集")
    involved_ids = [spec.dataset_id]
    for join in spec.joins:
        if join.right_dataset_id not in dataset_by_id:
            raise ToolError("关联引用了不存在的数据集")
        if join.right_dataset_id not in involved_ids:
            involved_ids.append(join.right_dataset_id)
    involved = [dataset_by_id[item] for item in involved_ids]
    table_aliases = {dataset_id: f"t{index}" for index, dataset_id in enumerate(involved_ids)}

    def resolve_field(field: str) -> tuple[str, str]:
        raw = str(field)
        requested_dataset = None
        if "::" in raw:
            requested_dataset, raw = raw.split("::", 1)
        elif "." in raw:
            prefix, candidate = raw.split(".", 1)
            if prefix in dataset_by_id:
                requested_dataset, raw = prefix, candidate
        candidates = [item for item in involved if item.id == requested_dataset] if requested_dataset else [
            item for item in involved if raw in {column.name for column in item.columns}
        ]
        if not candidates:
            raise ToolError(f"字段不在关联数据集中：{field}")
        if requested_dataset is None and len(candidates) > 1:
            raise ToolError(f"关联查询字段存在歧义，请使用 数据集ID::字段：{field}")
        return table_aliases[candidates[0].id], raw

    def qualified(field: str) -> str:
        alias, name = resolve_field(field)
        return f"{alias}.{_quote(name)}"

    for field in spec.dimensions:
        try:
            resolve_field(field)
        except ToolError as exc:
            raise ToolError(f"查询维度不在数据集中：{field}") from exc
    for measure in spec.measures:
        try:
            resolve_field(measure.field)
        except ToolError as exc:
            raise ToolError(f"查询指标不在数据集中：{measure.field}") from exc
    for item in spec.filters:
        try:
            resolve_field(item.field)
        except ToolError as exc:
            raise ToolError(f"查询筛选字段不在数据集中：{item.field}") from exc

    for join in spec.joins:
        left_ref = join.left_field if ("::" in join.left_field or "." in join.left_field) else f"{spec.dataset_id}::{join.left_field}"
        left_alias, left_name = resolve_field(left_ref)
        right_alias = table_aliases[join.right_dataset_id]
        if join.right_field not in {column.name for column in dataset_by_id[join.right_dataset_id].columns}:
            raise ToolError(f"关联字段不在数据集中：{join.right_field}")
        if left_alias == right_alias:
            raise ToolError("关联左右数据表不能相同")
    join_sql = ""
    for join in spec.joins:
        left_ref = join.left_field if ("::" in join.left_field or "." in join.left_field) else f"{spec.dataset_id}::{join.left_field}"
        left_alias, left_name = resolve_field(left_ref)
        right_alias = table_aliases[join.right_dataset_id]
        join_sql += f" {join.join_type.upper()} JOIN {_quote(dataset_by_id[join.right_dataset_id].table_name)} AS {right_alias} ON {left_alias}.{_quote(left_name)} = {right_alias}.{_quote(join.right_field)}"

    aggregation_sql = {
        "sum": "SUM",
        "average": "AVG",
        "min": "MIN",
        "max": "MAX",
        "count": "COUNT",
    }
    aliases: list[str] = []
    projections = [f"{qualified(field)} AS {_quote(resolve_field(field)[1])}" for field in spec.dimensions]
    for measure in spec.measures:
        alias = measure.alias or f"{measure.field}{measure.aggregation}"
        aliases.append(alias)
        projections.append(f"{aggregation_sql[measure.aggregation]}({qualified(measure.field)}) AS {_quote(alias)}")
    operators = {
        "eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    }
    conditions: list[str] = []
    for item in spec.filters:
        field = qualified(item.field)
        if item.operator == "contains":
            conditions.append(f"CAST({field} AS VARCHAR) LIKE {_literal('%' + str(item.value) + '%')}")
        else:
            conditions.append(f"{field} {operators[item.operator]} {_literal(item.value)}")
    sql = f"SELECT {', '.join(projections)} FROM {_quote(dataset.table_name)} AS t0{join_sql}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    if spec.dimensions:
        sql += " GROUP BY " + ", ".join(qualified(field) for field in spec.dimensions)
    order_fields = set(spec.dimensions) | set(aliases)
    order_by = spec.order_by or aliases[0]
    if order_by not in order_fields:
        raise ToolError("排序字段不在查询输出中")
    sql += f" ORDER BY {_quote(order_by)} {'DESC' if spec.descending else 'ASC'} LIMIT {spec.limit}"
    return query_data(task_id, involved, sql, title, run_id)


def query_department_profit(
    task_id: str,
    dataset: DatasetInfo,
    run_id: str | None = None,
) -> ToolExecutionResult:
    """Extract department-level net profit from a sectioned financial summary."""
    fields = {item.name for item in dataset.columns}
    if not {"Department", "Sum"} <= fields:
        raise ToolError("当前数据表不具备部门盈亏层级结构")
    table = _quote(dataset.table_name)
    sql = f"""
        WITH source AS (
            SELECT rowid AS source_row,
                   TRIM(CAST({_quote('Department')} AS VARCHAR)) AS label,
                   TRY_CAST({_quote('Sum')} AS DOUBLE) AS value
            FROM {table}
        ), sections AS (
            SELECT source_row,
                   TRIM(regexp_replace(label, '(?i)\\s+(Revenue|Income).*$' , '')) AS {_quote('部门')}
            FROM source
            WHERE regexp_matches(label, '(?i)(Revenue|Income)\\s*\\(')
        ), bounds AS (
            SELECT *, LEAD(source_row, 1, 9223372036854775807)
                OVER (ORDER BY source_row) AS next_row
            FROM sections
        )
        SELECT bounds.{_quote('部门')}, profit.value AS {_quote('净利润')}
        FROM bounds
        JOIN source AS profit
          ON profit.source_row > bounds.source_row
         AND profit.source_row < bounds.next_row
         AND regexp_matches(profit.label, '(?i)^Net Profit$')
        WHERE profit.value IS NOT NULL
        ORDER BY profit.value DESC
    """
    return query_data(
        task_id,
        [dataset],
        sql,
        "各部门净利润",
        run_id,
    )


def query_data(
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
                limited_sql = f"SELECT * FROM ({sql.rstrip(';')}) AS safe_query LIMIT {settings.max_query_rows}"
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
    persisted = _persist_result(
        task_id, "query_data", {"sql": sql}, title, result or [], run_id,
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
    return persisted


def auto_analyze(
    task_id: str,
    datasets: list[DatasetInfo],
    question: str,
    run_id: str | None = None,
) -> ToolExecutionResult:
    from .financial_reports import financial_kind, query_financial_report
    if datasets and all(financial_kind(d) for d in datasets):
        return query_financial_report(task_id, datasets, run_id)
    dataset = max(datasets, key=lambda item: item.row_count)
    numeric = [
        c for c in dataset.columns
        if any(token in c.data_type.lower() for token in ("int", "float", "decimal"))
        and c.semantic_type not in {"date", "id"}
    ]
    temporal = [
        c for c in dataset.columns
        if c.semantic_type == "date" or any(token in c.data_type.lower() for token in ("date", "time"))
    ]
    categorical = [
        c for c in dataset.columns
        if c.role == "dimension" and c.semantic_type != "date"
        or (c.role == "unknown" and "str" in c.data_type.lower())
    ]
    if not numeric:
        return profile_table(task_id, dataset, run_id)

    measure = _choose_measure(numeric, question)
    if measure is None:
        raise ToolError(
            "当前数据表没有可识别的业务指标列（仅发现未命名列等占位字段），"
            "请指定要统计的字段，或先在数据集页面修正表头。"
        )
    dimension = _choose_dimension(temporal or categorical, question)
    table = _quote(dataset.table_name)
    measure_sql = _quote(measure.name)
    if dimension:
        dimension_sql = _quote(dimension.name)
        if any(token in dimension.data_type.lower() for token in ("date", "time")):
            category = f"strftime({dimension_sql}, '%Y-%m')"
            category_alias = "期间"
            sql = f"""
                WITH grouped AS (
                    SELECT {category} AS {_quote(category_alias)},
                           SUM(TRY_CAST({measure_sql} AS DOUBLE)) AS {_quote(measure.display_name)},
                           COUNT(*) AS {_quote('记录数')}
                    FROM {table}
                    WHERE {measure_sql} IS NOT NULL
                    GROUP BY 1
                ), compared AS (
                    SELECT *,
                           LAG({_quote(measure.display_name)}) OVER (ORDER BY {_quote(category_alias)}) AS {_quote('上期值')},
                           LAG({_quote(category_alias)}) OVER (ORDER BY {_quote(category_alias)}) AS previous_period,
                           LAG({_quote(measure.display_name)}) OVER (
                               PARTITION BY RIGHT({_quote(category_alias)}, 2)
                               ORDER BY {_quote(category_alias)}
                           ) AS {_quote('上年同期值')},
                           SUM({_quote(measure.display_name)}) OVER (
                               PARTITION BY LEFT({_quote(category_alias)}, 4)
                           ) AS {_quote('年度范围合计')},
                           AVG({_quote(measure.display_name)}) OVER () AS avg_value,
                           STDDEV_POP({_quote(measure.display_name)}) OVER () AS std_value
                    FROM grouped
                )
                SELECT {_quote(category_alias)}, {_quote(measure.display_name)}, {_quote('记录数')},
                       SUM({_quote(measure.display_name)}) OVER () AS {_quote('范围合计')},
                       CASE WHEN date_diff('month', strptime(previous_period || '-01', '%Y-%m-%d'), strptime({_quote(category_alias)} || '-01', '%Y-%m-%d')) = 1
                            THEN {_quote('上期值')} END AS {_quote('上期值')},
                       CASE WHEN date_diff('month', strptime(previous_period || '-01', '%Y-%m-%d'), strptime({_quote(category_alias)} || '-01', '%Y-%m-%d')) = 1
                            THEN ROUND((({_quote(measure.display_name)} / NULLIF({_quote('上期值')}, 0)) - 1) * 100, 2) END AS {_quote('环比百分比')},
                       {_quote('上年同期值')},
                       ROUND((({_quote(measure.display_name)} / NULLIF({_quote('上年同期值')}, 0)) - 1) * 100, 2) AS {_quote('同比百分比')},
                       {_quote('年度范围合计')},
                       1.5 AS {_quote('异常阈值（标准差倍数）')},
                       CASE WHEN std_value > 0 AND ABS({_quote(measure.display_name)} - avg_value) > 1.5 * std_value THEN '是' ELSE '否' END AS {_quote('异常标记')}
                FROM compared ORDER BY {_quote(category_alias)}
            """
        else:
            category = dimension_sql
            category_alias = dimension.display_name
            sql = f"""
                WITH grouped AS (
                    SELECT {category} AS {_quote(category_alias)},
                           SUM(TRY_CAST({measure_sql} AS DOUBLE)) AS {_quote(measure.display_name)},
                           COUNT(*) AS {_quote('记录数')}
                    FROM {table} WHERE {measure_sql} IS NOT NULL GROUP BY 1
                )
                SELECT *,
                       SUM({_quote(measure.display_name)}) OVER () AS {_quote('范围合计')},
                       ROUND(ABS({_quote(measure.display_name)}) / NULLIF(SUM(ABS({_quote(measure.display_name)})) OVER (), 0) * 100, 2) AS {_quote('绝对金额贡献百分比')},
                       RANK() OVER (ORDER BY ABS({_quote(measure.display_name)}) DESC) AS {_quote('绝对影响排名')}
                FROM grouped ORDER BY ABS({_quote(measure.display_name)}) DESC
            """
        title = f"{measure.display_name}按{category_alias}汇总"
    else:
        sql = (
            f"SELECT SUM(TRY_CAST({measure_sql} AS DOUBLE)) AS {_quote(measure.display_name + '合计')}, "
            f"AVG(TRY_CAST({measure_sql} AS DOUBLE)) AS {_quote(measure.display_name + '平均值')}, "
            f"MIN(TRY_CAST({measure_sql} AS DOUBLE)) AS {_quote('最小值')}, "
            f"MAX(TRY_CAST({measure_sql} AS DOUBLE)) AS {_quote('最大值')}, COUNT(*) AS {_quote('记录数')} FROM {table}"
        )
        title = f"{measure.display_name}汇总"
    sql = f'SELECT *, COUNT(*) OVER () AS "结果分组数" FROM ({sql}) AS overview_groups'
    return query_data(task_id, datasets, sql, title, run_id)


def _validate_sql(sql: str, allowed_tables: set[str]) -> set[str]:
    if (
        not sql.strip()
        or FORBIDDEN_SQL.search(sql)
        or FORBIDDEN_EXTERNAL_ACCESS.search(sql)
        or FORBIDDEN_NONDETERMINISTIC.search(sql)
    ):
        raise ToolError("查询包含禁止的 SQL 操作")
    try:
        expressions = parse(sql, read="duckdb")
    except Exception as exc:
        raise ToolError(f"SQL 语法无效：{exc}") from exc
    if len(expressions) != 1 or not isinstance(expressions[0], (exp.Select, exp.Union, exp.Subquery)):
        raise ToolError("仅允许一条只读 SELECT 查询")
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


def _choose_measure(columns: list[Any], question: str) -> Any:
    normalized_question = question.casefold()
    for column in columns:
        # An explicit field request overrides the placeholder safeguard.
        if column.display_name.casefold() in normalized_question:
            return column
    semantic_amount = next(
        (column for column in columns
         if not is_placeholder_column_name(column.name) and column.semantic_type == "amount"),
        None,
    )
    if semantic_amount:
        return semantic_amount
    priorities = (
        "净利润", "net profit", "利润", "profit", "金额", "amount", "收入", "revenue",
        "income", "销售额", "sales", "成本", "cost", "费用", "expense", "回款",
    )
    for token in priorities:
        match = next(
            (column for column in columns
             if not is_placeholder_column_name(column.name)
             and token in column.display_name.casefold()),
            None,
        )
        if match:
            return match
    dimension_names = ("month", "月份", "year", "年度", "date", "日期", "期间", "序号", "排名", " id")
    measure = next(
        (
            column for column in columns
            if not is_placeholder_column_name(column.name)
            and not any(token in f" {column.display_name.casefold()}" for token in dimension_names)
        ),
        None,
    )
    return measure


def _choose_dimension(columns: list[Any], question: str) -> Any | None:
    for column in columns:
        if column.display_name in question:
            return column
    semantic_date = next((column for column in columns if column.semantic_type == "date"), None)
    if semantic_date:
        return semantic_date
    priorities = ("日期", "月份", "期间", "部门", "区域", "产品", "客户")
    for token in priorities:
        match = next((column for column in columns if token in column.display_name), None)
        if match:
            return match
    return columns[0] if columns else None


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return format(value, ".12g")
    return value
