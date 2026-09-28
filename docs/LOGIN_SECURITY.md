# 登录安全设计（前后端）

> 对应代码：`security/{auth,captcha,ratelimit,audit}.py`、`api/{deps,routers/auth}.py`、
> `static/login.html`、`static/js/api.js`。技术栈：FastAPI + PyJWT + SQLAlchemy + 原生 JS，**零新增依赖**。

## 一、威胁模型与对策

| 威胁 | 对策 | 代码位置 | 可验证方式 |
|---|---|---|---|
| 口令泄露（拖库） | PBKDF2-HMAC-SHA256，每用户 16B 随机盐，**60 万迭代**（OWASP 2023）；非可逆 | `security/auth.py` | `hash_password` 输出格式 `pbkdf2_sha256$600000$salt$hash` |
| 存量弱哈希 | 哈希串自描述迭代次数；登录成功即透明升级（`needs_rehash`） | `routers/auth.py` | 测试 `test_password_hash_upgraded_on_login` |
| SQL 注入 | 全部查询走 SQLAlchemy 参数化；登录输入白名单字符集（`^[A-Za-z0-9_.\-]{1,48}$`） | `api/deps.py`、`schemas.py` | 含引号/控制字符的用户名直接 422 |
| XSS | 错误信息一律 `textContent` 写入；验证码 SVG 为服务端生成常量模板，不含用户输入；`novalidate` 表单无 HTML 注入面 | `login.html` | 用户名填 `<img src=x onerror=alert(1)>` → 被白名单拒绝 |
| 暴力破解 | 阶梯：3 次失败 → 强制验证码；5 次 → 锁定 15 分钟（锁定期正确密码也拒绝） | `security/{captcha,ratelimit}.py` | 测试 `test_login_lockout_and_recovery_hint`、`test_login_captcha_required_after_threshold` |
| 账号枚举 | 账号不存在与密码错误返回**字节级相同**的 401；不返回剩余尝试次数 | `routers/auth.py` `_UNIFIED_LOGIN_FAIL` | 测试 `test_login_failure_responses_are_indistinguishable` |
| CSRF | 双轨凭证：Bearer 天然免疫；Cookie 会话的状态变更强制 `X-CSRF-Token` 双提交 + Origin 同源校验 | `api/deps.py` `_csrf_ok` | 测试 `test_session_cookie_and_csrf` |
| 验证码绕过/重放 | HMAC 签名无状态令牌（服务端零会话存储）+ 一次性使用集合 + 5 分钟过期 | `security/captcha.py` | 测试 `test_captcha_single_use` |
| 会话劫持 | 会话 Cookie `HttpOnly + SameSite=Lax + Secure + Path=/ + Max-Age=JWT 有效期`；改密 `token_version+1` 使旧凭证（含 Cookie）立即失效 | `routers/auth.py` `_set_session_cookies` | 浏览器 DevTools 检查 Cookie 属性 |
| 撞库提速 | 前端提交节流：in-flight 锁 + 两次提交最小间隔 2s（服务端限流才是权威） | `login.html` | 连点登录按钮 |
| 日志泄密 | 访问日志只记 `method/path/status`，永不记请求体；审计记录用户名，绝不记密码 | `middleware.py`、`audit.py` | grep 日志无 password 字段 |

## 二、双轨凭证说明

```
浏览器登录页 ──POST /v1/auth/token──▶ 同时获得：
  ① Bearer JWT（sessionStorage，API 客户端/测试使用，不受 CSRF 影响）
  ② HttpOnly 会话 Cookie（bdp_session）+ 可读 CSRF Cookie（bdp_csrf）
     └─ 之后浏览器内的状态变更请求必须带 X-CSRF-Token: <与 bdp_csrf 一致>
```

`get_principal` 解析顺序：Authorization 头优先 → 其次会话 Cookie。
Cookie 路径的状态变更请求未通过 CSRF 校验一律 403。
**file:// 本地模式**（直接双击 HTML）无 Cookie 环境：使用 Bearer + 响应体中的 `csrf_token`，
API 指向 `http://127.0.0.1:8000`（CORS 已验证放行 null origin）。

## 三、审计事件

`auth.login`（成功/失败/锁定/验证码错误）、`auth.logout`、`auth.change_password`
均写入 `audit_log`，含用户名、目标租户、允许/拒绝与原因；可在「质量与审计」视图查看。

## 四、配置项（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BDP_COOKIE_SECURE` | `true` | 生产 HTTPS 必须 true；纯 IP 的 http 访问才需 false（localhost 属安全上下文，true 可用） |
| `BDP_LOGIN_CAPTCHA_THRESHOLD` | `3` | 达到该连续失败次数后要求验证码 |
| `BDP_LOGIN_MAX_FAILURES` / `BDP_LOGIN_LOCK_SECONDS` | `5` / `900` | 锁定阈值与时长 |
| `BDP_CAPTCHA_TTL_SECONDS` | `300` | 验证码有效期 |
| `BDP_JWT_SECRET` | 演示值 | 生产必须换长随机串（启动自检会检查） |

## 五、品牌素材与替换方式

| 素材 | 路径 | 替换方法 |
|---|---|---|
| 完整标识（图形+文字） | `static/brand/logo.svg` | 直接替换文件；页面引用路径不变 |
| 图形标 | `static/brand/logo-mark.svg` | 同上 |
| Favicon（SVG + 16/32/48 PNG + 180 apple-touch） | `static/favicon*.{svg,png}`、`static/apple-touch-icon.png` | 改 `scripts/gen_favicons.py` 的 `COLORS`/几何参数后 `python scripts/gen_favicons.py` 重新生成；或用设计工具导出同名 PNG 覆盖 |
| 品牌色 | `static/css/brand-baozun.css`（前端品牌层文件名） | 单文件覆盖层，改色阶即全站换肤 |
| 背景装饰 | `static/css/login.css`（渐变/网格/几何均为 CSS 自绘） | 无外部图片，无版权风险 |

所有素材为**自绘几何图形**（数据柱 + 折线 + 琥珀端点，寓意数据驱动增长），不引用任何第三方受版权保护资源。

## 六、已知边界（诚实记录）

- 限流与验证码一次性记录均为**进程内存**：多实例部署需换 Redis（代码注释已标注迁移点）。
- PBKDF2 满足当前安全基线；若合规要求 Argon2id，替换 `security/auth.py` 两个函数即可（需引入 argon2-cffi）。
- 前端节流只是体验层减速带，权威防线在服务端。
