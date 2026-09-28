"""知识检索服务。

租户隔离的关键点：KB 检索**不接受**"无租户"的调用。
平台级账号（admin）也必须显式指定租户，因为知识内容与品牌强绑定，
跨品牌检索等于直接泄漏。这条规则在 retriever 层也做了二次拦截。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bdp.api.deps import require_roles, tenant_guard
from bdp.api.schemas import (
    KbDocumentUpsert,
    KbSearchHit,
    KbSearchRequest,
    KbSearchResponse,
)
from bdp.db import get_db
from bdp.kb.ingest import ingest_knowledge
from bdp.kb.manage import KbManageError, delete_document, upsert_document
from bdp.kb.retriever import search
from bdp.kb.store import default_store
from bdp.models import KbChunk, KbDocument
from bdp.security.auth import Principal

router = APIRouter(tags=["knowledge"])


@router.post("/v1/kb/search", response_model=KbSearchResponse, summary="知识库检索（多租户隔离）")
def kb_search(
    payload: KbSearchRequest,
    effective_tenant: str | None = Depends(tenant_guard("kb.search", "kb_chunk")),
    session: Session = Depends(get_db),
) -> KbSearchResponse:
    # 平台级身份不能不带租户检索
    if not effective_tenant:
        raise HTTPException(
            status_code=400,
            detail="知识库检索必须指定租户（X-Tenant-Id），平台级身份不支持跨品牌检索",
        )
    if payload.tenant_id and payload.tenant_id != effective_tenant:
        raise HTTPException(status_code=403, detail="请求体租户与身份租户不一致")

    result = search(
        session,
        query=payload.query,
        tenant_id=effective_tenant,
        kb_type=payload.kb_type,
        top_k=payload.top_k,
        mode=payload.mode,
        rerank=payload.rerank,
    )
    return KbSearchResponse(
        query=result.get("query", payload.query),
        tenant_id=effective_tenant,
        kb_type=result.get("kb_type", "all"),
        mode=result.get("mode", payload.mode),
        rerank=result.get("rerank", payload.rerank),
        candidates=result.get("candidates", 0),
        results=[KbSearchHit(**r) for r in result.get("results", [])],
        warning=result.get("warning"),
    )


@router.get("/v1/kb/collections", summary="向量库状态（collection / 向量数）")
def collections(
    _: str | None = Depends(tenant_guard("kb.collections", "kb_chunk")),
) -> dict:
    try:
        store = default_store()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return store.describe()


@router.get("/v1/kb/documents", summary="知识文档列表")
def documents(
    effective_tenant: str | None = Depends(tenant_guard("kb.documents", "kb_document")),
    session: Session = Depends(get_db),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    kb_type: str | None = None,
) -> dict:
    stmt = select(KbDocument)
    if effective_tenant:
        stmt = stmt.where(KbDocument.tenant_id == effective_tenant)
    if kb_type:
        stmt = stmt.where(KbDocument.kb_type == kb_type)

    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(KbDocument.doc_id).limit(limit).offset(offset)
    ).scalars().all()

    doc_ids = [r.doc_id for r in rows]
    chunk_counts: dict[str, int] = {}
    if doc_ids:
        chunk_counts = {
            doc_id: cnt
            for doc_id, cnt in session.execute(
                select(KbChunk.doc_id, func.count())
                .where(KbChunk.doc_id.in_(doc_ids))
                .group_by(KbChunk.doc_id)
            ).all()
        }

    return {
        "total": int(total),
        "items": [
            {
                "doc_id": r.doc_id,
                "tenant_id": r.tenant_id,
                "kb_type": r.kb_type,
                "source_id": r.source_id,
                "title": r.title,
                "chunks": chunk_counts.get(r.doc_id, 0),
                "updated_at": r.updated_at.isoformat(),
            }
            for r in rows
        ],
    }
# 知识库管理接口（增 / 删 / 改 / 重建）追加到 kb.py

@router.post("/v1/kb/documents", summary="新增知识文档")
def create_document(
    payload: KbDocumentUpsert,
    effective_tenant: str | None = Depends(tenant_guard("kb.doc.create", "kb_document")),
    session: Session = Depends(get_db),
) -> dict:
    if not effective_tenant:
        raise HTTPException(status_code=400, detail="新增知识文档必须指定租户")
    try:
        return upsert_document(
            session,
            tenant_id=effective_tenant,
            kb_type=payload.kb_type,
            source_id=payload.source_id,
            title=payload.title,
            content=payload.content,
        )
    except KbManageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/v1/kb/documents/{doc_id}", summary="更新知识文档（同步重建向量）")
def update_document(
    doc_id: str,
    payload: KbDocumentUpsert,
    effective_tenant: str | None = Depends(tenant_guard("kb.doc.update", "kb_document")),
    session: Session = Depends(get_db),
) -> dict:
    if not effective_tenant:
        raise HTTPException(status_code=400, detail="更新知识文档必须指定租户")
    try:
        return upsert_document(
            session,
            doc_id=doc_id,
            tenant_id=effective_tenant,
            kb_type=payload.kb_type,
            source_id=payload.source_id,
            title=payload.title,
            content=payload.content,
        )
    except KbManageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/v1/kb/documents/{doc_id}", summary="删除知识文档及其切片")
def remove_document(
    doc_id: str,
    effective_tenant: str | None = Depends(tenant_guard("kb.doc.delete", "kb_document")),
    session: Session = Depends(get_db),
) -> dict:
    if not effective_tenant:
        raise HTTPException(status_code=400, detail="删除知识文档必须指定租户")
    try:
        return delete_document(session, doc_id=doc_id, tenant_id=effective_tenant)
    except KbManageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/v1/kb/documents/{doc_id}/chunks", summary="查看某文档的全部切片")
def document_chunks(
    doc_id: str,
    effective_tenant: str | None = Depends(tenant_guard("kb.doc.chunks", "kb_chunk")),
    session: Session = Depends(get_db),
) -> dict:
    stmt = select(KbChunk).where(KbChunk.doc_id == doc_id).order_by(KbChunk.chunk_ix)
    if effective_tenant:
        stmt = stmt.where(KbChunk.tenant_id == effective_tenant)
    rows = session.execute(stmt).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 不存在或无切片")
    return {
        "doc_id": doc_id,
        "chunks": [
            {"chunk_id": r.chunk_id, "chunk_ix": r.chunk_ix, "text": r.text,
             "char_len": r.char_len, "content_hash": r.content_hash}
            for r in rows
        ],
    }


@router.post("/v1/kb/rebuild", summary="重建向量库（全量重建索引）")
def rebuild_knowledge(
    principal: Principal = Depends(require_roles("admin", "ops")),
    tenant_id: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> dict:
    # ops 重建其他租户前必须显式声明，避免误操作覆盖他人知识库
    if principal.role == "ops" and not tenant_id:
        raise HTTPException(status_code=400, detail="ops 重建知识库必须显式指定 tenant_id")
    with session.begin_nested():
        stats = ingest_knowledge(session, store=default_store(), tenant_id=tenant_id, rebuild=True)
        session.commit()
    return {"operator": principal.username, "tenant_id": tenant_id or "ALL", "stats": stats}
