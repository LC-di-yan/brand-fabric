"""向量存储：统一接口 + 两种后端实现。

设计意图
--------
把向量库抽象成接口，是为了让上层检索逻辑与具体产品解耦。本项目提供两个实现：

    MilvusVectorStore   生产形态：租户隔离用 Partition Key，支持 HNSW + 稀疏混合检索
    LocalVectorStore    离线形态：numpy 暴力检索 + 倒排稀疏索引，零依赖、可持久化

这样做的直接收益：
1. 没有 Docker 也能跑通完整链路（新机器上 5 分钟跑通）
2. 单元测试不需要拉起 Milvus
3. 也证明了"存储可替换"这个架构能力，而不是把业务逻辑焊死在某个产品上

注意：LocalVectorStore 是**为了可运行性与可测试性**而存在的，
它的检索能力（尤其稀疏部分）弱于 Milvus，不作为生产推荐。
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from bdp.config import PROJECT_ROOT, settings
from bdp.kb.embedding import SparseEncoder, build_embedder

DEFAULT_RRF_K = 60

# 进程级写锁：LocalVectorStore 的写路径是"改内存 + 全量重写 npz"，
# 必须串行化，否则并发写会互相覆盖整个文件（比丢一次 upsert 严重得多）。
_LOCAL_WRITE_LOCK = threading.Lock()


@dataclass
class VectorRecord:
    chunk_id: str
    tenant_id: str
    kb_type: str
    doc_id: str
    chunk_ix: int
    text: str
    dense: np.ndarray
    sparse: dict[int, float] = field(default_factory=dict)


@dataclass
class SearchHit:
    chunk_id: str
    tenant_id: str
    kb_type: str
    doc_id: str
    chunk_ix: int
    text: str
    score: float
    source: str  # dense | sparse | hybrid


class VectorStore(Protocol):
    backend: str

    def reset(self, kb_types: list[str] | None = None) -> None: ...
    def upsert(self, records: list[VectorRecord]) -> int: ...
    def delete(self, chunk_ids: list[str]) -> int: ...
    def count(self, kb_type: str | None = None, tenant_id: str | None = None) -> int: ...
    def dense_search(self, kb_type: str, query: np.ndarray, tenant_id: str, limit: int) -> list[tuple[str, float]]: ...
    def sparse_search(
        self, kb_type: str, query_sparse: dict[int, float], tenant_id: str, limit: int
    ) -> list[tuple[str, float]]: ...
    def fetch(self, chunk_ids: list[str]) -> dict[str, SearchHit]: ...
    def describe(self) -> dict: ...


# ---------------------------------------------------------------------------
# 本地实现
# ---------------------------------------------------------------------------


class LocalVectorStore:
    """numpy 暴力检索 + 倒排稀疏索引，持久化到 data/vector_store。"""

    backend = "local"

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = Path(settings.vector_path) if settings.vector_path else (PROJECT_ROOT / "data" / "vector_store")
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # -- 持久化 -------------------------------------------------------------

    def _kb_file(self, kb_type: str) -> Path:
        return self.path / f"{kb_type}.npz"

    def _meta_file(self, kb_type: str) -> Path:
        return self.path / f"{kb_type}.meta.json"

    def _load(self) -> None:
        for meta_file in self.path.glob("*.meta.json"):
            kb_type = meta_file.name[: -len(".meta.json")]
            npz_file = self._kb_file(kb_type)
            if not npz_file.exists():
                continue
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            npz = np.load(npz_file, allow_pickle=False)
            dense = npz["dense"]
            texts = meta["texts"]
            entries = {
                "ids": meta["ids"],
                "tenant_ids": meta["tenant_ids"],
                "doc_ids": meta["doc_ids"],
                "chunk_ix": meta["chunk_ix"],
                "texts": texts,
                "dense": dense,
                "index": {cid: i for i, cid in enumerate(meta["ids"])},
                "sparse_postings": {},
            }
            self._data[kb_type] = entries
            # 倒排索引不持久化（体积与文本重复、重建很快），进程启动时必须重建，
            # 否则稀疏通路会静默返回空结果 —— 这是一个非常隐蔽的降级故障。
            self._rebuild_sparse(kb_type)

    def _save(self, kb_type: str) -> None:
        entries = self._data[kb_type]
        np.savez_compressed(self._kb_file(kb_type), dense=entries["dense"])
        self._meta_file(kb_type).write_text(
            json.dumps(
                {
                    "ids": entries["ids"],
                    "tenant_ids": entries["tenant_ids"],
                    "doc_ids": entries["doc_ids"],
                    "chunk_ix": entries["chunk_ix"],
                    "texts": entries["texts"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _ensure(self, kb_type: str) -> dict[str, Any]:
        if kb_type not in self._data:
            self._data[kb_type] = {
                "ids": [], "tenant_ids": [], "doc_ids": [], "chunk_ix": [], "texts": [],
                # 维度在首次写入时才确定：不同的 embedding 后端维度不同，
                # 预先写死会导致与调用方不一致时 vstack 直接报错。
                "dense": np.zeros((0, 0), dtype=np.float32),
                "index": {},
                "sparse_postings": {},
            }
        return self._data[kb_type]

    def _rebuild_sparse(self, kb_type: str) -> None:
        """从文本重建倒排索引。稀疏表体积小、重建快，无需额外持久化。"""
        entries = self._ensure(kb_type)
        postings: dict[int, list[tuple[int, float]]] = {}
        for row, text in enumerate(entries["texts"]):
            for tid, w in SparseEncoder.encode(text).items():
                postings.setdefault(tid, []).append((row, w))
        entries["sparse_postings"] = postings

    # -- 写 ----------------------------------------------------------------

    def reset(self, kb_types: list[str] | None = None) -> None:
        with _LOCAL_WRITE_LOCK:
            targets = kb_types if kb_types is not None else list(self._data)
            for kb_type in targets:
                self._data.pop(kb_type, None)
                self._kb_file(kb_type).unlink(missing_ok=True)
                self._meta_file(kb_type).unlink(missing_ok=True)

    def upsert(self, records: list[VectorRecord]) -> int:
        with _LOCAL_WRITE_LOCK:
            return self._upsert_locked(records)

    def _upsert_locked(self, records: list[VectorRecord]) -> int:
        if not records:
            return 0
        by_type: dict[str, list[VectorRecord]] = {}
        for rec in records:
            by_type.setdefault(rec.kb_type, []).append(rec)

        written = 0
        for kb_type, recs in by_type.items():
            entries = self._ensure(kb_type)
            new_dense: list[np.ndarray] = []
            new_records: list[VectorRecord] = []
            updated_any = False

            for rec in recs:
                if rec.chunk_id in entries["index"]:
                    row = entries["index"][rec.chunk_id]
                    entries["dense"][row] = rec.dense
                    entries["texts"][row] = rec.text
                    updated_any = True
                    continue
                row = len(entries["ids"])
                entries["index"][rec.chunk_id] = row
                entries["ids"].append(rec.chunk_id)
                entries["tenant_ids"].append(rec.tenant_id)
                entries["doc_ids"].append(rec.doc_id)
                entries["chunk_ix"].append(rec.chunk_ix)
                entries["texts"].append(rec.text)
                new_dense.append(rec.dense)
                new_records.append(rec)
                written += 1

            if new_dense:
                new_mat = np.vstack(new_dense).astype(np.float32)
                if entries["dense"].size == 0:
                    entries["dense"] = new_mat
                elif entries["dense"].shape[1] != new_mat.shape[1]:
                    raise ValueError(
                        f"向量维度不一致：已存 {entries['dense'].shape[1]} 维，"
                        f"本次写入 {new_mat.shape[1]} 维。请确认 embedding 后端是否被更换，"
                        f"更换后端需要重建整个向量库。"
                    )
                else:
                    entries["dense"] = np.vstack([entries["dense"], new_mat]).astype(np.float32)

            # 文本发生更新时，倒排索引里的旧 posting 会失效，必须整体重建
            if updated_any or not entries.get("sparse_postings"):
                self._rebuild_sparse(kb_type)
            elif new_records:
                base_row = len(entries["ids"]) - len(new_records)
                for offset, rec in enumerate(new_records):
                    for tid, w in SparseEncoder.encode(rec.text).items():
                        entries["sparse_postings"].setdefault(tid, []).append((base_row + offset, w))

            self._save(kb_type)
        return written

    # -- 读 ----------------------------------------------------------------

    def count(self, kb_type: str | None = None, tenant_id: str | None = None) -> int:
        types = [kb_type] if kb_type else list(self._data)
        total = 0
        for t in types:
            entries = self._data.get(t)
            if not entries:
                continue
            if tenant_id is None:
                total += len(entries["ids"])
            else:
                total += sum(1 for x in entries["tenant_ids"] if x == tenant_id)
        return total

    def delete(self, chunk_ids: list[str]) -> int:
        """删除指定切片，并重建倒排索引。

        本地后端的删除是"物理移除 + 重建索引"：规模小，简单可靠；
        规模上来后应改为软删除 + 定期段合并（Milvus 的做法）。
        """
        with _LOCAL_WRITE_LOCK:
            return self._delete_locked(chunk_ids)

    def _delete_locked(self, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        removed = 0
        ids_set = set(chunk_ids)
        for kb_type in list(self._data):
            entries = self._data[kb_type]
            index = entries["index"]
            if not any(cid in index for cid in ids_set):
                continue
            keep_rows = [i for i, cid in enumerate(entries["ids"]) if cid not in ids_set]
            if len(keep_rows) == len(entries["ids"]):
                continue
            removed += len(entries["ids"]) - len(keep_rows)
            entries["ids"] = [entries["ids"][i] for i in keep_rows]
            entries["tenant_ids"] = [entries["tenant_ids"][i] for i in keep_rows]
            entries["doc_ids"] = [entries["doc_ids"][i] for i in keep_rows]
            entries["chunk_ix"] = [entries["chunk_ix"][i] for i in keep_rows]
            entries["texts"] = [entries["texts"][i] for i in keep_rows]
            if entries["dense"].size:
                entries["dense"] = entries["dense"][keep_rows]
            entries["index"] = {cid: i for i, cid in enumerate(entries["ids"])}
            entries["sparse_postings"] = {}
            self._rebuild_sparse(kb_type)
            self._save(kb_type)
        return removed

    def _tenant_rows(self, kb_type: str, tenant_id: str) -> np.ndarray:
        entries = self._data.get(kb_type)
        if not entries:
            return np.zeros(0, dtype=np.int64)
        return np.asarray(
            [i for i, t in enumerate(entries["tenant_ids"]) if t == tenant_id], dtype=np.int64
        )

    def dense_search(
        self, kb_type: str, query: np.ndarray, tenant_id: str, limit: int
    ) -> list[tuple[str, float]]:
        entries = self._data.get(kb_type)
        if not entries or len(entries["ids"]) == 0:
            return []
        rows = self._tenant_rows(kb_type, tenant_id)
        if rows.size == 0:
            return []
        mat = entries["dense"][rows]
        q = query.astype(np.float32).reshape(-1)
        qn = float(np.linalg.norm(q))
        if qn > 0:
            q = q / qn
        scores = mat @ q
        order = np.argsort(-scores)[:limit]
        return [(entries["ids"][rows[i]], float(scores[i])) for i in order]

    def sparse_search(
        self, kb_type: str, query_sparse: dict[int, float], tenant_id: str, limit: int
    ) -> list[tuple[str, float]]:
        entries = self._data.get(kb_type)
        if not entries or not query_sparse:
            return []
        rows = set(int(r) for r in self._tenant_rows(kb_type, tenant_id))
        if not rows:
            return []
        postings = entries.get("sparse_postings") or {}
        scores: dict[int, float] = {}
        for tid, qw in query_sparse.items():
            plist = postings.get(tid)
            if not plist:
                continue
            # 倒排列表越长，说明该词越常见 → idf 越低
            idf = math.log(1.0 + len(entries["ids"]) / (1.0 + len(plist)))
            for row, dw in plist:
                if row not in rows:
                    continue
                scores[row] = scores.get(row, 0.0) + qw * dw * idf
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return [(entries["ids"][row], float(score)) for row, score in ranked]

    def fetch(self, chunk_ids: list[str]) -> dict[str, SearchHit]:
        out: dict[str, SearchHit] = {}
        for kb_type, entries in self._data.items():
            for cid in chunk_ids:
                row = entries["index"].get(cid)
                if row is None:
                    continue
                out[cid] = SearchHit(
                    chunk_id=cid, tenant_id=entries["tenant_ids"][row], kb_type=kb_type,
                    doc_id=entries["doc_ids"][row], chunk_ix=entries["chunk_ix"][row],
                    text=entries["texts"][row], score=0.0, source="hybrid",
                )
        return out

    def describe(self) -> dict:
        return {
            "backend": "local",
            "path": str(self.path),
            "collections": {
                kb_type: {
                    "vectors": len(entries["ids"]),
                    "tenants": len(set(entries["tenant_ids"])),
                    "dim": int(entries["dense"].shape[1]) if entries["dense"].size else 0,
                }
                for kb_type, entries in self._data.items()
            },
        }


# ---------------------------------------------------------------------------
# Milvus 实现
# ---------------------------------------------------------------------------


class MilvusVectorStore:
    """生产形态：Collection 按知识域切分，租户隔离使用 Partition Key。

    关键设计（与 LocalVectorStore 的本质区别）
    -----------------------------------------
    1. **Collection 按知识域切，不按租户切** —— 否则 490 个品牌就是 490 个 collection，
       元数据与索引重建会失控。
    2. **tenant_id 声明为 partition_key**，查询时必须带该字段过滤，
       Milvus 自动路由到对应分区，既保证隔离又避免跨分区扫描。
    3. 标量字段（tenant_id / doc_id）建 INVERTED 索引 —— 不建索引时 Milvus 的
       标量过滤会退化为全量扫描，这是最常见的性能坑。
    """

    backend = "milvus"

    def __init__(self, uri: str | None = None, dim: int | None = None) -> None:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("未安装 pymilvus，无法使用 Milvus 后端") from exc

        self.uri = uri or settings.milvus_uri
        self.dim = dim or settings.embedding_dim
        self._client = MilvusClient(
            uri=self.uri,
            user=settings.milvus_user or "",
            password=settings.milvus_password or "",
        )

    @staticmethod
    def collection_name(kb_type: str) -> str:
        return f"bdp_kb_{kb_type}"

    # -- schema 与索引 ------------------------------------------------------

    def _build_schema(self):
        from pymilvus import DataType

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("tenant_id", DataType.VARCHAR, max_length=32, is_partition_key=True)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=40)
        schema.add_field("chunk_ix", DataType.INT32)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=self.dim)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        return schema

    def _build_index_params(self):
        params = self._client.prepare_index_params()
        # 稠密：HNSW，延迟优先
        params.add_index(
            field_name="dense", index_type="HNSW", metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        # 稀疏：倒排索引，用于混合检索的 BM25 通路
        params.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP")
        # 标量：不建索引会退化为全量扫描
        for scalar_field in ("tenant_id", "doc_id"):
            params.add_index(field_name=scalar_field, index_type="INVERTED")
        return params

    def ensure_collection(self, kb_type: str, drop: bool = False) -> str:
        name = self.collection_name(kb_type)
        if drop and self._client.has_collection(name):
            self._client.drop_collection(name)
        if not self._client.has_collection(name):
            self._client.create_collection(
                collection_name=name, schema=self._build_schema(), index_params=self._build_index_params()
            )
        return name

    def reset(self, kb_types: list[str] | None = None) -> None:
        for kb_type in kb_types or ["cs_faq", "product", "policy", "sop"]:
            name = self.collection_name(kb_type)
            if self._client.has_collection(name):
                self._client.drop_collection(name)

    # -- 写 ----------------------------------------------------------------

    def upsert(self, records: list[VectorRecord]) -> int:
        if not records:
            return 0
        by_type: dict[str, list[VectorRecord]] = {}
        for rec in records:
            by_type.setdefault(rec.kb_type, []).append(rec)

        total = 0
        for kb_type, recs in by_type.items():
            name = self.ensure_collection(kb_type)
            rows = [
                {
                    "chunk_id": r.chunk_id,
                    "tenant_id": r.tenant_id,
                    "doc_id": r.doc_id,
                    "chunk_ix": r.chunk_ix,
                    "text": r.text[:8000],
                    "dense": r.dense.tolist(),
                    "sparse": {int(k): float(v) for k, v in r.sparse.items()},
                }
                for r in recs
            ]
            # 分批写入：单批过大容易触发服务端限流
            for i in range(0, len(rows), 500):
                self._client.upsert(collection_name=name, data=rows[i : i + 500])
                total += len(rows[i : i + 500])
        return total

    def delete(self, chunk_ids: list[str]) -> int:
        """从 Milvus 删除指定切片。"""
        if not chunk_ids:
            return 0
        quoted = ", ".join(f'"{c}"' for c in chunk_ids)
        removed = 0
        for kb_type in ["cs_faq", "product", "policy", "sop"]:
            name = self.collection_name(kb_type)
            if not self._client.has_collection(name):
                continue
            res = self._client.delete(collection_name=name, filter=f"chunk_id in [{quoted}]")
            removed += int(getattr(res, "delete_count", 0) or 0)
        return removed

    # -- 读 ----------------------------------------------------------------

    def count(self, kb_type: str | None = None, tenant_id: str | None = None) -> int:
        types = [kb_type] if kb_type else ["cs_faq", "product", "policy", "sop"]
        total = 0
        for t in types:
            name = self.collection_name(t)
            if not self._client.has_collection(name):
                continue
            expr = f'tenant_id == "{tenant_id}"' if tenant_id else ""
            res = self._client.query(collection_name=name, filter=expr or "", output_fields=["count(*)"])
            if res:
                total += int(res[0].get("count(*)", 0))
        return total

    def dense_search(
        self, kb_type: str, query: np.ndarray, tenant_id: str, limit: int
    ) -> list[tuple[str, float]]:
        name = self.collection_name(kb_type)
        if not self._client.has_collection(name):
            return []
        hits = self._client.search(
            collection_name=name,
            data=[query.astype(np.float32).tolist()],
            anns_field="dense",
            limit=limit,
            filter=f'tenant_id == "{tenant_id}"',
            output_fields=["chunk_id"],
            search_params={"metric_type": "COSINE", "params": {"ef": 128}},
        )
        return [(h["id"] if "id" in h else h["entity"]["chunk_id"], float(h["distance"])) for h in hits[0]]

    def sparse_search(
        self, kb_type: str, query_sparse: dict[int, float], tenant_id: str, limit: int
    ) -> list[tuple[str, float]]:
        name = self.collection_name(kb_type)
        if not self._client.has_collection(name) or not query_sparse:
            return []
        hits = self._client.search(
            collection_name=name,
            data=[{int(k): float(v) for k, v in query_sparse.items()}],
            anns_field="sparse",
            limit=limit,
            filter=f'tenant_id == "{tenant_id}"',
            output_fields=["chunk_id"],
            search_params={"metric_type": "IP"},
        )
        return [(h["id"] if "id" in h else h["entity"]["chunk_id"], float(h["distance"])) for h in hits[0]]

    def hybrid_search(
        self, kb_type: str, query_dense: np.ndarray, query_sparse: dict[int, float],
        tenant_id: str, limit: int, rrf_k: int = DEFAULT_RRF_K,
    ) -> list[tuple[str, float]]:
        """Milvus 原生混合检索（dense + sparse 由服务端 RRF 融合）。

        若当前 pymilvus 版本不支持 hybrid_search，由调用方回退到手工融合。
        """
        from pymilvus import AnnSearchRequest, RRFRanker

        name = self.collection_name(kb_type)
        expr = f'tenant_id == "{tenant_id}"'
        reqs = [
            AnnSearchRequest(
                data=[query_dense.astype(np.float32).tolist()], anns_field="dense",
                param={"metric_type": "COSINE", "params": {"ef": 128}}, limit=limit,
            ),
            AnnSearchRequest(
                data=[{int(k): float(v) for k, v in query_sparse.items()}], anns_field="sparse",
                param={"metric_type": "IP"}, limit=limit,
            ),
        ]
        res = self._client.hybrid_search(
            collection_name=name, reqs=reqs, ranker=RRFRanker(rrf_k),
            limit=limit, filter=expr, output_fields=["chunk_id", "text", "doc_id", "chunk_ix"],
        )
        return [(_hit_id(h), float(h.get("distance", 0.0))) for h in res[0]]

    def fetch(self, chunk_ids: list[str]) -> dict[str, SearchHit]:
        out: dict[str, SearchHit] = {}
        quoted = ", ".join(f'"{c}"' for c in chunk_ids)
        for kb_type in ["cs_faq", "product", "policy", "sop"]:
            name = self.collection_name(kb_type)
            if not self._client.has_collection(name):
                continue
            rows = self._client.query(
                collection_name=name, filter=f"chunk_id in [{quoted}]",
                output_fields=["chunk_id", "tenant_id", "doc_id", "chunk_ix", "text"],
            )
            for r in rows:
                out[r["chunk_id"]] = SearchHit(
                    chunk_id=r["chunk_id"], tenant_id=r["tenant_id"], kb_type=kb_type,
                    doc_id=r["doc_id"], chunk_ix=r["chunk_ix"], text=r["text"],
                    score=0.0, source="hybrid",
                )
        return out

    def health(self) -> dict:
        try:
            collections = [c for c in self._client.list_collections() if c.startswith("bdp_kb_")]
            return {"ok": True, "uri": self.uri, "collections": collections}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "uri": self.uri, "error": str(exc)}

    def describe(self) -> dict:
        health = self.health()
        stats = {}
        for kb_type in ["cs_faq", "product", "policy", "sop"]:
            name = self.collection_name(kb_type)
            if health.get("ok") and name in (health.get("collections") or []):
                try:
                    res = self._client.query(collection_name=name, filter="", output_fields=["count(*)"])
                    stats[kb_type] = int(res[0].get("count(*)", 0)) if res else 0
                except Exception:
                    stats[kb_type] = "n/a"
        return {"backend": "milvus", "uri": self.uri, "ok": health.get("ok"), "counts": stats,
                "error": health.get("error")}


def _hit_id(hit: dict) -> str:
    if "id" in hit:
        return str(hit["id"])
    entity = hit.get("entity") or {}
    return str(entity.get("chunk_id", ""))


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def build_store(backend: str | None = None) -> VectorStore:
    backend = (backend or settings.vector_backend or "local").lower()
    if backend == "milvus":
        return MilvusVectorStore()
    if backend == "local":
        return LocalVectorStore()
    raise ValueError(f"未知的向量库后端：{backend}")


@lru_cache(maxsize=1)
def default_store() -> VectorStore:
    """进程级单例。

    修复点（原瓶颈 P1）：此前每个检索请求都会 build_store()——本地后端构造时
    全量加载 npz 并重建倒排索引，等于每次查询都是一次全库 IO。
    单例后同一进程内所有读写共享同一份内存状态（配合 _LOCAL_WRITE_LOCK 串行写）。
    显式指定 backend 的调用（CLI --backend、测试）仍走 build_store()。
    """
    return build_store()


def store_and_embedder(backend: str | None = None):
    return build_store(backend), build_embedder()
