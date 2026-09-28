"""检索效果评测（消融实验）。

为什么要做评测
--------------
"我们做了混合检索、效果更好"是简历上最容易被追问、也最容易答不上来的一句话。
被追问一句"好多少？怎么量的？"就露馅。

本项目把评测做成可复现的实验：
    查询集：从知识库抽样 N 篇文档，用其标题构造查询
    正样本：命中该文档的任意切片即为召回成功（doc 级 ground truth）
    指标：Recall@K 与 MRR@K
    对照组：dense / sparse / hybrid × 是否重排，形成消融矩阵

这样得到的对比数字是**实测**的，而不是拍脑袋写的。
"""

from __future__ import annotations

import random
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from bdp.kb.embedding import build_embedder
from bdp.kb.chunking import split_sentences
from bdp.kb.retriever import search
from bdp.kb.store import build_store
from bdp.models import KbDocument

DEFAULT_SAMPLE = 200


def build_eval_set(session: Session, sample: int = DEFAULT_SAMPLE, seed: int = 42) -> list[dict]:
    """构造评测集。

    查询的构造方式直接决定评测有没有意义。这里刻意避开两个"泄题"做法：
    ① 用标题当查询 —— 层级切片会把标题嵌进每个 chunk，等于把答案写进了候选里；
    ② 用整段正文当查询 —— 与 chunk 高度重合，稀疏通路必然满分。

    实际做法：从正文中抽取一个**不含标题词**的片段作为查询，模拟真实用户
    "描述问题/复述特点"的检索行为。这样稠密通路要靠语义匹配，稀疏通路要靠词面，
    两者的差异才会显现出来。
    """
    docs = session.execute(select(KbDocument)).scalars().all()
    if not docs:
        return []

    rng = random.Random(seed)
    if len(docs) > sample:
        docs = rng.sample(docs, sample)

    eval_set = []
    for doc in docs:
        title = (doc.title or "").strip()
        body = (doc.content or "").replace(title, "")
        for brand in ("NOVA", "AURORA", "LUMEN", "VERDE"):
            body = body.replace(brand, "")
        clauses = [c.strip("　 \n") for c in split_sentences(body) if len(c.strip()) >= 12]
        if not clauses:
            continue
        # 取中后段的句子，避开与标题同源的引入句
        clause = rng.choice(clauses[len(clauses) // 3 :]) if len(clauses) > 2 else rng.choice(clauses)
        query = clause[:28].strip()
        if len(query) < 8:
            continue
        eval_set.append(
            {
                "query": query,
                "tenant_id": doc.tenant_id,
                "kb_type": doc.kb_type,
                "gt_doc_id": doc.doc_id,
            }
        )
    return eval_set


def evaluate_modes(
    session: Session,
    *,
    sample: int = DEFAULT_SAMPLE,
    top_k: int = 5,
    modes: tuple[str, ...] = ("dense", "sparse", "hybrid"),
    rerank_options: tuple[bool, ...] = (False, True),
    store=None,
    embedder=None,
    eval_set: list[dict] | None = None,
) -> dict:
    eval_set = eval_set if eval_set is not None else build_eval_set(session, sample=sample)
    if not eval_set:
        return {"error": "评测集为空，请先执行 kb 入库"}

    store = store or build_store()
    embedder = embedder or build_embedder()

    matrix: list[dict] = []
    for mode in modes:
        for rerank in rerank_options:
            hit_cnt = 0
            rr_sum = 0.0
            latencies: list[float] = []
            for case in eval_set:
                t0 = time.perf_counter()
                res = search(
                    session, query=case["query"], tenant_id=case["tenant_id"],
                    top_k=top_k, store=store, embedder=embedder, mode=mode, rerank=rerank,
                )
                latencies.append((time.perf_counter() - t0) * 1000)

                rank = 0
                for i, r in enumerate(res.get("results", []), start=1):
                    if r.get("doc_id") == case["gt_doc_id"]:
                        rank = i
                        break
                if rank:
                    hit_cnt += 1
                    rr_sum += 1.0 / rank

            n = len(eval_set)
            latencies.sort()
            matrix.append(
                {
                    "mode": mode,
                    "rerank": rerank,
                    "recall_at_k": round(hit_cnt / n, 4),
                    "mrr_at_k": round(rr_sum / n, 4),
                    "queries": n,
                    "p50_latency_ms": round(latencies[n // 2], 2),
                    "p95_latency_ms": round(latencies[min(n - 1, int(n * 0.95))], 2),
                }
            )

    baseline = next((m for m in matrix if m["mode"] == "dense" and not m["rerank"]), None)
    best = max(matrix, key=lambda m: (m["recall_at_k"], m["mrr_at_k"]))
    improvement = None
    if baseline and baseline["recall_at_k"] > 0 and best["recall_at_k"] > baseline["recall_at_k"]:
        improvement = round(
            (best["recall_at_k"] - baseline["recall_at_k"]) / baseline["recall_at_k"] * 100, 1
        )

    return {
        "top_k": top_k,
        "eval_set_size": len(eval_set),
        "embedding_backend": embedder.name,
        "vector_backend": store.backend,
        "matrix": matrix,
        "baseline_dense_recall": baseline["recall_at_k"] if baseline else None,
        "best": {k: best[k] for k in ("mode", "rerank", "recall_at_k", "mrr_at_k", "p95_latency_ms")},
        "relative_improvement_pct": improvement,
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }


def run_eval(session: Session, top_k: int = 5, backend: str | None = None, sample: int = DEFAULT_SAMPLE) -> dict:
    store = build_store(backend)
    return evaluate_modes(session, sample=sample, top_k=top_k, store=store)


def compare_chunking(
    session: Session,
    *,
    strategies: tuple[str, ...] = ("fixed", "semantic", "hierarchical"),
    sample: int = 120,
    top_k: int = 5,
) -> dict:
    """切片策略对比：每种策略重新入库到一个独立的本地向量库再评测。

    这是本项目的核心实验之一 —— 切片策略对召回的影响往往大于模型选择。
    """
    from bdp.kb.ingest import ingest_knowledge
    from bdp.kb.store import LocalVectorStore
    from bdp.config import PROJECT_ROOT

    results = []
    eval_set = build_eval_set(session, sample=sample)
    embedder = build_embedder()

    for strategy in strategies:
        store = LocalVectorStore(path=PROJECT_ROOT / "data" / "vector_store_eval" / strategy)
        store.reset()
        t0 = time.perf_counter()
        ingest = ingest_knowledge(session, store=store, strategy=strategy, rebuild=True)
        ingest_sec = round(time.perf_counter() - t0, 2)

        res = evaluate_modes(
            session, top_k=top_k, modes=("hybrid",), rerank_options=(True,),
            store=store, embedder=embedder, eval_set=eval_set,
        )
        results.append(
            {
                "strategy": strategy,
                "chunks": ingest.get("chunks", 0),
                "avg_chunk_len": ingest.get("avg_chunk_len", 0),
                "ingest_sec": ingest_sec,
                "recall_at_k": res["matrix"][0]["recall_at_k"],
                "mrr_at_k": res["matrix"][0]["mrr_at_k"],
                "p95_latency_ms": res["matrix"][0]["p95_latency_ms"],
            }
        )

    best = max(results, key=lambda r: (r["recall_at_k"], r["mrr_at_k"])) if results else None
    return {
        "top_k": top_k,
        "eval_set_size": len(eval_set),
        "results": results,
        "best_strategy": best["strategy"] if best else None,
    }
