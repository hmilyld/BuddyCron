"""版本号的唯一来源。

为什么单独一个文件而不是写进 `pyproject.toml` 或 `package.json`：
    - 这个项目不是可安装的包（`pip install` 不适用），没有 pyproject 可读
    - package.json 只服务界面构建，不该成为后端版本的权威
    - 容器镜像、CLI、Web 页面三处都要显示版本，必须只有一个来源

**改版本号只改这一行**，`cli.py`、`web/server.py`、Docker 构建都从这里读。

发布流程（见 .github/workflows/release.yml）：
    改这里的 VERSION → git commit → git tag v<VERSION> → git push --tags
CI 会校验 tag 与这个常量一致，不一致直接失败——避免出现「镜像标着 0.3.0
但代码里是 0.2.9」这种对不上的情况。
"""

VERSION = "0.1.2"

# 给界面和 API 用的简短标识。带 v 前缀是为了显示时一眼看出是版本号。
def display() -> str:
    return f"v{VERSION}"
