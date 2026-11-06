#!/usr/bin/env bash
# 安装 systemd user timer。可重复执行。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_DIR"
for unit in buddycron-daily buddycron-poll; do
  for ext in service timer; do
    sed "s|%h/Codespace/BuddyCron|$ROOT|g" "deploy/$unit.$ext" > "$UNIT_DIR/$unit.$ext"
  done
done

systemctl --user daemon-reload
systemctl --user enable --now buddycron-daily.timer buddycron-poll.timer

echo "已安装："
systemctl --user list-timers buddycron-daily.timer buddycron-poll.timer --no-pager
echo
echo "看日志：journalctl --user -u buddycron-daily -n 50 --no-pager"
echo "卸载：  systemctl --user disable --now buddycron-daily.timer buddycron-poll.timer"
