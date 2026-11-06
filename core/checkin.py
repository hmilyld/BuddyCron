"""签到：查状态 → 未签才领。

接口契约（实测 + 88lin 交叉验证）：
    POST /v2/billing/meter/checkin-activity-status   查询（回退 /checkin-status）
    POST /v2/billing/meter/daily-checkin             领取（空体 {}，服务端按天幂等）

响应字段：
    active              False 表示活动未开启（非签到季，正常状态）
    today_checked_in    今日是否已签
    today_credit        今日到账积分
    streak_days         连续签到天数
    total_credits       累计积分

「已签到」有两种形态，都必须按成功处理：body 为 null，或 HTTP 400 + code 10001
「今天已签到，请明天再来」。这两种在脚本里区分开只会制造假失败。
"""

from __future__ import annotations

from . import accounts, http

METER = "/v2/billing/meter"


def _hosts(account: dict) -> list[str]:
    endpoint = (account.get("auth") or {}).get("endpoint") or ""
    host = endpoint.replace("https://", "").strip("/") if endpoint else ""
    out: list[str] = []
    if host in http.ALLOWED_HOSTS:
        out.append(host)
    out.extend(h for h in http.BILLING_HOSTS if h not in out)
    return out


def fetch_status(account: dict, budget: http.Budget) -> tuple[int, dict]:
    """查今日签到状态。主路径失败回退旧路径。

    这是只读接口，允许重试。
    """
    headers = http.build_headers(account)
    last: tuple[int, dict] = (http.CODE_NO_NETWORK, {"error": "请求未发出"})
    for host in _hosts(account):
        for path in (f"{METER}/checkin-activity-status", f"{METER}/checkin-status"):
            status, body = http.post_retry(
                f"https://{host}{path}", headers, {}, budget=budget
            )
            if http.api_success(status, body):
                return status, body
            last = (status, body)
            # 鉴权失败换 host 也没用，直接交回上层去刷新 token。
            if http.is_unauthorized(status, body):
                return status, body
    return last


def submit(account: dict, budget: http.Budget) -> tuple[int, dict]:
    """领取今日积分。服务端按天去重，所以允许重试。

    写操作通常不重试，这个接口是例外：当天重复调用只会返回「已签」，不会多发一次。
    """
    headers = http.build_headers(account)
    last: tuple[int, dict] = (http.CODE_NO_NETWORK, {"error": "请求未发出"})
    for host in _hosts(account):
        status, body = http.post_retry(
            f"https://{host}{METER}/daily-checkin", headers, {}, budget=budget
        )
        if http.api_success(status, body):
            return status, body
        if is_already(status, body):
            return status, body
        last = (status, body)
        if http.is_unauthorized(status, body):
            return status, body
    return last


def is_already(status: int, body: dict) -> bool:
    """「今日已签」的两种形态。"""
    if body is None or (isinstance(body, dict) and not body):
        return 200 <= status < 300
    if not isinstance(body, dict):
        return False
    code = body.get("code")
    message = str(body.get("msg") or body.get("message") or "")
    if code == 10001:
        return True
    return "已签" in message


def _checked_in(body: dict) -> bool:
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for source in (data, body):
        for key in ("today_checked_in", "todayCheckedIn"):
            if key in source and source[key] is not None:
                return bool(source[key])
    return False


def run(account: dict, budget: http.Budget, router: http.HostRouter | None = None) -> dict:
    """单账号签到。返回可直接落进汇报的结构。

    网络失败只重试同一请求；只有明确鉴权失效才刷新 token，且刷新走串行闸门。
    """
    working, patch = accounts.with_fresh_token(account, budget)

    status, body = fetch_status(working, budget)
    if http.is_unauthorized(status, body) and accounts.can_refresh(working):
        fresh = _refresh(working, budget, patch)
        if fresh is not None:
            working, patch = fresh
            status, body = fetch_status(working, budget)

    if not http.api_success(status, body):
        return _fail(http.describe(status, body), patch, status)

    if not _active(body):
        name = http.dig(body, "activity_name") or ""
        suffix = f"（{name}）" if name else ""
        return {
            "result": "INACTIVE",
            "ok": True,
            "message": f"签到活动未开启{suffix}",
            "patch": patch,
        }

    if _checked_in(body):
        return _already(body, patch)

    status, body = submit(working, budget)
    if http.is_unauthorized(status, body) and accounts.can_refresh(working):
        fresh = _refresh(working, budget, patch)
        if fresh is not None:
            working, patch = fresh
            status, body = submit(working, budget)

    if status in (http.CODE_NO_NETWORK, http.CODE_BUDGET_OUT):
        return _fail(f"领取请求未能送达（{http.describe(status, body)}）", patch, status)

    if is_already(status, body):
        # 客户端或另一轮刚刚签过：回头查一次拿最新数字，拿不到就用现有状态。
        status2, body2 = fetch_status(working, budget)
        latest = body2 if http.api_success(status2, body2) else body
        return _already(latest, patch, via="今日已签过（服务端判定已领取）")

    if http.api_success(status, body):
        credit = http.dig(body, "credit")
        if credit is None:
            return _fail(f"未识别的领取返回（HTTP {status}）", patch, status)
        # 领完再查一次，把连签/累计等展示值一起拿回来。
        status2, body2 = fetch_status(working, budget)
        latest = body2 if http.api_success(status2, body2) else body
        return _claimed(latest, credit, patch)

    return _fail(f"领取失败：{http.describe(status, body)}", patch, status)


def _refresh(working: dict, budget: http.Budget, patch: dict | None):
    """刷新一次并返回新工作副本。失败返回 None（保留原错误，不掩盖真因）。"""
    try:
        new_auth = accounts.refresh_account(working, budget)
    except RuntimeError:
        return None
    merged = dict(working)
    auth = dict(working.get("auth") or {})
    auth.update(new_auth)
    merged["auth"] = auth
    return merged, new_auth


def _active(body: dict) -> bool:
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for source in (data, body):
        if "active" in source and source["active"] is not None:
            return bool(source["active"])
    # 字段缺失时按「活动开启」处理：否则接口少给一个字段就把签到整个静默关掉。
    return True


def _week_progress(body: dict) -> list[bool]:
    """一周 7 天的签到进度。官方接口直接给 ``week_progress: [bool x7]``。

    这是界面上唯一能「一眼看出这周签了几天」的数据，比一个总数字更有信息量。
    拿不到就返回空列表，前端自行降级，不编造。
    """
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for source in (data, body):
        value = source.get("week_progress") if isinstance(source, dict) else None
        if isinstance(value, list) and value:
            return [bool(item) for item in value[:7]]
    return []


def _month_checkins(body: dict) -> list[str]:
    """本月已签到的日期列表。

    用 ``checkin_dates`` 而不是 heatmap 的 ``score``：两者含义不同 ——
    ``checkin_dates`` 是**签到记录**，``score`` 是活跃度分数（含对话、用技能等行为）。
    界面问的是「这个月签了哪几天」，只有前者能回答。
    """
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for source in (data, body):
        value = source.get("checkin_dates") if isinstance(source, dict) else None
        if isinstance(value, list):
            return [str(item)[:10] for item in value if isinstance(item, str)]
    return []


def _already(body: dict, patch, via: str | None = None) -> dict:
    inner: list[str] = []
    credit = http.dig(body, "today_credit") or http.dig(body, "daily_credit")
    streak = http.dig(body, "streak_days")
    total = http.dig(body, "total_credits")
    if credit is not None:
        inner.append(f"今日 +{http.as_int(credit)}")
    if streak is not None:
        inner.append(f"连续 {http.as_int(streak)} 天")
    if total is not None:
        inner.append(f"累计 {http.as_int(total)} 积分")
    text = via or "今日已签过"
    if inner:
        text += "（" + "，".join(inner) + "）"
    return {
        "result": "ALREADY",
        "ok": True,
        "message": text,
        "credit": http.as_int(credit) if credit is not None else 0,
        "streakDays": http.as_int(streak) if streak is not None else None,
        "weekProgress": _week_progress(body),
        "checkinDates": _month_checkins(body),
        "patch": patch,
    }


def _claimed(body: dict, credit, patch) -> dict:
    streak = http.dig(body, "streak_days")
    total = http.dig(body, "total_credits")
    is_streak_day = bool(http.dig(body, "is_streak_day"))
    parts = [f"成功领取 {http.as_int(credit)} 积分"]
    if is_streak_day:
        parts.append("且为连签奖励日")
    detail: list[str] = []
    if streak is not None:
        detail.append(f"连续 {http.as_int(streak)} 天")
    if total is not None:
        detail.append(f"累计 {http.as_int(total)} 积分")
    message = "，".join(parts)
    if detail:
        message += "（" + "，".join(detail) + "）"
    return {
        "result": "CLAIMED",
        "ok": True,
        "message": message,
        "credit": http.as_int(credit),
        "streakDays": http.as_int(streak) if streak is not None else None,
        "weekProgress": _week_progress(body),
        "checkinDates": _month_checkins(body),
        "patch": patch,
    }


def _fail(message: str, patch, status: int) -> dict:
    return {
        "result": "NETWORK" if status == http.CODE_NO_NETWORK else (
            "TIMEOUT" if status == http.CODE_BUDGET_OUT else "ERROR"
        ),
        "ok": False,
        "message": message,
        "patch": patch,
        "http": status,
    }
