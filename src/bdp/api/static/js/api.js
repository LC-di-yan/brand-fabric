/** 会话状态与 API 封装。
 *
 * state 是唯一的运行时状态容器（模块级单例）；
 * api() 统一处理鉴权头、租户头、401 跳转与错误规范化。
 *
 * 双模式访问：
 * - 经服务访问（http(s)）：API_BASE 为空，同源请求；
 * - 直接双击 HTML（file://）：资源是相对路径可以加载，但 API 需要
 *   指向本地服务（默认 http://127.0.0.1:8000，服务需已启动）。
 *   CORS 在 main.py 中为 allow_origins=["*"]，file:// 下可直接调用。
 */

export const IS_FILE = location.protocol === "file:";
export const API_BASE = IS_FILE ? "http://127.0.0.1:8000" : "";

export const state = {
  token: sessionStorage.getItem("bdp_token"),
  user: JSON.parse(sessionStorage.getItem("bdp_user") || "null"),
  tenant: null,
  rangeDays: 30,
  metricsList: [],
  metricDef: {},
  catPage: 0,
  catPageSize: 15,
  catKeyword: "",
};

if (!state.token || !state.user) {
  // file:// 下没有"站点根"，跳登录页（同目录相对路径）
  location.href = IS_FILE ? "login.html" : "/";
}
state.tenant = state.user?.tenant_id || "";

/** 统一请求入口。tenant 传 "" 则不带租户头（平台级）。
 *  状态变更请求自动附带 X-CSRF-Token（与登录时签发的 csrf 双提交匹配）；
 *  Bearer 凭证下服务端会跳过 CSRF 校验，此头仅为 Cookie 会话兜底，无害。
 */
export async function api(path, { method = "GET", body, tenant, raw } = {}) {
  const headers = { "Content-Type": "application/json", Authorization: "Bearer " + state.token };
  if (tenant !== undefined && tenant !== null && tenant !== "") headers["X-Tenant-Id"] = tenant;
  if (method !== "GET" && method !== "HEAD") {
    const csrf = sessionStorage.getItem("bdp_csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const res = await fetch(API_BASE + path, {
    method, headers,
    body: body ? JSON.stringify(body) : undefined,
    credentials: IS_FILE ? "omit" : "same-origin",
  });
  const text = await res.text();
  if (raw) return text;
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (res.status === 401) { logout(); throw new Error("凭证已失效，请重新登录"); }
  if (!res.ok) {
    const err = new Error(data.message || `HTTP ${res.status}`);
    err.code = data.code;
    err.detail = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

export async function logout() {
  // 通知服务端清除会话 Cookie（失败不阻塞本地登出）
  try {
    const csrf = sessionStorage.getItem("bdp_csrf");
    await fetch(API_BASE + "/v1/auth/logout", {
      method: "POST",
      headers: { Authorization: "Bearer " + state.token, ...(csrf ? { "X-CSRF-Token": csrf } : {}) },
      credentials: IS_FILE ? "omit" : "same-origin",
    });
  } catch { /* 网络异常也要完成本地登出 */ }
  sessionStorage.removeItem("bdp_token");
  sessionStorage.removeItem("bdp_user");
  sessionStorage.removeItem("bdp_csrf");
  location.href = IS_FILE ? "login.html" : "/";
}

/** 触发浏览器下载（CSV 带 BOM）。 */
export function downloadText(name, text) {
  const blob = new Blob(["\ufeff" + text], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}
