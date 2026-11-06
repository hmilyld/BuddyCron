# AGENTS.md

给 AI 代理（和接手的人）的约束。**项目事实看 `README.md`**，这里只写「怎么改」。

## 不能做

- **不提交数据文件**：`accounts.json` / `config.json` / `state.json` / `logs/`。
  含明文 token，`.gitignore` 已排除，`ci.yml` 会检查。
- **不加第三方 Python 依赖**。项目只用标准库——这是「任意 Python 3.11+ 直接跑」的前提。
- **不在运行时拉 CDN**。界面用构建期编译的 Tailwind（`web/dist/`）。CDN 意味着
  页面能执行任意远端代码，而这个服务持有 token。
- **不改成长中心七段的顺序**（见 README）。每段位置都有理由，动了会静默丢奖励。
- **不让写操作重试**。`http.post()` 默认 `retry=False`。抽奖、领奖、兑换、补登若在
  服务端处理完成后才超时，重试就是重复提交。只有签到两个接口例外。
- **不吞异常**。`except Exception: pass` 会让「当天签到失败」无痕消失。

## 必须做

- 改界面后 `python3 web/build.py` 并提交 `web/dist/`。`ci.yml` 会对比，不同步就失败。
- 改版本号只改 `core/version.py`。测试会扫全仓库找第二个 `x.y.z` 字面量。
- 改完跑 `python3 -m unittest discover -s tests`。
- 新增端口要慎重：测试只允许 `8787`。
- 提交信息用 Conventional Commits：`feat(ui): ...`。
- 新增环境变量要更新 README 的表格，`test_readme_documents_every_env_var` 会对比。

## 接口层

**双码校验**：HTTP 200 不等于成功。`{"code": 10001}` 配 200 是常态，一律用
`http.api_success(status, body)`。

**鉴权判定**：`http.is_unauthorized()` 只认 401/403 和明确的 token 失效语义。
不要用「消息里带 token 字样」兜底——「token 兑换活动已结束」这种文案会误触发刷新，
而刷新失败又整轮中止，结果是一次无关报错把当天签到弄没了。

**refresh 是单次轮换**：服务端每刷一次就作废旧的。必须走 `accounts.refresh_account()`
（内部有文件锁），进锁后先回读账号库——库里已变就直接采用，不再二次刷新。

**逐条结果**：`tasks/accept` 返回 `results[]` 逐条结果，HTTP 200 不代表全部成功。

**域名候选**：`http.ACTIVITY_HOSTS` / `BILLING_HOSTS` 的顺序不是随意排的。
新增端点沿用 `_hosts()`。

## 多账号隔离

四处必须「每账号一份」：

| 项 | 位置 | 不做的话 |
|---|---|---|
| 脱敏 | `runner._mask()` 只抹本账号 token | A 的 token 把 B 汇报里的同名字符串也替换掉 |
| 预算 | `Budget.child()` | 第 1 个账号超时吃光全局预算，第 5 个轮不到 |
| quiet | `runner._is_quiet()` | 5 个账号 4 个空跑，日志被淹没 |
| 锁 | `CycleGuard` + `accounts.file_lock` | timer 与手动 run 撞上，互相覆盖账号库 |

## 界面

**视觉**是「签到簿」：纸面底、朱红印章、墨绿连签。不要换成通用深色仪表盘。

**布局**：
- 账号行用 flex，不用绝对定位摆按钮——各算各的 `top` 永远对不齐
- 行的 padding 放 `summary` 上，不是外层容器——否则 hover 背景盖不满整行
- 折叠用原生 `<details>` / `<summary>`

**数据来源**：
- 「本月签到」用 `checkin_dates`，不是 heatmap 的 `score`（后者是活跃度分数）
- 积分到期取 `CycleEndTime`，不用 `ExpiredTime`（多数为空）
- 容量取 `*Precise` 字段，普通字段是整数截断

## 安全

- 只绑 `127.0.0.1`。绑非回环必须设 `BUDDYCRON_REQUIRE_HTTPS=1`（硬拦截）
- 口令 PBKDF2-HMAC-SHA256 20 万轮，不存明文
- 明文只在「生成后、首次登录前」保留
- 环境变量模式下 `passwd` 必须拒绝，不能产出会被覆盖的新口令
- 日志写前脱敏：抹掉「长串且只由 token 字符组成」的内容，宁可误抹

## 测试

纯逻辑：不联网、不需要真 token。验证外部依赖时 `patch` 掉 `http.post_retry`。

优先测「容易出错且代价高」的地方：双码校验、鉴权判定、补登日期选择（选错白耗
一张不可逆的卡）、多账号隔离、界面结构约束（如删除按钮必须在 `summary` 内部——
这个问题只在折叠时暴露）。

## 硬边界

服务端决定的，不要试图「修」：

- `first_buddy` 必须在客户端里真的聊一次，服务端没有上报进度的接口
- 旅行每天只能派一次，早领礼物不增加收益
- 能量只能换虚拟宠物，对积分零收益
