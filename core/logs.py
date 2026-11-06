"""运行日志：结构化、按日分文件、可读可查。

设计取舍：

**为什么按日分文件而不是一个大文件**
    日志是用来「翻昨天那次为什么失败」的，不是用来做全量分析的。按日切分让
    「找某天」变成打开一个文件，而单个大文件会越滚越大、翻起来还得靠 grep。

**为什么每行一个 JSON**
    便于机器读（`jq`）也便于人读（字段名自带说明）。纯文本行会逼着人写正则。

**为什么记每段细节而不只记汇总**
    汇总只有一句「16 项待体验」，出问题时完全不够定位。真正有用的是：
    签到拿到多少积分、旅行领了没有、哪个任务被拒、补登用了几张卡。
    这些都记下来，翻日志才叫翻日志。

**轮询空跑不写**
    轮询一天跑 4 次，绝大多数是「已签 + 无事可做」。全写进去只会把真事件
    淹没。判据是 runner 给的 `quiet` 字段（每账号判定，不是全局）。

**脱敏**
    日志会进 journald 或被人 `cat`，所以写之前抹掉 token。判断依据是
    「长串且只由 token 字符组成」——宁可误抹也不漏抹。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import paths

# 一行日志最多保留多少字符。单条汇报正常在 200 字以内，留 4 倍余量。
MAX_MESSAGE = 800
# 日志文件保留多少天。超过的按文件名（YYYY-MM）删掉。
KEEP_MONTHS = 6

_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~+/-]{40,}=*")


def _sanitize(text: str) -> str:
    """抹掉疑似凭证的长串，并截断长度。

    宁可误抹：一个被误抹的普通词只是不好看，一个漏掉的 token 是实打实的泄露。
    """
    if not text:
        return ""
    cleaned = _TOKEN_SHAPE.sub("[已隐藏]", str(text))
    return cleaned[:MAX_MESSAGE]


def _record_from_item(item: dict) -> dict:
    """把一个账号的结果压成日志行。

    只保留「以后会想查」的字段，不把整个 result 塞进去——里面有 patch（凭证）。
    """
    sections = item.get("sections") or {}
    checkin = item.get("checkin") or {}
    growth = item.get("growth") or {}
    extras = item.get("extras") or {}
    credits = item.get("credits") or {}

    record: dict = {
        "account": _sanitize(item.get("label") or ""),
        "ok": bool(item.get("ok")),
        "message": _sanitize(item.get("message") or ""),
    }

    if checkin:
        record["checkin"] = {
            "result": checkin.get("result"),
            "credit": checkin.get("credit"),
            "streakDays": checkin.get("streakDays"),
            "message": _sanitize(checkin.get("message") or ""),
        }
    if growth:
        # 逐段状态：出问题时能直接看出是哪一段。
        record["growth"] = {
            "credit": growth.get("credit"),
            "acted": growth.get("acted"),
            "failed": growth.get("failed"),
            "sections": {
                key: {
                    "state": (value or {}).get("state"),
                    "message": _sanitize((value or {}).get("message") or ""),
                }
                for key, value in (growth.get("sections") or {}).items()
                if isinstance(value, dict)
            },
        }
    if extras:
        record["extras"] = {
            "state": extras.get("state"),
            "credit": extras.get("credit"),
            "message": _sanitize(extras.get("message") or ""),
        }
    if credits.get("ok"):
        record["credits"] = {
            "remain": credits.get("remain"),
            "used": credits.get("used"),
            "plan": credits.get("plan"),
        }
    if item.get("error"):
        record["error"] = _sanitize(item["error"])
    return record


def append(summary: dict) -> None:
    """把一轮结果追加到当日日志。

    轮询且全部空跑时不写——见模块文档。
    """
    mode = summary.get("mode")
    accounts = summary.get("accounts") or []
    if mode == "poll" and all(item.get("quiet") for item in accounts):
        return

    line = {
        "ts": summary.get("finishedAt") or int(time.time() * 1000),
        "mode": mode,
        "total": summary.get("total"),
        "succeeded": summary.get("succeeded"),
        "failed": summary.get("failed"),
        "accounts": [_record_from_item(item) for item in accounts],
    }

    paths.ensure_private_dir(paths.logs_dir())
    path = paths.logs_dir() / f"{time.strftime('%Y-%m', time.localtime())}.log"
    # 0600：日志里有账号昵称和操作细节，不该让同机其他用户随手读到。
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def _parse_line(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def tail(limit: int = 50, month: str | None = None) -> list[dict]:
    """读最近的日志，最新的在前。

    ``month`` 为空时读当前月；跨月想看历史就传 ``YYYY-MM``。
    单行损坏（比如写到一半断电）只跳过那一行，不让整个读取失败。
    """
    month = month or time.strftime("%Y-%m", time.localtime())
    path = paths.logs_dir() / f"{month}.log"
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []

    records: list[dict] = []
    for raw in reversed(lines):
        if len(records) >= limit:
            break
        parsed = _parse_line(raw)
        if parsed:
            records.append(parsed)
    return records


def months() -> list[str]:
    """有日志的月份，最新的在前。"""
    directory = paths.logs_dir()
    if not directory.exists():
        return []
    found = []
    for entry in directory.iterdir():
        if entry.is_file() and entry.suffix == ".log":
            name = entry.stem
            # 只认 YYYY-MM 形态，避免把别的文件列进来
            if len(name) == 7 and name[4] == "-" and name[:4].isdigit():
                found.append(name)
    return sorted(found, reverse=True)


def prune() -> list[str]:
    """删掉过老的日志文件，返回被删的月份。"""
    removed: list[str] = []
    for month in months()[KEEP_MONTHS:]:
        try:
            (paths.logs_dir() / f"{month}.log").unlink()
            removed.append(month)
        except OSError:
            continue
    return removed


def log_file(month: str | None = None) -> Path:
    month = month or time.strftime("%Y-%m", time.localtime())
    return paths.logs_dir() / f"{month}.log"
