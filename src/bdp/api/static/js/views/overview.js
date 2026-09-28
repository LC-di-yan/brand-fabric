/** 经营概览视图：KPI（带 sparkline）+ 三口径趋势 + 平台结构 + 品牌对比 + 客服效率 + 数据质量。 */
import { api, state } from "../api.js";
import { el, esc, fmtMoney, fmtNum, fmtPct, setErr, skeleton, emptyState, progressBar } from "../ui.js";
import { makeChart, chartTheme, axisStyle, legendStyle } from "../charts.js";

const KPI_LABELS = { GMV_PAID: "支付 GMV", GMV_SETTLE: "结算 GMV", ORDER_CNT: "订单数", REFUND_RATE: "退款率" };

export async function load() {
  const box = el("ovKpis");
  box.innerHTML = Array.from({ length: 4 }, () =>
    `<div class="card kpi"><div class="skeleton title"></div><div class="skeleton row" style="width:60%"></div></div>`).join("");
  skeleton(el("ovDqTable"), "table");
  try {
    const d = await api("/v1/dashboard/summary", { tenant: state.tenant });
    renderKpis(d);
    renderTrend(d);
    renderPlatform(d);
    renderTenant(d);
    renderCs(d);
    renderDq(d);
  } catch (e) {
    setErr(box, e, () => load());
  }
}

function sparkSeries(d, code) {
  // dashboard summary 的 trend.series.points 是与 dates 对齐的纯数值数组
  const pts = (code2) => (d.trend.series || []).find((s) => s.metric_code === code2)?.points || [];
  const paid = pts("GMV_PAID");
  const refund = pts("REFUND_AMOUNT");
  if (code === "GMV_PAID") return paid;
  if (code === "GMV_SETTLE") return d.trend.dates.map((_, i) => (paid[i] || 0) - (refund[i] || 0));
  if (code === "REFUND_RATE") return d.trend.dates.map((_, i) => (paid[i] ? (refund[i] || 0) / paid[i] : 0));
  return null;
}

function sparkline(series, color) {
  if (!series || series.length < 2) return "";
  const id = `spark-${Math.random().toString(36).slice(2, 8)}`;
  requestAnimationFrame(() => {
    const dom = document.getElementById(id);
    if (!dom) return;
    const chart = echarts.init(dom);
    chart.setOption({
      grid: { left: 0, right: 0, top: 3, bottom: 0 },
      xAxis: { type: "category", show: false, data: series.map((_, i) => i) },
      yAxis: { type: "value", show: false, scale: true },
      series: [{ type: "line", data: series, showSymbol: false, smooth: true,
        lineStyle: { width: 1.6, color }, areaStyle: { color, opacity: 0.12 } }],
    });
  });
  return `<div class="chart spark" id="${id}"></div>`;
}

function renderKpis(d) {
  const t = chartTheme();
  el("ovKpis").innerHTML = d.kpis.filter((x) => KPI_LABELS[x.metric_code]).map((x) => {
    const v = x.unit === "CNY" ? fmtMoney(x.value) : x.unit === "比例" ? fmtPct(x.value) : fmtNum(x.value);
    return `<div class="card kpi">
      <div class="label">${KPI_LABELS[x.metric_code]}</div>
      <div class="value">${v}</div>
      ${sparkline(sparkSeries(d, x.metric_code), t.series[0])}
      <div class="meta">口径 ${esc(x.caliber_version)} ｜ ${esc(x.owner)}</div>
    </div>`;
  }).join("") + `<div class="card kpi"><div class="label">口径说明</div>
      <div class="meta" style="margin-top:8px; line-height:1.8">${esc(d.caliber.note)}</div></div>`;
}

function renderTrend(d) {
  const t = chartTheme();
  const names = { GMV_ORDER: "下单 GMV", GMV_PAID: "支付 GMV", REFUND_AMOUNT: "退款金额" };
  makeChart("ovTrend").setOption({
    tooltip: { trigger: "axis", valueFormatter: (v) => "¥" + Number(v).toLocaleString("zh-CN") },
    legend: legendStyle(),
    grid: { left: 56, right: 18, top: 34, bottom: 28 },
    xAxis: { type: "category", data: d.trend.dates, ...axisStyle() },
    yAxis: { type: "value", ...axisStyle(), axisLabel: { ...axisStyle().axisLabel, formatter: (v) => (v / 10000).toFixed(0) + "万" } },
    series: d.trend.series.map((s, i) => ({
      name: names[s.metric_code], type: "line", smooth: true, showSymbol: false,
      data: s.points, lineStyle: { width: 2, color: t.series[i] }, itemStyle: { color: t.series[i] },
    })),
  });
}

function renderPlatform(d) {
  const t = chartTheme();
  makeChart("ovPlatform").setOption({
    tooltip: { trigger: "item", formatter: (p) => `${p.name}<br/>¥${Number(p.value).toLocaleString("zh-CN")}（${p.percent}%）` },
    legend: { bottom: 0, textStyle: { fontSize: 10.5, color: t.text } },
    series: [{
      type: "pie", radius: ["48%", "70%"], center: ["50%", "44%"],
      label: { fontSize: 10.5, color: t.text, formatter: "{b}\n{d}%" },
      itemStyle: { borderColor: t.surface, borderWidth: 2 },
      data: d.platforms.map((p, i) => ({ name: p.platform, value: p.paid_gmv, itemStyle: { color: t.series[i % t.series.length] } })),
    }],
  });
}

function renderTenant(d) {
  const own = state.user.tenant_id;
  document.querySelector("#ovTenantTable tbody").innerHTML = d.tenant_compare.map((t) => `
    <tr class="${own === t.tenant_id ? "row-highlight" : ""}">
      <td>${esc(t.name)}${own === t.tenant_id ? ' <span class="badge info">本品牌</span>' : ""}</td>
      <td class="muted">${esc(t.category)}</td>
      <td class="num">${fmtMoney(t.paid_gmv)}</td><td class="num">${fmtMoney(t.settlement_gmv)}</td>
      <td class="num">${fmtPct(t.refund_rate)} ${t.refund_rate > 0.15 ? '<span class="badge danger">偏高</span>' : ""}</td>
      <td class="num">${fmtNum(t.order_cnt)}</td><td class="num">¥${t.aov}</td>
    </tr>`).join("") || `<tr><td colspan="7">${emptyState("▦", "无对比数据")}</td></tr>`;
}

function renderCs(d) {
  const t = chartTheme();
  const byCode = {};
  (d.trend.cs_series || []).forEach((s) => (byCode[s.metric_code] = s.points));
  makeChart("ovCs").setOption({
    tooltip: { trigger: "axis" },
    legend: legendStyle(),
    grid: { left: 45, right: 45, top: 34, bottom: 28 },
    xAxis: { type: "category", data: d.trend.dates, ...axisStyle() },
    yAxis: [
      { type: "value", name: "会话量", ...axisStyle(), nameTextStyle: { fontSize: 10, color: t.text } },
      { type: "value", name: "首响(秒)", ...axisStyle(), nameTextStyle: { fontSize: 10, color: t.text }, splitLine: { show: false } },
    ],
    series: [
      { name: "会话量", type: "bar", data: byCode.CS_SESSION_CNT || [], itemStyle: { color: t.series[1], borderRadius: [3, 3, 0, 0] } },
      { name: "机器人接待", type: "bar", data: byCode.CS_BOT_CNT || [], itemStyle: { color: t.series[0], borderRadius: [3, 3, 0, 0] } },
      { name: "平均首响", type: "line", yAxisIndex: 1, smooth: true, showSymbol: false,
        data: byCode.CS_FIRST_RESP_AVG || [], itemStyle: { color: t.orange }, lineStyle: { width: 2 } },
    ],
  });
}

function renderDq(d) {
  const q = d.data_quality;
  el("ovDqTag").textContent = q.run_id ? `最近校验 ${q.run_id}` : "尚未校验";
  document.querySelector("#ovDqTable tbody").innerHTML = (q.details || []).map((r) => {
    const failed = r.failed_rows > 0;
    return `<tr>
      <td><b>${esc(r.rule_id)}</b></td><td class="muted">${esc(r.table)}</td>
      <td class="num">${fmtNum(r.checked_rows)}</td>
      <td class="num">${failed ? `<span class="badge ${r.severity === "error" ? "danger" : "warning"}">${fmtNum(r.failed_rows)}</span>` : fmtNum(0)}</td>
      <td style="min-width:120px">${progressBar(r.pass_rate)}<span class="hint">${fmtPct(r.pass_rate, 3)}</span></td>
      <td class="muted" style="text-align:left; white-space:normal">${esc(r.description)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6">${emptyState("✓", "无校验结果", "执行 pipeline 后展示")}</td></tr>`;
}

export function onThemeChange() { load(); }
