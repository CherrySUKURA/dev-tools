#!/bin/bash
# 服务控制台.app 的入口脚本：
#   1. 本地控制台服务没在跑就先拉起来（日志仍写 /tmp/txlc-dashboard.log）；
#   2. 服务就绪后 exec 原生窗口二进制，得到不带浏览器地址栏的应用窗口；
#   3. 原生二进制缺失时退化成用默认浏览器打开，保证还能用。
#
# 不打开终端窗口，所有输出都在应用页面里查看。

export PATH="/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# launcher 位于 <控制台目录>/服务控制台.app/Contents/MacOS/
BUNDLE_CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
CONSOLE_BIN="$BUNDLE_CONTENTS/MacOS/TXLCConsole"
CONSOLE_DIR="$(cd "$BUNDLE_CONTENTS/../.." && pwd)"

PORT="${TXLC_DASHBOARD_PORT:-18000}"
# 用 localhost 而不是 127.0.0.1：和页面入口/文档地址保持同一个 Host，
# 避免部分后端把纯 IP 当成非法来源直接返回 500。
URL="http://localhost:$PORT/"

PYTHON="$(command -v python3 || true)"
[ -x "$PYTHON" ] || PYTHON="/usr/bin/python3"

# 已有控制台进程时不再重复启动。
if ! curl -fsS --max-time 2 "$URL/api/status" >/dev/null 2>&1; then
  nohup "$PYTHON" "$CONSOLE_DIR/dashboard.py" >/tmp/txlc-dashboard.log 2>&1 </dev/null &
  for _ in $(seq 1 24); do
    sleep 0.5
    curl -fsS --max-time 1 "$URL/api/status" >/dev/null 2>&1 && break
  done
fi

if ! curl -fsS --max-time 2 "$URL/api/status" >/dev/null 2>&1; then
  osascript -e 'display alert "服务控制台启动失败" message "请查看 /tmp/txlc-dashboard.log"'
  exit 1
fi

if [ -x "$CONSOLE_BIN" ]; then
  # 原生窗口：把控制台目录和地址传进去，菜单里的「打开配置文件 / 浏览器入口」要用。
  TXLC_CONSOLE_DIR="$CONSOLE_DIR" TXLC_DASHBOARD_URL="$URL" exec "$CONSOLE_BIN" "$URL"
fi

# 兜底：没有原生窗口二进制（比如没装 Xcode 命令行工具时），仍用浏览器打开。
open "$URL/?t=$(date +%s)"
