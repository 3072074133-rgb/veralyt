from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb

from .ingestion import task_dir
from .models import DatasetInfo


def planner_catalog(
    datasets: list[DatasetInfo],
    task_id: str | None = None,
    sample_size: int = 5,
) -> list[dict[str, Any]]:
    """Return every physical table with its exact schema and row-correlated samples."""
    samples: dict[str, list[dict[str, Any]]] = {}
    if task_id:
        db_path = task_dir(task_id) / "work" / "analysis.duckdb"
        if db_path.is_file():
            with duckdb.connect(str(db_path), read_only=True) as connection:
                for dataset in datasets:
                    table = '"' + dataset.table_name.replace('"', '""') + '"'
                    fields = ", ".join(
                        '"' + column.name.replace('"', '""') + '"' for column in dataset.columns
                    )
                    if not fields:
                        samples[dataset.id] = []
                        continue
                    head_size = (sample_size + 1) // 2
                    tail_size = sample_size - head_size
                    rows_by_index: dict[int, dict[str, Any]] = {}
                    for order, limit in (("ASC", head_size), ("DESC", tail_size)):
                        if not limit:
                            continue
                        cursor = connection.execute(
                            f'SELECT rowid AS "__sample_row_index", {fields} FROM {table} '
                            f'ORDER BY rowid {order} LIMIT ?', [limit]
                        )
                        names = [column[0] for column in cursor.description]
                        for row in cursor.fetchall():
                            rendered = dict(zip(names, (_sample_value(value) for value in row), strict=True))
                            rows_by_index[int(rendered["__sample_row_index"])] = rendered
                    samples[dataset.id] = [rows_by_index[index] for index in sorted(rows_by_index)]
    catalog = []
    for dataset in datasets:
        catalog.append({
            "dataset_id": dataset.id,
            "table_name": dataset.table_name,
            "display_name": dataset.display_name,
            "row_count": dataset.row_count,
            "source": dataset.source_region.model_dump(mode="json") if dataset.source_region else None,
            "fields": [
                {
                    "name": column.name,
                    "type": column.data_type,
                }
                for column in dataset.columns
            ],
            "sample_rows": samples.get(dataset.id, []),
            "field_count": len(dataset.columns),
        })
    return catalog


def _sample_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value
