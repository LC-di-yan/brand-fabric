"""Agent 运行基座：写域守卫会话。

WriteDomainViolation 的强制点覆盖现有代码的全部三种写入路径：
1. session.add() / 对象属性修改 / session.delete()  → before_flush 事件拦截
2. session.execute(delete(Model)/update(M)/insert(M)) → execute 覆写按语句目标表拦截
3. session.bulk_insert_mappings / bulk_update_mappings → 方法覆写按 mapper 拦截

注意：text() 原生写语句不在防护范围内（全部存量代码均使用 ORM 构造，
质量规则执行只读）。这是刻意的取舍：防护成本与真实风险对齐。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Delete, Insert, Update, event
from sqlalchemy.orm import Session, class_mapper, sessionmaker

from bdp.agents.errors import WriteDomainViolation
from bdp.agents.spec import AgentSpec, table_names_for_domains
from bdp.db import engine


class GuardedSession(Session):
    """只允许写声明写域内表的 Session。"""

    _allowed_tables: frozenset[str] = frozenset()

    def _guard(self, table_name: str) -> None:
        if table_name not in self._allowed_tables:
            raise WriteDomainViolation(
                f"写域违例：禁止写表 {table_name}（允许范围：{sorted(self._allowed_tables)}）"
            )

    def execute(self, statement, *args, **kwargs):
        if isinstance(statement, (Delete, Update, Insert)):
            name = getattr(getattr(statement, "table", None), "name", None)
            if name is not None:
                self._guard(name)
        return super().execute(statement, *args, **kwargs)

    def bulk_insert_mappings(self, mapper, *args, **kwargs):
        self._guard(class_mapper(mapper).local_table.name)
        return super().bulk_insert_mappings(mapper, *args, **kwargs)

    def bulk_update_mappings(self, mapper, *args, **kwargs):
        self._guard(class_mapper(mapper).local_table.name)
        return super().bulk_update_mappings(mapper, *args, **kwargs)


_guarded_local = sessionmaker(
    bind=engine, class_=GuardedSession, autoflush=False, expire_on_commit=False, future=True
)


@contextmanager
def guarded_session(spec: AgentSpec, *, domains=None, commit_on_exit: bool = True) -> Iterator[Session]:
    """打开一个按（任务级覆盖的）写域守卫的会话上下文。

    domains 为 None 时用 spec.write_domains；fan-out 子任务等场景可声明更窄的边界。
    """
    allowed = frozenset(table_names_for_domains(domains or spec.write_domains))

    session: GuardedSession = _guarded_local()
    session._allowed_tables = allowed

    def _before_flush(sess: Session, _flush_ctx, _instances) -> None:
        for objs in (sess.new, sess.dirty, sess.deleted):
            for obj in objs:
                name = getattr(type(obj), "__tablename__", None)
                if name is not None:
                    sess._guard(name)

    event.listen(session, "before_flush", _before_flush)
    try:
        yield session
        if commit_on_exit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        event.remove(session, "before_flush", _before_flush)
        session.close()
