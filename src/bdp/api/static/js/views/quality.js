/** 质量与审计视图：DQ 规则（严重度徽章 + 进度条）与失败样本、租户越权审计。 */
import { api, state } from "../api.js";
import { el, esc, fmtNum, setErr, skeleton, emptyState } from "../ui.js";

export async function load() {
  await Promise.all([loadDq(), loadAudit()]);
}

async function loadDq() {
  const tbody = document.querySelector("#dqTable tbody");
  skeleton(tbody, "table");
  try {
    const d = await api("/v1/admin/data-quality", { tenant: state.tenant });
    document.querySelector("#dqSummary").textContent =
      `最近校验 ${d.summary.run_id || "-"} ｜ 通过率 ${(d.summary.overall_pass_rate * 100).toFixed(3)}%`;
    tbody.innerHTML = (d.summary.details || []).map((r) => {
      const failed = r.failed_rows > 0;
      return `<tr style="cursor:pointer" data-rule="${esc(r.rule_id)}">
        <td><b>${esc(r.rule_id)}</b></td><td class="muted">${esc(r.table)}</td>
        <td class="num">${fmtNum(r.checked_rows)}</td>
        <td class="num">${failed ? `<span class="badge ${r.severity === "error" ? "danger" : "warning"}">${fmtNum(r.failed_rows)}</span>` : "0"}</td>
        <td class="num">${(r.pass_rate * 100).toFixed(3)}%</td>
        <td class="hint">${failed ? "点击查看失败样本" : ""}</td></tr>`;
    }).join("") || `<tr><td colspan="6">${emptyState("✓", "无校验结果", "执行 pipeline 后展示")}</td></tr>`;
  } catch (e) { setErr(tbody, e, loadDq); }
}

document.querySelector("#dqTable").addEventListener("click", (e) => {
  const tr = e.target.closest("[data-rule]");
  if (tr) loadSamples(tr.dataset.rule);
});

window.loadDqSamples = async (ruleId) => {
  const box = el("dqSamples");
  skeleton(box, "rows", 2);
  try {
    const d = await api(`/v1/admin/data-quality/${ruleId}/samples`, { tenant: state.tenant });
    const cols = d.samples.length ? Object.keys(d.samples[0]).slice(0, 6) : [];
    box.innerHTML = `<h2 style="font-size:var(--fs-sm); margin:10px 0 8px">失败样本 · ${esc(ruleId)}（${d.count} 条）</h2>
      ${d.samples.length ? `<div class="table-wrap" style="max-height:260px"><table>
        <thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${d.samples.map((s) => `<tr>${cols.map((c) => `<td class="mono muted" style="text-align:left">${esc(String(s[c]).slice(0, 32))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
      : emptyState("✓", "没有失败样本")}`;
  } catch (e) { setErr(box, e, () => loadSamples(ruleId)); }
};

async function loadAudit() {
  const tbody = document.querySelector("#auditTable tbody");
  skeleton(tbody, "table", 3);
  try {
    const qs = el("auditDenied").checked ? "?only_denied=true" : "";
    const d = await api("/v1/admin/audit" + qs, { tenant: state.tenant });
    tbody.innerHTML = d.items.map((i) => `<tr>
      <td class="muted num">${esc(i.ts.replace("T", " ").slice(5, 19))}</td>
      <td>${esc(i.username)} <span class="badge neutral">${esc(i.role)}</span></td>
      <td class="mono muted">${esc(i.action)}</td>
      <td class="mono">${esc(i.requested_tenant || "-")}</td>
      <td><span class="badge ${i.allowed ? "success" : "danger"}">${i.allowed ? "允许" : "拒绝"}</span></td></tr>`).join("")
      || `<tr><td colspan="5">${emptyState("✓", "暂无审计记录", "发生越权尝试时会在此留痕")}</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5">${emptyState("–", "当前身份无权查看审计日志")}</td></tr>`;
  }
}
el("auditDenied").addEventListener("change", loadAudit);
