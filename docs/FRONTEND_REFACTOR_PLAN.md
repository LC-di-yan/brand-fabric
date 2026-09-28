# 前端企业级优化方案（brand-data-platform）

> 依据对 login.html（148 行）与 dashboard.html（约 1100 行）的逐行审计。
> 遵循原则：**沿用现有技术栈（原生 HTML/CSS/JS + ECharts），不引入框架与构建链**；
> 功能零回归；按「字体 → 色彩 → 交互状态 → 布局 → 组件 → 状态完备性」优先级执行。

---

## 一、现状诊断（问题清单）

### 工程结构
| # | 问题 | 位置 |
|---|---|---|
| A1 | 单文件 1100+ 行：7 个视图的 HTML/CSS/JS 全部内联，无法按视图维护 | dashboard.html |
| A2 | **ECharts 依赖 jsdelivr CDN**——离线环境图表全部空白，与项目"lite 模式零依赖可复现"的核心卖点冲突 | dashboard.html:7 |
| A3 | FastAPI 仅用 FileResponse 暴露两个 HTML，无法引用子目录资源（css/js 拆分的前提不存在） | api/main.py |
| A4 | 交互用原生 `alert()` / `confirm()`（导出失败、删除知识、重建索引、触发运行、查看切片） | 多处 |
| A5 | 无 favicon（404 噪音、无品牌感） | 全局 |

### 设计系统
| # | 问题 |
|---|---|
| D1 | 语义色错位：`.up`=红/`.down`=绿（股市配色），但"退款率 > 15%" 标 `.up` 实际想表达**风险**——类名、颜色、语义三者混乱 |
| D2 | 无设计令牌：颜色硬编码散落（图表色、pill 色、muted 色各写各的），无法做主题 |
| D3 | 卡片无层次（纯 border + 白底）；阴影缺失；圆角 6/8/10px 无规则混用 |
| D4 | 数字未全局 tabular-nums（仅 `.num` 类手工添加）；标题层级仅靠 400/500 字重，缺 600 |
| D5 | 无暗色模式（企业级 dashboard 标配） |

### 交互状态完备性
| # | 问题 |
|---|---|
| I1 | 加载态是文字"加载中..."，无骨架屏 |
| I2 | 空态是纯文字"无数据"，无图标与引导 |
| I3 | 错误态只有 innerHTML 红框，无重试入口，无全局 toast |
| I4 | 按钮无 active 按压反馈、无 focus-visible 焦点环（键盘不可用性缺陷） |
| I5 | 表格无 sticky 表头（长表滚动丢表头）、部分无分页信息 |
| I6 | **Agent 视图运行中无自动轮询**，用户必须手动刷新才能看到任务推进 |
| I7 | 智能问答无会话历史、无 pending 态（可重复点击）、无示例问题、无复制答案 |
| I8 | 查看知识切片用 `alert()` 截断展示；检索结果无关键词高亮、无得分可视化 |
| I9 | 指标字典 15 个指标混排无分组；KPI 卡无趋势信息（sparkline） |

### 可访问性
| # | 问题 |
|---|---|
| X1 | 无 skip-link、无 aria-live（动态内容对读屏不可见）、无统一焦点环 |

---

## 二、技术选型决策（含理由与代价）

**决策：不引入 React/Vue/Node 构建链，采用「原生 ES Modules + 设计令牌 + 本地化 ECharts」。**

理由：
1. 项目反复强调的卖点是 **"lite 模式任何机器 5 分钟零依赖跑通"**（README/INTERVIEW），引入 node_modules + 构建产物直接违背；
2. 前端定位是数据中台的展示壳，7 个视图规模下 ES Modules 完全可控，技能准则也要求"work with the existing stack"；
3. ECharts 本地化（vendor 进仓库，约 1MB）后**整站离线可运行**，反而强化卖点。

代价与缓解：
- 无类型检查/热更新 → 模块按视图拆分 + JSDoc 注释；改动用浏览器实测回归
- ECharts 进仓库增大体积 → 仅 1 个 vendor 文件，`<script>` 同步加载不受 ES module 影响
- 需要后端配合暴露静态目录 → main.py 增加 `app.mount("/static", ...)`（一行）

**字体决策**：不引入 webfont。中文 webfont 动辄数 MB（破坏加载与离线故事），企业级数据界面的正解是系统字体栈 + **全数字 tabular-nums + 字重层级（600/500/400）**。字体"性格"通过字号阶梯、字距与色彩层次实现。

---

## 三、目标结构

```
static/
├── login.html            # 重构：共享设计令牌
├── dashboard.html        # 瘦壳：布局骨架 + <script type="module">
├── favicon.svg           # 新增
├── vendor/
│   └── echarts.min.js    # 本地化（离线可运行）
├── css/
│   ├── tokens.css        # 设计令牌：色彩（明/暗）/字阶/间距/圆角/阴影/z-index
│   ├── base.css          # reset / 排版 / 滚动条 / 工具类 / 焦点环
│   ├── components.css    # 按钮/徽章/卡片/表格/表单/骨架屏/空态/错误/toast/modal
│   └── views.css         # 各视图专属布局
└── js/
    ├── api.js            # fetch 封装：错误规范化、401 跳登录、租户头
    ├── ui.js             # toast / confirmModal / skeleton / empty / 格式化 / 关键词高亮
    ├── charts.js         # ECharts 令牌联动主题 + makeChart（暗色切换自动重绘）
    ├── app.js            # 路由 / 导航 / 主题切换 / 视图注册
    └── views/
        ├── overview.js  ├── metrics.js  ├── catalog.js
        ├── knowledge.js ├── quality.js  ├── agents.js  └── ask.js
```

## 四、设计系统要点

| 项 | 决策 |
|---|---|
| 语义色 | `success / warning / danger / info / neutral` 五族；**废弃 .up/.down 股市配色**，趋势涨跌用 `positive`(绿)/`negative`(红)，风险一律 `danger` |
| 主色 | 保留品牌蓝但降饱和精调（#1d5fa8 → 深空蓝系），暗色主题下提亮 |
| 暗色模式 | tokens 双主题；默认跟随系统，手动切换持久化 localStorage；ECharts 主题联动 |
| 阴影 | 三级 tinted shadow（蓝灰调，不用纯黑）；卡片默认 sm，悬浮/弹层 md/lg |
| 圆角 | 4（控件内元素）/ 8（控件）/ 12（卡片）/ 16（弹层） |
| 数字 | 全局 `font-variant-numeric: tabular-nums`；金额/指标列右对齐 |
| 动效 | 200ms ease 过渡；按钮 active `scale(.98)`；骨架屏 shimmer |
| z-index | 阶梯制（10/100/200/300），杜绝 9999 |

## 五、逐视图升级清单（功能零回归前提）

| 视图 | 升级点 |
|---|---|
| 全局 | toast 取代 alert；confirm modal 取代 confirm；骨架屏；统一空态；sticky 表头；暗色切换 |
| 登录页 | 共享令牌重绘（层次感、品牌渐变保留）、favicon、focus 态 |
| 经营概览 | KPI 卡加 30 天 sparkline；质量表通过率加进度条；品牌对比高亮当前租户行 |
| 指标查询 | 指标字典按域分组（交易/客服）；结果表 sticky；导出成功 toast |
| 商品主数据 | 覆盖率改成对齐的进度条列表；复核确认后 toast 反馈 |
| 知识检索 | **切片查看改 modal**；检索命中关键词 `<mark>` 高亮 + 得分条；重建索引走确认 modal + toast |
| 质量与审计 | 规则行加严重度徽章；审计结果列强化 |
| Agent 编排 | **运行中自动轮询（2s，仅运行期间）**；触发运行改参数化 modal（DAG/天数/种子）；任务表加耗时列；事件流按级别着色 |
| 智能问答 | 会话式历史（本次登录内）；示例问题 chips；pending 态禁用按钮 + 骨架；**复制答案**按钮 |

## 六、实施节奏与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| A 地基 | main.py 挂载 /static；vendor echarts；tokens/base/components/views 四层 CSS；api/ui/charts/app JS 基座；dashboard 瘦壳 + login 重绘 | 断网（无 CDN）打开：登录 → 概览图表正常；94 项后端测试不回归 |
| B 视图迁移 | 7 个视图迁入 `views/*.js` + 上表升级点逐项落地 | 手测清单逐项通过：登录、概览、指标查询/版本切换/CSV、主数据分页/复核、检索/CRUD/重建、质量/审计、Agent 触发/明细/重试、问答降级路径 |
| C 收尾 | 暗色模式全视图走查、a11y（aria-live/skip-link/focus-visible）、README/INTERVIEW 更新、提交 | 截图走查明暗两套；`pytest -q` 全绿 |

## 七、风险与回滚

| 风险 | 缓解 |
|---|---|
| 视图迁移引入功能回归 | 迁移是"搬运 + 增强"，逐视图迁移后立即手测该视图全部动作；旧 dashboard.html 在 git 历史可随时回滚 |
| vendor 文件下载失败 | 保留 CDN `<script onerror>` 兜底 |
| ES Modules 兼容性 | 仅面向现代浏览器（项目本身即内部演示定位）；不使用 import maps 以外的特性 |
| 后端 mount 影响现有路由 | `/static` 前缀独立，不与 `/v1`、`/dashboard`、`/metrics` 冲突 |
