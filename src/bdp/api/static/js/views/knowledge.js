/** 知识检索视图：混合检索（关键词高亮 + 得分条）+ 文档 CRUD + 切片 modal + 重建。 */
import { api, state } from "../api.js";
import { el, esc, highlight, setErr, skeleton, emptyState, toast, openModal, confirmAction } from "../ui.js";

export async function load() {
  await Promise.all([loadDocs()]);
}

/* ---------------- 检索 ---------------- */

let searching = false;
async function doSearch() {
  if (searching) return;
  const q = el("kbQuery").value.trim();
  const box = el("kbResults");
  if (!q) { toast("请输入检索内容", "warning"); return; }
  searching = true;
  el("kbSearchBtn").disabled = true;
  skeleton(box, "rows", 3);
  try {
    const d = await api("/v1/kb/search", {
      method: "POST", tenant: state.tenant,
      body: { query: q, kb_type: el("kbType").value || null, top_k: 5, mode: el("kbMode").value },
    });
    renderResults(d, q);
  } catch (e) { setErr(box, e, doSearch); }
  finally { searching = false; el("kbSearchBtn").disabled = false; }
}

function renderResults(d, q) {
  el("kbMeta").textContent = `候选 ${d.candidates} 条 ｜ 模式 ${d.mode}${d.rerank ? " · 重排" : ""}`;
  const maxScore = Math.max(...(d.results || []).map((r) => r.score), 0.0001);
  el("kbResults").innerHTML = (d.results || []).map((r, i) => `
    <div class="hit">
      <div class="head">
        <span><span class="badge neutral">#${i + 1}</span> <b>${esc(r.kb_type)}</b> · <span class="mono">${esc(r.doc_id)}</span> #${r.chunk_ix}</span>
        <span style="display:flex;align-items:center;gap:8px">
          <span style="width:80px"><div class="progress"><i class="success" style="width:${(r.score / maxScore) * 100}%"></i></div></span>
          <span class="num muted">${r.score.toFixed(4)}</span>
        </span>
      </div>
      <div class="text">${highlight(r.text, q)}</div>
    </div>`).join("")
    || emptyState("⌕", "没有命中", d.warning || "换个问法，或检查该品牌的知识库是否已入库");
  if (d.warning) toast(d.warning, "warning");
}

el("kbSearchBtn").addEventListener("click", doSearch);
el("kbQuery").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });

/* ---------------- 文档管理 ---------------- */

async function loadDocs() {
  const box = el("kbDocs");
  skeleton(box, "rows", 3);
  try {
    const d = await api("/v1/kb/documents", { tenant: state.tenant });
    el("kbDocTotal").textContent = `共 ${d.total} 篇`;
    box.innerHTML = (d.items || []).map((doc) => `
      <div class="hit">
        <div class="head"><span><b>${esc(doc.title)}</b></span><span class="badge neutral">${esc(doc.kb_type)}</span></div>
        <div class="text muted" style="font-size:var(--fs-2xs)">${esc(doc.doc_id)} ｜ ${doc.chunks} 片 ｜ 更新 ${esc(doc.updated_at.slice(0, 10))}</div>
        <div style="margin-top:8px;display:flex;gap:6px">
          <button class="btn link" data-chunks="${esc(doc.doc_id)}">查看切片</button>
          <button class="btn link danger" data-del="${esc(doc.doc_id)}" data-title="${esc(doc.title)}">删除</button>
        </div>
      </div>`).join("") || emptyState("▤", "没有文档", "先执行 mock 与 kb 入库");
  } catch (e) { setErr(box, e, loadDocs); }
}

el("kbDocs").addEventListener("click", async (e) => {
  const chunksBtn = e.target.closest("[data-chunks]");
  const delBtn = e.target.closest("[data-del]");
  if (chunksBtn) await showChunks(chunksBtn.dataset.chunks);
  if (delBtn) await deleteDoc(delBtn.dataset.del, delBtn.dataset.title);
});

async function showChunks(docId) {
  openModal({
    title: `切片 · ${docId}`,
    wide: true,
    content: `<div id="chunksBody">${Array.from({ length: 3 }, () => `<div class="skeleton row"></div>`).join("")}</div>`,
    cancelText: "关闭",
  });
  try {
    const d = await api(`/v1/kb/documents/${docId}/chunks`, { tenant: state.tenant });
    el("chunksBody").innerHTML = d.chunks.map((c) => `
      <div class="hit" style="margin-bottom:8px">
        <div class="head"><span class="mono">#${c.chunk_ix}</span>
          <span class="hint">${c.char_len} 字 · sha ${esc(c.content_hash.slice(0, 8))}</span></div>
        <div class="text">${esc(c.text)}</div>
      </div>`).join("") || emptyState("◌", "无切片");
  } catch (e) {
    el("chunksBody").innerHTML = `<div class="errbox">${esc(e.message)}</div>`;
  }
}

async function deleteDoc(docId, title) {
  const ok = await confirmAction({
    title: "删除知识文档",
    message: `确认删除「${title}」（${docId}）及其全部切片？删除后立即从向量库移除，不可恢复。`,
    confirmText: "删除",
  });
  if (!ok) return;
  try {
    await api(`/v1/kb/documents/${docId}`, { method: "DELETE", tenant: state.tenant });
    toast("文档已删除", "success");
    loadDocs();
  } catch (e) { toast(`删除失败：${e.message}`, "error"); }
}

el("kbCreate").addEventListener("click", async () => {
  const title = el("kbNewTitle").value.trim();
  const content = el("kbNewContent").value.trim();
  const msg = el("kbCreateMsg");
  if (!title || !content) { msg.textContent = "标题与内容不能为空"; return; }
  msg.textContent = "保存中…";
  try {
    await api("/v1/kb/documents", {
      method: "POST", tenant: state.tenant,
      body: { kb_type: el("kbTypeNew").value, source_id: "MANUAL", title, content },
    });
    el("kbNewTitle").value = "";
    el("kbNewContent").value = "";
    msg.textContent = "";
    toast("文档已保存，立即可检索", "success");
    loadDocs();
  } catch (e) { msg.textContent = `保存失败：${e.message}`; }
});

el("kbRebuild").addEventListener("click", async () => {
  const ok = await confirmAction({
    title: "重建向量索引",
    message: "将按当前知识文档重新切片与向量化，期间检索仍可用旧索引。确认执行？",
    confirmText: "重建",
    danger: false,
  });
  if (!ok) return;
  toast("重建任务已提交…", "info");
  try {
    const d = await api("/v1/kb/rebuild", { method: "POST", tenant: state.tenant || undefined });
    toast(`重建完成：${d.stats.chunks} 个切片，去重跳过 ${d.stats.dedup_skipped || 0} 个`, "success");
    loadDocs();
  } catch (e) { toast(`重建失败：${e.message}`, "error"); }
});
