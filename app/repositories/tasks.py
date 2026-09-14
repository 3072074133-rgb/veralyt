"""Task persistence boundary used during repository decomposition."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..repository import Repository


class TaskRepository:
    def __init__(self, backend: "Repository") -> None:
        self._backend = backend

    def create_task(self, *args: Any, **kwargs: Any) -> Any:
        return self._backend.create_task(*args, **kwargs)

    def get_task(self, task_id: str) -> Any:
        return self._backend.get_task(task_id)

    def update_task(self, *args: Any, **kwargs: Any) -> Any:
        return self._backend.update_task(*args, **kwargs)

    def delete_task(self, task_id: str) -> Any:
        return self._backend.delete_task(task_id)

