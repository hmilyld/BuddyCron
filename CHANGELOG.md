# Changelog

版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

发布时 CI 会从本文件里抽取对应版本的段落作为 Release 说明，所以**每个要发布的版本
必须有对应段落**（格式见下方）。抽取逻辑在 `scripts/changelog.py`，测试会校验
`core/version.py` 里的版本在本文件里存在。

## [Unreleased]

## [0.1.1] - 2026-11-06

### 修复

- **镜像地址大小写**：`github.repository` 保留仓库名的大小写（`hmilyld/BuddyCron`），
  而 Docker 镜像名必须全小写。原先 Release 里写的是 `ghcr.io/hmilyld/BuddyCron`，
  复制那条 `docker pull` 会失败。现在固定为 `ghcr.io/hmilyld/buddycron`。
- **CI 触发分支**：工作流里写的 `main`，而仓库默认分支是 `master`，推送到 master
  不会触发 CI。已改为 `master`。

### 变更

- **发布说明改从 CHANGELOG 生成**。原先用 GitHub 的 `generate_release_notes`，
  它按 commit 列表生成，读者看到的是「fix(ci): ...」这种开发者视角的碎片。
  现在 `CHANGELOG.md` 是唯一来源，`scripts/changelog.py` 负责抽取。
- **CI 增加 CHANGELOG 校验**：tag 对应的版本在 `CHANGELOG.md` 里没有段落时直接失败，
  避免发布出空白说明。

## [0.1.0] - 2026-11-06

首个版本。

### 新增

- **多账号签到**：每个账号独立 OAuth 授权，各自持有独立的 refresh 链，互不影响。
- **成长中心**：旅行领奖与派出、任务接取与领奖、断登补登、连登兑换、抽奖、Buddy 盲盒，
  七段按固定顺序执行。
- **积分看板**：余额、按用途分组、包级到期时间、订阅类型。按账号折叠，点开看明细。
- **每日自动签到**：默认 09:00 起跑（随机延迟 0~60 分），09/13/17/21 点补签兜底。
  关机错过后开机自动补跑。
- **Web 界面**：签到日历、积分合计、账号列表、日志查看，支持明暗主题切换。
- **单口令登录**：PBKDF2-HMAC-SHA256（20 万轮）散列存储，签名 cookie 会话，
  连续 5 次失败锁 5 分钟。
- **结构化日志**：按日分文件，每行一个 JSON，记每段细节，写前脱敏，保留 6 个月。
- **Docker 部署**：多架构镜像（amd64 / arm64），数据目录挂载，容器内自带调度。

### 说明

- 接口契约来自官方前端 bundle 实测，服务端改版可能失效。
- 任务类积分（一次性 1800）需要在客户端里完成真实操作，工具只负责「接取 + 领奖」两端。
- 旅行每天只能派一次，早领礼物不增加收益。
