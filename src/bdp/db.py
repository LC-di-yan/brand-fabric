"""数据库连接与会话管理。

设计说明
--------
本模块用 SQLAlchemy 同时支持两种后端：
- SQLite  ：lite 模式，零依赖，便于在任意机器上跑通与做单元测试
- PostgreSQL：full 模式，docker-compose 中的正式存储

两者的差异只体现在 DATABASE_URL 上，上层加工与指标计算代码不做任何方言判断，
保证"本地验证的 SQL 到生产环境语义一致"。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from bdp.config import PROJECT_ROOT, settings


def _resolve_sqlite_url(url: str) -> str:
    """把相对路径的 SQLite URL 解析到项目根目录，并确保父目录存在。

    否则从不同工作目录启动（例如 IDE 与命令行）会得到不同的库文件，
    这是本地开发里非常常见的"数据不见了"类问题。
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return url
    raw = url[len(prefix):]
    if not raw or raw == ":memory:" or raw.startswith(":memory:"):
        return url
    p = Path(raw)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return prefix + p.as_posix()


_database_url = _resolve_sqlite_url(settings.database_url)
_is_sqlite = _database_url.startswith("sqlite")

_connect_args: dict = {}
_engine_kwargs: dict = {"pool_pre_ping": True}

if _is_sqlite:
    # SQLite 在多线程（FastAPI 线程池）下需要关闭同线程检查
    _connect_args["check_same_thread"] = False
else:
    _engine_kwargs.update(pool_size=10, max_overflow=20)

engine: Engine = create_engine(_database_url, connect_args=_connect_args, **_engine_kwargs)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _):
        """SQLite 默认不开外键，且并发写容易锁；这里显式开启 WAL 与外键。"""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文：正常提交，异常回滚。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_context(commit_on_exit: bool = True) -> Iterator[Session]:
    """可配置提交行为的会话上下文。

    session_scope 等价于 session_context(commit_on_exit=True)。
    agent 运行时用它统一事务边界；个别"写后必须立即可见"的场景
    （如知识单文档操作）可显式传 commit_on_exit=False 自行提交。
    """
    session = SessionLocal()
    try:
        yield session
        if commit_on_exit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db(drop: bool = False) -> None:
    """建表。drop=True 时先删后建（仅用于本地重建，切勿在生产使用）。"""
    from bdp import models  # noqa: F401  确保模型已注册到 metadata
    from bdp.models import Base

    if drop:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
