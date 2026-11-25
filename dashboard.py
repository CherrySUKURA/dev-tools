#!/usr/bin/env python3
# TXLC 本地 Java 服务 Web 控制台后端。
# 只监听 127.0.0.1，通过调用 devctl.py 完成启动/停止/更新/热更新，
# 前端通过轮询获取状态、任务输出和项目日志，不依赖终端窗口。
#
# 启动：/usr/bin/python3 dashboard.py
# 访问：http://localhost:18000

import hashlib
import http.client
import io
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 平台差异（端口列表、进程树、通知、打开浏览器）统一走适配层，见 console_platform.py。
import console_platform as plat

# 源码运行时是脚本目录；PyInstaller 打包后配置和日志放在 exe 同级目录，
# 这样升级、备份、查日志都能在安装目录里完成，不会丢到临时解包目录。
BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
# 核心逻辑在 devctl.py；Windows 上不能直接执行 .py，必须显式用当前解释器启动。
DEVCTL = BASE_DIR / "devctl.py"
PYTHON = sys.executable
# 运行状态目录：POSIX 是 /tmp（保持历史 pid/日志路径），Windows 是 %TEMP%\txlc。
STATE_DIR = plat.state_dir()
HTML_FILE = BASE_DIR / "dashboard.html"
ENTRIES_CONF = BASE_DIR / "entries.conf"
PROJECTS_CONF = BASE_DIR / "projects.conf"
PORTS_CONF = BASE_DIR / "ports.conf"
SERVICES_DIR = BASE_DIR / "services"
JOB_DIR = STATE_DIR / "txlc-dashboard" / "jobs"
WATCH_DIR = STATE_DIR / "txlc-dashboard" / "watch"
# 控制台自身的日志目录：跟业务项目日志分开，重启后还能查；按大小轮转。
# 业务日志仍在项目目录/临时目录，诊断面板会把它们汇总到一起。
LOG_DIR = Path(os.environ.get("TXLC_LOG_DIR") or (BASE_DIR / "logs"))
# 页面每 1~2 秒轮询一次的只读接口：成功请求不写访问日志，否则一天上万行，
# 真正的报错会被挤掉。失败的请求（非 200）仍然记录。
QUIET_POLL_PATHS = frozenset({
    "/api/status", "/api/log", "/api/job", "/api/jobs",
    "/api/diagnostics", "/api/diagnostics/tail",
})
HOST = "127.0.0.1"
PORT = int(os.environ.get("TXLC_DASHBOARD_PORT", "18000"))
# 控制台对外展示、生成给浏览器打开的本地地址使用的主机名。
# 监听仍然固定 127.0.0.1（不对外暴露），但链接统一写成 localhost：
# 用纯 IP 访问时，部分旧系统和第三方登录会按 Host 判断来源，
# 把 127.0.0.1 当成非法来源直接返回 500，换成 localhost 后正常。
LOCAL_HOST = (os.environ.get("TXLC_LOCAL_HOST") or "localhost").strip() or "localhost"

# 旧系统 Tomcat 入口代理：浏览器无法直接设置 Host 头，控制台在这里代转，
# 让 txlcsystem 的域名入口校验（sub_entry_info）在 127.0.0.1 本地也能通过。
LEGACY_PROXY_PREFIXES = ("/efarm",)
LEGACY_PROXY_TARGET = ("127.0.0.1", 8080)
LEGACY_PROXY_HOST_HEADER = (os.environ.get("TXLC_LEGACY_PROXY_HOST") or "").strip()


def legacy_proxy_host():
    """旧系统入口代理用的 Host 头。

    优先级：环境变量 TXLC_LEGACY_PROXY_HOST → legacy-proxy.conf 第一行有效内容 → localhost。
    真实域名属于公司内部信息，不写进代码；放到 gitignore 的 legacy-proxy.conf 里，
    换机器时补一行即可，不影响其他项目。
    """
    if LEGACY_PROXY_HOST_HEADER:
        return LEGACY_PROXY_HOST_HEADER
    conf = BASE_DIR / "legacy-proxy.conf"
    try:
        for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return "localhost"

HEALTH_CONF = BASE_DIR / "health.conf"
# 外部依赖登记表：MySQL / Redis 这类不在本机、又不归控制台管的中间件。
DEPS_CONF = BASE_DIR / "deps.conf"
HEALTH_EVAL_EVERY = 2.0      # 健康检查线程的调度粒度（秒）
HEALTH_RESTART_COOLDOWN = 20.0  # 同一次自动重启后的冷却时间，避免连续重启打爆机器
HEALTH_RECOVER_RESET = 60.0  # 健康状态持续正常多久后，把重启次数预算还回去
HEALTH_START_GRACE = 90.0   # 启动/重启后多少秒内不判故障（Spring Boot 冷启动可能很久）
DEPS_REFRESH_EVERY = 15.0   # 外部依赖探活周期（秒）

# 外部依赖状态：名称 -> {name, kind, host, port, desc, ok, latency_ms, error, checked}
dep_state = {}
dep_lock = threading.Lock()
deps_refreshing = False

# 按依赖顺序执行的编排任务；同一时刻只允许一个，进度通过 /api/status 暴露。
ordered_task = None
ordered_lock = threading.Lock()

# 健康状态：项目名 -> {state, failures, restarts, last_check, last_ok, last_error, restart_at}
health_state = {}
health_lock = threading.Lock()
# health.conf 解析结果缓存：文件没变就不重复解析。
health_conf_cache = {"mtime": None, "data": {}}
health_conf_lock = threading.Lock()

ACTIONS = {
    "start", "stop", "restart", "update", "watch", "unwatch",
    "install", "gitpull", "gitfetch", "gitstash", "gitunstash",
    "start-bg", "stop-bg", "restart-bg",
}
# 启动/停止/重启走异步 wrapper，不占用更新任务队列；接口立即返回，前端轮询状态。
FAST_ACTIONS = {"start-bg": "start", "stop-bg": "stop", "restart-bg": "restart"}
MAX_JOB_LINES = 400
# 提交 diff 的展示上限：先按字符截断 git 输出，再按总行数限制，避免超大提交把页面拖死。
MAX_PATCH_CHARS = 600000
MAX_DIFF_LINES = 5000
MAX_LOG_LINES = 2000
LOG_TAIL_BYTES = 1024 * 1024
HOME_DIR = Path.home()

# jobs 保存当前及最近任务；active_job_id 用于串行执行变更类操作。
jobs = {}
jobs_lock = threading.Lock()
active_job_id = None
server_ref = None

# 多个标签页/轮询可能同时请求 /api/status；缓存并串行化 devctl json，
# 避免 14 个项目反复执行 find/lsof 把机器打满。
status_cache = {"data": None, "time": 0.0, "refreshing": False}
status_cache_lock = threading.Lock()
STATUS_REFRESH_AFTER = 1.0

# 每个项目的 git 状态单独缓存：git status 在大仓库上可能上百毫秒，
# 不能跟着 1 秒一次的状态轮询重复执行；过期后由后台线程刷新，本次先用旧值。
git_cache = {}
git_cache_lock = threading.Lock()
GIT_CACHE_TTL = 8.0

# 添加项目：项目名会写进 projects.conf，只允许字母数字和 ._-，避免命令注入和解析歧义。
PROJECT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SCRIPT_NAME_RE = re.compile(r"[A-Za-z0-9:_./-]{1,80}")
PACKAGE_MANAGERS = ("npm", "pnpm", "yarn", "bun")
project_conf_lock = threading.Lock()

# 前端框架默认端口：用于检测后给一个合理建议，实际端口仍以用户填写为准。
FRONTEND_FRAMEWORK_PORTS = {
    "vite": 5173, "svelte": 5173, "next": 3000, "nuxt": 3000,
    "cra": 3000, "umi": 8000, "vue-cli": 8080, "angular": 4200,
    "generic": 3000,
}
# 前端启动时传给 dev 脚本的端口参数；读 PORT 环境变量的框架留空。
FRONTEND_PORT_FLAGS = {
    "vite": "--port", "svelte": "--port", "next": "-p", "nuxt": "--port",
    "umi": "--port", "vue-cli": "--port", "angular": "--port",
}
FRONTEND_FRAMEWORK_LABELS = {
    "vite": "Vite", "svelte": "SvelteKit", "next": "Next.js", "nuxt": "Nuxt",
    "cra": "Create React App", "umi": "Umi", "vue-cli": "Vue CLI",
    "angular": "Angular", "generic": "通用 Node 项目",
}



# 控制台自身日志：用标准 logging + 文件轮转，出问题时有固定位置可查。
logger = logging.getLogger("txlc.console")


def configure_logging():
    """把控制台进程日志写到 <控制台目录>/logs/console.log（2MB × 3 轮转）。

    为什么不只依赖启动器的 stdout 重定向：那个文件在临时目录，重启机器就没了；
    控制台自己的报错（500、异常堆栈、退出原因）需要留下来才能定位问题。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    file_handler = RotatingFileHandler(
        LOG_DIR / "console.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    logger.info(
        "控制台启动：http://%s:%s，目录 %s，Python %s",
        LOCAL_HOST, PORT, BASE_DIR, sys.version.split()[0],
    )


def run_devctl(args, timeout=20):
    """同步调用 devctl.py 并返回 (returncode, stdout, stderr)。"""
    proc = subprocess.run(
        devctl_command(*args),
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        # Windows 控制台默认编码可能是 GBK；统一按 UTF-8 解码，避免中文日志乱码。
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=devctl_env(),
    )
    return proc.returncode, proc.stdout, proc.stderr


def detach_kwargs():
    """让子进程脱离控制台：POSIX 用新会话，Windows 用新进程组。"""
    if plat.IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def devctl_env():
    """给 devctl 子进程指定工具目录（打包后它自己的 __file__ 在解包目录里）。"""
    env = dict(os.environ)
    env["TXLC_TOOLS_DIR"] = str(BASE_DIR)
    return env


def devctl_command(*args):
    """devctl 的启动命令。

    源码模式：python devctl.py <args>；
    PyInstaller 模式：同一个 exe 带 --devctl 再进来，由 main() 分发到 devctl，
    避免依赖用户机器上是否还留着 devctl.py。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--devctl"] + list(args)
    return [PYTHON, str(DEVCTL)] + list(args)


def app_version():
    """用控制台相关文件的修改时间生成版本号，前端检测到变化后自动刷新。"""
    files = [HTML_FILE, ENTRIES_CONF, Path(__file__).resolve(), DEVCTL,
             BASE_DIR / "console_platform.py", HEALTH_CONF]
    mtimes = []
    for path in files:
        try:
            mtimes.append(int(path.stat().st_mtime_ns))
        except OSError:
            pass
    return str(max(mtimes) if mtimes else 0)


def _refresh_status_cache():
    """后台刷新状态缓存，不阻塞 /api/status。"""
    try:
        code, out, err = run_devctl(["json"], timeout=20)
        if code == 0:
            data = json.loads(out)
            with status_cache_lock:
                status_cache["data"] = data
                status_cache["time"] = time.time()
    except Exception:
        pass
    finally:
        with status_cache_lock:
            status_cache["refreshing"] = False


def get_entries():
    """读取 entries.conf，返回 {项目名: [{label, url, description}, ...]}。"""
    entries = {}
    if not ENTRIES_CONF.exists():
        return entries
    for raw in ENTRIES_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
            continue
        project, label, url = parts[0], parts[1], parts[2]
        description = parts[3] if len(parts) > 3 else ""
        entries.setdefault(project, []).append({
            "label": label,
            "url": url,
            "description": description,
        })
    return entries


def service_log_sources(project):
    """收集项目可查看的日志文件：应用日志、错误日志、启动日志。"""
    name = project.get("name", "")
    project_dir = Path(project.get("dir") or ".")
    sources = []
    seen_paths = set()

    def add(key, label, path):
        path = Path(path)
        try:
            key_path = str(path.resolve())
        except OSError:
            key_path = str(path)
        if key_path in seen_paths:
            return
        if path.exists() and path.is_file():
            seen_paths.add(key_path)
            sources.append({"key": key, "label": label, "path": str(path)})

    # Spring Boot 服务默认把应用日志/错误日志写到 ~/logs/<项目名>/。
    app_home = HOME_DIR / "logs" / name
    add("app", "应用日志", app_home / "app.log")
    add("error", "错误日志", app_home / "error.log")

    # 旧 efarm / Tomcat / gateway 项目把日志写在项目目录 logs/ 下。
    legacy_dir = project_dir / "logs"
    nested_log_dir = legacy_dir / name
    add("app", "应用日志", nested_log_dir / "app.log")
    add("error", "错误日志", nested_log_dir / "error.log")
    add("debug", "调试日志", nested_log_dir / "debug.log")
    if legacy_dir.is_dir():
        candidates = [item for item in legacy_dir.rglob("*.log") if item.is_file()]
        if candidates:
            latest = max(candidates, key=lambda item: item.stat().st_mtime)
            add("app", "应用日志", latest)

    # 启动输出始终保留，应用日志为空时也能看启动报错。
    add("startup", "启动日志", project.get("log") or str(STATE_DIR / f"{name}.log"))
    return sources


def _read_git_status(path):
    """读取一个项目的分支、未提交改动数、领先/落后远端提交数。

    使用 porcelain=v2 的 --branch 输出，一次 git 调用拿全所需信息；
    未跟踪文件不计入改动数，和 devctl.py“有未提交改动就跳过 pull”的判定保持一致。
    """
    if not path:
        return {"ok": False}
    project_dir = Path(path)
    if not project_dir.is_dir():
        return {"ok": False}

    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "status", "--porcelain=v2",
             "--branch", "--untracked-files=no"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return {"ok": False}
    if proc.returncode != 0:
        return {"ok": False}

    branch = ""
    upstream = ""
    ahead = 0
    behind = 0
    dirty = 0
    for line in proc.stdout.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
        elif line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream "):].strip()
        elif line.startswith("# branch.ab "):
            match = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if match:
                ahead = int(match.group(1))
                behind = int(match.group(2))
        elif line[:2] in ("1 ", "2 ", "u "):
            dirty += 1

    return {
        "ok": True,
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
    }


def _refresh_git_cache(path):
    """后台刷新 git 缓存，避免阻塞 /api/status。"""
    data = _read_git_status(path)
    with git_cache_lock:
        entry = git_cache.get(path) or {}
        entry["data"] = data
        entry["time"] = time.time()
        entry["refreshing"] = False
        git_cache[path] = entry


def get_git_status(path):
    """返回缓存的 git 状态；首次调用返回 None 并触发后台加载。"""
    if not path:
        return None
    key = str(path)
    now = time.time()
    with git_cache_lock:
        entry = git_cache.get(key)
        if entry and entry.get("data") is not None and now - entry.get("time", 0) < GIT_CACHE_TTL:
            return entry["data"]
        spawn = not entry or not entry.get("refreshing")
        if entry is None:
            entry = {"data": None, "time": 0.0, "refreshing": True}
            git_cache[key] = entry
        elif spawn:
            entry["refreshing"] = True
        data = entry.get("data")

    if spawn:
        threading.Thread(target=_refresh_git_cache, args=(key,), daemon=True).start()
    return data


# 本机地址的可选写法：历史配置里可能存成 127.0.0.1，展示时统一换成 LOCAL_HOST。
LOCAL_URL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]", LOCAL_HOST)
LOCAL_URL_RE = re.compile(
    r"^(https?://)(" + "|".join(re.escape(host) for host in dict.fromkeys(LOCAL_URL_HOSTS)) + r")(:\d+)?(.*)$",
    re.IGNORECASE,
)


def normalize_local_host(url):
    """把本机地址里的主机名换成 LOCAL_HOST，路径和端口保持不变。

    只处理 127.0.0.1 / localhost / LOCAL_HOST 这类本机地址；
    外部地址（含 /efarm 这类相对路径）原样返回，不会把别人的域名改坏。
    """
    text = str(url or "").strip()
    match = LOCAL_URL_RE.match(text)
    if not match:
        return text
    return f"{match.group(1)}{LOCAL_HOST}{match.group(3) or ''}{match.group(4)}"


def local_url_with_port(url, port):
    """把本机地址的端口换成实际端口，主机名统一成 LOCAL_HOST，保留路径。

    外部地址原样返回；地址为空时按本机默认地址补一个。
    """
    text = str(url or "").strip()
    match = LOCAL_URL_RE.match(text)
    if not match:
        return text or f"http://{LOCAL_HOST}:{port}/"
    return f"{match.group(1)}{LOCAL_HOST}:{port}{match.group(4) or '/'}"


def attach_entries(projects):
    """把前端入口、日志源和 git 状态挂到对应项目上，供控制台渲染。"""
    entries = get_entries()
    for project in projects:
        # 文档地址统一显示成 localhost，配置文件里的 127.0.0.1 不改，
        # 这样浏览器地址栏和页面入口的 Host 一致，不会被后端当成非法来源。
        project["doc"] = normalize_local_host(project.get("doc"))
        project_entries = entries.get(project.get("name"), [])
        # 入口里也可能写着 127.0.0.1，展示前同样归一化；相对路径（/efarm/...）保持原样。
        for entry in project_entries:
            entry["url"] = normalize_local_host(entry.get("url"))
        # 前端项目没有单独配置入口时，默认给一个首页入口，
        # 这样卡片始终能看到“前端页面”，不用再记端口。
        # 文档地址里即使写的是旧端口，也按当前生效端口展示。
        if not project_entries and project.get("type") == "frontend":
            project_entries = [{
                "label": "前端页面",
                "url": local_url_with_port(project.get("doc"), project.get("port")),
                "description": "前端开发服务器首页（启动后可打开）",
            }]
        project["entries"] = project_entries
        project["git"] = get_git_status(project.get("dir"))
        project["log_sources"] = {
            "service": service_log_sources(project),
            "action": [],
        }
        action_log = STATE_DIR / f"{project.get('name')}.action.log"
        if action_log.exists():
            project["log_sources"]["action"].append({
                "key": "action",
                "label": "操作日志",
                "path": str(action_log),
            })
    # 挂上健康状态：卡片上的绿/黄/红点和自动重启次数都来自这里。
    return attach_health(projects)


GIT_REF_RE = re.compile(r"[A-Za-z0-9._/@{}^~:-]{1,120}")


def valid_git_ref(ref):
    """校验前端传来的分支/提交引用，避免把 -xxx 这种参数当成 git 选项。"""
    return bool(GIT_REF_RE.fullmatch(ref or "")) and not ref.startswith("-")


def read_git_detail(path, ref="", limit=20):
    """读取“代码”弹窗要展示的 git 明细：分支、改动文件、提交历史、暂存记录。

    ref 为空表示当前分支（HEAD），"__all__" 表示所有分支（含远程）的合并历史，
    其它值可以是本地分支名或 origin/xxx 这类远程分支名。
    与状态轮询用的 _read_git_status 分开：这里只在用户打开弹窗或点刷新时调用，
    所以可以直接执行 git 命令拿完整数据，不需要加缓存。
    """
    if not path:
        return {"ok": False, "error": "项目路径为空"}
    project_dir = Path(path)
    if not project_dir.is_dir():
        return {"ok": False, "error": "项目目录不存在"}

    ref = (ref or "").strip()
    if ref and ref != "__all__" and not valid_git_ref(ref):
        return {"ok": False, "error": "分支名不合法"}

    def git(*args, timeout=5):
        try:
            proc = subprocess.run(
                ["git", "-C", str(project_dir), *args],
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    # 用 rev-parse 判断是否在 git 工作区内，前端项目放在仓库子目录时也能用。
    if (git("rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return {"ok": False, "error": "不是 git 仓库"}

    branch = (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    upstream = (git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "").strip()

    ahead = behind = 0
    counts = git("rev-list", "--left-right", "--count", "@{u}...HEAD") if upstream else None
    if counts:
        parts = counts.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])

    # 分支列表：本地 + 远程跟踪分支。%(symref) 非空的是 origin/HEAD 这类别名，跳过。
    # 用制表符分隔，分支名里不会出现制表符，解析比空格可靠。
    branches = []
    branch_raw = git(
        "for-each-ref", "--sort=-committerdate",
        "--format=%(refname)%09%(refname:short)%09%(HEAD)%09%(upstream:short)%09%(committerdate:relative)%09%(symref)",
        "refs/heads", "refs/remotes",
    ) or ""
    for line in branch_raw.splitlines():
        cols = line.split("\t")
        if len(cols) < 5:
            continue
        full, short, head_mark, up, when = cols[:5]
        symref = cols[5] if len(cols) > 5 else ""
        if symref or not short or short.endswith("/HEAD"):
            continue
        branches.append({
            "name": short,
            "kind": "local" if full.startswith("refs/heads/") else "remote",
            "current": head_mark.strip() == "*",
            "upstream": up,
            "when": when,
        })

    # 提交历史：默认看当前分支，可切远程分支或全部；limit 由“加载更多”递增。
    view_all = ref == "__all__"
    view_ref = "HEAD" if not ref or view_all else ref
    try:
        limit = max(10, min(int(limit or 20), 200))
    except (TypeError, ValueError):
        limit = 20

    log_args = [
        "log", f"-{limit}",
        "--pretty=format:%h%x1f%an%x1f%ae%x1f%ar%x1f%at%x1f%s%x1f%D",
    ]
    if view_all:
        log_args.append("--all")
    elif view_ref != "HEAD":
        log_args.append(view_ref)

    commits = []
    log_out = git(*log_args) or ""
    for line in log_out.splitlines():
        parts = line.split("\x1f")
        if len(parts) >= 6:
            commits.append({
                "hash": parts[0], "author": parts[1], "email": parts[2],
                "when": parts[3], "timestamp": int(parts[4] or 0),
                "subject": parts[5], "refs": parts[6] if len(parts) > 6 else "",
            })

    # 总数用于“加载更多”按钮显示进度；算不出来时退回已加载条数。
    count_args = ["rev-list", "--count"]
    count_args.append("--all" if view_all else view_ref)
    total_raw = (git(*count_args) or "").strip()
    try:
        total = int(total_raw)
    except ValueError:
        total = len(commits)

    # porcelain=v1 便于直接把 "XY 路径" 解析成改动列表；包含未跟踪文件。
    files = []
    unmerged = 0
    raw = git("status", "--porcelain", "--untracked-files=all") or ""
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        if code in UNMERGED_CODES:
            unmerged += 1
        files.append({"code": code.strip() or "?", "path": line[3:]})

    stashes = []
    stash_out = git("stash", "list", "-5") or ""
    for line in stash_out.splitlines():
        stash_ref, _, subject = line.partition(": ")
        entry = {"ref": stash_ref, "subject": subject, "blocked": False}
        stashes.append(entry)

    # 最近 3 条暂存额外判断“直接恢复会不会被 git 拒绝”：
    # 解冲突时改动已经手动恢复过的话，再点恢复必然报
    # error: Your local changes ... would be overwritten，
    # 这里提前标出来，引导用户直接删除这条记录而不是反复恢复。
    for entry in stashes[:3]:
        if valid_stash_ref(entry["ref"]):
            entry["blocked"] = _stash_apply_blocked(project_dir, entry["ref"])

    dirty = len([f for f in files if not f["code"].startswith("?")])
    return {
        "ok": True,
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
        "untracked": len(files) - dirty,
        "files": files[:60],
        "branches": branches,
        "ref": ref,
        "view_ref": view_ref if not view_all else "全部分支",
        "commits": commits,
        "total": total,
        "has_more": limit < total,
        "stashes": stashes,
        "unmerged": unmerged,
    }


# ---------------------------------------------------------------
# 冲突解决
# stash pop / pull / merge 之后，索引里会出现“未合并路径”（UU 等），
# 此时 git stash push 会直接失败（could not write index），
# 控制台原来的“暂存失败”就是这么来的。这里提供冲突列表 + 逐块选择保留哪一侧，
# 解决完再 git add 标记已解决，索引才能继续用。
# ---------------------------------------------------------------

# git status 里两位状态码都表示“未合并”的组合。
UNMERGED_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
UNMERGED_LABELS = {
    "UU": "双方都修改", "AA": "双方都新增", "DD": "双方都删除",
    "AU": "我方新增", "UA": "对方新增", "DU": "我方删除", "UD": "对方删除",
}
# 冲突标记：<<<<<<< 名字 / ||||||| 基线 / ======= / >>>>>>> 名字。
GIT_MARK_START = re.compile(r"^<{7}(?:\s(.*))?$")
GIT_MARK_BASE = re.compile(r"^\|{7}(?:\s(.*))?$")
GIT_MARK_SEP = re.compile(r"^={7}\s*$")
GIT_MARK_END = re.compile(r"^>{7}(?:\s(.*))?$")
# 返回给前端的上限：超大文件只给“整文件选一侧”，避免弹窗被拖死。
MAX_CONFLICT_HUNKS = 40
MAX_CONFLICT_HUNK_LINES = 80
MAX_CONFLICT_FILE_BYTES = 2 * 1024 * 1024


def _git_run(project_dir, *args, timeout=15):
    """在项目目录执行 git，返回 (退出码, stdout, stderr)；异常按失败处理。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _split_conflicted_text(text):
    """把带冲突标记的文件拆成「普通文本段 / 冲突段」，解析和重建共用一套结果。

    返回 (segments, newline)：
    - segments 中 text 段保留原始行，conflict 段带 ours/theirs 两侧行，
      以及 <<<<<<< / >>>>>>> 后面 git 写的名字（如 Updated upstream / Stashed changes）。
    - newline 用于重建时还原原文件的换行风格（CRLF/LF）。
    标记不完整（文件被手改坏、被截断）时按普通文本处理，宁可少解析也不丢内容。
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    segments = []
    buffer = []
    index = 0
    i = 0
    while i < len(lines):
        start = GIT_MARK_START.match(lines[i])
        if not start:
            buffer.append(lines[i])
            i += 1
            continue
        if buffer:
            segments.append({"type": "text", "lines": buffer})
            buffer = []

        marker_line = lines[i]
        line_no = i + 1
        ours_label = (start.group(1) or "").strip()
        ours, base, theirs = [], [], []
        current = ours
        end_label = ""
        closed = False
        i += 1
        while i < len(lines):
            line = lines[i]
            if GIT_MARK_BASE.match(line):
                current = base
                i += 1
                continue
            if GIT_MARK_SEP.match(line):
                current = theirs
                i += 1
                continue
            end = GIT_MARK_END.match(line)
            if end:
                end_label = (end.group(1) or "").strip()
                i += 1
                closed = True
                break
            current.append(line)
            i += 1

        if not closed:
            segments.append({"type": "text", "lines": [marker_line] + ours + base + theirs})
            continue

        segments.append({
            "type": "conflict",
            "index": index,
            "line": line_no,
            "ours_label": ours_label or "当前分支",
            "theirs_label": end_label or "冲突改动",
            "ours": ours,
            "base": base,
            "theirs": theirs,
        })
        index += 1

    if buffer:
        segments.append({"type": "text", "lines": buffer})
    return segments, newline


def _unmerged_entries(project_dir):
    """列出未合并路径，返回 (entries, error)；entries 为 [{path, code, label}]。"""
    code, out, err = _git_run(project_dir, "status", "--porcelain", "--untracked-files=no")
    if code != 0:
        return None, (err or out).strip() or "读取 git 状态失败"
    entries = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        if xy not in UNMERGED_CODES:
            continue
        path = line[3:]
        # 重命名冲突会写成 "旧名 -> 新名"，取实际存在的新名。
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append({
            "path": path,
            "code": xy,
            "label": UNMERGED_LABELS.get(xy, "冲突"),
        })
    return entries, ""


def read_git_conflicts(path):
    """读取冲突明细：哪些文件没解决、每个文件有哪几处冲突、两侧各是什么内容。

    只在用户打开冲突面板/刷新时调用，所以直接读文件，不加缓存。
    """
    if not path:
        return {"ok": False, "error": "项目路径为空"}
    project_dir = Path(path)
    if not project_dir.is_dir():
        return {"ok": False, "error": "项目目录不存在"}
    entries, error = _unmerged_entries(project_dir)
    if entries is None:
        return {"ok": False, "error": error}

    files = []
    for entry in entries:
        rel = entry["path"]
        item = dict(entry, hunks=[], hunk_count=0, truncated=False, editable=True, note="")
        abs_path = project_dir / rel
        try:
            size = abs_path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_CONFLICT_FILE_BYTES:
            item.update(editable=False, note=f"文件超过 {MAX_CONFLICT_FILE_BYTES // 1024 // 1024}MB，只能整文件选一侧")
            files.append(item)
            continue
        try:
            raw_bytes = abs_path.read_bytes()
        except OSError:
            raw_bytes = b""
        # NUL 字节基本可以断定是二进制（图片、jar、class 等），逐块选择没有意义。
        if b"\x00" in raw_bytes:
            item.update(editable=False, note="二进制文件，只能整文件选一侧")
            files.append(item)
            continue
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            item.update(editable=False, note="不是 UTF-8 文本，只能整文件选一侧")
            files.append(item)
            continue

        segments, _ = _split_conflicted_text(text)
        hunks = []
        for segment in segments:
            if segment["type"] != "conflict":
                continue
            hunk = {
                "index": segment["index"],
                "line": segment["line"],
                "ours_label": segment["ours_label"],
                "theirs_label": segment["theirs_label"],
                "ours": segment["ours"][:MAX_CONFLICT_HUNK_LINES],
                "theirs": segment["theirs"][:MAX_CONFLICT_HUNK_LINES],
                "ours_total": len(segment["ours"]),
                "theirs_total": len(segment["theirs"]),
                "has_base": bool(segment["base"]),
            }
            hunks.append(hunk)
        item["hunks"] = hunks[:MAX_CONFLICT_HUNKS]
        item["hunk_count"] = len(hunks)
        item["truncated"] = len(hunks) > MAX_CONFLICT_HUNKS
        if item["truncated"]:
            # 冲突块太多时逐块选择容易漏选，整文件选一侧更稳妥。
            item.update(editable=False, note=f"冲突超过 {MAX_CONFLICT_HUNKS} 处，只能整文件选一侧")
        if not hunks:
            item.update(editable=False, note="没有找到冲突标记，只能整文件选一侧")
        files.append(item)

    return {"ok": True, "conflicts": files, "count": len(files)}


def _safe_conflict_path(project_dir, rel):
    """把冲突文件路径限制在项目目录内，避免前端传入 ../ 写到仓库外。"""
    rel = str(rel or "").strip()
    if not rel:
        return None
    root = project_dir.resolve()
    target = (project_dir / rel).resolve()
    if target == root or root not in target.parents:
        return None
    return target


def _invalidate_git_cache(project_dir):
    """解决冲突后清掉状态缓存，让卡片/弹窗立刻看到新的改动数。"""
    # 调用方可能传 str（project["dir"]）也可能传 Path，统一成 Path 再比较。
    project_dir = Path(project_dir)
    with git_cache_lock:
        for key in (str(project_dir), str(project_dir.resolve())):
            git_cache.pop(key, None)
        # 缓存 key 可能带尾部分隔符差异，兜底按前缀匹配一次。
        for key in list(git_cache):
            if Path(key).resolve() == project_dir.resolve():
                git_cache.pop(key, None)


def resolve_git_conflicts(payload):
    """按前端选择解决冲突，返回 {ok, resolved, errors, conflicts}。

    两种模式：
    - mode=side：整文件取一侧（ours=当前分支，theirs=冲突改动），path 为空表示全部文件；
      内部用 git checkout --ours/--theirs，适合二进制或大文件。
    - mode=hunks：逐块选择（ours/theirs/both/base），要求每个文件所有冲突块都选完，
      重建文件后确认没有残留标记才写入，再 git add 标记已解决。
    """
    project_name = str(payload.get("project") or "")
    project = next(
        (item for item in get_projects() if item["name"] == project_name),
        None,
    )
    if not project:
        return {"ok": False, "error": "未知项目"}
    project_dir = Path(project.get("dir") or "")
    if not project_dir.is_dir():
        return {"ok": False, "error": "项目目录不存在"}

    current = read_git_conflicts(str(project_dir))
    if not current.get("ok"):
        return current
    conflicts = {item["path"]: item for item in current.get("conflicts", [])}
    if not conflicts:
        return {"ok": False, "error": "当前没有未解决的冲突"}

    mode = str(payload.get("mode") or "side")
    resolved = []
    errors = []

    if mode == "side":
        side = str(payload.get("side") or "")
        if side not in ("ours", "theirs"):
            return {"ok": False, "error": "只能选择保留当前分支或冲突改动一侧"}
        wanted = str(payload.get("path") or "").strip()
        targets = [wanted] if wanted else list(conflicts)
        for rel in targets:
            if rel not in conflicts:
                errors.append(f"{rel}：不是未解决的冲突文件")
                continue
            code, out, err = _git_run(project_dir, "checkout", f"--{side}", "--", rel)
            if code != 0:
                errors.append(f"{rel}：{(err or out).strip() or '取版本失败'}")
                continue
            code, out, err = _git_run(project_dir, "add", "--", rel)
            if code != 0:
                errors.append(f"{rel}：标记已解决失败 {(err or out).strip()}")
                continue
            resolved.append(rel)
    elif mode == "hunks":
        files = payload.get("files") or []
        if not files:
            return {"ok": False, "error": "没有传入要解决的冲突块"}
        for item in files:
            rel = str(item.get("path") or "").strip()
            conflict = conflicts.get(rel)
            if not conflict:
                errors.append(f"{rel}：不是未解决的冲突文件")
                continue
            if not conflict.get("editable"):
                errors.append(f"{rel}：{conflict.get('note') or '该文件不支持逐块选择'}")
                continue
            abs_path = _safe_conflict_path(project_dir, rel)
            if abs_path is None or not abs_path.is_file():
                errors.append(f"{rel}：文件路径不合法")
                continue
            try:
                # 用 bytes 解码而不是 read_text，避免把 CRLF 统一成 LF 后再写回去。
                text = abs_path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                errors.append(f"{rel}：读取失败，建议整文件选一侧")
                continue
            segments, newline = _split_conflicted_text(text)
            total = sum(1 for segment in segments if segment["type"] == "conflict")
            picks = item.get("picks") or {}
            missing = [
                index for index in range(total)
                if str(picks.get(str(index), picks.get(index, ""))) not in ("ours", "theirs", "both", "base")
            ]
            if missing:
                errors.append(f"{rel}：还有 {len(missing)} 处冲突没选（第 {', '.join(str(i + 1) for i in missing[:5])} 处）")
                continue

            rebuilt = []
            for segment in segments:
                if segment["type"] == "text":
                    rebuilt.extend(segment["lines"])
                    continue
                pick = str(picks.get(str(segment["index"]), picks.get(segment["index"], "")))
                if pick == "ours":
                    rebuilt.extend(segment["ours"])
                elif pick == "theirs":
                    rebuilt.extend(segment["theirs"])
                elif pick == "base":
                    rebuilt.extend(segment["base"])
                else:  # both：两侧都保留，先当前分支再冲突改动
                    rebuilt.extend(segment["ours"])
                    rebuilt.extend(segment["theirs"])

            content = newline.join(rebuilt)
            if text.endswith(("\n", "\r")):
                content += newline
            # 写入前再确认一次没有残留标记，避免把 <<<<<<< 提交进仓库。
            check, _ = _split_conflicted_text(content)
            if any(segment["type"] == "conflict" for segment in check):
                errors.append(f"{rel}：重建后仍有冲突标记，已跳过")
                continue
            try:
                with open(abs_path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(content)
            except OSError as exc:
                errors.append(f"{rel}：写入失败 {exc}")
                continue
            code, out, err = _git_run(project_dir, "add", "--", rel)
            if code != 0:
                errors.append(f"{rel}：标记已解决失败 {(err or out).strip()}")
                continue
            resolved.append(rel)
    else:
        return {"ok": False, "error": "不支持的解决方式"}

    _invalidate_git_cache(project_dir)
    remaining = read_git_conflicts(str(project_dir))
    return {
        "ok": bool(resolved) and not errors,
        "resolved": resolved,
        "errors": errors,
        "conflicts": remaining.get("conflicts", []),
        "count": remaining.get("count", 0),
    }


def valid_stash_ref(ref):
    """暂存引用只允许 stash@{数字}，避免把前端字符串当 git 选项执行。"""
    return bool(re.fullmatch(r"stash@\{\d+\}", str(ref or "").strip()))


def _stash_apply_blocked(project_dir, ref):
    """判断“直接恢复这条暂存”会不会被 git 拒绝。

    git 只在“暂存改了这个文件、工作区也改了这个文件”时报
    error: Your local changes ... would be overwritten。
    所以逐个检查暂存动过的路径是否在当前工作区有改动即可，
    命中就提前提示用户：内容可能已经在工作区了，别再反复点恢复。
    """
    code, out, _ = _git_run(project_dir, "diff", "--name-only", f"{ref}^", ref)
    if code != 0:
        return False
    paths = [line for line in out.splitlines() if line.strip()]
    for path in paths:
        # 工作区（含暂存区）相对 HEAD 有改动 → 这条路径上恢复会冲突。
        code, _, _ = _git_run(project_dir, "diff", "--quiet", "HEAD", "--", path)
        if code == 1:
            return True
    return False


def apply_git_stash(project_dir, ref):
    """恢复指定暂存（apply 不 drop），失败时把常见原因翻译成可执行建议。

    - “would be overwritten”：这条暂存的内容已经在工作区里（比如解冲突时已手动恢复），
      再恢复必然被拒绝，应该直接删除该记录。
    - 产生冲突：去“冲突”面板逐块选择。
    """
    ref = str(ref or "").strip()
    if not valid_stash_ref(ref):
        return {"ok": False, "error": "暂存引用不合法"}
    code, out, err = _git_run(project_dir, "stash", "apply", ref, timeout=60)
    raw = ((out or "") + (err or "")).strip()
    if code == 0:
        _invalidate_git_cache(project_dir)
        return {"ok": True, "message": f"已恢复 {ref}", "hint": "", "stashes": _git_stash_list(project_dir)}
    hint = ""
    lowered = raw.lower()
    if "would be overwritten" in lowered:
        hint = "这条暂存的内容已经在工作区里（解冲突时可能已经恢复过），不需要再恢复，直接删除这条记录即可"
    elif "conflict" in lowered or "needs merge" in lowered:
        hint = "恢复产生冲突，去上方“冲突”面板逐块选择后再标记已解决"
    return {"ok": False, "error": raw or "恢复暂存失败", "hint": hint,
            "stashes": _git_stash_list(project_dir)}


def drop_git_stash(project_dir, ref):
    """删除一条暂存记录：只删记录，不动工作区改动。"""
    ref = str(ref or "").strip()
    if not valid_stash_ref(ref):
        return {"ok": False, "error": "暂存引用不合法"}
    code, out, err = _git_run(project_dir, "stash", "drop", ref)
    raw = ((out or "") + (err or "")).strip()
    if code != 0:
        return {"ok": False, "error": raw or "删除暂存失败"}
    _invalidate_git_cache(project_dir)
    return {"ok": True, "message": raw or f"已删除 {ref}", "stashes": _git_stash_list(project_dir)}


def _git_stash_list(project_dir):
    """当前项目的暂存列表（最多 5 条），供操作后即时刷新。"""
    code, out, _ = _git_run(project_dir, "stash", "list", "-5")
    if code != 0:
        return []
    stashes = []
    for line in out.splitlines():
        stash_ref, _, subject = line.partition(": ")
        stashes.append({"ref": stash_ref, "subject": subject})
    return stashes


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_unified_diff(patch, status_map):
    """把 git show 的 unified diff 拆成「按文件」的块，并算好每一行的新旧行号。

    status_map: {新路径: (状态, 旧路径)}，来自 --name-status，用于补状态和重命名。
    返回 (diffs, truncated)：
      diffs: [{path, status, old_path, additions, deletions, binary,
               lines: [{type, old, new, text}]}]
      type 取值：hunk（@@ 头）、ctx（上下文）、add（新增）、del（删除）、
      meta（\\ No newline at end of file 提示）
    这样前端可以按文件折叠/跳转，并显示行号，不再是一整段糊在一起的文本。
    """
    diffs = []
    cur = None
    old_no = new_no = 0
    in_hunk = False
    total = 0
    truncated = False

    def finish():
        nonlocal cur
        if not cur:
            return
        status, old_path = status_map.get(cur["path"], ("", ""))
        if status:
            cur["status"] = status
        if old_path:
            cur["old_path"] = old_path
        diffs.append(cur)
        cur = None

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            finish()
            cur = {
                "path": "", "status": "", "old_path": "",
                "additions": 0, "deletions": 0, "binary": False, "lines": [],
            }
            in_hunk = False
            continue
        if cur is None:
            continue
        # 文件路径优先取 +++（新文件）；删除文件没有 +++ b/，退回 --- a/。
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target != "/dev/null":
                cur["path"] = target[2:] if target.startswith("b/") else target
            continue
        if line.startswith("--- "):
            source = line[4:].strip()
            if source != "/dev/null" and not cur["path"]:
                cur["path"] = source[2:] if source.startswith("a/") else source
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur["binary"] = True
            in_hunk = False
            continue

        m = HUNK_RE.match(line)
        if m:
            old_no = int(m.group(1))
            new_no = int(m.group(2))
            in_hunk = True
            if total < MAX_DIFF_LINES:
                cur["lines"].append({"type": "hunk", "old": None, "new": None, "text": line})
                total += 1
            else:
                truncated = True
            continue

        if not in_hunk:
            continue
        if total >= MAX_DIFF_LINES:
            truncated = True
            continue

        if line.startswith("+"):
            row = {"type": "add", "old": None, "new": new_no, "text": line[1:]}
            new_no += 1
            cur["additions"] += 1
        elif line.startswith("-"):
            row = {"type": "del", "old": old_no, "new": None, "text": line[1:]}
            old_no += 1
            cur["deletions"] += 1
        elif line.startswith("\\"):
            # “\ No newline at end of file”之类的提示，不占行号。
            row = {"type": "meta", "old": None, "new": None, "text": line}
        else:
            # 上下文行固定以一个空格开头；极少数空行按空内容处理。
            text = line[1:] if line.startswith(" ") else line
            row = {"type": "ctx", "old": old_no, "new": new_no, "text": text}
            old_no += 1
            new_no += 1
        cur["lines"].append(row)
        total += 1

    finish()
    return diffs, truncated


def find_vscode():
    """定位 VS Code 命令行；装.app 但没加 PATH 时也能用。"""
    candidates = [
        shutil.which("code"),
        "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
        str(HOME_DIR / "Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    return None


def open_diff_in_editor(project_dir, rev, file_path):
    """把某个文件在该提交前后的两个版本导出到临时目录，再用 VS Code 并排对比。

    文件必须属于这次提交，避免把任意路径传给 git / 编辑器。
    """
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", rev or ""):
        return {"ok": False, "error": "提交号不合法"}
    if not file_path or file_path.startswith("/") or ".." in Path(file_path).parts:
        return {"ok": False, "error": "文件路径不合法"}

    project_dir = Path(project_dir)

    def git_bytes(*args):
        try:
            proc = subprocess.run(
                ["git", "-C", str(project_dir), *args],
                capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    changed = (git_bytes("show", "--name-only", "--format=", rev) or b"").decode("utf-8", "ignore")
    if file_path not in changed.splitlines():
        return {"ok": False, "error": "该文件不在此提交中"}

    new_content = git_bytes("show", f"{rev}:{file_path}")
    if new_content is None:
        return {"ok": False, "error": "读取新版本失败"}
    old_content = git_bytes("show", f"{rev}^:{file_path}")
    if old_content is None:
        # 新增文件或根提交：旧版本当成空文件。
        old_content = b""

    code = find_vscode()
    if not code:
        return {"ok": False, "error": "没找到 VS Code，请先安装或把 code 命令加入 PATH"}

    target_dir = STATE_DIR / "txlc-dashboard" / "editor" / uuid.uuid4().hex[:12]
    target_dir.mkdir(parents=True, exist_ok=True)
    base = Path(file_path).name or "file"
    old_file = target_dir / f"旧-{base}"
    new_file = target_dir / f"新-{base}"
    old_file.write_bytes(old_content)
    new_file.write_bytes(new_content)

    try:
        subprocess.Popen(
            [code, "--diff", str(old_file), str(new_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **detach_kwargs(),
        )
    except OSError as exc:
        return {"ok": False, "error": f"启动 VS Code 失败：{exc}"}
    return {"ok": True, "old": str(old_file), "new": str(new_file)}


def read_git_commit(path, rev):
    """读取单个提交的明细：谁提交、什么时候、改了哪些文件、具体 diff。

    rev 来自前端，只允许十六进制提交号，避免把任意参数传给 git。
    """
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", rev or ""):
        return {"ok": False, "error": "提交号不合法"}
    if not path:
        return {"ok": False, "error": "项目路径为空"}
    project_dir = Path(path)
    if not project_dir.is_dir():
        return {"ok": False, "error": "项目目录不存在"}

    def git(*args, timeout=10):
        try:
            proc = subprocess.run(
                ["git", "-C", str(project_dir), *args],
                capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        # git show 对 merge 提交可能返回非零但不影响输出，这里保留 stdout。
        return proc.stdout

    if (git("rev-parse", "--is-inside-work-tree") or "").strip() != "true":
        return {"ok": False, "error": "不是 git 仓库"}

    meta_raw = git("show", "-s", "--date=iso", "--format=%H%x1f%an%x1f%ae%x1f%ad%x1f%s%x1f%b", rev) or ""
    parts = meta_raw.split("\x1f")
    if len(parts) < 5:
        return {"ok": False, "error": "找不到该提交"}
    full_hash, author, email, date, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
    message = parts[5].strip() if len(parts) > 5 else ""

    # --name-status 给出文件清单和状态（新增/修改/删除/重命名）。
    files = []
    status_map = {}
    for line in (git("show", "--name-status", "--format=", rev) or "").splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        status = cols[0]
        if (status.startswith("R") or status.startswith("C")) and len(cols) >= 3:
            old_path, file_path = cols[1], cols[2]
        else:
            old_path, file_path = "", cols[-1]
        files.append({
            "status": status, "path": file_path, "old_path": old_path,
            "additions": 0, "deletions": 0, "binary": False,
        })
        status_map[file_path] = (status, old_path)

    patch = git("show", "--format=", "--unified=3", "--no-color", rev) or ""
    char_truncated = len(patch) > MAX_PATCH_CHARS
    if char_truncated:
        patch = patch[:MAX_PATCH_CHARS]
    diffs, line_truncated = parse_unified_diff(patch, status_map)

    # 把每个文件的 +/- 行数和二进制标记补回文件列表，左侧清单也能显示统计。
    diff_by_path = {d["path"]: d for d in diffs}
    for item in files:
        diff = diff_by_path.get(item["path"])
        if diff:
            item["additions"] = diff["additions"]
            item["deletions"] = diff["deletions"]
            item["binary"] = diff["binary"]
        if diff and not diff["status"] and item["status"]:
            diff["status"] = item["status"]

    return {
        "ok": True,
        "hash": full_hash or rev,
        "short": (full_hash or rev)[:7],
        "author": author,
        "email": email,
        "date": date,
        "subject": subject,
        "message": message,
        "files": files,
        "diffs": diffs,
        "truncated": char_truncated or line_truncated,
        "is_merge": len(files) == 0,
    }


def configured_project_rows():
    """读取 projects.conf 的有效行（去掉注释和空行）。"""
    rows = []
    if not PROJECTS_CONF.is_file():
        return rows
    for raw in PROJECTS_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = [part.strip() for part in raw.split("|")]
        if len(cols) >= 2 and cols[0] and cols[1]:
            rows.append(cols)
    return rows


def configured_project_names():
    return {row[0] for row in configured_project_rows()}


def configured_ports():
    """读取 ports.conf 的显式端口覆盖：项目名 -> 端口。"""
    ports = {}
    if not PORTS_CONF.is_file():
        return ports
    for raw in PORTS_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split()
        if len(cols) >= 2 and cols[1].isdigit():
            ports[cols[0]] = int(cols[1])
    return ports


def port_available(port):
    """本机 127.0.0.1 上该端口是否还能监听。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not (1 <= port <= 65535):
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex(("127.0.0.1", port)) != 0
    finally:
        sock.close()


def detect_backend_port(project_dir):
    """从 application.yml/properties 里找 server.port，支持 ${SERVER_PORT:18089} 写法。"""
    files = []
    for pattern in (
        "src/main/resources/application*.yml",
        "src/main/resources/application*.yaml",
        "src/main/resources/application*.properties",
        "*/src/main/resources/application*.yml",
        "*/src/main/resources/application*.yaml",
        "*/src/main/resources/application*.properties",
    ):
        files.extend(sorted(project_dir.glob(pattern)))

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:200000]
        except OSError:
            continue
        value = ""
        if path.suffix == ".properties":
            match = re.search(r"(?m)^\s*server\.port\s*=\s*([^\r\n#]+)", text)
            if match:
                value = match.group(1)
        else:
            # server: 块内第一个 port:；只匹配缩进行，避免误读别的顶层配置。
            match = re.search(
                r"(?ms)^server:[ \t]*\r?\n(?:[ \t]+.*\r?\n)*?[ \t]+port:[ \t]*([^\r\n#]+)",
                text,
            )
            if match:
                value = match.group(1)
        if value:
            number = re.search(r"\d{2,5}", value)
            if number and 1 <= int(number.group(0)) <= 65535:
                return int(number.group(0))
    return 8080


def detect_backend_info(project_dir):
    """后端项目：确认 pom.xml，并扫描 *-api 模块用于“安装依赖”。"""
    if not (project_dir / "pom.xml").is_file():
        return None

    modules = []
    for pom in sorted(project_dir.glob("**/pom.xml")):
        if any(part in ("target", "node_modules", ".git") for part in pom.parts):
            continue
        rel = pom.parent.relative_to(project_dir)
        if str(rel) == ".":
            continue
        if rel.name.endswith("-api"):
            modules.append(rel.as_posix())

    return {
        "port": detect_backend_port(project_dir),
        "modules": modules,
        "install": ",".join(modules),
    }


def detect_package_manager(project_dir):
    """按锁文件判断包管理器，没有锁文件时默认 npm。"""
    if (project_dir / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (project_dir / "yarn.lock").is_file():
        return "yarn"
    if (project_dir / "bun.lockb").is_file() or (project_dir / "bun.lock").is_file():
        return "bun"
    return "npm"


def detect_frontend_framework(deps):
    """按依赖判断前端框架，用于默认端口和启动参数。"""
    if "next" in deps:
        return "next"
    if "nuxt" in deps:
        return "nuxt"
    if "@umijs/max" in deps or "umi" in deps:
        return "umi"
    if "@sveltejs/kit" in deps or ("svelte" in deps and "vite" in deps):
        return "svelte"
    if "vite" in deps:
        return "vite"
    if "@vue/cli-service" in deps:
        return "vue-cli"
    if "react-scripts" in deps:
        return "cra"
    if "@angular/cli" in deps:
        return "angular"
    return "generic"


def detect_frontend_port(project_dir, framework, scripts, dev_script):
    """端口优先级：dev 脚本里的 --port > 框架配置 > .env > 框架默认值。"""
    dev_cmd = str(scripts.get(dev_script, "") or "")
    match = re.search(r"(?:--port|-p)\s+(\d{2,5})", dev_cmd)
    if match:
        return int(match.group(1))

    config_patterns = (
        "vite.config.*", "vue.config.*", "next.config.*", "nuxt.config.*",
        "angular.json", "config/config.ts", ".umirc.ts", ".umirc.js",
    )
    for pattern in config_patterns:
        for path in sorted(project_dir.glob(pattern)):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:120000]
            except OSError:
                continue
            match = re.search(r"port\s*[:=]\s*['\"]?(\d{2,5})", text)
            if match:
                return int(match.group(1))

    for name in (".env", ".env.local", ".env.development", ".env.development.local"):
        path = project_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"(?m)^\s*PORT\s*=\s*['\"]?(\d{2,5})", text)
        if match:
            return int(match.group(1))

    return FRONTEND_FRAMEWORK_PORTS.get(framework, 3000)


def detect_frontend_info(project_dir):
    """前端项目：确认 package.json，识别包管理器、框架、脚本和端口。"""
    pkg_path = project_dir / "package.json"
    if not pkg_path.is_file():
        return None

    error = ""
    try:
        package = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        package = {}
        error = "package.json 解析失败"

    scripts = package.get("scripts") if isinstance(package, dict) else {}
    if not isinstance(scripts, dict):
        scripts = {}
    scripts = {str(k): str(v) for k, v in scripts.items()}

    deps = {}
    for key in ("dependencies", "devDependencies"):
        value = package.get(key) if isinstance(package, dict) else {}
        if isinstance(value, dict):
            deps.update(value)

    framework = detect_frontend_framework(deps)
    dev_script = next((name for name in ("dev", "start", "serve", "develop") if name in scripts), "")
    build_script = "build" if "build" in scripts else ""
    return {
        "package_json": True,
        "error": error,
        "package_manager": detect_package_manager(project_dir),
        "framework": framework,
        "framework_label": FRONTEND_FRAMEWORK_LABELS.get(framework, framework),
        "dev_script": dev_script,
        "build_script": build_script,
        "scripts": sorted(scripts.keys()),
        "port": detect_frontend_port(project_dir, framework, scripts, dev_script),
        "node_modules": (project_dir / "node_modules").is_dir(),
    }


def is_git_worktree(project_dir):
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def inspect_project(raw_dir, want_type=""):
    """检测目录是后端还是前端项目，返回表单需要的默认值。

    后端会重新扫描 pom/端口/api 模块，前端会重新读 package.json；
    真正添加时还会再检测一次，前端传回来的字段只作为用户选择，不被信任为命令。
    """
    text = (raw_dir or "").strip()
    if not text:
        return {"ok": False, "error": "请先选择项目目录"}
    try:
        project_dir = Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        return {"ok": False, "error": "项目目录不合法"}
    if not project_dir.is_dir():
        return {"ok": False, "error": "项目目录不存在"}

    backend = detect_backend_info(project_dir)
    frontend = detect_frontend_info(project_dir)
    types = []
    if backend:
        types.append("backend")
    if frontend:
        types.append("frontend")

    project_type = want_type if want_type in ("backend", "frontend") else (types[0] if types else "")
    if project_type == "backend" and not backend:
        return {"ok": False, "error": "该目录没有 pom.xml，不能作为后端服务"}
    if project_type == "frontend" and not frontend:
        return {"ok": False, "error": "该目录没有 package.json，不能作为前端项目"}
    if not project_type:
        return {"ok": False, "error": "该目录没有 pom.xml 或 package.json，无法识别项目类型"}

    info = backend if project_type == "backend" else frontend
    port = int(info.get("port") or (8080 if project_type == "backend" else 3000))

    # 建议名：目录名去掉不合法字符；已注册目录给出明确提示。
    name_suggestion = re.sub(r"[^A-Za-z0-9._-]+", "-", project_dir.name).strip("-.") or "project"
    rows = configured_project_rows()
    names = {row[0] for row in rows}
    registered = next(
        (row[0] for row in rows if Path(row[1]).expanduser().resolve() == project_dir),
        "",
    )

    warnings = []
    if registered:
        warnings.append(f"该目录已注册为 {registered}")
    if name_suggestion in names and name_suggestion != registered:
        warnings.append(f"项目名 {name_suggestion} 已被占用，请换一个")
    if not port_available(port):
        warnings.append(f"端口 {port} 已被占用，请换一个")
    if project_type == "frontend" and frontend and not frontend.get("dev_script"):
        warnings.append("package.json 没有 dev/start/serve 脚本，启动前需要补一个")

    return {
        "ok": True,
        "dir": str(project_dir),
        "type": project_type,
        "types": types,
        "name_suggestion": name_suggestion,
        "name_exists": name_suggestion in names,
        "registered": registered,
        "is_git": is_git_worktree(project_dir),
        "port": port,
        "port_in_use": not port_available(port),
        # 交给前端拼默认文档地址时用，避免前端再写死 127.0.0.1。
        "local_host": LOCAL_HOST,
        "backend": backend,
        "frontend": frontend,
        "warnings": warnings,
    }


def pick_project_dir():
    """弹出系统目录选择框；取消时返回 ok=false。"""
    path, cancelled, error = plat.pick_directory("选择要添加的项目目录")
    if error:
        return {"ok": False, "error": f"无法打开目录选择框：{error}"}
    if cancelled or not path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "dir": path}


def write_frontend_scripts(name, project_dir, package_manager, dev_script, build_script, port, framework):
    """生成前端启动/构建脚本，返回 (start_path, build_path)。

    脚本放到 dev-tools/services/，不往业务仓库写文件；projects.conf 用绝对路径引用。
    平台不同后缀不同：macOS/Linux 是 .sh，Windows 是 .cmd。
    """
    SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    flag = FRONTEND_PORT_FLAGS.get(framework, "")
    # 端口由 devctl 通过 PORT 环境变量传入，脚本里的数字只是兜底默认值。
    # 这样改端口不需要重写脚本，运行中的项目重启后就会用新端口。
    suffix = plat.script_suffix()
    start_path = SERVICES_DIR / f"{name}-start{suffix}"
    build_path = None
    labels = (
        f"# 项目：{name}\n"
        f"# 目录：{project_dir}\n"
        f"# 框架：{FRONTEND_FRAMEWORK_LABELS.get(framework, framework)}，包管理器：{package_manager}\n"
    )

    if plat.IS_WINDOWS:
        # .cmd 里 %PORT% 取环境变量；setlocal 保证变量不污染后续命令。
        # 注意注释必须用 rem，# 开头的行会被 cmd 当成未知命令。
        win_labels = (
            f"rem 项目：{name}\r\n"
            f"rem 目录：{project_dir}\r\n"
            f"rem 框架：{FRONTEND_FRAMEWORK_LABELS.get(framework, framework)}，包管理器：{package_manager}\r\n"
        )
        extra = f" -- {flag} %PORT%" if flag else ""
        start_path.write_text(
            "@echo off\r\n"
            "rem 由 TXLC 服务控制台生成：启动前端开发服务器。\r\n"
            + win_labels
            + "setlocal\r\n"
            f'cd /d "{project_dir}"\r\n'
            f'if not defined PORT set "PORT={port}"\r\n'
            'set "BROWSER=none"\r\n'
            f"{package_manager} run {dev_script}{extra}\r\n"
            "exit /b %errorlevel%\r\n",
            encoding="utf-8",
        )
        if build_script:
            build_path = SERVICES_DIR / f"{name}-build{suffix}"
            build_path.write_text(
                "@echo off\r\n"
                "rem 由 TXLC 服务控制台生成：构建前端项目。\r\n"
                + win_labels
                + "setlocal\r\n"
                f'cd /d "{project_dir}"\r\n'
                f"{package_manager} run {build_script}\r\n"
                "exit /b %errorlevel%\r\n",
                encoding="utf-8",
            )
        return start_path, build_path

    quoted_dir = shlex.quote(str(project_dir))
    quoted_dev = shlex.quote(dev_script)
    quoted_build = shlex.quote(build_script)
    extra = f' -- {flag} "$PORT"' if flag else ""
    start_path.write_text(
        "#!/bin/bash\n"
        "# 由 TXLC 服务控制台生成：启动前端开发服务器。\n"
        + labels
        + "set -e\n"
        f"cd {quoted_dir}\n"
        f'export PORT="${{PORT:-{port}}}"\n'
        "export BROWSER=none\n"
        f"exec {package_manager} run {quoted_dev}{extra}\n",
        encoding="utf-8",
    )
    start_path.chmod(0o755)

    if build_script:
        build_path = SERVICES_DIR / f"{name}-build.sh"
        build_path.write_text(
            "#!/bin/bash\n"
            "# 由 TXLC 服务控制台生成：构建前端项目。\n"
            + labels
            + "set -e\n"
            f"cd {quoted_dir}\n"
            f"exec {package_manager} run {quoted_build}\n",
            encoding="utf-8",
        )
        build_path.chmod(0o755)

    return start_path, build_path


def append_conf(path, line):
    """追加一行配置，保持文件末尾有换行。"""
    text = ""
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + line + "\n", encoding="utf-8")


def add_project(payload):
    """校验并注册新项目：写 projects.conf / ports.conf，前端额外生成启动脚本。"""
    project_type = str(payload.get("type") or "").strip()
    raw_dir = str(payload.get("dir") or "").strip()
    name = str(payload.get("name") or "").strip()
    doc = str(payload.get("doc") or "").strip()
    try:
        port = int(payload.get("port"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "端口必须是数字"}

    if project_type not in ("backend", "frontend"):
        return {"ok": False, "error": "项目类型只能是 backend 或 frontend"}
    if not PROJECT_NAME_RE.fullmatch(name) or name in ("all", "core"):
        return {"ok": False, "error": "项目名只能包含字母、数字、点、下划线、短横线，且不能叫 all/core"}
    if "|" in raw_dir or "\n" in raw_dir or "\r" in raw_dir:
        return {"ok": False, "error": "项目目录包含非法字符"}
    if not (1 <= port <= 65535):
        return {"ok": False, "error": "端口必须在 1-65535 之间"}
    if doc and not re.match(r"^https?://", doc):
        return {"ok": False, "error": "文档地址必须以 http:// 或 https:// 开头"}

    try:
        project_dir = Path(raw_dir).expanduser().resolve()
    except (OSError, RuntimeError):
        return {"ok": False, "error": "项目目录不合法"}
    if not project_dir.is_dir():
        return {"ok": False, "error": "项目目录不存在"}

    # 重新检测目录，避免信任前端传来的任意命令或脚本路径。
    backend = detect_backend_info(project_dir)
    frontend = detect_frontend_info(project_dir)
    if project_type == "backend" and not backend:
        return {"ok": False, "error": "该目录没有 pom.xml，不能作为后端服务"}
    if project_type == "frontend" and not frontend:
        return {"ok": False, "error": "该目录没有 package.json，不能作为前端项目"}
    if project_type == "frontend" and frontend and frontend.get("error"):
        return {"ok": False, "error": frontend["error"]}

    with project_conf_lock:
        rows = configured_project_rows()
        if name in {row[0] for row in rows}:
            return {"ok": False, "error": f"项目名 {name} 已存在"}
        for row in rows:
            try:
                if Path(row[1]).expanduser().resolve() == project_dir:
                    return {"ok": False, "error": f"该目录已注册为 {row[0]}"}
            except (OSError, RuntimeError):
                continue
        if port in configured_ports().values():
            return {"ok": False, "error": f"端口 {port} 已被其他项目的 ports.conf 占用"}
        if not port_available(port):
            return {"ok": False, "error": f"端口 {port} 已被占用，请换一个"}

        start_path = ""
        build_path = ""
        install = ""
        if project_type == "frontend":
            package_manager = str(payload.get("package_manager") or "").strip()
            if package_manager not in PACKAGE_MANAGERS:
                return {"ok": False, "error": "包管理器只能是 npm/pnpm/yarn/bun"}
            if not shutil.which(package_manager):
                # 命令只使用白名单里的值，但仍然要求本机真的装了它。
                return {"ok": False, "error": f"本机没有 {package_manager} 命令"}
            dev_script = str(payload.get("dev_script") or "").strip()
            build_script = str(payload.get("build_script") or "").strip()
            if not SCRIPT_NAME_RE.fullmatch(dev_script or ""):
                return {"ok": False, "error": "启动脚本名不合法"}
            if dev_script not in frontend["scripts"]:
                return {"ok": False, "error": f"package.json 里没有脚本 {dev_script}"}
            if build_script and build_script not in frontend["scripts"]:
                return {"ok": False, "error": f"package.json 里没有脚本 {build_script}"}
            start_file, build_file = write_frontend_scripts(
                name, project_dir, package_manager, dev_script, build_script, port,
                frontend["framework"],
            )
            start_path = str(start_file)
            build_path = str(build_file) if build_file else ""
            install = f"@{package_manager}"
            if not doc:
                doc = f"http://{LOCAL_HOST}:{port}/"
        else:
            if not doc:
                doc = f"http://{LOCAL_HOST}:{port}/doc.html"
            install = backend["install"]

        core = "1" if payload.get("core") else "0"
        columns = [
            name, str(project_dir), core, "",
            start_path, "", build_path, doc, install, project_type,
        ]
        append_conf(PROJECTS_CONF, "|".join(columns))
        append_conf(PORTS_CONF, f"{name} {port}")

    # 状态缓存立即失效，前端下一次轮询就能看到新项目。
    with status_cache_lock:
        status_cache["data"] = None
        status_cache["time"] = 0.0
        status_cache["refreshing"] = False

    return {
        "ok": True,
        "name": name,
        "type": project_type,
        "port": port,
        "dir": str(project_dir),
        "start_script": start_path,
        "build_script": build_path,
    }


def rewrite_ports_conf(name, port, old_name=""):
    """重写 ports.conf 里某个项目的端口，保留注释和其它项目。

    old_name 非空表示项目改名：旧名字的行会被替换成新名字，不会留下重复项。
    """
    lines = []
    if PORTS_CONF.is_file():
        lines = PORTS_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
    replaced = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split()
        if not cols:
            continue
        if cols[0] == name or (old_name and cols[0] == old_name):
            lines[index] = f"{name} {port}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name} {port}")
    PORTS_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rename_entries_project(old_name, new_name):
    """项目改名时同步 entries.conf 的第一列。"""
    if old_name == new_name or not ENTRIES_CONF.is_file():
        return
    lines = ENTRIES_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
    changed = False
    for index, raw in enumerate(lines):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        cols = [part.strip() for part in raw.split("|")]
        if cols and cols[0] == old_name:
            cols[0] = new_name
            lines[index] = "|".join(cols)
            changed = True
    if changed:
        ENTRIES_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")


def rewrite_project_doc(name, new_port):
    """把 projects.conf 里本机文档地址的端口改成新端口。

    只改 `127.0.0.1` / `localhost` 这类本机地址（主机名统一成 LOCAL_HOST），
    保留路径；外部文档或 CDN 地址保持原样。
    """
    if not PROJECTS_CONF.is_file():
        return
    text = PROJECTS_CONF.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    changed = False
    for index, raw in enumerate(lines):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        cols = [part.strip() for part in raw.split("|")]
        if len(cols) < 8 or cols[0] != name:
            continue
        updated = local_url_with_port(cols[7], new_port) if cols[7] else ""
        if updated != cols[7]:
            cols[7] = updated
            lines[index] = "|".join(cols)
            changed = True
    if changed:
        PROJECTS_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_project_port(payload):
    """修改某个已注册项目的端口：写 ports.conf，并同步本机文档地址。

    运行中的项目不会立刻换端口，前端会提示需要重启才生效。
    """
    name = str(payload.get("name") or "").strip()
    try:
        port = int(payload.get("port"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "端口必须是数字"}

    if not PROJECT_NAME_RE.fullmatch(name):
        return {"ok": False, "error": "项目名不合法"}
    if not (1 <= port <= 65535):
        return {"ok": False, "error": "端口必须在 1-65535 之间"}

    rows = configured_project_rows()
    row = next((item for item in rows if item[0] == name), None)
    if not row:
        return {"ok": False, "error": f"项目 {name} 不存在"}

    project_type = row[9] if len(row) > 9 and row[9] else "backend"
    try:
        project_dir = Path(row[1]).expanduser().resolve()
    except (OSError, RuntimeError):
        project_dir = Path(row[1])

    # 当前生效端口：ports.conf 优先，否则按类型检测。
    current = configured_ports().get(name)
    if current is None:
        info = detect_frontend_info(project_dir) if project_type == "frontend" else detect_backend_info(project_dir)
        current = int((info or {}).get("port") or (3000 if project_type == "frontend" else 8080))

    if port != current:
        # 不允许和其他项目的 ports.conf 端口撞车。
        for other, value in configured_ports().items():
            if other != name and value == port:
                return {"ok": False, "error": f"端口 {port} 已被项目 {other} 使用"}
        if not port_available(port):
            return {"ok": False, "error": f"端口 {port} 已被本机其他进程占用，请换一个"}

    with project_conf_lock:
        rewrite_ports_conf(name, port)
        rewrite_project_doc(name, port)
        # 前端启动脚本按 PORT 环境变量取端口；重新生成只是更新兜底默认值。
        if project_type == "frontend":
            frontend = detect_frontend_info(project_dir)
            if frontend and frontend.get("dev_script"):
                install = row[8] if len(row) > 8 else ""
                package_manager = install[1:] if install.startswith("@") else frontend["package_manager"]
                if package_manager not in PACKAGE_MANAGERS:
                    package_manager = frontend["package_manager"]
                try:
                    # 只更新启动脚本的默认端口，build 传空表示不动现有构建脚本。
                    write_frontend_scripts(
                        name, project_dir, package_manager,
                        frontend["dev_script"], "",
                        port, frontend["framework"],
                    )
                except OSError:
                    pass

    # 端口变了以后状态缓存必须失效，否则卡片还会显示旧端口。
    with status_cache_lock:
        status_cache["data"] = None
        status_cache["time"] = 0.0
        status_cache["refreshing"] = False

    return {
        "ok": True,
        "name": name,
        "type": project_type,
        "old_port": current,
        "port": port,
        "running": not port_available(current),
    }


def project_is_running(name):
    """判断项目是否还在运行：pid 文件存活或端口正在监听。"""
    pid_path = STATE_DIR / f"{name}.pid"
    if pid_path.is_file():
        try:
            if plat.pid_alive(int(pid_path.read_text().strip())):
                return True
        except (ValueError, OSError):
            pass
    port = configured_ports().get(name)
    if port:
        return not port_available(port)
    return False


def clean_script_value(value):
    """脚本路径/模块名不能包含 projects.conf 的分隔符和换行。"""
    text = str(value or "").strip()
    if any(ch in text for ch in "|\r\n"):
        raise ValueError("不能包含 | 或换行")
    return text


def update_project(payload):
    """修改已注册项目的设置：名称、目录、类型、端口、文档、脚本等。

    运行中的项目只允许改端口、核心标记、文档等软配置；
    改名/换目录/换类型会破坏 pid 和端口对应关系，必须先停止。
    """
    old_name = str(payload.get("name") or "").strip()
    new_name = str(payload.get("new_name") or old_name).strip()
    raw_dir = str(payload.get("dir") or "").strip()
    project_type = str(payload.get("type") or "backend").strip()

    if not PROJECT_NAME_RE.fullmatch(old_name):
        return {"ok": False, "error": "项目名不合法"}
    if not PROJECT_NAME_RE.fullmatch(new_name) or new_name in ("all", "core"):
        return {"ok": False, "error": "新项目名只能包含字母、数字、点、下划线、短横线，且不能叫 all/core"}
    if project_type not in ("backend", "frontend"):
        return {"ok": False, "error": "项目类型只能是 backend 或 frontend"}
    if not raw_dir:
        return {"ok": False, "error": "项目目录不能为空"}
    try:
        project_dir = Path(raw_dir).expanduser().resolve()
    except (OSError, RuntimeError):
        return {"ok": False, "error": "项目目录不合法"}
    if not project_dir.is_dir():
        return {"ok": False, "error": "项目目录不存在"}

    rows = configured_project_rows()
    row = next((item for item in rows if item[0] == old_name), None)
    if not row:
        return {"ok": False, "error": f"项目 {old_name} 不存在"}
    if new_name != old_name and any(item[0] == new_name for item in rows):
        return {"ok": False, "error": f"项目名 {new_name} 已存在"}

    old_type = row[9] if len(row) > 9 and row[9] else "backend"
    old_dir = row[1]
    running = project_is_running(old_name)
    if running and (new_name != old_name or str(project_dir) != old_dir or project_type != old_type):
        return {"ok": False, "error": "项目正在运行，请先停止再修改名称、目录或类型"}

    # 端口：没传就沿用当前值。
    try:
        port = int(payload.get("port"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "端口必须是数字"}
    if not (1 <= port <= 65535):
        return {"ok": False, "error": "端口必须在 1-65535 之间"}

    current_port = configured_ports().get(old_name)
    if current_port is None:
        info = detect_frontend_info(project_dir) if old_type == "frontend" else detect_backend_info(project_dir)
        current_port = int((info or {}).get("port") or (3000 if old_type == "frontend" else 8080))
    if port != current_port:
        for other, value in configured_ports().items():
            if other not in (old_name, new_name) and value == port:
                return {"ok": False, "error": f"端口 {port} 已被项目 {other} 使用"}
        if not port_available(port):
            return {"ok": False, "error": f"端口 {port} 已被本机其他进程占用，请换一个"}

    doc = str(payload.get("doc") or "").strip()
    if doc and not re.match(r"^https?://", doc):
        return {"ok": False, "error": "文档地址必须以 http:// 或 https:// 开头"}
    if not doc:
        doc = f"http://{LOCAL_HOST}:{port}/" if project_type == "frontend" else f"http://{LOCAL_HOST}:{port}/doc.html"

    try:
        start_script = clean_script_value(payload.get("start", row[4] if len(row) > 4 else ""))
        stop_script = clean_script_value(payload.get("stop", row[5] if len(row) > 5 else ""))
        build_script = clean_script_value(payload.get("build", row[6] if len(row) > 6 else ""))
        install = clean_script_value(payload.get("install", row[8] if len(row) > 8 else ""))
    except ValueError as exc:
        return {"ok": False, "error": f"脚本/模块配置不合法：{exc}"}

    # 旧的前端生成脚本在改名或换类型后不再使用，尽量清掉避免残留。
    old_start = row[4] if len(row) > 4 else ""
    old_build = row[6] if len(row) > 6 else ""

    def remove_generated(path):
        try:
            target = Path(path)
            if target.parent.resolve() == SERVICES_DIR.resolve() and target.name.startswith(old_name + "-"):
                target.unlink(missing_ok=True)
        except (OSError, RuntimeError):
            pass

    with project_conf_lock:
        if project_type == "frontend":
            frontend = detect_frontend_info(project_dir)
            if not frontend:
                return {"ok": False, "error": "该目录没有 package.json，不能作为前端项目"}
            package_manager = str(payload.get("package_manager") or "").strip()
            if not package_manager:
                package_manager = install[1:] if install.startswith("@") else frontend["package_manager"]
            if package_manager not in PACKAGE_MANAGERS:
                return {"ok": False, "error": "包管理器只能是 npm/pnpm/yarn/bun"}
            dev_script = str(payload.get("dev_script") or frontend.get("dev_script") or "").strip()
            if not SCRIPT_NAME_RE.fullmatch(dev_script or "") or dev_script not in frontend["scripts"]:
                return {"ok": False, "error": "package.json 里没有这个启动脚本"}
            # build_script 不传表示“保持现有构建脚本”，避免只改端口时把原来的构建配置清空。
            build_given = "build_script" in payload
            keep_build_path = ""
            build_name = payload.get("build_script")
            if build_given:
                build_name = str(build_name or "").strip()
                if build_name and build_name not in frontend["scripts"]:
                    return {"ok": False, "error": f"package.json 里没有脚本 {build_name}"}
            else:
                # 改名/换类型时旧脚本文件会被删除，只能回退到自动检测的构建脚本。
                if new_name == old_name and project_type == old_type and old_build and Path(old_build).is_file():
                    keep_build_path = old_build
                    build_name = ""
                else:
                    build_name = frontend.get("build_script") or ""
            start_file, build_file = write_frontend_scripts(
                new_name, project_dir, package_manager, dev_script, build_name,
                port, frontend["framework"],
            )
            start_script = str(start_file)
            build_script = keep_build_path or (str(build_file) if build_file else "")
            stop_script = ""
            install = f"@{package_manager}"
        else:
            backend = detect_backend_info(project_dir)
            if not backend:
                return {"ok": False, "error": "该目录没有 pom.xml，不能作为后端服务"}

        if new_name != old_name or project_type != old_type:
            remove_generated(old_start)
            remove_generated(old_build)
        if new_name != old_name:
            rename_entries_project(old_name, new_name)
        rewrite_ports_conf(new_name, port, old_name)

        core = "1" if payload.get("core") else "0"
        jar = row[3] if len(row) > 3 else ""
        columns = [
            new_name, str(project_dir), core, jar,
            start_script, stop_script, build_script, doc, install, project_type,
        ]
        lines = []
        if PROJECTS_CONF.is_file():
            lines = PROJECTS_CONF.read_text(encoding="utf-8", errors="replace").splitlines()
        changed = False
        for index, raw in enumerate(lines):
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            cols = [part.strip() for part in raw.split("|")]
            if cols and cols[0] == old_name:
                lines[index] = "|".join(columns)
                changed = True
                break
        if not changed:
            return {"ok": False, "error": f"projects.conf 里找不到项目 {old_name}"}
        PROJECTS_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rewrite_project_doc(new_name, port)

    with status_cache_lock:
        status_cache["data"] = None
        status_cache["time"] = 0.0
        status_cache["refreshing"] = False

    return {
        "ok": True,
        "name": new_name,
        "old_name": old_name,
        "type": project_type,
        "old_type": old_type,
        "dir": str(project_dir),
        "port": port,
        "old_port": current_port,
        "running": running,
        "renamed": new_name != old_name,
    }


def get_projects():
    """stale-while-revalidate：立即返回缓存，后台刷新，避免多标签轮询把 devctl json 打爆。"""
    with status_cache_lock:
        cached = status_cache["data"]
        age = time.time() - status_cache["time"]

        if cached is not None:
            if age >= STATUS_REFRESH_AFTER and not status_cache["refreshing"]:
                status_cache["refreshing"] = True
                threading.Thread(target=_refresh_status_cache, daemon=True).start()
            return attach_entries(cached)

        # 首次没有缓存时同步读取；锁会挡住并发请求，读完后续请求直接走缓存。
        code, out, err = run_devctl(["json"], timeout=20)
        if code != 0:
            raise RuntimeError(err.strip() or out.strip() or "读取项目状态失败")

        data = json.loads(out)
        status_cache["data"] = data
        status_cache["time"] = time.time()
        return attach_entries(data)


def project_names():
    return {p["name"] for p in get_projects()}


def read_projects_fresh():
    """绕过状态缓存，直接跑一次 devctl json。

    为什么需要：按依赖顺序推进时要判断「上一个项目是否真的起来了」，
    用缓存可能拿到几秒前的旧状态，导致提前宣布成功或提前放弃。
    只在编排任务里调用，频率很低，不担心多跑 lsof。
    """
    code, out, err = run_devctl(["json"], timeout=20)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def invalidate_status_cache():
    """让下一次 /api/status 重新读 devctl，而不是返回旧快照。"""
    with status_cache_lock:
        status_cache["data"] = None
        status_cache["time"] = 0.0
        status_cache["refreshing"] = False


def valid_target(target):
    """动作目标只允许 all / core / group:xx / 实际存在的项目名，避免命令注入。"""
    if target in ("all", "core"):
        return True
    if target.startswith("group:"):
        wanted = target.split(":", 1)[1]
        return any(project_group(p) == wanted for p in get_projects())
    return target in project_names()


def tail_file(path, lines):
    """读取文件末尾若干行；大文件只读最后 512KB，避免占用过多内存。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    size = p.stat().st_size
    with p.open("rb") as f:
        if size > LOG_TAIL_BYTES:
            f.seek(size - LOG_TAIL_BYTES)
        data = f.read()
    text = data.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------
# 诊断日志
#
# 为什么单独一块：出错时最怕「日志在哪」说不清。控制台自身日志、任务日志、
# 项目输出、健康巡检日志分散在控制台目录和系统临时目录里，这里统一汇总成
# 可浏览的清单，并支持导出诊断包（zip）直接发给别人排查。
# ---------------------------------------------------------------

DIAG_INDEX = {}  # 诊断 id -> 文件路径；只有登记过的路径允许被读取
DIAG_GROUPS = [
    ("console", "控制台"),
    ("job", "任务"),
    ("project", "项目日志"),
    ("health", "健康巡检"),
    ("app", "应用日志"),
]


def diag_id(path):
    """用路径哈希做诊断条目标识，避免把任意路径直接暴露成查询参数。"""
    resolved = Path(path).expanduser().resolve()
    return hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:12]


def _add_diag(entries, seen, group, label, path, project="", source="", kind="service"):
    """登记一个日志文件；不存在或重复的静默跳过。

    source/kind 只在能对应到「项目日志弹窗」的来源时填写（启动日志、操作日志、
    应用/错误/调试日志），前端据此提供「在项目日志中打开」，避免同一个文件
    有两个功能不一致的查看器。
    """
    candidate = Path(path).expanduser()
    try:
        if not candidate.is_file():
            return
        stat = candidate.stat()
    except OSError:
        return
    key = diag_id(candidate)
    if key in seen:
        return
    seen.add(key)
    DIAG_INDEX[key] = candidate
    entries.append({
        "id": key,
        "group": group,
        "label": label,
        "project": project,
        "source": source,
        "kind": kind,
        "name": candidate.name,
        "path": str(candidate),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
    })


def diagnostics_entries():
    """汇总控制台自身相关的日志文件，按「控制台 → 任务 → 项目」排序。

    注意：这里只登记路径，不读内容；读内容要再用 id 走 resolve_diag_entry，
    保证接口不能读任意文件。
    """
    DIAG_INDEX.clear()
    entries, seen = [], set()

    # 控制台自身日志（含轮转出来的历史文件）。
    _add_diag(entries, seen, "console", "控制台主日志", LOG_DIR / "console.log")
    if LOG_DIR.exists():
        for rotated in sorted(LOG_DIR.glob("console.log.*")):
            _add_diag(entries, seen, "console", f"控制台主日志（{rotated.name.rsplit('.', 1)[-1]}）", rotated)

    # 任务日志：最近 20 个，按修改时间倒序，标签用任务元数据补全。
    recent_jobs = []
    if JOB_DIR.exists():
        try:
            recent_jobs = sorted(
                JOB_DIR.glob("*.log"), key=lambda item: item.stat().st_mtime, reverse=True
            )[:20]
        except OSError:
            recent_jobs = []
    for log_path in recent_jobs:
        meta = {}
        try:
            meta = json.loads(log_path.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        action = meta.get("action") or "任务"
        target = meta.get("target") or log_path.stem
        _add_diag(entries, seen, "job", f"{action} · {target}", log_path, str(target))

    # 项目相关：服务输出、启停操作、健康巡检、应用日志（Spring Boot 自己写的）。
    try:
        projects = get_projects()
    except Exception:  # noqa: BLE001 - 诊断面板不能因为状态接口失败就整个打不开
        logger.exception("汇总项目日志时读取状态失败")
        projects = []
    for project in projects:
        name = project.get("name") or ""
        if not name:
            continue
        # 标签与项目日志弹窗的来源名保持一致：启动日志 / 操作日志 / 应用日志 / 错误日志。
        _add_diag(entries, seen, "project", "启动日志",
                  project.get("log") or str(STATE_DIR / f"{name}.log"), name, "startup", "service")
        _add_diag(entries, seen, "project", "操作日志",
                  str(STATE_DIR / f"{name}.action.log"), name, "action", "action")
        _add_diag(entries, seen, "health", "健康巡检", health_log_path(name, "health"), name)
        for kind, sources in (project.get("log_sources") or {}).items():
            for source in sources:
                if source.get("path"):
                    _add_diag(entries, seen, "app", source.get("label") or kind,
                              source["path"], name, source.get("key") or kind, "service")

    order = {key: index for index, (key, _) in enumerate(DIAG_GROUPS)}
    entries.sort(key=lambda item: (order.get(item["group"], 99), -item["mtime"]))
    return entries


def resolve_diag_entry(diag):
    """把前端传来的诊断 id 还原成路径；不认识的 id 一律拒绝。"""
    return DIAG_INDEX.get(str(diag or ""))


def read_tail_bytes(path, limit=LOG_TAIL_BYTES):
    """读取文件末尾的原始字节，用于打包诊断文件（不整份加载大日志）。"""
    file_path = Path(path)
    size = file_path.stat().st_size
    with file_path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
        return handle.read()


def build_diagnostics_zip(entries):
    """把诊断清单打成 zip：meta.txt + 每个日志文件的末尾 512KB。

    不打包 *.conf：里面可能有内网地址和本机绝对路径，发出去前应由人确认。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        lines = [
            "TXLC 服务控制台诊断包",
            f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"控制台地址: http://{LOCAL_HOST}:{PORT}",
            f"控制台目录: {BASE_DIR}",
            f"Python: {sys.version.split()[0]}",
            f"平台: {sys.platform}",
            "",
            "说明：只包含日志文件末尾内容，不包含 projects.conf / deps.conf 等配置",
            "（配置里可能有内网地址，需要时请单独确认后再发）。",
            "",
            "日志文件:",
        ]
        used_names = set()
        for entry in entries:
            path = resolve_diag_entry(entry.get("id"))
            if path is None:
                continue
            arcname = f'{entry["group"]}/{entry.get("project") or "console"}/{path.name}'
            if arcname in used_names:
                arcname = f'{entry["group"]}/{entry.get("project") or "console"}/{path.stem}-{entry["id"]}{path.suffix}'
            used_names.add(arcname)
            lines.append(f"  {arcname}  ({entry.get('size', 0)} bytes, {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry.get('mtime') or 0))})")
            try:
                data = read_tail_bytes(path)
            except OSError:
                continue
            archive.writestr(arcname, data)
        archive.writestr("meta.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()


def watch_pid(target):
    """检查热更新监听进程是否存活。"""
    name = "dev-watch-all.pid" if target == "all" else f"{target}.watch.pid"
    path = STATE_DIR / name
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
        return pid if plat.pid_alive(pid) else None
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------
# 健康检查与自动重启
#
# 背景：控制台原来只在启动/停止时知道服务状态，服务运行中崩掉（OOM、端口被抢、
# 数据库断连）只能靠人肉看日志。这里加一个后台巡检线程：
#   1 按 health.conf 的配置做 TCP/HTTP 探活
#   2 连续失败达到阈值就标记为异常，并按预算自动重启（默认开启，最多 5 次）
#   3 结果写进 /api/status 的 health 字段，前端显示红/绿点与重启次数
#
# health.conf 格式（一行一个项目，缺省的项目用默认值：TCP 探活 + 自动重启）：
#   项目名|模式(tcp/http/off)|目标(端口或URL)|间隔秒|超时秒|连续失败次数|自动重启(1/0)|最大重启次数
# ---------------------------------------------------------------

def load_health_conf():
    """解析 health.conf，返回 {项目名: {字段...}}；文件没变化时用缓存。"""
    if not HEALTH_CONF.exists():
        return {}
    try:
        mtime = HEALTH_CONF.stat().st_mtime
    except OSError:
        return {}
    with health_conf_lock:
        if health_conf_cache["mtime"] == mtime:
            return health_conf_cache["data"]
        data = {}
        for raw in HEALTH_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 2 or not parts[0]:
                continue
            data[parts[0]] = {
                "mode": parts[1] or "tcp",
                "target": parts[2] if len(parts) > 2 else "",
                "interval": parts[3] if len(parts) > 3 else "",
                "timeout": parts[4] if len(parts) > 4 else "",
                "failures": parts[5] if len(parts) > 5 else "",
                "restart": parts[6] if len(parts) > 6 else "",
                "max_restarts": parts[7] if len(parts) > 7 else "",
            }
        health_conf_cache["mtime"] = mtime
        health_conf_cache["data"] = data
        return data


def health_settings(project, conf=None):
    """把 health.conf 的一行合并成完整配置；没配置的项目按端口做 TCP 探活。"""
    if conf is None:
        conf = load_health_conf()
    row = conf.get(project.get("name", ""), {})

    def number(key, default):
        try:
            return float(row.get(key) or default)
        except (TypeError, ValueError):
            return float(default)

    target = (row.get("target") or "").strip()
    if not target:
        target = str(project.get("port") or "")
    auto = (row.get("restart") or "1").strip()
    return {
        "mode": (row.get("mode") or "tcp").strip().lower(),
        "target": target,
        "interval": max(2.0, number("interval", 10)),
        "timeout": max(0.3, number("timeout", 2)),
        "failures": max(1, int(number("failures", 3))),
        "restart": auto not in ("0", "false", "off", "no"),
        "max_restarts": max(0, int(number("max_restarts", 5))),
    }


def health_probe(settings):
    """执行一次探活，返回 (是否正常, 错误说明)。"""
    mode = settings["mode"]
    if mode == "off":
        return True, ""
    if mode == "http":
        url = settings["target"]
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=settings["timeout"]) as resp:
                return (200 <= resp.status < 400), "" if resp.status < 400 else f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            # HTTP 错误码说明端口是通的、服务在响应，只是这个路径或状态不对；按异常处理。
            return True, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - 探活失败原因很多，统一当异常
            return False, str(exc) or "连接失败"
    # 默认 TCP 探活
    try:
        port = int(settings["target"])
    except (TypeError, ValueError):
        return False, "端口未配置"
    ok = plat.port_open(port, timeout=settings["timeout"])
    return ok, "" if ok else f"端口 {port} 未监听"


def health_snapshot():
    """给前端用的健康状态快照（只读、加锁，避免和巡检线程打架）。"""
    with health_lock:
        return {name: dict(info) for name, info in health_state.items()}


def attach_health(projects):
    """把健康状态挂到项目对象上；前端卡片直接读 p.health。"""
    snapshot = health_snapshot()
    conf = load_health_conf()
    for project in projects:
        info = snapshot.get(project.get("name"))
        if info:
            project["health"] = info
        else:
            project["health"] = {
                "state": "unknown",
                "failures": 0,
                "restarts": 0,
                "last_error": "",
            }
        # 一并下发当前生效的探活配置，弹窗里才能显示/修改“是否自动重启”。
        settings = health_settings(project, conf)
        project["health"].update({
            "mode": settings["mode"],
            "target": settings["target"],
            "interval": settings["interval"],
            "timeout": settings["timeout"],
            "failures_threshold": settings["failures"],
            "auto_restart": settings["restart"],
            "max_restarts": settings["max_restarts"],
        })
    return projects


# ---------------------------------------------------------------
# 外部依赖（MySQL / Redis / Nacos 等）
#
# 背景：被管理的 Java 服务不自带数据库和缓存，依赖多在内网其他机器上。
# 依赖不通时 Spring 会等到连接超时才报错（可能几分钟），期间看起来像在正常启动。
# 这里在启动前先探活，把「哪个依赖不通」提前告诉用户。
#
# deps.conf 格式（一行一个依赖）：
#   名称|类型(tcp/http)|地址|端口|说明
# 项目与依赖的关联在 projects.conf 第 13 列（requires）。
# ---------------------------------------------------------------

def load_deps_conf():
    """解析 deps.conf，返回依赖定义列表；文件不存在时返回空列表。"""
    if not DEPS_CONF.exists():
        return []
    rows = []
    for raw in DEPS_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 4 or not parts[0] or not parts[2]:
            continue
        kind = (parts[1] or "tcp").lower()
        if kind not in ("tcp", "http"):
            kind = "tcp"
        rows.append({
            "name": parts[0],
            "kind": kind,
            "host": parts[2],
            "port": parts[3],
            "desc": parts[4] if len(parts) > 4 else "",
        })
    return rows


def probe_dep(dep, timeout=2.5):
    """探活单个依赖，返回 (是否可用, 延迟毫秒, 错误说明)。

    tcp：只建立 TCP 连接（MySQL/Redis 用这个，不引入驱动，也不会往对方发协议数据）
    http：GET 一次，任何 HTTP 响应都算服务活着（401/404 说明端口上确实有服务）
    """
    started = time.time()
    host = dep["host"]
    try:
        port = int(dep["port"])
    except (TypeError, ValueError):
        return False, 0, "端口未配置"

    if dep["kind"] == "http":
        url = f"http://{host}:{port}/"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return True, int((time.time() - started) * 1000), f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            # 有 HTTP 状态码说明服务在响应，只是这个路径不一定对。
            return True, int((time.time() - started) * 1000), f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - 探活失败原因很多，统一转成提示
            return False, int((time.time() - started) * 1000), str(exc) or "连接失败"

    ok = plat.port_open(port, host=host, timeout=timeout)
    latency = int((time.time() - started) * 1000)
    return ok, latency, "" if ok else f"{host}:{port} 连接失败"


def refresh_deps():
    """重新探活所有登记的外部依赖，结果写入 dep_state。"""
    global deps_refreshing
    with dep_lock:
        deps_refreshing = True
    try:
        results = {}
        for dep in load_deps_conf():
            ok, latency, error = probe_dep(dep)
            item = dict(dep)
            item.update({
                "ok": ok,
                "latency_ms": latency,
                "error": error,
                "checked": time.time(),
            })
            results[dep["name"]] = item
        with dep_lock:
            dep_state.clear()
            dep_state.update(results)
        return results
    finally:
        with dep_lock:
            deps_refreshing = False


def deps_snapshot(trigger=False):
    """返回依赖状态快照；首次请求时后台触发一次探活，不阻塞页面。"""
    global deps_refreshing
    with dep_lock:
        snapshot = [dict(item) for item in dep_state.values()]
        need_refresh = bool(trigger and not snapshot and not deps_refreshing)
        if need_refresh:
            deps_refreshing = True
    if need_refresh:
        threading.Thread(target=refresh_deps, daemon=True).start()
    return sorted(snapshot, key=lambda item: item["name"])


def deps_loop():
    """后台依赖巡检：独立于项目健康检查，避免慢依赖拖累服务探活。"""
    while True:
        try:
            refresh_deps()
        except Exception:  # noqa: BLE001 - 巡检线程不能因单次异常退出
            pass
        time.sleep(DEPS_REFRESH_EVERY)


def project_group(project):
    """项目的展示分组：配置了就用配置，否则按类型 / 核心标记推导。"""
    group = (project.get("group") or "").strip()
    if group:
        return group
    if project.get("type") == "frontend":
        return "frontend"
    if project.get("core"):
        return "core"
    return "business"


def dependency_gate(project):
    """启动前检查项目声明的必需外部依赖。

    返回 {blocked, down, message}；blocked=True 表示应拒绝启动（除非用户强制）。
    """
    requires = [item for item in (project.get("requires") or []) if item]
    if not requires:
        return {"blocked": False, "down": [], "message": ""}
    snapshot = {item["name"]: item for item in deps_snapshot(trigger=True)}
    down = []
    for name in requires:
        item = snapshot.get(name)
        if item is None:
            down.append({"name": name, "error": "deps.conf 中没有登记"})
        elif not item.get("ok"):
            down.append({"name": name, "error": item.get("error") or "不可用"})
    if not down:
        return {"blocked": False, "down": [], "message": ""}
    detail = "；".join(f"{item['name']}（{item['error']}）" for item in down)
    return {
        "blocked": True,
        "down": down,
        "message": f"外部依赖不可用：{detail}。可先在依赖面板确认，或强制启动。",
    }


def expand_target(target, names=None):
    """把 target（all / core / group:xx / 项目名）或 names 列表解析成项目名列表。"""
    projects = get_projects()
    known = [p["name"] for p in projects]
    if names:
        wanted = {str(item) for item in names}
        return [name for name in known if name in wanted]
    if target == "all":
        return known
    if target == "core":
        return [p["name"] for p in projects if p.get("core")]
    if target.startswith("group:"):
        wanted_group = target.split(":", 1)[1]
        return [p["name"] for p in projects if project_group(p) == wanted_group]
    return [target] if target in known else []


def topo_order(names, projects):
    """按 depends 做拓扑排序，依赖排在前；依赖不在本次范围内就忽略。

    深度优先：先访问依赖再输出自己。遇到环时跳过回边，保证不死循环。
    """
    wanted = list(dict.fromkeys(names))
    by_name = {p["name"]: p for p in projects}
    result, done, visiting = [], set(), set()

    def visit(name):
        if name in done or name not in by_name:
            return
        if name in visiting:
            return  # 环：忽略这条边
        visiting.add(name)
        for dep in by_name[name].get("depends") or []:
            if dep in by_name and dep in wanted:
                visit(dep)
        visiting.discard(name)
        done.add(name)
        result.append(name)

    for name in wanted:
        visit(name)
    return result


def wait_project_state(name, want_running, timeout):
    """等某个项目进入目标状态（运行/停止），供按依赖顺序推进时使用。

    每轮都读实时状态而不是缓存：SWR 缓存可能滞后几秒，
    会让「刚启动」被误判为已就绪，或「刚停止」被误判为还在跑。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        projects = read_projects_fresh()
        if projects is None:
            time.sleep(1.5)
            continue
        project = next((p for p in projects if p["name"] == name), None)
        if project is None:
            return False
        if bool(project.get("running")) == want_running:
            invalidate_status_cache()
            return True
        time.sleep(1.5)
    return False


def ordered_snapshot():
    """编排任务快照，给 /api/status 用。"""
    with ordered_lock:
        return dict(ordered_task) if ordered_task else None


def start_ordered(action, names, force=False):
    """按依赖顺序依次执行启停，进度写入 ordered_task 供前端轮询。

    为什么不在 devctl 里一次做完：顺序启动必须「等上一个端口就绪」才能继续，
    而 devctl 的 start 只负责单个项目；放这里还能复用健康状态与依赖检查。
    """
    global ordered_task
    projects = get_projects()
    order = topo_order(names, projects)
    if action == "stop":
        order = list(reversed(order))
    if not order:
        return {"ok": True, "message": "没有需要处理的项目", "names": []}

    with ordered_lock:
        if ordered_task and ordered_task.get("status") == "running":
            return {"ok": False, "error": "已有编排任务在执行，请等待完成"}
        task = {
            "id": uuid.uuid4().hex[:8],
            "action": action,
            "order": order,
            "done": [],
            "skipped": [],
            "failed": [],
            "current": "",
            "status": "running",
            "started": time.time(),
            "ended": 0.0,
        }
        ordered_task = task

    def worker():
        try:
            for name in order:
                with ordered_lock:
                    task["current"] = name
                project = next((p for p in get_projects() if p["name"] == name), None)

                if action == "start" and project is not None and not force:
                    gate = dependency_gate(project)
                    if gate["blocked"]:
                        with ordered_lock:
                            task["skipped"].append({"name": name, "reason": gate["message"]})
                        continue

                result = start_fast(f"{action}-bg", name)
                if not result.get("names"):
                    with ordered_lock:
                        task["skipped"].append({"name": name, "reason": "已在执行或已是目标状态"})
                    continue

                timeout = 60 if action == "stop" else 180
                ok = wait_project_state(name, action != "stop", timeout)
                with ordered_lock:
                    (task["done"] if ok else task["failed"]).append(name)
        finally:
            with ordered_lock:
                task["current"] = ""
                task["status"] = "done"
                task["ended"] = time.time()

    threading.Thread(target=worker, daemon=True).start()
    label = {"start": "启动", "stop": "停止", "restart": "重启"}[action]
    return {
        "ok": True,
        "message": f"已按依赖顺序提交 {len(order)} 个项目的{label}任务",
        "names": order,
        "ordered_id": task["id"],
    }


def _action_in_flight(name):
    """该项目是否有启动/停止/重启正在执行（有就先别插自动重启）。"""
    marker = STATE_DIR / f"{name}.action.running"
    try:
        return marker.exists() and time.time() - marker.stat().st_mtime < 600
    except OSError:
        return False


def trigger_auto_restart(name, reason):
    """按预算自动重启；返回是否真的提交了重启。"""
    result = start_fast("restart-bg", name)
    ok = bool(result.get("names"))
    if ok:
        log_path = health_log_path(name, "health")
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 自动重启：{reason}\n")
        except OSError:
            pass
        plat.notify("服务自动重启", f"{name} 探活失败，已自动重启")
    return ok


def health_log_path(name, kind):
    """健康/自愈相关日志路径：<临时目录>/<项目名>.health.log。

    单独一个函数是为了以后切到跨平台临时目录时只改一处。
    """
    return plat.temp_dir() / f"{name}.{kind}.log"


def health_loop():
    """后台巡检线程：按项目各自的间隔做探活，失败到阈值就自动重启。"""
    projects = []
    projects_fetched_at = 0.0
    while True:
        try:
            now = time.time()
            # 项目清单不需要 2 秒一刷：5 秒一次足够，避免没人开页面时也在反复跑 devctl。
            if now - projects_fetched_at >= 5:
                projects = get_projects()
                projects_fetched_at = now
            conf = load_health_conf()
            now = time.time()
            for project in projects:
                name = project.get("name") or ""
                if not name:
                    continue
                settings = health_settings(project, conf)

                with health_lock:
                    info = health_state.setdefault(name, {
                        "state": "unknown",
                        "failures": 0,
                        "restarts": 0,
                        "last_check": 0.0,
                        "last_ok": 0.0,
                        "last_error": "",
                        "restart_at": 0.0,
                        "next_check": 0.0,
                        "recovered_at": 0.0,
                    })
                    due = now >= info["next_check"]

                if not due or settings["mode"] == "off":
                    continue

                # 已停止的服务不需要探活；用户手动停了就别自动拉起。
                if not project.get("running"):
                    with health_lock:
                        # 之前已经判定异常（比如自动重启也失败了），保留红点状态，
                        # 否则卡片会从“异常”变回普通“已停止”，用户看不出需要处理。
                        if info["state"] != "down":
                            info["state"] = "stopped"
                        info["failures"] = 0
                        info["next_check"] = now + settings["interval"]
                    continue

                # 有操作正在执行（启动中/重启中/热更新触发）时跳过，避免自己打自己。
                if _action_in_flight(name):
                    with health_lock:
                        info["next_check"] = now + 2
                    continue

                # 刚启动完的宽限期：Spring Boot 冷启动可能要几十秒，这段时间端口没起来是正常的，
                # 不能当成故障去自动重启，否则会把正在启动的服务反复打断。
                try:
                    exit_time = float(project.get("action_exit_time") or 0)
                except (TypeError, ValueError):
                    exit_time = 0.0
                if exit_time and now - exit_time < HEALTH_START_GRACE:
                    with health_lock:
                        info["next_check"] = now + 3
                    continue

                ok, error = health_probe(settings)

                with health_lock:
                    info["last_check"] = now
                    info["next_check"] = now + settings["interval"]
                    if ok:
                        info["failures"] = 0
                        info["state"] = "ok"
                        info["last_ok"] = now
                        info["last_error"] = ""
                        # 持续正常一段时间后，把自动重启的预算还回去（避免一次抖动永久耗尽）。
                        if info["restarts"] and now - info["last_ok"] >= HEALTH_RECOVER_RESET:
                            info["restarts"] = 0
                        continue

                    info["failures"] += 1
                    info["last_error"] = error
                    if info["failures"] < settings["failures"]:
                        info["state"] = "degraded"
                        continue

                    info["state"] = "down"
                    can_restart = (
                        settings["restart"]
                        and info["restarts"] < settings["max_restarts"]
                        and now - info["restart_at"] >= HEALTH_RESTART_COOLDOWN
                    )
                    if can_restart:
                        info["restarts"] += 1
                        info["restart_at"] = now

                if can_restart:
                    trigger_auto_restart(name, f"{error}（连续 {info['failures']} 次）")
                else:
                    # 达到重启上限或关闭了自动重启：只告警，不再动手。
                    with health_lock:
                        notified_at = info.get("notified_at", 0.0)
                        should_notify = time.time() - notified_at > 300
                        if should_notify:
                            info["notified_at"] = time.time()
                    if should_notify:
                        plat.notify("服务异常", f"{name} 探活失败：{error}（已停止自动重启）")
        except Exception:  # noqa: BLE001 - 巡检线程不能因为一次异常退出
            pass
        time.sleep(HEALTH_EVAL_EVERY)


def job_summary(job):
    if not job:
        return None
    return {
        "id": job["id"],
        "action": job["action"],
        "target": job["target"],
        "status": job["status"],
        "exit_code": job.get("exit_code"),
        "started": job.get("started"),
        "ended": job.get("ended"),
    }


def current_job():
    with jobs_lock:
        job = jobs.get(active_job_id) if active_job_id else None
    if not job or job["status"] != "running":
        return None
    return job_summary(job)


def job_meta_path(job_id):
    return JOB_DIR / f"{job_id}.json"


def write_job_meta(job):
    """把任务元数据写入 sidecar，控制台重启后仍能列出历史更新日志。"""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": job["id"],
        "action": job["action"],
        "target": job["target"],
        "status": job["status"],
        "exit_code": job.get("exit_code"),
        "started": job.get("started"),
        "ended": job.get("ended"),
        "output": job.get("output"),
    }
    path = job_meta_path(job["id"])
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def read_job_meta(job_id):
    """读取历史任务元数据；job_id 只允许十六进制，防止路径穿越。"""
    if not re.fullmatch(r"[a-f0-9]{6,32}", job_id or ""):
        return None
    try:
        return json.loads(job_meta_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def infer_job_meta(log_path, known_names=None):
    """老任务没有 sidecar 时，从日志首尾推断项目和结果，避免历史里只显示空白。"""
    target = ""
    status = "unknown"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            head = [f.readline() for _ in range(80)]
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read()
    except OSError:
        return {"target": target, "status": status}

    # Maven 的 [INFO]/[ERROR] 也符合方括号格式，只认 projects.conf 里的项目名；
    # 单项目日志会反复出现同一个项目名，批量更新出现多个名字时不猜目标。
    names = []
    for line in head:
        match = re.match(r"\[([^\]]+)\]", line.strip())
        if not match:
            continue
        name = match.group(1)
        if known_names is not None and name not in known_names:
            continue
        if name not in names:
            names.append(name)
    if len(names) == 1:
        target = names[0]

    if re.search(r"编译失败|BUILD FAILURE|构建失败", tail):
        status = "failed"
    elif re.search(r"启动成功|更新完成|BUILD SUCCESS", tail):
        status = "done"
    return {"target": target, "status": status}


def list_jobs(limit=20, known_names=None):
    """列出最近的更新任务，合并内存任务、sidecar 元数据和遗留日志文件。"""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    ids = set()
    for path in list(JOB_DIR.glob("*.json")) + list(JOB_DIR.glob("*.log")):
        if re.fullmatch(r"[a-f0-9]{6,32}", path.stem):
            ids.add(path.stem)

    items = []
    for job_id in ids:
        meta = read_job_meta(job_id) or {}
        log_path = JOB_DIR / f"{job_id}.log"
        if not meta and log_path.exists():
            meta = infer_job_meta(log_path, known_names)
        try:
            size = log_path.stat().st_size
            mtime = log_path.stat().st_mtime
        except OSError:
            size = 0
            mtime = 0.0
        live = jobs.get(job_id)
        started = meta.get("started") or (live.get("started") if live else None) or mtime
        status = meta.get("status") or (live.get("status") if live else "unknown")
        # 控制台崩溃时 sidecar 可能停在 running；超过 10 分钟且内存里没有该任务就视为历史。
        if status == "running" and job_id not in jobs and time.time() - (started or 0) > 600:
            status = "unknown"
        items.append({
            "id": job_id,
            "action": meta.get("action") or (live.get("action") if live else "update"),
            "target": meta.get("target") or (live.get("target") if live else ""),
            "status": status,
            "exit_code": meta.get("exit_code", live.get("exit_code") if live else None),
            "started": started,
            "ended": meta.get("ended") or (live.get("ended") if live else None),
            "size": size,
        })

    items.sort(key=lambda item: item.get("started") or 0, reverse=True)
    return items[:limit]


def notify_job(job):
    """更新任务结束时发系统通知，后台更新不用一直盯着页面。"""
    label = {
        "update": "更新", "install": "安装依赖",
        "gitpull": "拉取", "gitstash": "暂存", "gitunstash": "恢复暂存",
    }.get(job.get("action", ""), job.get("action", ""))
    status = job.get("status")
    if status == "done":
        message = f"{label} {job.get('target', '')} 完成"
    elif status == "failed":
        message = f"{label} {job.get('target', '')} 失败，请查看更新日志"
    else:
        message = f"{label} {job.get('target', '')} 已停止"
    plat.notify("服务控制台", message)


def start_job(action, target, names=None):
    """启动后台任务，返回 job 或错误信息。

    names 为空时按 target 交给 devctl 自己解析；传入 names 时由 run_job 逐个执行，
    这样「多选项目更新」不用再依赖 devctl 是否支持逗号分隔的目标。
    """
    global active_job_id

    with jobs_lock:
        running = jobs.get(active_job_id) if active_job_id else None
        if running and running["status"] == "running":
            return None, "已有任务在执行，请等待完成"

        job_id = uuid.uuid4().hex[:10]
        output = JOB_DIR / f"{job_id}.log"
        job = {
            "id": job_id,
            "action": action,
            "target": target,
            "names": list(names or []),
            "status": "running",
            "exit_code": None,
            "output": str(output),
            "started": time.time(),
            "proc": None,
            "stop_requested": False,
        }
        jobs[job_id] = job
        active_job_id = job_id

    # 元数据落盘，控制台重启后任务历史仍可见。
    write_job_meta(job)
    thread = threading.Thread(target=run_job, args=(job,), daemon=True)
    thread.start()
    return job, None


def run_job(job):
    """在后台执行 devctl 动作，输出写入任务日志。

    有 names 时逐个执行并汇总失败项；没有时把 target 直接交给 devctl。
    """
    global active_job_id
    targets = job.get("names") or [job["target"]]
    failed = []
    try:
        with open(job["output"], "w", encoding="utf-8", errors="replace") as out:
            for index, target in enumerate(targets, 1):
                if job.get("stop_requested"):
                    break
                if len(targets) > 1:
                    out.write(f"[控制台] ({index}/{len(targets)}) {job['action']} {target}\n")
                    out.flush()
                proc = subprocess.Popen(
                    devctl_command(job["action"], target),
                    cwd=str(BASE_DIR),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=devctl_env(),
                    **detach_kwargs(),
                )
                job["proc"] = proc
                code = proc.wait()
                if code != 0:
                    failed.append(target)

        job["exit_code"] = 0 if not failed else 1
        if job.get("stop_requested"):
            job["status"] = "stopped"
        else:
            job["status"] = "done" if not failed else "failed"
        if failed:
            with open(job["output"], "a", encoding="utf-8", errors="replace") as out:
                out.write(f"\n[控制台] 失败项目：{', '.join(failed)}\n")
    except Exception as exc:  # noqa: BLE001 - 需要把异常展示到前端
        job["exit_code"] = -1
        job["status"] = "failed"
        with open(job["output"], "a", encoding="utf-8", errors="replace") as out:
            out.write(f"\n[控制台] 执行失败：{exc}\n")
    finally:
        job["ended"] = time.time()
        write_job_meta(job)
        notify_job(job)
        with jobs_lock:
            if active_job_id == job["id"]:
                active_job_id = None


def stop_job(job_id):
    """停止正在执行的任务：先 TERM，3 秒后 KILL。"""
    job = jobs.get(job_id)
    if not job or job["status"] != "running":
        return False
    job["stop_requested"] = True
    proc = job.get("proc")
    if not proc or proc.poll() is not None:
        return True

    plat.kill_tree(proc.pid, force=False)

    def force_kill():
        time.sleep(3)
        if proc.poll() is None:
            plat.kill_tree(proc.pid, force=True)

    threading.Thread(target=force_kill, daemon=True).start()
    return True


def target_project_names(target):
    """把 all/core/单个项目解析成项目名列表。"""
    projects = get_projects()
    if target == "all":
        return [p["name"] for p in projects]
    if target == "core":
        return [p["name"] for p in projects if p["core"]]
    return [target]


def start_fast(action, target):
    """异步启动/停止/重启：每个项目起一个 devctl 子进程，接口立即返回。

    devctl.py _action 会写 <状态目录>/<name>.action.running，结束后写 .action.exit，
    前端轮询 /api/status 即可知道进行到哪一步，不需要占用更新任务队列。
    """
    real = FAST_ACTIONS[action]
    names = target_project_names(target)
    started = []
    for name in names:
        running_file = STATE_DIR / f"{name}.action.running"
        if running_file.exists():
            try:
                # 超过 10 分钟的标记视为残留；否则认为该项目已有操作在执行。
                if time.time() - running_file.stat().st_mtime < 600:
                    continue
                running_file.unlink()
            except OSError:
                continue

        # 父进程先写 running 标记并清掉旧退出码，避免前端看到上一次的结果。
        exit_file = STATE_DIR / f"{name}.action.exit"
        try:
            running_file.write_text(real, encoding="utf-8")
            exit_file.unlink(missing_ok=True)
        except OSError:
            continue

        log_path = STATE_DIR / f"{name}.action.log"
        try:
            with open(log_path, "ab") as out:
                subprocess.Popen(
                    devctl_command("_action", name, real),
                    cwd=str(BASE_DIR),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    env=devctl_env(),
                    **detach_kwargs(),
                )
            started.append(name)
        except Exception:
            try:
                running_file.unlink(missing_ok=True)
            except OSError:
                pass

    label = {"start": "启动", "stop": "停止", "restart": "重启"}[real]
    if not started:
        return {"ok": True, "message": f"没有需要{label}的项目", "names": []}
    return {"ok": True, "message": f"已提交 {len(started)} 个项目的{label}操作", "names": started}


def start_watch(target):
    """启动热更新监听进程；监听器常驻，不占用任务队列。"""
    if watch_pid(target):
        return True, "热更新已在运行"

    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    log_path = WATCH_DIR / f"{target}.log"
    with open(log_path, "ab") as out:
        subprocess.Popen(
            devctl_command("watch", target),
            cwd=str(BASE_DIR),
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            env=devctl_env(),
            **detach_kwargs(),
        )

    # 等监听进程真正写出 pid 文件，避免接口先返回成功、前端却一直等状态。
    for _ in range(25):
        if watch_pid(target):
            return True, "热更新已启动"
        time.sleep(0.1)
    return False, "热更新启动失败，请查看热更新日志"


def list_ports(include_all=True):
    """端口面板数据：谁在监听、对应哪个项目、项目当前有没有在跑。

    include_all=False 时只返回 projects.conf 里登记过的端口，
    默认返回全部监听端口（把 QQ、Docker 之类也列出来，方便判断端口到底被谁占了）。
    """
    projects = get_projects()
    managed = {p.get("port"): p for p in projects if p.get("port")}
    listening = []
    for item in plat.listening_ports():
        project = managed.get(item["port"])
        entry = dict(item)
        entry["project"] = project.get("name") if project else ""
        entry["managed"] = bool(project)
        if include_all or entry["managed"]:
            listening.append(entry)

    # 登记了但没在监听的端口：说明项目该启动却没起来，前端标黄。
    silent = [
        {"project": p.get("name"), "port": p.get("port")}
        for p in projects
        if p.get("port") and not any(item["port"] == p["port"] for item in listening)
    ]
    return {"listening": listening, "silent": silent}


def release_port(port, expected_pid=None):
    """释放被占用的端口：先温和终止，1.5 秒还没退出再强制。

    安全约束：
      - 只允许操作当前真实处于 LISTEN 的端口（防止前端传任意 pid 来杀进程）
      - 不允许杀控制台自己
      - 不允许杀 pid 1 / 0
    """
    port = int(port)
    targets = [item for item in plat.listening_ports() if item["port"] == port]
    if not targets:
        return {"ok": False, "error": f"端口 {port} 当前没有监听进程"}
    pid = targets[0].get("pid")
    if not pid:
        return {"ok": False, "error": "看不到该端口的进程号（可能需要用管理员权限运行控制台）"}
    if expected_pid and int(expected_pid) != int(pid):
        return {"ok": False, "error": f"端口的进程已变化（当前 PID {pid}），请刷新后再试"}
    if pid in (0, 1) or pid == os.getpid():
        return {"ok": False, "error": "拒绝终止控制台自身或系统进程"}

    plat.kill_tree(pid, force=False)
    for _ in range(15):
        time.sleep(0.1)
        if not plat.port_open(port, timeout=0.2):
            return {"ok": True, "message": f"已停止 PID {pid}，端口 {port} 已释放"}
    plat.kill_tree(pid, force=True)
    time.sleep(0.3)
    if plat.port_open(port, timeout=0.2):
        return {"ok": False, "error": f"PID {pid} 仍未退出，可手动执行 kill -9 {pid}"}
    return {"ok": True, "message": f"已强制停止 PID {pid}，端口 {port} 已释放"}


def save_health_settings(payload):
    """保存单个项目的健康检查配置到 health.conf（保留其它项目与注释）。"""
    name = str(payload.get("project") or "").strip()
    if name not in project_names():
        return {"ok": False, "error": "未知项目"}

    mode = str(payload.get("mode") or "tcp").strip().lower()
    if mode not in ("tcp", "http", "off"):
        return {"ok": False, "error": "探活方式只能是 tcp / http / off"}
    target = str(payload.get("target") or "").strip()
    # target 会写进配置文件，禁止竖线和换行，避免把配置写坏。
    if "|" in target or "\n" in target:
        return {"ok": False, "error": "探活目标不能包含竖线或换行"}
    try:
        interval = max(2, min(600, int(float(payload.get("interval") or 10))))
        timeout = max(1, min(60, int(float(payload.get("timeout") or 2))))
        failures = max(1, min(20, int(float(payload.get("failures") or 3))))
        max_restarts = max(0, min(50, int(float(payload.get("max_restarts") or 5))))
    except (TypeError, ValueError):
        return {"ok": False, "error": "间隔 / 超时 / 失败次数 / 重启上限必须是数字"}
    restart = "1" if payload.get("auto_restart", True) else "0"

    # 列顺序必须和 health_settings() 的解析顺序一致：名称|方式|目标|间隔|超时|失败次数|自动重启|重启上限
    line = f"{name}|{mode}|{target}|{interval}|{timeout}|{failures}|{restart}|{max_restarts}"
    try:
        rows = []
        if HEALTH_CONF.exists():
            for raw in HEALTH_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    rows.append(raw)
                    continue
                if stripped.split("|")[0].strip() != name:
                    rows.append(raw)
        else:
            rows = [
                "# 健康检查配置：项目名|方式(tcp/http/off)|目标(端口或URL)|间隔秒|超时秒|连续失败次数|自动重启(1/0)|最大重启次数",
                "# 不在这里出现的项目，默认按端口做 TCP 探活并自动重启（最多 5 次）。",
            ]
        rows.append(line)
        tmp = HEALTH_CONF.with_suffix(".conf.tmp")
        tmp.write_text("\n".join(rows) + "\n", encoding="utf-8")
        tmp.replace(HEALTH_CONF)
    except OSError as exc:
        return {"ok": False, "error": f"写入 health.conf 失败：{exc}"}

    # 配置改了，下一次巡检立即按新值执行，并把重启预算重置（用户重新配置视为要重新观察）。
    with health_lock:
        info = health_state.get(name)
        if info:
            info["next_check"] = 0
            info["failures"] = 0
            info["restarts"] = 0
            info["restart_at"] = 0.0
            info["state"] = "unknown"
    return {"ok": True, "message": "健康检查配置已保存"}


def cleanup_old_jobs():
    """只保留最近 20 个任务日志（连同 sidecar 元数据）。"""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(JOB_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[20:]:
        for target in (path, path.with_suffix(".json")):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
    # 清理没有对应日志的孤立元数据和写入中断的临时文件。
    for path in JOB_DIR.glob("*.json"):
        if not path.with_suffix(".log").exists():
            try:
                path.unlink()
            except OSError:
                pass
    for path in JOB_DIR.glob("*.json.tmp"):
        try:
            path.unlink()
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 访问日志也进 logs/console.log；StreamHandler 同时保留终端输出。
        message = fmt % args
        # 状态轮询、日志尾巴刷新这类只读请求，成功时不写日志（见 QUIET_POLL_PATHS）；
        # 非 200 的仍然记录，排障时能看出是哪一次开始出问题。
        if " 200 " in message and self.path.split("?")[0] in QUIET_POLL_PATHS:
            logger.debug("%s %s", self.address_string(), message)
            return
        logger.info("%s %s", self.address_string(), message)

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, status, data, content_type, filename=None):
        """发送二进制内容（日志下载 / 诊断包），带 Content-Length 保证长连接正常结束。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_html(self):
        try:
            data = HTML_FILE.read_bytes()
        except OSError:
            self.send_json(500, {"error": "dashboard.html 不存在"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_body_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def rewrite_legacy_location(self, location):
        """把旧系统返回的测试域名重定向改回控制台代理路径。"""
        host = legacy_proxy_host()
        for base in (
            f"http://{host}:8080",
            f"https://{host}:8080",
            f"http://{host}",
            f"https://{host}",
        ):
            if location.startswith(base):
                return location[len(base):] or "/"
        return location

    def proxy_legacy(self):
        """把 /efarm/** 转发到本机 Tomcat，并注入旧系统能识别的 Host 头。"""
        parsed = urlparse(self.path)
        path = parsed.path or "/efarm/"
        if path == "/efarm":
            path = "/efarm/"
        target = path + (f"?{parsed.query}" if parsed.query else "")

        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length > 0 else None

        headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in ("host", "connection", "content-length", "accept-encoding"):
                continue
            headers[key] = value
        headers["Host"] = legacy_proxy_host()
        headers["X-Forwarded-Host"] = f"{LOCAL_HOST}:{PORT}"
        headers["X-Forwarded-Proto"] = "http"

        conn = http.client.HTTPConnection(*LEGACY_PROXY_TARGET, timeout=120)
        try:
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()

        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            lower = key.lower()
            if lower in ("connection", "content-length", "transfer-encoding"):
                continue
            if lower == "set-cookie":
                # 避免测试域名 Cookie 被 127.0.0.1 浏览器拒收。
                value = re.sub(r";\s*domain=[^;]+", "", value, flags=re.IGNORECASE)
            elif lower == "location":
                value = self.rewrite_legacy_location(value)
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/efarm" or parsed.path.startswith("/efarm/"):
                self.proxy_legacy()
                return
            if parsed.path == "/":
                self.send_html()
                return
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/api/status":
                self.send_json(200, {
                    "projects": get_projects(),
                    "active_job": current_job(),
                    "version": app_version(),
                    # 前端拼入口/文档地址时用，保证和浏览器地址栏的 Host 一致。
                    "local_host": LOCAL_HOST,
                    # 外部依赖（MySQL/Redis 等）的探活结果，启动前的拦截依据。
                    "deps": deps_snapshot(trigger=True),
                    # 按依赖顺序执行的编排进度（启动组 / 批量操作）。
                    "ordered": ordered_snapshot(),
                })
                return
            if parsed.path == "/api/diagnostics":
                entries = diagnostics_entries()
                self.send_json(200, {
                    "log_dir": str(LOG_DIR),
                    "temp_dir": str(plat.temp_dir()),
                    "groups": [{"key": key, "label": label} for key, label in DIAG_GROUPS],
                    "entries": entries,
                })
                return
            if parsed.path == "/api/diagnostics/tail":
                qs = parse_qs(parsed.query)
                entry_path = resolve_diag_entry((qs.get("id") or [""])[0])
                if entry_path is None:
                    self.send_json(404, {"error": "日志条目已过期，请刷新诊断面板"})
                    return
                try:
                    lines = int((qs.get("lines") or ["1200"])[0])
                except ValueError:
                    lines = 1200
                lines = max(50, min(lines, 5000))
                self.send_json(200, {
                    "content": tail_file(entry_path, lines),
                    "path": str(entry_path),
                })
                return
            if parsed.path == "/api/diagnostics/file":
                qs = parse_qs(parsed.query)
                entry_path = resolve_diag_entry((qs.get("id") or [""])[0])
                if entry_path is None:
                    self.send_json(404, {"error": "日志条目已过期，请刷新诊断面板"})
                    return
                try:
                    data = read_tail_bytes(entry_path, 2 * 1024 * 1024)
                except OSError as exc:
                    self.send_json(500, {"error": f"读取日志失败：{exc}"})
                    return
                self.send_bytes(200, data, "text/plain; charset=utf-8", entry_path.name)
                return
            if parsed.path == "/api/log":
                qs = parse_qs(parsed.query)
                project_name = (qs.get("project") or [""])[0]
                if project_name not in project_names():
                    self.send_json(400, {"error": "未知项目"})
                    return
                lines = int((qs.get("lines") or ["1200"])[0])
                lines = max(20, min(lines, MAX_LOG_LINES))
                kind = (qs.get("kind") or ["service"])[0]
                if kind not in ("service", "action"):
                    kind = "service"
                source_key = (qs.get("source") or [""])[0]

                project = next(
                    (item for item in get_projects() if item["name"] == project_name),
                    None,
                )
                sources = (project.get("log_sources") or {}).get(kind, []) if project else []
                selected = next((item for item in sources if item["key"] == source_key), None)
                if selected is None and sources:
                    selected = sources[0]

                if kind == "action":
                    path = str(STATE_DIR / f"{project_name}.action.log")
                    label = "操作日志"
                    source_key = "action"
                elif selected:
                    path = selected["path"]
                    label = selected["label"]
                    source_key = selected["key"]
                else:
                    path = (project or {}).get("log") or str(STATE_DIR / f"{project_name}.log")
                    label = "启动日志"
                    source_key = "startup"

                content = tail_file(path, lines)
                self.send_json(200, {
                    "content": content,
                    "project": project_name,
                    "kind": kind,
                    "source": source_key,
                    "label": label,
                    "sources": sources,
                })
                return
            if parsed.path == "/api/deps":
                # 依赖面板：?refresh=1 时同步重新探活（3 个依赖通常 1 秒内返回）。
                qs = parse_qs(parsed.query)
                if (qs.get("refresh") or ["0"])[0] == "1":
                    refresh_deps()
                self.send_json(200, {"deps": deps_snapshot(trigger=True)})
                return
            if parsed.path == "/api/ports":
                # 端口面板：谁在监听、是不是我们登记的项目。
                qs = parse_qs(parsed.query)
                include_all = (qs.get("all") or ["1"])[0] != "0"
                self.send_json(200, list_ports(include_all))
                return
            if parsed.path == "/api/health":
                # 健康检查的当前配置与运行状态。
                conf = load_health_conf()
                self.send_json(200, {
                    "projects": [
                        dict(project.get("health") or {}, project=project.get("name"))
                        for project in get_projects()
                    ],
                    "conf": conf,
                })
                return
            if parsed.path == "/api/project/inspect":
                # 添加项目：检测目录类型、端口、包管理器等默认值。
                qs = parse_qs(parsed.query)
                raw_dir = (qs.get("dir") or [""])[0]
                want_type = (qs.get("type") or [""])[0]
                self.send_json(200, inspect_project(raw_dir, want_type))
                return
            if parsed.path == "/api/project/pick-dir":
                # 调 macOS 原生目录选择框，省得手输长路径。
                self.send_json(200, pick_project_dir())
                return
            if parsed.path == "/api/git":
                # “代码”弹窗的数据：分支、改动文件、最近提交、暂存记录。
                qs = parse_qs(parsed.query)
                project_name = (qs.get("project") or [""])[0]
                project = next(
                    (item for item in get_projects() if item["name"] == project_name),
                    None,
                )
                if not project:
                    self.send_json(404, {"error": "未知项目"})
                    return
                ref = (qs.get("ref") or [""])[0]
                limit = (qs.get("limit") or ["20"])[0]
                self.send_json(200, read_git_detail(project.get("dir"), ref, limit))
                return
            if parsed.path == "/api/git/conflicts":
                # 冲突面板：哪些文件没解决、每处冲突两侧的内容。
                qs = parse_qs(parsed.query)
                project_name = (qs.get("project") or [""])[0]
                project = next(
                    (item for item in get_projects() if item["name"] == project_name),
                    None,
                )
                if not project:
                    self.send_json(404, {"error": "未知项目"})
                    return
                self.send_json(200, read_git_conflicts(project.get("dir")))
                return
            if parsed.path == "/api/git/commit":
                # 提交明细：谁提交、改了哪些文件、具体 diff。
                qs = parse_qs(parsed.query)
                project_name = (qs.get("project") or [""])[0]
                rev = (qs.get("hash") or [""])[0]
                project = next(
                    (item for item in get_projects() if item["name"] == project_name),
                    None,
                )
                if not project:
                    self.send_json(404, {"error": "未知项目"})
                    return
                self.send_json(200, read_git_commit(project.get("dir"), rev))
                return
            if parsed.path == "/api/jobs":
                # 任务历史：控制台重启后仍能回看最近 20 次更新构建日志。
                self.send_json(200, {"jobs": list_jobs(20, project_names())})
                return
            if parsed.path == "/api/job":
                qs = parse_qs(parsed.query)
                job_id = (qs.get("id") or [""])[0]
                if not re.fullmatch(r"[a-f0-9]{6,32}", job_id or ""):
                    self.send_json(400, {"error": "任务 ID 不合法"})
                    return

                job = jobs.get(job_id)
                if job:
                    payload = job_summary(job)
                    output_path = job["output"]
                else:
                    # 历史任务：从状态目录下 txlc-dashboard/jobs 的 sidecar 和日志恢复。
                    meta = read_job_meta(job_id) or {}
                    log_path = JOB_DIR / f"{job_id}.log"
                    if not log_path.exists():
                        self.send_json(404, {"error": "任务不存在"})
                        return
                    if not meta:
                        meta = infer_job_meta(log_path, project_names())
                    status = meta.get("status") or "unknown"
                    started = meta.get("started")
                    # 崩溃遗留的 running 标记超过 10 分钟按历史任务展示。
                    if status == "running" and time.time() - (started or 0) > 600:
                        status = "unknown"
                    payload = {
                        "id": job_id,
                        "action": meta.get("action") or "update",
                        "target": meta.get("target") or "历史任务",
                        "status": status,
                        "exit_code": meta.get("exit_code"),
                        "started": started,
                        "ended": meta.get("ended"),
                    }
                    output_path = str(log_path)

                payload["output"] = tail_file(output_path, MAX_JOB_LINES)
                self.send_json(200, payload)
                return
            self.send_json(404, {"error": "接口不存在"})
        except (BrokenPipeError, ConnectionResetError):
            # 页面在响应写完前被关闭/重载（重启控制台时最常见），不是故障，
            # 记为调试信息即可，避免真正的报错被这类堆栈淹没。
            logger.debug("客户端提前断开：GET %s", self.path)
        except Exception as exc:  # noqa: BLE001 - 统一返回错误给前端
            logger.exception("GET %s 失败", self.path)
            self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        global server_ref
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/efarm" or parsed.path.startswith("/efarm/"):
                self.proxy_legacy()
                return
            if parsed.path == "/api/action":
                body = self.read_body_json()
                action = body.get("action", "")
                target = str(body.get("target", "") or "")
                raw_names = body.get("names") or []
                force = bool(body.get("force"))
                if action not in ACTIONS:
                    self.send_json(400, {"error": "不支持的动作"})
                    return

                # 两种目标写法：target（all/core/group:xx/项目名）或 names 数组（卡片多选）。
                if raw_names:
                    if not isinstance(raw_names, list) or not all(isinstance(item, str) for item in raw_names):
                        self.send_json(400, {"error": "names 必须是项目名数组"})
                        return
                    resolved = expand_target("", raw_names)
                    if not resolved:
                        self.send_json(400, {"error": "未知项目"})
                        return
                    target_label = ",".join(resolved)
                else:
                    if not valid_target(target):
                        self.send_json(400, {"error": "未知项目"})
                        return
                    resolved = expand_target(target)
                    target_label = target

                if action in FAST_ACTIONS:
                    real = FAST_ACTIONS[action]
                    if not resolved:
                        self.send_json(200, {"ok": True, "message": "没有需要处理的项目", "names": []})
                        return
                    # 多个目标必须按依赖顺序串行；单个目标保持原来的立即返回。
                    if len(resolved) > 1:
                        result = start_ordered(real, resolved, force=force)
                    else:
                        name = resolved[0]
                        if real == "start" and not force:
                            project = next((p for p in get_projects() if p["name"] == name), None)
                            gate = dependency_gate(project or {})
                            if gate["blocked"]:
                                self.send_json(409, {
                                    "error": gate["message"],
                                    "down_deps": gate["down"],
                                    "can_force": True,
                                })
                                return
                        result = start_fast(action, name)
                    self.send_json(200 if result.get("ok") else 409, result)
                    return

                if action == "watch" or action == "unwatch":
                    results = []
                    for name in resolved:
                        if action == "watch":
                            ok, msg = start_watch(name)
                        else:
                            code, out, err = run_devctl(["unwatch", name], timeout=30)
                            ok, msg = code == 0, (out or err).strip()
                        results.append({"name": name, "ok": ok, "message": msg})
                    all_ok = all(item["ok"] for item in results)
                    self.send_json(200 if all_ok else 207, {"ok": all_ok, "results": results})
                    return

                job, err = start_job(action, target_label, names=resolved)
                if err:
                    self.send_json(409, {"error": err})
                    return
                self.send_json(202, {"ok": True, "job_id": job["id"]})
                return

            if parsed.path == "/api/project/add":
                # 添加项目：后端重新检测目录并生成脚本，不执行前端传来的任意命令。
                body = self.read_body_json()
                result = add_project(body)
                self.send_json(200 if result.get("ok") else 400, result)
                return

            if parsed.path == "/api/ports/kill":
                payload = self.read_body_json()
                try:
                    port = int(payload.get("port") or 0)
                except (TypeError, ValueError):
                    port = 0
                if not port:
                    self.send_json(400, {"error": "缺少端口号"})
                    return
                result = release_port(port, payload.get("pid"))
                self.send_json(200 if result.get("ok") else 409, result)
                return
            if parsed.path == "/api/health":
                # 保存单个项目的探活/自动重启配置。
                result = save_health_settings(self.read_body_json())
                self.send_json(200 if result.get("ok") else 400, result)
                return
            if parsed.path == "/api/project/port":
                # 修改端口：只允许已注册项目，写 ports.conf 并同步本机文档地址。
                body = self.read_body_json()
                result = set_project_port(body)
                self.send_json(200 if result.get("ok") else 400, result)
                return

            if parsed.path == "/api/project/update":
                # 项目设置：改名、目录、类型、端口、文档、脚本等，后端统一校验并重写配置。
                body = self.read_body_json()
                result = update_project(body)
                self.send_json(200 if result.get("ok") else 400, result)
                return

            if parsed.path == "/api/git/resolve":
                # 解决冲突：整文件选一侧，或逐块选择后重建文件并 git add。
                body = self.read_body_json()
                result = resolve_git_conflicts(body)
                self.send_json(200 if result.get("ok") else 400, result)
                return

            if parsed.path == "/api/git/stash-apply":
                # 恢复指定暂存（apply，不 drop；失败时给出可执行建议）。
                body = self.read_body_json()
                project = next(
                    (item for item in get_projects() if item["name"] == body.get("project", "")),
                    None,
                )
                if not project:
                    self.send_json(404, {"error": "未知项目"})
                    return
                result = apply_git_stash(project.get("dir"), body.get("ref", ""))
                self.send_json(200 if result.get("ok") else 400, result)
                return

            if parsed.path == "/api/git/stash-drop":
                # 删除一条暂存记录：只删记录，不动工作区。
                body = self.read_body_json()
                project = next(
                    (item for item in get_projects() if item["name"] == body.get("project", "")),
                    None,
                )
                if not project:
                    self.send_json(404, {"error": "未知项目"})
                    return
                result = drop_git_stash(project.get("dir"), body.get("ref", ""))
                self.send_json(200 if result.get("ok") else 400, result)
                return

            if parsed.path == "/api/git/open-diff":
                # 在 VS Code 里并排对比某个文件在提交前后的两个版本。
                body = self.read_body_json()
                project_name = body.get("project", "")
                project = next(
                    (item for item in get_projects() if item["name"] == project_name),
                    None,
                )
                if not project:
                    self.send_json(404, {"error": "未知项目"})
                    return
                self.send_json(200, open_diff_in_editor(
                    project.get("dir"), body.get("hash", ""), body.get("file", ""),
                ))
                return

            if parsed.path == "/api/job/stop":
                body = self.read_body_json()
                ok = stop_job(body.get("id", ""))
                self.send_json(200 if ok else 404, {"ok": ok})
                return

            if parsed.path == "/api/shutdown":
                self.send_json(200, {"ok": True, "message": "控制台已关闭"})
                if server_ref:
                    threading.Thread(target=server_ref.shutdown, daemon=True).start()
                return

            if parsed.path == "/api/diagnostics/export":
                entries = diagnostics_entries()
                payload = build_diagnostics_zip(entries)
                filename = time.strftime("txlc-diagnostics-%Y%m%d-%H%M%S.zip")
                self.send_bytes(200, payload, "application/zip", filename)
                return

            self.send_json(404, {"error": "接口不存在"})
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("客户端提前断开：POST %s", self.path)
        except Exception as exc:  # noqa: BLE001 - 统一返回错误给前端
            logger.exception("POST %s 失败", self.path)
            self.send_json(500, {"error": str(exc)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global server_ref
    # 打包后没有独立 python 解释器：同一个 exe 带 --devctl 时转发给 devctl.py。
    if len(sys.argv) > 1 and sys.argv[1] == "--devctl":
        # 打包后 devctl 的 __file__ 在临时解包目录，必须显式把配置目录指到 exe 同级，
        # 否则 `TXLCConsole.exe --devctl doctor` 会去解包目录找 projects.conf。
        os.environ.setdefault("TXLC_TOOLS_DIR", str(BASE_DIR))
        import devctl
        return devctl.main([sys.argv[0]] + sys.argv[2:])
    configure_logging()
    cleanup_old_jobs()
    # 健康巡检独立线程：不阻塞 HTTP 服务，控制台关掉后线程随进程退出。
    threading.Thread(target=health_loop, daemon=True).start()
    # 外部依赖巡检单独一个线程：慢依赖不能拖累服务探活。
    threading.Thread(target=deps_loop, daemon=True).start()
    server_ref = Server((HOST, PORT), Handler)
    print(f"TXLC 服务控制台已启动：http://{HOST}:{PORT}")

    # PyInstaller 打包的 exe 双击后没有终端：服务起来后自动打开应用窗口，
    # 行为和 macOS 的 .app 一致；源码模式由用户自己开浏览器，不抢焦点。
    if getattr(sys, "frozen", False):
        def open_window():
            for _ in range(40):
                if plat.port_open(PORT):
                    plat.open_app_window(f"http://localhost:{PORT}/")
                    return
                time.sleep(0.5)
        threading.Thread(target=open_window, daemon=True).start()
    try:
        server_ref.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，控制台退出")
    except Exception:  # noqa: BLE001 - 退出前把堆栈留在日志里
        logger.exception("控制台异常退出")
        raise
    finally:
        server_ref.server_close()
        logger.info("控制台已关闭")


if __name__ == "__main__":
    main()
