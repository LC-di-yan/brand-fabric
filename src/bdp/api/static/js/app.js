/** 应用入口：路由 / 导航 / 主题切换 / 初始化。 */
import { api, state, logout, IS_FILE } from "./api.js";
import { el, toast } from "./ui.js";
import { disposeAll } from "./charts.js";
import * as overview from "./views/overview.js";
import * as metrics from "./views/metrics.js";
import * as catalog from "./views/catalog.js";
import * as knowledge from "./views/knowledge.js";
import * as quality from "./views/quality.js";
import * as lineage from "./views/lineage.js";
import * as agents from "./views/agents.js";
import * as ask from "./views/ask.js";

const VIEWS = {
  overview: { title: "经营概览", mod: overview },
  metrics: { title: "指标查询", mod: metrics },
  catalog: { title: "商品主数据", mod: catalog },
  knowledge: { title: "知识检索", mod: knowledge },
  quality: { title: "质量与审计", mod: quality },
  lineage: { title: "数据血缘", mod: lineage },
  agents: { title: "Agent 编排", mod: agents },
  ask: { title: "智能问答", mod: ask },
};

let currentView = "overview";

function switchView(view) {
  if (!VIEWS[view]) view = "overview";
  const prev = VIEWS[currentView];
  if (prev?.mod.unload) prev.mod.unload();
  currentView = view;
  document.querySelectorAll("nav .item").forEach((n) =>
    n.classList.toggle("active", n.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) =>
    v.classList.toggle("active", v.dataset.view === view));
  el("viewTitle").textContent = VIEWS[view].title;
  history.replaceState(null, "", "#" + view);
  VIEWS[view].mod.load();
}

document.querySelectorAll("nav .item").forEach((n) => {
  n.addEventListener("click", () => switchView(n.dataset.view));
});
document.getElementById("logoutBtn").addEventListener("click", logout);

/* ---------------- 主题 ---------------- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("bdp_theme", theme);
  el("themeBtn").textContent = theme === "dark" ? "☀" : "☾";
  el("themeBtn").setAttribute("aria-label", theme === "dark" ? "切换到亮色" : "切换到暗色");
}

function toggleTheme() {
  disposeAll();
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  // 当前视图的图表用新主题重绘
  VIEWS[currentView].mod.onThemeChange?.();
}

/* ---------------- 初始化 ---------------- */

/** file:// 直接打开时的引导条：说明正确用法，服务在跑时本页仍可直接用。 */
function showFileHint() {
  const bar = document.createElement("div");
  bar.className = "note";
  bar.style.cssText = "margin:0;border-radius:0;border-width:0 0 1px 0;position:sticky;top:0;z-index:150";
  bar.innerHTML =
    "当前以本地文件方式打开。推荐用法：<code>python -m bdp.cli api</code> 后访问 " +
    '<a href="http://127.0.0.1:8000">http://127.0.0.1:8000</a>。' +
    "若服务已在 127.0.0.1:8000 运行，本页也可直接登录使用。" +
    '<button class="btn link" data-dismiss style="margin-left:auto">知道了</button>';
  bar.querySelector("[data-dismiss]").onclick = () => bar.remove();
  document.body.prepend(bar);
}

async function init() {
  if (IS_FILE) showFileHint();
  const u = state.user;
  el("whoami").textContent = `${u.username} · ${u.role}`;
  el("avatar").textContent = (u.username || "?").slice(0, 2).toUpperCase();

  applyTheme(localStorage.getItem("bdp_theme")
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  el("themeBtn").addEventListener("click", toggleTheme);

  const sel = el("tenantSel");
  try {
    const d = await api("/v1/catalog/tenants");
    const items = d.items || [];
    if (u.role === "brand") {
      sel.innerHTML = items.map((t) => `<option value="${t.tenant_id}">${t.name}</option>`).join("");
      sel.value = u.tenant_id;
      sel.disabled = true;
      state.tenant = u.tenant_id;
    } else {
      sel.innerHTML = `<option value="">平台全量</option>` +
        items.map((t) => `<option value="${t.tenant_id}">${t.name}</option>`).join("");
      sel.value = state.tenant || "";
    }
  } catch (e) {
    toast("加载品牌列表失败：" + e.message, "error");
  }
  el("scopeCrumb").textContent = `范围 ${state.tenant || "ALL"}`;
  sel.addEventListener("change", () => {
    state.tenant = sel.value;
    el("scopeCrumb").textContent = `范围 ${state.tenant || "ALL"}`;
    state.metricsList = []; // 租户切换后字典需重取
    switchView(currentView);
  });
  el("rangeSel").value = String(state.rangeDays);
  el("rangeSel").addEventListener("change", () => {
    state.rangeDays = parseInt(el("rangeSel").value, 10);
    switchView(currentView);
  });

  const startView = location.hash.replace("#", "") || "overview";
  switchView(VIEWS[startView] ? startView : "overview");
}

init();
