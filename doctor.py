#!/usr/bin/env python3
# 环境与配置自检（doctor）。
#
# 为什么单独一层：
#   换机器时最常见的失败不是代码问题，而是「Java 没装 / mvn 不在 PATH /
#   projects.conf 里还是别人的绝对路径 / 端口被占」。这些都能在启动之前查出来，
#   不需要等 Spring 启动几分钟后看超时日志。
#
# 原则：
#   - 只读检查，不启动、不停止、不修改任何文件；
#   - 不 import dashboard.py（那是 HTTP 层，doctor 不该把服务器带起来）；
#   - 不依赖第三方库，只用标准库 + devctl 已有的解析能力；
#   - 结论分三档：OK / WARN / FAIL，有 FAIL 时退出码非 0，可直接接 CI 或钩子。
#
# 用法：
#   ./devctl.py doctor          # 环境 + 配置 + 外部依赖
#   ./devctl.py doctor --env    # 只查环境（Java/Maven/Git/Node/包管理器）
#   python3 doctor.py --env     # 不经过 devctl 直接跑也可以
#
# 退出码：0 = 没有 FAIL；1 = 有 FAIL；2 = 参数错误。

import os
import re
import subprocess
import sys
from pathlib import Path

# 输出顺序固定，避免不同机器上同一份配置的日志顺序不一样。
LEVEL_LABELS = ("FAIL", "WARN", "INFO", "OK")

# 前端 install 列支持这些包管理器标记（与 devctl.install_one 保持一致）。
PACKAGE_MANAGERS = ("npm", "pnpm", "yarn", "bun")


class Report:
    """收集检查结果；section 变化时在输出里打小标题。"""

    def __init__(self):
        self.items = []

    def add(self, level, section, text, hint=""):
        """level 只接受 OK/INFO/WARN/FAIL，hint 是给用户看的下一步动作。"""
        self.items.append({
            "level": level,
            "section": section,
            "text": text,
            "hint": hint,
        })

    def ok(self, section, text, hint=""):
        self.add("OK", section, text, hint)

    def info(self, section, text, hint=""):
        self.add("INFO", section, text, hint)

    def warn(self, section, text, hint=""):
        self.add("WARN", section, text, hint)

    def fail(self, section, text, hint=""):
        self.add("FAIL", section, text, hint)

    def counts(self):
        """返回 {level: 数量}，用于末尾汇总。"""
        result = {level: 0 for level in LEVEL_LABELS}
        for item in self.items:
            result[item["level"]] += 1
        return result

    def has_fail(self):
        return any(item["level"] == "FAIL" for item in self.items)


# ---------------------------------------------------------------
# 配置解析（带行号，方便直接告诉用户改哪一行）
# ---------------------------------------------------------------

def split_conf(value):
    """把逗号分隔的依赖列拆成列表；空值和多余空格都丢掉。"""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def read_conf_rows(dv):
    """读取 projects.conf，返回 (项目列表, 问题列表)。

    项目列表为 None 表示文件不存在（与「文件存在但没有项目」区分开）；
    问题列表里的元素是 (level, text, hint)，由 check_config 统一输出。
    """
    path = Path(dv.PROJECTS_CONF)
    if not path.is_file():
        return None, []

    projects = []
    issues = []
    seen = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return None, [("FAIL", f"读取 {path.name} 失败：{exc}", "")]

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            issues.append((
                "FAIL",
                f"第 {lineno} 行列数不足：至少需要「项目名|目录」",
                "格式见 projects.conf.example 顶部注释。",
            ))
            continue
        if len(parts) > len(dv.CONF_FIELDS):
            issues.append((
                "WARN",
                f"第 {lineno} 行有 {len(parts)} 列，超过 {len(dv.CONF_FIELDS)} 列的部分会被忽略",
                "",
            ))
        name = parts[0]
        if name in seen:
            # 重名会让 start/stop 永远只命中第一行，必须当错误处理。
            issues.append((
                "FAIL",
                f"第 {lineno} 行项目名重复：{name}",
                "删掉重复行，或给其中一个改名。",
            ))
            continue
        seen.add(name)
        projects.append({
            field: (parts[i] if i < len(parts) else "")
            for i, field in enumerate(dv.CONF_FIELDS)
        })
    return projects, issues


def read_deps(path):
    """解析 deps.conf，返回 (名称集合, 问题列表)。

    只做结构校验，不发网络请求：doctor 不应该因为依赖机器不在线而卡住。
    """
    names = set()
    issues = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return names, [("FAIL", f"读取 {path.name} 失败：{exc}", "")]

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 4 or not parts[0] or not parts[2]:
            issues.append((
                "WARN",
                f"deps.conf 第 {lineno} 行格式不完整，应为主体「名称|tcp或http|地址|端口」",
                "格式见 deps.conf.example。",
            ))
            continue
        if parts[1] and parts[1].lower() not in ("tcp", "http"):
            issues.append((
                "WARN",
                f"deps.conf 第 {lineno} 行类型 {parts[1]} 不认识，将按 tcp 处理",
                "类型只支持 tcp / http。",
            ))
        if not re.fullmatch(r"\d{1,5}", parts[3]) or not 1 <= int(parts[3]) <= 65535:
            issues.append((
                "WARN",
                f"deps.conf 第 {lineno} 行端口不合法：{parts[3]}",
                "",
            ))
        if parts[0] in names:
            issues.append((
                "WARN",
                f"deps.conf 第 {lineno} 行依赖名重复：{parts[0]}",
                "后一行会覆盖前一行，建议删掉重复行。",
            ))
        names.add(parts[0])
    return names, issues


# ---------------------------------------------------------------
# 环境检查
# ---------------------------------------------------------------

def command_output(exe, args):
    """执行 --version 这类只读命令；失败返回空字符串，不影响后续检查。"""
    try:
        proc = subprocess.run(
            [exe] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()


def first_line(text):
    """取命令输出的第一行，用于把版本号压成一行。"""
    return text.splitlines()[0].strip() if text else ""


def java_version(text):
    """从 `java -version` 输出里取版本号（macOS 与 Temurin 写法不同）。"""
    match = re.search(r'version "([^"]+)"', text)
    if not match:
        match = re.search(r"(\d+(?:\.\d+)+)", text)
    return match.group(1) if match else first_line(text)


def java_major(version):
    """把 1.8.0_392 / 17.0.9 统一成主版本号；解析不了返回 0。"""
    if not version:
        return 0
    parts = re.findall(r"\d+", version)
    if not parts:
        return 0
    major = int(parts[0])
    # JDK 8 及更早用 1.x 命名，真正的主版本是第二段。
    if major == 1 and len(parts) > 1:
        return int(parts[1])
    return major


def needs_from_projects(projects):
    """根据配置推断环境检查的严格程度：有没有后端、前端、需要哪些包管理器。"""
    result = {"backend": False, "frontend": False, "package_managers": set()}
    for project in projects or []:
        kind = project.get("type") or "backend"
        if kind == "frontend":
            result["frontend"] = True
            spec = project.get("install") or ""
            if spec.startswith("@") and spec[1:] in PACKAGE_MANAGERS:
                result["package_managers"].add(spec[1:])
        else:
            result["backend"] = True
    return result


def check_environment(dv, report, needs):
    """检查运行依赖；有后端项目时 Java 缺失是 FAIL，否则只提示。"""
    section = "环境"

    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 8):
        report.ok(section, f"Python {version}（{sys.executable}）")
    else:
        report.fail(
            section,
            f"Python {version} 过旧，需要 3.8 及以上",
            "控制台用到 pathlib/unlink(missing_ok=...) 等 3.8 特性。",
        )

    java = dv.find_tool("java", "java.exe")
    if java:
        text = command_output(java, ["-version"])
        java_ver = java_version(text)
        major = java_major(java_ver)
        if major and major < 17:
            report.warn(
                section,
                f"Java {java_ver} 版本偏低（{java}）",
                "多数 Spring Boot 3 项目要求 17+；仍可尝试启动，但可能报 UnsupportedClassVersionError。",
            )
        else:
            report.ok(section, f"Java {java_ver or '已安装'}（{java}）")
    else:
        level = "FAIL" if needs["backend"] else "WARN"
        report.add(
            level,
            section,
            "未找到 java",
            "安装 JDK 17+ 并确认 `java -version` 可用；GUI 启动时 PATH 可能不完整，建议设置 JAVA_HOME。",
        )

    maven = dv.find_tool("mvn", "mvn.cmd")
    if maven:
        text = command_output(maven, ["-v"])
        report.ok(section, f"Maven {first_line(text) or '已安装'}")
    else:
        # 已有 jar 时可以不构建直接启动，所以只算警告。
        level = "WARN" if needs["backend"] else "INFO"
        report.add(
            level,
            section,
            "未找到 mvn",
            "需要构建/更新后端项目时必需；常见做法是把 Maven 解压到 dev-tools 下或加入 PATH。",
        )

    git = dv.find_tool("git", "git.exe")
    if git:
        report.ok(section, f"Git {command_output(git, ['--version']).replace('git version ', '') or '已安装'}")
    else:
        report.warn(
            section,
            "未找到 git",
            "不影响启动，但 `update` / `gitpull` / `gitstash` 等按钮会失败。",
        )

    if needs["frontend"]:
        node = dv.find_tool("node", "node.exe")
        if node:
            report.ok(section, f"Node {command_output(node, ['--version']).lstrip('v') or '已安装'}")
        else:
            report.fail(section, "前端项目需要 Node.js，但未找到 node", "安装 Node.js 18+，并确认 `node -v` 可用。")
        for pm in sorted(needs["package_managers"]):
            found = dv.find_tool(pm, f"{pm}.cmd")
            if found:
                report.ok(section, f"{pm} 已安装（{found}）")
            else:
                report.fail(
                    section,
                    f"前端项目声明使用 {pm}，但未找到该命令",
                    f"安装 {pm}，或把 projects.conf 的 install 列改成已安装的包管理器。",
                )

    if needs["backend"]:
        settings = Path(dv.MAVEN_SETTINGS)
        if settings.is_file():
            report.ok(section, f"{settings.name} 已存在")
        else:
            report.warn(
                section,
                f"未找到 {settings.name}",
                "只有使用内网 Maven 仓库时才需要：从 maven-settings.xml.example 复制后填写账号。",
            )


# ---------------------------------------------------------------
# 配置检查
# ---------------------------------------------------------------

def expected_suffix():
    """当前平台期望的脚本后缀；用于提示跨平台配置错误。"""
    return ".cmd" if os.name == "nt" else ".sh"


def check_project_scripts(dv, report, section, project, directory):
    """检查自定义启动/停止/构建脚本是否存在、是否属于当前平台。"""
    # 启动脚本缺失直接算 FAIL：配置了却跑不起来，比没配置更难排查。
    rules = (("start", "启动", "FAIL"), ("stop", "停止", "WARN"), ("build", "构建", "WARN"))
    for field, label, level in rules:
        raw = project.get(field) or ""
        if not raw:
            continue
        script = Path(dv.resolve_cmd_path(directory, raw))
        if not script.is_file():
            report.add(
                level,
                section,
                f"{project['name']} 的{label}脚本不存在：{script}",
                "迁移机器后 services/ 不会进仓库，需要重新生成脚本或改 projects.conf。",
            )
            continue
        suffix = script.suffix.lower()
        if suffix and suffix != expected_suffix() and suffix in (".sh", ".cmd", ".bat"):
            report.warn(
                section,
                f"{project['name']} 的{label}脚本是 {suffix} 文件，当前平台使用 {expected_suffix()}",
                "本机只能执行对应平台的脚本；Windows 需要重新生成 .cmd。",
            )


def check_config(dv, report, projects, issues):
    """检查 projects.conf 的结构、目录、端口与脚本。"""
    section = "配置"
    path = Path(dv.PROJECTS_CONF)

    for level, text, hint in issues:
        report.add(level, section, text, hint)

    if projects is None:
        report.fail(
            section,
            f"未找到 {path.name}",
            "运行 ./install.sh 生成配置模板，修改后再执行 doctor。",
        )
        return

    if not projects:
        report.warn(
            section,
            f"{path.name} 里还没有任何项目",
            "运行 ./install.sh --scan <你的代码目录> 生成候选行，再人工确认项目名和路径。",
        )
        return

    report.ok(section, f"{path.name}：{len(projects)} 个项目")

    names = [project["name"] for project in projects]
    port_map = {}
    for project in projects:
        name = project["name"]
        raw_dir = project["dir"]

        if not raw_dir:
            report.fail(section, f"{name} 没有配置目录", "第 2 列必填。")
            continue

        directory = Path(raw_dir).expanduser()
        if not directory.is_absolute():
            report.warn(
                section,
                f"{name} 的目录不是绝对路径：{raw_dir}",
                "相对路径会按运行命令时的当前目录解析，换目录就会找不到项目。",
            )
        if not directory.is_dir():
            report.fail(
                section,
                f"{name} 的目录不存在：{directory}",
                "这是迁移机器后最常见的问题：把 projects.conf 里的路径改成本机路径。",
            )
            continue

        core = project.get("core") or ""
        if core not in ("", "0", "1"):
            report.warn(section, f"{name} 的 core 列应为 1 或 0，当前是：{core}")

        kind = project.get("type") or "backend"
        if kind not in ("backend", "frontend"):
            report.warn(
                section,
                f"{name} 的 type 列应为 backend 或 frontend，当前是：{kind}",
                "未知类型会按 backend 处理。",
            )
            kind = "backend"

        port = dv.project_port(name)
        try:
            number = int(port)
            if not 1 <= number <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            number = None
            report.fail(
                section,
                f"{name} 的端口不合法：{port}",
                "检查 ports.conf 或 src/main/resources/application.yml 里的 server.port。",
            )
        if number is not None:
            port_map.setdefault(number, []).append(name)

        check_project_scripts(dv, report, section, project, directory)

        jar_setting = project.get("jar") or ""
        jar = dv.project_jar(name)
        if jar_setting and not jar:
            report.warn(
                section,
                f"{name} 配置的 jar 尚未生成：{jar_setting}",
                "先执行 update/构建，或清空第 4 列改用自动查找。",
            )
        elif not jar and not project.get("start") and kind == "backend":
            report.info(
                section,
                f"{name} 还没有可执行 jar，首次启动会自动 mvn package",
                "",
            )

        if kind == "backend" and not (directory / "pom.xml").is_file() and not project.get("start"):
            report.warn(
                section,
                f"{name} 目录下没有 pom.xml，也没有启动脚本",
                "确认目录是否填错；传统 WAR 项目需要 start 脚本配合 Tomcat。",
            )

        if kind == "frontend":
            if not (directory / "package.json").is_file():
                report.fail(
                    section,
                    f"{name} 被标为前端，但目录下没有 package.json",
                    "确认目录是否填错，或把 type 列改回 backend。",
                )
            spec = project.get("install") or ""
            if not spec:
                report.warn(
                    section,
                    f"{name} 未指定前端包管理器（install 列）",
                    "填 @npm / @pnpm / @yarn / @bun，否则 install 命令会跳过。",
                )
            elif not (spec.startswith("@") and spec[1:] in PACKAGE_MANAGERS):
                report.warn(
                    section,
                    f"{name} 的 install 列不是前端标记：{spec}",
                    "前端只支持 @npm / @pnpm / @yarn / @bun。",
                )

        # 后端 install 列是 Maven 模块路径；模块不存在时 install 会报错。
        spec = project.get("install") or ""
        if spec and not spec.startswith("@"):
            for module in split_conf(spec):
                if module and not (directory / module).exists():
                    report.warn(
                        section,
                        f"{name} 的 install 模块不存在：{module}",
                        "检查第 9 列是否写成了当前仓库里的模块路径。",
                    )

    for number, owners in sorted(port_map.items()):
        if len(owners) > 1:
            report.fail(
                section,
                f"端口 {number} 被多个项目占用：{', '.join(owners)}",
                "在 ports.conf 里给其中一个项目换端口（格式：项目名 端口）。",
            )

    known = set(names)
    for project in projects:
        for dependency in split_conf(project.get("depends")):
            if dependency not in known:
                report.warn(
                    section,
                    f"{project['name']} depends 引用了未登记的项目：{dependency}",
                    "检查第 12 列，或先把这个项目加进 projects.conf。",
                )

    # 端口占用只提示，不当错误：服务可能正是由控制台启动的。
    # 汇总成一行，避免 17 个项目里有 10 个在跑时刷出一屏 WARN。
    running = []
    occupied = []
    for project in projects:
        name = project["name"]
        directory = Path(project["dir"]).expanduser()
        if not directory.is_dir():
            continue
        port = dv.project_port(name)
        if not dv.port_listening(name):
            continue
        if dv.pid_alive(name):
            running.append(f"{name}({port})")
        else:
            occupied.append(f"{name}({port})")

    if running:
        report.info(section, f"正在运行：{', '.join(running)}")
    if occupied:
        report.warn(
            section,
            f"以下端口已被其他进程占用：{', '.join(occupied)}",
            "若这些服务不是由本控制台启动的（例如 IDE），控制台无法接管，启动会被跳过。",
        )


# ---------------------------------------------------------------
# 外部依赖检查
# ---------------------------------------------------------------

def check_deps(dv, report, projects):
    """检查 requires 引用的依赖是否都在 deps.conf 里登记。"""
    section = "外部依赖"
    deps_path = Path(dv.TOOLS_DIR) / "deps.conf"
    requires = []
    for project in projects:
        for name in split_conf(project.get("requires")):
            requires.append((project["name"], name))

    if not requires:
        report.info(section, "没有项目声明外部依赖（第 13 列 requires）")
        return

    if not deps_path.is_file():
        report.fail(
            section,
            f"未找到 {deps_path.name}，但有项目声明了 requires",
            "从 deps.conf.example 复制并按内网地址填写。",
        )
        return

    deps, issues = read_deps(deps_path)
    for level, text, hint in issues:
        report.add(level, section, text, hint)

    for project, name in requires:
        if name not in deps:
            report.fail(
                section,
                f"{project} 声明的依赖未登记：{name}",
                "在 deps.conf 补一行，或删掉 projects.conf 里的这一项；否则启动闸门会拦截。",
            )

    report.ok(section, f"{deps_path.name}：{len(deps)} 个依赖，{len(requires)} 处引用")


# ---------------------------------------------------------------
# 输出与入口
# ---------------------------------------------------------------

def render(report, stream=None):
    """按 section 分组打印；末尾给汇总，有 FAIL 时给明确退出提示。"""
    stream = stream or sys.stdout
    current = None
    for item in report.items:
        if item["section"] != current:
            current = item["section"]
            print(f"\n== {current} ==", file=stream)
        print(f"  {item['level']:<4} {item['text']}", file=stream)
        if item["hint"]:
            print(f"       {item['hint']}", file=stream)

    counts = report.counts()
    print("\n== 结果 ==", file=stream)
    print(
        f"  OK {counts['OK']} 项，WARN {counts['WARN']} 项，FAIL {counts['FAIL']} 项",
        file=stream,
    )
    if counts["FAIL"]:
        print("  有 FAIL：先按上面的提示修复，再启动服务。", file=stream)
    elif counts["WARN"]:
        print("  没有 FAIL，可以启动；WARN 项建议后续处理。", file=stream)
    else:
        print("  全部通过。", file=stream)


def collect(dv, env_only=False):
    """执行全部检查并返回 Report；抽出来便于单元测试直接断言。"""
    report = Report()
    projects, issues = read_conf_rows(dv)
    needs = needs_from_projects(projects)
    check_environment(dv, report, needs)
    if env_only:
        return report
    check_config(dv, report, projects, issues)
    check_deps(dv, report, projects or [])
    return report


def run(env_only=False, dv=None):
    """供 devctl 调用：打印结果并返回退出码。"""
    if dv is None:
        import devctl as dv  # 延迟导入：避免 doctor 直接拉起 devctl 的模块级副作用
    report = collect(dv, env_only=env_only)
    render(report)
    return 1 if report.has_fail() else 0


def main(argv=None):
    """直接运行 doctor.py 时的参数解析。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ("-h", "--help") for arg in argv):
        print(__doc__ or "用法：doctor.py [--env]")
        return 0
    unknown = [arg for arg in argv if arg != "--env"]
    if unknown:
        print(f"未知参数：{' '.join(unknown)}", file=sys.stderr)
        print("用法：doctor.py [--env]", file=sys.stderr)
        return 2
    return run(env_only="--env" in argv)


if __name__ == "__main__":
    sys.exit(main())
