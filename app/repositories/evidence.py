"""Evidence persistence boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..repository import Repository


class EvidenceRepository:
    def __init__(self, backend: "Repository") -> None:
        self._backend = backend

    def get_evidence(self, task_id: str, evidence_id: str) -> Any:
        return self._backend.get_evidence(task_id, evidence_id)

    def evidence_ids(self, task_id: str) -> Any:
        return self._backend.evidence_ids(task_id)

