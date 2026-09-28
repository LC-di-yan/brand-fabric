/** 指标查询视图：字典（按域分组）+ 口径版本 + 同比环比 + 图表/明细 + CSV 导出。 */
import { api, state, downloadText } from "../api.js";
import { el, esc, fmtNum, fmtPct, setErr, skeleton, emptyState, toast, debounce } from "../ui.js";
import { makeChart, chartTheme, axisStyle } from "../charts.js";

const UNIT_FMT = {
  CNY: (v) => "¥" + Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 0 }),
  "比例": (v) => fmtPct(v),
  "分": (v) => Number(v).toFixed(2),
  "秒": (v) => Number(v).toFixed(1) + "s",
  "笔": (v) => fmtNum(v),
  "次": (v) => fmtNum(v),
};

const DOMAIN = (code) => (code.startsWith("CS_") ? "客服域" : "交易域");

export async function load() {
  if (!state.metricsList.length) {
    skeleton(el("mqList"), "rows", 5);
    try {
      const d = await api("/v1/metrics", { tenant: state.tenant });
      state.metricsList = d.items || [];
      state.metricDef = Object.fromEntries(state.metricsList.map((m) => [m.metric_code, m]));
      el("mqMetric").innerHTML = state.metricsList
        .filter((m, i, arr) => arr.findIndex((x) => x.metric_code === m.metric_code) === i)
        .map((m) => `<option value="${m.metric_code}">${m.metric_name}</option>`).join("");
      await refreshVersions();
    } catch (e) { setErr(el("mqList"), e, load); }
  }
  renderList(el("mqFilter")?.value.trim() || "");
}

function renderList(kw) {
  const items = state.metricsList.filter((m) =>
    !kw || (m.metric_name + m.metric_code + m.definition).toLowerCase().includes(kw.toLowerCase()));
  const groups = new Map();
  for (const m of items) {
    const dom = DOMAIN(m.metric_code);
    if (!groups.has(dom)) groups.set(dom, []);
    groups.get(dom).push(m);
  }
  el("mqList").innerHTML = [...groups.entries()].map(([dom, list]) => `
    <div class="hint" style="margin: 6px 0 2px; letter-spacing:.04em">${dom} · ${list.length}</div>
    ${list.map((m) => `
    <div class="hit clickable" data-metric="${esc(m.metric_code)}">
      <div class="head"><span><b>${esc(m.metric_name)}</b> <span class="mono">${esc(m.metric_code)}</span> · ${esc(m.caliber_version)}</span>
        <span class="badge ${m.is_derived ? "info" : "success"}">${m.is_derived ? "派生" : "基础"}</span></div>
      <div class="text muted" style="font-size:var(--fs-xs)">${esc(m.definition)}</div>
    </div>`).join("")}`).join("")
    || emptyState("⌕", "无匹配指标", "换个关键词试试");
}

export function pickMetric(code) {
  el("mqMetric").value = code;
  refreshVersions().then(runQuery);
}

// 事件绑定（module 脚本默认 defer，DOM 已就绪）
el("mqFilter").addEventListener("input", debounce(() => renderList(el("mqFilter").value.trim()), 200));
el("mqList").addEventListener("click", (e) => {
  const hit = e.target.closest("[data-metric]");
  if (hit) pickMetric(hit.dataset.metric);
});
el("mqMetric").addEventListener("change", () => refreshVersions());
el("mqRun").addEventListener("click", runQuery);
el("mqExport").addEventListener("click", exportCsv);

async function refreshVersions() {
  const code = el("mqMetric").value;
  const sel = el("mqVersion");
  sel.innerHTML = `<option value="">自动（按日期解析）</option>`;
  try {
    const d = await api(`/v1/metrics/${code}/versions`, { tenant: state.tenant });
    (d.versions || []).forEach((v) => {
      sel.innerHTML += `<option value="${v.caliber_version}">${v.caliber_version}（${v.effective_from} 起）</option>`;
    });
  } catch { /* 版本加载失败不阻塞查询 */ }
}

function currentRange() {
  const end = new Date("2026-09-25");
  const start = new Date(end);
  start.setDate(start.getDate() - (state.rangeDays - 1));
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
}

async function runQuery() {
  const code = el("mqMetric").value;
  const version = el("mqVersion").value || null;
  const dim = el("mqDim").value;
  const compare = el("mqCompare").value;
  const { start, end } = currentRange();
  const totalBox = el("mqTotal");
  totalBox.textContent = "计算中…";
  el("mqNotice").style.display = "none";
  try {
    const d = await api("/v1/metrics/query", {
      method: "POST", tenant: state.tenant,
      body: { metric_code: code, caliber_version: version, dim_type: dim, compare, start, end },
    });
    renderResult(d);
  } catch (e) {
    totalBox.textContent = "";
    setErr(el("mqResultBox"), e, runQuery);
  }
}

function renderResult(d) {
  const fmt = UNIT_FMT[d.unit] || ((v) => Number(v).toLocaleString("zh-CN"));
  let totalTxt = `${esc(d.metric_name)}（口径 ${esc(d.caliber_version)}）合计 <b>${fmt(d.total)}</b>`;
  const pct = d.compare?.delta_pct;
  if (pct !== null && pct !== undefined) {
    totalTxt += `，环比 <span class="${pct >= 0 ? "" : ""}" style="color:var(${pct >= 0 ? "--positive" : "--negative"})">${pct >= 0 ? "▲" : "▼"} ${(pct * 100).toFixed(2)}%</span>`;
  }
  el("mqTotal").innerHTML = totalTxt;
  el("mqNotice").textContent = d.caliber_notice || "";
  el("mqNotice").style.display = d.caliber_notice ? "block" : "none";

  const t = chartTheme();
  const byDate = {};
  d.points.forEach((p) => { byDate[p.dt] = (byDate[p.dt] || 0) + p.value; });
  const dates = Object.keys(byDate).sort();
  makeChart("mqChart").setOption({
    tooltip: { trigger: "axis" },
    grid: { left: 60, right: 18, top: 20, bottom: 28 },
    xAxis: { type: "category", data: dates, ...axisStyle() },
    yAxis: { type: "value", ...axisStyle() },
    series: [{ name: d.metric_name, type: "bar", barMaxWidth: 26,
      data: dates.map((x) => byDate[x]),
      itemStyle: { color: t.accent, borderRadius: [3, 3, 0, 0] } }],
  });

  const rows = d.points.slice(0, 400).map((p) => `<tr>
    <td class="muted">${p.dt}</td><td class="muted">${esc(p.dim_value)}</td><td class="num">${fmt(p.value)}</td></tr>`).join("");
  document.querySelector("#mqTable tbody").innerHTML =
    rows || `<tr><td colspan="3">${emptyState("◌", "该区间无数据点")}</td></tr>`;
}

async function exportCsv() {
  const code = el("mqMetric").value;
  const version = el("mqVersion").value || "";
  const dim = el("mqDim").value;
  const { start, end } = currentRange();
  const qs = new URLSearchParams({ metric_code: code, dim_type: dim, start, end });
  if (version) qs.set("caliber_version", version);
  try {
    const text = await api("/v1/metrics/export?" + qs.toString(), { tenant: state.tenant, raw: true });
    downloadText(`${code}.csv`, text);
    toast("CSV 已导出", "success");
  } catch (e) {
    toast(`导出失败：${e.message}`, "error");
  }
}

export function onThemeChange() { if (el("mqTotal").innerHTML) runQuery(); }
