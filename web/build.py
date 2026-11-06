"""把 web/src 编译成 web/dist：Tailwind CSS + 单页 HTML。

为什么在构建期编译而不是运行时拉 CDN：
    这是本地工具，断网也要能用；而且 CDN 脚本意味着页面能执行任意远端代码，
    对一个持有 token 的本地服务来说不可接受。

产物（web/dist/）提交进仓库，所以运行侧**不需要 Node**——只有改界面时才要重编。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent
SRC = WEB / "src"
DIST = WEB / "dist"


def build() -> int:
    if not (SRC / "input.css").exists():
        print("缺少 web/src/input.css", file=sys.stderr)
        return 2

    DIST.mkdir(parents=True, exist_ok=True)

    css_out = DIST / "app.css"
    # 用项目本地的 tailwind CLI（不是 npx 远端拉取）：构建要可重复、可离线。
    local_cli = WEB.parent / "node_modules" / ".bin" / "tailwindcss"
    if local_cli.exists():
        cmd = [str(local_cli), "-i", "input.css", "-o", str(css_out), "--minify"]
    else:
        cmd = ["npx", "--yes", "@tailwindcss/cli", "-i", "input.css", "-o", str(css_out), "--minify"]
    print("编译 CSS：", " ".join(cmd))
    result = subprocess.run(cmd, cwd=SRC, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        return result.returncode
    print(f"  -> {css_out} ({css_out.stat().st_size} 字节)")

    for name in ("index.html", "login.html"):
        src = SRC / name
        if not src.exists():
            print(f"缺少 web/src/{name}", file=sys.stderr)
            return 2
        out = DIST / name
        shutil.copyfile(src, out)
        print(f"  -> {out} ({out.stat().st_size} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(build())
