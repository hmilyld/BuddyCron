# 公网部署

```
浏览器 ──HTTPS──> Caddy（自动证书） ──HTTP──> buddycron:8787
```

**BuddyCron 不对外暴露端口，TLS 由 Caddy 终止。** 明文 HTTP 会把登录口令和
所有带 token 的请求暴露在链路上，所以这一层不能省。

## 用现成的 stack.yml

仓库根目录的 `stack.yml` + `Caddyfile` 就是完整部署配置。

```bash
mkdir -p ./data
# 改 stack.yml 里的域名（BUDDYCRON_HOSTS）和密码（BUDDYCRON_PASSWORD）
docker stack deploy -c stack.yml buddycron
```

不用 swarm 也可以：`docker compose -f stack.yml up -d`。

Caddy 自动申请 Let's Encrypt 证书，前提是域名 A 记录指向这台服务器，
且 80 端口能从公网访问（验证域名归属要用）。

## 三个必填变量

`stack.yml` 里已配好，只需要改域名和密码。

| 变量 | 不设的后果 |
|---|---|
| `BUDDYCRON_REQUIRE_HTTPS=1` | 服务直接拒绝启动（硬拦截，防明文暴露口令） |
| `BUDDYCRON_HOSTS` | 所有请求 403——反代改写了 Host，白名单里没有你的域名 |
| `BUDDYCRON_TRUST_PROXY=1` | 所有访客算同一个人，别人试错会把你一起锁 5 分钟 |

## 两个部署时会踩的坑

**① 数据目录属主**

容器以 UID 1000 运行。`stack.yml` 默认用**命名卷**，镜像里的 `/data` 已经
chown 成 1000:1000，卷初始化会继承这个属主 —— **不需要手动 chown**。

如果你想改用绑定挂载（数据文件直接可见、便于备份），宿主机目录必须属于
同一个 UID，否则报 `PermissionError: /data/.config.json`：

```bash
mkdir -p ./data && chown -R 1000:1000 ./data
```

服务器上的用户不是 1000 时，改 `stack.yml` 里的 `user:`。

**② 代理变量**

如果 shell 里设了 `HTTP_PROXY`，Docker 会把它传进容器，而容器里的 `127.0.0.1`
指向容器自己。**两个服务都要清**：

- buddycron 走代理 → 签到请求全部 `Connection refused`
- Caddy 走代理 → Let's Encrypt 证书永远签不下来，443 一直连不上

`stack.yml` 里已经给两个服务都清了。

## 自己写反向代理

不用 stack.yml 里的 Caddy 时，参考这段：

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

- 不要把 `data/accounts.json` 放进同步盘或版本库
- 服务器本身的 SSH 访问权限比这个 Web 页更值得加固
- 定期换口令

## 不想开公网？

大多数情况不需要。SSH 隧道已经把链路加密了，比裸 HTTP 监听 `0.0.0.0` 安全得多，
也不用配证书：

```bash
ssh -L 8787:127.0.0.1:8787 you@server
# 浏览器开 http://127.0.0.1:8787
```
