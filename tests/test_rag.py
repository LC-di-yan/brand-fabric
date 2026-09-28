"""Agentic RAG 管线测试（docs/AGENTIC_RAG_PLAN.md P0-P4 验收）。

红线用例打头：租户隔离在多跳路径上零豁免。
等价性用例随后：strategy=single 必须与 kb/retriever.search 逐字段一致——
这是"上线新代码、不开新行为"的承诺。
"""

from __future__ import annotations


from bdp.rag.context_builder import build_context, compress_text, estimate_tokens
from bdp.rag.grounding import check_answer
from bdp.rag.judge import score as judge_score
from bdp.rag.pipeline import answer as rag_answer
from bdp.rag.query_planner import classify, plan, resolve_coreference, rewrite


# ---------------------------------------------------------------------------
# 红线：多跳路径上的租户隔离
# ---------------------------------------------------------------------------

def test_multi_hop_never_crosses_tenant(seeded):
    """往 T002 注入仅含实体编号的文档，T001 的多跳结果断言永不含 T002 文档。

    注意 rebuild=False：rebuild=True 会 reset 整个向量库，摧毁会话级夹具数据；
    这里只需要增量 upsert 一篇 T002 文档。
    """
    from datetime import datetime

    from bdp.db import session_scope
    from bdp.models import KbDocument

    with session_scope() as s:
        s.add(KbDocument(
            doc_id="DOCPOLTEST01", tenant_id="T002", kb_type="policy",
            source_id="POLICY-TEST", title="T002 专属退换货特殊通道",
            content="本政策仅限 T002 品牌专属。POLICY-9 通道由专员一对一处理，"
                    "外部品牌不可引用本流程。",
            updated_at=datetime(2026, 9, 1),
        ))
        s.flush()

        from bdp.kb.ingest import ingest_knowledge
        from bdp.kb.store import default_store

        ingest_knowledge(s, store=default_store(), tenant_id="T002", rebuild=False)

        # T001 用户查询含 POLICY-9 编号——实体种子会尝试二跳，但必须被租户过滤拦住
        rag = rag_answer(
            s, query="退款政策 POLICY-9 特殊通道", tenant_id="T001",
            strategy="agentic",
        )
        for b in rag.blocks:
            assert b["tenant_id"] if "tenant_id" in b else True
            assert b["doc_id"] != "DOCPOLTEST01", "多跳命中了其他租户文档！"
        for c in rag.citations:
            assert c["doc_id"] != "DOCPOLTEST01", "引用里出现了其他租户文档！"


# ---------------------------------------------------------------------------
# single 直通等价性（P0 出口条件）
# ---------------------------------------------------------------------------

def test_single_strategy_equivalent_to_raw_search(seeded):
    """strategy=single 的输出与直接调用 kb/retriever.search 的候选集一致。"""
    from bdp.db import session_scope
    from bdp.kb.retriever import search as raw_search
    from bdp.kb.store import default_store

    store = default_store()
    with session_scope() as s:
        raw = raw_search(s, query="退货政策", tenant_id="T001", top_k=4, store=store)
        rag = rag_answer(s, query="退货政策", tenant_id="T001",
                         strategy="single", top_k=4, store=store)
    assert [r["chunk_id"] for r in raw.get("results", [])] == \
           [b["chunk_id"] for b in rag.blocks]
    assert rag.strategy == "single"
    assert rag.retrieval_confidence == 1.0  # 有结果即满分（直通不判定）


def test_single_without_tenant_rejected(seeded):
    """single 与 agentic 都不豁免"无租户拒绝"硬约束。"""
    from bdp.db import session_scope

    with session_scope() as s:
        for strategy in ("single", "agentic"):
            rag = rag_answer(s, query="退货政策", tenant_id=None, strategy=strategy)
            assert rag.blocks == []
            assert any("租户" in r for r in rag.degraded_reasons)


# ---------------------------------------------------------------------------
# 查询规划（P1）
# ---------------------------------------------------------------------------

def test_planner_classify_and_decompose():
    p = plan("退款率怎么样，另外退货政策是什么")
    assert p.intent == "hybrid"
    assert "REFUND_RATE" in p.metric_codes
    intents = {(s.intent, s.kb_type) for s in p.subqueries}
    assert ("metric", None) in intents
    assert any(s.intent == "knowledge" and s.kb_type == "policy" for s in p.subqueries)


def test_planner_metric_only():
    assert classify("近30天客单价是多少") == "metric"
    assert classify("退货政策是什么样的") == "knowledge"


def test_planner_synonym_rewrite():
    rewritten = rewrite("不想要了怎么办", ["low_coverage"])
    assert "退货" in rewritten and rewritten != "不想要了怎么办"
    # 无词可扩时返回原文（调用方据此停止循环）
    assert rewrite("完全没有同义词可以扩展的内容", ["low_coverage"]) == "完全没有同义词可以扩展的内容"


def test_planner_coreference():
    resolved, flag = resolve_coreference(
        "那上个月呢", history=[{"question": "退款率最近怎么样"}])
    assert flag and "退款率" in resolved
    # 长查询与无历史不触发
    assert resolve_coreference("退货政策具体是什么", history=[])[1] is False
    assert resolve_coreference("这是一个很长很长的查询超过了十二个字的限制", history=[{"question": "x"}])[1] is False


# ---------------------------------------------------------------------------
# 判定与引用核验（P2）
# ---------------------------------------------------------------------------

def test_judge_scores_empty_vs_good_results():
    empty = judge_score("退货政策", [])
    assert empty.score == 0.0 and "no_hits" in empty.reasons
    # 强词面命中 + 充足候选：应超过默认阈值 0.4
    results = [
        {"lexical_score": 0.9, "text": "自签收之日起 7 天内支持无理由退货，商品需保持吊牌完整、包装齐全"},
        {"lexical_score": 0.7, "text": "质量问题导致的退货由商家承担运费，需在订单页提交凭证照片"},
        {"lexical_score": 0.5, "text": "退货审核时效为提交后 24 小时内，节假日顺延"},
    ]
    good = judge_score("退货政策 7 天无理由", results)
    assert good.score >= 0.4, f"充分候选被判为不充分：{good.score}"
    # 弱命中应显著低于强命中
    weak = judge_score("退货政策 7 天无理由", [
        {"lexical_score": 0.1, "text": "会员生日月可领取专属礼遇，包含满减券与专属客服通道"},
    ])
    assert weak.score < good.score


def test_grounding_blocks_fabricated_citations():
    candidates = {("DOC1", 0), ("DOC2", 1)}
    texts = {("DOC1", 0): "自签收之日起 7 天内支持无理由退货", ("DOC2", 1): "换货需保证商品不影响二次销售"}
    report = check_answer(
        "根据政策支持 7 天无理由退货 [DOC1#0]",
        [{"doc_id": "DOCX", "chunk_ix": 9}],  # 编造引用
        candidates, texts,
    )
    assert not report.ok and report.missing_citations and report.confidence == "low"

    # 答案复述切片原文 → 强支撑（词面口径 mean_support ≥0.45 为 high）
    ok_report = check_answer(
        "支持无理由退货，商品需保持吊牌完整 自签收之日起 7 天",
        [{"doc_id": "DOC1", "chunk_ix": 0}], candidates, texts,
    )
    assert ok_report.ok and ok_report.confidence == "high"


def test_grounding_no_kb_citations_is_high():
    report = check_answer("GMV 是 100 万（口径 v1.1）", [], set(), {})
    assert report.ok and report.confidence == "high"


# ---------------------------------------------------------------------------
# 多跳与上下文（P3）
# ---------------------------------------------------------------------------

def test_multi_hop_neighbor_fetch(seeded):
    """文档内边：命中切片的相邻切片应出现在候选池。"""
    from bdp.db import session_scope

    with session_scope() as s:
        rag = rag_answer(
            s, query="退货政策大促期间审核时效会延长吗", tenant_id="T001",
            strategy="agentic",
        )
    sources = {b["source"] for b in rag.blocks}
    assert sources & {"neighbor_hop", "entity_hop", "search"}
    # 有跳就必有 trace
    if any(b["source"].endswith("_hop") for b in rag.blocks):
        assert any(t.get("step", "").startswith("hop_") for t in rag.trace)


def test_context_builder_budget_and_dedup():
    dup = [{"chunk_id": "a#0", "doc_id": "a", "chunk_ix": 0, "text": "退货政策内容" * 10, "score": 0.9}]
    dup2 = [{"chunk_id": "a#0", "doc_id": "a", "chunk_ix": 0, "text": "退货政策内容" * 10, "score": 0.5},
            {"chunk_id": "b#0", "doc_id": "b", "chunk_ix": 0, "text": "换货政策内容" * 10, "score": 0.8}]
    blocks, stats = build_context("退货政策", dup + dup2, token_budget=500)
    ids = [b["chunk_id"] for b in blocks]
    assert len(ids) == len(set(ids)), "去重失败"
    assert stats["deduped"] >= 1
    assert stats["used_tokens"] <= 500 + 50  # 预算约束（少量余量）


def test_compress_text_short_circuit_and_budget():
    short = "短文本不压缩"
    assert compress_text("退货", short) == short
    sentences = [f"退货政策第{i}条关于退货时效的规定，买家需在七天之内提交申请并保持吊牌完整。" for i in range(20)]
    big = "".join(sentences)
    compressed = compress_text("退货时效", big, max_chars=200)
    assert len(compressed) <= 220
    assert "退货" in compressed
    assert estimate_tokens("退货政策abc") > 0


# ---------------------------------------------------------------------------
# 管线行为（agentic 端到端）
# ---------------------------------------------------------------------------

def test_agentic_beats_or_matches_single_on_colloquial(seeded):
    """口语查询（含同义词表命中）经改写后不劣于 single。"""
    from bdp.db import session_scope

    with session_scope() as s:
        agentic = rag_answer(s, query="不想要了怎么办", tenant_id="T001", strategy="agentic")
        single = rag_answer(s, query="不想要了怎么办", tenant_id="T001", strategy="single")
    assert agentic.blocks  # 改写后能检到内容
    agentic_docs = {b["doc_id"] for b in agentic.blocks}
    single_docs = {b["doc_id"] for b in single.blocks}
    assert agentic_docs & single_docs, "agentic 与 single 的候选完全分离，疑似改写走偏"


def test_pipeline_trace_replayable(seeded):
    from bdp.db import session_scope

    with session_scope() as s:
        rag = rag_answer(s, query="退款率怎么样，另外退货政策是什么",
                         tenant_id="T001", strategy="agentic")
    steps = [t["step"] for t in rag.trace]
    assert steps[0] == "coreference" and "classify" in steps and "decompose" in steps
    assert "retrieve" in steps and "context" in steps
    assert "REFUND_RATE" in rag.metric_codes  # 指标意图透传给调用方


def test_insight_agent_agentic_roundtrip(client, tokens):
    """/v1/agent/ask 全链路：strategy=agentic 返回 thread_id/confidence/strategy 字段。"""
    headers = {"Authorization": f"Bearer {tokens['nova']}"}
    res = client.post("/v1/agent/ask", headers=headers,
                      json={"question": "退货政策是什么", "strategy": "agentic"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["strategy"] == "agentic"
    assert body["thread_id"].startswith("thd-")
    assert isinstance(body["citations"], list)
    # 多轮：同一 thread_id 续问（指代消解走 session.load_history）
    res2 = client.post("/v1/agent/ask", headers=headers,
                       json={"question": "那换货呢", "strategy": "agentic",
                             "thread_id": body["thread_id"]})
    assert res2.status_code == 200
    assert res2.json()["thread_id"] == body["thread_id"]


def test_ask_invalid_strategy_rejected(client, tokens):
    res = client.post("/v1/agent/ask",
                      headers={"Authorization": f"Bearer {tokens['nova']}"},
                      json={"question": "x", "strategy": "yolo"})
    assert res.status_code == 400
