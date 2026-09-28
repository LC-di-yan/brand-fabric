# Agentic RAG 演进方案（brand-fabric）

> 基于 2026-09-28 对检索与问答链路（`kb/retriever.py`、`kb/store.py`、`kb/evaluate.py`、
> `agents/insight_agent.py`、`agents/llm.py`）的逐模块阅读。
> 所有结论标注真实代码位置；所有新组件为纯 Python + SQLAlchemy 实现，**零强制新增依赖**——
> LLM 未配置时全链路走确定性退化路径，lite/full 双模式均可运行与测试。

---

## 第一部分 · 现状与差距

### 1.1 当前检索链路（能做什么）

```
POST /v1/kb/search ──▶ kb/retriever.search()
  ├ dense: embedding(query) → store.dense_search(tenant, limit)
  ├ sparse: SparseEncoder(query) → store.sparse_search(tenant, limit)
  ├ 融合: RRF（先按分数汇总再全局排名，kb/store.py DEFAULT_RRF_K=60）
  └ 重排: 词面覆盖度打分（非交叉编码器）
返回: {candidates, results[{score, rrf_score, lexical_score, kb_type, doc_id, chunk_ix, text}]}
```

### 1.2 当前问答链路（InsightAgent）

`agents/insight_agent.py` 已具备一个**微型的工具循环**：

- `MAX_TOOL_ROUNDS = 8`，TOOLS_SCHEMA 含 `query_metric` / `search_kb` / `dq_summary`；
- 三条硬约束（写在系统提示与实现两处）：数字只来自工具、租户由 ctx 注入工具层、
  引用随调用登记（指标带口径版本、知识带 doc_id）；
- 无 LLM / 超时 / 限流 → `_degraded_answer`（指标卡片 + 检索片段，degraded=true 显式标记）。

### 1.3 差距（按影响排序）

| # | 环节 | 现状 | 差距导致的真实症状 |
|---|---|---|---|
| 1 | 查询理解 | 无。原文直接检索 | 复合问题（"退款率怎么样+退货政策"）一次检索两头落空；含糊/口语化查询命中率低 |
| 2 | 检索策略 | 单跳、固定 top-k | 第一跳不命中不会改写重试；跨知识域的关联问题（policy↔sop）召回不到 |
| 3 | 相关性判定 | 无。RRF 分数≠"足以回答" | 检索了 3 条不相关切片照样往下生成，答案质量靠运气 |
| 4 | 上下文加工 | 命中切片原文直接进 prompt | 冗余重复、超长，浪费 token 且稀释要点 |
| 5 | 引用核验 | 引用有登记（insight_log）但无自动核验 | LLM 可能引用一个不在候选里的 doc_id，无人发现 |
| 6 | 多轮对话 | 无会话记忆 | "那上个月呢？"无法解析指代 |
| 7 | 评测 | 仅检索级（Recall@5 0.800 / MRR@5 / P95，200 条） | 无答案级指标（引用精确率、忠实度）；现有集合全是单跳问题，测不出多跳收益 |

---

## 第二部分 · 目标与非目标

### 2.1 目标

1. **可回答性**：复合、含糊、多跳问题可解——新增 100 条复合/多跳评测集，分档给出门槛（见 §8）。
2. **可验证**：每条答案的引用自动核验（存在性 + 支撑度）；核验不过 → `degraded=true` 显式降级，
   延续"静默降级必须消灭"的项目原则。
3. **可解释**：管线的每一步（分类/改写/判定/跳数/压缩）落 trace，可经 API 回放。
4. **零依赖可运行**：无 LLM 时规则改写 + 词面判定全链路可测；有 LLM 时同一接口平滑增强。

### 2.2 非目标（诚实边界）

- 不训练/微调模型；不引入知识图谱数据库（多跳靠文档关系 + 实体种子，不是 KG）；
- 不承诺任意领域问答质量——语料仍是程序生成的模拟知识库；
- 不做流式输出（SSE 可后补，不依赖本方案）；
- 不新建第二个 LLM 循环——Agentic RAG 管线是 InsightAgent 的**检索工具升级**，不是并列的问答系统。

---

## 第三部分 · 总体架构

```
用户问题（+ 会话历史）
   │
┌──▼─────────────────┐
│ QueryPlanner        │ 分类(指标/知识/混合) → 意图拆分 → 改写/指代消解
└──┬──────────────────┘   （LLM 可选，规则兜底）
   │ 子查询队列 q1..qn
┌──▼──────────────────────────────┐
│ AdaptiveRetriever（有界循环）    │
│   hits = retriever.search(qi)   │
│   s = Judge.score(qi, hits)     │
│   ├ s ≥ 阈值 → 出循环           │
│   └ s < 阈值 → 改写 → 再检索    │  预算: max_rewrites
│   MultiHop.expand(hits)         │  预算: max_hops
│    ├ 同文档相邻切片补上下文      │
│    └ 实体种子 → 跨知识域二跳     │
└──┬──────────────────────────────┘   ★ 每一跳强制 tenant 过滤
┌──▼─────────────────┐
│ ContextBuilder      │ 去重 → bge 重排(可选后端) → 句级压缩 → token 预算裁剪
└──┬─────────────────┘
┌──▼─────────────────┐
│ AnswerStage         │ LLM 生成（引用强制）/ 无 LLM: 指标卡+片段降级
└──┬─────────────────┘
┌──▼─────────────────┐
│ GroundingCheck      │ 引用存在性 + 支撑度 → confidence
└──┬─────────────────┘   不过阈值 → degraded=true + 前端提示
   ▼
RagResult { answer, citations[], trace[], confidence, degraded }
```

---

## 第四部分 · 模块设计（新增 `src/bdp/rag/`）

| 文件 | 职责 | 关键签名（草案） |
|---|---|---|
| `query_planner.py` | 分类、拆分、改写、指代消解 | `classify(q) -> Intent`（metric/knowledge/hybrid）；`decompose(q) -> list[SubQuery]`；`rewrite(q, misses, history) -> str` |
| `judge.py` | 检索充分性判定 | `score(query, hits) -> JudgeResult(score, reasons[])`；启发式=词面覆盖+域匹配；LLM=pointwise 短输出 |
| `multi_hop.py` | 多跳扩展 | `expand(hits, tenant_id, budget) -> list[SubQuery]`：同 doc_id 相邻 chunk_ix → 实体种子跨 kb_type |
| `context_builder.py` | 去重/重排/压缩 | `build(query, hits, token_budget) -> list[ContextBlock]`；句切分→相关度排序→预算裁剪 |
| `grounding.py` | 引用核验 | `check(answer, citations, hits) -> GroundingReport`；引用存在性 + 支撑度（词面/向量相似） |
| `pipeline.py` | 编排入口（唯一新增循环） | `answer(session, query, tenant_id, *, history, strategy) -> RagResult`；预算控制与 trace 组装 |
| `session.py` | 会话记忆 | `SessionStore`：最近 N 轮 query/answer/citations（落 `insight_log` 扩展 `thread_id`） |

**复用边界（不重写）**：检索原语仍是 `kb/retriever.search()`；向量存取仍是 `kb/store.py`；
指标查询仍是 `metrics/engine.py`；LLM 客户端仍是 `agents/llm.py`。
`pipeline.answer` 替换 `insight_agent._tool_executor` 里 `search_kb` 的内部实现——
**InsightAgent 的系统提示、约束、降级路径全部不动**，它拿到的只是"变聪明的检索工具"。

---

## 第五部分 · 关键机制

### 5.1 查询分类与拆分（无 LLM 也能跑）

规则表驱动，词表放 `rag/lexicon.py`（数据文件，可运营）：

| 信号 | 判定 | 动作 |
|---|---|---|
| 命中指标词表（"退款率/GMV/客单价/满意度…"） | metric 意图 | 生成 metric 子查询（映射到指标代码） |
| 命中知识域词表（"政策/流程/怎么办/SOP"） | knowledge 意图 | 生成 knowledge 子查询（带 kb_type 提示） |
| 连接词（"和/以及/另外"）切分后两段各命中不同词表 | hybrid | 拆成两个子查询分别检索，结果合并去重 |
| 均不命中 | knowledge 兜底 | 原文直检索（等价现状，保证不劣化） |

有 LLM 时：LLM 分类 + 拆分（JSON 输出），规则版作为校验兜底；两者分歧时保守取 hybrid。

### 5.2 检索-判定-改写循环（预算制）

```python
budget = 1 + settings.rag_max_rewrites        # 默认 1+2
for attempt in range(budget):
    hits = retriever.search(session, query=qi, tenant_id=tenant, ...)
    verdict = judge.score(qi, hits)
    trace.append({"q": qi, "score": verdict.score, "attempt": attempt})
    if verdict.score >= settings.rag_judge_threshold:
        break
    qi = planner.rewrite(qi, misses=verdict.reasons, history=history)
```

- 阈值默认 0.5（词面判定尺度），LLM 判定换 0.6；
- `rewrite` 的输入带 `misses`（判定器给出的失配原因：域不对/关键词缺失），改写有方向；
- trace 全部落 `insight_log.tool_trace`（现有字段，直接复用）。

### 5.3 多跳扩展（两条边，预算 max_hops=2）

1. **文档内边**：命中切片的 doc_id 相邻 chunk_ix（±1）直接并入候选——补上下文，成本一次 fetch；
2. **实体种子边**：从命中文本抽取实体词（规则：品牌词表/商品词表/政策编号正则；可选 LLM NER），
   以实体为种子发起第二跳检索，**允许跨 kb_type**（如 policy 命中提到"SOP-101"→ 去 sop 域找原文）。

**红线**：每一跳都必须携带同一个 `tenant_id` 走 `retriever.search`（现有硬约束零豁免）。
专项测试：往 T002 注入仅含该实体的文档，T001 的多跳结果断言永不含 T002 文档。

### 5.4 引用核验（GroundingCheck）

- **存在性**：answer 引用的每个 (doc_id, chunk_ix) 必须在最终候选集内——LLM 编造引用号直接拦截，
  拦截后重试一次生成，再失败 → degraded；
- **支撑度**：对每条引用计算 answer 相关句与被引切片的相似度（无 LLM：词面 F1；有 LLM：向量相似），
  低于阈值 → confidence 降档 → `degraded=true`，前端提示"部分内容未被语料直接支撑"。

### 5.5 安全：检索内容是不可信输入

- 检索文本只以 data 形式进入 user 消息（定界包裹），**绝不拼接进 system prompt**；
- 命中内容含"忽略以上指令/泄露系统提示"类模式时打标隔离（过滤词表 + 字符脱敏）；
- InsightAgent 工具白名单不变；`pipeline` 不新增任何可写工具。

### 5.6 会话记忆

- `thread_id`（会话 ID）扩展进 `insight_log`；`session.py` 保存最近 `rag_session_turns`（默认 5）轮；
- 指代消解在 planner.rewrite 中完成："那上个月呢" + 上一轮 query"退款率" → "上个月退款率"；
- 无 LLM 退化：只做时间词/指标词的槽位继承（规则模板），解不了就原样检索并提示。

---

## 第六部分 · 配置项（`config.py` 追加，全部有默认值）

```python
rag_strategy: str = "single"        # single | agentic（agentic 为新管线；single 保持现行为）
rag_max_rewrites: int = 2           # 改写预算
rag_max_hops: int = 2               # 多跳预算
rag_judge_threshold: float = 0.5    # 判定阈值
rag_judge_backend: str = "heuristic"  # heuristic | llm
rag_rerank_backend: str = "lexical"   # lexical | bge（bge 需装 sentence-transformers）
rag_compress_token_budget: int = 3000
rag_session_turns: int = 5
```

`.env.example` 同步补充注释说明各档位语义。

---

## 第七部分 · API 变化（只增不改）

| 端点 | 变化 |
|---|---|
| `POST /v1/agent/ask` | 请求体增加 `strategy`（缺省读配置）；响应增加 `trace[]`、`confidence`；`citations` 结构不变 |
| `GET /v1/rag/trace/{insight_id}` | 新增：按 run 回放管线条步（权限同 `/v1/agent/ask`：本人或 admin/ops） |
| `POST /v1/kb/search` | 不变（原始检索接口保留，`pipeline` 内部也用它） |

前端 `ask.js` 视图：答案卡片增加 confidence 徽章与"查看推理轨迹"折叠区（读 trace）。

---

## 第八部分 · 评测方案（先建靶场再开枪）

### 8.1 数据集扩展（`kb/evaluate.py` 相邻新增 `rag_eval_set`）

- 现有 200 条单跳集：作为**回归基线**，agentic 模式不得劣化（Recall@5 ≥ 0.80）；
- **新增 100 条复合/多跳/含糊查询**，每条标注：期望文档集 / 期望指标代码 / 期望子查询数。
  这一步必须先行——现有集合全是单跳，无法体现管线收益，也没法验收。

### 8.2 指标定义

| 层 | 指标 | 定义 |
|---|---|---|
| 检索 | Recall@5 / MRR@5 | 子查询合并候选集上计算（与现口径对齐） |
| 答案 | 引用精确率 | 被支撑引用数 / 总引用数（grounding.check） |
| 答案 | 可回答率 | 非降级答案 / 总请求 |
| 工程 | P95 延迟 | agentic ≤ 3× single（预算内循环次数上限决定） |

### 8.3 消融矩阵与验收门槛（诚实值）

| 配置 | 单跳集 Recall@5 | 复合集 Recall@5 | 引用精确率 |
|---|---|---|---|
| single（现状） | 0.800（基线） | 预计 <0.5（测出来才有说服力） | — |
| agentic 无 LLM | ≥0.80 | **≥0.65** | ≥0.90 |
| agentic 有 LLM | ≥0.82 | **≥0.85** | ≥0.95 |

门槛定这么细的原因：无 LLM 时规则改写上限有限，写 0.85 是撒谎；分档给出才是可验收的承诺。

### 8.4 消融开关

`strategy` × `judge_backend` × `rag_max_hops=0/2` 组合跑 `evaluate.py`，结果表落
`out/rag_ablation.md` 并在 README"实测结果"节更新（沿用现有消融实验的呈现方式）。

---

## 第九部分 · 实施阶段

| 阶段 | 内容 | 涉及文件 | 新增测试 | 工作量 | 出口条件 |
|---|---|---|---|---|---|
| **P0 靶场** | 评测集扩展 100 条 + trace 通道 + `rag/` 骨架（pipeline 直通 single 等价） | `kb/evaluate.py`、`rag/pipeline.py`、`insight_log` 迁移（thread_id） | `test_rag_pipeline.py`（等价性 4 条） | ~3 天 | agentic=直通 时与现状逐字段等价 |
| **P1 规划改写** | query_planner（规则版全量 + LLM 可选）+ lexicon 数据文件 | `rag/query_planner.py`、`rag/lexicon.py` | 分类/拆分/指代 12 条 | ~3 天 | 复合集无 LLM Recall@5 ≥0.60 |
| **P2 判定循环** | judge（heuristic）+ 预算循环 + 引用存在性核验 | `rag/judge.py`、`rag/grounding.py`、`pipeline` 主循环 | 超预算/低分改写/编造引用拦截 8 条 | ~3 天 | 引用精确率 ≥0.90；P95 ≤3×single |
| **P3 多跳+加工** | multi_hop + context_builder + bge 重排可选后端 | `rag/multi_hop.py`、`rag/context_builder.py` | **跨租户泄漏红线 3 条**、hop 预算 4 条 | ~4 天 | 复合集无 LLM ≥0.65；泄漏测试全绿 |
| **P4 会话+展示** | session.py + thread_id + ask.js trace 折叠区 | `rag/session.py`、`insight_agent` 接线、`static/js/views/ask.js` | 多轮指代 6 条 | ~3 天 | 多轮集可回答率 ≥0.8（规则版） |

依赖：P0 → P1 → P2 → P3 → P4 严格串行（每阶段有独立验收，可随时停在任意阶段——停在 P2 也有完整收益）。

---

## 第十部分 · 风险与对策

| 风险 | 对策 |
|---|---|
| 规则改写天花板低 | 分档门槛诚实标注；LLM 后端即插即用，不重复造轮子 |
| LLM 判定引入延迟与成本 | pointwise 短输出 + 同 query 结果缓存（run 级）+ 预算硬顶 |
| 多跳引入噪声候选 | hop 预算 + 每跳过判定门槛才扩散 |
| **租户泄漏（红线）** | 每跳强制过滤 + 专项红线测试（P3 出口条件，不绿不发布） |
| 与 InsightAgent 职责重叠 | pipeline 只替换 search_kb 工具内部实现；系统提示/降级/引用结构零改动 |
| 评测集自编自导 | 标注规则写进文档；消融矩阵公开；基线（single 现状）先钉死 |

---

## 附：与《多 Agent 协作系统演进方案》的关系

本方案的 `pipeline` 是**检索工具层**升级（InsightAgent 消费）；
演进方案的 Planner/Verifier 是**编排层**升级。两者共享 `agents/llm.py`、trace 通道与
"零依赖可运行 + 确定性降级"的设计哲学，互不阻塞、可并行实施。
联动点：演进方案的 verify agent（只读复核）可消费本方案 GroundingReport 作为知识侧质量门禁。
