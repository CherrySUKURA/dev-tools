#!/usr/bin/env bash
# 首次初始化脚本（macOS / Linux）。
#
# 检查环境并初始化空配置，不自动安装软件，不覆盖现有配置：
#   1. 创建空配置（示例保留在 *.example）；
#   2. 建好运行目录 services/ logs/ tmp/；
#   3. 检查 Python / Java / Maven / Git / Node 是否可用。
#
# 可选 --scan：调用 discover.py 扫描 Maven 工作区，生成 projects.conf 候选行。
# 候选只做启发式识别，项目名、路径、核心标记仍要人工确认。
#
# 用法：
#   ./install.sh                       # 初始化配置并检查环境
#   ./install.sh --scan ~/java         # 同时扫描 ~/java 生成候选
#   ./install.sh --scan ~/java ~/work  # 多个工作区
#
# 退出码：0 = 初始化完成（可以有 WARN）；1 = 关键依赖缺失；2 = 参数错误。

set -u

# 所有相对路径都以脚本所在目录为准，允许从任意目录调用。
cd "$(dirname "$0")" || {
    echo "无法进入脚本目录" >&2
    exit 1
}

SCAN=0
ROOTS=()
WARN_COUNT=0
FAIL_COUNT=0

usage() {
    cat <<'EOF'
用法：./install.sh [--scan <目录>...]

  --scan <目录>   扫描 Maven 工作区，生成 projects.conf 候选行
  -h, --help      显示本帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --scan)
            SCAN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "未知参数：$1" >&2
            usage
            exit 2
            ;;
        *)
            ROOTS+=("$1")
            shift
            ;;
    esac
done

if [ "$SCAN" -eq 1 ] && [ ${#ROOTS[@]} -eq 0 ]; then
    echo "--scan 需要至少一个目录，例如：./install.sh --scan ~/java" >&2
    exit 2
fi

ok() { printf '  OK   %s\n' "$1"; }
warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    printf '  WARN %s\n' "$1"
}
fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf '  FAIL %s\n' "$1"
}

# 判断 projects.conf 是否已有真实项目行（注释和空行不算）。
conf_has_rows() {
    [ -f "$1" ] && grep -Eq '^[[:space:]]*[^#[:space:]]' "$1"
}

echo "TXLC 本地服务控制台 · 初始化"
echo "目录：$(pwd)"

# ---------------------------------------------------------------
# 1. 配置模板
# ---------------------------------------------------------------

echo
echo "== 配置模板 =="

# 带 --scan 时暂不创建配置，以便扫描结果可写入新配置。
if [ "$SCAN" -eq 0 ]; then
    if [ ! -f projects.conf ]; then
        printf "# 项目配置；示例见 projects.conf.example\n" > projects.conf
        ok "创建空的 projects.conf（示例见 projects.conf.example）"
    else
        ok "projects.conf 已存在，保持不变"
    fi
else
    if [ -f projects.conf ]; then
        ok "projects.conf 已存在，扫描结果不会覆盖它"
    else
        ok "projects.conf 尚未创建，将直接写入扫描结果"
    fi
fi

for base in deps ports entries health legacy-proxy; do
    if [ ! -f "${base}.conf" ]; then
        if [ -f "${base}.conf.example" ]; then
            printf "# 本机配置；示例见 %s.conf.example\n" "$base" > "${base}.conf"
            ok "创建 ${base}.conf"
        else
            warn "缺少 ${base}.conf.example，跳过"
        fi
    else
        ok "${base}.conf 已存在，保持不变"
    fi
done

if [ ! -f maven-settings.xml ] && [ -f maven-settings.xml.example ]; then
    cp maven-settings.xml.example maven-settings.xml
    ok "创建 maven-settings.xml（仅内网 Maven 仓库需要填写）"
fi

# 运行目录：services/ 放本机生成的启动脚本（含绝对路径，已被 .gitignore 忽略）。
mkdir -p services logs tmp
ok "运行目录 services/ logs/ tmp/ 已就绪"

chmod +x devctl.py devctl.sh discover.py install.sh 2>/dev/null || true

# ---------------------------------------------------------------
# 2. 扫描工作区（可选）
# ---------------------------------------------------------------

if [ "$SCAN" -eq 1 ]; then
    echo
    echo "== 扫描 Maven 项目 =="
    PY_BIN=$(command -v python3 || command -v python || true)
    if [ -z "$PY_BIN" ]; then
        fail "找不到 python3，无法扫描"
    else
        # discover.py 把端口建议打到 stderr，候选行打到 stdout。
        TMP_OUT=$(mktemp "${TMPDIR:-/tmp}/txlc-discover.XXXXXX")
        if "$PY_BIN" discover.py "${ROOTS[@]}" > "$TMP_OUT"; then
            if grep -Eq '^[^#[:space:]]' "$TMP_OUT"; then
                if conf_has_rows projects.conf; then
                    cp "$TMP_OUT" projects.conf.candidate
                    ok "已有配置，扫描结果写入 projects.conf.candidate（确认后再替换）"
                else
                    cp "$TMP_OUT" projects.conf
                    ok "已写入 projects.conf（$(grep -Ec '^[^#[:space:]]' projects.conf) 个项目）"
                fi
            else
                warn "没有识别到 Spring Boot 项目，保留手写配置的方式"
                if [ ! -f projects.conf ]; then
                    printf "# 项目配置；示例见 projects.conf.example\n" > projects.conf
                    ok "创建空 projects.conf；示例见 projects.conf.example"
                fi
            fi
        else
            fail "discover.py 执行失败，可稍后手动运行：python3 discover.py <目录>"
        fi
        rm -f "$TMP_OUT"
    fi
fi

# ---------------------------------------------------------------
# 3. 环境检查
# ---------------------------------------------------------------

echo
echo "== 环境检查 =="

PY_BIN=$(command -v python3 || command -v python || true)
if [ -n "$PY_BIN" ] && "$PY_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    ok "Python $("$PY_BIN" -c 'import platform; print(platform.python_version())' 2>/dev/null)（$PY_BIN）"
else
    fail "需要 Python 3.8+；请从 https://www.python.org/downloads/ 安装，然后重新运行"
fi

if command -v java >/dev/null 2>&1; then
    JAVA_VER=$(java -version 2>&1 | head -n 1)
    ok "Java：$JAVA_VER"
else
    warn "未找到 Java（后端项目需要 JDK 17+）：https://adoptium.net/temurin/releases/"
fi

# Maven 既可能在 PATH，也可能解压在 dev-tools 下（与 devctl.child_env 的兜底一致）。
if command -v mvn >/dev/null 2>&1 || ls -d apache-maven-*/bin/mvn >/dev/null 2>&1; then
    ok "Maven 可用"
else
    warn "未找到 Maven（构建后端项目需要）：https://maven.apache.org/download.cgi"
fi

if command -v git >/dev/null 2>&1; then
    ok "Git $(git --version | awk '{print $3}')"
else
    warn "未找到 Git（Git 功能需要）：https://git-scm.com/downloads"
fi

if command -v node >/dev/null 2>&1; then
    ok "Node $(node --version 2>/dev/null)"
else
    echo "  INFO 未找到 Node.js（前端项目需要）：https://nodejs.org/"
fi

# ---------------------------------------------------------------
# 4. 下一步
# ---------------------------------------------------------------

echo
echo "== 下一步 =="
if conf_has_rows projects.conf; then
    echo "  1. 检查 projects.conf 里的路径是否为本机路径"
else
    echo "  1. 编辑 projects.conf，或运行 ./install.sh --scan <你的代码目录>"
fi
echo "  2. bash start.command       # 启动控制台并打开浏览器"
echo "  3. 在页面里添加本机项目；需要时运行 ./devctl.py doctor 自检"

echo
if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "初始化未完成：有 $FAIL_COUNT 个 FAIL，请先修复。"
    exit 1
fi
if [ "$WARN_COUNT" -gt 0 ]; then
    echo "初始化完成，有 $WARN_COUNT 个 WARN；不影响先试启动。"
else
    echo "初始化完成。"
fi
