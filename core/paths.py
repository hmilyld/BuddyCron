"""路径与落盘：所有持久化文件的唯一入口。

数据目录默认 ``~/.buddycron``（可用 ``BUDDYCRON_HOME`` 覆盖），**刻意不放在仓库里**：
accounts.json 含明文 token，放在代码目录下迟早会被 git add 进去。

权限：目录 0700，文件 0600。写入走「同目录临时文件 + os.replace」，且临时文件在
创建那一刻就带 0600 —— 先写后 chmod 会留出一个可被读取的窗口。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "buddycron"


def home_dir() -> Path:
    override = os.environ.get("BUDDYCRON_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / f".{APP_NAME}"


def data_dir() -> Path:
    return home_dir()


def accounts_file() -> Path:
    return data_dir() / "accounts.json"


def config_file() -> Path:
    return data_dir() / "config.json"


def state_file() -> Path:
    return data_dir() / "state.json"


def lock_file() -> Path:
    return data_dir() / "run.lock"


def logs_dir() -> Path:
    return data_dir() / "logs"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        # 非 POSIX 或只读挂载：权限做不到就退让，不因为 chmod 失败把功能整个弄挂。
        pass


def write_private(path: Path, content: str) -> None:
    """原子写入 + 0600。临时文件在创建时就带权限，避免 rename 前的可读窗口。"""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default):
    import json

    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default
