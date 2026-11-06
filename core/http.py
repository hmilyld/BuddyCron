"""HTTP 请求层：双码校验、分级退避、每账号时间预算、域名选择。

这一层只做三件事，别的什么都不管：**把请求发出去、把响应归一成 (status, body)、
按错误性质决定要不要重试**。业务语义一律留给上层。

三条来自 88lin 实测的硬约束（改动前请先读）：

1. **写操作默认不重试**。抽奖 / 领奖 / 兑换 / 补登若在服务端处理完成后才超时，
   重试就是重复提交。只有明确幂等的两个签到接口例外。
2. **退避要分级**。网络类失败（连不上、超时）用 (5, 15, 30, 60, 90) 秒——定时任务
   最容易撞上「刚开机 WiFi 还没连上」，5 秒后再试一次等于两次撞同一堵墙。
   服务端 5xx 只用 (3, 10) 秒：干等几分钟救不了它，反而把预算耗光。
3. **4xx 绝不重试**。业务规则或参数问题，重试一百次也是同一个答案。

域名选择是这一层里唯一的「猜」。同一个路径在 copilot.tencent.com /
www.codebuddy.cn / www.workbuddy.cn 三个 host 上都能通到鉴权网关，但三家参考
实现各用不同 host，静态代码没法判断谁对。所以这里按配置的 host 顺序逐个尝试，
成功一次就记住（见 HostRouter），结果落到 config.json。
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request

# 伪状态码：区分「没拿到响应」的两种原因，与官方 HTTP 码不冲突。
CODE_NO_NETWORK = -1  # 连不上 / 超时 / DNS 失败
CODE_BUDGET_OUT = -2  # 本次预算耗尽，请求没发出去

REQUEST_TIMEOUT = 30
NETWORK_RETRY_DELAYS = (5, 15, 30, 60, 90)
SERVER_RETRY_DELAYS = (3, 10)

# 账号 domain 命中这些值时才走对应 host；其余一律回落到默认。
# 白名单的意义：账号记录被篡改后不会把 Bearer token 发往攻击者主机。
ALLOWED_HOSTS = ("www.codebuddy.cn", "www.workbuddy.cn", "www.workbuddy.ai")

# 活动/成长中心接口的候选 host（按优先级）。doctor 会实测并收敛成一个。
ACTIVITY_HOSTS = ("www.workbuddy.cn", "www.codebuddy.cn")
# 签到 / 积分 / 插件接口的候选 host。
BILLING_HOSTS = ("www.codebuddy.cn", "www.workbuddy.cn")

USER_AGENT = "WorkBuddy"


class Budget:
    """一次运行的时间预算。

    存在的理由：systemd / cron 把跑超时的进程直接杀掉，而结果往往在最后才落盘，
    进程一死当天记录整条丢失。所以自设一个比任务时限更小的预算，保证总能走到
    写日志那一步。

    多账号时用 ``child()`` 给每个账号切一份配额 —— 否则第 1 个账号超时会把全局
    预算吃光，第 5 个永远轮不到。
    """

    def __init__(self, seconds: float):
        self.seconds = max(0.0, float(seconds))
        self.started = time.monotonic()

    def left(self) -> float:
        return self.seconds - (time.monotonic() - self.started)

    def expired(self) -> bool:
        return self.left() <= 1.0

    def child(self, seconds: float) -> "Budget":
        """从剩余预算里切出一份子预算，同时受全局剩余时间约束。"""
        return Budget(min(seconds, max(0.0, self.left())))


class HostRouter:
    """按候选顺序选 host，成功一次就锁定。

    不缓存到磁盘：host 的可达性可能因为出口 IP / 网络环境变化，每次运行重新
    探测一次的代价只是多一个请求，比记住一个过期结论安全。
    """

    def __init__(self, preferred: str = ""):
        self.preferred = preferred.strip()
        self._locked: dict[str, str] = {}

    def candidates(self, group: tuple[str, ...]) -> list[str]:
        out: list[str] = []
        if self.preferred and self.preferred in group:
            out.append(self.preferred)
        out.extend(item for item in group if item not in out)
        return out

    def lock(self, group_name: str, host: str) -> None:
        self._locked[group_name] = host

    def locked(self, group_name: str) -> str | None:
        return self._locked.get(group_name)


def build_headers(account: dict, token: str | None = None) -> dict[str, str]:
    """构造请求头。

    活动/成长中心接口用极简头（88lin 实测：UA + Bearer + X-User-Id 就够）。
    积分三件套额外需要 X-Client-Platform: web，否则网关当成未知客户端；
    该字段由调用方通过 ``extra`` 补，这里不写死——写死会让签到请求也带上它。
    """
    auth = account.get("auth") or {}
    info = account.get("account") or {}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": "Bearer %s" % (token or auth.get("accessToken") or ""),
    }
    uid = info.get("uid")
    if uid:
        headers["X-User-Id"] = uid
    enterprise = info.get("enterpriseId") or info.get("enterprise_id")
    if enterprise:
        headers["X-Enterprise-Id"] = enterprise
        headers["X-Tenant-Id"] = enterprise
    domain = auth.get("domain")
    if domain:
        headers["X-Domain"] = domain
    return headers


def _retry_delays(code: int) -> tuple[int, ...]:
    """该失败码对应的退避节奏；空元组表示「重试也没用，立刻如实返回」。"""
    if code == CODE_NO_NETWORK:
        return NETWORK_RETRY_DELAYS
    if code >= 500:
        return SERVER_RETRY_DELAYS
    return ()


def request(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    payload=None,
    budget: Budget | None = None,
    retry: bool = True,
    timeout: int = REQUEST_TIMEOUT,
) -> tuple[int, dict]:
    """发一次请求，返回 (status, body)。

    status 可能是官方 HTTP 码，也可能是 CODE_NO_NETWORK / CODE_BUDGET_OUT。
    body 永远是 dict：非 JSON 响应包成 {"raw": 前 500 字}，调用方不用自己判类型。
    """
    delays = _retry_delays  # 局部别名，便于单测替换
    body_bytes = None if payload is None else json.dumps(payload).encode("utf-8")
    context = ssl.create_default_context()

    attempt = 0
    while True:
        if budget is not None and budget.left() <= 1:
            return CODE_BUDGET_OUT, {"error": "已达本次运行时间预算，跳过剩余请求"}

        request_timeout = timeout
        if budget is not None:
            request_timeout = max(1, min(timeout, int(budget.left())))

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        status: int
        result: dict
        try:
            with urllib.request.urlopen(req, timeout=request_timeout, context=context) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = {"raw": raw[:500]}
                result = parsed if isinstance(parsed, dict) else {"data": parsed}
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            status = error.code
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"raw": raw[:500]}
            result = parsed if isinstance(parsed, dict) else {"data": parsed}
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", None) or str(error)
            status = CODE_NO_NETWORK
            result = {"error": str(reason)}

        if not retry:
            return status, result

        schedule = delays(status)
        if attempt >= len(schedule):
            return status, result
        wait = schedule[attempt]
        if budget is not None and budget.left() - wait <= 1:
            # 等下去就要超预算了：现在收手，把「没拿到结果」如实交给上层。
            return status, result
        time.sleep(wait)
        attempt += 1


def post(url, headers, payload=None, **kwargs) -> tuple[int, dict]:
    """POST 默认不重试。写操作若在服务端处理完成后才超时，重试会重复提交。"""
    return request(url, headers, method="POST", payload=payload, retry=False, **kwargs)


def post_retry(url, headers, payload=None, **kwargs) -> tuple[int, dict]:
    """仅供明确幂等的接口使用：签到状态查询、签到领取（服务端按天去重）。"""
    return request(url, headers, method="POST", payload=payload, retry=True, **kwargs)


def get(url, headers, **kwargs) -> tuple[int, dict]:
    return request(url, headers, method="GET", retry=True, **kwargs)


def api_success(status: int, body: dict) -> bool:
    """同时校验 HTTP 码与 JSON 业务码。

    只看 HTTP 200 会把业务失败当成功：``{"code": 10001, "msg": ...}`` 配 200 是常态。
    """
    if not (200 <= status < 300):
        return False
    code = body.get("code") if isinstance(body, dict) else None
    return code is None or (isinstance(code, int) and code == 0)


def dig(obj, key):
    """穿透 data/result/resp/response 信封找字段。

    上游字段可能被任意一层包裹，所以递归找（限深 8 层）。信封本身也可能是数组
    （``{"data": [{...}, {...}]}``），所以 dict 和 list 两种形态都要下钻。
    """
    return _dig(obj, key, 0)


def _dig(obj, key, depth: int):
    if depth > 8:
        return None
    if isinstance(obj, list):
        for item in obj:
            found = _dig(item, key, depth + 1)
            if found is not None:
                return found
        return None
    if not isinstance(obj, dict):
        return None
    if key in obj and obj[key] is not None:
        return obj[key]
    for wrapper in ("data", "result", "resp", "response"):
        inner = obj.get(wrapper)
        if isinstance(inner, (dict, list)):
            found = _dig(inner, key, depth + 1)
            if found is not None:
                return found
    return None


def as_int(value, default: int = 0) -> int:
    """安全转 int。OverflowError 也要吞：json.loads 接受 Infinity 字面量。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default


def is_unauthorized(status: int, body: dict) -> bool:
    """服务端明确的鉴权/权限拒绝。

    只认 401 / 403 和明确的 token 失效语义。**不要**用「消息里带 token 字样」
    兜底：任何带 token 的普通业务文案都会误触发刷新，而刷新一旦失败会把整轮
    中止，结果是一次无关报错就把当天签到弄没了。
    """
    if status in (401, 403):
        return True
    message = ""
    if isinstance(body, dict):
        message = str(body.get("msg") or body.get("message") or "").lower()
    english = (
        "unauthorized",
        "invalid_token",
        "invalid token",
        "token expired",
        "token is expired",
    )
    if any(needle in message for needle in english):
        return True
    subject = ("登录", "凭证", "鉴权", "授权")
    state = ("失效", "过期", "无效", "未登录")
    return any(word in message for word in subject) and any(
        word in message for word in state
    )


def describe(status: int, body: dict) -> str:
    """把响应归一成一句人话。网关返回 HTML 时截取片段。"""
    if status == CODE_NO_NETWORK:
        return "网络不可达（%s）" % (body.get("error") or "")
    if status == CODE_BUDGET_OUT:
        return "已达时间预算，请求跳过"
    if isinstance(body, dict):
        for key in ("msg", "message", "error_msg", "error"):
            text = body.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()[:200]
    return "HTTP %s" % status
