"""积分查询：余额、分组明细、包级到期时间、订阅类型。

三个接口（注意前缀**不带 /v2**，与签到那组不是同一批路由）：

    POST /billing/meter/get-user-resource-summary          余额 + 按用途分组
    POST /billing/meter/get-user-resource-free-packages    免费包的到期时间
    POST /billing/meter/get-user-resource-paid-packages    付费包的到期时间

summary 只有聚合数字，**没有到期时间**；要回答「这笔积分什么时候过期」必须查
packages 接口，而且 `PackageCodes` 是必填（缺省直接返回
`PackageCodes required`）。商品码写死在官方套餐配置里，不是可选筛选条件。

`CycleEndTime` 是「本周期结束」，也就是这笔额度失效的时刻；`ExpiredTime`
多数时候为空，所以取 CycleEndTime 为准。

只读操作，不消耗积分，可以随时查。
"""

from __future__ import annotations

import datetime as dt

from . import accounts, http

SUMMARY = "/billing/meter/get-user-resource-summary"
FREE = "/billing/meter/get-user-resource-free-packages"
PAID = "/billing/meter/get-user-resource-paid-packages"

# 官方套餐配置里的商品码。缺省会直接返回 PackageCodes required。
PAID_CODES = (
    "TCACA_code_002_AkiJS3ZHF5",
    "TCACA_code_023_4xbGhMrE6q",
    "TCACA_code_026_BaESVICNoi",
    "TCACA_code_027_0FCGVA6vSa",
    "TCACA_code_009_0XmEQc2xOf",
    "TCACA_code_038_OhvqZtiPKr",
)
FREE_CODES = (
    "TCACA_code_008_cfWoLwvjU4",
    "TCACA_code_007_nzdH5h4Nl0",
    "TCACA_code_028_NtpWi0jzXs",
    "TCACA_code_029_6wCGEWquYy",
    "TCACA_code_030_BjSt89qTvr",
)
# 包级明细最多保留多少条。一个账号的积分包通常个位数，留 40 足够。
MAX_PACKAGES = 40


def _hosts(account: dict) -> list[str]:
    endpoint = (account.get("auth") or {}).get("endpoint") or ""
    host = endpoint.replace("https://", "").strip("/") if endpoint else ""
    out: list[str] = []
    if host in http.ALLOWED_HOSTS:
        out.append(host)
    out.extend(h for h in http.BILLING_HOSTS if h not in out)
    return out


def _headers(account: dict) -> dict[str, str]:
    headers = http.build_headers(account)
    # 缺 X-Client-Platform 会被网关当成未知客户端。
    headers["X-Client-Platform"] = "web"
    headers["Accept"] = "application/json, text/plain, */*"
    return headers


def _num(value) -> float:
    """容量字段在响应里是**字符串**（"223.92000018"），需要单独解析。"""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else 0.0


def _date(value) -> str:
    """把各种时间形态归一成 ``YYYY-MM-DD``，拿不到就返回空串。

    packages 接口给的是 ``"2026-12-03 16:57:04"`` 这种字符串，偶尔也有毫秒时间戳。
    界面上只显示到日——精确到秒对「还剩几天」这个判断没有帮助。
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            seconds = value / 1000 if value > 1_000_000_000_000 else value
            return dt.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _days_left(date_str: str) -> int | None:
    """距到期还有几天。负数表示已过期，0 表示今天到期。"""
    if not date_str:
        return None
    try:
        target = dt.date.fromisoformat(date_str)
    except ValueError:
        return None
    return (target - dt.date.today()).days


def _packages(account: dict, budget: http.Budget, path: str, codes: tuple[str, ...]) -> list[dict]:
    """查一批积分包的明细。失败返回空列表——到期时间缺失不影响余额显示。"""
    if budget.expired():
        return []
    headers = _headers(account)
    today = dt.date.today()
    body = {
        "PageNumber": 1,
        "PageSize": 200,
        # Status [0,3] = 正常 + 已过期（官方前端同款）。只查 0 会漏掉刚过期的包，
        # 而「刚过期」恰恰是最需要提醒的状态。
        "Status": [0, 3],
        "SlicePeriodStartTime": f"{today} 00:00:00",
        "SlicePeriodEndTime": f"{today} 23:59:59",
        "PackageCodes": list(codes),
    }
    last: tuple[int, dict] = (http.CODE_NO_NETWORK, {"error": "请求未发出"})
    for host in _hosts(account):
        status, response = http.post_retry(
            f"https://{host}{path}", headers, body, budget=budget
        )
        if http.api_success(status, response) or http.is_unauthorized(status, response):
            last = (status, response)
            break
        last = (status, response)

    status, response = last
    if not http.api_success(status, response):
        return []

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    rows = data.get("Accounts")
    if not isinstance(rows, list):
        return []

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 优先用精确字段：CapacityRemain 是整数截断，会丢掉小数。
        remain = _num(
            row.get("CycleCapacityRemainPrecise") or row.get("CycleCapacityRemain")
        )
        total = _num(row.get("CycleCapacitySizePrecise") or row.get("CycleCapacitySize"))
        expires = _date(row.get("CycleEndTime")) or _date(row.get("ExpiredTime"))
        if remain <= 0 and total <= 0:
            continue
        out.append(
            {
                "code": str(row.get("PackageCode") or ""),
                "name": str(
                    row.get("SubProductName") or row.get("PackageName") or "积分包"
                ),
                "remain": round(remain, 2),
                "total": round(total, 2),
                "expiresAt": expires,
                "daysLeft": _days_left(expires),
            }
        )

    # 快到期的排前面：那才是需要行动的信息。
    out.sort(key=lambda item: item["expiresAt"] or "9999-12-31")
    return out[:MAX_PACKAGES]


def _nearest_expiry(packages: list[dict]) -> dict | None:
    """最近一个还会到期的包。已过期的跳过——它们不再有「还剩几天」的意义。"""
    live = [p for p in packages if p.get("expiresAt") and (p.get("daysLeft") or 0) >= 0]
    if not live:
        return None
    return min(live, key=lambda item: item["expiresAt"])


def fetch(account: dict, budget: http.Budget) -> dict:
    """查一次积分。不做重试——只读接口，失败如实报。

    返回 ``{ok, total, remain, used, unit, plan, groups[], packages[],
    nearestExpiry, message}``。

    packages 与 summary 并发查：summary 给余额、packages 给到期日，
    两者互不依赖。到期日查不到时余额照常显示，不把整块功能弄挂。
    """
    headers = _headers(account)
    last: tuple[int, dict] = (http.CODE_NO_NETWORK, {"error": "请求未发出"})
    for host in _hosts(account):
        status, body = http.post_retry(f"https://{host}{SUMMARY}", headers, {}, budget=budget)
        last = (status, body)
        if http.api_success(status, body) or http.is_unauthorized(status, body):
            break

    status, body = last
    if not http.api_success(status, body):
        return {
            "ok": False,
            "message": http.describe(status, body),
            "unauthorized": http.is_unauthorized(status, body),
        }

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    total = _num(data.get("TotalCredits"))
    plan = str(data.get("SubscriptionPackageName") or "").strip()

    groups: list[dict] = []
    remain_sum = 0.0
    used_sum = 0.0
    unit = "credits"
    raw_groups = data.get("CreditGroups")
    if isinstance(raw_groups, list):
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            capacity = _num(item.get("CycleTotalCapacity"))
            remain = _num(item.get("CycleRemainCapacity"))
            used = _num(item.get("CycleUsedCapacity"))
            count = http.as_int(item.get("TotalCount"))
            remain_sum += remain
            used_sum += used
            if item.get("CapacityUnit"):
                unit = str(item["CapacityUnit"])
            # 容量为 0 且没有条目的分组是空的，列出来只会占版面。
            if capacity <= 0 and count <= 0:
                continue
            groups.append(
                {
                    "kind": str(item.get("Kind") or ""),
                    "name": str(item.get("Name") or item.get("Kind") or "未命名"),
                    "total": round(capacity, 2),
                    "remain": round(remain, 2),
                    "used": round(used, 2),
                    "count": count,
                }
            )

    # 到期时间：两个 packages 接口各自覆盖免费包与付费包，合并后按到期日排序。
    packages = _packages(account, budget, FREE, FREE_CODES)
    packages.extend(_packages(account, budget, PAID, PAID_CODES))
    packages.sort(key=lambda item: item["expiresAt"] or "9999-12-31")
    packages = packages[:MAX_PACKAGES]

    # TotalCredits 是权威值；分组求和只在它缺失时兜底。
    if not total:
        total = remain_sum

    return {
        "ok": True,
        "total": round(total, 2),
        "remain": round(remain_sum if remain_sum else total, 2),
        "used": round(used_sum, 2),
        "unit": unit,
        "plan": plan,
        "groups": groups,
        "packages": packages,
        "nearestExpiry": _nearest_expiry(packages),
        "finishedAt": accounts.now_ms(),
    }
