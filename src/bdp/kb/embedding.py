"""向量化（Embedding）。

为什么要做可插拔
----------------
真实项目里 embedding 一定是调模型（BGE-M3、OpenAI 等），但把模型硬编码进代码会带来
两个后果：① 换一台机器就跑不起来（要下 2GB 模型）；② 无法离线做单元测试。

因此这里定义统一接口 + 三种实现：
    hash    离线确定性哈希向量（默认）—— 零依赖、零下载，任何机器 5 分钟能跑通全链路
    bge     本地 BGE 小模型（可选，需 pip install sentence-transformers）
    openai  兼容 OpenAI /v1/embeddings 协议的接口

同时提供**稀疏编码**：中文专有名词（型号、货号）在稠密向量上召回很差，
必须靠 BM25 类稀疏信号兜底，两者融合才稳。
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

import numpy as np

from bdp.config import settings

SPARSE_DIM = 1 << 20  # 稀疏向量维度上限（Milvus 稀疏索引需要较大空间）

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    """中文用字符二元组，英文数字用整词。

    不引入 jieba 是为了保持"零额外依赖"。对检索场景而言，中文二元组的召回
    已经足够好（且不会因为分词错误漏召回），精度问题交给重排处理。
    """
    text = text or ""
    tokens: list[str] = []
    cjk_chars = _CJK.findall(text)
    for i in range(len(cjk_chars)):
        tokens.append(cjk_chars[i])
        if i + 1 < len(cjk_chars):
            tokens.append(cjk_chars[i] + cjk_chars[i + 1])
    tokens.extend(w.lower() for w in _LATIN.findall(text))
    return tokens


def term_id(token: str) -> int:
    return int(hashlib.blake2b(token.encode("utf-8"), digest_size=4).hexdigest(), 16) % SPARSE_DIM


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    dim: int
    name: str

    def encode(self, texts: list[str]) -> np.ndarray:  # (n, dim) float32, L2 归一化
        ...


# ---------------------------------------------------------------------------
# 离线哈希实现（默认）
# ---------------------------------------------------------------------------


class HashEmbedder:
    """确定性哈希向量。

    做法：把 token 哈希到 dim 维空间、按 sublinear tf 加权、L2 归一化。
    它是"词袋模型的稠密投影"，语义能力弱于真实模型，但**完全确定性**：
    同样的文本永远得到同样的向量，测试可复现，且无任何外部依赖。

    项目里通过 BDP_EMBEDDING_BACKEND=bge 可切换到真实模型。
    """

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.embedding_dim
        self.name = f"hash-{self.dim}"

    def _one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = tokenize(text)
        if not tokens:
            return vec
        counts: dict[int, float] = {}
        for tok in tokens:
            tid = int(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).hexdigest(), 16) % self.dim
            counts[tid] = counts.get(tid, 0.0) + 1.0
        for tid, cnt in counts.items():
            vec[tid] = 1.0 + math.log(cnt)
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._one(t) for t in texts])


# ---------------------------------------------------------------------------
# 可选：本地 BGE 小模型
# ---------------------------------------------------------------------------


class BGEEmbedder:
    """基于 sentence-transformers 的本地模型（需额外安装）。"""

    def __init__(self, model_name: str | None = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 sentence-transformers。请执行：pip install sentence-transformers\n"
                "或改用 BDP_EMBEDDING_BACKEND=hash（离线哈希）"
            ) from exc
        self.name = model_name or settings.embedding_model
        self._model = SentenceTransformer(self.name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)


# ---------------------------------------------------------------------------
# 可选：兼容 OpenAI 协议的接口
# ---------------------------------------------------------------------------


class OpenAICompatEmbedder:
    def __init__(self, model_name: str | None = None) -> None:
        import httpx  # 局部导入，避免无网络场景下的启动开销

        if not settings.embedding_api_base:
            raise RuntimeError("未配置 BDP_EMBEDDING_API_BASE")
        self._httpx = httpx
        self.name = model_name or settings.embedding_model
        self.dim = settings.embedding_dim
        self._base = settings.embedding_api_base.rstrip("/")

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        headers = {"Content-Type": "application/json"}
        if settings.embedding_api_key:
            headers["Authorization"] = f"Bearer {settings.embedding_api_key}"
        resp = self._httpx.post(
            f"{self._base}/v1/embeddings",
            headers=headers,
            json={"model": self.name, "input": texts},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        arr = np.asarray([d["embedding"] for d in data], dtype=np.float32)
        self.dim = arr.shape[1]
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms


def build_embedder(backend: str | None = None) -> Embedder:
    backend = (backend or settings.embedding_backend or "hash").lower()
    if backend == "hash":
        return HashEmbedder()
    if backend == "bge":
        return BGEEmbedder()
    if backend == "openai":
        return OpenAICompatEmbedder()
    raise ValueError(f"未知的 embedding 后端：{backend}")


@lru_cache(maxsize=1)
def default_embedder() -> Embedder:
    """进程级单例。

    hash 后端构造廉价，但 bge 后端每次构造要加载约 100MB 模型——
    检索路径按请求构造 embedder 在真实模型下是灾难性的。显式 backend 仍走 build_embedder()。
    """
    return build_embedder()


# ---------------------------------------------------------------------------
# 稀疏编码（BM25 风格）
# ---------------------------------------------------------------------------


class SparseEncoder:
    """把文本编码成 {term_id: weight} 的稀疏向量。

    权重采用 1 + log(tf) 的次线性形式（BM25 的 tf 部分），乘上 idf 由查询侧体现。
    这里不做全局 idf 统计，是为了让编码保持无状态、可增量写入向量库。
    """

    @staticmethod
    def encode(text: str) -> dict[int, float]:
        tokens = tokenize(text)
        if not tokens:
            return {}
        counts: dict[int, float] = {}
        for tok in tokens:
            tid = term_id(tok)
            counts[tid] = counts.get(tid, 0.0) + 1.0
        return {tid: round(1.0 + math.log(c), 6) for tid, c in counts.items()}
