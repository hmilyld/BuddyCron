"""CLI 入口。

    python3 cli.py doctor              只读核对接口契约（第一步必须跑这个）
    python3 cli.py run                 签到 + 成长 + extras（所有账号）
    python3 cli.py poll                只补签（未签才跑全套）
    python3 cli.py checkin             只签到
    python3 cli.py status              看账号列表与最近结果
    python3 cli.py web                 起本地 Web 页做登录与配置
    python3 cli.py remove <id>         删账号
"""

from __future__ import annotations

import argparse
import json
import sys

from core import accounts, doctor, http, paths, runner


def _pick(selector: str | None) -> list[dict]:
    all_accounts = accounts.load_accounts()
    if selector:
        picked = [a for a in all_accounts if str(a.get("id")) == selector]
        if not picked:
            print(f"找不到账号 {selector}", file=sys.stderr)
        return picked
    return all_accounts


def cmd_doctor(args) -> int:
    picked = _pick(args.id)
    if not picked:
        print("没有账号。先跑 `python3 cli.py web` 添加账号。", file=sys.stderr)
        return 2
    if not args.all:
        picked = picked[:1]

    budget = http.Budget(runner.DAILY_BUDGET)
    reports = []
    for account in picked:
        report = doctor.probe_account(account, budget)
        reports.append(report)
        print(doctor.render(report))
        print()

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0 if all(report.get("ok") for report in reports) else 1


def cmd_run(args) -> int:
    summary = runner.run_cycle("daily")
    runner.append_log(summary)
    _print_summary(summary)
    return 0 if summary.get("failed", 0) == 0 else 1


def cmd_poll(args) -> int:
    summary = runner.run_cycle("poll")
    runner.append_log(summary)
    if not args.quiet:
        _print_summary(summary)
    return 0 if summary.get("failed", 0) == 0 else 1


def cmd_checkin(args) -> int:
    summary = runner.run_cycle("checkin")
    runner.append_log(summary)
    _print_summary(summary)
    return 0 if summary.get("failed", 0) == 0 else 1


def _print_summary(summary: dict) -> None:
    if summary.get("skipped"):
        print(summary.get("message") or "已跳过")
        return
    for item in summary.get("accounts") or []:
        mark = "✓" if item.get("ok") else "✗"
        print(f"{mark} {item.get('label')}：{item.get('message')}")
    print(
        f"\n共 {summary.get('total', 0)} 个账号，"
        f"成功 {summary.get('succeeded', 0)}，失败 {summary.get('failed', 0)}"
    )


def cmd_status(args) -> int:
    all_accounts = accounts.load_accounts()
    if not all_accounts:
        print("没有账号。先跑 `python3 cli.py web` 添加账号。")
        return 0
    print(f"数据目录：{paths.data_dir()}")
    print(f"账号数：{len(all_accounts)}\n")
    for account in all_accounts:
        view = accounts.public_view(account)
        flags = []
        if view.get("needsRelogin"):
            flags.append("需重新登录")
        if view.get("agreedAt"):
            flags.append("已同意规则")
        mark = " ".join(flags)
        print(
            f"{view.get('nickname')}  {view.get('id')}\n"
            f"    今日：{view.get('lastCheckinResult') or '未签'}  "
            f"{view.get('lastCheckinMessage') or ''}"
            f"{('  [' + mark + ']') if mark else ''}"
        )
    state = runner.read_state()
    if state:
        print(
            f"\n最近一轮：{state.get('mode')}  "
            f"成功 {state.get('succeeded')}/{state.get('total')}"
        )
    return 0


def cmd_remove(args) -> int:
    if accounts.remove_account(args.id):
        print(f"已删除 {args.id}")
        return 0
    print(f"找不到账号 {args.id}", file=sys.stderr)
    return 1


def cmd_version(args) -> int:
    from core.version import VERSION

    print(VERSION)
    return 0


def cmd_web(args) -> int:
    from web.server import serve

    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_passwd(args) -> int:
    from core import auth

    if args.show:
        hint = auth.current_password_hint()
        if hint:
            print(f"当前口令：{hint}")
            print("（登录成功一次后明文会被清除，届时用 python3 cli.py passwd 重设）")
        else:
            print("明文已清除（你已成功登录过一次）。重设：python3 cli.py passwd")
        return 0

    if args.password:
        auth.set_password(args.password)
        print("口令已更新，旧的登录会话已失效。")
        return 0

    password = auth.reset_password()
    print()
    print("=" * 46)
    print(f"  新口令：{password}")
    print("  旧会话已失效，需要重新登录。")
    print("=" * 46)
    print()
    return 0


def cmd_credits(args) -> int:
    from core import credits, http

    picked = _pick(args.id)
    if not picked:
        print("没有账号。先跑 `python3 cli.py web` 添加账号。", file=sys.stderr)
        return 2

    budget = http.Budget(runner.DAILY_BUDGET)
    for account in picked:
        label = (account.get("account") or {}).get("nickname") or account.get("id")
        result = credits.fetch(account, budget)
        if not result.get("ok"):
            print(f"{label}：查询失败（{result.get('message')}）")
            continue
        print(
            f"{label}  {result.get('plan') or ''}\n"
            f"  可用 {result.get('remain')} / 已用 {result.get('used')}"
            f" / 周期总量 {round((result.get('remain') or 0) + (result.get('used') or 0), 2)}"
        )
        for group in result.get("groups") or []:
            print(
                f"    {group['name']}  "
                f"{group['remain']} / {group['total']}"
                + (f"（{group['count']} 个包）" if group.get("count") else "")
            )
        nearest = result.get("nearestExpiry")
        if nearest:
            days = nearest.get("daysLeft")
            tail = f"剩 {days} 天" if isinstance(days, int) and days >= 0 else "已过期"
            print(
                f"    最近到期：{nearest['expiresAt']}（{tail}）"
                f"  {nearest.get('name')} {nearest.get('remain')}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    from core.version import VERSION

    parser = argparse.ArgumentParser(
        prog="buddycron",
        description=f"WorkBuddy 多账号签到与积分工具（v{VERSION}）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="显示版本号")
    p_version.set_defaults(func=cmd_version)

    p_doctor = sub.add_parser("doctor", help="只读核对接口契约")
    p_doctor.add_argument("--id", help="指定账号 id")
    p_doctor.add_argument("--all", action="store_true", help="所有账号（默认只跑第一个）")
    p_doctor.add_argument("--json", action="store_true", help="额外输出 JSON")
    p_doctor.set_defaults(func=cmd_doctor)

    p_run = sub.add_parser("run", help="签到 + 成长 + extras")
    p_run.set_defaults(func=cmd_run)

    p_poll = sub.add_parser("poll", help="只补签（未签才跑全套）")
    p_poll.add_argument("--quiet", action="store_true", help="空跑不打印")
    p_poll.set_defaults(func=cmd_poll)

    p_checkin = sub.add_parser("checkin", help="只签到")
    p_checkin.set_defaults(func=cmd_checkin)

    p_status = sub.add_parser("status", help="看账号与最近结果")
    p_status.set_defaults(func=cmd_status)

    p_remove = sub.add_parser("remove", help="删除账号")
    p_remove.add_argument("id")
    p_remove.set_defaults(func=cmd_remove)

    p_web = sub.add_parser("web", help="起本地 Web 页")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8787)
    p_web.add_argument("--no-browser", action="store_true")
    p_web.set_defaults(func=cmd_web)

    p_passwd = sub.add_parser("passwd", help="查看或重设登录口令")
    p_passwd.add_argument("password", nargs="?", help="指定新口令；留空则随机生成")
    p_passwd.add_argument("--show", action="store_true", help="查看当前口令（若明文尚未清除）")
    p_passwd.set_defaults(func=cmd_passwd)

    p_credits = sub.add_parser("credits", help="查积分余额")
    p_credits.add_argument("--id", help="指定账号 id")
    p_credits.set_defaults(func=cmd_credits)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
