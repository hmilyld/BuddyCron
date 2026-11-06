"""本地 Web 页：OAuth 登录的落点 + 账号列表 + 手动触发。

为什么需要它：OAuth 授权必须跳浏览器，而 state 轮询得有个地方接住。顺手就把
账号列表和手动触发放在这里，比单独做个 CLI 交互式输入好——登录本来就在浏览器里。

**不是控制台**，所以不引框架：标准库 http.server 加几百行 HTML 就够。

安全约束（本地服务最容易被忽略的地方）：
    1. 只绑 127.0.0.1，不监听 0.0.0.0 —— 同网段其他机器连不上。
    2. 校验 Host 头，防 DNS rebinding（攻击者把自己的域名解析到 127.0.0.1，
       再让浏览器访问，此时 Host 是他自己的域名）。
    3. 拒绝带 Origin 的跨站请求，POST 强制 application/json —— 这两个组合起来
       挡住了「恶意网页偷偷 POST 到 127.0.0.1:8787」这条路（表单提交发不出
       application/json，跨域 JSON 请求会触发预检而被拒）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import accounts, auth, credits, doctor, http, logs, paths, runner

WEB_DIR = Path(__file__).resolve().parent
DIST_DIR = WEB_DIR / "dist"
# 编译产物的白名单。只放行这几个文件 —— 不给任意路径读取，避免目录穿越。
ASSETS = {
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 4096
# 不需要登录就能访问的路径。登录页本身、它的资源、版本号（登录页要显示）、
# 以及健康检查。
PUBLIC_PATHS = {
    "/login",
    "/api/login",
    "/api/version",
    "/assets/app.css",
    "/favicon.svg",
    "/favicon.ico",
}

# 公网部署时由环境变量追加允许的 Host。反向代理（Caddy/Nginx）会把真实域名
# 写进 Host 头，所以这里必须能配上，否则请求全被 403。
_EXTRA_HOSTS = {
    item.strip().lower()
    for item in (os.environ.get("BUDDYCRON_HOSTS") or "").split(",")
    if item.strip()
}
# 公网模式下允许反代带过来的 X-Forwarded-For 作为失败计数 key，
# 否则所有请求都算在反代那一个 IP 上，锁一次就把自己锁死。
_TRUST_PROXY = (os.environ.get("BUDDYCRON_TRUST_PROXY") or "").strip() == "1"
# 公网部署必须开启：强制要求 HTTPS（或反代声明了 X-Forwarded-Proto=https）。
_REQUIRE_HTTPS = (os.environ.get("BUDDYCRON_REQUIRE_HTTPS") or "").strip() == "1"

# 同一时刻只允许一个登录会话：多个账号串行添加。
_login_lock = threading.Lock()
_login_session: accounts.LoginSession | None = None
_run_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "BuddyCron"

    # ------------------------------------------------------------------
    # 响应工具
    # ------------------------------------------------------------------

    def _send(self, code: int, payload, content_type: str = "application/json", extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else (
            payload.encode("utf-8") if isinstance(payload, str) else
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._send(code, {"ok": False, "error": message})

    def _client(self) -> str:
        """失败计数用的客户端标识。

        反向代理下所有请求的来源 IP 都是代理那一个，直接用它做 key 会让
        「别人试错」把你一起锁死。所以信任代理时要取 X-Forwarded-For 的第一段
        （最靠近客户端的那个）。这个值只在计数用，不参与任何鉴权判断，
        伪造它最多让自己多试几次 —— 而真正的门禁是密码本身。
        """
        if _TRUST_PROXY:
            forwarded = self.headers.get("X-Forwarded-For") or ""
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        return str(self.client_address[0]) if self.client_address else "unknown"

    def _session_cookie(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == auth.COOKIE_NAME:
                return value
        return None

    def _logged_in(self) -> bool:
        return auth.check_cookie(self._session_cookie())

    # ------------------------------------------------------------------
    # 防护
    # ------------------------------------------------------------------

    def _host_ok(self) -> bool:
        raw = (self.headers.get("Host") or "").strip()
        host = raw.rsplit(":", 1)[0] if ":" in raw and not raw.endswith("]") else raw
        return host.lower() in ALLOWED_HOSTS or host.lower() in _EXTRA_HOSTS

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # 同源导航 / 本地直接打开，不带 Origin
        parsed = urllib.parse.urlsplit(origin)
        host = (parsed.hostname or "").lower()
        return host in ALLOWED_HOSTS or host in _EXTRA_HOSTS

    def _https_ok(self) -> bool:
        """公网模式强制 HTTPS。

        明文 HTTP 会把登录口令和所有 API 请求（含 token）暴露在链路上，
        所以一旦开了 BUDDYCRON_REQUIRE_HTTPS，非 https 的请求一律拒绝。
        反向代理终止 TLS 时靠 X-Forwarded-Proto 判定，直接访问则看 socket。
        """
        if not _REQUIRE_HTTPS:
            return True
        proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
        if proto:
            return proto == "https"
        # 没有反代头：只在本地回环上放行（本机调试用），其余一律要求 TLS。
        return self._client() in ("127.0.0.1", "::1")

    def _guard(self) -> bool:
        if not self._https_ok():
            self._error(403, "必须通过 HTTPS 访问")
            return False
        if not self._host_ok():
            self._error(403, "Host 不在白名单内")
            return False
        if not self._origin_ok():
            self._error(403, "跨站请求已拒绝")
            return False
        return True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if not self._guard():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in PUBLIC_PATHS:
            if path == "/login":
                return self._serve_login()
            if path in ASSETS:
                return self._serve_asset(path)
            if path == "/favicon.ico":
                # 浏览器和老工具会盲猜 /favicon.ico。重定向到 svg，
                # 比在那里放一个 404 干净 —— 有些客户端会把 404 记进日志。
                return self._redirect("/favicon.svg")
            if path == "/api/version":
                return self._version()
            return self._error(404, "未知路径")

        if not self._logged_in():
            # 页面请求就跳登录页；API 请求返回 401 让前端处理。
            if path.startswith("/api/"):
                return self._error(401, "未登录")
            return self._redirect("/login")

        if path in ("/", "/index.html"):
            return self._serve_index()
        if path in ASSETS:
            return self._serve_asset(path)
        if path == "/api/accounts":
            return self._send(200, {"accounts": [accounts.public_view(a) for a in accounts.load_accounts()]})
        if path == "/api/state":
            return self._send(200, runner.read_state())
        if path == "/api/version":
            return self._version()
        if path == "/api/logs":
            limit = (query.get("limit") or ["50"])[0]
            month = (query.get("month") or [""])[0]
            try:
                limit_num = max(1, min(500, int(limit)))
            except ValueError:
                limit_num = 50
            return self._send(
                200,
                {
                    "records": logs.tail(limit_num, month or None),
                    "months": logs.months(),
                    "current": month or time.strftime("%Y-%m", time.localtime()),
                },
            )
        if path == "/api/login/poll":
            login_id = (query.get("loginId") or [""])[0]
            return self._send(200, self._poll_login(login_id))
        if path == "/api/doctor":
            account_id = (query.get("id") or [""])[0]
            return self._send(200, self._doctor(account_id))
        return self._error(404, "未知路径")

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type != "application/json":
            return self._error(415, "只接受 application/json")
        path = urllib.parse.urlsplit(self.path).path
        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as error:
            return self._error(400, f"请求体无效：{error}")

        # 登录是唯一不需要会话的 POST。
        if path == "/api/login":
            return self._handle_login(payload)

        if not self._logged_in():
            return self._error(401, "未登录")

        if path == "/api/login/start":
            return self._send(200, self._start_login())
        if path == "/api/run":
            mode = str(payload.get("mode") or "daily")
            return self._send(200, self._run(mode))
        if path == "/api/accounts/delete":
            account_id = str(payload.get("id") or "")
            ok = accounts.remove_account(account_id)
            return self._send(200, {"ok": ok})
        return self._error(404, "未知路径")

    # ------------------------------------------------------------------
    # 业务
    # ------------------------------------------------------------------

    def _serve_index(self) -> None:
        index = DIST_DIR / "index.html"
        if not index.exists():
            index = WEB_DIR / "index.html"
        if not index.exists():
            return self._error(
                500,
                "界面未构建。先跑 python3 web/build.py（需要 Node），"
                "或使用仓库里已提交的 web/dist。",
            )
        self._send(200, index.read_bytes(), "text/html; charset=utf-8")

    def _version(self) -> None:
        """版本号。登录前也要能读 —— 登录页会显示它。"""
        from core.version import VERSION

        self._send(200, {"version": VERSION, "display": f"v{VERSION}"})

    def _serve_login(self) -> None:
        page = DIST_DIR / "login.html"
        if not page.exists():
            return self._error(
                500,
                "登录页未构建。先跑 python3 web/build.py。",
            )
        self._send(200, page.read_bytes(), "text/html; charset=utf-8")

    def _serve_asset(self, path: str) -> None:
        entry = ASSETS.get(path)
        if entry is None:
            return self._error(404, "未知资源")
        name, content_type = entry
        target = DIST_DIR / name
        if not target.exists():
            return self._error(404, f"资源缺失：{name}")
        self._send(200, target.read_bytes(), content_type)

    def _redirect(self, location: str) -> None:
        self._send(302, b"", "text/plain", {"Location": location})

    def _handle_login(self, payload: dict) -> None:
        client = self._client()
        wait = auth.locked(client)
        if wait:
            return self._error(429, f"尝试次数过多，请 {wait} 秒后再试")

        password = str(payload.get("password") or "")
        if not password:
            return self._error(400, "请输入密码")
        if not auth.verify(password):
            auth.note_failure(client)
            left = max(0, auth.MAX_FAILS - _fail_count(client))
            return self._error(401, f"密码不正确（还可尝试 {left} 次）")

        auth.clear_failures(client)
        # 首次登录成功即清除明文口令 —— 从此刻起磁盘上只剩散列。
        auth.clear_password_hint()
        cookie = auth.issue_cookie()
        # 公网模式下必须带 Secure：否则浏览器可能把 cookie 发到明文请求上。
        secure = "; Secure" if _REQUIRE_HTTPS else ""
        self._send(
            200,
            {"ok": True},
            extra={
                "Set-Cookie": (
                    f"{auth.COOKIE_NAME}={cookie}; Path=/; HttpOnly; "
                    f"SameSite=Strict; Max-Age={auth.SESSION_SECONDS}{secure}"
                )
            },
        )

    def _start_login(self) -> dict:
        global _login_session
        with _login_lock:
            # 内存里没有就尝试从磁盘恢复 —— 容器重启后用户可能还停在登录页。
            if _login_session is None:
                _login_session = accounts.load_login_session()
            if _login_session and not _login_session.done and not _login_session.expired:
                return {
                    "ok": True,
                    "loginId": _login_session.state,
                    "authUrl": _login_session.auth_url,
                    "expiresIn": _login_session.snapshot()["expiresIn"],
                }
            budget = http.Budget(120)
            try:
                session = accounts.start_login(http.HostRouter(), budget)
            except RuntimeError as error:
                return {"ok": False, "error": str(error)}
            _login_session = session
            accounts.save_login_session(session)
            return {
                "ok": True,
                "loginId": session.state,
                "authUrl": session.auth_url,
                "expiresIn": session.snapshot()["expiresIn"],
            }

    def _poll_login(self, login_id: str) -> dict:
        global _login_session
        with _login_lock:
            # 会话标识用 state（不是内存地址）：进程重启后地址会变，
            # 而 state 是官方发的、唯一且已落盘，前端手里的值仍然对得上。
            if _login_session is None or _login_session.state != login_id:
                _login_session = accounts.load_login_session()
            session = _login_session
            if session is None or session.state != login_id:
                return {"ok": False, "error": "登录会话不存在或已取消"}
            budget = http.Budget(60)
            try:
                snapshot = accounts.poll_login(session, budget)
            except Exception as error:  # noqa: BLE001 - 轮询失败不该把服务弄挂
                return {"ok": False, "error": f"{type(error).__name__}: {error}"}
            # 完成或失败都清掉落盘文件；还在等就更新一次（error_streak 会变）。
            if session.done:
                accounts.clear_login_session()
            else:
                accounts.save_login_session(session)
            return {"ok": True, **snapshot}

    def _doctor(self, account_id: str) -> dict:
        picked = accounts.load_accounts()
        if account_id:
            picked = [a for a in picked if str(a.get("id")) == account_id]
        if not picked:
            return {"ok": False, "error": "没有账号"}
        budget = http.Budget(runner.DAILY_BUDGET)
        report = doctor.probe_account(picked[0], budget)
        return {"ok": True, "report": report, "text": doctor.render(report)}

    def _run(self, mode: str) -> dict:
        if mode not in ("daily", "poll", "checkin"):
            return {"ok": False, "error": "mode 只能是 daily / poll / checkin"}
        if not _run_lock.acquire(blocking=False):
            return {"ok": False, "error": "另一轮正在运行，稍后再试"}
        try:
            summary = runner.run_cycle(mode)
            runner.append_log(summary)
            return {"ok": True, **summary}
        finally:
            _run_lock.release()

    # 默认日志会打 stdout，systemd 下进 journald。保留默认行为即可。
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return


def _fail_count(client: str) -> int:
    entry = auth._fails.get(client)
    return entry[0] if entry else 0


def serve(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> None:
    # 默认只绑回环。要对外提供服务必须显式指定 --host 并配好反代与 HTTPS ——
    # 不做「自动监听 0.0.0.0」这种事，那会让一个没配 TLS 的服务直接暴露出去。
    public = host not in ("127.0.0.1", "localhost", "::1")
    # 容器里绑 0.0.0.0 是正常的：暴露面由 docker 的端口映射控制
    # （-p 127.0.0.1:8787:8787 只映射到宿主机回环）。这种场景由
    # BUDDYCRON_IN_CONTAINER=1 显式声明，避免把「容器内 0.0.0.0」和
    # 「宿主机 0.0.0.0」混为一谈。
    in_container = (os.environ.get("BUDDYCRON_IN_CONTAINER") or "").strip() == "1"
    if public and not _REQUIRE_HTTPS and not in_container:
        raise SystemExit(
            "绑定到非回环地址时必须同时设置 BUDDYCRON_REQUIRE_HTTPS=1，\n"
            "否则登录口令会以明文经过网络。正确做法是让 Caddy/Nginx 终止 TLS，\n"
            "本服务只监听 127.0.0.1，由反代转发过来。\n"
            "容器部署请设 BUDDYCRON_IN_CONTAINER=1 并把端口映射到宿主机回环。\n"
            "详见 deploy/PUBLIC.md。"
        )
    paths.ensure_private_dir(paths.data_dir())

    config = auth.ensure_config()
    if config.get("from_env"):
        # 密码来自环境变量：不打印明文（那是你自己填的），但要说明来源，
        # 免得有人翻遍日志找不到「登录密码」那一行。
        print("登录密码来自环境变量 BUDDYCRON_PASSWORD。")
        sys.stdout.flush()
    else:
        hint = auth.current_password_hint()
        if hint:
            print()
            print("=" * 52)
            print(f"  登录密码：{hint}")
            print("  已写入 config.json，登录成功一次后自动清除明文。")
            print("  忘了密码：python3 cli.py passwd --show")
            print("=" * 52)
            print()
            sys.stdout.flush()

    server = ThreadingHTTPServer((host, port), Handler)
    scheme = "https" if _REQUIRE_HTTPS and public else "http"
    url = f"{scheme}://{host}:{port}/"
    print(f"BuddyCron Web 页：{url}")
    print(f"数据目录：{paths.data_dir()}")
    if _EXTRA_HOSTS:
        print(f"允许的 Host：{', '.join(sorted(_EXTRA_HOSTS))}")
    sys.stdout.flush()
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
