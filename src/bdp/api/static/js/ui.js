/** UI 基座：格式化、骨架屏、空态、错误框、toast、modal、关键词高亮。 */

export const el = (id) => document.getElementById(id);

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

export const fmtMoney = (v) => "¥" + (v / 10000).toLocaleString("zh-CN", { maximumFractionDigits: 1 }) + " 万";
export const fmtNum = (v) => (v || 0).toLocaleString("zh-CN");
export const fmtPct = (v, digits = 2) => (v * 100).toFixed(digits) + "%";
export const fmtDelta = (v) =>
  v === null || v === undefined ? "" :
  `<span class="delta ${v >= 0 ? "positive" : "negative"}">${v >= 0 ? "▲" : "▼"} ${(v * 100).toFixed(1)}%</span>`;

export const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

/** 检索关键词高亮：先转义再包 <mark>，token 优先长词。 */
export function highlight(text, query) {
  const safe = esc(text);
  if (!query) return safe;
  const tokens = [...new Set(
    query.split(/\s+/).filter((w) => w.length >= 2).flatMap((w) => {
      const out = [w];
      // 中文按 2-gram 拆分，命中率更高
      const cjk = w.match(/[\u4e00-\u9fff]{2,}/);
      if (cjk) {
        const s = cjk[0];
        for (let i = 0; i + 2 <= s.length; i++) out.push(s.slice(i, i + 2));
      }
      return out;
    })
  )].sort((a, b) => b.length - a.length)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!tokens.length) return safe;
  return safe.replace(new RegExp(`(${tokens.join("|")})`, "g"), "<mark>$1</mark>");
}

/* ---------------- 骨架屏 / 空态 / 错误 ---------------- */

export function skeleton(box, kind = "rows", n = 4) {
  const target = typeof box === "string" ? el(box) : box;
  if (!target) return;
  if (kind === "rows") {
    target.innerHTML = Array.from({ length: n }, () => `<div class="skeleton row"></div>`).join("");
  } else if (kind === "block") {
    target.innerHTML = `<div class="skeleton block"></div>`;
  } else if (kind === "table") {
    target.innerHTML = `<tr>${Array.from({ length: n }, () => `<td><div class="skeleton row" style="margin:0"></div></td>`).join("")}</tr>`;
  }
}

export function emptyState(icon = "◌", title = "暂无数据", desc = "") {
  return `<div class="empty-state"><div class="icon">${icon}</div>
    <div class="title">${esc(title)}</div>${desc ? `<div class="desc">${esc(desc)}</div>` : ""}</div>`;
}

/** 错误渲染：文案 + 可选重试按钮（retry 传重试函数）。 */
export function setErr(container, err, retry) {
  const box = typeof container === "string" ? el(container) : container;
  if (!box) return;
  const msg = esc(err?.message || err);
  const retryBtn = retry
    ? `<button class="btn link" data-retry>重试</button>` : "";
  box.innerHTML = `<div class="errbox"><span>${msg}${err?.status ? `（HTTP ${err.status}）` : ""}</span>${retryBtn}</div>`;
  if (retry) box.querySelector("[data-retry]").onclick = () => retry();
}

/* ---------------- toast ---------------- */

let toastRegion;
export function toast(message, level = "info", { duration = 3600 } = {}) {
  if (!toastRegion) {
    toastRegion = document.createElement("div");
    toastRegion.className = "toast-region";
    toastRegion.setAttribute("aria-live", "polite");
    document.body.appendChild(toastRegion);
  }
  const icons = { success: "✓", error: "✕", warning: "!", info: "ℹ" };
  const item = document.createElement("div");
  item.className = `toast ${level}`;
  item.innerHTML = `<span>${icons[level] || icons.info}</span><span class="msg">${esc(message)}</span>
    <button class="close" aria-label="关闭">×</button>`;
  const dismiss = () => {
    item.classList.add("leaving");
    setTimeout(() => item.remove(), 240);
  };
  item.querySelector(".close").onclick = dismiss;
  toastRegion.appendChild(item);
  if (duration) setTimeout(dismiss, duration);
  return dismiss;
}

/* ---------------- modal ---------------- */

/**
 * 打开模态框。content 为 body 内 HTML；返回 { close, root }。
 * options.danger 时确认按钮为 danger 样式；onConfirm 返回 promise，
 * resolve 后自动关闭，reject 则保持打开（调用方自行 toast 错误）。
 */
export function openModal({ title, content, confirmText = "确认", cancelText = "取消",
                            danger = false, onConfirm, wide = false }) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <header>${esc(title)}<button class="btn link" data-close aria-label="关闭">×</button></header>
      <div class="body">${content}</div>
      ${onConfirm || confirmText ? `<footer>
        <button class="btn" data-cancel>${esc(cancelText)}</button>
        ${onConfirm ? `<button class="btn ${danger ? "danger" : "primary"}" data-confirm>${esc(confirmText)}</button>` : ""}
      </footer>` : ""}
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  overlay.querySelector("[data-close]").onclick = close;
  const cancelBtn = overlay.querySelector("[data-cancel]");
  if (cancelBtn) cancelBtn.onclick = close;
  const confirmBtn = overlay.querySelector("[data-confirm]");
  if (confirmBtn && onConfirm) {
    confirmBtn.onclick = async () => {
      confirmBtn.disabled = true;
      try {
        const keepOpen = await onConfirm(overlay) === false;
        if (!keepOpen) close();
      } catch (e) {
        toast(e.message || "操作失败", "error");
        confirmBtn.disabled = false;
      }
    };
  }
  document.addEventListener("keydown", function onKey(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", onKey); }
  });
  const focusable = overlay.querySelector("input, select, textarea, button:not([data-close])");
  if (focusable) focusable.focus();
  return { close, root: overlay };
}

/** 危险操作确认（替代 window.confirm）。 */
export function confirmAction({ title, message, confirmText = "确认执行", danger = true }) {
  return new Promise((resolve) => {
    openModal({
      title, content: `<p>${esc(message)}</p>`, confirmText, danger,
      onConfirm: () => resolve(true),
      cancelText: "取消",
    });
    // 取消 / 关闭 / ESC 都视为不确认
    setTimeout(() => {
      document.addEventListener("click", function once(e) {
        const cancel = e.target.closest?.("[data-cancel], [data-close], .modal-overlay");
        if (cancel) { resolve(false); document.removeEventListener("click", once); }
      }, true);
    }, 0);
  });
}

/* ---------------- 进度条 ---------------- */

export function progressBar(ratio, level) {
  const pct = Math.max(0, Math.min(1, ratio)) * 100;
  const auto = ratio >= 0.99 ? "success" : ratio >= 0.95 ? "warning" : "danger";
  return `<div class="progress" title="${pct.toFixed(3)}%"><i class="${level || auto}" style="width:${pct}%"></i></div>`;
}
