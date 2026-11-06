"""本地登录口令。

需求很明确：**只要一个密码，不要用户名**。这是一个单人自用的本地工具，
加账号体系只会让登录变麻烦，而它要防的只是「同一台机器上的其他浏览器页面 /
其他用户误入」。

设计取舍：
    - 首次启动**自动生成**一个 8 位口令写进 config.json（0600），
      并在终端打印一次。这样「默认就有」，不用你手动配。
    - 口令用 PBKDF2-HMAC-SHA256 校验，不存明文。虽然配置文件本来就在 0600 下，
      但密码散列化是零成本的，没理由留明文。
    - 会话用签名 cookie：`HMAC(secret, expiry)`。secret 同样在 config.json 里，
      重启后已登录的会话仍然有效（不用每天重登）。
    - 失败次数限制：同一 IP 连续 5 次失败后锁 5 分钟。防的是「本地端口被扫」，
      不是防专业攻击——本地服务本来就在你的机器上。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

from . import paths

CONFIG_NAME = "config.json"
COOKIE_NAME = "bc_session"
SESSION_SECONDS = 7 * 24 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 300
PBKDF2_ROUNDS = 200_000

# 进程内的失败计数。key 是客户端标识，value 是 (失败次数, 首次失败时间)。
_fails: dict[str, tuple[int, float]] = {}


def _config() -> dict:
    data = paths.read_json(paths.config_file(), {})
    return data if isinstance(data, dict) else {}


def _save_config(config: dict) -> None:
    paths.write_private(paths.config_file(), json.dumps(config, ensure_ascii=False, indent=2))


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()


def ensure_config() -> dict:
    """读配置；缺密码或密钥就生成一个。

    返回的 ``created`` 字段表示本次是否新生成了密码——调用方据此决定要不要打印。
    """
    config = _config()
    created = False

    if not config.get("password_hash") or not config.get("password_salt"):
        password = _random_password()
        salt = secrets.token_hex(16)
        config["password_hash"] = _hash(password, salt)
        config["password_salt"] = salt
        # 明文只在「用户还没成功登录过一次」期间保留，登录成功后立刻清除。
        # 理由：密码是散列存的，用户忘了就再也看不到，只能重置——而首次使用时
        # 恰恰最可能没记下来。留到首次登录成功，两头都照顾到了。
        config["password_hint"] = password
        created = True

    if not config.get("session_secret"):
        config["session_secret"] = secrets.token_hex(32)

    if created or "session_secret" not in config:
        _save_config(config)

    return {**config, "created": created}


def _random_password() -> str:
    """8 位口令，去掉容易看错的字符（0/O、1/l/I）。"""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def take_new_password() -> str | None:
    """取出「刚生成」的口令并清掉，保证只在终端打印一次。

    留着它等于在磁盘上多存一份明文，没必要。
    """
    config = _config()
    hint = config.get("password_hint")
    if not hint:
        return None
    config.pop("password_hint", None)
    _save_config(config)
    return str(hint)


def current_password_hint() -> str | None:
    """还没登录成功时返回明文口令，供 ``cli.py passwd --show`` 查看。

    登录成功一次后就会被清掉，所以这不是长期后门。
    """
    config = ensure_config()
    hint = config.get("password_hint")
    return str(hint) if hint else None


def clear_password_hint() -> None:
    """登录成功后调用：从此不再保留明文。"""
    config = _config()
    if config.pop("password_hint", None) is not None:
        _save_config(config)


def verify(password: str) -> bool:
    config = ensure_config()
    salt = str(config.get("password_salt") or "")
    expected = str(config.get("password_hash") or "")
    if not salt or not expected:
        return False
    return hmac.compare_digest(_hash(password, salt), expected)


def locked(client: str) -> int:
    """还要等多少秒才允许再试。0 表示没锁。"""
    entry = _fails.get(client)
    if not entry:
        return 0
    count, first = entry
    if count < MAX_FAILS:
        return 0
    elapsed = time.monotonic() - first
    if elapsed >= LOCK_SECONDS:
        _fails.pop(client, None)
        return 0
    return int(LOCK_SECONDS - elapsed)


def note_failure(client: str) -> None:
    count, first = _fails.get(client, (0, time.monotonic()))
    _fails[client] = (count + 1, first)


def clear_failures(client: str) -> None:
    _fails.pop(client, None)


def issue_cookie() -> str:
    """签一个带过期时间的会话值。"""
    config = ensure_config()
    secret = str(config["session_secret"])
    expiry = int(time.time()) + SESSION_SECONDS
    payload = str(expiry)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{signature}"


def check_cookie(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    payload, signature = value.rsplit(".", 1)
    config = ensure_config()
    secret = str(config.get("session_secret") or "")
    if not secret:
        return False
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, signature):
        return False
    try:
        return int(payload) > int(time.time())
    except ValueError:
        return False


def reset_password() -> str:
    """重置口令并返回新值。供 ``cli.py passwd`` 使用。"""
    config = _config()
    password = _random_password()
    salt = secrets.token_hex(16)
    config["password_hash"] = _hash(password, salt)
    config["password_salt"] = salt
    # 重设后同样保留明文，直到用户登录成功一次 —— 否则刚重设就看不到，
    # 而重设往往正是「我忘了密码」的场景。
    config["password_hint"] = password
    # 重置后旧会话应当失效：换掉签名密钥。
    config["session_secret"] = secrets.token_hex(32)
    _save_config(config)
    return password


def set_password(password: str) -> None:
    """设定指定口令（供 ``cli.py passwd <密码>``）。

    用户自己指定的口令不需要留明文——他知道自己设了什么。
    """
    config = _config()
    salt = secrets.token_hex(16)
    config["password_hash"] = _hash(password, salt)
    config["password_salt"] = salt
    config.pop("password_hint", None)
    config["session_secret"] = secrets.token_hex(32)
    _save_config(config)
