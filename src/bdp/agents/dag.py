"""DAG：任务依赖的声明与校验。

DAG 是纯 Python 声明（不引入图库——节点数是个位数，拓扑排序十几行）。
TaskDef 声明任务用什么 agent、依赖谁、需要黑板上哪些 artifact；
fan_out 标记的节点在运行时按租户展开成子任务（见 orchestrator）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bdp.agents.errors import FatalError


@dataclass
class TaskDef:
    name: str
    agent: str
    deps: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    # 执行前从黑板合并进 ctx.artifacts 的 key 列表
    artifact_keys: list[str] = field(default_factory=list)
    # 覆盖 spec.write_domains（收窄边界，如 dwd 三路各自只允许写自己的表）
    write_domains: tuple[str, ...] | None = None
    # 并发锁键；缺省用 write_domains。fan-out 子任务自动追加租户后缀
    lock_keys: list[str] | None = None
    # 运行时按租户 fan-out（目前仅 metrics prepare 使用）
    fan_out: str | None = None  # None | "tenants"


@dataclass
class Dag:
    dag_id: str
    tasks: list[TaskDef]
    description: str = ""

    def task(self, name: str) -> TaskDef:
        for t in self.tasks:
            if t.name == name:
                return t
        raise FatalError(f"DAG {self.dag_id} 中不存在任务 {name}")

    def validate(self) -> None:
        """声明期校验：名字唯一、依赖存在、无环、agent 已注册。"""
        from bdp.agents import registry

        names = [t.name for t in self.tasks]
        if len(names) != len(set(names)):
            raise FatalError(f"DAG {self.dag_id} 任务名重复")
        name_set = set(names)
        for t in self.tasks:
            for d in t.deps:
                if d not in name_set:
                    raise FatalError(f"任务 {t.name} 依赖了不存在的任务 {d}")
        self._assert_acyclic()
        for t in self.tasks:
            try:
                registry.get(t.agent)
            except FatalError as exc:
                raise FatalError(f"任务 {t.name}：{exc}") from exc

    def _assert_acyclic(self) -> None:
        # Kahn 拓扑排序：能排完即无环
        indeg = {t.name: 0 for t in self.tasks}
        out: dict[str, list[str]] = {t.name: [] for t in self.tasks}
        for t in self.tasks:
            for d in t.deps:
                out[d].append(t.name)
                indeg[t.name] += 1
        queue = [n for n, d in indeg.items() if d == 0]
        seen = 0
        while queue:
            n = queue.pop()
            seen += 1
            for m in out[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        if seen != len(self.tasks):
            raise FatalError(f"DAG {self.dag_id} 存在循环依赖")


def _nightly() -> Dag:
    """全链路日批：接入 → 数仓三路并行 → 汇总 → [质量 ‖ 知识库] → 指标（带门禁 + 租户展开）。"""
    return Dag(
        dag_id="nightly",
        description="接入 → dwd 三路 → dws → quality ‖ kb_rebuild → metrics(fan-out 按租户)",
        tasks=[
            TaskDef(name="ingest", agent="ingest", deps=[]),
            TaskDef(
                name="dwd_orders", agent="pipeline", deps=["ingest"],
                params={"domain": "orders"}, write_domains=("dwd_order",),
                lock_keys=["dwd_orders"],
            ),
            TaskDef(
                name="dwd_refunds", agent="pipeline", deps=["ingest"],
                params={"domain": "refunds"}, write_domains=("dwd_refund",),
                lock_keys=["dwd_refunds"],
            ),
            TaskDef(
                name="dwd_cs", agent="pipeline", deps=["ingest"],
                params={"domain": "cs"}, write_domains=("dwd_cs",),
                lock_keys=["dwd_cs"],
            ),
            TaskDef(
                name="dws", agent="pipeline", deps=["dwd_orders", "dwd_refunds", "dwd_cs"],
                params={"domain": "dws"}, write_domains=("dws",), lock_keys=["dws"],
            ),
            TaskDef(name="quality", agent="quality", deps=["dws"]),
            TaskDef(name="kb_rebuild", agent="kb", deps=["dws"]),
            TaskDef(
                name="metrics", agent="metrics", deps=["dws", "quality"],
                params={"kind": "prepare"},
                artifact_keys=["dq"],
                fan_out="tenants",
            ),
        ],
    )


DAGS: dict[str, Dag] = {"nightly": _nightly()}


def get_dag(dag_id: str) -> Dag:
    dag = DAGS.get(dag_id)
    if dag is None:
        raise FatalError(f"未知 DAG：{dag_id}（可用：{sorted(DAGS)}）")
    return dag
