#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != Linux ]]; then
  echo "此脚本仅支持 Linux。" >&2
  exit 1
fi
if [[ ! -f /etc/debian_version ]]; then
  echo "当前初始化脚本面向 Debian/Ubuntu Linux（apt-get）。" >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || { echo "未找到 $PYTHON，请先安装 Python 3.10+。" >&2; exit 1; }
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10 或更高版本")'

if ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'install ok installed' \
  || ! command -v tesseract >/dev/null 2>&1 \
  || ! dpkg-query -W -f='${Status}' tesseract-ocr-eng 2>/dev/null | grep -q 'install ok installed' \
  || ! dpkg-query -W -f='${Status}' tesseract-ocr-chi-sim 2>/dev/null | grep -q 'install ok installed'; then
  if [[ "${EUID}" -eq 0 ]]; then SUDO=(); else SUDO=(sudo); fi
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y python3-venv tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim
fi

if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install-deps chromium
.venv/bin/python -m playwright install chromium

printf '\n环境初始化完成。运行 ./scripts/restart.sh 启动应用。\n'
printf '确认 config.py 中的 PostgreSQL DATABASE_URL 可连接。\n'
printf 'OCR 语言：\n'
tesseract --list-langs
