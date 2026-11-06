"""doctor：只读核对接口契约。

**必须卡在开放写操作之前**。所有端点契约都是从官方前端 bundle 读出来的，没有
拿真 token 打过；写操作一旦用错契约，可能空耗补登卡、抽错奖、重复提交。

doctor 只调只读接口，把原始响应 dump 出来供人工核对：
    streak / heatmap / lottery/chances / buddy/quota / buddy/travel/status
    redeem/summary / tasks / energy / buddy/agreement（GET）
    check-gift-claimed / compensation-status

用法：
    python3 cli.py doctor              # 第一个账号
    python3 cli.py doctor --all        # 所有账号
    python3 cli.py doctor --id uid:xxx # 指定账号
"""

from __future__ import annotations

import json

from . import accounts, http

# (分组, 方法, 路径)。方法必须逐个写死：成长中心大部分是 GET，签到和
# billing/meter 系列是 POST，混用会得到一片假的 404。
READ_ONLY = (
    ("growth", "GET", "/v2/activity/growth/streak"),
    ("growth", "GET", "/v2/activity/growth/heatmap"),
    ("growth", "GET", "/v2/activity/growth/lottery/chances"),
    ("growth", "GET", "/v2/activity/growth/buddy/quota"),
    ("growth", "GET", "/v2/activity/growth/buddy/travel/status"),
    ("growth", "GET", "/v2/activity/growth/buddy/travel/config"),
    ("growth", "GET", "/v2/activity/growth/redeem/summary"),
    ("growth", "GET", "/v2/activity/growth/tasks"),
    ("growth", "GET", "/v2/activity/growth/energy"),
    ("growth", "GET", "/v2/activity/growth/buddy/info"),
    ("growth", "GET", "/v2/activity/growth/profile"),
    ("billing", "POST", "/billing/meter/check-gift-claimed"),
    ("billing", "POST", "/billing/meter/compensation-status"),
    ("billing", "POST", "/billing/meter/get-user-resource-summary"),
    ("billing", "POST", "/v2/billing/meter/checkin-activity-status"),
)


def _hosts(account: dict, group: str) -> list[str]:
    endpoint = (account.get("auth") or {}).get("endpoint") or ""
    host = endpoint.replace("https://", "").strip("/") if endpoint else ""
    out: list[str] = []
    if host in http.ALLOWED_HOSTS:
        out.append(host)
    pool = http.ACTIVITY_HOSTS if group == "growth" else http.BILLING_HOSTS
    out.extend(item for item in pool if item not in out)
    return out


def _headers(account: dict, group: str) -> dict[str, str]:
    headers = http.build_headers(account)
    if group == "growth":
        headers["X-Client-Platform"] = "workbuddy"
    else:
        headers["X-Client-Platform"] = "web"
    return headers


def probe_account(account: dict, budget: http.Budget) -> dict:
    """对单个账号跑一遍只读探测，返回每个端点的原始响应。"""
    label = (account.get("account") or {}).get("nickname") or (
        account.get("account") or {}
    ).get("uid") or account.get("id")
    out: dict = {"id": account.get("id"), "label": label, "probes": [], "ok": True}

    working = account
    refreshed = False

    for group, method, path in READ_ONLY:
        if budget.expired():
            out["probes"].append(
                {"path": path, "method": method, "status": "budget", "host": "", "body": None}
            )
            continue
        row = _probe(working, group, method, path, budget)
        # 401 且还有 refresh token：刷一次再重试同一批（只做一次，避免连环刷新）
        if row["status"] == 401 and not refreshed and accounts.can_refresh(working):
            try:
                patch = accounts.refresh_account(working, budget)
            except RuntimeError as error:
                out["ok"] = False
                out["refreshError"] = str(error)
                break
            fresh = dict(working)
            auth = dict(working.get("auth") or {})
            auth.update(patch)
            fresh["auth"] = auth
            working = fresh
            refreshed = True
            row = _probe(working, group, method, path, budget)
        if not isinstance(row["status"], int) or row["status"] >= 500 or row["status"] in (
            http.CODE_NO_NETWORK,
            http.CODE_BUDGET_OUT,
        ):
            out["ok"] = False
        out["probes"].append(row)

    return out


def _probe(account: dict, group: str, method: str, path: str, budget: http.Budget) -> dict:
    headers = _headers(account, group)
    last: dict = {"path": path, "method": method, "status": http.CODE_NO_NETWORK, "host": "", "body": None}
    for host in _hosts(account, group):
        url = f"https://{host}{path}"
        if method == "GET":
            status, body = http.get(url, headers, budget=budget)
        else:
            status, body = http.post_retry(url, headers, {}, budget=budget)
        last = {"path": path, "method": method, "status": status, "host": host, "body": body}
        if http.api_success(status, body) or http.is_unauthorized(status, body):
            return last
    return last


def render(report: dict) -> str:
    lines = [f"账号 {report.get('label')}（{report.get('id')}）"]
    for row in report.get("probes") or []:
        status = row.get("status")
        mark = "OK " if isinstance(status, int) and 200 <= status < 300 else "!! "
        lines.append(
            f"  {mark}{status}  {row.get('method')}  {row.get('host')}  {row.get('path')}"
        )
        body = row.get("body")
        if isinstance(body, dict):
            preview = json.dumps(body, ensure_ascii=False)[:400]
            lines.append(f"        {preview}")
    if report.get("refreshError"):
        lines.append(f"  刷新失败：{report['refreshError']}")
    return "\n".join(lines)
