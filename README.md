# BuddyCron

WorkBuddy 多账号签到与积分工具。CLI + 本地 Web 页，无第三方依赖。

## 为什么是这个设计

底座取自 [`88lin/workbuddy-auto-signin`](https://github.com/88lin/workbuddy-auto-signin)（MIT，2k star），
但它的三个前提在本项目里都不成立：

| 88lin 的前提 | 本项目 |
|---|---|
| 单账号（全局一个 `WORKBUDDY_AUTH_FILE`） | 多账号，每账号独立凭证 |
| 不刷新 token（靠桌面客户端替它刷） | 本地无客户端，必须自己管 refresh |
| 从客户端 auth 文件导入凭据 | 只能走 OAuth 独立授权 |

接口契约以**官方前端 bundle** 为准，不是以任何第三方脚本为准。从
`assets.workbuddy.cn/web/usercenter/.../growthSpace-*.js` 读出的真实调用里，
两个参考实现各有一处错误：

| 端点 | 官方前端 | 88lin | wb-checkin |
|---|---|---|---|
| `buddy/travel/depart` | `{location_id}` | 正确 | ❌ `{destination}` |
| `buddy/travel/claim` | `{}` 空体 | ⚠️ 多传 `record_id` | ⚠️ 同 |
| `lottery/draw` | `{client_token}` | 正确 | ❌ 发空 `{}` |
| `buddy/open` | `{count}` | 正确 | ❌ 无此功能 |
| `redeem` | `{tier, client_token}` | 正确 | ❌ 无此功能 |

所以：**端点覆盖和健壮性抄 88lin，多账号凭证管理抄 wb-checkin 的思路，契约以官方前端为准。**

## 快速开始

```bash
python3 cli.py web          # 起本地 Web 页，浏览器里登录账号
python3 cli.py doctor       # 只读核对接口契约（第一次必跑）
python3 cli.py run          # 签到 + 成长全套 + extras
python3 cli.py status       # 看账号与最近结果
```

**doctor 必须卡在 run 之前。** 所有端点契约都是读官方前端 bundle 读出来的，
没拿真 token 验证过。写操作一旦用错契约，可能空耗补登卡、抽错奖、重复提交。

## 界面

界面是「签到簿」：本月七格日历是主角，朱红实心是已签，今天描边，未来极淡。
**运行侧不需要 Node** —— `web/dist/` 里是编译好的产物。

四个区块：
- **本月签到** —— 整月日历，右上角是已签天数，底部是「漏签几天」
- **积分合计** —— 所有账号汇总 + 最近到期提醒
- **各账号积分 / 到期日历** —— 并排两栏，各账号按剩余从少到多
- **账号 + 执行** —— 账号状态、添加/删除、手动触发

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

首次 `python3 cli.py web` 会自动生成一个 8 位口令，**打印在终端**。
明文会一直留在 `config.json` 里，直到你**成功登录一次**为止——散列存储意味着
忘了就再也看不到，而刚生成时恰恰最可能没记下来。

```bash
python3 cli.py passwd --show    # 查看当前口令（若明文尚未清除）
python3 cli.py passwd           # 随机重设
python3 cli.py passwd 我的密码    # 指定新口令
```

只校验密码，不需要用户名。会话用 HMAC 签名的 HttpOnly cookie，有效期 7 天。
同一客户端连续 5 次失败锁 5 分钟；改密码会换掉签名密钥，所有旧会话立刻失效。

## 放到网上

**默认不该这么做**，这个服务持有你所有账号的 token。大多数情况用 SSH 隧道就够：

```bash
ssh -L 8787:127.0.0.1:8787 you@server
```

确实要公网访问时，完整步骤见 **[deploy/PUBLIC.md](deploy/PUBLIC.md)**。核心是：
Caddy 终止 TLS，BuddyCron 仍然只监听 127.0.0.1。

```bash
BUDDYCRON_REQUIRE_HTTPS=1 BUDDYCRON_HOSTS=buddy.example.com \
BUDDYCRON_TRUST_PROXY=1 python3 cli.py web --no-browser
```

三个变量的作用：

| 变量 | 作用 |
|---|---|
| `BUDDYCRON_REQUIRE_HTTPS` | 拒绝非 HTTPS 请求，cookie 加 `Secure` |
| `BUDDYCRON_HOSTS` | 允许的 Host 列表（反代会改写 Host，必须配上） |
| `BUDDYCRON_TRUST_PROXY` | 按 `X-Forwarded-For` 分段计数失败次数 |

**硬拦截**：绑到非回环地址却没设 `BUDDYCRON_REQUIRE_HTTPS=1` 时，服务直接拒绝
启动——不会让你不小心把一个明文服务暴露出去。

## Docker 部署

```bash
mkdir -p data
# 首次启动会自动生成口令并打印到容器日志
docker compose up -d
docker compose logs -f buddycron
```

更新版本只需 `docker compose up -d --build`，**`./data` 不动，账号不会丢**。

### 两个必须知道的坑

**① 数据目录权限**

容器以你的 UID 运行（compose 里 `user: "${BUDDYCRON_UID:-1000}"`），所以
`./data` 必须属于你。如果报 `PermissionError: /data/.config.json`，说明属主不对：

```bash
id -u    # 看看自己的 UID
BUDDYCRON_UID=$(id -u) BUDDYCRON_GID=$(id -g) docker compose up -d
```

**② 代理变量**

如果你的 shell 设了 `HTTP_PROXY=http://127.0.0.1:7897`，docker 会把它传进容器，
而容器里的 `127.0.0.1` 是容器自己、不是宿主机 —— 所有出站请求都会
`Connection refused`。compose 里已经把这几个变量清空；如果你的代理是必需的，
改用 `host.docker.internal` 并加 `extra_hosts`（见 compose 注释）。

### 容器内的定时任务

容器自己跑调度，不依赖宿主机 cron：

| 时间 | 动作 |
|---|---|
| 每天 00:05 | 签到 + 成长全套 + extras |
| 每 6 小时 | 只补签（未签才跑全套） |

用墙钟比较而不是 `sleep` 到目标时刻 —— 容器被暂停/恢复后单调时钟不走，
`sleep` 会继续倒数，导致当天的签到被推到第二天。

可调：

```yaml
environment:
  BUDDYCRON_DAILY_HOUR: "0"
  BUDDYCRON_DAILY_MINUTE: "5"
  BUDDYCRON_POLL_HOURS: "6"
  BUDDYCRON_DISABLE_SCHEDULER: "1"   # 只跑 Web，不要定时
```

### 端口

固定 8787，且**只映射到宿主机回环**（`127.0.0.1:8787:8787`）。外网访问不到。

## 日志

界面底部有「日志」区块，按月份切换，每条可展开看每账号的每段细节：

```
2026/11/6 14:03:23  全套  1/1
  ✓ 胡志刚
      签到 ALREADY +100 积分 · 连签 1 天
      余额 2185.05 · 体验版
      今日已签过（今日 +100，连续 1 天，累计 100 积分）；任务·16 项待体验…
```

文件在 `~/.buddycron/logs/YYYY-MM.log`（或容器里 `/data/logs/`），**每行一个 JSON**，
可以直接 `jq`：

```bash
tail -1 ~/.buddycron/logs/2026-11.log | jq '.accounts[0].growth.sections'
```

几个设计取舍：
- **按日分文件**而不是一个大文件 —— 找「昨天那次」就是打开一个文件
- **记每段细节**而不只记汇总 —— 汇总一句「16 项待体验」定位不了问题
- **轮询空跑不写** —— 一天跑 4 次绝大多数无事，全写会淹没真事件
- **写前脱敏** —— 日志会进 journald 或被人 `cat`，token 不能落盘
- **保留 6 个月**，更老的自动清理

## 定时

```bash
bash deploy/install.sh      # 装 systemd user timer
```

| timer | 频率 | 内容 |
|---|---|---|
| `buddycron-daily` | 每天 00:05 | 签到 + 成长全套 + extras |
| `buddycron-poll` | 每 6 小时 | 只查签到，未签才补 |

`Persistent=true` 让关机/休眠错过后开机自动补跑。轮询任务常态只发 1 个请求，
成本几乎为零，但把「00:05 没跑成 = 断签」这个风险降到一天 4 次机会。

## 数据

```
~/.buddycron/
├── accounts.json     账号库（含明文 token，0600）
├── state.json        最近一轮结果
├── logs/YYYY-MM.log  按日分的汇报
└── run.lock          跨进程锁
```

目录 0700、文件 0600，写入走「临时文件 + rename」且临时文件创建时就带 0600。
可用 `BUDDYCRON_HOME` 覆盖数据目录。

## 结构

```
core/
├── http.py       请求层：双码校验 / 分级退避 / 预算 / 信封解包
├── accounts.py   账号库 / OAuth 登录 / refresh 串行轮换
├── checkin.py    签到
├── growth.py     成长中心七段（顺序不可改）
├── extras.py     礼包 + 平台补偿
├── runner.py     多账号编排 / 轮次锁 / 每账号 quiet
└── doctor.py     只读契约核对
web/server.py     本地 Web 页（只绑 127.0.0.1，Host/Origin 双校验）
cli.py            命令行入口
deploy/           systemd user units
```

## 成长中心的执行顺序（不可改）

```
1. 旅行：先领已有礼物，派出留到最后
2. 任务：接取 + 领已完成奖励（任务送的能量/抽奖机会后面马上要用）
3. 补登：断登补一张卡（会改变连签天数，必须早于兑换）
4. 连登兑换：7/14/28 天三档
5. 抽奖：用兑换得到的次数
6. Buddy 盲盒：用能量开桌宠（会覆盖旅行记录，必须在旅行明确 idle 时）
7. 最后派出：不覆盖在途礼物，也让「每天只跑一次」的用户有机会开 Buddy
```

## 测试

```bash
python3 -m unittest discover -s tests
```

纯逻辑测试，不依赖网络、不需要真 token。

## 实测契约与能力边界

以下结论都来自**真 token 实测**（2026-11-06），不是从代码推的。

### 域名

三个 host（`www.codebuddy.cn` / `www.workbuddy.cn` / `copilot.tencent.com`）对成长中心
和签到接口**都能通**。默认走账号 `domain` 字段指定的那个，失败再换。所以「哪个域名对」
不是问题，不用纠结。

### 积分天花板

单个账号每月稳态收益（官方活动规则原文）：

| 来源 | 频率 | 单次 | 每月 |
|---|---|---|---|
| 每日签到 | 每天 | 100 | **3000** |
| 旅行奖励 | 每天 1 次 | 5–10 | ~225 |
| 连登兑换 | 每月 3 档 | 200 | 200 |
| 抽奖 | 每月 3 次 | 期望 19 | ~57 |
| 成长任务 | 一次性 | 最多 1800 | 1800（仅首月） |

**签到一项占稳态收益的 86%，而它每天只要 1 个请求。** 旅行占 6.5%，需要每 4 小时
轮询才能及时领（不领会压到第二天，但当天本来也只能派一次）。盲盒/徽章/Buddy 养成
对积分是**零收益**——能量只能换虚拟宠物。

### 任务类功能的天花板（重要）

18 项成长任务全部需要先完成 `first_buddy`（首领 Buddy 任务，300 积分 + 8 能量）。
而 `first_buddy` 的完成条件是**在客户端里真的产生一次对话**：

```
POST /v2/activity/growth/tasks/accept  {"task_codes":["first_buddy"]}
→ {"status":"error","message":"task does not require acceptance"}

POST /v2/activity/growth/buddy/first   {}
→ 400 {"msg":"first_buddy task not completed yet (need at least one conversation)"}
```

服务端**没有上报任务进度的入口**（`tasks/{code}/complete`、`/finish`、`/report`、
`/progress` 四个形态实测全 404），进度只随客户端里的真实行为增长。

### 门控：未打通时不白打请求

工具先读 `first_buddy` 的状态再决定要不要接取进阶任务：

| `first_buddy` 状态 | 行为 |
|---|---|
| `claimed` / `completed` | 已打通 → 接取 16 项 + 领已完成奖励 |
| 其它（`not_accepted` 等） | 未打通 → **只尝试领首领任务本身的奖**，不碰进阶任务 |
| 接口没给这一项 | 保守按未打通处理 |

不做这道门控的代价：每轮白打 16 个接取请求、收回 16 条同样的
`prerequisite not met: first_buddy`，汇报里永远挂着一句「16 项接取被拒」——
用户看不出这是「还没开始」而不是故障。现在未打通时的汇报是
「进阶任务待解锁（需先在客户端完成一次对话）」，不报假失败。

### 前置打通之后，接取链路完全可用（实测）

在任意一台机器上装 WorkBuddy 客户端、登录、聊一句话之后，`first_buddy` 变成
`claimed`，其余 16 项任务的前置条件随之满足。此后本工具能自动完成：

| 动作 | 实测结果 |
|---|---|
| 批量接取 16 项任务 | ✅ 全部进入 `accepted`，进度槽从 `null` 变成 `0/1`、`0/5` |
| 领已完成任务的奖励 | ✅ 幂等（`already_claimed: true`、`credit: 0`，不会重复发奖） |
| 开 Buddy 盲盒 | ✅ `{count}` 契约正确 |
| 派出旅行 | ✅ `{location_id}` 契约正确，实测派出 2 小时后返回 |

**一次性的 1800 积分仍然要靠你在客户端里逐项完成**（对话、用技能、召唤专家、
用模板、建自动化任务）。工具负责的是「接取 + 领奖」两端，中间那段真实使用行为
没法代做——服务端不给上报入口，这是设计上的硬边界，不是实现缺陷。

所以「能自动拿到的」和「需要你动手的」是这样分的：

```
自动（≈3480/月/账号）      需要你在客户端动手（一次性 1800）
├─ 签到        3000        ├─ 聊 5 次
├─ 旅行         ~225       ├─ 用 1 个技能
├─ 连登兑换      200       ├─ 召唤 5 位专家
└─ 抽奖          ~57       ├─ 用 5 个模板
                            └─ 建 1 个自动化任务
```

### 旅行的时间账（决定了调度设计）

规则原文：每位用户每日可派 Buddy 1 次；须在下一次主动发起旅行之前领取奖励。

所以「早领礼物」**不增加收益**——一天只能派一次，早领晚领都是那 5~10 积分。
轮询的真实价值不是「及时领旅行」，而是：

1. **补签兜底**：00:05 那次若撞上关机/断网，当天还能补上，避免断签。
2. **礼物不丢**：礼物必须在下一次派出前领，否则作废。所以 `poll` 模式在签到
   成功后仍会跑一次「只领礼物 + 派出」（3 个请求），不会把礼物压到下一个 daily。

### 补登卡的一个陷阱

账号从未活跃过时，本月所有历史日期 `score` 都是 0，但它们不是「断登」而是「还没开始」。
`makeup_candidates` 用 `streak.days == 0 && month_total_days == 0` 排除这种情况——
否则会拿不可逆的补登卡去补一个从没开始过的月份。

### 已修正的三处第三方实现错误

| 端点 | 官方前端 | 88lin | wb-checkin |
|---|---|---|---|
| `buddy/travel/depart` | `{location_id}` | ✅ | ❌ `{destination}` |
| `lottery/draw` | `{client_token}` | ✅ | ❌ 空 `{}`（缺了会 400） |
| `buddy/open` | `{count}` | ✅ | ❌ 无此功能 |
| `redeem` | `{tier, client_token}` | ✅ | ❌ 无此功能 |

另外 `tasks/accept` 的响应是**逐条**结果，HTTP 200 不等于全部接取成功：
实测 17 项全部返回 `{"status":"error","message":"prerequisite not met: first_buddy"}`。
旧实现把 200 当成 `len(batch)` 全部成功，会把「全部被拒」报成「接取 17 项」。

## 免责

非官方工具，与 WorkBuddy / CodeBuddy 官方无任何关联。接口从官方前端逆向所得，
腾讯改一版就可能失效。官方活动规则明确禁止「恶意注册多个账号」与「使用脚本
篡改任务完成数据」，两者都在取消资格条款里。仅用于管理你自己名下的账号，
风险自担。
