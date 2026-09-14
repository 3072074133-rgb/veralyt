"""Dataset persistence boundary.

This adapter is intentionally thin: the existing SQLite implementation remains
the source of truth while callers depend on a domain specific interface.  It
provides a safe seam for moving dataset queries out of the monolithic
``repository.py`` incrementally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..repository import Repository


class DatasetRepository:
    """Dataset focused facade over the legacy repository implementation."""

    def __init__(self, backend: "Repository") -> None:
        self._backend = backend

    def get_task_dataset(self, task_id: str, dataset_id: str | None = None) -> Any:
        return self._backend.get_task_dataset(task_id, dataset_id)

    def get_data_asset(self, dataset_id: str) -> Any:
        return self._backend.get_data_asset(dataset_id)

    def revision_table_records(self, dataset_id: str, revision_id: str) -> Any:
        return self._backend.revision_table_records(dataset_id, revision_id)

    def publish_dataset_correction(self, *args: Any, **kwargs: Any) -> Any:
        return self._backend.publish_dataset_correction(*args, **kwargs)

    def attach_revision_to_task(self, *args: Any, **kwargs: Any) -> Any:
        return self._backend.attach_revision_to_task(*args, **kwargs)

