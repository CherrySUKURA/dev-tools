#!/bin/bash
# Double-clickable macOS launcher; run chmod +x start.command once after cloning.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
  echo "需要 Python 3.8+，请访问 https://www.python.org/downloads/ 安装。"
  read -r -p "按回车退出..."
  exit 1
fi
PORT="${TXLC_DASHBOARD_PORT:-18000}"
URL="http://127.0.0.1:${PORT}"
if ! curl -fsS --max-time 2 "$URL/api/status" >/dev/null 2>&1; then
  mkdir -p logs
  nohup "$PYTHON" dashboard.py >> logs/launcher.log 2>&1 </dev/null &
  for ((i=0; i<40; i++)); do
    curl -fsS --max-time 1 "$URL/api/status" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi
if ! curl -fsS --max-time 2 "$URL/api/status" >/dev/null 2>&1; then
  echo "控制台启动失败，请查看 logs/launcher.log"
  read -r -p "按回车退出..."
  exit 1
fi
open "$URL/"
