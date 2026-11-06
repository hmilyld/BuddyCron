# 公网部署

这个服务持有你所有 WorkBuddy 账号的 token，**放到网上比放在本地危险得多**。
下面这套做法是「单用户 + 强 TLS + 不暴露任何内部细节」的最小可行方案。

## 一句话方案

```
浏览器 ──HTTPS──> Caddy（自动证书） ──HTTP──> 127.0.0.1:8787
```

**BuddyCron 永远只监听 127.0.0.1，TLS 由 Caddy 终止。** 不要反过来做——
在 Python 里自己实现 TLS 容易配错（证书链、协议版本、HSTS），而 Caddy 是
专门干这个的，一行配置就拿到 Let's Encrypt 自动续期。

## 为什么必须 HTTPS

明文 HTTP 下，登录口令和所有 API 请求（含 `Authorization: Bearer <token>`）
都会以明文经过链路。任何一个中间节点都能读到你的账号凭证。

代码里做了硬拦截：绑定到非回环地址时，**必须**同时设
`BUDDYCRON_REQUIRE_HTTPS=1`，否则直接拒绝启动。这不是提醒，是拦住。

## 步骤

### 1. 准备域名

一个指向你服务器的 A 记录，比如 `buddy.example.com`。
Let's Encrypt 需要能通过 80 端口验证域名归属。

### 2. 起服务（只绑回环）

```bash
BUDDYCRON_REQUIRE_HTTPS=1 \
BUDDYCRON_HOSTS=buddy.example.com \
BUDDYCRON_TRUST_PROXY=1 \
python3 cli.py web --host 127.0.0.1 --port 8787 --no-browser
```

三个环境变量各自的用途：

| 变量 | 作用 | 不设的后果 |
|---|---|---|
| `BUDDYCRON_REQUIRE_HTTPS` | 拒绝所有非 HTTPS 请求，cookie 加 `Secure` | 口令明文传输 |
| `BUDDYCRON_HOSTS` | 允许的 Host 列表（逗号分隔） | 反代转发过来的请求全被 403 |
| `BUDDYCRON_TRUST_PROXY` | 用 `X-Forwarded-For` 做失败计数 | 别人试错会把你一起锁 5 分钟 |

### 3. Caddy 配置

```
buddy.example.com {
    reverse_proxy 127.0.0.1:8787 {
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-For {remote_host}
    }
    encode gzip
    # 不给搜索引擎索引，也不允许被嵌入 iframe
    header X-Robots-Tag "noindex, nofollow"
    header X-Frame-Options "DENY"
}
```

`header_up` 那两行**不能省**：少了 `X-Forwarded-Proto`，服务会认为你在用 HTTP
而拒绝所有请求；少了 `X-Forwarded-For`，失败计数会把所有访客算成同一个人。

### 4. 系统服务

`deploy/buddycron-web.service`：

```ini
[Unit]
Description=BuddyCron Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/YOU/Codespace/BuddyCron
Environment=BUDDYCRON_REQUIRE_HTTPS=1
Environment=BUDDYCRON_HOSTS=buddy.example.com
Environment=BUDDYCRON_TRUST_PROXY=1
ExecStart=/usr/bin/python3 cli.py web --host 127.0.0.1 --port 8787 --no-browser
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

## 安全边界（这些是已做的，也说明哪些没做）

**已做：**

- 密码用 PBKDF2-HMAC-SHA256（20 万轮）散列存储，磁盘上没有明文
- 会话 cookie 是 `HttpOnly` + `SameSite=Strict` + `Secure`（公网模式）
- 连续 5 次失败锁 5 分钟，按 `X-Forwarded-For` 分段计数
- Host 白名单 + Origin 校验，挡 DNS rebinding 与跨站请求
- 改密码换签名密钥，所有旧会话立即失效
- 静态资源走白名单，不可路径穿越
- 明文口令在首次登录成功后自动清除

**没做（如果你需要，告诉我）：**

- **没有 TOTP / 2FA**。单口令是这个工具的定位（单人自用）。如果你的服务器
  不止你一个人能访问，建议加一层。
- **没有 IP 白名单**。想更收紧可以在 Caddy 里加 `@allowed remote_ip ...`。
- **没有审计日志**。谁在什么时候登录过、跑了什么，目前只进 journald。
- **密码强度不可配**。生成的是 8 位随机串（去掉易混字符），你也可以
  `python3 cli.py passwd <你自己的密码>` 指定任意强度。

## 如果你不想开公网

大多数情况下**不需要**。这台机器上：
- 本机用 → `http://127.0.0.1:8787`，不用配任何东西
- 局域网其他设备 → 建议仍然走 SSH 隧道，而不是监听 `0.0.0.0`：

```bash
# 在你自己的电脑上
ssh -L 8787:127.0.0.1:8787 you@server
# 然后浏览器开 http://127.0.0.1:8787
```

SSH 隧道已经把整条链路加密了，比裸 HTTP 监听 `0.0.0.0` 安全得多，
也不用配证书。

## 一个必须知道的风险

这些 token 能直接调用你的 WorkBuddy 账号，**能签到、能领奖、能查积分**。
一旦泄露，别人可以拿它跑同样的操作。所以：

- 不要把 `~/.buddycron/accounts.json` 放进任何同步盘或版本库
- 公网部署时，服务器本身的访问权限（SSH）比这个 Web 页更值得加固
- 定期 `python3 cli.py passwd` 换口令
