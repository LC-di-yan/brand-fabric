/** Agent 编排视图：运行列表 + 明细（任务/事件流）+ 触发运行（参数化 modal）+ 重试 + 自动轮询。 */
import { api, state } from "../api.js";
import { el, esc, setErr, skeleton, emptyState, toast, openModal } from "../ui.js";

const STATUS_BADGE = {
  succeeded: ["success", "成功"], degraded: ["warning", "降级完成"], failed: ["danger", "失败"],
  skipped: ["warning", "跳过"], cancelled: ["danger", "取消"], running: ["info", "运行中"], pending: ["neutral", "排队"],
};
const badge = (s) => {
  const [cls, label] = STATUS_BADGE[s] || ["neutral", s];
  return `<span class="badge ${cls}">${label}</span>`;
};

let pollTimer = null;
let activeRunId = null;

export async function load() {
  const isPlat = ["admin", "ops"].includes(state.user.role);
  el("agentOps").style.display = isPlat ? "" : "none";
  if (isPlat) loadCapabilities();
  await loadRuns();
  ensurePolling();
}

export function unload() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function ensurePolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    if (!document.querySelector('.view[data-view="agents"].active')) return;
    const running = [...document.querySelectorAll("#runTable [data-status='running']")].length > 0
      || (activeRunId && (await isRunActive(activeRunId)));
    if (running) {
      await loadRuns({ silent: true });
      if (activeRunId) showRun(activeRunId, { silent: true });
    }
  }, 2000);
}

async function isRunActive(runId) {
  try {
    const d = await api(`/v1/agent/runs/${runId}`);
    if (d.status === "running") return true;
    if (d.status !== "running" && activeRunId === runId) {
      toast(`运行结束：${d.status}`, d.status === "succeeded" ? "success" : "warning");
      activeRunId = null;
    }
    return false;
  } catch { return false; }
}

async function loadCapabilities() {
  try {
    const capa = await api("/v1/agent/capabilities");
    const names = capa.agents.map((a) => esc(a.name)).join(" / ");
    el("agentCapa").style.display = "";
    el("agentCapa").innerHTML =
      `workers=${capa.workers}${capa.sqlite_mode ? "（SQLite 单写者，串行执行）" : ""} ｜ ` +
      `质量门禁 pass_rate ≥ ${capa.dq_gate}（阻断动作 ${esc(capa.dq_gate_action)}） ｜ agents：${names}`;
  } catch { /* 无权查看时静默 */ }
}

async function loadRuns({ silent = false } = {}) {
  const tbody = document.querySelector("#runTable tbody");
  if (!silent) skeleton(tbody, "table", 3);
  try {
    const d = await api("/v1/agent/runs");
    tbody.innerHTML = (d.items || []).map((r) => {
      const taskCount = r.stats?.tasks ? Object.values(r.stats.tasks).length : "";
      const summary = r.stats
        ? `${taskCount} 任务 ｜ ${r.stats.failed_tasks?.length ? `<span class="badge danger">失败 ${r.stats.failed_tasks.length}</span>` : ""}`
        : "";
      return `<tr style="cursor:pointer" data-run="${esc(r.run_id)}" data-status="${esc(r.status)}">
        <td class="mono muted" style="font-size:var(--fs-2xs)">${esc(r.run_id)}</td>
        <td>${esc(r.dag)}</td><td>${badge(r.status)}</td>
        <td class="muted">${esc(r.trigger)}</td>
        <td class="muted num">${esc((r.started_at || "").replace("T", " ").slice(5, 19))}</td>
        <td class="muted">${summary || "—"}</td></tr>`;
    }).join("") || `<tr><td colspan="6">${emptyState("◷", "尚无运行记录", "点击右上角「运行 nightly」或执行 cli agent run")}</td></tr>`;
  } catch (e) { if (!silent) setErr(document.querySelector("#runTable"), e, loadRuns); }
}

document.querySelector("#runTable").addEventListener("click", (e) => {
  const tr = e.target.closest("[data-run]");
  if (tr) showRun(tr.dataset.run);
});

export async function showRun(runId, { silent = false } = {}) {
  activeRunId = runId;
  el("runDetailCard").style.display = "";
  el("runDetailId").textContent = runId;
  if (!silent) skeleton(el("eventList"), "rows", 3);
  try {
    const d = await api(`/v1/agent/runs/${runId}`);
    const dur = (t) => (t.started_at && t.finished_at
      ? ((new Date(t.finished_at) - new Date(t.started_at)) / 1000).toFixed(1) + "s" : "—");
    document.querySelector("#taskTable tbody").innerHTML = (d.tasks || []).map((t) => `<tr>
      <td><b>${esc(t.name)}</b></td><td class="muted">${esc(t.agent)}</td>
      <td>${badge(t.status)}${t.attempt > 1 ? ` <span class="hint">×${t.attempt}</span>` : ""}</td>
      <td class="num muted">${t.attempt}/${t.max_attempts}</td>
      <td class="muted">${esc(t.tenant || "-")}</td>
      <td class="muted num">${dur(t)}</td>
      <td class="muted" style="max-width:180px; white-space:normal">${esc(t.error || (t.result?.warning ?? ""))}</td></tr>`).join("");
    const arts = d.artifacts || {};
    el("runArtifacts").innerHTML = arts.materialize
      ? `<div class="note info">fan-in 汇总：物化 ${Object.keys(arts).filter((a) => a.startsWith("materialized:")).length} 个租户，共 ${arts.materialize.total_written || 0} 行</div>`
      : "";
    const retryable = (d.tasks || []).filter((t) => ["failed", "skipped", "cancelled"].includes(t.status));
    el("taskRetryBox").innerHTML =
      (["admin", "ops"].includes(state.user.role) && retryable.length)
        ? retryable.map((t) => `<button class="btn sm" data-retry="${esc(t.task_id)}">重试 ${esc(t.name)}</button>`).join(" ")
        : "";
    renderEvents(d.events);
  } catch (e) { if (!silent) setErr(el("eventList"), e, () => showRun(runId)); }
}

function renderEvents(events) {
  const list = el("eventList");
  if (!events) return;
  list.innerHTML = events.slice(0, 60).map((e) => `
    <div class="event-item ${e.level}">
      <span class="ts">${esc(e.ts.replace("T", " ").slice(5, 19))}</span>${esc(e.message)}
    </div>`).join("") || `<div class="hint">暂无事件</div>`;
}

el("taskRetryBox").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-retry]");
  if (!btn) return;
  btn.disabled = true;
  try {
    await api(`/v1/agent/tasks/${btn.dataset.retry}/retry`, { method: "POST" });
    toast("已复位重试，运行在后台继续", "success");
    setTimeout(() => { loadRuns(); if (activeRunId) showRun(activeRunId); }, 600);
  } catch (err) { btn.disabled = false; toast(`重试失败：${err.message}`, "error"); }
});

el("agentRunNightly").addEventListener("click", () => {
  openModal({
    title: "触发 nightly 全链路",
    content: `
      <div class="field"><label>DAG</label>
        <select class="input" id="runDag" style="width:100%"><option value="nightly">nightly（接入→数仓→质量/知识库→指标）</option></select></div>
      <div class="form-grid" style="margin-top:10px">
        <div class="field"><label>数据天数（留空用默认）</label><input class="input" id="runDays" type="number" min="1" max="3650" placeholder="如 90" /></div>
        <div class="field"><label>随机种子（留空用默认）</label><input class="input" id="runSeed" type="number" placeholder="如 20260926" /></div>
      </div>
      <div class="note" style="margin-top:10px">SQLite 模式下为串行执行；运行可在「运行列表」观察进度。</div>`,
    confirmText: "触发运行",
    onConfirm: async () => {
      const params = {};
      const days = el("runDays").value;
      const seed = el("runSeed").value;
      if (days) params.days = Number(days);
      if (seed) params.seed = Number(seed);
      const d = await api("/v1/agent/runs", { method: "POST", body: { dag: el("runDag").value, params } });
      toast(`已触发运行 ${d.run_id}`, "success");
      activeRunId = d.run_id;
      await loadRuns();
      showRun(d.run_id);
    },
  });
});

export function onThemeChange() { /* Agent 视图无图表 */ }
