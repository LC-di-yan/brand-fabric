# 多 Agent 协作系统演进方案（brand-fabric）

> 基于 2026-09-28 对 `agents/` 全模块（spec/base/state/dag/orchestrator/registry + 6 个 agent）
> 与 `api/routers/agents.py` 的逐文件阅读。V3 重构方案（`AGENT_REFACTOR_PLAN.md`）已全部落地，
> 本方案回答下一个问题：**V3 解决了"批量任务怎么可靠地跑"，这里解决"智能任务怎么协同地想"。**
> 所有新组件为纯 Python + SQLAlchemy 实现，零强制新增依赖；SQLite inline 模式行为保持不变。

---

## 第一部分 · 现状与边界

### 1.1 已有能力（V3 落地清单，均有测试背书）

| 能力 | 实现位置 |
|---|---|
| DB 任务表消息总线（run/task/artifact/event） | `agents/state.py` |
| 静态 DAG 声明 + 校验（名字唯一/依赖存在/无环/agent 已注册） | `agents/dag.py`（Kahn 拓扑） |
| 抢占式派发（UPDATE...WHERE status='pending'）+ 写域锁 | `agents/orchestrator.py` |
| 写域运行时守卫（flush/DML/bulk 三路拦截） | `agents/base.py` GuardedSession |
| 指数退避重试 + Retryable/Fatal 分类 + 下游短路 | `agents/errors.py`、orchestrator |
| 协作式超时 + 心跳崩溃恢复（reap_stale_tasks） | orchestrator |
| 租户 fan-out 并行物化（SQLite 单写者自动降串行） | dag.py fan_out、orchestrator |
| 质量门禁（pass_rate 阈值 → skip/degraded） | quality_agent + orchestrator |
| InsightAgent 工具循环 + 确定性降级 | insight_agent.py |
| API：触发/列表/明细/事件/重试/capabilities/ask | `api/routers/agents.py` |

### 1.2 边界（诚实清单，每条对应本方案一节）

| # | 边界 | 症状 |
|---|---|---|
| 1 | **静态 DAG**：只有 nightly 一张图，新分析需求要改 `dag.py` 代码 | "分析 T001 近 30 天退款异常"无法作为目标提交 |
| 2 | **执行体在 API 进程内**（`routers/agents.py:26` 的 `_running` 线程表） | API 重启编排即停（可恢复但中断）；无法水平扩 worker；API 进程承担批处理负载 |
| 3 | **无结论复核**：物化结果没有第二个"眼睛" | 指标算错只能靠 DQ 规则（输入侧），输出侧无校验 |
| 4 | **无人工审批闸口**：门禁 block 时自动 degraded/skip | 高风险动作（如带降级标记物化）绕过了人 |
| 5 | **无跨 run 记忆**：artifact 是黑板但 run 结束即死，agent 之间无经验沉淀 | 同类问题每次从零开始 |
| 6 | **artifact 无 schema 约束**：消费者裸读 dict | 生产者改字段名，消费者静默拿到 None |
| 7 | **可观测缺成本视角**：event 是流水，无 token/延迟/重试率聚合 | LLM 成本失控不可见；慢 agent 无法定位 |
| 8 | **无背压**：可以无限并发提交 run | 大 run 挤垮小 run，无配额 |

---

## 第二部分 · 目标与非目标

### 2.1 目标

1. **目标级编排**：`POST /v1/agent/goals` 提交自然语言/结构化目标 → 自动生成合法 DAG（模板白名单内）并执行；
2. **执行体独立**：worker 从 API 进程分离（`python -m bdp.worker`），租约式认领，可多实例；
3. **结论复核协作**：只读 Verifier agent 交叉校验物化结果，fail 阻断下游；
4. **人机协同**：关键动作支持 `waiting_approval` 状态 + 审批 API；
5. **三层记忆**：working（run 级 artifact）/ episodic（run 摘要）/ semantic（KB 共享语义记忆）；
6. **可观测**：每 agent 延迟/重试率/LLM token 成本进 Prometheus；run 详情可回放；
7. **兼容承诺**：nightly 结果逐表等价、SQLite inline 全功能、现有测试零回归。

### 2.2 非目标

- 不做通用自主 agent 平台（不让 LLM 生成自由代码/自由 DAG）；
- 不引入 K8s、Kafka/NATS 为强依赖（接口预留，触发条件见 §4.9）；
- 不追求多 agent"辩论"的学术趣味——每个协作模式都要能回答"它拦住了什么事故"。

---

## 第三部分 · 演进总览

| 能力 | 现状 | 目标 | 涉及模块 | 阶段 |
|---|---|---|---|---|
| 执行体 | API 进程内线程 | 独立 worker 进程 + 租约认领 | `worker.py`、`state.py`、`orchestrator.py` | P0 |
| 规划 | 静态 nightly DAG | Planner + 模板白名单 → 动态 DAG | `agents/planner.py`、`agents/templates.py` | P1 |
| 复核 | 无 | VerifierAgent（只读）嵌入 nightly 与 goal | `agents/verifier_agent.py` | P2 |
| 审批 | 门禁自动 degraded | `waiting_approval` + 审批 API | `state.py`、`routers/agents.py`、`admin` | P2 |
| 记忆 | run 级 artifact | 三层记忆（working/episodic/semantic） | `agents/memory.py`、新表 `agent_memory` | P3 |
| 协议 | artifact 裸 dict | 信封 + schema 版本 + pydantic 校验 | `spec.py`、`state.py` | P3 |
| 可观测 | event 流水 | 每 agent 指标 + token 成本 | `middleware.py`、`agents/*` | P0 |
| 背压 | 无 | max_concurrent_runs | `orchestrator.prepare` | P0 |

---

## 第四部分 · 分项设计

### 4.1 执行体分离与租约认领（P0，其余各项的地基）

**现状问题**：`routers/agents.py` 在 API 进程里起 `threading.Thread` 跑编排循环；
批处理负载与在线服务共享进程，且只能单实例。

**设计**：

```
API 进程（uvicorn）                     Worker 进程（可 1..N）
POST /v1/agent/runs                    python -m bdp.worker --id w1
  └─ orchestrator.prepare()            循环:
     创建 run + 任务入队                  1. claim: UPDATE agent_task
     返回 run_id（立即返回）                 SET status='running', worker_id=:wid,
                                             lease_expires=now()+:lease
┌─────────────────────────────┐          WHERE task_id IN (
│ agent_task 表 = 消息总线     │            SELECT ... WHERE status='pending'
│ （现有，不变）               │            AND deps 已满足
└─────────────────────────────┘          )
                                         2. 执行（GuardedSession 不变）
                                         3. finish / 心跳续租
```

- `state.py` 新增 `claim_tasks(worker_id, lease_sec) -> list[AgentTask]`：
  抢占语义沿用现有"UPDATE...WHERE status='pending'"，加 `lease_expires` 列；
  `reap_stale_tasks` 升级为按 `lease_expires` 回收（现有按心跳时间，兼容）。
- **租约 vs 心跳**：现有心跳是"任务阶段边界更新时间戳"，租约是"worker 定期续期"——
  两者叠加：租约判 worker 死，心跳判任务卡死，语义分层。
- SQLite 模式：`worker_mode=inline`，编排仍在原进程内执行（现状路径原样保留），
  `claim` 退化为内存派发——**inline 是 process 的一个特例，不是两条代码路径**：
  区别只在 Dispatcher 实现（InlineDispatcher / LeaseDispatcher），orchestrator 主循环不动。
- 优雅停机：worker 收到 SIGTERM 后不再认领新任务、跑完手中任务、释放锁退出（finishing 界）。

**测试**（`test_worker.py`）：双 worker 并发认领不双跑；租约过期回收后被另一 worker 接管；
优雅停机不丢任务；inline 与 process 模式对同一 DAG 产出逐表等价。

### 4.2 Planner：目标 → 动态 DAG（P1）

**核心原则：LLM 只做"选模板、填参数"，不做自由规划。**

```
goal: "分析 T001 近 30 天退款异常"
        │
┌───────▼────────────┐
│ PlannerAgent        │  LLM/规则 → Plan {steps: [TemplateCall]}
│ agents/planner.py   │  模板白名单 = 现有 5 个确定性 agent 的参数化封装
└───────┬────────────┘
        │ 实例化 TaskDef[]
┌───────▼────────────┐
│ dag.validate()      │  复用现有校验：名字唯一/依赖存在/无环/agent 已注册
└───────┬────────────┘
        │ 写域守卫在 TaskDef.write_domains 声明（模板内固化，LLM 不可改）
        ▼
orchestrator.execute()（现有，零改动）
```

**模板白名单**（`agents/templates.py`，首版 6 个）：

| 模板 | 参数（pydantic 校验） | 实例化的 TaskDef |
|---|---|---|
| `window_metrics` | days, tenant? | metrics prepare + fan-out |
| `dq_deep_scan` | rule_ids?, tenant? | quality（参数化规则子集） |
| `kb_refresh` | tenant?, doc_ids? | kb_rebuild |
| `full_refresh` | days, tenant? | ingest → dwd 三路 → dws → quality ‖ kb → metrics |
| `caliber_diff` | metric_code, versions[2] | 新只读对账任务（P2 Verifier 的一部分） |
| `rag_answer` | question, tenant | 调 Agentic RAG pipeline 的问答任务（联动方案） |

**安全边界**：
- LLM 输出被 pydantic 模板 schema 硬校验，未知字段拒绝；生成的 DAG 标记 `params.planner="llm"`；
- 模板内写域固化，LLM 无法声明新的 `write_domains`（写域守卫照常生效）；
- 目标继承发起者租户（TenantScope），LLM 参数里出现 tenant 冲突直接拒绝；
- 降级：无 LLM → 关键词规则映射（"退款/异常"→ dq_deep_scan + window_metrics），覆盖高频 80% 诉求。

**API**：`POST /v1/agent/goals {goal, tenant?, dry_run?}` → `{run_id, plan}`；
`dry_run=true` 只返回 plan 不执行（前端可预览 DAG）。

**测试**（`test_planner.py`）：模板越权拒绝（LLM 伪造写域/未知模板/跨租户参数）；
规则降级路径；dry_run 不留 run 记录；生成的 DAG 与手写等价时结果等价。

### 4.3 结论复核协作：VerifierAgent（P2）

**协作模式选型**：引入"生产者-复核者"（producer-verifier）而不是"辩论"（debate）——
复核者回答的是"这次物化可信吗"，有明确的拦截对象：**输出侧事故**（口径用错版本、行数异常、
门禁被降级绕过）。辩论模式适合主观判断，数据物化没有主观空间。

**设计**：

- `agents/verifier_agent.py`：`write_domains=()`（纯只读），夜间 DAG 在 metrics 之后追加 `verify` 任务：
  1. 行数水位：`metric_result` 今日行数 vs 历史均值 ±X%（异常波动拦截）；
  2. 口径一致性：物化记录的 caliber_version 与字典当前生效版本对账；
  3. 门禁审计：本轮是否有任务被 degraded/skip，原因是否与 DQ verdict 一致；
  4. 产出 `verify` artifact {verdict, findings[]}，fail → 下游（无下游则标 run=degraded）。
- goal 模式下 `caliber_diff` 模板复用同一 Verifier 的对账能力（v1.0 vs v1.1 差异报告）。

**测试**（`test_verifier.py`）：注入"行数骤降"数据 → verdict=fail 且 run 标记；
口径版本错配被捕获；只读断言（verifier 尝试写任何表 → WriteDomainViolation）。

### 4.4 人机协同审批（P2）

- `agent_task.status` 增加 `waiting_approval`（非终态；`TERMINAL_STATUSES` 不变）；
- 门禁动作新增 `hold`：`BDP_AGENT_DQ_GATE_ACTION=hold` 时，block 的下游任务进入 `waiting_approval`
  而非 skip/degraded；
- API：`POST /v1/agent/tasks/{id}/approve` / `/reject`（admin/ops，写审计日志复用 `security/audit.py`）；
  审批通过 → pending 继续编排；拒绝 → skipped(带 reason)；
- 超时策略：`waiting_approval` 超过 `BDP_AGENT_APPROVAL_TTL`（默认 24h）→ skipped，event 留痕；
- 前端 agents 视图：任务行显示"待审批"徽章 + 通过/拒绝按钮（复用现有重试按钮的交互骨架）。

**测试**（`test_approval.py`）：hold 生效、approve 后下游续跑、reject 后下游 skipped、
TTL 超时、非 admin 审批 403、审批动作落审计。

### 4.5 三层记忆（P3）

| 层 | 载体 | 生命周期 | 用途 |
|---|---|---|---|
| working | 现有 `agent_artifact`（run 级黑板） | run 内 | 任务间传值（现状，补 schema 版本） |
| episodic | 新表 `agent_memory`（scope=run_summary） | 跨 run，TTL 默认 30 天 | run 结束时聚合摘要（结论/异常/成本），下次同类 goal 冷启动注入 |
| semantic | `kb_document`（现有知识库） | 持久 | agent 产出可沉淀为知识（如口径说明），**必须走审批**（见 4.4）后经 `kb/manage.py` 入库 |

- `agents/memory.py`：`remember(scope, key, value, ttl)` / `recall(scope, key)`；
  `agent_memory` 表带 `(scope, scope_id, key)` 唯一键与租户列，**写域守卫自动覆盖**
  （memory 写入按 agent 的 write_domains 检查，默认 agent 无 memory 写权，需在 spec 显式声明）；
- 冷启动注入：planner 组装 prompt 时 `recall` 同类 run_summary（最多 3 条），超出上下文预算丢弃。

**测试**（`test_memory.py`）：写域外的 memory 写入被拦；TTL 过期不可 recall；
run_summary 自动生成；冷启动注入数量上限。

### 4.6 通信协议标准化（P3）

- artifact 统一信封：`{schema_version, produced_by, produced_at, refs[], payload}`；
  `schema_version` 由各 agent 声明（如 `metrics.v1`），消费者 pydantic 校验，不匹配 → FatalError
  （显式失败优于静默拿 None）；
- 任务间传值仍只允许 artifact 引用（现状原则），补大小上限（`BDP_AGENT_ARTIFACT_MAX_BYTES`，默认 1MB），
  大对象出库到文件/KB 并在 payload 里放引用；
- 消息信封扩展 event：`{type, refs[], payload}`——为未来换 broker 预留（见 4.9）。

### 4.7 可观测性（P0，最先见效）

- `agent_run` 表增加 `llm_prompt_tokens` / `llm_completion_tokens` 列（insight/planner 上报，
  `agents/llm.py` 返回值已带 usage，透传即可）；
- Prometheus 指标（挂进现有 `api/middleware.py` 的 registry）：
  - `bdp_agent_task_duration_seconds{agent, status}`（histogram）
  - `bdp_agent_task_retries_total{agent}`
  - `bdp_agent_llm_tokens_total{agent, kind}`（counter）
  - `bdp_agent_runs_active`（gauge）
- run 详情聚合视图：`GET /v1/agent/runs/{id}` 响应增加 `summary{duration_by_agent, retries, tokens}`；
- 前端：运行明细表加"耗时/尝试"已有，补 token 列与任务时间线（events 流聚合，不引入新图库）。

### 4.8 可靠性补强（P0）

- **幂等**：fan-out 子任务键从 `{name}:{tenant}` 升级为显式 `idempotency_key`（`run_id + name + key`），
  重复入队被唯一约束拦截；
- **背压**：`BDP_AGENT_MAX_CONCURRENT_RUNS`（默认 3）：`prepare()` 检查 running 数，超限返回 429
  （API 语义）或排队（worker 语义，goal 模式默认排队）；排队深度进指标 `bdp_agent_queue_depth`；
- **死信视图**：重试耗尽 `failed(retryable=False)` 已有；runs 列表 API 支持 `filter=dead_letter`，
  前端加过滤器（一键复位已有，不重复造）。

### 4.9 分布式路径（预留，不实施）

- `StateStore` 已是 broker 无关接口；`LeaseDispatcher`（4.1）落地后，多 worker 天然可水平扩
  （同一 PG 库即可）；
- **换 broker 的触发条件**（写在这里防止过度设计）：任务吞吐 > DB 单表写入承载
  （经验值：持续 >50 任务/秒）或需要跨机房派发。届时实现 `BrokerStore` 适配器
  （NATS/Redis Streams），orchestrator 主循环与 agent 代码零改动。

---

## 第五部分 · 安全边界（贯穿）

| 边界 | 机制 |
|---|---|
| 写域守卫覆盖新 agent | registry 注册时校验 AgentSpec.write_domains；Verifier 只读由空写域保证 |
| LLM 不可越模板 | 模板 pydantic schema 硬校验 + 写域模板内固化（4.2） |
| 租户不可被参数覆盖 | TenantScope 继承发起者；planner 参数中的 tenant 与之冲突即拒绝 |
| RAG 内容不可信 | 联动《Agentic RAG 方案》§5.5：检索文本只作 data，不进 system prompt |
| 审批权收敛 | approve/reject 仅 admin/ops + 审计留痕（`security/audit.py`） |
| LLM 工具白名单 per-agent | AgentSpec 增加 `allowed_tools`（insight 现有工具表收编为显式声明） |

---

## 第六部分 · 配置项（`config.py` 追加）

```python
agent_worker_mode: str = "inline"      # inline（现状，SQLite 必须）| process
agent_lease_sec: int = 60              # 租约时长
agent_max_concurrent_runs: int = 3     # 背压
agent_planner: str = "off"             # off | rules | llm
agent_approval: str = "off"            # off | on（hold 动作需开启）
agent_approval_ttl_hours: int = 24
agent_verifier: str = "off"            # off | on（nightly 追加 verify 任务）
agent_artifact_max_bytes: int = 1_000_000
agent_memory_ttl_days: int = 30
```

---

## 第七部分 · 实施阶段

| 阶段 | 内容 | 出口条件 | 工作量 |
|---|---|---|---|
| **P0 执行体与观测** | worker 分离（inline/process 双 Dispatcher）+ 租约 + 幂等键 + 背压 + Prometheus 指标 + token 成本 | 双 worker 等价性测试绿；指标端点可见 agent 指标；nightly 等价 | ~1 周 |
| **P1 Planner** | 模板白名单 6 个 + goal API（dry_run）+ 规则降级 | 越权测试全绿；"退款异常"类 goal 端到端可跑 | ~1 周 |
| **P2 复核与审批** | VerifierAgent 入 nightly + hold 动作 + 审批 API/前端 | 行数骤降被拦；审批闭环 + 审计 | ~1 周 |
| **P3 记忆与协议** | 三层记忆 + artifact 信封 + schema 校验 | 写域拦截/TTL/schema 不匹配显式失败 | ~1 周 |

依赖：P0 → P1（planner 派发依赖 worker 分离）；P2、P3 可并行。
可与《Agentic RAG 方案》并行（唯一交点：P2 的 Verifier 可消费 RAG 的 GroundingReport）。

---

## 第八部分 · 兼容承诺（验收红线）

1. `nightly` 结果与 V3 **逐表逐和等价**（现有等价性测试保持绿）；
2. SQLite 下 `agent_worker_mode=inline` 行为与现状完全一致（process 模式在 SQLite 下拒绝启动并提示）；
3. 现有 API 字段只增不删；`POST /v1/agent/runs` 行为不变；
4. 全部现有测试零回归；每阶段新增测试随阶段交付（预计 +40 项左右）；
5. 零强制新增依赖——LLM、向量重排、未来 broker 全部是可选后端，off 档完整可用。

---

## 附：与《Agentic RAG 演进方案》的分工

| | 本方案 | Agentic RAG 方案 |
|---|---|---|
| 层 | 编排层（谁在什么时候跑什么） | 检索工具层（单个问题怎么被回答） |
| 智能体 | Planner / Verifier / 现有 5 个确定性 agent | QueryPlanner / Judge / MultiHop（管线内组件，非注册 agent） |
| 共享 | `agents/llm.py`、trace/观测通道、审批、写域守卫、降级哲学 ||

判断一个新需求归谁的试金石：**它改变"任务怎么被调度"→ 本方案；它改变"检索怎么发生"→ RAG 方案。**
