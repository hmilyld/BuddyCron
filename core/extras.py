"""礼包与平台补偿：两份「白给的积分」。

这两个端点是从官方前端 bundle（index-CIb8jkEw.js）里挖出来的，两个参考仓库
都没有覆盖：

    POST /billing/meter/check-gift-claimed    查礼包是否已领
    POST /billing/meter/claim-gift            领礼包
    POST /billing/meter/compensation-status   查补偿状态
    POST /billing/meter/claim-compensation    领补偿

响应形态（从前端调用点读出）：
    check-gift-claimed   data.active + data.claimed
    compensation-status  data.active + data.claimed + data.end_time
    claim-gift           code == 0 表示成功

两者都是**不定期**的，没活动时 active=false 属于正常状态，不是故障。
注意前缀：挂在 /billing/meter 下、**不带 /v2**（与签到那组不是同一批路由）。
"""

from __future__ import annotations

from . import accounts, http

METER = "/billing/meter"


def _hosts(account: dict) -> list[str]:
    endpoint = (account.get("auth") or {}).get("endpoint") or ""
    host = endpoint.replace("https://", "").strip("/") if endpoint else ""
    out: list[str] = []
    if host in http.ALLOWED_HOSTS:
        out.append(host)
    out.extend(h for h in http.BILLING_HOSTS if h not in out)
    return out


def _call(account, budget, path: str) -> tuple[int, dict]:
    headers = http.build_headers(account)
    headers["X-Client-Platform"] = "web"
    last: tuple[int, dict] = (http.CODE_NO_NETWORK, {"error": "请求未发出"})
    for host in _hosts(account):
        status, body = http.post_retry(f"https://{host}{path}", headers, {}, budget=budget)
        if http.api_success(status, body) or http.is_unauthorized(status, body):
            return status, body
        last = (status, body)
    return last


def _status_flag(body: dict, key: str) -> bool:
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for source in (data, body):
        if key in source and source[key] is not None:
            return bool(source[key])
    return False


def _deadline(body: dict) -> str:
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for source in (data, body):
        for key in ("end_time", "endTime", "deadline"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:10]
    return ""


def run(account, budget) -> tuple[dict, bool]:
    """领礼包 + 领补偿。返回 (结果, 是否鉴权失效)。

    两项各自容错：一个失败不影响另一个。
    """
    parts: list[str] = []
    failed = 0
    earned = 0

    # --- 礼包 ---
    st, bd = _call(account, budget, f"{METER}/check-gift-claimed")
    if http.is_unauthorized(st, bd):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(st, bd):
        parts.append(f"查礼包失败：{http.describe(st, bd)}")
        failed += 1
    elif not _status_flag(bd, "active"):
        parts.append("无礼包活动")
    elif _status_flag(bd, "claimed"):
        parts.append("礼包已领")
    else:
        cs, cb = _call(account, budget, f"{METER}/claim-gift")
        if http.is_unauthorized(cs, cb):
            return {"state": "auth", "message": "登录态失效"}, True
        if http.api_success(cs, cb):
            parts.append("已领礼包")
            earned += http.as_int(http.dig(cb, "credit") or http.dig(cb, "credits"))
        else:
            parts.append(f"领礼包失败：{http.describe(cs, cb)}")
            failed += 1

    # --- 平台补偿 ---
    st, bd = _call(account, budget, f"{METER}/compensation-status")
    if http.is_unauthorized(st, bd):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(st, bd):
        parts.append(f"查补偿失败：{http.describe(st, bd)}")
        failed += 1
    elif not _status_flag(bd, "active"):
        parts.append("无补偿活动")
    elif _status_flag(bd, "claimed"):
        parts.append("补偿已领")
    else:
        deadline = _deadline(bd)
        cs, cb = _call(account, budget, f"{METER}/claim-compensation")
        if http.is_unauthorized(cs, cb):
            return {"state": "auth", "message": "登录态失效"}, True
        if http.api_success(cs, cb):
            parts.append("已领补偿" + (f"（{deadline} 前）" if deadline else ""))
            earned += http.as_int(http.dig(cb, "credit") or http.dig(cb, "credits"))
        else:
            parts.append(f"领补偿失败：{http.describe(cs, cb)}")
            failed += 1

    acted = sum(1 for part in parts if part.startswith("已领"))
    return (
        {
            "state": "ok" if acted else ("fail" if failed else "skip"),
            "message": "，".join(parts) if parts else "无可领取项",
            "credit": earned,
        },
        False,
    )
