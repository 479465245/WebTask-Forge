#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_CANDIDATES=(automation-workbench.service web-browser-demo.service)

if command -v systemctl >/dev/null 2>&1; then
  for service in "${SERVICE_CANDIDATES[@]}"; do
    if systemctl is-active --quiet "$service"; then
      systemctl stop "$service"
      echo "已停止 systemd 服务 $service。"
      exit 0
    fi
  done
fi

found=0
for proc in /proc/[0-9]*; do
  [[ -r "$proc/cmdline" ]] || continue
  pid="${proc##*/}"
  cwd="$(readlink -f "$proc/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$ROOT" ]] || continue
  cmdline="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
  [[ "$cmdline" == *app.py* && "$cmdline" == *python* ]] || continue
  kill -TERM "$pid" 2>/dev/null || continue
  found=1
  for _ in {1..30}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "进程 $pid 未在 15 秒内正常退出；未强制结束。" >&2
    exit 1
  fi
  echo "已停止应用进程 $pid。"
done

if [[ "$found" -eq 0 ]]; then
  echo "没有找到本项目正在运行的应用进程。"
fi
