from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any, Iterable

from ollama import Client

from .config import settings
from .models import (
    KnowledgeBaseCreate,
    KnowledgeBaseDetail,
    KnowledgeBaseList,
    KnowledgeBaseRevision,
    KnowledgeBaseSummary,
    KnowledgeBindingItem,
    KnowledgeDocument,
    KnowledgeDocumentInput,
    KnowledgeMatch,
    KnowledgeRevisionCreate,
    TaskStatus,
    TaskKnowledgeBinding,
    utc_now,
)
from .repository import repository


class KnowledgeEmbeddingError(RuntimeError):
    pass


class EmbeddingGateway:
    def __init__(self) -> None:
        self.client = Client(host=settings.ollama_host, trust_env=False, timeout=180)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self.client.embed(
                model=settings.ollama_embedding_model,
                input=texts,
                dimensions=settings.knowledge_embedding_dimensions,
                keep_alive="2m",
            )
        except Exception as exc:
            raise KnowledgeEmbeddingError(
                f"知识向量生成失败，请确认已安装 {settings.ollama_embedding_model}：{exc}"
            ) from exc
        vectors = [list(vector) for vector in response.embeddings]
        if len(vectors) != len(texts):
            raise KnowledgeEmbeddingError("向量模型返回的结果数量与知识片段不一致")
        return vectors


embedding_gateway = EmbeddingGateway()


def create_knowledge_base(request: KnowledgeBaseCreate) -> KnowledgeBaseDetail:
    knowledge_base_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    now = utc_now()
    prepared = _prepare_documents(request.documents, revision_id, now)
    content_hash = _knowledge_hash(request.documents)
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO knowledge_bases(
            id,owner_id,name,description,status,latest_revision,created_at,updated_at)
            VALUES (?,'local',?,?,'active',1,?,?)""",
            (knowledge_base_id, request.name.strip(), request.description.strip(), now, now),
        )
        connection.execute(
            """INSERT INTO knowledge_base_revisions(
            id,knowledge_base_id,revision_number,parent_revision_id,status,embedding_model,
            embedding_dimensions,content_hash,change_summary,created_at)
            VALUES (?,?,1,NULL,'published',?,?,?,?,?)""",
            (
                revision_id,
                knowledge_base_id,
                settings.ollama_embedding_model,
                settings.knowledge_embedding_dimensions,
                content_hash,
                request.change_summary.strip(),
                now,
            ),
        )
        _insert_prepared(connection, prepared, revision_id, now)
        connection.execute(
            """INSERT OR IGNORE INTO resource_permissions(
            resource_type,resource_id,principal_id,role,created_at)
            VALUES ('knowledge_base',?,'local','owner',?)""",
            (knowledge_base_id, now),
        )
    return get_knowledge_base(knowledge_base_id)


def publish_knowledge_revision(
    knowledge_base_id: str, request: KnowledgeRevisionCreate
) -> KnowledgeBaseDetail:
    now = utc_now()
    revision_id = str(uuid.uuid4())
    prepared = _prepare_documents(request.documents, revision_id, now)
    content_hash = _knowledge_hash(request.documents)
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        base = connection.execute(
            "SELECT * FROM knowledge_bases WHERE id=?", (knowledge_base_id,)
        ).fetchone()
        if base is None:
            raise KeyError(knowledge_base_id)
        if base["status"] != "active":
            raise ValueError("已归档的知识库不能发布新版本")
        previous = connection.execute(
            """SELECT id,content_hash FROM knowledge_base_revisions
            WHERE knowledge_base_id=? ORDER BY revision_number DESC LIMIT 1""",
            (knowledge_base_id,),
        ).fetchone()
        if previous and previous["content_hash"] == content_hash:
            raise ValueError("知识内容没有变化，无需发布新版本")
        revision_number = int(base["latest_revision"]) + 1
        connection.execute(
            """INSERT INTO knowledge_base_revisions(
            id,knowledge_base_id,revision_number,parent_revision_id,status,embedding_model,
            embedding_dimensions,content_hash,change_summary,created_at)
            VALUES (?,?,?,?,'published',?,?,?,?,?)""",
            (
                revision_id,
                knowledge_base_id,
                revision_number,
                previous["id"] if previous else None,
                settings.ollama_embedding_model,
                settings.knowledge_embedding_dimensions,
                content_hash,
                request.change_summary.strip(),
                now,
            ),
        )
        _insert_prepared(connection, prepared, revision_id, now)
        connection.execute(
            "UPDATE knowledge_bases SET latest_revision=?,updated_at=? WHERE id=?",
            (revision_number, now, knowledge_base_id),
        )
    return get_knowledge_base(knowledge_base_id)


def list_knowledge_bases(include_archived: bool = False) -> KnowledgeBaseList:
    clause = "" if include_archived else "WHERE kb.status='active'"
    with repository.connect() as connection:
        rows = connection.execute(
            f"""SELECT kb.*,
            (SELECT COUNT(*) FROM knowledge_documents d
             JOIN knowledge_base_revisions r ON r.id=d.revision_id
             WHERE r.knowledge_base_id=kb.id AND r.revision_number=kb.latest_revision) document_count,
            (SELECT COUNT(*) FROM knowledge_chunks c
             JOIN knowledge_base_revisions r ON r.id=c.revision_id
             WHERE r.knowledge_base_id=kb.id AND r.revision_number=kb.latest_revision) chunk_count
            FROM knowledge_bases kb {clause} ORDER BY kb.updated_at DESC"""
        ).fetchall()
    items = [_summary(row) for row in rows]
    return KnowledgeBaseList(items=items, total=len(items))


def get_knowledge_base(knowledge_base_id: str) -> KnowledgeBaseDetail:
    with repository.connect() as connection:
        base = connection.execute(
            """SELECT kb.*,
            (SELECT COUNT(*) FROM knowledge_documents d
             JOIN knowledge_base_revisions r ON r.id=d.revision_id
             WHERE r.knowledge_base_id=kb.id AND r.revision_number=kb.latest_revision) document_count,
            (SELECT COUNT(*) FROM knowledge_chunks c
             JOIN knowledge_base_revisions r ON r.id=c.revision_id
             WHERE r.knowledge_base_id=kb.id AND r.revision_number=kb.latest_revision) chunk_count
            FROM knowledge_bases kb WHERE kb.id=?""",
            (knowledge_base_id,),
        ).fetchone()
        if base is None:
            raise KeyError(knowledge_base_id)
        revision_rows = connection.execute(
            """SELECT * FROM knowledge_base_revisions WHERE knowledge_base_id=?
            ORDER BY revision_number DESC""",
            (knowledge_base_id,),
        ).fetchall()
        revisions: list[KnowledgeBaseRevision] = []
        for row in revision_rows:
            documents = connection.execute(
                """SELECT d.*,(SELECT COUNT(*) FROM knowledge_chunks c
                WHERE c.document_id=d.id) chunk_count
                FROM knowledge_documents d WHERE d.revision_id=? ORDER BY d.created_at,d.id""",
                (row["id"],),
            ).fetchall()
            revisions.append(
                KnowledgeBaseRevision(
                    id=row["id"],
                    knowledge_base_id=row["knowledge_base_id"],
                    revision_number=row["revision_number"],
                    parent_revision_id=row["parent_revision_id"],
                    status=row["status"],
                    embedding_model=row["embedding_model"],
                    embedding_dimensions=row["embedding_dimensions"],
                    content_hash=row["content_hash"],
                    change_summary=row["change_summary"],
                    documents=[KnowledgeDocument(**dict(document)) for document in documents],
                    created_at=row["created_at"],
                )
            )
    summary = _summary(base)
    return KnowledgeBaseDetail(**summary.model_dump(), revisions=revisions, permission="owner")


def archive_knowledge_base(knowledge_base_id: str) -> None:
    with repository.connect() as connection:
        cursor = connection.execute(
            "UPDATE knowledge_bases SET status='archived',updated_at=? WHERE id=?",
            (utc_now(), knowledge_base_id),
        )
    if cursor.rowcount == 0:
        raise KeyError(knowledge_base_id)


def replace_task_bindings(task_id: str, bindings: list[KnowledgeBindingItem]) -> list[TaskKnowledgeBinding]:
    now = utc_now()
    unique = {item.knowledge_base_id: item for item in bindings}
    if len(unique) != len(bindings):
        raise ValueError("同一个知识库不能重复绑定")
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
            raise KeyError(task_id)
        previous = {
            (row["knowledge_base_id"], row["revision_id"])
            for row in connection.execute(
                "SELECT knowledge_base_id,revision_id FROM task_knowledge_bindings WHERE task_id=?",
                (task_id,),
            ).fetchall()
        }
        replacement = {(item.knowledge_base_id, item.revision_id) for item in bindings}
        for item in bindings:
            valid = connection.execute(
                """SELECT 1 FROM knowledge_base_revisions r
                JOIN knowledge_bases kb ON kb.id=r.knowledge_base_id
                WHERE r.id=? AND r.knowledge_base_id=? AND kb.status='active'""",
                (item.revision_id, item.knowledge_base_id),
            ).fetchone()
            if valid is None:
                raise ValueError("知识库版本不存在或已归档")
        connection.execute("DELETE FROM task_knowledge_bindings WHERE task_id=?", (task_id,))
        connection.executemany(
            """INSERT INTO task_knowledge_bindings(
            task_id,knowledge_base_id,revision_id,bound_at) VALUES(?,?,?,?)""",
            [(task_id, item.knowledge_base_id, item.revision_id, now) for item in bindings],
        )
        if previous != replacement:
            connection.execute("UPDATE execution_runs SET is_active=0 WHERE task_id=?", (task_id,))
            connection.execute(
                """UPDATE tasks SET active_run_id=NULL,result_json=NULL,clarification_question=NULL,
                error=NULL,status=?,progress=15,status_message=?,updated_at=? WHERE id=?""",
                (TaskStatus.READY, "知识库已更新，可以重新分析", now, task_id),
            )
            connection.execute(
                """INSERT INTO events(task_id,event_type,status,progress,message,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    task_id,
                    "knowledge.bindings.updated",
                    TaskStatus.READY,
                    15,
                    "知识库已更新，可以重新分析",
                    json.dumps(
                        {
                            "knowledge_revision_ids": sorted(
                                revision_id for _, revision_id in replacement
                            )
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
    return list_task_bindings(task_id)


def list_task_bindings(task_id: str) -> list[TaskKnowledgeBinding]:
    with repository.connect() as connection:
        rows = connection.execute(
            """SELECT b.knowledge_base_id,kb.name knowledge_base_name,b.revision_id,
            r.revision_number,r.embedding_model,b.bound_at
            FROM task_knowledge_bindings b
            JOIN knowledge_bases kb ON kb.id=b.knowledge_base_id
            JOIN knowledge_base_revisions r ON r.id=b.revision_id
            WHERE b.task_id=? ORDER BY kb.name""",
            (task_id,),
        ).fetchall()
    return [TaskKnowledgeBinding(**dict(row)) for row in rows]


def retrieve_for_run(run_id: str, question: str) -> list[KnowledgeMatch]:
    snapshot = repository.get_run_input_snapshot(run_id)
    revision_ids = list(snapshot.get("knowledge_revision_ids", []))
    if not revision_ids:
        return []
    query_vector = embedding_gateway.embed([question])[0]
    placeholders = ",".join("?" for _ in revision_ids)
    with repository.connect() as connection:
        rows = connection.execute(
            f"""SELECT c.id chunk_id,c.content,c.document_id,d.title document_title,
            c.revision_id,r.revision_number,r.knowledge_base_id,kb.name knowledge_base_name,
            c.embedding_json
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.id=c.document_id
            JOIN knowledge_base_revisions r ON r.id=c.revision_id
            JOIN knowledge_bases kb ON kb.id=r.knowledge_base_id
            WHERE c.revision_id IN ({placeholders})""",
            revision_ids,
        ).fetchall()
    scored = [(_cosine(query_vector, json.loads(row["embedding_json"])), row) for row in rows]
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [item for item in scored if item[0] >= settings.knowledge_retrieval_min_score]
    selected = selected[: settings.knowledge_retrieval_limit]
    selected = _fit_context_budget(selected, settings.knowledge_context_max_chars)
    now = utc_now()
    matches = [
        KnowledgeMatch(
            chunk_id=row["chunk_id"],
            knowledge_base_id=row["knowledge_base_id"],
            knowledge_base_name=row["knowledge_base_name"],
            revision_id=row["revision_id"],
            revision_number=row["revision_number"],
            document_id=row["document_id"],
            document_title=row["document_title"],
            content=row["content"],
            score=round(score, 6),
            rank=index,
        )
        for index, (score, row) in enumerate(selected, start=1)
    ]
    with repository.connect() as connection:
        connection.execute("DELETE FROM run_knowledge_matches WHERE run_id=?", (run_id,))
        connection.executemany(
            """INSERT INTO run_knowledge_matches(
            run_id,chunk_id,revision_id,score,rank,created_at) VALUES(?,?,?,?,?,?)""",
            [(run_id, item.chunk_id, item.revision_id, item.score, item.rank, now) for item in matches],
        )
    return matches


def list_run_matches(run_id: str) -> list[KnowledgeMatch]:
    with repository.connect() as connection:
        rows = connection.execute(
            """SELECT c.id chunk_id,c.content,c.document_id,d.title document_title,
            m.revision_id,r.revision_number,r.knowledge_base_id,kb.name knowledge_base_name,
            m.score,m.rank FROM run_knowledge_matches m
            JOIN knowledge_chunks c ON c.id=m.chunk_id
            JOIN knowledge_documents d ON d.id=c.document_id
            JOIN knowledge_base_revisions r ON r.id=m.revision_id
            JOIN knowledge_bases kb ON kb.id=r.knowledge_base_id
            WHERE m.run_id=? ORDER BY m.rank""",
            (run_id,),
        ).fetchall()
    return [KnowledgeMatch(**dict(row)) for row in rows]


def _prepare_documents(
    documents: list[KnowledgeDocumentInput], revision_id: str, now: str
) -> list[tuple[str, KnowledgeDocumentInput, list[tuple[str, str, list[float]]]]]:
    prepared: list[tuple[str, KnowledgeDocumentInput, list[tuple[str, str, list[float]]]]] = []
    all_chunks: list[tuple[str, int, str]] = []
    for document in documents:
        document_id = str(uuid.uuid4())
        chunks = _chunk_text(document.content)
        for index, content in enumerate(chunks):
            all_chunks.append((document_id, index, content))
        prepared.append((document_id, document, []))
    vectors: list[list[float]] = []
    for batch in _batches([item[2] for item in all_chunks], 16):
        vectors.extend(embedding_gateway.embed(batch))
    vector_by_chunk = {
        (document_id, index): vector
        for (document_id, index, _), vector in zip(all_chunks, vectors, strict=True)
    }
    result = []
    for document_id, document, _ in prepared:
        chunks = [
            (str(uuid.uuid4()), content, vector_by_chunk[(doc_id, index)])
            for doc_id, index, content in all_chunks
            if doc_id == document_id
        ]
        result.append((document_id, document, chunks))
    return result


def _insert_prepared(connection, prepared, revision_id: str, now: str) -> None:
    for document_id, document, chunks in prepared:
        connection.execute(
            """INSERT INTO knowledge_documents(
            id,revision_id,title,content,source_name,created_at) VALUES(?,?,?,?,?,?)""",
            (
                document_id,
                revision_id,
                document.title.strip(),
                document.content.strip(),
                document.source_name,
                now,
            ),
        )
        connection.executemany(
            """INSERT INTO knowledge_chunks(
            id,revision_id,document_id,chunk_index,content,embedding_json,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            [
                (chunk_id, revision_id, document_id, index, content, json.dumps(vector), now)
                for index, (chunk_id, content, vector) in enumerate(chunks)
            ],
        )


def _chunk_text(content: str) -> list[str]:
    text = "\n".join(line.rstrip() for line in content.strip().splitlines())
    size = settings.knowledge_chunk_size
    overlap = min(settings.knowledge_chunk_overlap, max(0, size // 3))
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks or [text]


def _knowledge_hash(documents: list[KnowledgeDocumentInput]) -> str:
    payload = [document.model_dump(mode="json") for document in documents]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    if denominator == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator


def _batches(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _fit_context_budget(
    scored_rows: list[tuple[float, Any]], max_chars: int
) -> list[tuple[float, Any]]:
    if max_chars <= 0:
        return []
    selected = []
    used = 0
    for score, row in scored_rows:
        content_length = len(row["content"])
        if used + content_length > max_chars:
            continue
        selected.append((score, row))
        used += content_length
        if used >= max_chars:
            break
    return selected


def _summary(row) -> KnowledgeBaseSummary:
    return KnowledgeBaseSummary(
        id=row["id"],
        owner_id=row["owner_id"],
        name=row["name"],
        description=row["description"],
        status=row["status"],
        latest_revision=row["latest_revision"],
        document_count=row["document_count"],
        chunk_count=row["chunk_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
