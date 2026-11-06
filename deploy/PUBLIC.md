# 公网部署

```
浏览器 ──HTTPS──> Caddy（自动证书） ──HTTP──> 127.0.0.1:8787
```

**BuddyCron 永远只监听 127.0.0.1，TLS 由 Caddy 终止。** 明文 HTTP 会把登录口令和
所有带 token 的请求暴露在链路上，所以这一层不能省。

## 步骤

### 1. 域名

一个指向服务器的 A 记录，比如 `buddy.example.com`。Let's Encrypt 需要能通过
80 端口验证域名归属。

### 2. 起服务

```bash
BUDDYCRON_REQUIRE_HTTPS=1 \
BUDDYCRON_HOSTS=buddy.example.com \
BUDDYCRON_TRUST_PROXY=1 \
python3 cli.py web --host 127.0.0.1 --port 8787 --no-browser
```

三个变量都是**必填**：

| 变量 | 不设的后果 |
|---|---|
| `BUDDYCRON_REQUIRE_HTTPS=1` | 服务直接拒绝启动（硬拦截，防止明文暴露口令） |
| `BUDDYCRON_HOSTS` | 所有请求 403——反代会改写 Host，白名单里没有你的域名 |
| `BUDDYCRON_TRUST_PROXY=1` | 所有访客算同一个人，别人试错会把你一起锁 5 分钟 |

### 3. Caddy

```
buddy.example.com {
    reverse_proxy 127.0.0.1:8787 {
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-For {remote_host}
    }
    encode gzip
    header X-Robots-Tag "noindex, nofollow"
    header X-Frame-Options "DENY"
}
```

`header_up` 两行**不能省**：少了 `X-Forwarded-Proto` 服务会以为你在用 HTTP 而拒绝
所有请求；少了 `X-Forwarded-For` 失败计数会算错。

### 4. 常驻

用 `deploy/buddycron-web.service`，把三个 `Environment=` 改成你的值，然后：

```bash
systemctl --user enable --now buddycron-web.service
```

## 安全措施

- 口令 PBKDF2-HMAC-SHA256（20 万轮）散列，磁盘无明文
- cookie `HttpOnly` + `SameSite=Strict` + `Secure`
- 连续 5 次失败锁 5 分钟，按 `X-Forwarded-For` 分段
- Host 白名单 + Origin 校验，挡 DNS rebinding 与跨站请求
- 改密码换签名密钥，旧会话立即失效
- 静态资源白名单，不可路径穿越
- 明文口令在首次登录成功后自动清除

## 风险

这些 token 能直接操作你的账号——签到、领奖、查积分。泄露了别人能做同样的事。

- 不要把 `~/.buddycron/accounts.json` 放进同步盘或版本库
- 服务器本身的 SSH 访问权限比这个 Web 页更值得加固
- 定期换口令

## 不想开公网？

大多数情况不需要。SSH 隧道已经把链路加密了，比裸 HTTP 监听 `0.0.0.0` 安全得多，
也不用配证书：

```bash
ssh -L 8787:127.0.0.1:8787 you@server
# 浏览器开 http://127.0.0.1:8787
```
