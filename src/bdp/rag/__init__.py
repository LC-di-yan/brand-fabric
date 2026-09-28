"""RAG 子包：Agentic 检索管线（docs/AGENTIC_RAG_PLAN.md）。

模块分工
--------
- lexicon / query_planner : 查询分类、拆分、改写、指代消解
- judge                   : 检索充分性判定（决定是否改写重试）
- multi_hop               : 有界多跳扩展（文档内边 + 实体种子边）
- context_builder         : 去重、压缩、token 预算裁剪
- grounding               : 引用存在性与支撑度核验
- session                 : 会话记忆（指代消解的上下文来源）
- pipeline                : 编排入口（唯一的新增循环）

设计约束：检索原语仍是 kb/retriever.search()，租户硬约束在其内部零豁免；
无 LLM 时全链路走规则退化，可离线运行与测试。
"""
