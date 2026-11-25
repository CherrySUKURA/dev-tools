#!/usr/bin/env python3
# TXLC 本地服务控制核心（跨平台）。
#
# 为什么有这一层：
#   原来所有进程/构建/Git 逻辑都在 devctl.sh（1100+ 行 bash），Windows 上跑不了，
#   也没法单元测试。S3 把核心迁到这里，devctl.sh / devctl.cmd 只做参数转发。
#
# 用法（与旧 bash 版一致）：
#   ./devctl.py json                     # 供 Web 控制台读取的状态（单行 JSON）
#   ./devctl.py list
#   ./devctl.py doctor [--env]           # 环境与配置自检（只检查，不启动服务）
#   ./devctl.py status|start|stop|restart|update [项目|all|core]
#   ./devctl.py install|gitpull|gitfetch|gitstash|gitunstash [项目|all|core]
#   ./devctl.py watch|unwatch [项目|all|core]
#   ./devctl.py panel
#
# 内部命令（由 dashboard.py 调用，不是给人用的）：
#   ./devctl.py _action <项目> <start|stop|restart>
#     写 <状态目录>/<项目>.action.running / .action.exit，供前端轮询进度。
#
# 项目名可写全名（svc-order）或简称（order）；gateway 对应 txlc-gateway。

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# 平台差异（端口/进程/状态目录/常驻进程）统一走适配层，Windows 分支已经在里面。
import console_platform as plat

# 打包成 exe 后 __file__ 指向解包目录，配置必须跟着 exe 走；
# 控制台启动子进程时会用 TXLC_TOOLS_DIR 显式指定。
TOOLS_DIR = Path(os.environ.get("TXLC_TOOLS_DIR") or Path(__file__).resolve().parent)
# BASE_DIR 只在没有 projects.conf 时作为默认扫描根；项目实际位置由配置决定。
BASE_DIR = TOOLS_DIR.parent
PROJECTS_CONF = TOOLS_DIR / "projects.conf"
PORTS_CONF = TOOLS_DIR / "ports.conf"
MAVEN_SETTINGS = TOOLS_DIR / "maven-settings.xml"

# 未提供 projects.conf 时的默认核心项目；提供后以第三列为准。
DEFAULT_CORE = ("svc-auth", "svc-baseinfo", "txlc-gateway")

# 运行状态（pid、服务日志、watch/action 标记）：POSIX 仍是 /tmp，Windows 用 %TEMP%\txlc。
STATE = plat.state_dir()

# 启动等待：最多 90 秒；停止时最多等 10 秒再强杀。与 bash 版保持一致。
START_TIMEOUT = 90
STOP_GRACE = 10
TAIL_LINES = 40

# projects.conf 13 列的字段名（顺序即列序），解析后按名字取，避免魔法下标。
CONF_FIELDS = (
    "name", "dir", "core", "jar", "start", "stop", "build",
    "doc", "install", "type", "group", "depends", "requires",
)

# 已本地化的服务间地址覆盖：只改指向本机的依赖，其余仍走各项目 local 配置。
LOCAL_SERVICE_ENVS = {
    "svc-baseinfo": {
        "TXLC_SVC_AUTH_URL": "http://127.0.0.1:18089",
        "TXLC_AUTH_PROFILE_QUERY_URL": "http://127.0.0.1:18089/auth/internal/profile/query",
    },
    "svc-auth": {
        "TXLC_SVC_BASEINFO_URL": "http://127.0.0.1:18090",
        "TXLC_SVC_DATA_CENTER_URL": "http://127.0.0.1:18080",
    },
    "svc-price": {
        "TXLC_AUTH_PROFILE_QUERY_URL": "http://127.0.0.1:18089/auth/internal/profile/query",
    },
    "txlc-gateway": {
        "TXLC_SVC_AUTH_URL": "http://127.0.0.1:18089",
        "TXLC_SVC_BASEINFO_URL": "http://127.0.0.1:18090",
        "TXLC_GATEWAY_AUTH_INTROSPECT_URL": "http://127.0.0.1:18089/auth/internal/token/introspect",
    },
}


def say(message):
    """统一输出：立即 flush，保证 Web 控制台的任务日志能实时看到。"""
    print(message, flush=True)


# ---------------------------------------------------------------
# 进程环境（双击 .app / GUI 启动时 PATH 往往不完整）
# ---------------------------------------------------------------

def child_env(extra=None):
    """给子进程准备环境变量：补齐 PATH、注入端口与本地服务地址。"""
    env = dict(os.environ)
    # Windows 没有这些目录；写进 PATH 只会让查找变慢，不影响结果但不该带过去。
    paths = [] if plat.IS_WINDOWS else ["/usr/local/bin", "/opt/homebrew/bin"]
    # 项目目录里的 Maven 可供双击启动的 GUI 进程使用，无需更改系统 PATH。
    if not shutil.which("mvn") and not shutil.which("mvn.cmd"):
        candidates = list(TOOLS_DIR.glob("apache-maven-*/bin"))
        if not plat.IS_WINDOWS:
            candidates += list(Path.home().glob(".sdkman/candidates/maven/current/bin"))
        for candidate in sorted(candidates, reverse=True):
            if (candidate / "mvn").is_file() or (candidate / "mvn.cmd").is_file():
                paths.insert(0, str(candidate))
                break
    if paths:
        env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
    if extra:
        env.update(extra)
    return env


def find_tool(*names):
    """在（补齐后的）PATH 里找可执行文件；找不到返回 None。"""
    env_path = child_env().get("PATH", "")
    for name in names:
        found = shutil.which(name) or shutil.which(name, path=env_path)
        if found:
            return found
    return None


# ---------------------------------------------------------------
# projects.conf / ports.conf
# ---------------------------------------------------------------

def read_projects():
    """解析 projects.conf：忽略注释与空行，只保留「名字 + 目录」都有的行。

    每行 13 列，见 CONF_FIELDS；老配置缺列时对应字段为空，由上层按默认逻辑推导。
    """
    if not PROJECTS_CONF.is_file():
        return []
    projects = []
    for raw in PROJECTS_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        project = {field: (parts[i] if i < len(parts) else "") for i, field in enumerate(CONF_FIELDS)}
        projects.append(project)
    return projects


def project_conf(name):
    """按名字取 projects.conf 的一行（dict）；没有配置时返回 None。"""
    for project in read_projects():
        if project["name"] == name:
            return project
    return None


def conf_field(name, field):
    """取某个项目的某一列；项目不存在或列为空时返回空字符串。"""
    project = project_conf(name)
    return (project or {}).get(field, "")


def all_names():
    """所有可管理项目：有 projects.conf 时完全以它为准，否则扫描同级 svc/* 与 gateway。"""
    if PROJECTS_CONF.is_file():
        return [project["name"] for project in read_projects()]
    names = []
    for parent, pattern in ((BASE_DIR / "svc", "*"), (BASE_DIR, "txlc-gateway")):
        for path in sorted(parent.glob(pattern)):
            if path.is_dir():
                names.append(path.name)
    return names


def project_dir(name):
    """项目根目录：优先配置，其次按历史目录结构推断。"""
    configured = conf_field(name, "dir")
    if configured:
        return Path(configured).expanduser()
    if name == "txlc-gateway":
        return BASE_DIR / "txlc-gateway"
    return BASE_DIR / "svc" / name


def project_type(name):
    """项目类型：backend（默认）/ frontend。老配置没有第 10 列时按后端处理。"""
    return conf_field(name, "type") or "backend"


def resolve_cmd_path(directory, command):
    """配置里的脚本路径：绝对路径直接用，相对路径按项目根目录展开。"""
    path = Path(command)
    return path if path.is_absolute() else Path(directory) / command


def is_core(name):
    """是否核心项目：配置第 3 列为 1；没配置时用 DEFAULT_CORE 兜底。"""
    core = conf_field(name, "core")
    if core:
        return core == "1"
    return name in DEFAULT_CORE


def core_names():
    """核心项目列表：配置里标 1 的项目，一个都没有时回退默认集合。"""
    if PROJECTS_CONF.is_file():
        names = [project["name"] for project in read_projects() if project["core"] == "1"]
        if names:
            return names
    return list(DEFAULT_CORE)


def resolve_name(value):
    """把简称解析成项目全名，并校验目录存在。找不到返回 None。"""
    name = "txlc-gateway" if value == "gateway" else value
    if name and project_dir(name).is_dir():
        return name
    for candidate in all_names():
        if candidate == name or candidate == f"svc-{name}":
            return candidate
    return None


def resolve_targets(target):
    """把 all / core / 单个项目解析成项目名列表；未知目标返回 None。"""
    if target == "all":
        return all_names()
    if target == "core":
        return core_names()
    resolved = resolve_name(target)
    return [resolved] if resolved else None


def port_override(name):
    """ports.conf 的端口覆盖，空白或 | 分隔都接受；没有覆盖返回 None。"""
    if not PORTS_CONF.is_file():
        return None
    for raw in PORTS_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part for part in re.split(r"[|\s]+", line) if part]
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    return None


def find_application_yml(directory):
    """定位 src/main/resources/application.yml，跳过构建产物与依赖目录。

    为什么不用 rglob：控制台每秒轮询一次 json，17 个项目全树扫描会把 CPU 吃满；
    这里的目录很深，target 里又复制了一份配置，必须剪枝。
    """
    root = Path(directory)
    if not root.is_dir():
        return None
    skip = {"target", ".git", "node_modules", "dist", "build"}
    base_depth = len(root.parts)
    for current, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in skip and not d.startswith(".")]
        # 只找 src/main/resources 这一层，超过 6 层的非常规布局直接放弃。
        if len(Path(current).parts) - base_depth > 6:
            subdirs[:] = []
            continue
        if "application.yml" in files:
            path = Path(current) / "application.yml"
            if path.parent.name == "resources" and path.parent.parent.name == "main":
                return path
    return None


def project_port(name):
    """项目端口：优先 ports.conf，其次 application.yml 的 server.port，最后 8080。"""
    override = port_override(name)
    if override:
        return override
    config = find_application_yml(project_dir(name))
    if config is None:
        return "8080"
    try:
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^\s*port:", line):
                numbers = re.findall(r"\d+", line)
                if numbers:
                    return numbers[-1]
                break
    except OSError:
        pass
    return "8080"


def port_number(name):
    """端口转成整数；配置写错时退化为 0（旧 bash 版的 JSON 里 port 是数字）。"""
    try:
        return int(project_port(name))
    except (TypeError, ValueError):
        return 0


def project_jar(name):
    """可执行 jar：优先配置第 4 列，否则在 *-server/target 与 target 下自动查找。"""
    directory = project_dir(name)
    relative = conf_field(name, "jar")
    if relative:
        candidate = Path(relative)
        candidate = candidate if candidate.is_absolute() else directory / relative
        if candidate.is_file():
            return candidate
    excluded = (".original", "-sources.jar", "-javadoc.jar")

    def usable(path):
        return path.is_file() and not any(str(path).endswith(suffix) for suffix in excluded)

    if directory.is_dir():
        for path in sorted(directory.rglob("*.jar")):
            if path.parent.name == "target" and path.parent.parent.name.endswith("-server") and usable(path):
                return path
        target_dir = directory / "target"
        if target_dir.is_dir():
            for path in sorted(target_dir.glob("*.jar")):
                if usable(path):
                    return path
    return None


# ---------------------------------------------------------------
# 运行状态与探活
# ---------------------------------------------------------------

def pid_file(name):
    return STATE / f"{name}.pid"


def log_file(name):
    return STATE / f"{name}.log"


def action_running_file(name):
    return STATE / f"{name}.action.running"


def action_exit_file(name):
    return STATE / f"{name}.action.exit"


def action_log_file(name):
    return STATE / f"{name}.action.log"


def read_pid(name):
    """读取 pid 文件里的 PID；文件不存在或内容非法时返回 None。"""
    try:
        return int(pid_file(name).read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(name):
    """pid 文件记录的进程是否仍存活（Windows 走 tasklist）。"""
    pid = read_pid(name)
    return bool(pid and plat.pid_alive(pid))


def port_listening(name):
    """端口已监听即视为服务可用（比 pid 更准：进程可能还在但端口没起来）。"""
    return plat.port_open(project_port(name))


def listening_ports():
    """一次性取全部监听端口（集合），供 json_status 批量判断，避免每个项目都探活。"""
    return {item["port"] for item in plat.listening_ports()}


def recent_file(path, max_age):
    """文件存在且修改时间在 max_age 秒内；用于清理残留的 action 标记。"""
    path = Path(path)
    try:
        return path.is_file() and time.time() - path.stat().st_mtime < max_age
    except OSError:
        return False


def tail(path, lines=TAIL_LINES):
    """读日志末尾若干行；失败时返回空字符串（启动失败提示用）。"""
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])


# ---------------------------------------------------------------
# 构建与启动
# ---------------------------------------------------------------

def run_mvn(args, cwd=None):
    """统一 Maven 入口：用 maven-settings.xml 放行内网 HTTP Nexus。"""
    mvn = find_tool("mvn", "mvn.cmd")
    if not mvn:
        say("找不到 mvn：请安装 Maven 或把 mvn 放进 PATH")
        return 1
    command = [mvn]
    if MAVEN_SETTINGS.is_file():
        command += ["-s", str(MAVEN_SETTINGS)]
    command += list(args)
    return subprocess.run(command, cwd=cwd).returncode


def build_one(name):
    """完整构建：clean package，跳过测试代码编译（skipTests 仍会编译测试类）。"""
    directory = project_dir(name)
    say(f"[{name}] mvn clean package ...")
    return run_mvn(["-B", "-Dmaven.test.skip=true", "clean", "package"], cwd=directory)


def start_command(name, directory):
    """按项目类型给出启动命令与额外环境变量；找不到可执行目标时返回 (None, 原因)。"""
    start_cmd = conf_field(name, "start")
    if start_cmd:
        script = resolve_cmd_path(directory, start_cmd)
        if not script.is_file():
            return None, f"[{name}] 启动脚本不存在：{script}"
        port = project_port(name)
        env = {"PORT": port, "SERVER_PORT": port}
        if plat.IS_WINDOWS:
            command = [str(script)] if script.suffix.lower() in (".cmd", ".bat", ".exe") else ["cmd", "/c", str(script)]
        else:
            command = ["bash", str(script)]
        return (command, env, "custom"), None

    jar = project_jar(name)
    if jar is None:
        say(f"[{name}] 未找到可执行 jar，先构建...")
        if build_one(name) != 0:
            return None, f"[{name}] 构建失败，未启动"
        jar = project_jar(name)
        if jar is None:
            return None, f"[{name}] 构建后仍未找到 jar"

    java = find_tool("java", "java.exe")
    if not java:
        return None, f"[{name}] 找不到 java，请设置 JAVA_HOME 或把 java 放进 PATH"
    override = port_override(name)
    env = {"SERVER_PORT": override} if override else {}
    command = [java, "-jar", str(jar), "--spring.profiles.active=local"]
    return (command, env, "jar"), None


def start_one(name):
    """启动单个服务；已在运行则跳过，jar 缺失时先构建。"""
    directory = project_dir(name)
    log = log_file(name)

    if pid_alive(name):
        say(f"[{name}] 已在运行 (PID {read_pid(name)})")
        return 0
    if port_listening(name):
        say(f"[{name}] 端口 {project_port(name)} 已被占用，未启动")
        return 1

    started, error = start_command(name, directory)
    if started is None:
        say(error)
        return 1
    command, env, kind = started

    extra = dict(LOCAL_SERVICE_ENVS.get(name, {}))
    extra.update(env)
    proc = plat.spawn_detached(command, directory, log, env=child_env(extra))
    pid = proc.pid
    pid_file(name).write_text(str(pid))

    if kind == "custom":
        say(f"[{name}] 启动中（自定义命令 PID {pid}），端口 {project_port(name)}，日志：{log}")
    else:
        say(f"[{name}] 启动中 (PID {pid})，端口 {project_port(name)}，日志：{log}")

    for _ in range(START_TIMEOUT):
        if port_listening(name):
            say(f"[{name}] 启动成功")
            return 0
        if not plat.pid_alive(pid):
            say(f"[{name}] 启动失败，日志末尾：")
            say(tail(log))
            return 1
        time.sleep(1)
    say(f"[{name}] 启动超时，日志末尾：")
    say(tail(log))
    return 1


def kill_process(pid, grace=STOP_GRACE):
    """先温和终止，等 grace 秒仍未退出再强杀；只保留一个实现，避免各分支行为不一致。"""
    plat.kill_pid(pid, force=False)
    deadline = time.time() + grace
    while time.time() < deadline:
        if not plat.pid_alive(pid):
            return
        time.sleep(0.5)
    plat.kill_pid(pid, force=True)
    # taskkill /F is asynchronous; allow the process to release its port/log.
    deadline = time.time() + 3
    while time.time() < deadline:
        if not plat.pid_alive(pid):
            return
        time.sleep(0.1)


def kill_by_port(name, message):
    """pid 文件失效时的兜底：按端口找监听进程并停止。"""
    pid = plat.pid_on_port(project_port(name))
    if not pid:
        return False
    say(f"[{name}] {message} (PID {pid})")
    kill_process(pid, grace=1)
    return True


def stop_one(name):
    """停止单个服务：自定义停止脚本优先，之后按 pid / 端口兜底。"""
    directory = project_dir(name)
    stop_cmd = conf_field(name, "stop")
    if stop_cmd:
        script = resolve_cmd_path(directory, stop_cmd)
        if script.is_file():
            say(f"[{name}] 停止中（自定义命令）")
            if plat.IS_WINDOWS:
                subprocess.run([str(script)] if script.suffix.lower() in (".cmd", ".bat", ".exe")
                               else ["cmd", "/c", str(script)], cwd=directory)
            else:
                subprocess.run(["bash", str(script)], cwd=directory)
        else:
            say(f"[{name}] 停止脚本不存在：{script}")
        kill_by_port(name, "按端口兜底停止")
        pid_file(name).unlink(missing_ok=True)
        say(f"[{name}] 已停止")
        return 0

    pid = read_pid(name) if pid_alive(name) else None
    if pid:
        say(f"[{name}] 停止中 (PID {pid})")
        kill_process(pid)
    else:
        kill_by_port(name, "pid 文件失效，按端口停止")

    # 前端开发服务器常由 npm/pnpm 派生子进程：父进程退出后端口可能仍被占用。
    kill_by_port(name, "端口仍被占用，兜底停止")
    pid_file(name).unlink(missing_ok=True)
    say(f"[{name}] 已停止")
    return 0


def update_one(name):
    """更新：有本地改动时跳过 git pull；构建成功后才重启原本在运行的服务。"""
    directory = project_dir(name)
    build_cmd = conf_field(name, "build")
    start_cmd = conf_field(name, "start")
    was_running = pid_alive(name) or port_listening(name)

    dirty = run_git(name, "status", "--porcelain", "--untracked-files=no")[1].strip()
    if dirty:
        say(f"[{name}] 有未提交改动，跳过 git pull，按本地代码构建")
    else:
        say(f"[{name}] git pull ...")
        code, _, err = run_git(name, "pull", "--ff-only", stream=True)
        if code != 0:
            say(f"[{name}] git pull 失败，按本地代码继续构建")
            if err:
                say(err.rstrip())

    if build_cmd:
        script = resolve_cmd_path(directory, build_cmd)
        if not script.is_file():
            say(f"[{name}] 构建脚本不存在：{script}")
            return 1
        say(f"[{name}] 自定义构建 ...")
        if run_script(script, directory) != 0:
            say(f"[{name}] 编译失败，继续使用旧进程")
            return 1
    elif start_cmd:
        say(f"[{name}] 未配置 build 命令，跳过构建")
    elif build_one(name) != 0:
        say(f"[{name}] 编译失败，继续使用旧进程")
        return 1

    if was_running:
        stop_one(name)
        return start_one(name)
    say(f"[{name}] 更新完成；原本未运行，保持停止（需要时手动启动）")
    return 0


def run_script(script, directory):
    """执行项目自带脚本：Windows 用 cmd，POSIX 用 bash。"""
    script = Path(script)
    if plat.IS_WINDOWS:
        command = [str(script)] if script.suffix.lower() in (".cmd", ".bat", ".exe") else ["cmd", "/c", str(script)]
    else:
        command = ["bash", str(script)]
    return subprocess.run(command, cwd=directory).returncode


# ---------------------------------------------------------------
# Git 操作
# ---------------------------------------------------------------

def run_git(name, *args, stream=False):
    """在项目目录执行 git，返回 (code, stdout, stderr)。

    stream=True 时把输出直接打到控制台（拉取/恢复这类需要实时看的操作）。
    """
    directory = project_dir(name)
    command = ["git", "-C", str(directory)] + list(args)
    proc = subprocess.run(command, capture_output=True, text=True)
    if stream and proc.stdout:
        say(proc.stdout.rstrip())
    return proc.returncode, proc.stdout, proc.stderr


def is_git_repo(name):
    return run_git(name, "rev-parse", "--is-inside-work-tree")[0] == 0


def git_dirty_count(name):
    """未提交改动数（不含未跟踪文件），与"有改动就跳过 pull"的判定一致。"""
    return len([line for line in run_git(name, "status", "--porcelain", "--untracked-files=no")[1].splitlines() if line])


def git_unmerged_count(name):
    """未解决冲突数：有未合并路径时 stash/pull 必然失败，需要先给明确提示。"""
    count = 0
    for line in run_git(name, "status", "--porcelain", "--untracked-files=no")[1].splitlines():
        code = line[:2]
        if code in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"):
            count += 1
    return count


def git_pull_one(name):
    """拉取单个项目：暂存改动 → pull --ff-only → 恢复改动（无论成功与否）。"""
    if not is_git_repo(name):
        say(f"[{name}] 不是 git 仓库，跳过")
        return 0
    directory = project_dir(name)
    branch = run_git(name, "rev-parse", "--abbrev-ref", "HEAD")[1].strip() or "?"
    upstream = run_git(name, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")[1].strip() or "无"
    say(f"[{name}] 分支 {branch} 上游 {upstream}")

    unmerged = git_unmerged_count(name)
    if unmerged > 0:
        say(f"[{name}] 有 {unmerged} 个文件未解决冲突，暂存/拉取都会失败")
        say(f"[{name}] 解决方式：控制台点“代码 → 冲突”逐块选择，或手动处理后 git add")
        return 1

    dirty = git_dirty_count(name)
    label = time.strftime("devctl-auto %Y-%m-%d %H:%M:%S")
    if dirty > 0:
        say(f"[{name}] 检测到 {dirty} 处未提交改动，先暂存（{label}）")
        code, _, err = run_git(name, "stash", "push", "-m", label)
        if code != 0:
            say(f"[{name}] 暂存失败，未执行拉取")
            say(err.rstrip())
            return 1

    say(f"[{name}] git pull --ff-only ...")
    code, _, err = run_git(name, "pull", "--ff-only", stream=True)
    if code != 0:
        say(f"[{name}] 拉取失败（常见原因：本地有未推送提交，无法快进）")
        if err:
            say(err.rstrip())

    if dirty > 0:
        say(f"[{name}] 恢复本地改动 ...")
        pop_code = run_git(name, "stash", "pop")[0]
        if pop_code == 0:
            say(f"[{name}] 本地改动已恢复")
        else:
            stash_head = run_git(name, "stash", "list")[1].splitlines()[:1]
            say(f"[{name}] 恢复冲突：改动仍保存在 git stash（{stash_head[0] if stash_head else ''}）")
            say(f"[{name}] 解决方式：控制台点“代码 → 冲突”逐块选择保留哪一侧，")
            say(f"[{name}]   解决并标记已解决后，再决定是否 git stash drop（{directory}）")

    if code == 0:
        say(f"[{name}] 拉取完成")
    return code


def git_stash_one(name):
    """手动暂存本地改动，供"先放着、稍后恢复"的场景使用。"""
    if not is_git_repo(name):
        say(f"[{name}] 不是 git 仓库")
        return 1
    unmerged = git_unmerged_count(name)
    if unmerged > 0:
        say(f"[{name}] 有 {unmerged} 个文件未解决冲突，无法暂存")
        say(f"[{name}] 解决方式：控制台点“代码 → 冲突”逐块选择，或手动处理后 git add")
        return 1
    dirty = git_dirty_count(name)
    if dirty == 0:
        say(f"[{name}] 没有未提交改动，无需暂存")
        return 0
    label = time.strftime("devctl-manual %Y-%m-%d %H:%M:%S")
    code, _, err = run_git(name, "stash", "push", "-m", label)
    if code == 0:
        say(f"[{name}] 已暂存 {dirty} 处改动：{label}")
        return 0
    say(f"[{name}] 暂存失败")
    say(err.rstrip())
    return 1


def git_fetch_one(name):
    """只更新 origin/* 远程跟踪分支，不改工作区。"""
    if not is_git_repo(name):
        say(f"[{name}] 不是 git 仓库")
        return 1
    say(f"[{name}] git fetch --all --prune ...")
    code, _, err = run_git(name, "fetch", "--all", "--prune", stream=True)
    if code == 0:
        say(f"[{name}] 已获取远端最新分支和提交")
        return 0
    if err:
        say(err.rstrip())
    say(f"[{name}] 获取远端失败（检查网络或远程地址）")
    return 1


def git_unstash_one(name):
    """恢复最近一次由 devctl 打的暂存，避免误弹别人的 stash。"""
    if not is_git_repo(name):
        say(f"[{name}] 不是 git 仓库")
        return 1
    stash_list = run_git(name, "stash", "list")[1]
    if "devctl-" not in stash_list:
        say(f"[{name}] 没有可恢复的 devctl 暂存")
        return 1
    say(f"[{name}] git stash pop ...")
    code, out, err = run_git(name, "stash", "pop")
    if out:
        say(out.rstrip())
    if code == 0:
        say(f"[{name}] 已恢复")
        return 0
    output = f"{out}\n{err}"
    say(f"[{name}] 恢复冲突，改动仍保留在 stash")
    if "would be overwritten" in output.lower():
        say(f"[{name}] 这条暂存的内容已经在工作区里（解冲突时可能已经恢复过），不用再恢复")
        say(f"[{name}] 在控制台“代码 → 暂存记录”里点这条的“删除”即可")
    return 1


# ---------------------------------------------------------------
# 安装依赖（api 模块 / 前端包管理器）
# ---------------------------------------------------------------

def pom_artifact(pom):
    """读 pom 的 artifactId，跳过 <parent> 块（父模块的 id 不是本模块的 id）。"""
    try:
        text = Path(pom).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = re.sub(r"<parent>.*?</parent>", "", text, flags=re.S)
    match = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", text)
    return match.group(1) if match else ""


def detect_api_modules(directory):
    """未配置第 9 列时自动识别：artifactId 以 -api 结尾的模块。"""
    modules = []
    root = Path(directory)
    for pom in sorted(root.rglob("pom.xml")):
        rel = pom.relative_to(root)
        if "target" in rel.parts or len(rel.parts) > 4:
            continue
        if pom_artifact(pom).endswith("-api"):
            modules.append(str(rel.parent).replace(os.sep, "/") if str(rel.parent) != "." else "")
    return ",".join(module for module in modules if module)


def install_modules_of(name):
    """安装目标：配置第 9 列；否则自动识别 api 模块；前端用 @npm/@pnpm 这类标记。"""
    spec = conf_field(name, "install")
    if spec:
        return spec
    directory = project_dir(name)
    if not (directory / "pom.xml").is_file():
        return ""
    return detect_api_modules(directory)


def install_one(name):
    """只 install 不重启：安装的是给别的项目复用的 api 包。"""
    directory = project_dir(name)
    modules = install_modules_of(name)
    if modules in ("@npm", "@pnpm", "@yarn", "@bun"):
        pm = modules[1:]
        executable = find_tool(pm, f"{pm}.cmd")
        if not executable:
            say(f"[{name}] 未找到 {pm} 命令")
            return 1
        say(f"[{name}] {pm} install ...")
        return subprocess.run([executable, "install"], cwd=directory).returncode
    if not (directory / "pom.xml").is_file():
        say(f"[{name}] 不是 Maven 项目，跳过")
        return 0
    if not modules:
        say(f"[{name}] 未找到 api 模块，跳过")
        return 0
    say(f"[{name}] mvn install 模块：{modules}")
    return run_mvn(["-B", "-Dmaven.test.skip=true", "-pl", modules, "-am", "install"], cwd=directory)


# ---------------------------------------------------------------
# 状态输出
# ---------------------------------------------------------------

def watch_pid_path(target):
    """热更新 pid 文件：单项目 <项目>.watch.pid，全部 dev-watch-all.pid。"""
    return STATE / ("dev-watch-all.pid" if target == "all" else f"{target}.watch.pid")


def watch_pid_value(target):
    """仍存活的监听进程 PID；顺带清理失效文件。"""
    path = watch_pid_path(target)
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    if plat.pid_alive(pid):
        return pid
    path.unlink(missing_ok=True)
    return None


def status_one(name):
    """命令行状态：运行情况 + 热更新监听。"""
    if pid_alive(name):
        say(f"[{name}] 运行中 PID={read_pid(name)} 端口={project_port(name)} 日志={log_file(name)}")
    elif port_listening(name):
        say(f"[{name}] 端口 {project_port(name)} 监听中（pid 文件未记录）")
    else:
        say(f"[{name}] 未运行")
    watch = watch_pid_value(name)
    if watch:
        say(f"  热更新监听中 PID={watch}")


def action_state(name):
    """给 Web 控制台的异步动作标记：{action, running, exit, exit_time}。"""
    action, running, exit_code, exit_time = "", False, "", 0
    running_path = action_running_file(name)
    if recent_file(running_path, 600):
        try:
            action = running_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            action = ""
        running = True
    exit_path = action_exit_file(name)
    if recent_file(exit_path, 600):
        try:
            exit_code = exit_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            exit_code = ""
        try:
            exit_time = int(exit_path.stat().st_mtime)
        except OSError:
            exit_time = 0
    return action, running, exit_code, exit_time


def json_status():
    """输出单行 JSON（Web 控制台 1 秒轮询的数据源），字段与旧 bash 版一致。"""
    local_host = os.environ.get("TXLC_LOCAL_HOST", "localhost")
    ports = listening_ports()
    projects = []
    for name in all_names():
        port = port_number(name)
        kind = project_type(name)
        doc = conf_field(name, "doc") or (
            f"http://{local_host}:{port}/" if kind == "frontend" else f"http://{local_host}:{port}/doc.html"
        )
        if pid_alive(name):
            running, pid = True, str(read_pid(name) or "")
        elif port in ports:
            running, pid = True, ""
        else:
            running, pid = False, ""
        action, action_running, action_exit, action_exit_time = action_state(name)
        depends = [item.strip() for item in conf_field(name, "depends").split(",") if item.strip()]
        requires = [item.strip() for item in conf_field(name, "requires").split(",") if item.strip()]
        projects.append({
            "name": name,
            "port": port,
            "running": running,
            "pid": pid,
            "watch": bool(watch_pid_value(name)),
            "core": is_core(name),
            "log": str(log_file(name)),
            "dir": str(project_dir(name)),
            "doc": doc,
            "action": action,
            "action_running": action_running,
            "action_exit": action_exit,
            "action_exit_time": action_exit_time,
            "install": conf_field(name, "install"),
            "type": kind,
            "start": conf_field(name, "start"),
            "stop": conf_field(name, "stop"),
            "build": conf_field(name, "build"),
            "group": conf_field(name, "group"),
            "depends": depends,
            "requires": requires,
        })
    say(json.dumps(projects, ensure_ascii=False, separators=(",", ":")))


# ---------------------------------------------------------------
# 热更新
# ---------------------------------------------------------------

WATCH_SUFFIXES = (".java", ".xml", ".yml", ".yaml", ".properties")


def scan_changes(dirs, stamp):
    """找出比 stamp 新的源码文件；跳过 target/.git/node_modules，避免扫描构建产物。"""
    changed = []
    for directory in dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for current, subdirs, files in os.walk(root):
            subdirs[:] = [d for d in subdirs if d not in ("target", ".git", "node_modules")]
            for filename in files:
                if not filename.endswith(WATCH_SUFFIXES) and filename != "pom.xml":
                    continue
                path = Path(current) / filename
                try:
                    if path.stat().st_mtime > stamp:
                        changed.append(path)
                except OSError:
                    continue
    return changed


def watch_projects(target, names):
    """监听源码变化：构建成功后重启对应服务；构建失败保留旧进程。"""
    existing = watch_pid_value(target)
    if existing:
        say(f"[{target}] 热更新已在运行 (PID {existing})")
        return 1

    pidfile = watch_pid_path(target)
    stamp_file = STATE / f"dev-watch-{target}.stamp"
    stamp_file.touch()
    pidfile.write_text(str(os.getpid()))

    def cleanup(*_):
        pidfile.unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    if plat.IS_WINDOWS and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, cleanup)
    try:
        say(f"[{target}] 热更新监听中，源码变化后自动构建并重启；Ctrl+C 退出")
        say(f"监听项目：{' '.join(names)}")
        while True:
            changed = scan_changes([project_dir(name) for name in names], stamp_file.stat().st_mtime)
            if changed:
                stamp_file.touch()
                time.sleep(2)
                touched = {path.parent for path in changed}
                for name in names:
                    directory = project_dir(name).resolve()
                    # 只有变更文件落在本项目的目录树里才构建，避免全量重启。
                    if not any(directory == parent or directory in parent.parents for parent in touched):
                        continue
                    say(f"[{name}] 检测到变更，构建中...")
                    build_cmd = conf_field(name, "build")
                    ok = False
                    if build_cmd:
                        script = resolve_cmd_path(directory, build_cmd)
                        ok = script.is_file() and run_script(script, directory) == 0
                    else:
                        ok = run_mvn(["-B", "-q", "-Dmaven.test.skip=true", "package"], cwd=directory) == 0
                    if ok:
                        say(f"[{name}] 构建成功，重启...")
                        stop_one(name)
                        start_one(name)
                    else:
                        say(f"[{name}] 构建失败，保留当前进程")
            time.sleep(2)
    finally:
        pidfile.unlink(missing_ok=True)


def unwatch_one(target):
    """停止热更新监听进程。"""
    path = watch_pid_path(target)
    pid = watch_pid_value(target)
    if pid:
        say(f"[{target}] 停止热更新 (PID {pid})")
        kill_process(pid, grace=1)
        path.unlink(missing_ok=True)
    else:
        say(f"[{target}] 未在监听")


def order_for_start(targets):
    """启动顺序：核心项目（gateway 除外）→ 其他 → gateway；不在目标里的不补。"""
    core = core_names()
    ordered = [name for name in core if name != "txlc-gateway" and name in targets]
    ordered += [name for name in targets if name != "txlc-gateway" and name not in core]
    if "txlc-gateway" in targets:
        ordered.append("txlc-gateway")
    return ordered


# ---------------------------------------------------------------
# 交互式面板（保留给终端用户）
# ---------------------------------------------------------------

def panel():
    while True:
        if sys.stdout.isatty():
            os.system("clear" if not plat.IS_WINDOWS else "cls")
        print("==============================================")
        print(" TXLC Java 服务控制面板")
        print("==============================================")
        do_status("all")
        print("\n项目列表：")
        do_list()
        print("\n1) 启动项目        2) 停止项目")
        print("3) 重启项目        4) 更新项目")
        print("5) 热更新项目      6) 停止热更新")
        print("7) 启动核心项目    8) 停止全部")
        print("0) 退出\n")
        try:
            choice = input("选择操作: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == "0":
            return 0
        if choice == "7":
            do_start("core")
            continue
        if choice == "8":
            do_stop("all")
            continue
        if choice not in {"1", "2", "3", "4", "5", "6"}:
            print("无效选择")
            time.sleep(1)
            continue
        try:
            project = input("输入项目名（全名或简称，回车=all）: ").strip() or "all"
        except (EOFError, KeyboardInterrupt):
            return 0
        {"1": do_start, "2": do_stop, "3": do_restart, "4": do_update,
         "5": lambda value: do_watch(value), "6": do_unwatch}[choice](project)
        try:
            input("\n回车返回菜单...")
        except (EOFError, KeyboardInterrupt):
            return 0


# ---------------------------------------------------------------
# 命令实现（命令行与面板共用）
# ---------------------------------------------------------------

def config_hint():
    """配置为空时的统一提示：告诉用户「怎么把项目加进来」，而不是只报未知项目。"""
    if PROJECTS_CONF.is_file():
        return (
            "projects.conf 里还没有任何项目。请编辑 projects.conf，"
            "或运行 ./install.sh --scan <你的代码目录> 生成候选。"
        )
    return (
        "未找到 projects.conf。请先运行 ./install.sh 生成模板，"
        "或运行 ./install.sh --scan <你的代码目录> 生成候选。"
    )


def do_list():
    names = all_names()
    if not names:
        say(config_hint())
        return 0
    for name in names:
        print(f"{name}  port={project_port(name)}  dir={project_dir(name)}")
    return 0


def do_status(target):
    names = resolve_targets(target)
    if names is None:
        say(f"未知项目：{target}")
        return 1
    if not names:
        say(config_hint())
        return 1
    for name in names:
        status_one(name)
    return 0


def do_doctor(target):
    """环境自检入口；doctor 放在独立模块，避免 dashboard/HTTP 层被一起加载。"""
    if target not in ("all", "--env"):
        say("用法：devctl.py doctor [--env]")
        return 1
    import doctor  # 延迟导入：只有真正执行 doctor 时才加载检查逻辑

    return doctor.run(env_only=(target == "--env"))


def do_start(target):
    names = resolve_targets(target)
    if names is None:
        say(f"未知项目：{target}")
        return 1
    for name in order_for_start(names):
        if start_one(name) != 0:
            return 1
    return 0


def do_stop(target):
    names = resolve_targets(target)
    if names is None:
        say(f"未知项目：{target}")
        return 1
    for name in reversed(order_for_start(names)):
        stop_one(name)
    return 0


def do_restart(target):
    names = resolve_targets(target)
    if names is None:
        say(f"未知项目：{target}")
        return 1
    ordered = order_for_start(names)
    for name in reversed(ordered):
        stop_one(name)
    for name in ordered:
        if start_one(name) != 0:
            return 1
    return 0


def do_update(target):
    names = resolve_targets(target)
    if names is None:
        say(f"未知项目：{target}")
        return 1
    for name in order_for_start(names):
        update_one(name)
    return 0


def do_git_many(target, action):
    names = resolve_targets(target)
    if names is None:
        say(f"未知项目：{target}")
        return 1
    failed = 0
    for name in names:
        print()
        failed |= 1 if action(name) != 0 else 0
    return failed


def do_install(target):
    names = resolve_targets(target)
    if names is None:
        say(f"未知项目：{target}")
        return 1
    failed = 0
    for name in names:
        print()
        failed |= 1 if install_one(name) != 0 else 0
    return failed


def do_watch(target):
    names = resolve_targets(target)
    if names is None:
        say(f"未知项目：{target}")
        return 1
    if target in ("all", "core"):
        return watch_projects(target, names)
    name = resolve_name(target)
    return watch_projects(name, [name])


def do_git_stash(target):
    """暂存单个项目的本地改动（只接受单个项目名）。"""
    name = resolve_name(target)
    if not name:
        say(f"未知项目：{target}")
        return 1
    return git_stash_one(name)


def do_git_unstash(target):
    """恢复单个项目最近一次 devctl 暂存。"""
    name = resolve_name(target)
    if not name:
        say(f"未知项目：{target}")
        return 1
    return git_unstash_one(name)


def do_unwatch(target):
    if target == "all":
        for name in all_names():
            unwatch_one(name)
        unwatch_one("all")
        return 0
    name = resolve_name(target)
    if not name:
        say(f"未知项目：{target}")
        return 1
    unwatch_one(name)
    return 0


def run_action_marker(name, action):
    """异步动作包装：写 running/exit 标记，前端据此显示进行中的按钮状态。"""
    running = action_running_file(name)
    exit_path = action_exit_file(name)
    running.write_text(action, encoding="utf-8")
    exit_path.unlink(missing_ok=True)
    code = 1
    try:
        handler = {"start": do_start, "stop": do_stop, "restart": do_restart}[action]
        code = int(handler(name) or 0)
    finally:
        exit_path.write_text(str(code), encoding="utf-8")
        running.unlink(missing_ok=True)
    return code


def main(argv):
    action = argv[1] if len(argv) > 1 else "status"
    target = argv[2] if len(argv) > 2 else "all"

    # 内部命令：由控制台调用，不给人用。
    if action == "_action":
        if len(argv) < 4:
            say("用法：devctl.py _action <项目> <start|stop|restart>")
            return 1
        name = resolve_name(argv[2])
        if not name or argv[3] not in ("start", "stop", "restart"):
            say(f"未知项目或动作：{argv[2]} {argv[3]}")
            return 1
        return run_action_marker(name, argv[3])

    handlers = {
        "list": lambda: do_list(),
        "doctor": lambda: do_doctor(target),
        "status": lambda: do_status(target),
        "start": lambda: do_start(target),
        "stop": lambda: do_stop(target),
        "restart": lambda: do_restart(target),
        "update": lambda: do_update(target),
        "install": lambda: do_install(target),
        "gitpull": lambda: do_git_many(target, git_pull_one),
        "gitfetch": lambda: do_git_many(target, git_fetch_one),
        "gitstash": lambda: do_git_stash(target),
        "gitunstash": lambda: do_git_unstash(target),
        "watch": lambda: do_watch(target),
        "unwatch": lambda: do_unwatch(target),
        "panel": lambda: panel(),
        "json": lambda: (json_status(), 0)[1],
    }
    if action in ("-h", "--help", "help"):
        print(__doc__ or "用法：devctl.py <list|doctor|status|start|stop|restart|update|install|git*|watch|unwatch|panel|json> [项目|all|core]")
        return 0
    handler = handlers.get(action)
    if handler is None:
        say("用法：devctl.py <list|doctor|status|start|stop|restart|update|install|gitpull|gitfetch|gitstash|gitunstash|watch|unwatch|panel> [项目|all|core]")
        return 1
    result = handler()
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
