"""知识库入库：文档 → 切片 → 向量化 → 写入向量库。

同时把切片元数据落到关系库（kb_chunk），用于：
- 检索结果回查原文与来源
- 评测时定位 ground truth（同文档的切片视为正样本）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bdp.kb.chunking import chunk_text
from bdp.kb.embedding import SparseEncoder, default_embedder
from bdp.kb.store import VectorRecord, VectorStore, default_store
from bdp.models import KbChunk, KbDocument

# 默认使用层级切片：电商知识文档的标题往往就是用户问题本身（"退货政策"），
# 把标题嵌入每个切片能显著提升召回。切片策略对比实验见 kb/evaluate.py::compare_chunking。
DEFAULT_STRATEGY = "hierarchical"


def ingest_knowledge(
    session: Session,
    *,
    store: VectorStore | None = None,
    tenant_id: str | None = None,
    strategy: str = DEFAULT_STRATEGY,
    rebuild: bool = False,
    batch_size: int = 256,
) -> dict:
    started = datetime.now()
    store = store or default_store()
    embedder = default_embedder()

    stmt = select(KbDocument).order_by(KbDocument.doc_id)
    if tenant_id:
        stmt = stmt.where(KbDocument.tenant_id == tenant_id)
    documents = session.execute(stmt).scalars().all()
    if not documents:
        return {"error": "知识库为空，请先执行 mock 生成数据"}

    if rebuild:
        store.reset()
        session.execute(delete(KbChunk))

    # 清空关系侧切片元数据后重建，保证与向量库一致
    if tenant_id:
        session.execute(delete(KbChunk).where(KbChunk.tenant_id == tenant_id))
    else:
        session.execute(delete(KbChunk))

    total_chunks = 0
    written = 0
    dedup_skipped = 0
    per_type: dict[str, int] = {}
    pending_records: list[VectorRecord] = []
    pending_meta: list[dict] = []
    # 内容哈希去重：同租户同知识域下完全相同的切片只入库一次。
    # 多店铺/多平台共享的"平台规则"类文档重复度很高，不去重会污染检索结果。
    seen_hashes: set[tuple[str, str, str]] = set()

    def flush_batch() -> None:
        nonlocal written
        if not pending_records:
            return
        written += store.upsert(pending_records)
        session.bulk_insert_mappings(KbChunk, pending_meta)
        session.flush()
        pending_records.clear()
        pending_meta.clear()

    for doc in documents:
        chunks = chunk_text(doc.content, title=doc.title, strategy=strategy)
        if not chunks:
            continue
        texts = [c.text for c in chunks]
        vectors = embedder.encode(texts)
        for chunk, vec in zip(chunks, vectors, strict=True):
            dedup_key = (doc.tenant_id, doc.kb_type, chunk.content_hash)
            if dedup_key in seen_hashes:
                dedup_skipped += 1
                continue
            seen_hashes.add(dedup_key)
            chunk_id = f"{doc.doc_id}#{chunk.chunk_ix:03d}"
            pending_records.append(
                VectorRecord(
                    chunk_id=chunk_id,
                    tenant_id=doc.tenant_id,
                    kb_type=doc.kb_type,
                    doc_id=doc.doc_id,
                    chunk_ix=chunk.chunk_ix,
                    text=chunk.text,
                    dense=vec,
                    sparse=SparseEncoder.encode(chunk.text),
                )
            )
            pending_meta.append(
                {
                    "chunk_id": chunk_id,
                    "doc_id": doc.doc_id,
                    "tenant_id": doc.tenant_id,
                    "kb_type": doc.kb_type,
                    "chunk_ix": chunk.chunk_ix,
                    "text": chunk.text,
                    "char_len": chunk.char_len,
                    "content_hash": chunk.content_hash,
                }
            )
            per_type[doc.kb_type] = per_type.get(doc.kb_type, 0) + 1
            total_chunks += 1
            if len(pending_records) >= batch_size:
                flush_batch()
    flush_batch()

    avg_len = (
        session.execute(select(KbChunk.char_len)).scalars().all()
    )
    return {
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 2),
        "documents": len(documents),
        "chunks": total_chunks,
        "written_to_vector_store": written,
        "dedup_skipped": dedup_skipped,
        "strategy": strategy,
        "embedding_backend": embedder.name,
        "vector_backend": store.backend,
        "per_kb_type": per_type,
        "avg_chunk_len": round(sum(avg_len) / len(avg_len), 1) if avg_len else 0,
        "store": store.describe(),
    }
