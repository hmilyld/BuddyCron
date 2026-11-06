"""从 CHANGELOG.md 抽取某个版本的段落，供 Release 使用。

为什么不用 GitHub 的 generate_release_notes：
    - 它按 commit 列表生成，读者看到的是「fix(ci): 镜像地址用小写」这种
      开发者视角的碎片，而不是「这个版本带来了什么」
    - 提交信息本来就有 Conventional Commits 的结构，但那是给机器看的；
      CHANGELOG 是给人看的，能写「为什么」和「有什么影响」

用法：
    python3 scripts/changelog.py 0.1.0        # 打印该版本段落
    python3 scripts/changelog.py 0.1.0 --body # 同上，供 CI 写入 Release
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"


def extract(version: str) -> str:
    """抽出 ``## [version]`` 到下一个 ``## [`` 之间的正文。

    找不到就返回空串，由调用方决定是报错还是降级。
    """
    if not CHANGELOG.exists():
        return ""
    text = CHANGELOG.read_text(encoding="utf-8")

    # 匹配 "## [0.1.0]" 或 "## [0.1.0] - 2026-11-06"，版本号里的点要转义
    pattern = re.compile(
        rf"^##\s*\[{re.escape(version)}\][^\n]*\n(.*?)(?=^##\s*\[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(1).strip()


def versions() -> list[str]:
    """本文件里出现过的版本号，最新的在前。"""
    if not CHANGELOG.exists():
        return []
    text = CHANGELOG.read_text(encoding="utf-8")
    return re.findall(r"^##\s*\[([0-9]+\.[0-9]+\.[0-9]+)\]", text, re.MULTILINE)


def main() -> int:
    parser = argparse.ArgumentParser(description="从 CHANGELOG 抽取版本说明")
    parser.add_argument("version", help="版本号，例如 0.1.0")
    parser.add_argument(
        "--body", action="store_true", help="原样输出（供 CI 写入 Release 正文）"
    )
    args = parser.parse_args()

    body = extract(args.version)
    if not body:
        print(
            f"CHANGELOG.md 里没有 [{args.version}] 段落。"
            f"已有版本：{', '.join(versions()) or '（无）'}",
            file=sys.stderr,
        )
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
