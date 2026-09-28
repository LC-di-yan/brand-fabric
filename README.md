# BrandFabric · 多品牌电商数据中台

[![CI](https://github.com/LC-di-yan/brand-fabric/actions/workflows/ci.yml/badge.svg)](https://github.com/LC-di-yan/brand-fabric/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Tests](https://img.shields.io/badge/tests-128%20passing-brightgreen)

一个面向**多品牌电商代运营（TP）**场景的多租户数据中台：把多平台订单、退款、客服会话
统一到一套分层数仓与指标体系，以 API 与看板对外服务，并保证品牌之间**数据严格隔离**。

全部数据由程序生成（模拟平台 API 脏数据），开箱即可在任意机器 5 分钟内跑通完整链路，
无需 Docker、无需 GPU、无需外网。

---

## 一、解决什么问题

多品牌代运营公司同时服务多个品牌，且这些品牌互为竞争对手，带来三个真实且苛刻的约束：

| 约束 | 为什么是硬约束 | 本项目的解法 |
|---|---|---|
| **多品牌数据隔离** | 合同要求数据不得外泄，隔离失效属于不可逆事故 | 网关注入 `tenant_id` + 数据库强制谓词 + 审计留痕，三道防线收敛在一个入口 |
| **跨平台口径统一** | 天猫/京东/抖音/拼多多成交口径不同，报表对不上映射数据能力失信任 | 指标字典 + 口径版本管理，报表强制回传口径版本号，历史数据用历史口径复算 |
| **跨平台商品主数据** | 同一 SKU 在每个平台编码不同，且上游编码不规范 | 归一化 → 规则匹配 → 编辑距离模糊匹配三级还原，配人工复核闭环 |

## 二、核心功能

- **五层数仓**：raw（贴源）→ dwd（清洗 + 主数据映射 + 质量标记）→ dws（轻度汇总）
  → ads（指标物化）→ 治理域（质量规则 / 审计）。脏数据不丢行，只打标记，可追溯可统计。
- **指标字典与口径版本**：15 个指标全部字典化声明（口径/来源表/事件日/公式/Owner）；
  支持"支付 GMV 含预售 v1.0 / 剔除预售 v1.1"多版本并存，同比环比与维度下钻一致复用。
- **多租户隔离**：`api/deps.py` 是全项目唯一租户入口；指标 SQL 谓词由引擎强制注入，
  不接受外部过滤条件；越权 403 + 审计留痕；`ops` 角色必须显式指定租户。
- **知识库与混合检索**：dense + sparse 双路召回 → RRF 融合 → 重排；
  Milvus（Partition Key 租户隔离）/ 本地 numpy 双后端；内置 200 条评测集与消融实验。
- **Agentic RAG 检索管线**（`strategy=agentic`）：查询规划（分类/拆分/指代消解）→ 检索充分性
  判定 → 预算内改写重试 → 有界多跳（文档内相邻切片 + 实体种子跨域）→ 抽取式上下文压缩
  → 引用核验（编造引用拦截 + 支撑度置信分档）。零强制 LLM 依赖——无 LLM 时规则退化全链路
  可用；100 条复合/含糊靶场实测 Recall@K 0.90 vs 直通 0.88，P95 13ms。
- **多 Agent 批处理编排**：nightly 全链路 = 8 个任务（接入 → dwd 三路并行 → 汇总
  → 质量门禁 ‖ 知识库重建 → 指标按租户 fan-out）；DB 任务表作消息总线，带写域守卫、
  指数退避重试、协作式超时、心跳崩溃恢复。
- **数据质量门禁**：声明式规则（SQL 谓词）产出通过率，低于阈值时指标物化被阻断或带降级标记。
- **数据血缘**：表级（raw→dwd→dws→ads→服务）与指标级（来源表→基础指标→派生指标）
  两张血缘图，sankey 可视化，点击节点看上下游；声明与 models/DAG/指标字典的一致性由测试强制。
- **企业级 API 与可观测**：JWT + 会话双凭证、CSRF 双提交、登录限流与验证码、改密即旧凭证失效；
  统一错误格式 + 请求 ID；Prometheus 指标 + liveness/readiness 分离探针；
  full 模式带安全启动闸门（占位密钥拒绝启动）。
- **工程化配套**：Alembic 迁移、GitHub Actions CI（ruff + pytest 双版本 + 迁移冒烟）、
  Grafana 监控看板一键拉起。

## 三、技术架构

```
数据源                 数据中台                                    应用
┌──────────────┐   ┌──────────────────────────────────────┐   ┌──────────────┐
│ 平台侧订单    │   │ ① 接入层  模拟平台 API / CDC 落 raw  │   │ 经营看板      │
│ 退款记录      │──▶│ ② 数仓层  raw→ods→dwd→dws→ads       │──▶│ 指标服务 API  │
│ 客服会话      │   │ ③ 主数据  商品跨平台映射 + 覆盖率     │   │ 知识检索服务  │
│ 知识文档      │   │ ④ 指标层  指标字典 + 口径版本        │   │ 品牌自助数据  │
└──────────────┘   │ ⑤ 知识层  切片→向量化→Milvus→混合检索│   └──────────────┘
                   │ ⑥ 治理层  数据质量校验 + 数据血缘     │
                   └──────────────────────────────────────┘
        ┌────────────────────────────────────────────────────────┐
        │ 贯穿全链路：tenant_id 强制注入 · 数据库强制谓词 · 审计日志 │
        └────────────────────────────────────────────────────────┘
```

**技术栈**：Python 3.11+ · FastAPI · SQLAlchemy 2.0 · PostgreSQL / SQLite 双后端 ·
Milvus / 本地向量索引双后端 · ECharts · Alembic · Prometheus · Grafana

**五层数仓**：

| 层 | 表 | 说明 |
|---|---|---|
| raw | `raw_order` / `raw_refund` / `raw_cs_session` | 贴源，字段未清洗，含注入的脏数据 |
| dwd | `dwd_order` / `dwd_refund` / `dwd_cs_session` | 清洗 + 主数据映射 + 质量标记（不丢行，只打标记） |
| dws | `dws_shop_day` / `dws_tenant_day` | 按事件日轻度汇总 |
| ads / 指标 | `metric_def` / `metric_result` | 指标字典与物化结果 |
| 治理 | `dq_rule` / `dq_result` / `audit_log` | 质量校验、数据血缘与审计 |

## 四、快速开始

### 方式一：lite 模式（推荐，无需 Docker）

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt

export PYTHONPATH=src
python -m bdp.cli all --drop      # 一键：建表 → 90 天模拟数据 → 数仓 → 指标 → 知识库
python -m bdp.cli api             # 启动服务
```

打开 <http://127.0.0.1:8000/> 进入登录页（演示账号 `admin / admin123`），
<http://127.0.0.1:8000/docs> 查看接口文档，`/metrics` 查看 Prometheus 指标。

### 方式二：full 模式（PostgreSQL + Milvus + 监控栈）

```bash
docker compose up -d              # 一并拉起 Prometheus(9090) + Grafana(3000)
cp .env.example .env              # BDP_MODE=full、BDP_VECTOR_BACKEND=milvus
export PYTHONPATH=src
python -m alembic upgrade head    # 迁移建表
python -m bdp.cli all
```

> full 模式安全闸门：`BDP_JWT_SECRET` 为占位值时拒绝启动，
> 请通过环境变量注入真实密钥：`python -c "import secrets;print(secrets.token_urlsafe(48))"`。

### 运行测试与检查

```bash
pytest -q                         # 128 项测试（含租户越权矩阵与故障注入）
ruff check src tests migrations   # lint
python -m bdp.cli eval --top-k 5  # 检索消融评测
```

## 五、使用示例

**指标查询（口径版本 + 同比 + 维度下钻）**

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"nova","password":"nova123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s -X POST http://127.0.0.1:8000/v1/metrics/query \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"metric_code":"GMV_PAID","start":"2026-09-01","end":"2026-09-28","compare":"mom"}'
# 返回点序列 + 上期值 + 差值 + 百分比，并回传口径版本号
```

**知识混合检索（租户强制）**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/kb/search \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"退货政策是怎样的","mode":"hybrid","top_k":3}'
```

**数据血缘（表级 / 指标级 / 全链路）**

```bash
curl -s http://127.0.0.1:8000/v1/admin/lineage -H "Authorization: Bearer $ADMIN_TOKEN"
```

**Agent 编排（CLI 全链路 / 业务问答）**

```bash
python -m bdp.cli agent run --dag nightly --drop   # 多 agent 全链路（12 任务）
python -m bdp.cli agent status                     # 观察运行与任务状态
python -m bdp.cli agent ask "退款率怎么样" --tenant-id T001
```

## 六、实测结果（本机可复现）

数据规模（90 天模拟）：4 品牌 / 10 店铺 / 6 平台，**55,333** 订单行 / 7,522 退款单 / 32,664 客服会话，204 知识文档 / 424 切片。

| 项 | 结果 |
|---|---|
| 主数据映射命中率 | **100%**（精确 98.1% + 编辑距离兜底 1.9%），归一化冲突 0 |
| 数据质量捕获 | 注入的空值/负值/超额退款全部命中，整体通过率 99.94% |
| 混合检索 Recall@5 | **0.800**（纯稠密 0.760），P95 延迟 5.29ms（200 条自建评测集） |
| 租户隔离 | 品牌越权 403 + 审计留痕；`ops` 未指定租户 403；改密后旧 token 401 |
| 自动化测试 | **128 项全部通过**（CI 双 Python 版本强制） |

## 七、目录结构

```
brand-fabric/
├── docker-compose.yml            # PostgreSQL + Milvus Standalone + API + Prometheus + Grafana
├── alembic.ini / migrations/     # Schema 迁移（baseline + 增量，SQLite batch 模式）
├── deploy/                       # Prometheus 抓取配置 + Grafana 数据源/看板自动装配
├── .github/workflows/ci.yml      # CI：ruff + pytest（3.11/3.12）+ 迁移冒烟
├── src/bdp/
│   ├── config.py                 # 配置（BDP_ 前缀环境变量，含 CORS 白名单）
│   ├── db.py / models.py         # SQLAlchemy 会话（双后端）/ 26 张表
│   ├── bootstrap.py              # 启动自检 + full 模式安全启动闸门
│   ├── security/                 # 口令、JWT、限流、验证码、审计
│   ├── mock/                     # 模拟数据生成（含脏数据注入）
│   ├── pipeline/                 # dwd / dws / 主数据映射 / 质量规则 / 数据血缘
│   ├── metrics/                  # 指标字典（口径版本）+ 计算引擎
│   ├── kb/                       # 切片 / 向量化 / 双后端存储 / 混合检索 / 评测
│   ├── api/                      # FastAPI + 统一错误/请求 ID/指标中间件 + 前端静态资源
│   │   └── static/js/views/      # 8 个前端视图（原生 ES Modules，零构建链）
│   └── agents/                   # 多 agent 编排内核 + 6 个确定性/问答 agent
└── tests/                        # 128 项测试
```

## 八、工程取舍

刻意保留的简化，以及何时应当升级：

| # | 取舍 | 何时升级 |
|---|---|---|
| 1 | OLAP 用 PostgreSQL 而非 Doris | 数据过亿行或并发查询压垮业务库时 |
| 2 | 不做流式（Kafka/Flink），全 T+1 批处理 | 需要分钟级实时指标时引入 Flink CDC |
| 3 | 不引入湖仓（Paimon/Iceberg） | 需要多引擎读写、历史快照时 |
| 4 | 默认离线哈希 embedding，零依赖可运行 | 生产换 BGE-M3 + 交叉编码器重排 |
| 5 | 向量库双后端（Milvus + 本地索引） | 只保留 Milvus Cluster |
| 6 | 血缘采用"声明式编译 + 测试校验"而非埋点上报 | 链路跨越多个系统时引入 DataHub/OpenMetadata |

## 设计文档

- [docs/AGENT_REFACTOR_PLAN.md](docs/AGENT_REFACTOR_PLAN.md) — 多 Agent 协作系统重构设计（V3，已落地）
- [docs/AGENTIC_RAG_PLAN.md](docs/AGENTIC_RAG_PLAN.md) — Agentic RAG 检索管线演进方案（规划中）
- [docs/MULTI_AGENT_EVOLUTION_PLAN.md](docs/MULTI_AGENT_EVOLUTION_PLAN.md) — 多 Agent 协作系统演进方案（规划中）
- [docs/LOGIN_SECURITY.md](docs/LOGIN_SECURITY.md) — 登录安全设计

## 九、已知限制

诚实记录，避免高估：

1. **离线哈希 embedding 的语义能力有限**（同义改写弱于真实模型），这是"零依赖可运行"
   的代价；切换 `BDP_EMBEDDING_BACKEND=bge` 需重建向量库。
2. **合成语料偏短**，切片策略对比的差异不显著；切片收益要在长文档上才明显。
3. **重排用词面覆盖度而非交叉编码器**，生产应换 `bge-reranker`。
4. **模拟数据无法覆盖真实平台 API 的全部边界**（限流、字段缺失、回传延迟），
   真实接入会占项目大部分工作量。
5. **金额使用 Float** 以规避 SQLite 的 Decimal 聚合差异，生产应改 `Numeric(18, 2)`。

## 十、声明

本项目为个人学习与开源演示用途。全部品牌、商品、订单、会话与知识内容均为**程序生成的虚构数据**，
不含任何真实企业数据或个人信息；演示界面中的品牌视觉元素亦为虚构演示用途。

## License

[MIT](LICENSE)
