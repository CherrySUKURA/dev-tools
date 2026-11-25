#!/usr/bin/env python3
# 平台适配层：把 macOS / Windows / Linux 的差异收敛在这里。
#
# 为什么单独一个模块：
#   控制台原来直接在 shell / dashboard.py 里调 lsof、pkill、open、/tmp，
#   这些命令在 Windows 上全部不存在。把差异收进这一层后，上层代码只调本模块的函数，
#   以后支持 Windows 只需要在这里补实现，不用改业务逻辑，也方便单独测试。
#
# 注意：本文件名不能叫 platform.py，会覆盖 Python 标准库的 platform 模块。

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")


def temp_dir():
    """返回跨平台的临时目录（macOS 是 /var/folders/... 下，Windows 是 %TEMP%）。

    控制台的 pid/日志/任务文件都放这里，避免污染项目目录；
    以前写死 /tmp，Windows 上会直接失败。
    """
    return Path(tempfile.gettempdir())


def port_open(port, host="127.0.0.1", timeout=0.8):
    """TCP 探活：能建立连接就认为端口在监听。

    健康检查用它判断服务是否真的活着（比看 pid 更准：进程可能还在但端口没起来）。
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _run(cmd, timeout=5):
    """执行系统命令，失败时返回空字符串（健康检查这类路径不能因为取不到信息就崩）。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            errors="replace",
        )
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def listening_ports():
    """列出本机所有处于 LISTEN 状态的 TCP 端口。

    返回 [{port, pid, process, address}]，pid/process 可能为空（权限不足时）。
    macOS 看 lsof，Linux 看 ss，Windows 看 netstat + tasklist。
    """
    if IS_WINDOWS:
        return _listening_ports_windows()
    if IS_LINUX:
        return _listening_ports_linux()
    return _listening_ports_macos()


def _listening_ports_macos():
    """macOS：lsof -F 输出逐条记录，p=pid、c=进程名、n=地址:端口。"""
    out = _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"])
    items, pid, process = [], "", ""
    for raw in out.splitlines():
        if not raw:
            continue
        tag, value = raw[0], raw[1:]
        if tag == "p":
            pid = value
        elif tag == "c":
            process = value
        elif tag == "n":
            # 形如 127.0.0.1:8080、*:18000、[::1]:8080
            addr, _, port_text = value.rpartition(":")
            if not port_text.isdigit():
                continue
            items.append({
                "port": int(port_text),
                "pid": int(pid) if pid.isdigit() else None,
                "process": process,
                "address": addr,
            })
    return _dedupe_ports(items)


def _listening_ports_linux():
    """Linux：优先 ss -ltnp，没有 ss 时退回 netstat。"""
    out = _run(["ss", "-ltnp"])
    items = []
    for line in out.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        port_text = local.rpartition(":")[2]
        if not port_text.isdigit():
            continue
        pid, process = None, ""
        if "users:" in line:
            chunk = line.split("users:", 1)[1]
            for part in chunk.strip("()").split(","):
                part = part.strip()
                if part.startswith("pid="):
                    digits = part[4:].split(",")[0]
                    pid = int(digits) if digits.isdigit() else None
                elif part.startswith('(("') or part.startswith('"'):
                    process = part.strip('("').split('"')[0]
        items.append({
            "port": int(port_text),
            "pid": pid,
            "process": process,
            "address": local.rpartition(":")[0],
        })
    if items:
        return _dedupe_ports(items)

    out = _run(["netstat", "-lntp"])
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4 or "LISTEN" not in line:
            continue
        port_text = fields[3].rpartition(":")[2]
        if not port_text.isdigit():
            continue
        pid_text = fields[6].split("/")[0] if len(fields) > 6 else ""
        items.append({
            "port": int(port_text),
            "pid": int(pid_text) if pid_text.isdigit() else None,
            "process": fields[6].split("/")[1] if "/" in (fields[6] if len(fields) > 6 else "") else "",
            "address": fields[3].rpartition(":")[0],
        })
    return _dedupe_ports(items)


def _listening_ports_windows():
    """Windows：netstat -ano 拿 端口→PID，再按 PID 批量查进程名。"""
    out = _run(["netstat", "-ano"])
    by_port, pids = {}, set()
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP":
            continue
        if fields[3].upper() != "LISTENING":
            continue
        port_text = fields[1].rpartition(":")[2]
        if not port_text.isdigit() or not fields[4].isdigit():
            continue
        pid = int(fields[4])
        by_port[int(port_text)] = {"port": int(port_text), "pid": pid, "process": "", "address": fields[1].rpartition(":")[0]}
        pids.add(pid)

    # 进程名查询：一次 tasklist 拿全部，避免每个端口都起一次子进程。
    names = {}
    for line in _run(["tasklist", "/FO", "CSV", "/NH"]).splitlines():
        cells = [cell.strip('"') for cell in line.split('","')]
        if len(cells) >= 2 and cells[1].isdigit():
            names[int(cells[1])] = cells[0].strip('"')
    for pid, name in names.items():
        for item in by_port.values():
            if item["pid"] == pid:
                item["process"] = name
    return list(by_port.values())


def _dedupe_ports(items):
    """同一个端口可能被 IPv4/IPv6 各监听一次，按端口去重，保留有 pid 的那条。"""
    merged = {}
    for item in items:
        key = item["port"]
        if key not in merged or (not merged[key]["pid"] and item["pid"]):
            if key in merged and merged[key]["pid"] and not item["pid"]:
                continue
            merged[key] = item
    return sorted(merged.values(), key=lambda x: x["port"])


def pid_on_port(port):
    """返回监听该端口的 PID；查不到返回 None。"""
    for item in listening_ports():
        if item["port"] == int(port) and item["pid"]:
            return item["pid"]
    return None


def process_tree(pid):
    """列出 pid 及其所有子进程（用于停止 npm 派生的 dev server 这类进程树）。"""
    pids = [int(pid)]
    if IS_WINDOWS:
        return pids
    out = _run(["ps", "-Ao", "pid=,ppid="])
    children = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        children.setdefault(int(parts[1]), []).append(int(parts[0]))
    stack = [int(pid)]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in pids:
                pids.append(child)
                stack.append(child)
    return pids


def kill_tree(pid, force=False):
    """停止进程及其子进程；返回是否发出成功信号（不代表进程已退出）。

    force=False 先温和终止（SIGTERM / taskkill 不带 /F），force=True 强制（SIGKILL / /F）。
    Windows 上 taskkill /T 已经会处理进程树。
    """
    if not pid:
        return False
    pid = int(pid)
    if IS_WINDOWS:
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        _run(cmd)
        return True

    ok = False
    if force:
        for target in reversed(process_tree(pid)):
            try:
                os.kill(target, signal.SIGKILL)
                ok = True
            except OSError:
                pass
        return ok

    # 先让子进程退出，再退父进程，避免 npm/vite 派生进程变成孤儿继续占端口。
    for target in reversed(process_tree(pid)):
        try:
            os.kill(target, signal.SIGTERM)
            ok = True
        except OSError:
            pass
    return ok


def open_url(url):
    """用默认浏览器打开地址（三平台各自的命令）。"""
    try:
        if IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]  # 仅 Windows 存在
        elif IS_MACOS:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def notify(title, message):
    """发系统通知；做不到时静默失败（不影响主流程）。

    macOS 用 osascript，Windows 用 PowerShell 的 Toast（无第三方依赖的最简做法），
    Linux 暂时只写日志。
    """
    if IS_MACOS:
        script = (
            'display notification "%s" with title "%s"'
            % (str(message).replace('"', "'"), str(title).replace('"', "'"))
        )
        _run(["osascript", "-e", script], timeout=5)
        return True
    if IS_WINDOWS:
        # Windows 10+ 的 Toast 需要注册 AppID；这里用最简的 PowerShell 气泡提示，失败就算了。
        ps = (
            "[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null;"
            "[System.Windows.Forms.MessageBox]::Show('%s','%s')"
            % (str(message).replace("'", "''"), str(title).replace("'", "''"))
        )
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


def find_executable(*names):
    """在 PATH 和常见安装位置里找一个可执行文件，返回绝对路径或 None。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    if IS_WINDOWS:
        candidates = [r"C:\Program Files\Java\jdk-17\bin\java.exe"]
    else:
        home = Path.home()
        candidates = [
            home / "dev-tools" / "apache-maven-3.9.11" / "bin" / "mvn",
            Path("/opt/homebrew/bin") / names[0],
            Path("/usr/local/bin") / names[0],
        ]
    for path in candidates:
        if Path(path).exists():
            return str(path)
    return None
