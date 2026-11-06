# BuddyCron

WorkBuddy 多账号签到与积分工具。CLI + 本地 Web 页，零第三方依赖。

## 功能

| 功能 | 说明 |
|---|---|
| 多账号管理 | 每个账号独立 OAuth 授权，各自持有独立的 refresh 链 |
| 每日签到 | 每天一次，领取 100 积分，连签天数累计 |
| 成长中心 | 旅行领奖与派出、任务接取与领奖、断登补登、连登兑换、抽奖、Buddy 盲盒 |
| 平台礼包 | 新人礼包与平台补偿（不定期活动） |
| 积分查询 | 余额、按用途分组、包级到期时间、订阅类型 |
| 定时执行 | 每天签到 + 定期补签兜底 |
| 日志 | 结构化记录，界面按月份查看，可展开看每段细节 |
| Web 界面 | 签到日历、积分汇总、账号列表、手动触发 |
| 单口令登录 | PBKDF2 散列存储，签名 cookie 会话 |

## 快速开始

```bash
python3 cli.py web          # 起本地 Web 页，浏览器里登录账号
python3 cli.py doctor       # 只读核对接口契约
python3 cli.py run          # 签到 + 成长全套 + extras
python3 cli.py status       # 看账号与最近结果
```

## 界面

界面是「签到簿」：本月日历是主角，朱红实心是已签，今天描边，未来极淡。
**运行侧不需要 Node** —— `web/dist/` 里是编译好的产物。

四个区块：

- **本月签到** —— 整月日历，右上角是已签天数，底部是「漏签几天」
- **积分合计** —— 所有账号汇总 + 最近到期提醒
- **账号** —— 每行一个账号的完整档案（签到状态、积分余额、到期提示），点开看包级明细
- **日志** —— 按月份切换，每条可展开看每段细节

只有改界面时才需要重编：

```bash
npm install          # 首次
python3 web/build.py # 编译 Tailwind → web/dist/
```

用 Tailwind 而不是运行时 CDN：这是本地工具，断网也要能用；而且 CDN 脚本意味着
页面能执行任意远端代码，对一个持有 token 的本地服务不可接受。

## 端口

**只有一个端口：8787。** 默认只绑 `127.0.0.1`。

```bash
python3 cli.py web                       # http://127.0.0.1:8787/
python3 cli.py web --port 8787           # 显式指定（一般不需要）
```

## 登录

密码有两种来源，**环境变量优先**。

### 方式一：环境变量（Docker 部署推荐）

```yaml
environment:
  BUDDYCRON_PASSWORD: "你的密码"
```

设了就用它，不再随机生成，也不打印明文。**改了环境变量就等于改密码**（下次启动生效）。

### 方式二：首次启动自动生成

不设环境变量时，首次启动生成一个 8 位随机口令，**打印在终端**。
明文会一直留在 `config.json` 里，直到你**成功登录一次**为止——散列存储意味着
忘了就再也看不到，而刚生成时恰恰最可能没记下来。

```bash
python3 cli.py passwd --show    # 查看当前口令（若明文尚未清除）
python3 cli.py passwd           # 随机重设
python3 cli.py passwd 我的密码    # 指定新口令
```

> **环境变量模式下 `passwd` 会直接拒绝**（退出码 1）并告诉你去改环境变量。
> 不这么做的话，它会产出一个下次启动就被覆盖的新口令，让你以为改成功了。

只校验密码，不需要用户名。会话用 HMAC 签名的 HttpOnly cookie，有效期 7 天。
同一客户端连续 5 次失败锁 5 分钟；改密码会换掉签名密钥，所有旧会话立刻失效。

## 部署

### Docker

```bash
mkdir -p data
docker compose up -d
docker compose logs -f buddycron   # 首次会打印登录密码
```

更新版本 `docker compose up -d --build`，**`./data` 不动，账号不会丢**。

两个必须知道的坑：

**① 数据目录权限** —— 容器以你的 UID 运行（compose 里 `user: "${BUDDYCRON_UID:-1000}"`），
`./data` 必须属于你。报 `PermissionError: /data/.config.json` 就是属主不对：

```bash
BUDDYCRON_UID=$(id -u) BUDDYCRON_GID=$(id -g) docker compose up -d
```

**② 代理变量** —— 如果 shell 设了 `HTTP_PROXY=http://127.0.0.1:7897`，docker 会传进容器，
而容器里的 `127.0.0.1` 是容器自己、不是宿主机，所有出站请求都会 `Connection refused`。
compose 里已把这几个变量清空；代理必需时改用 `host.docker.internal` + `extra_hosts`。

### systemd

```bash
bash deploy/install.sh      # 装 user timer
```

### 定时

两套部署方式各有一套调度，**只启用其中一套**，不要同时开：

| 时刻 | 动作 | 请求量 |
|---|---|---|
| 09:00（随机延迟 0~60 分） | 签到 + 成长全套 + extras + 积分 | ~20 / 账号 |
| 09:00 / 13:00 / 17:00 / 21:00 | 补签兜底 + 旅行 | ~4 / 账号 |

为什么是 9 点而不是 0 点：签到没有「必须几点」的硬限制，过了 0 点就算新的一天。
0 点跑只是「尽早拿到」，代价是你得熬夜或者第二天才知道有没有失败。9 点后跑，
醒来就能看到结果。`Persistent=true`（systemd）/ 启动补跑（容器）会兜住「当天没跑成」。

补签不在 9 点前跑：主签到还没跑过，那时候补签只会白跑一趟。

```yaml
# 容器里可调
environment:
  BUDDYCRON_DAILY_HOUR: "9"
  BUDDYCRON_DAILY_MINUTE: "0"
  BUDDYCRON_POLL_HOURS: "4"
  BUDDYCRON_DISABLE_SCHEDULER: "1"   # 只跑 Web，不要定时
```

### 时间预算

账号**串行**跑，每个账号切一份配额：

| 常量 | 值 | 含义 |
|---|---|---|
| `DAILY_BUDGET` | 900s | 每日全套的全局上限 |
| `POLL_BUDGET` | 300s | 补签轮次的全局上限 |
| `PER_ACCOUNT_BUDGET` | 120s | 单账号配额 |

900 ÷ 120 = **7 个账号**。超过这个数，后面的会被跳过并在结果里标注「时间预算耗尽」。

单账号实测约 20 个请求、正常 20~40 秒跑完；120 秒配额留了 3 倍余量给网络抖动时的重试。
改这些常量时必须同步改 `deploy/*.service` 的 `TimeoutStartSec`（要大于预算，否则
进程会被 systemd 杀掉而结果还没落盘）。

### 对外访问

默认只绑回环，外网访问不到。大多数情况用 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 you@server
```

确实要公网访问时，Caddy 终止 TLS，BuddyCron 仍只监听 127.0.0.1，
完整步骤见 **[deploy/PUBLIC.md](deploy/PUBLIC.md)**。

```bash
BUDDYCRON_REQUIRE_HTTPS=1 BUDDYCRON_HOSTS=buddy.example.com \
BUDDYCRON_TRUST_PROXY=1 python3 cli.py web --no-browser
```

| 变量 | 作用 |
|---|---|
| `BUDDYCRON_REQUIRE_HTTPS` | 拒绝非 HTTPS 请求，cookie 加 `Secure` |
| `BUDDYCRON_HOSTS` | 允许的 Host 列表（反代会改写 Host） |
| `BUDDYCRON_TRUST_PROXY` | 按 `X-Forwarded-For` 分段计数失败次数 |

**硬拦截**：绑到非回环地址却没设 `BUDDYCRON_REQUIRE_HTTPS=1` 时，服务拒绝启动。

## 数据

默认只绑回环，外网访问不到。大多数情况用 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 you@server
```

确实要公网访问时，Caddy 终止 TLS，BuddyCron 仍只监听 127.0.0.1，
完整步骤见 **[deploy/PUBLIC.md](deploy/PUBLIC.md)**。

```bash
BUDDYCRON_REQUIRE_HTTPS=1 BUDDYCRON_HOSTS=buddy.example.com \
BUDDYCRON_TRUST_PROXY=1 python3 cli.py web --no-browser
```

| 变量 | 作用 |
|---|---|
| `BUDDYCRON_REQUIRE_HTTPS` | 拒绝非 HTTPS 请求，cookie 加 `Secure` |
| `BUDDYCRON_HOSTS` | 允许的 Host 列表（反代会改写 Host） |
| `BUDDYCRON_TRUST_PROXY` | 按 `X-Forwarded-For` 分段计数失败次数 |

**硬拦截**：绑到非回环地址却没设 `BUDDYCRON_REQUIRE_HTTPS=1` 时，服务拒绝启动。

## 数据

```
~/.buddycron/
├── accounts.json     账号库（含明文 token，0600）
├── config.json       口令散列 + 会话密钥（0600）
├── state.json        最近一轮结果
├── logs/YYYY-MM.log  按日分的汇报
└── run.lock          跨进程锁
```

目录 0700、文件 0600，写入走「临时文件 + rename」且临时文件创建时就带 0600。
可用 `BUDDYCRON_HOME` 覆盖数据目录。

## 日志

界面底部按月份查看，每条可展开看每账号的每段细节。文件在 `~/.buddycron/logs/`，
**每行一个 JSON**，可以直接 `jq`：

```bash
tail -1 ~/.buddycron/logs/2026-11.log | jq '.accounts[0].growth.sections'
```

设计取舍：

- **按日分文件**而不是一个大文件 —— 找「昨天那次」就是打开一个文件
- **记每段细节**而不只记汇总 —— 汇总一句「16 项待体验」定位不了问题
- **轮询空跑不写** —— 一天跑多次绝大多数无事，全写会淹没真事件
- **写前脱敏** —— 日志会进 journald 或被人 `cat`，token 不能落盘
- **保留 6 个月**，更老的自动清理

## 版本与发布

版本号只在 `core/version.py` 一处定义。CLI、Web 页面、镜像标签都从它读。

```bash
vim core/version.py              # 1. 改版本号
python3 web/build.py             # 2. 改了界面就重编
git commit -am "chore: 升到 v0.2.0" && git push
git tag v0.2.0 && git push origin v0.2.0    # 3. 这一步才触发发布
```

**tag 与 `core/version.py` 不一致时 CI 直接失败**——一个标错版本号的镜像比
没有镜像更麻烦。

| 工作流 | 触发 | 内容 |
|---|---|---|
| `ci.yml` | 推送 / PR | 测试、检查 `web/dist` 与 `web/src` 同步、校验版本号格式、确认没提交数据文件 |
| `release.yml` | 打 `v*` tag | 校验 tag 一致 → 建 amd64+arm64 镜像推 ghcr.io → 建 Release |

镜像标签：`0.2.0` / `0.2` / `latest`。

## 项目结构

```
core/
├── http.py       请求层：双码校验 / 分级退避 / 预算 / 信封解包
├── accounts.py   账号库 / OAuth 登录 / refresh 串行轮换
├── auth.py       登录口令 / 会话
├── checkin.py    签到
├── growth.py     成长中心七段
├── credits.py    积分与到期时间
├── extras.py     平台礼包 + 补偿
├── runner.py     多账号编排 / 轮次锁 / 每账号 quiet
├── logs.py       结构化日志
├── doctor.py     只读契约核对
├── version.py    版本号
└── paths.py      数据目录与落盘
web/
├── server.py     本地 Web 页
├── build.py      界面构建（Tailwind）
├── src/          界面源码
└── dist/         编译产物（提交进仓库）
cli.py            命令行入口
deploy/           systemd units / 容器入口 / 公网部署文档
```

## 测试

```bash
python3 -m unittest discover -s tests
```

纯逻辑测试，不依赖网络、不需要真 token。

## 接口契约与能力边界

以下结论来自**真 token 实测**（2026-11-06）。

### 域名

三个 host（`www.codebuddy.cn` / `www.workbuddy.cn` / `copilot.tencent.com`）对成长中心
和签到接口**都能通**。默认走账号 `domain` 字段指定的那个，失败再换。

### 积分来源

单个账号每月稳态收益（官方活动规则原文）：

| 来源 | 频率 | 单次 | 每月 |
|---|---|---|---|
| 每日签到 | 每天 | 100 | **3000** |
| 旅行奖励 | 每天 1 次 | 5–10 | ~225 |
| 连登兑换 | 每月 3 档 | 200 | 200 |
| 抽奖 | 每月 3 次 | 期望 19 | ~57 |
| 成长任务 | 一次性 | 最多 1800 | 1800（仅首月） |

**签到一项占稳态收益的 86%。** 盲盒/徽章/Buddy 养成对积分是**零收益**——
能量只能换虚拟宠物。

### 任务类功能的前提

18 项成长任务全部需要先完成 `first_buddy`（首领 Buddy 任务，300 积分 + 8 能量）。
它的完成条件是**在客户端里真的产生一次对话**：

```
POST /v2/activity/growth/tasks/accept  {"task_codes":["first_buddy"]}
→ {"status":"error","message":"task does not require acceptance"}

POST /v2/activity/growth/buddy/first   {}
→ 400 {"msg":"first_buddy task not completed yet (need at least one conversation)"}
```

服务端没有上报任务进度的入口（`tasks/{code}/complete`、`/finish`、`/report`、
`/progress` 四个形态实测全 404），进度只随客户端里的真实行为增长。

工具先读 `first_buddy` 状态再决定要不要接取进阶任务：

| 状态 | 行为 |
|---|---|
| `claimed` / `completed` | 已打通 → 接取 16 项 + 领已完成奖励 |
| 其它 | 未打通 → 只尝试领首领任务本身的奖，不碰进阶任务 |
| 接口没给这一项 | 保守按未打通处理 |

打通之后能自动完成的是「接取」和「领奖」两端；中间那段真实使用行为
（对话、用技能、召唤专家、用模板、建自动化任务）需要在客户端里逐项完成。

### 补登卡的判断

账号从未活跃过时，本月所有历史日期 `score` 都是 0，但它们不是「断登」而是「还没开始」。
`makeup_candidates` 用 `streak.days == 0 && month_total_days == 0` 排除这种情况——
否则会拿不可逆的补登卡去补一个从没开始过的月份。

### 成长中心的执行顺序

```
1. 旅行：先领已有礼物，派出留到最后
2. 任务：接取 + 领已完成奖励（任务送的能量/抽奖机会后面马上要用）
3. 补登：断登补一张卡（会改变连签天数，必须早于兑换）
4. 连登兑换：7/14/28 天三档
5. 抽奖：用兑换得到的次数
6. Buddy 盲盒：用能量开桌宠（会覆盖旅行记录，必须在旅行明确 idle 时）
7. 最后派出：不覆盖在途礼物，也让「每天只跑一次」的用户有机会开 Buddy
```

## 免责

非官方工具，与 WorkBuddy / CodeBuddy 官方无任何关联。接口从官方前端逆向所得，
腾讯改一版就可能失效。官方活动规则明确禁止「恶意注册多个账号」与「使用脚本
篡改任务完成数据」，两者都在取消资格条款里。仅用于管理你自己名下的账号，
风险自担。
