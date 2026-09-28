/** 数据血缘视图：表级 / 指标级 / 全链路三视角，ECharts sankey 渲染，点击节点看上游下游。 */
import { api, state } from "../api.js";
import { el, esc, setErr, skeleton, emptyState } from "../ui.js";
import { chartTheme, makeChart } from "../charts.js";

// 分层配色：与设计令牌的 series 色板对应，血缘分层固定映射保证语义稳定
const LAYER_COLOR = {
  raw: 0, dim: 1, dwd: 2, dws: 3, ads: 4, 治理: 3, 知识: 4, 外部: 2, other: 2,
};

let cache = {};
let graph = null;

export async function load() {
  const mode = el("liMode").value;
  const box = el("liDetail");
  skeleton(box, "rows", 2);
  try {
    cache = await fetchGraph(mode);
    graph = cache;
    render(mode);
  } catch (e) {
    setErr(box, e, load);
  }
}

async function fetchGraph(mode) {
  const path = { tables: "tables", metrics: "metrics", full: "" }[mode];
  return api(`/v1/admin/lineage${path ? "/" + path : ""}`);
}

function render(mode) {
  const t = chartTheme();
  const colorList = t.series;
  const inDegrees = new Map(), outDegrees = new Map();
  for (const e of graph.edges) {
    outDegrees.set(e.source, (outDegrees.get(e.source) || 0) + 1);
    inDegrees.set(e.target, (inDegrees.get(e.target) || 0) + 1);
  }
  const nodes = graph.nodes.map((n) => ({
    name: n.id,
    itemStyle: {
      color: colorList[LAYER_COLOR[n.layer] ?? 2],
      borderColor: t.surface,
    },
    label: n.kind === "metric" && n.label ? `${n.label}（${n.id}）` : n.id,
  }));
  const links = graph.edges.map((e) => ({
    source: e.source, target: e.target, value: 1,
    lineStyle: { color: "gradient", curveness: 0.5, opacity: 0.35 },
  }));
  const chart = makeChart("liChart");
  chart.setOption({
    tooltip: { trigger: "item", triggerOn: "mousemove" },
    series: [{
      type: "sankey",
      data: nodes,
      links,
      emphasis: { focus: "adjacency" },
      nodeGap: 10,
      nodeWidth: 14,
      label: { fontSize: 11, color: t.text },
      lineStyle: { color: "gradient", curveness: 0.5 },
      levels: [
        { depth: 0, label: { position: "right" } },
        { depth: 1 },
        { depth: 2, label: { position: "right" } },
      ],
    }],
  }, true);
  const viaCount = {};
  for (const e of graph.edges) viaCount[e.via] = (viaCount[e.via] || 0) + 1;
  const modeName = { tables: "表级", metrics: "指标级", full: "全链路" }[mode];
  const modeTag = {
    tables: "raw → dwd → dws → ads · 边标注加工任务",
    metrics: "来源表 → 基础指标 → 派生指标 · 由指标字典推导",
    full: "表级 + 指标级合并 · 完整数据流",
  }[mode];
  el("liTitle").textContent = `${modeName}血缘`;
  el("liTag").textContent = modeTag;
  el("liMeta").textContent =
    `${modeName}血缘：${nodes.length} 节点 / ${links.length} 条数据流 ｜ ` +
    `覆盖任务：${Object.keys(viaCount).filter((v) => v !== "api").join("、")}`;
  chart.off("click");
  chart.on("click", (p) => {
    if (p.dataType === "node") showDetail(p.name);
  });
  showDetail(null);
}

function showDetail(nodeId) {
  const box = el("liDetail");
  if (!nodeId) {
    box.innerHTML = `<div class="hint">点击图中节点查看它的上游（数据从哪来）与下游（改动影响谁）。</div>`;
    return;
  }
  const node = graph.nodes.find((n) => n.id === nodeId) || {};
  const ups = graph.edges.filter((e) => e.target === nodeId);
  const downs = graph.edges.filter((e) => e.source === nodeId);
  const pill = (n) => `<span class="badge neutral">${esc(n)}</span>`;
  const chain = (list) => list.map((e) =>
    `${pill(e.source)} <span class="muted">—${esc(e.via)}→</span> ${pill(e.target)}`).join("<br/>") || "—";
  const metricExtra = node.kind === "metric"
    ? `<div class="note" style="margin-top:8px">口径版本 ${esc((node.all_versions || []).join(" / "))} ｜ Owner ${esc(node.owner || "—")} ｜ 当前版本 ${esc(node.version || "—")}</div>`
    : "";
  box.innerHTML = `
    <div class="note info">
      <b>${esc(nodeId)}</b>（${esc(node.kind)} · ${esc(node.layer)}）${metricExtra}
      <div class="form-grid" style="margin-top:8px; grid-template-columns:1fr 1fr">
        <div><b class="hint">上游 ${ups.length}</b><br/>${chain(ups)}</div>
        <div><b class="hint">下游 ${downs.length}</b><br/>${chain(downs)}</div>
      </div>
    </div>`;
}

el("liMode").addEventListener("change", load);

export function onThemeChange() {
  if (graph) render(el("liMode").value);
}
