#!/bin/sh
# 容器入口：既跑 Web 服务，也跑定时任务。
#
# 为什么在容器里自己跑定时器而不是依赖宿主机 cron：
#   容器是可丢弃的，宿主机上不该为它装东西。一个容器内的小循环更简单，
#   也不会因为宿主机没配 cron 就悄悄不签到。
#
# 两个节奏（与 systemd 版本一致）：
#   每天 00:05  签到 + 成长全套 + extras
#   每 6 小时    只补签（未签才跑全套）
#
# 时区由 TZ 环境变量决定；签到按「本地日期」判定今天，时区错了会算错日子。

set -eu

DATA="${BUDDYCRON_HOME:-/data}"
DAILY_HOUR="${BUDDYCRON_DAILY_HOUR:-0}"
DAILY_MINUTE="${BUDDYCRON_DAILY_MINUTE:-5}"
POLL_HOURS="${BUDDYCRON_POLL_HOURS:-6}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# --- 定时循环 ---------------------------------------------------------
# 用「墙钟比较」而不是 sleep 到目标时刻：容器被暂停/恢复后单调时钟不走，
# sleep 会继续倒数，导致当天的签到被推迟到第二天。
scheduler() {
  last_daily=""
  last_poll=0
  while true; do
    now=$(date +%s)
    today=$(date '+%Y-%m-%d')
    hm=$(date '+%H:%M')

    # 每日签到：到了设定时刻且今天还没跑过
    if [ "$hm" \> "$(printf '%02d:%02d' "$DAILY_HOUR" "$DAILY_MINUTE")" ] \
       || [ "$hm" = "$(printf '%02d:%02d' "$DAILY_HOUR" "$DAILY_MINUTE")" ]; then
      if [ "$last_daily" != "$today" ]; then
        log "定时：每日签到开始"
        python3 cli.py run || log "每日签到退出码非零（继续运行）"
        last_daily="$today"
        last_poll=$now
        sleep 10
        continue
      fi
    fi

    # 补签兜底：每 N 小时一次，先查状态、未签才跑全套
    if [ $(( now - last_poll )) -ge $(( POLL_HOURS * 3600 )) ]; then
      log "定时：补签检查"
      python3 cli.py poll --quiet || log "补签退出码非零（继续运行）"
      last_poll=$now
    fi

    sleep 30
  done
}

# --- 主流程 -----------------------------------------------------------
mkdir -p "$DATA" 2>/dev/null || true

if [ "${BUDDYCRON_DISABLE_SCHEDULER:-0}" = "1" ]; then
  log "定时任务已禁用（BUDDYCRON_DISABLE_SCHEDULER=1）"
else
  scheduler &
  SCHED_PID=$!
  log "定时任务已启动：每天 ${DAILY_HOUR}:$(printf '%02d' "$DAILY_MINUTE") 签到，每 ${POLL_HOURS} 小时补签"
fi

# Web 服务放前台，容器生命周期跟着它走。
exec python3 cli.py web --host 0.0.0.0 --port 8787 --no-browser
