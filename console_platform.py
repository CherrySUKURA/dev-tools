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


def state_dir():
    """跨平台的运行状态目录：pid、服务日志、watch 与 action 标记都放这里。

    为什么不用 temp_dir()：macOS 的 temp_dir() 是 /var/folders/...，和历史上
    /tmp/<项目>.pid 不一致；直接切过去会让正在跑的服务变成"未运行"。
    所以 POSIX 明确保持 /tmp，Windows 用 %TEMP%\\txlc（没有 /tmp 可写）。
    """
    base = Path(tempfile.gettempdir()) / "txlc" if IS_WINDOWS else Path("/tmp")
    base.mkdir(parents=True, exist_ok=True)
    return base


def pid_alive(pid):
    """判断进程是否存活。

    POSIX 用 kill -0；Windows 没有这个语义，用 tasklist 查 PID（找不到即为已退出）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in (proc.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_pid(pid, force=False):
    """终止单个进程；返回是否发出信号（不代表已退出）。

    需要连子进程一起停时用 kill_tree()；这里用于 pid 文件里记录的主进程。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        _run(cmd)
        return True
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return True
    except OSError:
        return False


def spawn_detached(cmd, cwd, log_path, env=None, append=False):
    """启动常驻进程（java / npm / watch），让它脱离当前控制台进程。

    关键点：新会话（Windows 是新进程组）、stdin 关闭、输出重定向到日志，
    这样关闭终端或重启控制台都不会把服务带走。返回 Popen 对象（.pid 可用）。
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    handle = open(log_path, mode)
    kwargs = dict(
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    if env is not None:
        kwargs["env"] = env
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kwargs)
    finally:
        # 父进程关掉自己的句柄；子进程仍持有自己的那份。
        handle.close()


def script_suffix():
    """本地脚本后缀：Windows 用 .cmd，其余用 .sh（前端启动/构建脚本按平台生成）。"""
    return ".cmd" if IS_WINDOWS else ".sh"


def open_app_window(url):
    """用"应用窗口"打开控制台地址（无地址栏），没有可用浏览器时退回默认浏览器。

    macOS 的窗口由 app/shell.swift 提供，这里只是兜底；Windows/Linux 用
    Edge/Chrome 的 --app= 模式，效果接近原生应用。
    """
    if IS_WINDOWS:
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        for exe in candidates:
            if exe.is_file():
                subprocess.Popen([str(exe), f"--app={url}"], close_fds=True)
                return True
        os.startfile(url)  # type: ignore[attr-defined]  # 仅 Windows 存在
        return True
    if IS_LINUX:
        for exe in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            path = shutil.which(exe)
            if path:
                subprocess.Popen([path, f"--app={url}"], close_fds=True)
                return True
    return open_url(url)


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


def pick_directory(prompt="选择目录"):
    """弹出系统目录选择框，返回 (路径, 是否取消, 错误信息)。

    macOS 用 AppleScript；Windows 用 PowerShell 的 FolderBrowserDialog；
    Linux 尝试 zenity/kdialog。调用方负责把结果转成接口返回结构。
    """
    if IS_MACOS:
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, False, str(exc)
        if proc.returncode != 0:
            return None, True, None
        path = (proc.stdout or "").strip().rstrip("/")
        return (path, False, None) if path else (None, True, None)

    if IS_WINDOWS:
        # FolderBrowserDialog 是 Windows 自带组件，不需要额外依赖。
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            f"$d.Description = '{prompt}';"
            "$d.ShowNewFolderButton = $true;"
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps],
                capture_output=True, text=True, timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, False, str(exc)
        path = (proc.stdout or "").strip().strip('"')
        return (path, False, None) if path else (None, True, None)

    commands = (
        ["zenity", "--file-selection", "--directory", f"--title={prompt}"],
        ["kdialog", "--getexistingdirectory", str(Path.home()), "--title", prompt],
    )
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, False, str(exc)
        if proc.returncode != 0:
            return None, True, None
        path = (proc.stdout or "").strip()
        return (path, False, None) if path else (None, True, None)
    return None, False, "当前系统没有可用的目录选择框，请手动输入路径"


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
