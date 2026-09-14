from __future__ import annotations

from typing import Any

from .models import DatasetInfo


def planner_catalog(
    datasets: list[DatasetInfo],
) -> list[dict[str, Any]]:
    """Return table schemas in source order for the planning model."""
    catalog = []
    for dataset in datasets:
        catalog.append({
            "dataset_id": dataset.id,
            "display_name": dataset.display_name,
            "row_count": dataset.row_count,
            "fields": [
                {
                    "name": column.name,
                    "type": column.data_type,
                    "sample_values": column.sample_values,
                }
                for column in dataset.columns
            ],
            "field_count": len(dataset.columns),
        })
    return catalog


def query_catalog(
    dataset: DatasetInfo,
) -> dict[str, Any]:
    """Return the complete schema for one model-selected table."""
    return {
        "dataset_id": dataset.id,
        "display_name": dataset.display_name,
        "row_count": dataset.row_count,
        "columns": [
            {
                "name": column.name,
                "display_name": column.display_name,
                "type": column.data_type,
                "unit": column.unit or column.currency,
                "sample_values": column.sample_values,
            }
            for column in dataset.columns
        ],
        "field_count": len(dataset.columns),
    }
