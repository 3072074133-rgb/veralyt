from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from .ingestion import task_dir
from .models import (
    ColumnMetadataCorrection,
    DatasetColumn,
    DatasetColumnProfile,
    DatasetCorrectionRequest,
    DatasetCorrectionResult,
    DatasetInfo,
    DatasetPreview,
    DatasetProfile,
    UploadedFile,
)
from .repository import repository
from .repositories.datasets import DatasetRepository
from .row_identity import (
    INTERNAL_ROW_ID,
    ensure_internal_row_id,
    initialize_row_sequence,
    reserve_row_ids,
    table_has_internal_row_id,
)


dataset_repository = DatasetRepository(repository)


def preview_dataset(
    task_id: str,
    dataset_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    sort_by: str | None = None,
    sort_direction: str = "asc",
) -> DatasetPreview:
    snapshot = repository.get_task(task_id)
    dataset = dataset_repository.get_task_dataset(task_id, dataset_id)
    allowed = {column.name for column in dataset.columns}
    if sort_by and sort_by not in allowed:
        raise ValueError("排序字段不存在")
    offset = (page - 1) * page_size
    db_path = task_dir(task_id) / "work" / "analysis.duckdb"
    select_columns = ",".join(f'"{_quote_identifier(column.name)}"' for column in dataset.columns)
    with duckdb.connect(str(db_path), read_only=True) as connection:
        row_id = (
            f'"{_quote_identifier(INTERNAL_ROW_ID)}"'
            if table_has_internal_row_id(connection, dataset.table_name)
            else "rowid + 1"
        )
        order_parts = []
        if sort_by:
            order_parts.append(
                f'"{_quote_identifier(sort_by)}" '
                f'{"DESC" if sort_direction == "desc" else "ASC"} NULLS LAST'
            )
        order_parts.append(row_id)
        order = " ORDER BY " + ",".join(order_parts)
        query = (
            f'SELECT {row_id} AS __row_id,{select_columns} '
            f'FROM "{_quote_identifier(dataset.table_name)}"{order} LIMIT ? OFFSET ?'
        )
        rows = _records(connection.execute(query, [page_size, offset]))
    return DatasetPreview(
        dataset_id=dataset.id,
        display_name=dataset.display_name,
        columns=dataset.columns,
        rows=rows,
        page=page,
        page_size=page_size,
        total=dataset.row_count,
        data_revision=snapshot.data_revision,
    )


def profile_dataset(task_id: str, dataset_id: str) -> DatasetProfile:
    snapshot = repository.get_task(task_id)
    dataset = dataset_repository.get_task_dataset(task_id, dataset_id)
    db_path = task_dir(task_id) / "work" / "analysis.duckdb"
    table = f'"{_quote_identifier(dataset.table_name)}"'
    columns: list[DatasetColumnProfile] = []
    with duckdb.connect(str(db_path), read_only=True) as connection:
        hash_columns = ",".join(
            f'"{_quote_identifier(column.name)}"' for column in dataset.columns
        )
        duplicate_count = int(connection.execute(
            f"SELECT COUNT(*)-COUNT(DISTINCT hash({hash_columns})) FROM {table}"
        ).fetchone()[0])
        for column in dataset.columns:
            name = f'"{_quote_identifier(column.name)}"'
            row = connection.execute(
                f"""SELECT COUNT(*) FILTER (WHERE {name} IS NULL),
                COUNT(DISTINCT {name}),MIN({name}),MAX({name}) FROM {table}"""
            ).fetchone()
            samples = connection.execute(
                f"SELECT DISTINCT {name} FROM {table} WHERE {name} IS NOT NULL LIMIT 5"
            ).fetchall()
            columns.append(DatasetColumnProfile(
                name=column.name,
                display_name=column.display_name,
                data_type=column.data_type,
                null_count=int(row[0]),
                null_ratio=(int(row[0]) / dataset.row_count) if dataset.row_count else 0,
                distinct_count=int(row[1]),
                minimum=_json_value(row[2]),
                maximum=_json_value(row[3]),
                sample_values=[_json_value(value[0]) for value in samples],
            ))
    return DatasetProfile(
        dataset_id=dataset.id,
        row_count=dataset.row_count,
        duplicate_count=duplicate_count,
        columns=columns,
        data_revision=snapshot.data_revision,
    )


def correct_dataset(
    task_id: str, dataset_id: str, request: DatasetCorrectionRequest
) -> DatasetCorrectionResult:
    current = dataset_repository.get_task_dataset(task_id, dataset_id)
    snapshot = repository.get_task(task_id)
    if snapshot.data_revision != request.expected_data_revision:
        raise RuntimeError("数据已被其他操作更新，请刷新预览后重试")
    if not request.cell_updates and not request.metadata_updates and not request.deleted_row_ids and not request.added_rows:
        raise ValueError("没有需要发布的修改")

    db_path = task_dir(task_id) / "work" / "analysis.duckdb"
    with duckdb.connect(str(db_path), read_only=True) as connection:
        row_id = (
            f'"{_quote_identifier(INTERNAL_ROW_ID)}"'
            if table_has_internal_row_id(connection, current.table_name)
            else "rowid + 1"
        )
        business_columns = [column.name for column in current.columns]
        selected_columns = ",".join(
            f'"{_quote_identifier(name)}"' for name in business_columns
        )
        result = connection.execute(
            f'SELECT {row_id} AS "{INTERNAL_ROW_ID}",{selected_columns} '
            f'FROM "{_quote_identifier(current.table_name)}"'
        )
        names = [item[0] for item in result.description]
        frame = pl.DataFrame(result.fetchall(), schema=names, orient="row")
    known_columns = set(business_columns)
    known_row_ids = set(frame[INTERNAL_ROW_ID].to_list())
    minimum_next_row = int(frame[INTERNAL_ROW_ID].max() or 0) + 1
    with duckdb.connect(str(db_path)) as connection:
        reserve_row_ids(connection, dataset_id, 0, minimum_next_row)
    for update in request.cell_updates:
        if update.column not in known_columns:
            raise ValueError(f"字段不存在：{update.column}")
        if update.row_id not in known_row_ids:
            raise ValueError(f"行不存在：{update.row_id}")
        dtype = frame.schema[update.column]
        try:
            frame = frame.with_columns(
                pl.when(pl.col(INTERNAL_ROW_ID) == update.row_id)
                .then(pl.lit(update.value).cast(dtype, strict=True))
                .otherwise(pl.col(update.column))
                .alias(update.column)
            )
        except Exception as exc:
            raise ValueError(f"{update.column} 的新值与字段类型不匹配") from exc
    if request.deleted_row_ids:
        invalid = [row_id for row_id in request.deleted_row_ids if row_id not in known_row_ids]
        if invalid:
            raise ValueError(f"待删除行不存在：{invalid[0]}")
        frame = frame.filter(~pl.col(INTERNAL_ROW_ID).is_in(request.deleted_row_ids))
    if request.added_rows:
        normalized = []
        for source in request.added_rows:
            unknown = set(source) - known_columns
            if unknown:
                raise ValueError(f"新增行包含未知字段：{sorted(unknown)[0]}")
            normalized.append({name: source.get(name) for name in business_columns})
        try:
            additions = pl.DataFrame(
                normalized,
                schema={name: frame.schema[name] for name in business_columns},
            )
        except Exception as exc:
            raise ValueError("新增行中的值与字段类型不匹配") from exc
        with duckdb.connect(str(db_path)) as connection:
            next_row = reserve_row_ids(
                connection, dataset_id, len(additions), minimum_next_row
            )
        additions = additions.with_row_index(INTERNAL_ROW_ID, offset=next_row).with_columns(
            pl.col(INTERNAL_ROW_ID).cast(frame.schema[INTERNAL_ROW_ID])
        )
        frame = pl.concat([frame, additions.select(frame.columns)], how="vertical")

    columns = _updated_columns(current.columns, request.metadata_updates, frame)
    published = frame
    revision_token = uuid.uuid4().hex[:12]
    table_name = f"{current.table_name}_r{revision_token}"
    output_path = task_dir(task_id) / "work" / f"{dataset_id}_{revision_token}.parquet"
    published.write_parquet(output_path)
    try:
        with duckdb.connect(str(db_path)) as connection:
            connection.execute(
                f'CREATE TABLE "{_quote_identifier(table_name)}" AS SELECT * FROM read_parquet(?)',
                [str(output_path)],
            )
        revised = current.model_copy(update={
            "table_name": table_name,
            "row_count": published.height,
            "columns": columns,
        })
        content_hash = _file_hash(output_path)
        data_revision, asset_id, revision_id, revision_number = dataset_repository.publish_dataset_correction(
            task_id, dataset_id, revised, output_path, content_hash,
            request.change_summary, request.expected_data_revision,
        )
    except Exception:
        output_path.unlink(missing_ok=True)
        try:
            with duckdb.connect(str(db_path)) as connection:
                connection.execute(f'DROP TABLE IF EXISTS "{_quote_identifier(table_name)}"')
        except Exception:
            pass
        raise
    return DatasetCorrectionResult(
        dataset=revised,
        data_revision=data_revision,
        dataset_asset_id=asset_id,
        dataset_revision_id=revision_id,
        revision_number=revision_number,
    )


def create_task_from_revision(dataset_id: str, revision_id: str) -> str:
    asset = dataset_repository.get_data_asset(dataset_id)
    revision = next((item for item in asset.revisions if item.id == revision_id), None)
    if revision is None:
        raise KeyError(revision_id)
    records = dataset_repository.revision_table_records(dataset_id, revision_id)
    if not records:
        raise ValueError("数据版本不包含可分析的数据表")
    task_id = repository.create_task()
    directory = task_dir(task_id)
    work_dir = directory / "work"
    work_dir.mkdir(exist_ok=True)
    db_path = work_dir / "analysis.duckdb"
    file_id = str(uuid.uuid4())
    datasets: list[DatasetInfo] = []
    total_size = 0
    try:
        with duckdb.connect(str(db_path)) as connection:
            connection.execute("BEGIN TRANSACTION")
            for index, record in enumerate(records, start=1):
                source_path = Path(record["parquet_path"])
                if not source_path.is_file():
                    raise ValueError(f"版本文件不可用：{source_path.name}")
                total_size += source_path.stat().st_size
                source = DatasetInfo.model_validate_json(record["catalog_json"])
                task_dataset_id = str(uuid.uuid4())
                table_name = f"data_asset_{dataset_id.replace('-', '')[:8]}_{index}"
                connection.execute(
                    f'CREATE TABLE "{_quote_identifier(table_name)}" AS SELECT * FROM read_parquet(?)',
                    [str(source_path)],
                )
                ensure_internal_row_id(connection, table_name)
                initialize_row_sequence(connection, task_dataset_id, table_name)
                datasets.append(source.model_copy(update={
                    "id": task_dataset_id,
                    "file_id": file_id,
                    "table_name": table_name,
                }))
            connection.execute("COMMIT")
        file_record = UploadedFile(
            id=file_id,
            original_name=f"{asset.name}-v{revision.revision_number}",
            size=total_size,
            status="ready",
            sheet_count=len(datasets),
            detected_sheet_count=len(datasets),
            row_count=sum(item.row_count for item in datasets),
        )
        dataset_repository.attach_revision_to_task(
            task_id, asset, revision_id, revision.revision_number, file_record, datasets,
            [record["id"] for record in records],
        )
    except Exception:
        repository.delete_task(task_id)
        root = (task_dir(task_id).parent.parent).resolve()
        resolved = directory.resolve()
        if root in resolved.parents and resolved.exists():
            shutil.rmtree(resolved)
        raise
    return task_id


def _updated_columns(
    columns: list[DatasetColumn], updates: list[ColumnMetadataCorrection], frame: pl.DataFrame
) -> list[DatasetColumn]:
    by_name = {update.column: update for update in updates}
    unknown = set(by_name) - {column.name for column in columns}
    if unknown:
        raise ValueError(f"字段不存在：{sorted(unknown)[0]}")
    result = []
    for column in columns:
        update = by_name.get(column.name)
        values = frame[column.name]
        changes = {
            "null_count": values.null_count(),
            "sample_values": [_json_value(value) for value in values.drop_nulls().head(3).to_list()],
        }
        if update:
            changes.update({
                key: value for key, value in update.model_dump(exclude_none=True).items() if key != "column"
            })
        result.append(column.model_copy(update=changes))
    return result


def _records(result: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    names = [item[0] for item in result.description]
    return [{name: _json_value(value) for name, value in zip(names, row)} for row in result.fetchall()]


def _quote_identifier(value: str) -> str:
    return value.replace('"', '""')


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
