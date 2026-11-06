"""成长中心：旅行 / 任务 / 补登 / 连登兑换 / 抽奖 / Buddy 盲盒。

接口契约以**官方前端 bundle 为准**（assets.workbuddy.cn 的 growthSpace-*.js），
不是以任何第三方脚本为准。两处第三方实现都在这上面出过错：

    travel/depart     官方 {location_id}     wb-checkin 传 {destination}  → 错
    travel/claim      官方 {} 空体           两份都传 {record_id}        → 多传
    lottery/draw      官方 {client_token}    wb-checkin 传 {}            → 缺了会 400
    buddy/open        官方 {count}           wb-checkin 没有这个功能

**执行顺序不能改**（这一段是 88lin 最值钱的部分，每一条都有理由）：

    1. 旅行：先领已有礼物，派出留到最后
    2. 任务：接取 + 领已完成奖励（任务送的能量/抽奖机会后面马上要用）
    3. 补登：断登补一张卡（会改变连签天数，必须早于兑换）
    4. 连登兑换：7/14/28 天三档（读最新连签状态）
    5. 抽奖：用兑换得到的次数
    6. Buddy 盲盒：用能量开桌宠（**会覆盖旅行记录**，所以必须在旅行明确 idle 时）
    7. 最后派出：既不覆盖在途礼物，也让「每天只跑一次」的用户有机会开 Buddy

任何一段失败都不影响其余段。整轮遇凭证失效时刷新一次后重跑。
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

from . import accounts, http

GROWTH = "/v2/activity/growth"
TASKS = "/v2/activity/growth/tasks"

# 单账号单轮最多抽多少次，防接口异常时死循环。
MAX_DRAWS = 20
# 每轮最多用掉几张补登卡。写操作不可逆，一张就够，剩下的留给下一轮。
MAX_MAKEUP_PER_RUN = 1
# 任务接取分批大小，别把请求体撑大。
ACCEPT_BATCH = 20

# 连登兑换三档：(档位标识, /redeem/summary 的状态字段前缀, 展示名)
# tier 传的是**档位标识字符串**（"7d"），不是天数也不是档位名：
# 实测传 "starter" / "7" / 7 分别得到 unknown tier / unknown tier / invalid request。
REDEEM_TIERS = (
    ("7d", "starter", "入门"),
    ("14d", "advanced", "进阶"),
    ("28d", "legendary", "巅峰"),
)

# 首领 Buddy 任务：必做，自动激活，不需要（也不能）接取。
# 它是其余全部进阶任务的前置 —— 没完成它，接取其他任务一律返回
# "prerequisite not met: first_buddy"。而它的完成条件是**在客户端里真的聊一次**，
# 服务端没有上报进度的入口。所以没有桌面客户端时，这条链在它这里就断了。
LEADER_TASK = "first_buddy"


def _hosts(account: dict) -> list[str]:
    endpoint = (account.get("auth") or {}).get("endpoint") or ""
    host = endpoint.replace("https://", "").strip("/") if endpoint else ""
    out: list[str] = []
    if host in http.ALLOWED_HOSTS:
        out.append(host)
    out.extend(h for h in http.ACTIVITY_HOSTS if h not in out)
    return out


def _headers(account: dict) -> dict[str, str]:
    """活动接口用极简头（UA + Bearer + X-User-Id），官方前端另外带
    X-Client-Platform 与 Accept-Language，一并补上更贴近真实客户端。"""
    headers = http.build_headers(account)
    headers["X-Client-Platform"] = "workbuddy"
    headers["Accept"] = "application/json, text/plain, */*"
    return headers


def _call(account, budget, method, path, payload=None, retry=None):
    """按 host 候选依次尝试；写操作默认不重试。

    返回 (status, body, host)。成功即锁定该 host，后续同账号请求直接用。
    """
    headers = _headers(account)
    if retry is None:
        retry = method == "GET"
    last: tuple[int, dict, str] = (http.CODE_NO_NETWORK, {"error": "请求未发出"}, "")
    for host in _hosts(account):
        url = f"https://{host}{path}"
        if method == "GET":
            status, body = http.request(url, headers, method="GET", budget=budget, retry=retry)
        else:
            status, body = http.request(
                url, headers, method="POST", payload=payload, budget=budget, retry=retry
            )
        if http.api_success(status, body) or http.is_unauthorized(status, body):
            return status, body, host
        last = (status, body, host)
    return last


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def _number(value) -> int | None:
    """把可能带小数的数字字段转成 int；负数或非法值返回 None。"""
    if value is None:
        return None
    parsed = http.as_int(value, default=-1)
    return parsed if parsed >= 0 else None


def client_token(prefix: str = "u") -> str:
    """活动接口要求的防重放 token。

    官方前端用 ``crypto.randomUUID()`` 拼成 ``u-<uuid>``；服务端只做幂等去重、
    不校验格式。缺了它 ``/lottery/draw`` 直接 400（实测过）。
    """
    return f"{prefix}-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# 1. 旅行
# ---------------------------------------------------------------------------


def _travel(account, budget) -> tuple[dict, str, bool]:
    """返回 (结果, 旅行状态, 今日名额是否已用完)。

    旅行状态：idle / traveling / arrived / unknown
    """
    status, body, _ = _call(account, budget, "GET", f"{GROWTH}/buddy/travel/status")
    if http.is_unauthorized(status, body):
        return {"state": "auth", "message": "登录态失效"}, "unknown", False
    if not http.api_success(status, body):
        return {"state": "fail", "message": http.describe(status, body)}, "unknown", False

    data = body.get("data") if isinstance(body.get("data"), dict) else body
    raw_state = _text(http.dig(data, "state"))
    daily_limit = _truthy(http.dig(data, "daily_limit_reached"))
    if raw_state in ("idle", "traveling", "arrived"):
        travel = raw_state
    else:
        # 兼容只有 arrived 布尔的老形态
        arrived = _truthy(http.dig(data, "arrived") or http.dig(data, "is_arrived"))
        travel = "arrived" if arrived else "unknown"
        if travel == "unknown":
            return (
                {"state": "fail", "message": "旅行状态无法识别，跳过开 Buddy 与派出"},
                "unknown",
                daily_limit,
            )

    if travel != "arrived":
        if travel == "traveling":
            location = http.dig(data, "location")
            name = location.get("name") if isinstance(location, dict) else None
            suffix = f"（{name}）" if name else ""
            return {"state": "skip", "message": f"Buddy 旅行中{suffix}"}, travel, daily_limit
        return {"state": "skip", "message": "今日未出发"}, travel, daily_limit

    # 已到达：先领奖。官方前端发空体；若服务端要 record_id 再退一步带上。
    record_id = http.dig(data, "record_id") or http.dig(data, "recordId")
    claim_status, claim_body, _ = _call(account, budget, "POST", f"{GROWTH}/buddy/travel/claim", {})
    if http.is_unauthorized(claim_status, claim_body):
        return {"state": "auth", "message": "登录态失效"}, travel, daily_limit
    if not http.api_success(claim_status, claim_body) and record_id:
        claim_status, claim_body, _ = _call(
            account, budget, "POST", f"{GROWTH}/buddy/travel/claim", {"record_id": record_id}
        )
    if not http.api_success(claim_status, claim_body):
        message = http.describe(claim_status, claim_body)
        # 官方前端认这两个文案，说明它们是正常的业务竞争而非故障：
        #   not arrived yet     —— 查询后被服务端判定还没到（时钟差/状态滞后）
        #   no unclaimed travel —— 已经被客户端或另一轮领过了
        # 当成失败会让轮询每轮刷一条假错误。
        if "not arrived yet" in message.lower() or "no unclaimed travel" in message.lower():
            return {"state": "skip", "message": "礼物尚不可领或已被领过"}, travel, daily_limit
        return (
            {"state": "fail", "message": f"领旅行礼物失败：{message}"},
            travel,
            daily_limit,
        )

    reward = http.dig(claim_body, "reward_credit")
    message = f"领旅行礼物 +{http.as_int(reward)} 积分" if reward is not None else "已领旅行礼物"
    # 领成功后旅行记录清空，允许开 Buddy / 派出。
    return {"state": "ok", "message": message, "credit": http.as_int(reward)}, "idle", daily_limit


# ---------------------------------------------------------------------------
# 2. 任务
# ---------------------------------------------------------------------------


def _common_prefix(messages: list[str]) -> str:
    """多条失败来自同一个根因时，把根因提炼出来。

    实测形态：17 项全被拒、错误消息都是 ``prerequisite not met: first_buddy``。
    逐条列 17 条同样的文案没有信息量，反而把真正该做的事（先领养 Buddy）淹没。
    """
    if not messages:
        return ""
    keys = []
    for message in messages:
        key = message.split("：", 1)[-1].strip()
        keys.append(key)
    if len(set(keys)) != 1:
        return ""
    key = keys[0]
    if key.startswith("prerequisite not met"):
        missing = key.split(":", 1)[-1].strip()
        if missing == "first_buddy":
            return "需先领养 Buddy"
        return f"缺前置条件 {missing}"
    return ""


def _task_status(item: dict) -> str:
    for key in ("accept_status", "acceptStatus", "status"):
        value = item.get(key)
        if value is not None:
            return _text(value)
    return ""


def _task_code(item: dict) -> str:
    for key in ("task_code", "taskCode", "code"):
        value = item.get(key)
        if value:
            return _text(value)
    return ""


def _task_done(item: dict) -> bool:
    """已完成但未领取 → 可以领奖。

    服务端用 ``progress.current >= progress.target`` 表示完成，另有 ``completed``
    状态；``claimed`` 表示奖励已到手，不必再请求。
    """
    status = _task_status(item)
    if status == "claimed":
        return False
    if status == "completed":
        return True
    progress = item.get("progress")
    if not isinstance(progress, dict):
        return False
    current = http.as_int(progress.get("current"), -1)
    target = http.as_int(progress.get("target"), -1)
    return target >= 1 and current >= target


def _pending_view(item: dict) -> dict | None:
    """待体验任务的展示形态：未完成、未领取的都算。"""
    code = _task_code(item)
    if not code or _task_done(item) or _task_status(item) == "claimed":
        return None
    progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
    return {
        "code": code,
        "title": _text(http.dig(item, "title") or http.dig(item, "name")),
        "jumpUrl": _text(http.dig(item, "jump_url") or http.dig(item, "jumpUrl")),
        "reward": http.as_int(item.get("reward_credit")),
        "current": http.as_int(progress.get("current")),
        "target": http.as_int(progress.get("target")) or 1,
    }


def _leader_unlocked(items: list[dict]) -> tuple[bool, dict | None]:
    """首领 Buddy 任务是否已打通，返回 (是否打通, 该项本身)。

    ``first_buddy`` 是其余 16 项进阶任务的前置：没完成它，接取一律返回
    ``prerequisite not met: first_buddy``。它的完成条件是**在客户端里真的产生
    一次对话**，服务端没有上报进度的入口。

    所以这里先看它的状态再决定要不要接取进阶任务：
        claimed / completed → 已打通，可以接取并领奖
        其它（not_accepted / accepted / in_progress）→ 未打通，跳过接取

    不做这道门控的代价：每轮白打 16 个接取请求、收回 16 条同样的错误，
    汇报里永远挂着一句「16 项接取被拒」——用户看不出这是「还没开始」而不是故障。
    """
    leader = next((t for t in items if _task_code(t) == LEADER_TASK), None)
    if leader is None:
        # 接口没给这一项（改版或权限差异）：保守按未打通处理，不白打接取请求。
        return False, None
    status = _task_status(leader)
    if status == "claimed" or _task_done(leader):
        return True, leader
    return False, leader


def _tasks(account, budget) -> tuple[dict, bool]:
    status, body, _ = _call(account, budget, "GET", TASKS)
    if http.is_unauthorized(status, body):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(status, body):
        return {"state": "fail", "message": http.describe(status, body)}, False

    data = body.get("data") if isinstance(body.get("data"), dict) else body
    items = http.dig(data, "tasks") or http.dig(data, "list") or http.dig(data, "records")
    if not isinstance(items, list) or not items:
        return {"state": "skip", "message": "没有任务"}, False

    titles = {_task_code(t): _text(http.dig(t, "title") or _task_code(t)) for t in items}

    unlocked, leader = _leader_unlocked(items)

    claimed = 0
    already = 0
    failed = 0
    credit = 0
    claimed_titles: list[str] = []
    accepted = 0
    accept_errors: list[str] = []

    if not unlocked:
        # 未打通：先看首领任务本身能不能领奖（它完成但没领时，领掉就是 300 积分，
        # 同时解锁其余 16 项）。领奖是幂等操作，重复调用不会重复发奖。
        if leader is not None and _task_done(leader):
            code = _task_code(leader)
            st, bd, _ = _call(account, budget, "POST", f"{TASKS}/{code}/claim", {})
            if http.is_unauthorized(st, bd):
                return {"state": "auth", "message": "登录态失效"}, True
            if http.api_success(st, bd) and not _truthy(http.dig(bd, "already_claimed")):
                claimed += 1
                credit += http.as_int(http.dig(bd, "credit") or http.dig(bd, "reward_credit"))
                claimed_titles.append(titles.get(code, code))
        return (
            {
                "state": "ok" if claimed else "skip",
                "message": (
                    f"领了首领 Buddy 任务（{titles.get(_task_code(leader), '')}），"
                    f"进阶任务已解锁，下轮接取"
                    if claimed
                    else "进阶任务待解锁（需先在客户端完成一次对话）"
                ),
                "claimed": claimed,
                "accepted": 0,
                "credit": credit,
                "pending": [],
                "locked": True,
            },
            False,
        )

    # 已打通：接取未接取的进阶任务。
    # 服务端只有接取之后才给进度槽（progress 从 null 变成 0/N），不报名的话
    # 用户在客户端里真的用了也不会被记进度。
    #
    # 两个例外必须排掉：
    #   first_buddy  实测返回 "task does not require acceptance" —— 它是必做任务，
    #                自动激活，接取请求会被逐条拒掉。
    #   已 locked 的  接取也没用。
    pending = [
        _task_code(t)
        for t in items
        if _task_status(t) == "not_accepted"
        and not t.get("locked")
        and _task_code(t) != LEADER_TASK
    ]
    for index in range(0, len(pending), ACCEPT_BATCH):
        if budget.expired():
            break
        batch = [code for code in pending[index : index + ACCEPT_BATCH] if code]
        if not batch:
            continue
        st, bd, _ = _call(account, budget, "POST", f"{GROWTH}/tasks/accept", {"task_codes": batch})
        if http.is_unauthorized(st, bd):
            return {"state": "auth", "message": "登录态失效"}, True

        # 服务端**逐条**给结果，HTTP 200 不等于全部接取成功。
        # 旧实现把 200 当成 len(batch) 全部成功，会把「全部被拒」报成「接取 N 项」。
        results = http.dig(bd, "results")
        if isinstance(results, list):
            for row in results:
                if not isinstance(row, dict):
                    continue
                code = _text(row.get("task_code"))
                row_status = _text(row.get("status"))
                if row_status == "error":
                    message = _text(row.get("message")) or http.describe(st, bd)
                    accept_errors.append(f"{titles.get(code, code)}：{message}")
                elif row_status in ("ok", "success", "accepted"):
                    accepted += 1
        elif http.api_success(st, bd):
            # 没有逐条结果（旧形态）：只能按整体成功计。
            accepted += len(batch)
        else:
            accept_errors.append(http.describe(st, bd))

    claimed = 0
    already = 0
    failed = 0
    credit = 0
    claimed_titles: list[str] = []
    for item in items:
        if budget.expired():
            break
        if not _task_done(item):
            continue
        code = _task_code(item)
        if not code:
            continue
        # 领奖幂等：已领过的返回 already_claimed，不会重复发奖。
        st, bd, _ = _call(account, budget, "POST", f"{TASKS}/{code}/claim", {})
        if http.is_unauthorized(st, bd):
            return {
                "state": "auth",
                "message": f"登录态失效（已领 {claimed} 项）",
            }, True
        if not http.api_success(st, bd):
            failed += 1
            continue
        if _truthy(http.dig(bd, "already_claimed")):
            already += 1
            continue
        claimed += 1
        credit += http.as_int(http.dig(bd, "credit") or http.dig(bd, "reward_credit"))
        title = titles.get(code, code)
        if title:
            claimed_titles.append(title)

    pending_view = [v for v in (_pending_view(t) for t in items) if v]

    parts: list[str] = []
    if accepted:
        parts.append(f"接取 {accepted} 项")
    if accept_errors:
        # 已打通之后仍被拒，说明是别的根因（不是 first_buddy 了），如实报出来。
        root = _common_prefix(accept_errors)
        if root:
            parts.append(f"{len(accept_errors)} 项接取被拒（{root}）")
        else:
            for message in accept_errors[:3]:
                parts.append(f"接取失败 {message}")
            if len(accept_errors) > 3:
                parts.append(f"另有 {len(accept_errors) - 3} 项接取失败")
    if claimed:
        detail = f"（{'、'.join(claimed_titles)}）" if claimed_titles else ""
        parts.append(f"领了 {claimed} 项{detail}，共 {credit} 积分")
    if already:
        parts.append(f"{already} 项此前已领")
    if failed:
        parts.append(f"{failed} 项领取失败")
    if pending_view:
        names = "、".join(v["title"] for v in pending_view[:3] if v["title"])
        suffix = f"（{names}…）" if names else ""
        parts.append(f"{len(pending_view)} 项待体验{suffix}")

    return (
        {
            "state": "ok" if claimed or accepted else "skip",
            "message": "，".join(parts) if parts else "没有可领的任务",
            "claimed": claimed,
            "accepted": accepted,
            "credit": credit,
            "pending": pending_view,
            "locked": False,
        },
        False,
    )


# ---------------------------------------------------------------------------
# 3. 补登卡
# ---------------------------------------------------------------------------


def makeup_candidates(streak_body: dict, heatmap_body: dict) -> list[str]:
    """从活跃日历选断登日期。

    ``makeup_dates`` 是**已补登历史**，不能当待办清单——旧实现就是这么错的，
    结果既反复提交已补登日期，也在历史为空时漏掉真正的断登。

    官方规则（活动页面原文）：仅可补救**当月**断登；补登卡上限 4 张。
    这里另外要求不早于活动上线日，且只认日历里 score == 0 的历史日期；
    日历缺项不推测为零——靠猜测消耗补登卡是不可逆的损失。
    """
    import datetime as dt

    def parse(value) -> dt.date:
        if not isinstance(value, str) or not _DATE_RE.match(value):
            raise ValueError("日期格式无效")
        return dt.date.fromisoformat(value)

    today_obj = http.dig(heatmap_body, "today")
    if not isinstance(today_obj, dict):
        raise ValueError("活跃日历缺少服务端日期")
    today = parse(today_obj.get("date"))
    launch_raw = http.dig(streak_body, "launch_date")
    launch = parse(launch_raw) if launch_raw else dt.date(2026, 6, 17)
    start = max(today.replace(day=1), launch, dt.date(2026, 6, 17))

    streak = http.dig(streak_body, "streak")
    made_up_raw = streak.get("makeup_dates") if isinstance(streak, dict) else []
    if made_up_raw is None:
        made_up_raw = []
    if not isinstance(made_up_raw, list):
        raise ValueError("已补登记录格式无效")
    made_up = {parse(value) for value in made_up_raw}

    cells = http.dig(heatmap_body, "cells")
    if not isinstance(cells, list):
        raise ValueError("活跃日历缺少日期列表")
    scores: dict[dt.date, float] = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("活跃日历记录格式无效")
        day = parse(cell.get("date"))
        score = cell.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("活跃日历分数无效")
        if day in scores and scores[day] != score:
            raise ValueError("活跃日历同一日期的记录冲突")
        scores[day] = float(score)

    # 最近的断点优先，以便尽早恢复当前连登。
    #
    # 注意一个容易误判的形态：账号从未活跃过时，本月所有历史日期都是 score == 0，
    # 但它们不是「断登」而是「还没开始」。判断依据是 streak.days == 0 且
    # month_total_days == 0 —— 此时用补登卡补一个从没开始过的月份没有意义，
    # 而且补登卡是不可逆的消耗。
    streak_days = http.as_int(streak.get("days")) if isinstance(streak, dict) else 0
    month_total = (
        http.as_int(streak.get("month_total_days")) if isinstance(streak, dict) else 0
    )
    never_started = streak_days <= 0 and month_total <= 0

    if never_started:
        return []

    return sorted(
        (
            day.isoformat()
            for day, score in scores.items()
            if start <= day < today and score == 0 and day not in made_up
        ),
        reverse=True,
    )


_DATE_RE = __import__("re").compile(r"\d{4}-\d{2}-\d{2}")


def _makeup(account, budget) -> tuple[dict, bool, dict | None, bool]:
    """返回 (结果, 是否鉴权失效, 最新 streak 响应, 是否需要重取 streak)。"""
    st, bd, _ = _call(account, budget, "GET", f"{GROWTH}/streak")
    if http.is_unauthorized(st, bd):
        return {"state": "auth", "message": "登录态失效"}, True, None, False
    if not http.api_success(st, bd):
        return {"state": "fail", "message": f"查连登状态失败：{http.describe(st, bd)}"}, False, None, False

    streak_body = bd
    cards_raw = http.dig(bd, "makeup_cards")
    if isinstance(cards_raw, dict):
        cards = http.as_int(cards_raw.get("balance"))
    else:
        cards = http.as_int(cards_raw)
    if cards <= 0:
        return {"state": "skip", "message": "没有补登卡"}, False, streak_body, False

    hs, hb, _ = _call(account, budget, "GET", f"{GROWTH}/heatmap")
    if http.is_unauthorized(hs, hb):
        return {"state": "auth", "message": "登录态失效"}, True, streak_body, False
    if not http.api_success(hs, hb):
        return (
            {"state": "fail", "message": f"查补登日历失败：{http.describe(hs, hb)}"},
            False,
            streak_body,
            False,
        )

    try:
        dates = makeup_candidates(streak_body, hb)
    except ValueError as error:
        return {"state": "fail", "message": f"补登日历异常：{error}"}, False, streak_body, False
    if not dates:
        return {"state": "skip", "message": f"无断登可补（{cards} 张卡）"}, False, streak_body, False

    used = 0
    stale = False
    for target in dates[: min(cards, MAX_MAKEUP_PER_RUN)]:
        if budget.expired():
            break
        us, ub, _ = _call(
            account, budget, "POST", f"{GROWTH}/makeup-cards/use", {"target_date": target}
        )
        if http.is_unauthorized(us, ub):
            return {"state": "auth", "message": "登录态失效"}, True, streak_body, stale
        if http.api_success(us, ub):
            used += 1
            stale = True
            continue
        message = http.describe(us, ub)
        # 查询后被客户端或另一轮补上，是正常竞争，不算失败。
        if us == 400 and "no makeup needed" in message.lower():
            break
        return (
            {"state": "fail", "message": f"补登 {target} 失败：{message}"},
            False,
            streak_body,
            stale,
        )

    if not used:
        return {"state": "skip", "message": "补登未执行"}, False, streak_body, False
    remaining = max(0, cards - used)
    tail = f"，另有 {len(dates) - used} 天可补" if len(dates) > used else ""
    return (
        {"state": "ok", "message": f"补登 {used} 天（剩 {remaining} 张卡{tail}）"},
        False,
        streak_body,
        True,
    )


# ---------------------------------------------------------------------------
# 4. 连登兑换
# ---------------------------------------------------------------------------


def _redeem(account, budget) -> tuple[dict, bool]:
    st, bd, _ = _call(account, budget, "GET", f"{GROWTH}/redeem/summary")
    if http.is_unauthorized(st, bd):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(st, bd):
        return {"state": "fail", "message": f"查连登兑换失败：{http.describe(st, bd)}"}, False

    data = bd.get("data") if isinstance(bd.get("data"), dict) else bd
    granted: list[str] = []
    skipped_locked = 0
    failed = 0
    for tier, status_key, label in REDEEM_TIERS:
        if budget.expired():
            break
        status = http.dig(data, f"{status_key}_status")
        # 字段缺失（None）同样跳过：接口改版时不该对三档无脑 POST。
        if not status or status in ("claimed", "locked"):
            if status == "locked":
                skipped_locked += 1
            continue
        rs, rb, _ = _call(
            account, budget, "POST", f"{GROWTH}/redeem",
            {"tier": tier, "client_token": client_token()},
        )
        if http.is_unauthorized(rs, rb):
            return {"state": "auth", "message": "登录态失效"}, True
        if http.api_success(rs, rb):
            granted.append(f"{label}{_redeem_detail(rb, tier)}")
            continue
        message = http.describe(rs, rb)
        # 403「连登天数不足」是业务常态，不是权限拒绝。
        if rs == 403 and ("连登" in message or "locked" in message.lower() or "不足" in message):
            skipped_locked += 1
            continue
        failed += 1

    parts: list[str] = []
    if granted:
        parts.append("兑换 " + "、".join(granted))
    if skipped_locked:
        parts.append(f"{skipped_locked} 档未解锁")
    if failed:
        parts.append(f"{failed} 档兑换失败")
    return (
        {
            "state": "ok" if granted else "skip",
            "message": "，".join(parts) if parts else "无可兑换档位",
        },
        False,
    )


def _redeem_detail(body: dict, tier: str) -> str:
    """兑换成功的奖励明细。实发字段带 _granted 后缀，读 ``credit`` 恒为空。"""
    bits = []
    for key, unit in (
        ("credit_granted", "积分"),
        ("energy_granted", "能量"),
        ("cards_granted", "补登卡"),
        ("chances_granted", "次抽奖"),
    ):
        value = http.as_int(http.dig(body, key))
        if value:
            bits.append(f"+{value} {unit}")
    return f"（{' '.join(bits)}）" if bits else ""


# ---------------------------------------------------------------------------
# 5. 抽奖
# ---------------------------------------------------------------------------


def _lottery(account, budget) -> tuple[dict, bool]:
    st, bd, _ = _call(account, budget, "GET", f"{GROWTH}/lottery/chances")
    if http.is_unauthorized(st, bd):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(st, bd):
        return {"state": "fail", "message": f"查抽奖次数失败：{http.describe(st, bd)}"}, False

    balance = http.as_int(http.dig(bd, "balance"))
    if balance <= 0:
        return {"state": "skip", "message": "没有抽奖次数"}, False

    draws = 0
    prizes: list[str] = []
    need_address = False
    while draws < MAX_DRAWS and draws < balance:
        if budget.expired():
            break
        ds, db, _ = _call(
            account, budget, "POST", f"{GROWTH}/lottery/draw",
            {"client_token": client_token()},
        )
        if http.is_unauthorized(ds, db):
            return {
                "state": "auth",
                "message": f"登录态失效（已抽 {draws} 次）",
            }, True
        if not http.api_success(ds, db):
            if draws == 0:
                return {"state": "fail", "message": f"抽取失败：{http.describe(ds, db)}"}, False
            break
        draws += 1
        prize = http.dig(db, "prize_name") or http.dig(db, "prize")
        if prize is not None:
            prizes.append(_text(prize))
        # 实物奖（冰箱贴/胸针/杯子）中奖后要用户自己填收件信息，脚本代填不了，
        # 但必须提醒——否则奖品会卡在未填地址状态。
        if _truthy(http.dig(db, "need_address")) or _truthy(http.dig(db, "require_address")):
            need_address = True

    if not draws:
        return {"state": "fail", "message": "抽取失败"}, False
    message = f"抽了 {draws} 次" + (f"：{'、'.join(prizes)}" if prizes else "")
    if need_address:
        message += "（含实物奖，需到成长中心填写收件信息）"
    return {"state": "ok", "message": message}, False


# ---------------------------------------------------------------------------
# 6. Buddy 盲盒
# ---------------------------------------------------------------------------


def _buddy_open(account, budget) -> tuple[dict, bool]:
    st, bd, _ = _call(account, budget, "GET", f"{GROWTH}/buddy/quota")
    if http.is_unauthorized(st, bd):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(st, bd):
        return {"state": "fail", "message": f"查能量失败：{http.describe(st, bd)}"}, False

    affordable = http.as_int(http.dig(bd, "affordable"))
    max_open = http.as_int(http.dig(bd, "max_open_count")) or 1
    if affordable <= 0:
        return {"state": "skip", "message": "能量不足"}, False

    count = min(affordable, max_open)
    os_, ob, _ = _call(account, budget, "POST", f"{GROWTH}/buddy/open", {"count": count})
    if http.is_unauthorized(os_, ob):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(os_, ob):
        return {"state": "fail", "message": f"开 Buddy 失败：{http.describe(os_, ob)}"}, False

    name = http.dig(ob, "buddy") or http.dig(ob, "name") or http.dig(ob, "buddies")
    label = _text(name) if name is not None else "新 Buddy"
    return {"state": "ok", "message": f"开 Buddy 盲盒 ×{count}（{label}）"}, False


# ---------------------------------------------------------------------------
# 7. 派出
# ---------------------------------------------------------------------------


def _depart(account, budget) -> tuple[dict, bool]:
    st, bd, _ = _call(account, budget, "GET", f"{GROWTH}/buddy/travel/config")
    if http.is_unauthorized(st, bd):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(st, bd):
        return {"state": "fail", "message": f"查旅行配置失败：{http.describe(st, bd)}"}, False

    data = bd.get("data") if isinstance(bd.get("data"), dict) else bd
    locations = http.dig(data, "locations")
    if not isinstance(locations, list) or not locations:
        return {"state": "skip", "message": "没有可选目的地"}, False
    first = locations[0] if isinstance(locations[0], dict) else {}
    location_id = first.get("id")
    if location_id is None:
        return {"state": "skip", "message": "目的地缺少 id"}, False

    ds, db, _ = _call(
        account, budget, "POST", f"{GROWTH}/buddy/travel/depart", {"location_id": location_id}
    )
    message = http.describe(ds, db)
    if ds == 403 and "no active buddy" in message.lower():
        return {"state": "skip", "message": "尚未领养 Buddy"}, False
    if http.is_unauthorized(ds, db):
        return {"state": "auth", "message": "登录态失效"}, True
    if not http.api_success(ds, db):
        return {"state": "fail", "message": f"派 Buddy 失败：{message}"}, False

    location = http.dig(db, "location")
    name = location.get("name") if isinstance(location, dict) else first.get("name")
    hours = http.dig(db, "duration_hours") or first.get("duration_hours")
    tail = []
    if name:
        tail.append(f"去{name}")
    if hours:
        tail.append(f"{hours} 小时后回")
    return {"state": "ok", "message": "派 Buddy " + ("（" + "，".join(tail) + "）" if tail else "")}, False


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def agree_if_needed(account, budget) -> dict:
    """静默代签活动规则。

    官方前端把这条做成强制 modal（首次进成长中心就弹），通常意味着服务端在别的
    端点里会校验 agreement 状态。用户明确要求静默代签，所以这里照做——但落盘记
    ``agreed_at``，万一以后要说明「什么时候同意的」有据可查。
    """
    if account.get("agreed_at"):
        return {"ok": True, "skipped": True}

    st, bd, _ = _call(account, budget, "GET", f"{GROWTH}/buddy/agreement")
    if http.is_unauthorized(st, bd):
        return {"ok": False, "auth": True}
    if http.api_success(st, bd) and _truthy(http.dig(bd, "agreed")):
        # 服务端说已经同意了（比如在官方客户端点过），只补记时间戳，不发 POST。
        _mark_agreed(account)
        return {"ok": True, "skipped": True}

    ps, pb, _ = _call(account, budget, "POST", f"{GROWTH}/buddy/agreement", {"agree": True})
    if http.is_unauthorized(ps, pb):
        return {"ok": False, "auth": True}
    if not http.api_success(ps, pb):
        return {"ok": False, "message": http.describe(ps, pb)}
    _mark_agreed(account)
    return {"ok": True, "signed": True}


def _mark_agreed(account) -> None:
    """记下同意时间。写失败不影响主流程——这只是审计线索，不是功能依赖。"""
    account_id = str(account.get("id") or "")
    if not account_id:
        return
    try:
        accounts.patch_account(account_id, {"agreed_at": accounts.now_ms()})
    except Exception:  # noqa: BLE001 - 审计记录失败不该拖垮签到
        pass


def run(account, budget, with_lottery: bool = True, with_buddy: bool = True) -> dict:
    """单账号跑一轮成长中心。

    每段独立容错：任何一段失败都不影响其余段。整轮遇凭证失效时刷新一次后重跑。
    """
    result, needs_auth = _round(account, budget, with_lottery, with_buddy)
    if not needs_auth or not accounts.can_refresh(account):
        return _finish(result, None)

    try:
        patch = accounts.refresh_account(account, budget)
    except RuntimeError as error:
        return {"ok": False, "message": str(error), "patch": None}

    fresh = dict(account)
    auth = dict(account.get("auth") or {})
    auth.update(patch)
    fresh["auth"] = auth
    retried, _ = _round(fresh, budget, with_lottery, with_buddy)
    return _finish(retried, patch)


def run_auto(account, budget) -> dict:
    """签到之后的顺手处理：旅行 + 任务 + 补登 + 兑换 + 抽奖，**不开 Buddy**。

    开 Buddy 会覆盖旅行记录，自动轮次里只在旅行明确 idle 时才做，交给 run() 的
    with_buddy 参数控制；这里保留完整顺序但跳过盲盒，避免一次自动运行把礼物冲掉。
    """
    return run(account, budget, with_lottery=True, with_buddy=True)


def _round(account, budget, with_lottery: bool, with_buddy: bool) -> tuple[dict, bool]:
    agree = agree_if_needed(account, budget)
    if agree.get("auth"):
        return {"travel": {"state": "auth", "message": "登录态失效"}}, True

    travel, travel_state, daily_limit = _travel(account, budget)
    auth_hit = travel.get("state") == "auth"

    tasks_result, tasks_auth = _tasks(account, budget)
    auth_hit = auth_hit or tasks_auth

    makeup_result, makeup_auth, streak_body, streak_stale = _makeup(account, budget)
    auth_hit = auth_hit or makeup_auth

    redeem_result, redeem_auth = _redeem(account, budget)
    auth_hit = auth_hit or redeem_auth

    lottery_result = {"state": "skip", "message": "本次未抽奖"}
    if with_lottery:
        lottery_result, lottery_auth = _lottery(account, budget)
        auth_hit = auth_hit or lottery_auth

    buddy_result = {"state": "skip", "message": "本次未开盲盒"}
    if with_buddy:
        if travel_state != "idle":
            buddy_result = {
                "state": "skip",
                "message": "Buddy 盲盒跳过：旅行尚未结束或礼物尚未领到",
            }
        elif budget.expired():
            buddy_result = {"state": "skip", "message": "时间预算耗尽，开盲盒跳过"}
        else:
            buddy_result, buddy_auth = _buddy_open(account, budget)
            auth_hit = auth_hit or buddy_auth

    depart_result = {"state": "skip", "message": "今日不派出"}
    if travel_state == "idle" and not daily_limit:
        if budget.expired():
            depart_result = {"state": "skip", "message": "时间预算耗尽，派出跳过"}
        else:
            depart_result, depart_auth = _depart(account, budget)
            auth_hit = auth_hit or depart_auth
    elif travel_state == "idle" and daily_limit:
        depart_result = {"state": "skip", "message": "今日旅行名额已用完"}

    energy, streak_days = _display(account, budget, streak_body, streak_stale)

    return (
        {
            "travel": travel,
            "tasks": tasks_result,
            "makeup": makeup_result,
            "redeem": redeem_result,
            "lottery": lottery_result,
            "buddy": buddy_result,
            "depart": depart_result,
            "energy": energy,
            "streakDays": streak_days,
        },
        auth_hit,
    )


def _display(account, budget, streak_body, streak_stale):
    """能量与连签天数，纯展示值。拿不到就不显示，不计失败。"""
    energy = None
    if not budget.expired():
        st, bd, _ = _call(account, budget, "GET", f"{GROWTH}/energy")
        if http.api_success(st, bd):
            energy = http.dig(bd, "balance")

    streak_days = None
    if streak_body is not None and not streak_stale:
        streak = http.dig(streak_body, "streak")
        if isinstance(streak, dict):
            streak_days = streak.get("days")
    elif not budget.expired():
        st, bd, _ = _call(account, budget, "GET", f"{GROWTH}/streak")
        if http.api_success(st, bd):
            streak = http.dig(bd, "streak")
            if isinstance(streak, dict):
                streak_days = streak.get("days")
    return energy, streak_days


_STATE_LABEL = {
    "ok": "成功",
    "skip": "跳过",
    "fail": "失败",
    "auth": "登录失效",
}


def _finish(result: dict, patch) -> dict:
    """把七段结果拼成一句汇报 + 结构化明细。

    ``skip`` 通常不值得说（「今日未出发」「没有抽奖次数」），但有两种 skip 必须透出：
    一是**接取被拒**这类带根因的（用户需要知道要先领养 Buddy），二是待体验清单的
    规模。否则最终汇报只剩一句「无可领取项」，用户完全看不出为什么没领到。
    """
    parts: list[str] = []
    earned = 0
    hard_failed = 0
    acted = 0
    notable_skip: list[str] = []
    for key, label in (
        ("travel", "旅行"),
        ("tasks", "任务"),
        ("makeup", "补登"),
        ("redeem", "兑换"),
        ("lottery", "抽奖"),
        ("buddy", "盲盒"),
        ("depart", "派出"),
    ):
        section = result.get(key) or {}
        state = section.get("state")
        message = _text(section.get("message"))
        if state == "ok":
            acted += 1
            parts.append(f"{label}·{message}")
            earned += http.as_int(section.get("credit"))
        elif state == "fail":
            hard_failed += 1
            parts.append(f"{label}·{message}")
        elif state == "auth":
            parts.append(f"{label}·{message}")
        elif state == "skip" and _worth_reporting(section):
            notable_skip.append(f"{label}·{message}")

    parts.extend(notable_skip)

    tail: list[str] = []
    energy = result.get("energy")
    if energy is not None:
        tail.append(f"能量 {http.as_int(energy)}")
    streak_days = result.get("streakDays")
    if streak_days is not None:
        tail.append(f"连签 {http.as_int(streak_days)} 天")
    if earned:
        tail.append(f"本轮 +{earned} 积分")

    message = "；".join(parts) if parts else "成长中心无可领取项"
    if tail:
        message += "（" + "，".join(tail) + "）"

    return {
        "ok": hard_failed == 0,
        "message": message,
        "sections": result,
        "credit": earned,
        "energy": http.as_int(energy) if energy is not None else None,
        "streakDays": http.as_int(streak_days) if streak_days is not None else None,
        "acted": acted,
        "failed": hard_failed,
        "patch": patch,
    }


def _worth_reporting(section: dict) -> bool:
    """这条 skip 值不值得写进汇报。

    「今日未出发」「没有抽奖次数」是常态，写进去只会把真正的问题淹没。
    但三类必须透出：
      - 带根因的失败（「N 项接取被拒」）
      - 待解锁状态（进阶任务还没开，用户需要知道差什么）
      - 待体验清单的规模（说明还有多少可做）
    """
    message = _text(section.get("message"))
    if "被拒" in message or "失败" in message:
        return True
    if section.get("locked") or "待解锁" in message:
        return True
    if section.get("pending"):
        return True
    return False


def run_travel_only(account, budget) -> dict:
    """只做旅行：领已有礼物 + 派出今天这一次。

    轮询模式专用。为什么必须单独存在：**旅行礼物要在下一次派出之前领**，
    否则那份奖励作废（每天只有一个派出名额）。而签到一天只需一个请求，
    没必要为了「领一份礼物」把任务/补登/兑换/抽奖那 20 个请求全跑一遍。

    代价是 3 个请求（status → claim → config → depart），收益是「礼物不丢」。
    """
    result, needs_auth = _travel_only(account, budget)
    if not needs_auth or not accounts.can_refresh(account):
        return _finish_travel(result, None)

    try:
        patch = accounts.refresh_account(account, budget)
    except RuntimeError as error:
        return {"ok": False, "message": str(error), "patch": None}

    fresh = dict(account)
    auth = dict(account.get("auth") or {})
    auth.update(patch)
    fresh["auth"] = auth
    retried, _ = _travel_only(fresh, budget)
    return _finish_travel(retried, patch)


def _travel_only(account, budget) -> tuple[dict, bool]:
    travel, travel_state, daily_limit = _travel(account, budget)
    auth_hit = travel.get("state") == "auth"

    depart_result = {"state": "skip", "message": "今日不派出"}
    if travel_state == "idle" and not daily_limit:
        if budget.expired():
            depart_result = {"state": "skip", "message": "时间预算耗尽，派出跳过"}
        else:
            depart_result, depart_auth = _depart(account, budget)
            auth_hit = auth_hit or depart_auth
    elif travel_state == "idle" and daily_limit:
        depart_result = {"state": "skip", "message": "今日旅行名额已用完"}

    return {"travel": travel, "depart": depart_result}, auth_hit


def _finish_travel(result: dict, patch) -> dict:
    """旅行两段的汇报。空跑（在途 / 名额用完）不产生噪音。"""
    parts: list[str] = []
    earned = 0
    hard_failed = 0
    acted = 0
    for key, label in (("travel", "旅行"), ("depart", "派出")):
        section = result.get(key) or {}
        state = section.get("state")
        message = _text(section.get("message"))
        if state == "ok":
            acted += 1
            parts.append(f"{label}·{message}")
            earned += http.as_int(section.get("credit"))
        elif state == "fail":
            hard_failed += 1
            parts.append(f"{label}·{message}")
        elif state == "auth":
            parts.append(f"{label}·{message}")

    if earned:
        parts.append(f"本轮 +{earned} 积分")
    return {
        "ok": hard_failed == 0,
        "message": "；".join(parts) if parts else "旅行无可领取项",
        "sections": result,
        "credit": earned,
        "acted": acted,
        "failed": hard_failed,
        "patch": patch,
    }


def pending_only(account, budget) -> dict:
    """只读地拉一次待体验任务（顺带接取未接取的），供 Web 页展示。"""
    result, auth = _tasks(account, budget)
    if auth:
        return {"ok": False, "message": "登录态失效", "pending": []}
    return {
        "ok": result.get("state") != "fail",
        "message": result.get("message"),
        "pending": result.get("pending") or [],
    }
