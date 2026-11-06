"""多账号编排：轮次锁、每账号预算配额、每账号 quiet、结果落库。

88lin 的实现是全局单例，直接照抄会在多账号下出错。四处必改：

    _sensitive_values  全局脱敏集合 → 每账号一份
    _budget_seconds    全局预算     → 全局上限 + 每账号配额
    quiet              全局空跑     → 每账号判定
    无锁                              → 跨进程文件锁 + 进程内闸

「quiet 每账号判定」的实际意义：5 个账号 4 个空跑、1 个真领到，只写那一行。
不改的话轮询日志会被「Buddy 旅行中」淹没，真出问题时翻不出来。
"""

from __future__ import annotations

import json
import time

from . import accounts, checkin, credits, extras, growth, http, logs, paths

# 全局预算上限（秒）。systemd 任务的 TimeoutStartSec 必须比它大。
#
# 这些数字是按「单账号跑完成长中心七段要多久」反推的，不是随便定的：
#   单账号实测约 20 个请求，正常网络下 20~40 秒跑完；网络抖动时重试会翻倍。
#   给 120 秒配额，7 个账号就是 840 秒——留出 40% 余量给最慢的那一个。
#
# 旧值 420 / 90 只够 4~5 个账号，第 5 个之后会被跳过。账号数超过这个数时
# 必须同步调大，否则「后面的账号今天没签上」会静默发生。
DAILY_BUDGET = 900
POLL_BUDGET = 300
# 单账号配额。串行时每账号至少留这么多，否则前面的账号超时会把后面饿死。
PER_ACCOUNT_BUDGET = 120


class CycleGuard:
    """进程内轮次闸。

    后台 timer 与手动 ``buddycron run`` 撞上时，整轮直接跳过并明确回报，
    而不是两个流程互相覆盖 accounts.json 的写入。
    """

    _running = False

    @classmethod
    def acquire(cls) -> bool:
        if cls._running:
            return False
        cls._running = True
        return True

    @classmethod
    def release(cls) -> None:
        cls._running = False


def _display_name(account: dict) -> str:
    info = account.get("account") or {}
    return str(info.get("nickname") or info.get("uid") or account.get("id") or "未知账号")


def _mask(text: str, token: str) -> str:
    """单账号脱敏：只抹掉本账号自己的 token。

    全局集合会让 A 的 token 把 B 汇报里的同名字符串也替换掉。
    """
    if not token:
        return text
    return text.replace(token, "[REDACTED]")


def run_one(account: dict, budget: http.Budget, mode: str) -> dict:
    """跑一个账号。

    mode:
        ``daily``   签到 + 成长全套 + extras
        ``poll``    签到；已签则只做旅行（领礼物 + 派出），未签则跑全套
        ``checkin`` 只签到
    """
    account_budget = budget.child(PER_ACCOUNT_BUDGET)
    token = str((account.get("auth") or {}).get("accessToken") or "")
    label = _display_name(account)
    result: dict = {"id": account.get("id"), "label": label, "ok": True, "sections": {}}

    patch = None
    try:
        checkin_result = checkin.run(account, account_budget)
        patch = checkin_result.get("patch") or patch
        result["checkin"] = _strip(checkin_result)
        result["ok"] = bool(checkin_result.get("ok"))

        if checkin_result.get("ok"):
            # poll：已签就只跑旅行这一段，省掉成长中心其余 20 个请求。
            # 旅行必须跑 —— 礼物要在下一次派出前领，否则那份奖励作废。
            if mode == "poll":
                growth_result = growth.run_travel_only(account, account_budget)
                patch = growth_result.get("patch") or patch
                result["growth"] = _strip(growth_result)
            elif mode == "daily":
                growth_result = growth.run_auto(account, account_budget)
                patch = growth_result.get("patch") or patch
                result["growth"] = _strip(growth_result)

                extras_result, extras_auth = extras.run(account, account_budget)
                result["extras"] = _strip(extras_result)
                if extras_auth:
                    result["ok"] = False

            # 积分是只读查询，任何模式都顺手查一次：界面要显示当前余额，
            # 而它不消耗任何东西，多这一个请求没有代价。
            credits_result = credits.fetch(account, account_budget)
            result["credits"] = _strip(credits_result)
    except Exception as error:  # noqa: BLE001 - 单账号异常不能拖垮整轮
        result["ok"] = False
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if patch:
            accounts.patch_account(str(account.get("id") or ""), {"auth": _merged(account, patch)})

    result["message"] = _compose(result, mode)
    result["message"] = _mask(result["message"], token)
    result["quiet"] = _is_quiet(result)
    _record(account, result)
    return result


def _merged(account: dict, patch: dict) -> dict:
    auth = dict(account.get("auth") or {})
    auth.update(patch)
    return auth


def _strip(payload: dict) -> dict:
    """落盘前去掉 patch（凭证不重复存一份）。"""
    return {k: v for k, v in payload.items() if k != "patch"}


def _compose(result: dict, mode: str) -> str:
    """把各段拼成一句人话。

    成长中心那句不能只在 ``acted`` 时才输出：``skip`` 里也有必须让用户看到的
    信息（「16 项接取被拒（需先领养 Buddy）」「N 项待体验」）。只在成功时拼，
    用户会看到「今日已签过」就以为一切正常，完全不知道还有 1800 积分拿不到。
    """
    parts: list[str] = []
    checkin_result = result.get("checkin") or {}
    if checkin_result:
        parts.append(str(checkin_result.get("message") or ""))
    if mode == "daily":
        growth_result = result.get("growth") or {}
        growth_message = str(growth_result.get("message") or "")
        if growth_message and growth_message != "成长中心无可领取项":
            parts.append(growth_message)
        extras_result = result.get("extras") or {}
        extras_message = str(extras_result.get("message") or "")
        if extras_result.get("state") == "ok" and extras_message:
            parts.append(extras_message)
    if result.get("error"):
        parts.append(f"异常：{result['error']}")
    return "；".join(part for part in parts if part)


def _is_quiet(result: dict) -> bool:
    """这一轮有没有值得一提的事。

    已签 + 成长无可领 + 无 extras 到账 = 空跑，轮询不落盘。
    """
    checkin_result = result.get("checkin") or {}
    if checkin_result.get("result") not in ("ALREADY", "INACTIVE"):
        return False
    growth_result = result.get("growth") or {}
    if growth_result.get("acted") or growth_result.get("failed"):
        return False
    extras_result = result.get("extras") or {}
    if extras_result.get("state") == "ok":
        return False
    return not result.get("error")


def _record(account: dict, result: dict) -> None:
    """把签到结果与积分写回账号记录，供 Web 页显示。"""
    checkin_result = result.get("checkin") or {}
    credits_result = result.get("credits") or {}
    patch: dict = {}

    if checkin_result:
        patch.update(
            {
                "last_checkin_at": accounts.now_ms(),
                "last_checkin_date": accounts.today_local(),
                "last_checkin_result": str(checkin_result.get("result") or "error"),
                "last_checkin_message": str(checkin_result.get("message") or "")[:240],
            }
        )
        # 本周印章是界面的主角，顺手落库：否则每次打开 Web 页都要重新查一次接口。
        week = checkin_result.get("weekProgress")
        if isinstance(week, list) and len(week) == 7:
            patch["week_progress"] = [bool(item) for item in week]
        dates = checkin_result.get("checkinDates")
        if isinstance(dates, list):
            patch["checkin_dates"] = [str(item) for item in dates][:400]

    if credits_result.get("ok"):
        patch["credits"] = {
            "total": credits_result.get("total"),
            "remain": credits_result.get("remain"),
            "used": credits_result.get("used"),
            "plan": credits_result.get("plan"),
            "groups": credits_result.get("groups"),
            "packages": credits_result.get("packages"),
            "nearestExpiry": credits_result.get("nearestExpiry"),
            "at": accounts.now_ms(),
        }

    if patch:
        accounts.patch_account(str(account.get("id") or ""), patch)


def run_cycle(mode: str = "daily", app=None) -> dict:
    """所有账号依次跑一轮。

    ``mode`` 取 ``daily`` / ``poll`` / ``checkin``：
        daily   签到 + 成长全套 + extras
        poll    先查签到；已签就只做「领旅行礼物 + 派出」，未签才跑全套
        checkin 只签到，不碰成长

    ``poll`` 为什么不是「只签到」：签到一天只有一次，而**旅行礼物必须在下一次
    派出之前领**，否则那份奖励就废了（每天只有一个派出名额）。如果 00:05 那次
    因为关机/断网失败，只签到的轮询会把礼物一直压到下一个 daily —— 压一整天
    等于白丢一份奖励。所以轮询在签到成功后顺带把旅行这一段跑掉，代价是 3 个
    请求，收益是「礼物不丢」。
    """
    if not CycleGuard.acquire():
        return {"skipped": True, "message": "另一轮正在运行，本次跳过", "accounts": []}
    try:
        budget = http.Budget(POLL_BUDGET if mode == "poll" else DAILY_BUDGET)
        items: list[dict] = []
        for account in accounts.load_accounts():
            if budget.expired():
                items.append(
                    {
                        "id": account.get("id"),
                        "label": _display_name(account),
                        "ok": False,
                        "message": "时间预算耗尽，本轮跳过",
                        "quiet": False,
                    }
                )
                continue
            item = run_one(account, budget, mode)
            items.append(item)

        succeeded = sum(1 for item in items if item.get("ok"))
        summary = {
            "mode": mode,
            "total": len(items),
            "succeeded": succeeded,
            "failed": len(items) - succeeded,
            "accounts": items,
            "finishedAt": accounts.now_ms(),
        }
        _write_state(summary)
        return summary
    finally:
        CycleGuard.release()


def _write_state(summary: dict) -> None:
    paths.write_private(
        paths.state_file(),
        json.dumps(summary, ensure_ascii=False, indent=2),
    )


def read_state() -> dict:
    data = paths.read_json(paths.state_file(), {})
    return data if isinstance(data, dict) else {}


def append_log(summary: dict) -> None:
    """写日志。委托给 core.logs —— 那里管格式、脱敏与按日切分。"""
    logs.append(summary)
