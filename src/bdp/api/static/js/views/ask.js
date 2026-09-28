/** 智能问答视图：会话式历史 + 示例问题 + pending 态 + 复制答案 + 降级显式标注。 */
import { api, state } from "../api.js";
import { el, esc, toast, emptyState } from "../ui.js";

const EXAMPLES = ["退款率最近怎么样？", "退货政策是什么？", "客单价环比变化？", "客服满意度如何？"];

const thread = [];

export function load() {
  if (!thread.length) {
    el("askThread").innerHTML = emptyState("✦", "向 InsightAgent 提问",
      state.user.role === "brand" ? "答案中的数值都来自指标工具并标注口径版本" : "平台级账号请先在右上角选择租户");
    renderExamples();
  }
}

function renderExamples() {
  el("askExamples").innerHTML = `<div class="chips">` +
    EXAMPLES.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("") +
    `</div>`;
}

el("askExamples").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-q]");
  if (chip) { el("askInput").value = chip.dataset.q; ask(); }
});

el("askBtn").addEventListener("click", ask);
el("askInput").addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });

let pending = false;
async function ask() {
  if (pending) return;
  const q = el("askInput").value.trim();
  if (!q) return;
  if (!state.tenant) { toast("请先在右上角指定租户（X-Tenant-Id）", "warning"); return; }

  pending = true;
  el("askBtn").disabled = true;
  thread.push({ role: "user", text: q });
  renderThread({ pending: true });
  el("askInput").value = "";
  try {
    const d = await api("/v1/agent/ask", { method: "POST", tenant: state.tenant, body: { question: q } });
    thread.push({
      role: "assistant", text: d.answer, degraded: d.degraded,
      reason: d.degraded_reason, citations: d.citations || [], mode: d.mode,
    });
  } catch (e) {
    thread.push({ role: "assistant", text: `查询失败：${e.message}`, degraded: true, citations: [] });
  } finally {
    pending = false;
    el("askBtn").disabled = false;
    renderThread({});
  }
}

function renderThread({ pending: isPending }) {
  const box = el("askThread");
  box.innerHTML = thread.map((t, i) => t.role === "user"
    ? `<div class="chat-turn"><div class="q">${esc(t.text)}</div></div>`
    : `<div class="chat-turn"><div class="a">
        <div class="answer-text">${esc(t.text)}</div>
        ${t.degraded && t.reason ? `<div class="note" style="margin-top:8px">降级：${esc(t.reason)}</div>` : ""}
        ${t.citations?.length ? `<div class="meta">${t.citations.map(citeChip).join("")}</div>` : ""}
        <div class="meta">
          ${t.mode ? `<span class="badge neutral">模式 ${esc(t.mode)}</span>` : ""}
          ${t.role === "assistant" && !isLastPending(i) ? `<button class="btn link" data-copy="${i}">复制答案</button>` : ""}
        </div>
      </div></div>`
  ).join("");
  if (isPending) {
    box.insertAdjacentHTML("beforeend",
      `<div class="chat-turn"><div class="a"><div class="skeleton row" style="width:70%"></div><div class="skeleton row" style="width:45%"></div></div></div>`);
  }
  box.scrollTop = box.scrollHeight;
}

const isLastPending = () => false;

function citeChip(c) {
  return c.type === "metric"
    ? `<span class="badge success">指标 ${esc(c.code)} · ${esc(c.caliber_version || "?")}</span>`
    : `<span class="badge info">知识 ${esc(c.doc_id)}#${c.chunk_ix}</span>`;
}

el("askThread").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const turn = thread[Number(btn.dataset.copy)];
  if (turn?.text) navigator.clipboard.writeText(turn.text).then(() => toast("答案已复制", "success"));
});

export function onThemeChange() { /* 无图表 */ }
