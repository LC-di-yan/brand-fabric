/** 商品主数据视图：SKU 分页列表 + 覆盖率（进度条）+ 人工复核队列。 */
import { api, state, downloadText } from "../api.js";
import { el, esc, fmtNum, fmtPct, setErr, skeleton, emptyState, toast } from "../ui.js";

export async function load() {
  await Promise.all([loadProducts(), loadCoverage(), loadReview()]);
}

async function loadProducts() {
  const tbody = document.querySelector("#catTable tbody");
  skeleton(tbody, "table");
  try {
    const qs = new URLSearchParams({ limit: state.catPageSize, offset: state.catPage * state.catPageSize });
    if (state.catKeyword) qs.set("keyword", state.catKeyword);
    const d = await api("/v1/catalog/products?" + qs.toString(), { tenant: state.tenant });
    el("catTotal").textContent = `共 ${fmtNum(d.total)} 个 SKU`;
    tbody.innerHTML = d.items.map((p) => `<tr>
      <td class="mono muted">${esc(p.sku_id)}</td>
      <td>${esc(p.spu_name)}</td><td class="mono muted">${esc(p.barcode)}</td>
      <td class="muted">${esc(p.spec)}</td><td class="num">¥${p.list_price}</td>
      <td class="num">${p.platform_mapping_count}</td></tr>`).join("")
      || `<tr><td colspan="6">${emptyState("▦", "无商品", "调整关键词后重试")}</td></tr>`;
    el("catPageInfo").textContent = `第 ${state.catPage + 1} 页 / 共 ${Math.max(1, Math.ceil(d.total / state.catPageSize))} 页`;
    el("catPrev").disabled = state.catPage === 0;
    el("catNext").disabled = state.catPage + 1 >= Math.ceil(d.total / state.catPageSize);
  } catch (e) { setErr(tbody, e, loadProducts); }
}

el("catReload").addEventListener("click", () => {
  state.catPage = 0;
  state.catKeyword = el("catSearch").value.trim();
  loadProducts();
});
el("catSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") el("catReload").click(); });
el("catPrev").addEventListener("click", () => { if (state.catPage > 0) { state.catPage--; loadProducts(); } });
el("catNext").addEventListener("click", () => { state.catPage++; loadProducts(); });
el("catExport").addEventListener("click", exportProductsCsv);

async function loadCoverage() {
  const box = el("catCoverage");
  skeleton(box, "block");
  try {
    const d = await api("/v1/catalog/mapping/coverage", { tenant: state.tenant });
    box.innerHTML = `
      <div class="stat-pair">
        <div><div class="label">映射率</div><div class="value num">${fmtPct(d.match_rate)}</div></div>
        <div><div class="label">模糊匹配占比</div><div class="value num">${fmtPct(d.fuzzy_share)}</div></div>
      </div>
      ${progressRows(d)}
      <div class="hint" style="margin-top:10px">${esc(d.note)}</div>`;
  } catch (e) { setErr(box, e, loadCoverage); }
}

function progressRows(d) {
  return d.by_platform.map((p) => `
    <div class="kv-row">
      <span>${esc(p.platform)}</span>
      <span style="display:flex;align-items:center;gap:10px">
        <span class="num muted">${fmtNum(p.mapped)}/${fmtNum(p.orders)}</span>
        <span style="width:90px">${barWith(p.match_rate)}</span>
        <span class="num" style="width:56px;text-align:right">${fmtPct(p.match_rate, 1)}</span>
      </span>
    </div>`).join("");
}

function barWith(ratio) {
  const pct = Math.min(1, Math.max(0, ratio)) * 100;
  const level = ratio >= 0.99 ? "success" : ratio >= 0.95 ? "warning" : "danger";
  return `<div class="progress" style="height:6px"><i class="${level}" style="width:${pct}%"></i></div>`;
}

let reviewItems = [];
async function loadReview() {
  const box = el("catReview");
  skeleton(box, "rows", 2);
  try {
    const d = await api("/v1/catalog/mapping/review", { tenant: state.tenant });
    reviewItems = d.items || [];
    box.innerHTML = reviewItems.map((it, i) => `
      <div class="hit">
        <div class="head"><span><b class="mono">${esc(it.platform_sku_code)}</b> · ${esc(it.platform)}</span>
          <span class="badge warning">置信度 ${it.confidence}</span></div>
        <div class="text muted" style="font-size:var(--fs-xs)">${fmtNum(it.orders)} 笔订单 ｜ 候选：
          ${it.candidates.map((c) => `<span class="badge info" style="margin-right:4px">${esc(c.sku_id)} · ${c.score.toFixed(2)}</span>`).join("")}
        </div>
        <div style="margin-top:8px;display:flex;gap:8px">
          ${it.candidates.length
            ? `<button class="btn sm" data-verify="${i}">确认最佳候选</button>`
            : `<span class="hint">无可用候选，需人工补录</span>`}
        </div>
      </div>`).join("") || emptyState("✓", "没有待复核记录", "全部映射已确认，覆盖率 100%");
  } catch (e) { setErr(box, e, loadReview); }
}

el("catReview").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-verify]");
  if (!btn) return;
  const it = reviewItems[Number(btn.dataset.verify)];
  const cand = it?.candidates?.[0];
  if (!cand) return;
  btn.disabled = true;
  try {
    await api("/v1/catalog/mapping/verify", {
      method: "POST", tenant: state.tenant,
      body: { platform: it.platform, shop_id: it.shop_id, platform_sku_code: it.platform_sku_code,
              sku_id: cand.sku_id, note: "人工确认" },
    });
    toast(`已确认映射：${it.platform_sku_code} → ${cand.sku_id}`, "success");
    loadCoverage();
    loadReview();
  } catch (err) {
    btn.disabled = false;
    toast(`确认失败：${err.message}`, "error");
  }
});

async function exportProductsCsv() {
  try {
    const qs = new URLSearchParams({ limit: 500 });
    if (state.catKeyword) qs.set("keyword", state.catKeyword);
    const d = await api("/v1/catalog/products?" + qs.toString(), { tenant: state.tenant });
    const lines = ["sku_id,spu_name,barcode,spec,list_price,platform_mapping_count"];
    d.items.forEach((p) => lines.push(`${p.sku_id},"${p.spu_name.replace(/"/g, '""')}",${p.barcode},"${p.spec}",${p.list_price},${p.platform_mapping_count}`));
    downloadText("products.csv", lines.join("\n") + "\n");
    toast("商品主数据已导出", "success");
  } catch (e) { toast(`导出失败：${e.message}`, "error"); }
}
