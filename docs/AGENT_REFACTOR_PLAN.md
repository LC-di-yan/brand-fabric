# 多 Agent 协作系统重构方案（brand-data-platform）

> 基于 2026-09-28 对全部源码（src/bdp 共 41 个 Python 文件、约 7335 行；tests 7 个文件 1170 行）的逐模块阅读。
> 所有结论均标注真实代码位置；所有新组件均为纯 Python + SQLAlchemy 实现，**零强制新增依赖**。

---

## 第一部分 · 项目现状分析

### 1.1 技术栈与规模

| 项 | 内容 |
|---|---|
| 语言/框架 | Python 3.11 · FastAPI · SQLAlchemy 2.0（全同步，靠 FastAPI 线程池并发） |
| 存储 | SQLite（lite）/ PostgreSQL（full），双后端仅靠 URL 切换（`db.py`） |
| 向量库 | Milvus / 本地 numpy 双后端（`kb/store.py` Protocol 抽象） |
| 关键依赖 | pydantic-settings、PyJWT、rapidfuzz、numpy、pymilvus、httpx（仅测试用） |
| 前端 | 两个单文件 HTML（login 148 行 + dashboard 875 行五视图 vanilla JS） |
| 测试 | 77 项，conftest 在 import bdp 前注入环境变量（tests/conftest.py:16-24） |
| 版本管理 | **尚未 git 化**（重构前必须先做） |

### 1.2 模块划分与核心数据流

```
批处理链（cli.py 顺序编排）:
  mock/generator.py ──▶ raw_order/refund/cs_session
    ──▶ pipeline/dwd.py（清洗 + mdm 三级映射 + 质量标记，不丢行只打标）
    ──▶ pipeline/dws.py（shop_day / tenant_day，三口径按各自事件日归集）
    ──▶ pipeline/quality.py（6 条 DQ 规则 → dq_result）
    ──▶ metrics/engine.py（指标字典 → 声明式 SQL → metric_result 物化）

知识链:
  kb_document ──▶ kb/ingest.py（切片→哈希去重→向量化）──▶ kb/store.py（Milvus/本地）
  查询: api/routers/kb.py ──▶ kb/retriever.py（dense+sparse 双路 → RRF → 词面重排）

服务链:
  api/deps.py tenant_guard（全项目唯一租户入口）──▶ 各 router ──▶ 业务函数 ──▶ Session
```

### 1.3 耦合点（按严重程度排序）

| # | 耦合点 | 位置 | 影响 |
|---|---|---|---|
| C1 | **编排逻辑硬编码在 CLI**：`cmd_all` 顺序调用 5 个命令，步骤间隐式依赖（`cmd_metrics` 直接查 `dws_tenant_day` 的 MIN/MAX 作为物化窗口），无 run 级状态、无重试、无门禁 | `cli.py:217-222`, `cli.py:90-91` | 想加一步（如 DQ 门槛）要同时改 CLI 与 `tests/conftest.py:85-106`（编排顺序在测试里又抄了一遍） |
| C2 | **模块级单例**：`settings`（lru_cache）、`engine`、`SessionLocal` 在 import 时创建 | `config.py:68-73`, `db.py:45-57` | 任何并发/多配置组件（agent runtime）不能按运行上下文取会话；测试被迫"import 前设环境变量" |
| C3 | **指标层反向依赖安全层**：`metrics/engine.py` import `security.auth.build_tenant_filter_sql`，且指标 SQL 靠字符串拼接（agg_expr/filter_expr 来自 DB，白名单约束 + 手工转义） | `metrics/engine.py:24`, `64, 80-87, 135, 227` | agent 化后租户上下文来源增多，这条"唯一注入路径"必须保持唯一，否则隔离失效 |
| C4 | **事务边界不统一**：CLI 用 `session_scope`（自动提交）；`kb/manage.py` 里手动 `session.commit()`；`tenant_guard` 依赖里也 commit | `db.py:73-84`, `kb/manage.py:121,158`, `api/deps.py:113` | agent 内嵌套调用业务函数时容易出现"半提交"状态，重构前必须收敛 |
| C5 | **各模块自定义异常**：`MetricError`、`KbManageError` 各自继承 ValueError，router 层逐一 try/except 转 HTTPException | `metrics/engine.py:36`, `kb/manage.py:25`, 各 router | 重试/降级策略无法按统一异常分类，agent 层需要统一的"可重试/不可重试"判定 |
| C6 | `mock/generator.py` 763 行单文件混 4 类职责（主数据/交易/会话/知识/用户） | 全文件 | IngestionAgent 拆分时顺手切分，避免继续膨胀 |

### 1.4 性能与可维护性瓶颈

| # | 瓶颈 | 位置 | 说明 |
|---|---|---|---|
| P1 | **每个检索请求重建向量库**：`/v1/kb/search` 未传 store → `search()` fallback `build_store()` → LocalVectorStore 构造时全量加载 npz + 重建倒排索引 | `api/routers/kb.py:47-55`, `kb/retriever.py:114`, `kb/store.py:100-123` | 当前 424 切片尚可，数据量涨后每个请求都是 O(全库) IO；且 `_load` 全量读、`_save` 全量重写 npz |
| P2 | **全量重建模式贯穿批处理**：dwd/dws/metric 全部 delete-all + insert-all；`materialize` 每次重算 19 targets × 全租户 | `pipeline/dwd.py:41,146,227`, `pipeline/dws.py:115-116`, `metrics/engine.py:414` | 无增量/断点；一次失败全部重来。agent 化的"重试"价值被全量重建稀释，需配套增量或按租户分片 |
| P3 | **内存换 SQL**：`build_dwd_refunds` 把全部订单 pay_amount/sku 装入 dict | `pipeline/dwd.py:149-157` | 5.5 万行可行，亿行不可行 |
| P4 | **DQ004 相关子查询逐行核对** | `pipeline/quality.py:44-47` | O(n·m) 风险 |
| P5 | **SQLite 单写者**：WAL 允许多读单写 | `db.py:59-67` | 多 agent 并发写同一库会 `database is locked`——这是并发设计的**硬约束**，lite 模式必须默认串行 |
| P6 | LocalVectorStore 无进程内锁；API 线程池写（manage.upsert）与批量 ingest 并发时有竞态 | `kb/store.py:79+` | agent 并发写向量库需要按域加锁 |
| M1 | 批处理不可观测：dq_result 有 run_id，但 pipeline 步骤无 run 概念，失败只能翻日志 | 全局 | agent 化的核心收益点 |
| M2 | 无统一业务异常基类 / 无重试语义 | C5 | 同上 |

### 1.5 哪些模块适合拆成 Agent（判断标准与结论）

判断标准：① 输入输出可类型化；② 有独立数据域与独立失败语义；③ 拆出后获得真实收益（故障隔离 / 并行 / 可观测 / 独立演进），而非为拆而拆。

| 模块 | 结论 | 理由 |
|---|---|---|
| raw 接入（mock/generator + raw 表写入） | ✅ **IngestionAgent** | 独立失败域；天然可按租户/平台分片；生产环境这一步就是"平台 API 拉数"，边界最清晰 |
| dwd/dws 加工 | ✅ **PipelineAgent** | CPU 密集批处理；订单/退款/会话三路独立可并行；全量重建天然幂等，重试安全 |
| 质量校验 | ✅ **QualityAgent** | 天然是"门禁"角色——agent 化后才有资格**阻塞或放行**下游指标，这是纯函数时代没有的语义 |
| 指标字典+物化 | ✅ **MetricsAgent** | 可按 (metric, tenant) 并行；口径版本解析已是独立子域 |
| 知识库全链 | ✅ **KBAgent** | 独立存储域（向量库），已有双后端抽象；入库/检索/重建三种任务类型 |
| 新增：业务问答 | ✅ **InsightAgent（LLM）** | 组合 metrics API + KB 检索回答"退款率为什么涨"类问题，是多 agent 系统里唯一需要 LLM 的角色，也是把中台能力串成智能体的自然出口 |
| security/auth、tenant_guard | ❌ **不拆** | 项目自己的设计原则就是"全项目唯一的租户入口"（`api/deps.py` 模块 docstring）。拆成 agent 等于允许每个 agent 自行解释租户，隔离底线被破坏。保留为**横切组件**，由 Orchestrator 解析后以 `TenantScope` 值对象强制下发 |
| api/ 路由层 | ❌ 不拆 | 定位为 agent 的触发器与观察窗，不是 agent |
| db/models/config | ❌ 不拆 | 基础设施，被所有 agent 共享 |

---

## 第二部分 · Agent 拆分与职责边界

### 2.1 六个 Agent 的规格

统一约定：所有 Agent 的输入/输出是**类型化 JSON**（pydantic 模型校验）；数据本身不进消息，消息只带**引用**（batch_id、日期窗口、租户、表行数统计）——数据总线就是现有数据库。

#### A1 `ingest` — 数据接入 Agent
- **输入**：`{window: [start,end], tenants: [...], seed?: int, source: "mock"|"platform_api"(预留)}`
- **输出**：`artifacts: {ingest_batch_id, raw_rows: {order, refund, cs_session}, window}`；stats 含脏数据注入统计
- **能力**：调用 `mock/generator.generate_all`（Phase 3 顺手切分为 4 个子模块）；写 raw_* / dim_* / app_user / kb_document
- **禁止**：触碰 dwd_* / dws_* / metric_* / kb_chunk / 向量库；禁止解释租户权限（scope 由编排器下发）
- **超时/重试**：600s / 2 次（generate_all 确定性，重试前须先清本 batch——幂等键 ingest_batch）

#### A2 `pipeline` — 数仓加工 Agent
- **输入**：`{ingest_batch_id, window}`（来自 A1 artifact）
- **输出**：`{dwd_stats, dws_stats, mdm: {match_rate, conflicts}, window}`
- **能力**：调用 `pipeline/dwd.build_dwd`、`pipeline/dws.build_dws`；内部三路（orders/refunds/cs）线程并行；写 dwd_* / dws_*
- **禁止**：修改 raw_*（只读贴源）；禁止自行触发质量校验（那是 A3 的域）；禁止读取/写入 metric_*
- **超时/重试**：900s / 2 次；全量重建幂等，重试安全

#### A3 `quality` — 数据质量门禁 Agent
- **输入**：`{dws_ready: true, run_id}`（依赖 A2）
- **输出**：`{dq_run_id, overall_pass_rate, failed_rules: [...], verdict: "pass"|"warn"|"block"}`
- **能力**：`pipeline/quality.ensure_rules + run_quality_checks`；按规则给下游签名（verdict 写入 artifact）
- **禁止**：修改任何业务表（只写 dq_rule/dq_result）；**无权直接失败整个 run**——只给出 verdict，是否阻塞由编排器按配置决定（职责分离）
- **超时/重试**：300s / 1 次（校验本身幂等且轻）

#### A4 `metrics` — 指标物化 Agent
- **输入**：`{window, tenants: [...], dq_verdict, dq_run_id}`（依赖 A2+A3）
- **输出**：`{materialized: {code: points}, caliber_versions: {...}}`
- **能力**：`metrics/registry.ensure_definitions`、`engine.materialize`；**按租户 fan-out 成子任务**（fan-in 汇总）；尊重 DQ 门禁：verdict=block 时按配置 skip 或以 degraded 状态物化（结果行打 `degraded=true` 标记——不静默）
- **禁止**：绕过 `registry.resolve_version` 自选口径；禁止在无 TenantScope 时执行带租户谓词的查询（admin 聚合仅限显式声明的平台级任务）
- **超时/重试**：900s / 2 次

#### A5 `kb` — 知识库 Agent
- **输入**（三种任务类型）：`rebuild {tenant_id?}` / `sync_docs {batch_id}` / `query {query, tenant_id, kb_type?, top_k, mode}`
- **输出**：rebuild→`{chunks, written, per_kb_type}`；query→检索结果（现有 `search()` 返回结构）
- **能力**：`kb/ingest.ingest_knowledge`、`kb/manage.upsert_document/delete_document`、`kb/retriever.search`；写 kb_chunk / 向量库
- **禁止**：**任何无租户调用**（沿用 `retriever.search` 的硬拒绝，`kb/retriever.py:108-112`）；禁止写关系业务表
- **降级**：向量库不可用时，query 返回 `degraded=true + warning`（显式降级，呼应 README 里"倒排索引静默降级"的教训）；ingest 失败进重试，检索继续用旧索引
- **超时/重试**：600s / 3 次（embedding 后端是外部服务，网络类失败可重试）

#### A6 `insight` — 业务问答 Agent（LLM，可关）
- **输入**：`{question, tenant_id(必填), window?}`
- **输出**：`{answer, citations: [{metric_code, caliber_version} | {doc_id, chunk_ix}], tool_calls: [...], degraded: bool}`
- **能力**：工具调用循环（手写，≤8 轮），绑定三个工具：`metrics.query`（=A4 引擎的 `compute_with_compare`）、`kb.search`（=A5）、`dq.summary`；答案必须引用口径版本与知识来源
- **禁止**：生成任何数字——数值一律来自工具返回（LLM 只做组织与解释）；禁止跨租户聚合（工具层强制 TenantScope，LLM 无法注入租户参数——工具签名里 tenant_id 由 ctx 填，LLM 不可达）
- **超时/重试**：120s / 1 次；LLM 不可用 → 降级为"指标卡片 + 检索片段"裸返回（`degraded=true`）
- **模型**：OpenAI 兼容协议，复用 `kb/embedding.py` OpenAICompatEmbedder 的配置模式（`BDP_LLM_*`），HTTP 客户端用已有的 httpx

### 2.2 不做成 Agent 的横切能力

| 能力 | 处理方式 |
|---|---|
| 租户解析 | Orchestrator 唯一调用 `resolve_tenant`，产出 `TenantScope(tenant_id, is_violation)` 放入 AgentContext；Agent 内拿不到原始请求头 |
| 写域隔离（禁止事项的强制手段） | AgentSpec 声明 `write_domains`；AgentContext 提供的 session 挂 SQLAlchemy `before_flush` 监听，越域写直接抛异常——禁止事项从"约定"升级为"运行时强制" |
| 审计 | 沿用 `security/audit.py`，Agent 执行关键动作时以 system principal 落 audit_log |

---

## 第三部分 · 协作机制

### 3.1 编排方式选型

| 方案 | 结论 | 理由 |
|---|---|---|
| **A. 进程内 Orchestrator + DB 任务表（ThreadPoolExecutor）** | ✅ 采用 | 任务量级是"每 run 十几个"，DB 已是系统记录，任务表继承其事务一致性；零新增依赖；lite/full 双模式天然兼容 |
| B. Celery + Redis | 否 | 引入 broker 运维成本，单机演示无收益；等 worker 需要跨机扩展时再迁移（任务表 → 消息表的迁移路径清晰） |
| C. LangGraph / AutoGen | 否 | 本系统 5/6 个 agent 是确定性数据任务，绑上 LLM 框架抽象反而难测；A6 内部手写 ≤100 行工具循环足够 |

### 3.2 调度与编排

**DAG 声明**（`agents/dag.py`，纯 Python dict，不引第三方图库）：

```python
NIGHTLY_DAG = Dag("nightly", tasks=[
    T("ingest",   agent="ingest",   deps=[]),
    T("dwd_orders",  agent="pipeline", deps=["ingest"], params={"domain": "orders"}),
    T("dwd_refunds", agent="pipeline", deps=["ingest"], params={"domain": "refunds"}),
    T("dwd_cs",      agent="pipeline", deps=["ingest"], params={"domain": "cs"}),
    T("dws",         agent="pipeline", deps=["dwd_orders","dwd_refunds","dwd_cs"], params={"domain":"dws"}),
    T("quality",     agent="quality",  deps=["dws"]),
    T("kb_rebuild",  agent="kb",       deps=["dws"]),          # 与 quality 无依赖 → 并行
    T("metrics",     agent="metrics",  deps=["dws","quality"]), # 带 DQ 门禁
])
```

三个并行点：① dwd 三域并行；② quality ‖ kb_rebuild；③ metrics 内部按租户 fan-out（4 个子任务 fan-in）。受 `BDP_AGENT_WORKERS` 与写域锁约束实际并行度。

**运行循环**（`agents/orchestrator.py`）：
1. 创建 `agent_run`（run_id、dag_id、trigger）→ 按 DAG 拓扑插入 `agent_task`（status=pending，input 从上游 artifact 引用构造）；
2. Dispatcher：扫描 pending 且 deps 全 succeeded 的任务 → 检查写域锁空闲 → `UPDATE ... WHERE status='pending'` 抢占（防双派发）→ 提交线程池；
3. Worker：构造 AgentContext（含 TenantScope、cancel flag、emit 回调）→ `agent.run(ctx)` → 写 result/artifact/事件；
4. 完成/失败 → 触发下游评估；run 内所有任务终态 → 汇总 run（succeeded / partial_success / failed）。
5. 崩溃恢复：进程重启时把 heartbeat 超时（>2×timeout）的 running 任务标为 failed(retryable)，允许重试。

**任务分解**： DAG 声明级（上面）+ 运行时 fan-out（metrics 按租户；kb rebuild 按租户可选）。fan-out 子任务共享父任务 input，各自带 `TenantScope`。

### 3.3 消息传递与共享状态（记忆分层）

| 层 | 载体 | 生命周期 | 内容 |
|---|---|---|---|
| 任务记忆 | `agent_task.input / .result`（JSON） | 单任务 | 类型化参数与结果摘要 |
| 运行记忆（黑板） | `agent_artifact`（run_id, key, value JSON, producer_task） | 单 run | `ingest_batch_id`、`dq_run_id/verdict`、`materialize_window`——下游凭 key 引用，**不拷贝数据** |
| 长期记忆 | 业务表本身（dwd/dws/metric_result/向量库）+ 新增 `insight_log`（问答记录） | 永久 | 系统的真实状态 |
| 事件流 | `agent_event`（run_id, task_id, ts, level, message, data） | 保留 N 天 | 进度与告警，供 API/SSE 与看板 |

原则：**Agent 之间零直接调用**——只通过 DAG 依赖 + artifact 引用通信。每个 agent 可独立单测（构造 ctx 即可），这是把现有"函数直调"改为"任务协作"后最重要的可测性收益。

### 3.4 并发与超时控制

| 机制 | 设计 |
|---|---|
| Worker 池 | `ThreadPoolExecutor(max_workers=BDP_AGENT_WORKERS)`；**lite(SQLite) 默认 1**，full(PG) 默认 4——直视 P5 约束而不是假装能并发 |
| 写域锁 | 每个写域一把 `threading.Lock`（raw / dwd / dws / metric / dq / kb_store / kb_meta）；AgentSpec 声明，Dispatcher 抢占前检查，同域任务串行、异域并行；同时解决 P6（向量库并发写竞态） |
| 超时 | 每 AgentSpec `timeout_sec`；实现为**协作式取消**：ctx.cancel() 置位，Agent 在批次边界（BATCH_SIZE 循环、每租户、每文档）检查并抛 `TaskCancelled`。Python 线程无法强杀，这一点在文档中如实声明；线程池整体有 watchdog 兜底记录 |
| 心跳 | 每阶段边界 `emit()` 顺带更新 `agent_task.heartbeat_at`；用于崩溃恢复与看板 |
| 租户并行 | metrics/kb 的 fan-out 子任务按租户分片，共享同一写域锁时自然排队，PG 模式下 metric_result 写入无冲突 |

### 3.5 失败重试与降级

**错误分类**（新增 `agents/errors.py`，并让现有业务异常挂上标记）：

```python
class RetryableError(Exception): ...      # 网络/embedding 服务/向量库暂不可用
class FatalError(Exception): ...          # 口径缺失(MetricError)、参数错(KbManageError)、契约违例
# MetricError / KbManageError 增加 mixin 或由 orchestrator 按类型映射，不改继承树
```

| 策略 | 规则 |
|---|---|
| 重试 | `attempt < max_attempts` 且异常属 Retryable → 指数退避 `5s × 2^n`；全量重建类任务（A1 清 batch、A2、A4）幂等所以重试安全；A5 单文档操作靠 chunk_id 幂等 |
| 短路 | 上游 failed → 下游 skipped（DAG 短路）；run = partial_success |
| DQ 门禁 | `BDP_AGENT_DQ_GATE`（默认 0.99）：verdict=block 时 metrics 任务按 `BDP_AGENT_DQ_GATE_ACTION=skip|degraded` 执行；degraded 物化的结果行显式打标，API 层透出（不静默——沿用本项目"不丢行只打标"哲学） |
| KB 降级 | 向量库不可用：query 走旧索引 + `degraded=true + warning`（把 README 第六节"静默降级"教训制度化为显式状态位） |
| LLM 降级 | A6 无 key/超时/限流 → 返回指标卡片+检索片段，`degraded=true`；不影响其他 agent |
| 死信 | `max_attempts` 耗尽 → task=failed，`agent_event` 记 ERROR，run 继续/结束按 DAG 依赖 |

---

## 第四部分 · 工程实现

### 4.1 目录与文件变更

```
src/bdp/agents/                    # 全新模块
  __init__.py
  spec.py                          # AgentSpec / AgentContext / AgentResult / TenantScope
  errors.py                        # RetryableError / FatalError / TaskCancelled
  state.py                         # AgentRun/AgentTask/AgentArtifact/AgentEvent ORM + StateStore
  dag.py                           # DAG/T 定义 + nightly 声明 + 拓扑校验
  orchestrator.py                  # dispatcher / 写域锁 / 重试 / 超时 / 崩溃恢复
  base.py                          # BaseAgent 模板方法（flush 钩子装写域监听）
  ingest_agent.py  pipeline_agent.py  quality_agent.py
  metrics_agent.py kb_agent.py     insight_agent.py
  llm.py                           # OpenAI 兼容客户端（httpx，可选启用）
src/bdp/api/routers/agents.py      # 新路由
tests/test_agents.py               # 集成测试（沿用 conftest 的 sqlite+local 夹具）
docs/AGENT_REFACTOR_PLAN.md        # 本文档
```

修改（均为增量）：`models.py`（+5 张表）、`config.py`（+BDP_AGENT_* / BDP_LLM_*）、`cli.py`（+agent 子命令，旧命令变薄包装）、`api/main.py`（注册 router）、`api/static/dashboard.html`（+第 6 视图"Agent 运行"）、`kb/manage.py`、`mock/generator.py`（Phase 3 切分）、`requirements.txt`（**不变**）。

新增表（`models.py` 追加，全部纯新增不碰业务表）：

```python
class AgentRun(Base):        # run_id PK, dag_id, trigger, status, params JSON, started/finished_at, stats JSON
class AgentTask(Base):       # task_id PK, run_id FK, name, agent, deps JSON, params JSON, input JSON,
                             # status(pending/running/succeeded/failed/skipped/cancelled/degraded),
                             # attempt, max_attempts, timeout_sec, write_domains JSON, tenant_scope JSON,
                             # result JSON, error, heartbeat_at, started/finished_at
class AgentArtifact(Base):   # run_id, key, value JSON, producer_task  (unique: run_id+key)
class AgentEvent(Base):      # run_id, task_id, ts, level, message, data JSON
class InsightLog(Base):      # id, ts, tenant_id, question, answer, citations JSON, tool_trace JSON, degraded
```

`config.py` 追加：

```python
agent_workers: int = 1            # SQLite 必须 1；full 模式建议 4
agent_dq_gate: float = 0.99
agent_dq_gate_action: str = "degraded"   # skip | degraded
agent_heartbeat_sec: int = 30
agent_event_retention_days: int = 14
llm_backend: str = "none"         # none | openai
llm_api_base / llm_model / llm_api_key: str = ""
```

### 4.2 关键接口与数据结构

```python
# agents/spec.py
@dataclass(frozen=True)
class TenantScope:
    tenant_id: str | None          # None 仅限显式声明的平台级任务
    # 只能由 Orchestrator 调 resolve_tenant 产出；agent 内禁止二次解析

@dataclass
class AgentContext:
    run_id: str
    task_id: str
    scope: TenantScope
    params: dict
    window: tuple[date, date] | None
    session_factory: Callable[[], AbstractContextManager[Session]]  # 产出带写域监听的 session
    cancel: Callable[[], bool]                                     # 协作式取消
    emit: Callable[[str, str, dict], None]                         # (level, message, data) → 事件+心跳

@dataclass
class AgentResult:
    status: Literal["succeeded", "degraded", "skipped", "failed"]
    artifacts: dict = field(default_factory=dict)   # 写入黑板，供下游引用
    stats: dict = field(default_factory=dict)
    warning: str | None = None
    retryable: bool = False

@dataclass(frozen=True)
class AgentSpec:
    name: str
    write_domains: tuple[str, ...]     # 运行时强制
    timeout_sec: int
    max_attempts: int
    input_model: type[BaseModel]       # pydantic 校验任务输入

class BaseAgent(Protocol):
    spec: AgentSpec
    def run(self, ctx: AgentContext) -> AgentResult: ...
```

Agent 实现示例（展示"现有业务函数零改写，agent 只是壳"）：

```python
# agents/quality_agent.py
class QualityAgent(BaseAgent):
    spec = AgentSpec(name="quality", write_domains=("dq",), timeout_sec=300,
                     max_attempts=1, input_model=QualityInput)
    def run(self, ctx):
        with ctx.session_factory() as s:
            ensure_rules(s)
            results = run_quality_checks(s)          # ← 现有函数原样复用
            summary = quality_summary(s)
        verdict = "block" if any(r["status"] == "error" for r in results) else "pass"
        return AgentResult("succeeded",
                           artifacts={"dq_run_id": summary["run_id"],
                                      "dq_verdict": verdict,
                                      "dq_pass_rate": summary["overall_pass_rate"]},
                           stats={"rules": len(results)})
```

CLI 与 API 接入：

```bash
python -m bdp.cli agent run --dag nightly            # 全链路（等价替代 all）
python -m bdp.cli agent run --agent kb.rebuild --tenant-id T001   # 单 agent 按需
python -m bdp.cli agent status [run_id]              # 运行/任务状态
python -m bdp.cli agent retry <task_id>
```

```
POST /v1/agent/runs                  {dag, params}   → run_id   (admin/ops)
GET  /v1/agent/runs[/{id}]           状态 + 任务明细
POST /v1/agent/tasks/{id}/retry                                    (admin/ops)
GET  /v1/agent/runs/{id}/events      轮询/SSE
POST /v1/agent/ask                   {question}      → InsightAgent，走 tenant_guard，
                                       行为约束与 /v1/kb/search 完全一致（无租户 400）
```

### 4.3 与现有代码的接入点与兼容过渡

| 现状 | 过渡方案 |
|---|---|
| `cli.py cmd_pipeline/metrics/kb/all` | 保留命令名与输出格式，内部改为**同步直调**对应 Agent（不走队列）；`cmd_all` 保留为 legacy 别名 = `agent run --dag nightly --sync`。两条路径共用同一 Agent 代码，集成测试断言"旧命令输出 == agent run 输出"防行为漂移 |
| `tests/conftest.py seeded` 夹具 | **Phase 0-1 不动**（业务函数直调，保持单测隔离）；Phase 2 起新增 `test_agents.py` 用同一夹具跑 mini-DAG |
| 全局 `SessionLocal` | Phase 0 新增 `db.session_factory(bind=None)` 工厂（默认行为与现在完全一致）；Agent 只允许用 `ctx.session_factory()`（含写域监听）；存量代码不动 |
| C4 事务边界 | Agent 内统一规则：`ctx.session_factory()` 上下文退出时提交/回滚（等价 session_scope）；`kb/manage.py` 的手动 commit 保留（单文档操作即时可见是其语义），但被 Agent 调用时通过上下文检测跳过——在 manage.py 加 `commit: bool = True` 参数，Agent 传 False |
| 租户入口唯一性 | Orchestrator 是 API 之外第二个 `resolve_tenant` 调用方；对系统任务统一以 system principal（role=admin）+ 显式 TenantScope 下发，审计照记。**Agent 内部代码拿不到请求头**，越权面反而收敛 |
| metrics→security 的 import | 不改（保持唯一租户谓词来源），仅要求 A4 复用同一函数 |

### 4.4 顺手修复（Agent 化的前置/伴随项，范围克制）

1. **P1 向量库每请求重建**：`kb/store.py` 加进程级缓存 `@lru_cache build_store_singleton()`（embedder 同理），`routers/kb.py` 检索路径改用单例——一行级修改，收益立现；写路径仍每次显式构造并由写域锁保护。
2. `insight_log`/`agent_*` 表的 JSON 列在 SQLite/PG 双端都走 SQLAlchemy JSON 类型（与现有"不做方言判断"原则一致）。

---

## 第五部分 · 落地节奏

> 前置动作（半天）：`git init` + 全量测试基线（当前 77 项应全绿），之后每阶段一个 commit、可独立回滚。

### Phase 0 · 地基（1-2 天）
- 做事：`db.session_factory` 工厂；config 增 agent 段；models 增 5 张 agent 表；`agents/spec.py + errors.py + state.py`；`kb/manage.py` 加 `commit` 参数。
- 产出：现有功能零变化 + `pytest` 全绿 + `cli stats` 行为一致。
- 验收：`make test` 77 项通过；新旧入口冒烟对比（`cli all` 前后 `stats` 输出 diff 为空）。
- 回滚：纯增量代码，revert commit 即可。

### Phase 1 · Agent 内核串行跑通（2-3 天）
- 做事：`base.py + dag.py + orchestrator.py`（workers=1）+ 5 个确定性 Agent（A1-A5，不含 LLM）；`cli agent run/status`。
- 产出：`cli agent run --dag nightly` 产出与 `cli all` 等价；agent_task/artifact/event 全程留痕。
- 验收：① 结果等价（stats 表行数、dq pass_rate、metric points 数与旧路径一致）；② 幂等：连续跑两次 nightly 结果一致；③ 故意让 kb 失败（指错 vector backend）→ metrics 不受影响、run=partial_success、下游 skipped 有记录。
- 回滚：`BDP_AGENT_MODE` 无需开关——旧命令未删除，直接用旧命令即回滚。

### Phase 2 · 并发与韧性（2-3 天）
- 做事：写域锁、dwd 三路并行、metrics 租户 fan-out；重试/退避/协作取消/心跳/崩溃恢复；DQ 门禁；P1 修复（store 单例）；`routers/agents.py` + dashboard 第 6 视图。
- 产出：full 模式 4 workers 跑通；API 可观察可重试。
- 验收：① SQLite workers=1 无死锁、PG workers=4 结果与串行一致；② 超时注入（给 quality 塞 sleep）→ 协作取消生效、任务标 failed(retryable)、重试成功；③ DQ_GATE=1.0 时 metrics 被 skip/degraded 且 API 透出标记；④ kill 进程重启 → heartbeat 超时任务被回收可重试；⑤ 检索 P95 不高于现状（store 单例生效）。
- 回滚：agent API 与视图可单独下线；写域锁/单例修复独立成 commit。

### Phase 3 · InsightAgent（LLM）（2-3 天）
- 做事：`llm.py`（httpx、OpenAI 兼容、streaming 可选）；A6 工具循环 + 三工具绑定 + 引用强制；`insight_log`；`/v1/agent/ask` + 前端问答视图；顺带把 `mock/generator.py` 切分为 4 个子模块。
- 产出：租户内自然语言问答，答案带口径版本与知识引用。
- 验收：① 无 LLM key → `degraded=true` 的指标卡片+检索片段返回，不 500；② 有 key → 问"上个月 NOVA 退款率环比"答案中的每个数字可溯源到 tool_calls；③ A6 无法构造跨租户工具调用（工具签名不含 tenant 参数，由 ctx 注入）——用测试断言。
- 回滚：`BDP_LLM_BACKEND=none` 一键关闭；A6 是叶子节点，不影响批处理链。

### Phase 4 · 可选演进（按需）
- 增量化：metric_result/dwd 按 ingest_batch 增量（解决 P2）；DQ004 改 JOIN 聚合（P4）；dwd refunds 内存 dict 改临时表（P3）；worker 进程分离（任务表协议已就绪）；真实平台 API 接入替换 mock（A1 的 `source="platform_api"` 预留）。

### 风险清单

| 风险 | 缓解 |
|---|---|
| SQLite 单写者使"并发"在 lite 模式名存实亡 | 如实默认 workers=1；写域锁保证正确性优先于并行度；文档明示 full 模式才解锁并行 |
| Python 线程超时无法强杀 | 协作式取消 + 批次边界检查 + 崩溃恢复兜底；不承诺硬超时 |
| 双入口（CLI 直调 vs 队列）行为漂移 | 共用 Agent 实现 + 等价性集成测试 |
| 全量重建稀释重试价值 | Phase 4 增量化前，重试主要保护 A5/A6 与外部服务调用；A2/A4 重试成本可控（当前全链路秒级） |
| LLM 引入外部依赖与不确定性 | 默认 off；数值强制来自工具；引用可溯源；degraded 语义完备 |
| agent 表膨胀 | agent_event 按 retention 清理；agent_task 随 run 归档 |

---

## 附 · 新增依赖说明

**无强制新增依赖。** Orchestrator/线程池/锁用标准库；LLM 客户端用已在 requirements 的 httpx；pydantic 校验用已有 pydantic v2。若未来 worker 需跨机：任务表协议（抢占式 UPDATE + heartbeat）可直接换 Celery broker，Agent 代码不变——这是选 DB 队列而非 Celery 的核心理由：迁移点被隔离在 StateStore 一层。
