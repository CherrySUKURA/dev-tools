#!/bin/bash
# 兼容入口：真正的逻辑已经迁到 devctl.py（跨平台、可测试）。
#
# 保留这个 .sh 是因为 README、快捷键和用户习惯都写的是 ./devctl.sh；
# 这里只负责找到 python3 然后把参数原样转发，不再包含任何业务逻辑。
#
# 用法与以前完全一致：
#   ./devctl.sh list
#   ./devctl.sh json
#   ./devctl.sh doctor [--env]
#   ./devctl.sh start|stop|restart|update [项目|all|core]
#
# Windows 对应入口是 devctl.cmd。

set -u

TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)"

# 双击 .app / GUI 启动时 PATH 往往不完整，补上常见的 Python 安装位置。
export PATH="/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:$PATH"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "找不到 python3：请安装 Python 3 或把 python3 加入 PATH" >&2
  exit 1
fi

# exec 让退出码原样返回，控制台据此判断动作成功与否。
exec "$PYTHON_BIN" "$TOOLS_DIR/devctl.py" "$@"
