#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if command -v systemctl >/dev/null 2>&1; then
  for service in automation-workbench.service web-browser-demo.service; do
    if systemctl is-active --quiet "$service"; then
      systemctl restart "$service"
      echo "已重启 systemd 服务 $service。"
      exit 0
    fi
  done
fi

"$ROOT/scripts/stop.sh"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "未找到项目虚拟环境，请先运行 ./scripts/init.sh。" >&2
  exit 1
fi
mkdir -p "$ROOT/.browser-data"
nohup "$ROOT/.venv/bin/python" "$ROOT/app.py" >> "$ROOT/app.log" 2>&1 < /dev/null &
pid=$!
printf '%s\n' "$pid" > "$ROOT/.app.pid"
port="${BROWSER_DEMO_PORT:-8765}"

for _ in {1..30}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$ROOT/.app.pid"
    echo "应用启动失败，请查看 $ROOT/app.log。" >&2
    exit 1
  fi
  if "$ROOT/.venv/bin/python" -c 'import socket, sys; socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1).close()' "$port"; then
    echo "应用已启动（PID $pid）：http://127.0.0.1:$port/"
    exit 0
  fi
  sleep 1
done

echo "应用进程已启动（PID $pid），但 30 秒内未能通过 http://127.0.0.1:$port/ 确认就绪。请查看 $ROOT/app.log。" >&2
exit 1
