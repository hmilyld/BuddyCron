"""账号库、OAuth 独立授权、token 刷新。

为什么必须自己管 token（88lin 不管）：
    88lin 不刷新是「客户端替我刷」换来的前提。多账号意味着大部分账号不在客户端里
    登录，没人替它们刷新，access token 一过期就永久失联。所以这里必须自己刷新。

为什么必须走 OAuth 独立授权（不能从本机导入）：
    客户端同一时刻只能登录一个账号，所以「读客户端 auth 文件」天生只能拿一个。
    要凑够 N 个账号，只剩独立授权这条路。实测该流程今天仍然可用：
        POST /v2/plugin/auth/state?platform=workbuddy -> {state, authUrl}
        GET  /v2/plugin/auth/token?state=<state>      -> 11217 登录中 / code 0 + token

refresh token 是**单次轮换**的：刷一次旧的立刻作废。同一账号并发刷新时后到者必拿
12153，而且这一刷会把刚发下来的新凭证又作废一次。所以刷新必须串行，且进锁后先回读
账号库——如果库里的 refresh token 已经和手上不一样，说明别人刷过了，直接采用库里的。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import http, paths

PLUGIN_PREFIX = "/v2/plugin"
CODE_LOGIN_PENDING = 11217
LOGIN_SESSION_TIMEOUT = 600  # 与官方 state 存活时间一致
TOKEN_LIMIT = 32768
_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# 跨进程锁
# ---------------------------------------------------------------------------


@contextmanager
def file_lock(path: Path, blocking: bool = True):
    """基于 flock 的跨进程互斥。

    web 进程与 systemd timer 是**两个进程**，进程内的 threading.Lock 拦不住它们
    同时改写 accounts.json。所以用文件锁。
    """
    paths.ensure_private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ---------------------------------------------------------------------------
# 账号库
# ---------------------------------------------------------------------------


def load_accounts() -> list[dict]:
    data = paths.read_json(paths.accounts_file(), [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_accounts(accounts: list[dict]) -> None:
    text = json.dumps(accounts, ensure_ascii=False, indent=2)
    paths.write_private(paths.accounts_file(), text)


def _account_key(account: dict) -> tuple[str, str]:
    uid = (account.get("account") or {}).get("uid") or ""
    return (str(account.get("id") or ""), str(uid))


def upsert_account(account: dict) -> None:
    """按 id 或 uid 合并。同一账号重复授权只更新，不产生第二条记录。"""
    with file_lock(paths.lock_file()):
        accounts = load_accounts()
        target_id, target_uid = _account_key(account)
        found = None
        for existing in accounts:
            existing_id, existing_uid = _account_key(existing)
            if (target_id and existing_id == target_id) or (
                target_uid and existing_uid == target_uid
            ):
                found = existing
                break
        if found is None:
            accounts.append(account)
        else:
            merged = dict(found)
            for key, value in account.items():
                if value is not None:
                    merged[key] = value
            # auth 是整块替换：半合并会让 accessToken 和 refreshToken 来自两次刷新。
            if account.get("auth"):
                merged["auth"] = dict(account["auth"])
            if account.get("account"):
                merged["account"] = dict(account["account"])
            accounts[accounts.index(found)] = merged
        save_accounts(accounts)


def patch_account(account_id: str, patch: dict) -> None:
    """按 id 打补丁。只改传入的键，不碰其余字段。"""
    if not patch:
        return
    with file_lock(paths.lock_file()):
        accounts = load_accounts()
        changed = False
        for account in accounts:
            if str(account.get("id") or "") == account_id:
                account.update({k: v for k, v in patch.items() if v is not None})
                changed = True
                break
        if changed:
            save_accounts(accounts)


def remove_account(account_id: str) -> bool:
    with file_lock(paths.lock_file()):
        accounts = load_accounts()
        kept = [a for a in accounts if str(a.get("id") or "") != account_id]
        if len(kept) == len(accounts):
            return False
        save_accounts(kept)
        return True


def find_account(account_id: str) -> dict | None:
    for account in load_accounts():
        if str(account.get("id") or "") == account_id:
            return account
    return None


def public_view(account: dict) -> dict:
    """前端可见字段。刻意不含任何 token。"""
    auth = account.get("auth") or {}
    info = account.get("account") or {}
    return {
        "id": account.get("id"),
        "uid": info.get("uid"),
        "nickname": info.get("nickname") or info.get("uid"),
        "domain": auth.get("domain"),
        "hasRefreshToken": bool(auth.get("refreshToken")),
        "expiresAt": auth.get("expiresAt") or _expires_from_token(
            str(auth.get("accessToken") or "")
        ),
        "agreedAt": account.get("agreed_at"),
        "needsRelogin": bool(account.get("needs_relogin")),
        "needsReloginReason": account.get("needs_relogin_reason"),
        "lastCheckinDate": account.get("last_checkin_date"),
        "lastCheckinResult": account.get("last_checkin_result"),
        "lastCheckinMessage": account.get("last_checkin_message"),
        "lastCheckinAt": account.get("last_checkin_at"),
        "weekProgress": account.get("week_progress") or [],
        "checkinDates": account.get("checkin_dates") or [],
        "credits": account.get("credits"),
        "createdAt": account.get("createdAt"),
    }


def now_ms() -> int:
    return int(time.time() * 1000)


def today_local() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


# ---------------------------------------------------------------------------
# OAuth 独立授权
# ---------------------------------------------------------------------------


class LoginSession:
    """一次登录会话。同一时刻只允许一个，多个账号串行添加。"""

    def __init__(self, state: str, auth_url: str, host: str):
        self.state = state
        self.auth_url = auth_url
        self.host = host
        self.created_at = time.time()
        self.done = False
        self.error: str | None = None
        self.account: dict | None = None
        self.error_streak = 0

    @property
    def expired(self) -> bool:
        return time.time() > self.created_at + LOGIN_SESSION_TIMEOUT

    def snapshot(self) -> dict:
        return {
            "done": self.done,
            "status": "error" if (self.done and self.error) else ("ok" if self.done else "pending"),
            "authUrl": self.auth_url,
            "error": self.error,
            "account": public_view(self.account) if self.account else None,
            "expiresIn": max(0, int(LOGIN_SESSION_TIMEOUT - (time.time() - self.created_at))),
        }


def start_login(router: http.HostRouter, budget: http.Budget) -> LoginSession:
    """向官方申请一个 state，拿到交给浏览器的授权地址。"""
    last_error = "申请登录 state 失败"
    for host in router.candidates(http.BILLING_HOSTS):
        url = f"https://{host}{PLUGIN_PREFIX}/auth/state?platform=workbuddy"
        status, body = http.post_retry(url, {"Accept": "application/json", "Content-Type": "application/json"}, {}, budget=budget)
        if not http.api_success(status, body):
            last_error = f"{host}: {http.describe(status, body)}"
            continue
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        state = data.get("state")
        auth_url = data.get("authUrl") or data.get("auth_url") or data.get("url")
        if not state or not auth_url:
            last_error = f"{host}: 响应缺少 state / authUrl"
            continue
        if not _allowed_login_url(auth_url):
            raise RuntimeError("官方返回的登录地址不在白名单内，已拒绝使用")
        router.lock("billing", host)
        return LoginSession(state, auth_url, host)
    raise RuntimeError(last_error)


def _allowed_login_url(url: str) -> bool:
    """交给浏览器打开的地址必须是 https 且落在官方域名白名单内。

    否则官方响应一旦被篡改就能把用户导向钓鱼页。
    """
    if not url.startswith("https://"):
        return False
    rest = url.split("://", 1)[1]
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    host = authority.rsplit("@", 1)[-1].split(":")[0].strip().lower()
    return host in http.ALLOWED_HOSTS


def poll_login(session: LoginSession, budget: http.Budget) -> dict:
    """轮询一次。未完成返回 pending，完成则落库并返回账号视图。"""
    if session.done:
        return session.snapshot()
    if session.expired:
        session.done = True
        session.error = "登录超时，请重新发起"
        return session.snapshot()

    url = f"https://{session.host}{PLUGIN_PREFIX}/auth/token?state={session.state}"
    status, body = http.get(url, {"Accept": "application/json"}, budget=budget)

    if http.api_success(status, body):
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            return session.snapshot()
        account = _build_account(data, session.state, session.host, budget)
        upsert_account(account)
        patch_account(str(account["id"]), {"needs_relogin": False, "needs_relogin_reason": None})
        session.done = True
        session.account = account
        return session.snapshot()

    code = body.get("code") if isinstance(body, dict) else None
    message = str((body or {}).get("msg") or "")
    if code == CODE_LOGIN_PENDING or "login ing" in message.lower():
        return session.snapshot()

    # 既不是成功也不是「登录中」：可能是网关抖动，也可能是 state 已废。
    # 连续多次才判定失败，偶发抖动不该打断扫码。
    session.error_streak += 1
    if session.error_streak >= 10:
        session.done = True
        session.error = f"官方返回 code={code} {message}"
    return session.snapshot()


def _fetch_profile(state: str, token: str, host: str, budget: http.Budget) -> dict:
    """拉账号资料。失败不阻断登录——token 才是必需的，资料只是让列表更好认。"""
    url = f"https://{host}{PLUGIN_PREFIX}/login/account?state={state}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    status, body = http.get(url, headers, budget=budget)
    if not http.api_success(status, body):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _build_account(data: dict, state: str, host: str, budget: http.Budget) -> dict:
    access_token = data.get("accessToken") or data.get("access_token") or ""
    refresh_token = data.get("refreshToken") or data.get("refresh_token") or ""
    domain = data.get("domain")
    if domain not in http.ALLOWED_HOSTS:
        domain = host if host in http.ALLOWED_HOSTS else http.ALLOWED_HOSTS[0]

    profile = _fetch_profile(state, access_token, host, budget)
    uid = str(profile.get("uid") or data.get("uid") or "").strip() or (
        _jwt_claim(access_token, "sub") or ""
    )
    nickname = profile.get("nickname") or data.get("nickname") or _jwt_claim(
        access_token, "nickname"
    )
    enterprise = profile.get("enterpriseId") or profile.get("enterprise_id") or data.get("enterpriseId")

    # 过期时间优先用响应给的，没有就从 JWT 的 exp 解（实测响应里恒为 null）。
    expires_at = _norm_ts(data.get("expiresAt") or data.get("expires_at")) or _expires_from_token(
        access_token
    )

    return {
        "id": f"uid:{uid}" if uid else str(uuid.uuid4()),
        "auth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": expires_at,
            "refreshExpiresAt": _norm_ts(
                data.get("refreshExpiresAt") or data.get("refresh_expires_at")
            ),
            "domain": domain,
            "endpoint": f"https://{host}",
        },
        "account": {
            "uid": uid,
            "nickname": nickname,
            "enterpriseId": enterprise,
        },
        "createdAt": now_ms(),
        "agreed_at": None,
        "needs_relogin": False,
    }


def _uid_from_token(token: str) -> str:
    """资料接口偶发失败时从 JWT 的 sub 认人。

    否则 uid 为空会生成随机 id，同一个账号会被记成两条，独立凭证的迁移就乱了。
    只解不验签——这个字段本来就只用于显示和去重。
    """
    return _jwt_claim(token, "sub")


def _jwt_claim(token: str, key: str):
    """从 JWT payload 里读一个字段。只解码不验签。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    import base64
    import json as _json

    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        parsed = _json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get(key)


def _expires_from_token(token: str) -> int | None:
    """OAuth 响应不给 expiresAt，但 JWT 的 ``exp`` 就是它（实测 30 天）。

    不解析这个字段就只能靠 401 触发刷新——那意味着每次过期后第一个请求必然失败，
    多账号串行时每个账号都要白吃一次 401 再重试。
    """
    exp = _jwt_claim(token, "exp")
    return _norm_ts(exp)


def _norm_ts(value) -> int | None:
    """时间戳归一：秒级自动升为毫秒。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        raw = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return raw * 1000 if raw < 10_000_000_000 else raw


# ---------------------------------------------------------------------------
# token 刷新
# ---------------------------------------------------------------------------

_CODE_CHAIN_DEAD = (
    "12153",
    "invalid_grant",
    "refresh token is invalid",
    "doesn't have required client",
    "重新登录",
)


def chain_dead(message: str) -> bool:
    """refresh 链是否已被服务端轮换或作废。

    只认服务端明确的凭证失效语义——网络错误不该触发「重新登录」提示。
    """
    text = (message or "").lower()
    return any(needle in text for needle in _CODE_CHAIN_DEAD)


def can_refresh(account: dict) -> bool:
    return bool((account.get("auth") or {}).get("refreshToken"))


def refresh_account(account: dict, budget: http.Budget) -> dict:
    """串行 + 互认地刷新一次。

    进锁后先回读账号库：如果库里那条 refresh token 已经和手上不一样，说明别人
    （另一条链路、或上一次自己）已经刷过，直接采用库里的凭证，不再二次刷新 ——
    否则这一刷就是把刚发下来的新凭证又作废一次。

    返回新的 auth 块。失败抛 RuntimeError（消息已脱敏，不含 token）。
    """
    account_id = str(account.get("id") or "")
    mine = str((account.get("auth") or {}).get("refreshToken") or "")
    if not mine:
        raise RuntimeError("缺少 refresh token，需重新登录")

    with file_lock(paths.lock_file()):
        if account_id:
            stored = find_account(account_id)
            if stored:
                theirs = str((stored.get("auth") or {}).get("refreshToken") or "")
                if theirs and theirs != mine:
                    patch = dict(stored.get("auth") or {})
                    if patch.get("accessToken"):
                        return patch

        patch = _do_refresh(account, mine, budget)
        if account_id:
            _write_auth(account_id, patch)
        return patch


def _do_refresh(account: dict, refresh_token: str, budget: http.Budget) -> dict:
    endpoint = (account.get("auth") or {}).get("endpoint") or ""
    host = endpoint.replace("https://", "").strip("/") if endpoint else ""
    hosts = [host] if host in http.ALLOWED_HOSTS else []
    hosts.extend(h for h in http.BILLING_HOSTS if h not in hosts)

    last_error = "刷新失败"
    for candidate in hosts:
        url = f"https://{candidate}{PLUGIN_PREFIX}/auth/token/refresh"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Refresh-Token": refresh_token,
        }
        status, body = http.post(url, headers, {}, budget=budget)
        if not http.api_success(status, body):
            last_error = http.describe(status, body)
            continue
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        access = data.get("accessToken") or data.get("access_token")
        if not access:
            last_error = "刷新响应缺少 accessToken"
            continue
        patch = {
            "accessToken": access,
            "expiresAt": _norm_ts(data.get("expiresAt") or data.get("expires_at"))
            or _expires_from_token(access),
        }
        new_refresh = data.get("refreshToken") or data.get("refresh_token")
        if new_refresh:
            patch["refreshToken"] = new_refresh
        patch["refreshExpiresAt"] = _norm_ts(
            data.get("refreshExpiresAt") or data.get("refresh_expires_at")
        )
        return {k: v for k, v in patch.items() if v is not None}
    raise RuntimeError(last_error)


def _write_auth(account_id: str, patch: dict) -> None:
    """刷新成功后立刻落盘，让同时排队的其他链路能命中短路分支。"""
    with file_lock(paths.lock_file()):
        accounts = load_accounts()
        for account in accounts:
            if str(account.get("id") or "") != account_id:
                continue
            auth = dict(account.get("auth") or {})
            auth.update(patch)
            account["auth"] = auth
            account.pop("needs_relogin", None)
            account.pop("needs_relogin_reason", None)
            save_accounts(accounts)
            return


def with_fresh_token(account: dict, budget: http.Budget) -> tuple[dict, dict | None]:
    """跑业务前先确认 token 还没过期；快过期就主动刷一次。

    返回 (可用的账号副本, 新 auth 块或 None)。主动刷新能避免每个请求都先吃一次 401。

    过期时间可能缺失（老记录、或 OAuth 响应没给），此时从 JWT 的 exp 补出来：
    不补就只能靠 401 触发刷新，多账号串行时每个账号都要白吃一次 401 再重试。
    """
    auth = dict(account.get("auth") or {})
    expires_at = auth.get("expiresAt") or _expires_from_token(
        str(auth.get("accessToken") or "")
    )
    needs = False
    if expires_at:
        try:
            needs = int(expires_at) - now_ms() < 60_000
        except (TypeError, ValueError):
            needs = False
    if not needs or not can_refresh(account):
        return account, None

    patch = refresh_account(account, budget)
    auth.update(patch)
    fresh = dict(account)
    fresh["auth"] = auth
    return fresh, patch


def mark_relogin(account_id: str, reason: str) -> None:
    patch_account(account_id, {"needs_relogin": True, "needs_relogin_reason": reason[:240]})
