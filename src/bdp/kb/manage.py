"""知识库单文档管理（增 / 删 / 改）。

与批量入库（ingest.py）的区别：批量入库是全量重建，单文档操作必须做到
"只动这一篇"——先删掉该文档的旧切片，再按当前切片策略重建其向量，
保证知识库内容与文档内容实时一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bdp.kb.chunking import chunk_text
from bdp.kb.embedding import SparseEncoder, default_embedder
from bdp.kb.store import VectorRecord, VectorStore, default_store
from bdp.kb.ingest import DEFAULT_STRATEGY
from bdp.models import KbChunk, KbDocument

KB_TYPES = ("cs_faq", "product", "policy", "sop")


class KbManageError(ValueError):
    pass


def _new_doc_id() -> str:
    return f"DOC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def upsert_document(
    session: Session,
    *,
    store: VectorStore | None = None,
    doc_id: str | None = None,
    tenant_id: str,
    kb_type: str,
    source_id: str,
    title: str,
    content: str,
    strategy: str = DEFAULT_STRATEGY,
    commit: bool = True,
) -> dict:
    """新增或更新一篇知识文档，并同步更新其向量。

    commit=True（默认）时写后立即提交——单文档操作要求"写完立即可检索"；
    agent / 批处理路径传 commit=False，把事务边界交给上层上下文统一管理。
    """
    if kb_type not in KB_TYPES:
        raise KbManageError(f"未知知识域：{kb_type}，可选 {KB_TYPES}")
    if not (title or "").strip():
        raise KbManageError("标题不能为空")
    if not (content or "").strip():
        raise KbManageError("内容不能为空")

    store = store or default_store()
    embedder = default_embedder()
    is_update = doc_id is not None

    if is_update:
        existing = session.execute(
            select(KbDocument).where(KbDocument.doc_id == doc_id)
        ).scalar_one_or_none()
        if existing is None:
            raise KbManageError(f"文档 {doc_id} 不存在")
        if existing.tenant_id != tenant_id:
            # 不允许跨租户改写他人知识 —— 与服务层的租户约束一致
            raise KbManageError(f"文档 {doc_id} 不属于当前租户，拒绝改写")
        tenant_id = existing.tenant_id
    else:
        doc_id = _new_doc_id()

    # ---- 先清理旧切片（向量 + 元数据），保证幂等 ----
    old_chunk_ids = [
        c for (c,) in session.execute(select(KbChunk.chunk_id).where(KbChunk.doc_id == doc_id)).all()
    ]
    if old_chunk_ids:
        store.delete(old_chunk_ids)
    session.execute(delete(KbChunk).where(KbChunk.doc_id == doc_id))

    # ---- 写文档 ----
    now = datetime.now()
    if is_update:
        existing.title = title
        existing.content = content
        existing.kb_type = kb_type
        existing.source_id = source_id
        existing.updated_at = now
    else:
        session.add(
            KbDocument(
                doc_id=doc_id, tenant_id=tenant_id, kb_type=kb_type,
                source_id=source_id, title=title, content=content, updated_at=now,
            )
        )

    # ---- 切片 + 向量化 + 入库 ----
    chunks = chunk_text(content, title=title, strategy=strategy)
    if not chunks:
        raise KbManageError("内容切片后为空，无法入库")

    texts = [c.text for c in chunks]
    vectors = embedder.encode(texts)
    records, metas = [], []
    for chunk, vec in zip(chunks, vectors, strict=True):
        chunk_id = f"{doc_id}#{chunk.chunk_ix:03d}"
        records.append(
            VectorRecord(
                chunk_id=chunk_id, tenant_id=tenant_id, kb_type=kb_type,
                doc_id=doc_id, chunk_ix=chunk.chunk_ix, text=chunk.text,
                dense=vec, sparse=SparseEncoder.encode(chunk.text),
            )
        )
        metas.append(
            dict(chunk_id=chunk_id, doc_id=doc_id, tenant_id=tenant_id, kb_type=kb_type,
                 chunk_ix=chunk.chunk_ix, text=chunk.text, char_len=chunk.char_len,
                 content_hash=chunk.content_hash)
        )

    written = store.upsert(records)
    session.bulk_insert_mappings(KbChunk, metas)
    session.flush()
    if commit:
        # get_db 提供的是"用毕即关"的会话，不自动提交；写操作必须显式提交。
        session.commit()

    return {
        "doc_id": doc_id,
        "tenant_id": tenant_id,
        "kb_type": kb_type,
        "operation": "update" if is_update else "create",
        "chunks": len(chunks),
        "written_to_vector_store": written,
        "updated_at": now.isoformat(timespec="seconds"),
    }


def delete_document(
    session: Session,
    *,
    store: VectorStore | None = None,
    doc_id: str,
    tenant_id: str | None,
    commit: bool = True,
) -> dict:
    """删除一篇文档及其全部切片。"""
    store = store or default_store()
    existing = session.execute(
        select(KbDocument).where(KbDocument.doc_id == doc_id)
    ).scalar_one_or_none()
    if existing is None:
        raise KbManageError(f"文档 {doc_id} 不存在")
    if tenant_id and existing.tenant_id != tenant_id:
        raise KbManageError(f"文档 {doc_id} 不属于当前租户，拒绝删除")

    chunk_ids = [
        c for (c,) in session.execute(select(KbChunk.chunk_id).where(KbChunk.doc_id == doc_id)).all()
    ]
    removed_from_store = store.delete(chunk_ids) if chunk_ids else 0
    session.execute(delete(KbChunk).where(KbChunk.doc_id == doc_id))
    session.execute(delete(KbDocument).where(KbDocument.doc_id == doc_id))
    session.flush()
    if commit:
        session.commit()

    return {
        "doc_id": doc_id,
        "deleted_chunks": len(chunk_ids),
        "removed_from_vector_store": removed_from_store,
        "tenant_id": existing.tenant_id,
    }
