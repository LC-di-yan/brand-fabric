/** 智能问答视图：会话式历史 + 示例问题 + pending 态 + 复制答案 + 降级显式标注
 *  + Agentic RAG：置信徽章 / 推理轨迹折叠区 / 策略切换 / 多轮会话（thread_id）。 */
import { api, state } from "../api.js";
import { el, esc, toast, emptyState } from "../ui.js";

const EXAMPLES = ["退款率最近怎么样？", "退货政策是什么？", "客单价环比变化？", "不想要了怎么办？"];

const thread = [];
// Agentic RAG 会话状态：策略（缺省读后端配置）与 thread_id（多轮指代消解）
const session = { strategy: "", threadId: "" };

export function load() {
  if (!thread.length) {
    el("askThread").innerHTML = emptyState("✦", "向 InsightAgent 提问",
      state.user.role === "brand" ? "答案中的数值都来自指标工具并标注口径版本" : "平台级账号请先在右上角选择租户");
    renderExamples();
  }
}

function renderExamples() {
  el("askExamples").innerHTML = `
    <div class="chips">
      <select id="askStrategy" class="input" style="width:auto" aria-label="RAG 策略">
        <option value="" ${session.strategy === "" ? "selected" : ""}>策略：默认</option>
        <option value="single" ${session.strategy === "single" ? "selected" : ""}>策略：single 直通</option>
        <option value="agentic" ${session.strategy === "agentic" ? "selected" : ""}>策略：agentic 管线</option>
      </select>
      ${EXAMPLES.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}
    </div>`;
}

el("askExamples").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-q]");
  if (chip) { el("askInput").value = chip.dataset.q; ask(); }
});
el("askExamples").addEventListener("change", (e) => {
  if (e.target.id === "askStrategy") {
    session.strategy = e.target.value;
    toast(session.strategy ? `RAG 策略已切换：${session.strategy}` : "RAG 策略：跟随后端配置", "info");
  }
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
    const body = { question: q };
    if (session.strategy) body.strategy = session.strategy;
    if (session.threadId) body.thread_id = session.threadId;
    const d = await api("/v1/agent/ask", { method: "POST", tenant: state.tenant, body });
    session.threadId = d.thread_id || session.threadId;  // 记住会话，续问自动带
    thread.push({
      role: "assistant", text: d.answer, degraded: d.degraded,
      reason: d.degraded_reason, citations: d.citations || [], mode: d.mode,
      confidence: d.confidence || "", strategy: d.strategy || "single",
      toolTrace: d.tool_trace || [],
    });
  } catch (e) {
    thread.push({ role: "assistant", text: `查询失败：${e.message}`, degraded: true, citations: [] });
  } finally {
    pending = false;
    el("askBtn").disabled = false;
    renderThread({});
  }
}

const CONF_BADGE = {
  high: ["success", "置信 高"],
  medium: ["warning", "置信 中"],
  low: ["danger", "置信 低"],
};

function renderThread({ pending: isPending }) {
  const box = el("askThread");
  box.innerHTML = thread.map((t, i) => t.role === "user"
    ? `<div class="chat-turn"><div class="q">${esc(t.text)}</div></div>`
    : `<div class="chat-turn"><div class="a">
        <div class="answer-text">${esc(t.text)}</div>
        ${t.degraded && t.reason ? `<div class="note" style="margin-top:8px">降级：${esc(t.reason)}</div>` : ""}
        ${t.citations?.length ? `<div class="meta">${t.citations.map(citeChip).join("")}</div>` : ""}
        <div class="meta">
          ${t.strategy && t.strategy !== "single" ? `<span class="badge info">RAG ${esc(t.strategy)}</span>` : ""}
          ${t.confidence ? confidenceBadge(t.confidence) : ""}
          ${t.mode ? `<span class="badge neutral">模式 ${esc(t.mode)}</span>` : ""}
          ${t.role === "assistant" && !isLastPending(i) ? `<button class="btn link" data-copy="${i}">复制答案</button>` : ""}
          ${t.toolTrace?.some((x) => x.trace?.length)
            ? `<button class="btn link" data-trace="${i}">推理轨迹</button>`
            : ""}
        </div>
        <div id="traceBox${i}" style="display:none; margin-top:8px"></div>
      </div></div>`
  ).join("");
  if (isPending) {
    box.insertAdjacentHTML("beforeend",
      `<div class="chat-turn"><div class="a"><div class="skeleton row" style="width:70%"></div><div class="skeleton row" style="width:45%"></div></div></div>`);
  }
  box.scrollTop = box.scrollHeight;
}

const isLastPending = () => false;

function confidenceBadge(conf) {
  const [cls, label] = CONF_BADGE[conf] || ["neutral", `置信 ${conf}`];
  return `<span class="badge ${cls}" title="RAG 引用核验置信">${label}</span>`;
}

function citeChip(c) {
  return c.type === "metric"
    ? `<span class="badge success">指标 ${esc(c.code)} · ${esc(c.caliber_version || "?")}</span>`
    : `<span class="badge info">知识 ${esc(c.doc_id)}#${c.chunk_ix}</span>`;
}

function renderTrace(turn) {
  const steps = [];
  for (const t of turn.toolTrace || []) {
    if (!t.trace?.length) continue;
    steps.push(`<div class="hint" style="margin:6px 0 2px">检索：${esc(t.args?.query || "")}（${t.hits ?? 0} 块 · 置信 ${t.retrieval_confidence ?? "-"}）</div>`);
    for (const s of t.trace) {
      const desc = traceStepDesc(s);
      if (desc) steps.push(`<div class="event-item info"><span class="ts">${esc(s.step)}</span>${desc}</div>`);
    }
  }
  return steps.join("") || `<div class="hint">本轮无管线轨迹（single 直通模式）。</div>`;
}

function traceStepDesc(s) {
  switch (s.step) {
    case "coreference":
      return s.resolved ? `指代消解 → ${esc(s.query)}` : "无指代需消解";
    case "classify":
      return `意图分类 → ${esc(s.intent)}`;
    case "decompose":
      return `拆分 ${s.subqueries.length} 个子查询：${s.subqueries.map((x) => esc(x.text)).join(" ｜ ")}`;
    case "metric_intent":
      return `指标意图（改走指标工具）：${(s.codes || []).map(esc).join(", ")}`;
    case "retrieve":
      return `检索「${esc(s.query)}」 命中 ${s.hits} · 判定 ${s.judge}${s.misses?.length ? `（${s.misses.map(esc).join(",")}）` : ""}`;
    case "domain_unlock":
      return `域解锁：提示域 ${esc(s.hinted)} 判定 ${s.hinted_score} → 全域 ${s.open_score}`;
    case "rewrite":
      return `改写：「${esc(s.from)}」 → 「${esc(s.to)}」`;
    case "hop_neighbor":
      return `多跳·文档内边：取相邻切片 ${s.fetched} 条`;
    case "hop_entity":
      return `多跳·实体种子「${esc(s.seed)}」新增 ${s.added} 条`;
    case "context":
      return `上下文：入 ${s.input_chunks} → 去重后 ${s.input_chunks - s.deduped} → 压缩 ${s.compressed} → 用 ${s.used_tokens}/${s.budget_tokens} tokens`;
    case "rejected":
      return `已拒绝：${esc(s.reason)}`;
    case "single_search":
      return `直通检索命中 ${s.hits} 条`;
    default:
      return "";
  }
}

el("askThread").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (btn) {
    const turn = thread[Number(btn.dataset.copy)];
    if (turn?.text) navigator.clipboard.writeText(turn.text).then(() => toast("答案已复制", "success"));
    return;
  }
  const tbtn = e.target.closest("[data-trace]");
  if (tbtn) {
    const i = Number(tbtn.dataset.trace);
    const box = el(`traceBox${i}`);
    if (box.style.display === "none") {
      box.innerHTML = `<div class="note" style="max-height:280px; overflow:auto">${renderTrace(thread[i])}</div>`;
      box.style.display = "";
      tbtn.textContent = "收起轨迹";
    } else {
      box.style.display = "none";
      tbtn.textContent = "推理轨迹";
    }
  }
});

export function onThemeChange() { /* 无图表 */ }
