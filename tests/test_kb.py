"""知识库测试：切片、稀疏编码、向量存储、RRF 融合、租户过滤。"""

from __future__ import annotations

import numpy as np
import pytest

from bdp.kb.chunking import chunk_fixed, chunk_hierarchical, chunk_semantic, chunk_text
from bdp.kb.embedding import HashEmbedder, SparseEncoder, tokenize
from bdp.kb.retriever import rrf_fuse, search
from bdp.kb.store import LocalVectorStore, VectorRecord

from .conftest import TEST_DAYS  # noqa: F401  确保 conftest 先被加载


# ---------------------------------------------------------------------------
# 切片
# ---------------------------------------------------------------------------


def test_chunk_fixed_respects_overlap_and_covers_text():
    text = "甲" * 500
    chunks = chunk_fixed(text, size=200, overlap=40)
    assert len(chunks) >= 3
    assert all(c.char_len <= 200 for c in chunks)
    # 重叠部分应当存在，保证句子被切断时上下文不丢
    assert chunks[1].text[:40] == chunks[0].text[-40:]


def test_chunk_fixed_rejects_invalid_overlap():
    with pytest.raises(ValueError):
        chunk_fixed("内容", size=50, overlap=50)


def test_chunk_semantic_keeps_sentence_intact():
    text = "第一句话。第二句话。第三句话。"
    chunks = chunk_semantic(text, max_len=20)
    # 不应把句子从中间劈开
    assert all("。" in c.text or c.text in text for c in chunks)
    joined = "".join(c.text for c in chunks)
    assert joined == text


def test_chunk_hierarchical_prefixes_title():
    chunks = chunk_hierarchical("商品支持 7 天无理由退货。质量问题商家承担运费。", title="退货政策", max_len=60)
    assert chunks
    assert all(c.text.startswith("【退货政策】") for c in chunks)


def test_chunk_text_dispatch_and_empty_input():
    assert chunk_text("", strategy="semantic") == []
    assert chunk_text("只有一句话。", strategy="fixed")
    with pytest.raises(ValueError):
        chunk_text("内容", strategy="not-exist")
    # 长文本下语义切片应当产生多个切片
    long_text = "。".join(f"这是第{i}条说明" for i in range(30)) + "。"
    assert len(chunk_text(long_text, strategy="semantic")) > 1


# ---------------------------------------------------------------------------
# 向量化与稀疏编码
# ---------------------------------------------------------------------------


def test_hash_embedder_is_deterministic_and_normalized():
    emb = HashEmbedder(dim=256)
    a = emb.encode(["退货政策是怎样的"])[0]
    b = emb.encode(["退货政策是怎样的"])[0]
    assert np.allclose(a, b), "同一文本必须得到同一向量，否则测试不可复现"
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-5
    assert emb.encode(["退货政策"])[0] @ emb.encode(["退货政策说明"])[0] > 0


def test_tokenize_produces_bigrams_and_latin_words():
    tokens = tokenize("NOVA 退货政策")
    assert "退货" in tokens and "货政" in tokens
    assert "nova" in tokens


def test_sparse_encoder_weights_are_sublinear():
    single = SparseEncoder.encode("退货")
    repeated = SparseEncoder.encode("退货退货退货退回")
    assert set(single.keys()) <= set(repeated.keys())
    assert all(v >= 1.0 for v in single.values())


# ---------------------------------------------------------------------------
# 向量存储
# ---------------------------------------------------------------------------


def _record(cid: str, tenant: str, text: str, emb: HashEmbedder, kb_type: str = "cs_faq") -> VectorRecord:
    return VectorRecord(
        chunk_id=cid, tenant_id=tenant, kb_type=kb_type, doc_id="D1", chunk_ix=0,
        text=text, dense=emb.encode([text])[0], sparse=SparseEncoder.encode(text),
    )


@pytest.fixture
def store(tmp_path) -> LocalVectorStore:
    s = LocalVectorStore(path=tmp_path / "vs")
    emb = HashEmbedder(dim=256)
    s.upsert([
        _record("C1", "T001", "退货政策：7 天无理由退货", emb),
        _record("C2", "T001", "运费政策：满 199 免运费", emb),
        _record("C3", "T002", "退货政策：30 天退货", emb),
    ])
    return s


def test_local_store_filters_by_tenant(store):
    emb = HashEmbedder(dim=256)
    q = emb.encode(["退货政策"])[0]
    hits_t1 = store.dense_search("cs_faq", q, "T001", 10)
    hits_t2 = store.dense_search("cs_faq", q, "T002", 10)
    assert {cid for cid, _ in hits_t1} == {"C1", "C2"}
    assert {cid for cid, _ in hits_t2} == {"C3"}
    assert store.count(tenant_id="T001") == 2
    assert store.count() == 3


def test_local_store_sparse_search_works_after_reload(store, tmp_path):
    """回归测试：倒排索引不持久化，若加载时不重建会静默返回空结果。"""
    reloaded = LocalVectorStore(path=tmp_path / "vs")
    hits = reloaded.sparse_search("cs_faq", SparseEncoder.encode("退货政策"), "T001", 10)
    assert hits, "重载后稀疏通路必须有结果（历史缺陷：返回空列表）"
    assert hits[0][0] == "C1"


def test_local_store_upsert_is_idempotent(store):
    emb = HashEmbedder(dim=256)
    before = store.count()
    written = store.upsert([_record("C1", "T001", "退货政策：7 天无理由退货", emb)])
    assert written == 0, "同一 chunk_id 重复写入不应新增"
    assert store.count() == before


def test_local_store_fetch_returns_metadata(store):
    got = store.fetch(["C1"])
    assert got["C1"].tenant_id == "T001"
    assert "退货" in got["C1"].text


# ---------------------------------------------------------------------------
# 融合与检索
# ---------------------------------------------------------------------------


def test_rrf_fuse_rewards_documents_present_in_multiple_channels():
    fused = rrf_fuse([["a", "b", "c"], ["b", "a"]])
    assert fused["a"] > fused["c"]
    assert fused["b"] > fused["c"]
    # 只在稀疏通路出现的文档，分数低于双路都出现的文档
    assert fused["b"] > fused["c"]


def test_search_returns_empty_without_tenant(store):
    """租户不明时不得执行检索 —— 否则就是跨品牌泄漏。"""
    res = search(None, query="退货政策", tenant_id=None, store=store, embedder=HashEmbedder(dim=256))
    assert res["results"] == []
    assert "拒绝" in res["warning"]


def test_search_modes_are_consistent(store):
    emb = HashEmbedder(dim=256)
    for mode in ("dense", "sparse", "hybrid"):
        res = search(None, query="退货政策", tenant_id="T001", top_k=2,
                     store=store, embedder=emb, mode=mode)
        assert res["results"], f"{mode} 模式应当有结果"
        assert all(r["chunk_id"] in {"C1", "C2"} for r in res["results"])
    hybrid = search(None, query="退货政策", tenant_id="T001", top_k=2,
                    store=store, embedder=emb, mode="hybrid")
    # 混合检索应同时具备 RRF 分数与词面分数
    assert hybrid["results"][0]["rrf_score"] > 0
    assert hybrid["results"][0]["lexical_score"] > 0
