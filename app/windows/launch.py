#!/usr/bin/env python3
# Windows 双击入口：确保控制台服务在跑，然后用 Edge/Chrome 的 --app 窗口打开。
#
# 和 macOS 的 app/launcher.sh 对齐：
#   1. /api/status 能通 → 直接开窗口（已有实例时不重复启动）；
#   2. 不通 → 后台启动 dashboard.py，最多等 20 秒；
#   3. 仍失败 → 弹出消息框并提示日志位置。
#
# 为什么不用 pythonw：
#   这里由 launch.cmd 调用，本身不弹黑框；dashboard.py 用 DETACHED_PROCESS 启动，
#   关掉窗口后控制台服务仍在后台，和 macOS 的行为一致。

import ctypes
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# app/windows/launch.py -> dev-tools/
ROOT = Path(__file__).resolve().parent.parent.parent
PORT = os.environ.get("TXLC_DASHBOARD_PORT", "18000")
URL = f"http://localhost:{PORT}/"
LOG = Path(os.environ.get("TEMP", ".")) / "txlc-dashboard.log"


def healthy(timeout=1.0):
    """控制台是否已经能响应 /api/status。"""
    try:
        with urllib.request.urlopen(URL + "api/status", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 任何异常都视为未就绪
        return False


def show_error(text):
    """Windows 消息框；非 Windows 上退化为 stderr（方便本地冒烟测试）。"""
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(0, text, "服务控制台", 0x10)
    else:
        print(text, file=sys.stderr)


def start_console():
    """后台启动 dashboard.py；输出写 %TEMP%\\txlc-dashboard.log。"""
    flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "ab") as log:
        subprocess.Popen(
            [sys.executable, str(ROOT / "dashboard.py")],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )


def main():
    if not healthy(2):
        start_console()
        for _ in range(40):
            time.sleep(0.5)
            if healthy(1):
                break

    if not healthy(2):
        show_error(f"服务控制台启动失败。\n请查看日志：{LOG}")
        return 1

    # 平台层负责找 Edge/Chrome 的 --app 模式；都没有时退回默认浏览器。
    sys.path.insert(0, str(ROOT))
    import console_platform as plat

    plat.open_app_window(URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
