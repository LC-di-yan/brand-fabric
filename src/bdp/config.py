"""全局配置：通过 BDP_ 前缀的环境变量或 .env 覆盖。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BDP_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 运行模式：lite（SQLite + 本地向量索引）/ full（PG + Milvus）
    mode: str = "lite"

    # 关系型存储
    database_url: str = "sqlite:///./data/bdp.db"

    # 向量存储
    vector_backend: str = "local"  # milvus | local
    vector_path: str = ""  # local 后端的持久化目录，留空则用 <项目根>/data/vector_store
    milvus_uri: str = "http://localhost:19530"
    milvus_user: str = ""
    milvus_password: str = ""

    # Embedding
    embedding_backend: str = "hash"  # hash | bge | openai
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 1024
    embedding_api_base: str = ""
    embedding_api_key: str = ""

    # 安全
    jwt_secret: str = "dev-only-secret-please-override-with-a-long-random-string"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720
    # 会话 Cookie：生产 HTTPS 下保持 secure=True；本地 http 演示可设 False
    cookie_secure: bool = True
    cookie_name: str = "bdp_session"
    csrf_cookie_name: str = "bdp_csrf"
    # 登录安全：连续失败达到阈值先要求验证码，再达到上限锁定
    login_captcha_threshold: int = 3
    login_max_failures: int = 5
    login_lock_seconds: int = 900
    captcha_ttl_seconds: int = 300

    # 模拟数据
    mock_seed: int = 20260926
    mock_days: int = 90
    mock_brands: int = 4

    # Agent 运行时
    agent_workers: int = 4  # 有效并发会被钳制：SQLite 单写者模式下强制 1
    # inline：编排与执行都在当前进程（SQLite 唯一合法形态，V3 行为）
    # process：API/CLI 只入队，python -m bdp.worker 独立执行（PostgreSQL 下可用）
    agent_worker_mode: str = "inline"
    agent_lease_sec: int = 60          # worker 租约时长（认领后定期续租）
    agent_max_concurrent_runs: int = 0  # 背压：running 状态 run 数上限（0 = 不限；生产建议 3）
    agent_dq_gate: float = 0.99  # 质量门禁：overall_pass_rate 低于该值视为 block
    agent_dq_gate_action: str = "degraded"  # block 时的动作：skip | degraded | hold（待审批）
    agent_approval_ttl_hours: int = 24  # waiting_approval 超时自动 skip
    agent_heartbeat_sec: int = 30  # 心跳间隔（任务阶段边界更新）
    agent_event_retention_days: int = 14
    agent_default_timeout_sec: int = 900

    # Planner（P1：目标 → 模板白名单 DAG；off 关闭 goal API）
    agent_planner: str = "rules"  # off | rules（LLM 模板规划为可选增强，接口一致）
    # Verifier（P2：nightly 追加只读复核任务）
    agent_verifier: bool = False
    agent_memory_ttl_days: int = 30  # 记忆 TTL（0 = 永久）

    # LLM（InsightAgent 用，none 表示关闭并走降级路径）
    llm_backend: str = "none"  # none | openai
    llm_model: str = ""
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_timeout_sec: float = 60.0

    # Agentic RAG（检索管线升级，docs/AGENTIC_RAG_PLAN.md）
    # single：现状直通（一次检索）；agentic：规划→检索→判定→改写/多跳→压缩→引用核验
    rag_strategy: str = "single"
    rag_max_rewrites: int = 2          # 改写预算（额外检索次数上限）
    rag_max_hops: int = 2              # 多跳预算
    rag_judge_threshold: float = 0.4   # 检索充分性阈值（按实测分布校准：中位 0.55/p25 0.45，取 p25 下方）
    rag_judge_backend: str = "heuristic"  # heuristic | llm
    rag_rerank_backend: str = "lexical"   # lexical | bge（bge 需 sentence-transformers）
    rag_compress_token_budget: int = 3000
    rag_session_turns: int = 5         # 会话记忆保留轮数
    rag_top_k: int = 4                 # 管线每跳召回的最终保留数

    log_level: str = "INFO"

    # CORS 允许来源（逗号分隔）。默认为空 = 不启用 CORS 中间件：
    # 前端由本服务同源托管，无需跨域；file:// 双击打开的演示场景走同源兜底。
    # 需要跨域接入时显式配置，如：BDP_CORS_ORIGINS=http://localhost:5173,https://bi.example.com
    # 注意 allow_credentials=True 时 allow_origins 不能是 "*"（违反 CORS 规范，浏览器会拒绝）。
    cors_origins: str = ""

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        d = PROJECT_ROOT / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
