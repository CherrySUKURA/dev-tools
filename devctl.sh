#!/bin/bash
# 本地 Java 服务统一控制脚本。
# 项目位置由 projects.conf 决定（项目名|绝对路径|是否核心|jar相对路径）；
# 没有 projects.conf 时才回退到自动发现 svc-* 与 txlc-gateway。
#
# 常用命令：
#   ./devctl.sh list
#   ./devctl.sh json                     # 供 Web 控制台读取的状态
#   ./devctl.sh status [项目|all|core]
#   ./devctl.sh start|stop|restart|update [项目|all|core]
#   ./devctl.sh watch [项目|all|core]    # 监听源码，自动重编译并重启
#   ./devctl.sh unwatch [项目|all|core]
#   ./devctl.sh panel                    # 打开交互式控制面板
#
# 项目名可写全名（svc-order）或简称（order）；gateway 对应 txlc-gateway。

set -u

# 控制台可以放在任意目录；项目位置由 projects.conf 决定。
TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)"
# BASE_DIR 只作为没有 projects.conf 时的默认扫描根，不再假设项目一定在这里。
BASE_DIR="$(cd "$TOOLS_DIR/.." && pwd)"
PROJECTS_CONF="$TOOLS_DIR/projects.conf"
PORTS_CONF="$TOOLS_DIR/ports.conf"
MAVEN_SETTINGS="$TOOLS_DIR/maven-settings.xml"
# 双击 .app 或 GUI 启动时 PATH 可能不完整，补上常用 bin 目录。
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
# 兼容常见的本机 Maven 安装位置；找不到时要求用户在 PATH 中提供 mvn。
if ! command -v mvn >/dev/null 2>&1; then
  for d in "$HOME/dev-tools"/apache-maven-*/bin "$HOME/.sdkman/candidates/maven/current/bin"; do
    [ -x "$d/mvn" ] && export PATH="$d:$PATH" && break
  done
fi

# 未提供 projects.conf 时的默认核心项目；提供后以 projects.conf 第三列为准。
DEFAULT_CORE="svc-auth svc-baseinfo txlc-gateway"

# projects.conf 是位置无关的项目注册表，格式：
#   项目名|项目绝对路径|是否核心(1/0)|jar相对路径(可选)
# 忽略空行和 # 注释；jar 列留空时自动在项目里查找。
projects_conf_field() {
  local name="$1" field="$2"
  [ -f "$PROJECTS_CONF" ] || return 0
  awk -F'|' -v n="$name" -v f="$field" '
    /^[[:space:]]*#/ { next }
    {
      name=$1; dir=$2; core=$3; jar=$4;
      startcmd=$5; stopcmd=$6; buildcmd=$7; doc=$8; installmod=$9; kindcol=$10;
      groupcol=$11; dependscol=$12; requirescol=$13;
      gsub(/^[ \t]+|[ \t]+$/, "", name);
      gsub(/^[ \t]+|[ \t]+$/, "", dir);
      gsub(/^[ \t]+|[ \t]+$/, "", core);
      gsub(/^[ \t]+|[ \t]+$/, "", jar);
      gsub(/^[ \t]+|[ \t]+$/, "", startcmd);
      gsub(/^[ \t]+|[ \t]+$/, "", stopcmd);
      gsub(/^[ \t]+|[ \t]+$/, "", buildcmd);
      gsub(/^[ \t]+|[ \t]+$/, "", doc);
      gsub(/^[ \t]+|[ \t]+$/, "", installmod);
      gsub(/^[ \t]+|[ \t]+$/, "", kindcol);
      gsub(/^[ \t]+|[ \t]+$/, "", groupcol);
      gsub(/^[ \t]+|[ \t]+$/, "", dependscol);
      gsub(/^[ \t]+|[ \t]+$/, "", requirescol);
      if (name == n) {
        if (f == "dir") print dir;
        else if (f == "core") print core;
        else if (f == "jar") print jar;
        else if (f == "start") print startcmd;
        else if (f == "stop") print stopcmd;
        else if (f == "build") print buildcmd;
        else if (f == "doc") print doc;
        else if (f == "install") print installmod;
        else if (f == "type") print kindcol;
        else if (f == "group") print groupcol;
        else if (f == "depends") print dependscol;
        else if (f == "requires") print requirescol;
        exit;
      }
    }
  ' "$PROJECTS_CONF"
}

# 列出所有可管理的项目；有 projects.conf 时完全以它为准。
all_names() {
  if [ -f "$PROJECTS_CONF" ]; then
    awk -F'|' '
      /^[[:space:]]*#/ { next }
      NF >= 2 {
        name=$1; dir=$2;
        gsub(/^[ \t]+|[ \t]+$/, "", name);
        gsub(/^[ \t]+|[ \t]+$/, "", dir);
        if (name != "" && dir != "") print name;
      }
    ' "$PROJECTS_CONF"
    return
  fi

  local d
  for d in "$BASE_DIR"/svc/*/ "$BASE_DIR"/txlc-gateway/; do
    [ -d "$d" ] || continue
    basename "$d"
  done
}

# 返回项目根目录；优先读 projects.conf。
project_dir() {
  local name="$1" dir
  dir="$(projects_conf_field "$name" dir)"
  if [ -n "$dir" ]; then echo "$dir"; return 0; fi
  case "$name" in
    txlc-gateway) echo "$BASE_DIR/txlc-gateway" ;;
    *) echo "$BASE_DIR/svc/$name" ;;
  esac
}

# 项目类型：backend（默认）或 frontend。老配置没有第 10 列时按后端处理。
project_type() {
  local kind
  kind="$(projects_conf_field "$1" type)"
  echo "${kind:-backend}"
}

# 自定义命令路径：支持绝对路径或相对项目根目录的路径。
resolve_cmd_path() {
  local dir="$1" cmd="$2"
  case "$cmd" in
    /*) echo "$cmd" ;;
    *) echo "$dir/$cmd" ;;
  esac
}

# 判断是否核心项目：projects.conf 有值以它为准，否则用 DEFAULT_CORE。
is_core() {
  local name="$1" core
  core="$(projects_conf_field "$name" core)"
  if [ -n "$core" ]; then [ "$core" = "1" ]; return; fi
  case " $DEFAULT_CORE " in *" $name "*) return 0 ;; esac
  return 1
}

# 返回核心项目列表；projects.conf 没标记时回退到 DEFAULT_CORE。
core_names() {
  if [ -f "$PROJECTS_CONF" ]; then
    local names
    names="$(awk -F'|' '
      /^[[:space:]]*#/ { next }
      {
        name=$1; dir=$2; core=$3;
        gsub(/^[ \t]+|[ \t]+$/, "", name);
        gsub(/^[ \t]+|[ \t]+$/, "", dir);
        gsub(/^[ \t]+|[ \t]+$/, "", core);
        if (name != "" && dir != "" && core == "1") print name;
      }
    ' "$PROJECTS_CONF")"
    # 统一成空格分隔，避免 case 匹配时被换行影响。
    [ -n "$names" ] && { echo "$names" | tr '\n' ' '; return; }
  fi
  echo "$DEFAULT_CORE"
}

# 把简称解析成项目全名，同时校验目录存在。
resolve_name() {
  local input="$1" n
  # gateway 是历史简称，实际项目目录通常为 txlc-gateway。
  [ "$input" = "gateway" ] && input="txlc-gateway"
  [ -n "$input" ] && [ -d "$(project_dir "$input")" ] && { echo "$input"; return 0; }
  for n in $(all_names); do
    [ "$n" = "$input" ] && { echo "$n"; return 0; }
    [ "$n" = "svc-$input" ] && { echo "$n"; return 0; }
  done
  return 1
}

# 把目标参数解析成项目列表；支持 all / core / 单个项目。
resolve_targets() {
  case "$1" in
    all) all_names ;;
    core) core_names ;;
    *) resolve_name "$1" || return 1 ;;
  esac
}

# 端口覆盖配置，避免多个项目默认 8080 冲突；格式：项目名 端口。
# 同时兼容「项目名|端口」写法：老用户可能按其他配置文件的竖线风格写，不该因此失效。
port_override() {
  [ -f "$PORTS_CONF" ] || return 0
  awk -F'[|[:space:]]+' -v n="$1" '!/^[[:space:]]*#/ && $1 == n { print $2; exit }' "$PORTS_CONF"
}

# 优先使用 ports.conf 覆盖，否则从 application.yml 的 server.port 读取默认值。
project_port() {
  local name="$1" f port override
  override="$(port_override "$name")"
  [ -n "$override" ] && { echo "$override"; return; }
  f="$(find "$(project_dir "$name")" -path '*/src/main/resources/application.yml' ! -path '*/target/*' -print 2>/dev/null | head -1)"
  [ -n "$f" ] || { echo "8080"; return; }
  port="$(grep -m1 '^[[:space:]]*port:' "$f" | grep -oE '[0-9]+' | tail -1)"
  echo "${port:-8080}"
}

# 返回可执行 jar；优先用 projects.conf 第四列，否则自动查找。
# 自动查找顺序：项目内 *-server/target/*.jar -> 项目根 target/*.jar。
project_jar() {
  local name="$1" dir rel jar
  dir="$(project_dir "$name")"
  rel="$(projects_conf_field "$name" jar)"

  if [ -n "$rel" ]; then
    case "$rel" in
      /*) [ -f "$rel" ] && { echo "$rel"; return; } ;;
      *)  [ -f "$dir/$rel" ] && { echo "$dir/$rel"; return; } ;;
    esac
  fi

  jar="$(find "$dir" -path '*-server/target/*.jar' ! -name '*.original' ! -name '*-sources.jar' ! -name '*-javadoc.jar' -print 2>/dev/null | head -1)"
  if [ -z "$jar" ]; then
    jar="$(find "$dir/target" -maxdepth 1 -name '*.jar' ! -name '*.original' ! -name '*-sources.jar' ! -name '*-javadoc.jar' -print 2>/dev/null | head -1)"
  fi
  echo "$jar"
}

pid_file() { echo "/tmp/$1.pid"; }
log_file() { echo "/tmp/$1.log"; }

# 端口已监听即视为服务可用。
port_listening() {
  lsof -nP -iTCP:"$(project_port "$1")" -sTCP:LISTEN >/dev/null 2>&1
}

# 一次性获取所有监听端口，供 json_status 使用；避免每个项目都跑一次 lsof。
LISTENING_PORT_CACHE=""
load_listening_ports() {
  [ -n "$LISTENING_PORT_CACHE" ] && return
  # 注意：lsof 默认文本输出最后一列是状态 "(LISTEN)"，端口在倒数第二列；
  # 用 -Fn 只输出 n 字段（如 n*:19082），再提取端口，避免把 "(LISTEN)" 当成端口导致状态永远为未运行。
  LISTENING_PORT_CACHE=" $(lsof -nP -iTCP -sTCP:LISTEN -Fn 2>/dev/null | sed -n 's/^n.*:\([0-9][0-9]*\)$/\1/p' | sort -u | tr '\n' ' ') "
}

port_in_cache() {
  case " $LISTENING_PORT_CACHE " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# 判断 pid 文件记录的进程是否仍存活。
pid_alive() {
  local name="$1" file pid
  file="$(pid_file "$name")"
  [ -f "$file" ] || return 1
  pid="$(cat "$file" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# 统一 Maven 入口：使用 dev-tools/maven-settings.xml 放行内网 HTTP Nexus。
run_mvn() {
  if [ -f "$MAVEN_SETTINGS" ]; then
    mvn -s "$MAVEN_SETTINGS" "$@"
  else
    mvn "$@"
  fi
}

# 完整构建：clean 可避免历史 target 里的旧 class 干扰。
build_one() {
  local name="$1" dir
  dir="$(project_dir "$name")"
  echo "[$name] mvn clean package ..."
  # 用 maven.test.skip=true 连测试代码一起跳过编译，避免历史失效测试阻塞构建（skipTests 仍会编译测试类）
  ( cd "$dir" && run_mvn -B -Dmaven.test.skip=true clean package )
}

# 启动单个服务；jar 不存在时先自动构建，已在运行则跳过。
start_one() {
  local name="$1" dir jar log pid i
  local -a envs=()
  dir="$(project_dir "$name")"
  log="$(log_file "$name")"

  if pid_alive "$name"; then
    echo "[$name] 已在运行 (PID $(cat "$(pid_file "$name")"))"
    return 0
  fi
  if port_listening "$name"; then
    echo "[$name] 端口 $(project_port "$name") 已被占用，未启动"
    return 1
  fi

  # 自定义服务：projects.conf 第 5 列指定启动脚本（非 Spring Boot jar / WAR/Tomcat 等）。
  local start_cmd start_path
  start_cmd="$(projects_conf_field "$name" start)"
  if [ -n "$start_cmd" ]; then
    start_path="$(resolve_cmd_path "$dir" "$start_cmd")"
    [ -f "$start_path" ] || { echo "[$name] 启动脚本不存在：$start_path"; return 1; }

    # 自定义/前端脚本统一从 PORT 取端口，改 ports.conf 后重启即可生效。
    local custom_port
    custom_port="$(project_port "$name")"
    ( cd "$dir" && exec env "PORT=$custom_port" "SERVER_PORT=$custom_port" nohup bash "$start_path" ) </dev/null >"$log" 2>&1 &
    pid=$!
    echo "$pid" > "$(pid_file "$name")"
    disown "$pid" 2>/dev/null || true
    echo "[$name] 启动中（自定义命令 PID ${pid}），端口 $(project_port "$name")，日志：$log"

    for i in $(seq 1 90); do
      if port_listening "$name"; then
        echo "[$name] 启动成功"
        return 0
      fi
      if ! kill -0 "$pid" 2>/dev/null; then
        echo "[$name] 启动失败，日志末尾："
        tail -40 "$log" 2>/dev/null
        return 1
      fi
      sleep 1
    done

    echo "[$name] 启动超时，日志末尾："
    tail -40 "$log" 2>/dev/null
    return 1
  fi

  jar="$(project_jar "$name")"
  if [ -z "$jar" ] || [ ! -f "$jar" ]; then
    echo "[$name] 未找到可执行 jar，先构建..."
    build_one "$name" || { echo "[$name] 构建失败，未启动"; return 1; }
    jar="$(project_jar "$name")"
    [ -n "$jar" ] && [ -f "$jar" ] || { echo "[$name] 构建后仍未找到 jar"; return 1; }
  fi

  # ports.conf 的端口覆盖通过 SERVER_PORT 注入，兼容固定端口与 ${SERVER_PORT:...} 两种写法。
  local override
  override="$(port_override "$name")"
  [ -n "$override" ] && envs+=("SERVER_PORT=$override")

  # 本机联调地址覆盖：只覆盖当前已本地化的依赖，其余仍走各项目 local 配置里的远端地址。
  case "$name" in
    svc-baseinfo)
      envs+=(
        "TXLC_SVC_AUTH_URL=http://127.0.0.1:18089"
        "TXLC_AUTH_PROFILE_QUERY_URL=http://127.0.0.1:18089/auth/internal/profile/query"
      )
      ;;
    svc-auth)
      # auth 通过 Feign 直连 baseinfo/数据中心；local 配置里的旧远端地址已不可用。
      envs+=(
        "TXLC_SVC_BASEINFO_URL=http://127.0.0.1:18090"
        "TXLC_SVC_DATA_CENTER_URL=http://127.0.0.1:18080"
      )
      ;;
    svc-price)
      # 参考价查询使用本机 auth 的用户资料；避免 svc-price local 配置默认指向远端 221。
      envs+=(
        "TXLC_AUTH_PROFILE_QUERY_URL=http://127.0.0.1:18089/auth/internal/profile/query"
      )
      ;;
    txlc-gateway)
      envs+=(
        "TXLC_SVC_AUTH_URL=http://127.0.0.1:18089"
        "TXLC_SVC_BASEINFO_URL=http://127.0.0.1:18090"
        "TXLC_GATEWAY_AUTH_INTROSPECT_URL=http://127.0.0.1:18089/auth/internal/token/introspect"
      )
      ;;
  esac

  # 在项目目录内启动，避免 Spring/Logback 的相对 logs 目录落到 dev-tools。
  # 绝对 jar 路径 + nohup/disown + stdin 重定向，避免关闭终端或按钮窗口时把服务带走。
  if [ ${#envs[@]} -gt 0 ]; then
    ( cd "$dir" && exec env "${envs[@]}" nohup java -jar "$jar" --spring.profiles.active=local ) </dev/null >"$log" 2>&1 &
  else
    ( cd "$dir" && exec nohup java -jar "$jar" --spring.profiles.active=local ) </dev/null >"$log" 2>&1 &
  fi
  pid=$!
  echo "$pid" > "$(pid_file "$name")"
  disown "$pid" 2>/dev/null || true

  echo "[$name] 启动中 (PID $pid)，端口 $(project_port "$name")，日志：$log"

  # 最多等待 90 秒；进程提前退出则打印日志尾部帮助定位。
  for i in $(seq 1 90); do
    if port_listening "$name"; then
      echo "[$name] 启动成功"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[$name] 启动失败，日志末尾："
      tail -40 "$log" 2>/dev/null
      return 1
    fi
    sleep 1
  done

  echo "[$name] 启动超时，日志末尾："
  tail -40 "$log" 2>/dev/null
  return 1
}

# 停止单个服务；pid 文件失效时按端口兜底查找。
stop_one() {
  local name="$1" pid i dir stop_cmd stop_path
  dir="$(project_dir "$name")"

  # 自定义服务：projects.conf 第 6 列指定停止脚本。
  stop_cmd="$(projects_conf_field "$name" stop)"
  if [ -n "$stop_cmd" ]; then
    stop_path="$(resolve_cmd_path "$dir" "$stop_cmd")"
    if [ -f "$stop_path" ]; then
      echo "[$name] 停止中（自定义命令）"
      ( cd "$dir" && bash "$stop_path" ) || true
    else
      echo "[$name] 停止脚本不存在：$stop_path"
    fi

    # 脚本没处理干净时兜底按端口停止。
    pid="$(lsof -tiTCP:"$(project_port "$name")" -sTCP:LISTEN 2>/dev/null | head -1)"
    if [ -n "$pid" ]; then
      echo "[$name] 按端口兜底停止 (PID $pid)"
      kill "$pid" 2>/dev/null
      sleep 1
      kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$(pid_file "$name")"
    echo "[$name] 已停止"
    return 0
  fi

  if pid_alive "$name"; then
    pid="$(cat "$(pid_file "$name")")"
    echo "[$name] 停止中 (PID $pid)"
    kill "$pid" 2>/dev/null
    for i in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "$pid" 2>/dev/null
  else
    pid="$(lsof -tiTCP:"$(project_port "$name")" -sTCP:LISTEN 2>/dev/null | head -1)"
    if [ -n "$pid" ]; then
      echo "[$name] pid 文件失效，按端口停止 (PID $pid)"
      kill "$pid" 2>/dev/null
      sleep 1
      kill -9 "$pid" 2>/dev/null
    fi
  fi

  # 前端开发服务器常由 npm/pnpm 派生子进程；父进程退出后端口可能仍被占用，
  # 这里再做一次端口兜底，避免下次启动提示端口被占。
  pid="$(lsof -tiTCP:"$(project_port "$name")" -sTCP:LISTEN 2>/dev/null | head -1)"
  if [ -n "$pid" ]; then
    echo "[$name] 端口仍被占用，兜底停止 (PID $pid)"
    kill "$pid" 2>/dev/null
    sleep 1
    kill -9 "$pid" 2>/dev/null
  fi
  rm -f "$(pid_file "$name")"
  echo "[$name] 已停止"
}

# 更新：有本地改动时跳过 git pull，仅构建本地代码；构建成功后才重启。
update_one() {
  local name="$1" dir was_running=0 build_cmd build_path start_cmd
  dir="$(project_dir "$name")"
  build_cmd="$(projects_conf_field "$name" build)"
  start_cmd="$(projects_conf_field "$name" start)"

  # 记录更新前是否在运行：原本停止的服务只构建，不自动启动。
  if pid_alive "$name" || port_listening "$name"; then
    was_running=1
  fi

  if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    echo "[$name] 有未提交改动，跳过 git pull，按本地代码构建"
  else
    echo "[$name] git pull ..."
    git -C "$dir" pull --ff-only || echo "[$name] git pull 失败，按本地代码继续构建"
  fi

  if [ -n "$build_cmd" ]; then
    build_path="$(resolve_cmd_path "$dir" "$build_cmd")"
    if [ ! -f "$build_path" ]; then
      echo "[$name] 构建脚本不存在：$build_path"
      return 1
    fi
    echo "[$name] 自定义构建 ..."
    if ! ( cd "$dir" && bash "$build_path" ); then
      echo "[$name] 编译失败，继续使用旧进程"
      return 1
    fi
  elif [ -n "$start_cmd" ]; then
    echo "[$name] 未配置 build 命令，跳过构建"
  else
    if ! build_one "$name"; then
      echo "[$name] 编译失败，继续使用旧进程"
      return 1
    fi
  fi

  if [ "$was_running" = "1" ]; then
    stop_one "$name"
    start_one "$name"
  else
    echo "[$name] 更新完成；原本未运行，保持停止（需要时手动启动）"
  fi
}

# ---------------------------------------------------------------
# Git 操作
# 本地经常有还没提交的改动，所以拉取前先暂存、拉完再恢复，
# 避免“有改动就不能拉”把本地工作卡住。
# ---------------------------------------------------------------

# 项目目录是否在 git 工作区内（前端子目录也支持）。
is_git_repo() {
  local dir
  dir="$(project_dir "$1")"
  [ -n "$dir" ] && git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

# 未提交改动数（不含未跟踪文件），与更新时“有改动就跳过 pull”的判定一致。
git_dirty_count() {
  local dir
  dir="$(project_dir "$1")"
  git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' '
}

# 未解决冲突数：索引里有未合并路径时，git stash push 会直接失败
# （error: could not write index / needs merge），所以要先解冲突。
git_unmerged_count() {
  local dir
  dir="$(project_dir "$1")"
  git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null \
    | awk '$1 ~ /^(DD|AU|UD|UA|DU|AA|UU)$/' | wc -l | tr -d ' '
}

# 拉取单个项目：暂存改动 -> git pull --ff-only -> 恢复改动。
# 无论 pull 成功与否都要恢复，否则用户的改动会被留在 stash 里。
git_pull_one() {
  local name="$1" dir dirty pull_code pull_out upstream stash_err unmerged
  dir="$(project_dir "$name")"
  if ! is_git_repo "$name"; then
    echo "[$name] 不是 git 仓库，跳过"
    return 0
  fi

  # 没有上游时 rev-parse 会回显 @{u} 本身，所以要判空后替换成“无”。
  upstream="$(git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"
  [ -n "$upstream" ] || upstream="无"
  echo "[$name] 分支 $(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null) 上游 $upstream"
  dirty="$(git_dirty_count "$name")"

  # 先拦住未合并状态：此时暂存/拉取必然失败，提示比等 git 报错更清楚。
  unmerged="$(git_unmerged_count "$name")"
  if [ "$unmerged" -gt 0 ]; then
    echo "[$name] 有 $unmerged 个文件未解决冲突，暂存/拉取都会失败"
    echo "[$name] 解决方式：控制台点“代码 → 冲突”逐块选择，或手动处理后 git add"
    return 1
  fi

  if [ "$dirty" -gt 0 ]; then
    stash_label="devctl-auto $(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$name] 检测到 $dirty 处未提交改动，先暂存（${stash_label}）"
    if ! stash_err="$(git -C "$dir" stash push -m "$stash_label" 2>&1)"; then
      echo "[$name] 暂存失败，未执行拉取"
      printf '%s\n' "$stash_err" | sed 's/^/    /'
      return 1
    fi
  fi

  echo "[$name] git pull --ff-only ..."
  pull_out="$(git -C "$dir" pull --ff-only 2>&1)"
  pull_code=$?
  printf '%s\n' "$pull_out"
  if [ "$pull_code" -ne 0 ]; then
    echo "[$name] 拉取失败（常见原因：本地有未推送提交，无法快进）"
  fi

  if [ "$dirty" -gt 0 ]; then
    echo "[$name] 恢复本地改动 ..."
    if git -C "$dir" stash pop >/dev/null 2>&1; then
      echo "[$name] 本地改动已恢复"
    else
      echo "[$name] 恢复冲突：改动仍保存在 git stash（$(git -C "$dir" stash list | head -1)）"
      echo "[$name] 解决方式：控制台点“代码 → 冲突”逐块选择保留哪一侧，"
      echo "[$name]   解决并标记已解决后，再决定是否 git stash drop（$dir）"
    fi
  fi

  if [ "$pull_code" -eq 0 ]; then
    echo "[$name] 拉取完成"
  fi
  return "$pull_code"
}

# 手动暂存本地改动，供“先放着、稍后恢复”的场景使用。
git_stash_one() {
  local name="$1" dir dirty label unmerged stash_err
  dir="$(project_dir "$name")"
  if ! is_git_repo "$name"; then
    echo "[$name] 不是 git 仓库"
    return 1
  fi
  # 未合并状态下 git stash push 必然失败，先给出明确提示。
  unmerged="$(git_unmerged_count "$name")"
  if [ "$unmerged" -gt 0 ]; then
    echo "[$name] 有 $unmerged 个文件未解决冲突，无法暂存"
    echo "[$name] 解决方式：控制台点“代码 → 冲突”逐块选择，或手动处理后 git add"
    return 1
  fi
  dirty="$(git_dirty_count "$name")"
  if [ "$dirty" -eq 0 ]; then
    echo "[$name] 没有未提交改动，无需暂存"
    return 0
  fi
  label="devctl-manual $(date '+%Y-%m-%d %H:%M:%S')"
  if stash_err="$(git -C "$dir" stash push -m "$label" 2>&1)"; then
    echo "[$name] 已暂存 $dirty 处改动：$label"
  else
    echo "[$name] 暂存失败"
    printf '%s\n' "$stash_err" | sed 's/^/    /'
    return 1
  fi
}

# 获取远端分支和提交：只更新 origin/* 远程跟踪分支，不改工作区、不动本地改动。
git_fetch_one() {
  local name="$1" dir
  dir="$(project_dir "$name")"
  if ! is_git_repo "$name"; then
    echo "[$name] 不是 git 仓库"
    return 1
  fi
  echo "[$name] git fetch --all --prune ..."
  if git -C "$dir" fetch --all --prune 2>&1; then
    echo "[$name] 已获取远端最新分支和提交"
  else
    echo "[$name] 获取远端失败（检查网络或远程地址）"
    return 1
  fi
}

# 恢复最近一次暂存（仅 devctl 打的标记，避免误弹别人的 stash）。
git_unstash_one() {
  local name="$1" dir
  dir="$(project_dir "$name")"
  if ! is_git_repo "$name"; then
    echo "[$name] 不是 git 仓库"
    return 1
  fi
  if ! git -C "$dir" stash list 2>/dev/null | grep -q "devctl-"; then
    echo "[$name] 没有可恢复的 devctl 暂存"
    return 1
  fi
  echo "[$name] git stash pop ..."
  if pop_out="$(git -C "$dir" stash pop 2>&1)"; then
    printf '%s\n' "$pop_out"
    echo "[$name] 已恢复"
  else
    printf '%s\n' "$pop_out"
    echo "[$name] 恢复冲突，改动仍保留在 stash"
    # 内容已经在工作区时 git 只会说 would be overwritten，直接告诉用户下一步怎么做。
    if printf '%s' "$pop_out" | grep -qi "would be overwritten"; then
      echo "[$name] 这条暂存的内容已经在工作区里（解冲突时可能已经恢复过），不用再恢复"
      echo "[$name] 在控制台“代码 → 暂存记录”里点这条的“删除”即可"
    fi
    return 1
  fi
}

# ---------------------------------------------------------------
# 安装依赖：把项目里的 api 模块 mvn install 进本机仓库，
# 供其他项目编译时使用（api 包只进本地仓库，不上传私服）。
# ---------------------------------------------------------------

# 未配置第 9 列时自动识别：<parent> 之外 artifactId 以 -api 结尾的模块。
detect_api_modules() {
  local dir="$1" pom art rel out=""
  while IFS= read -r pom; do
    art="$(awk '
      /<parent>/ { inp = 1 }
      /<\/parent>/ { inp = 0; next }
      !inp && /<artifactId>/ {
        line = $0
        sub(/.*<artifactId>/, "", line)
        sub(/<\/artifactId>.*/, "", line)
        gsub(/^[ \t]+|[ \t]+$/, "", line)
        print line
        exit
      }
    ' "$pom")"
    case "$art" in
      *-api)
        rel="${pom#$dir/}"
        rel="${rel%/pom.xml}"
        out="${out:+$out,}$rel"
        ;;
    esac
  done < <(find "$dir" -maxdepth 4 -name pom.xml -not -path '*/target/*' 2>/dev/null | sort)
  printf '%s' "$out"
}

install_modules_of() {
  local name="$1" spec dir
  spec="$(projects_conf_field "$name" install)"
  if [ -n "$spec" ]; then
    printf '%s' "$spec"
    return 0
  fi
  dir="$(project_dir "$name")"
  [ -f "$dir/pom.xml" ] || return 0
  detect_api_modules "$dir"
}

# 只 install 不重启：安装的是给别的项目复用的 api 包，服务本身不受影响。
install_one() {
  local name="$1" dir modules pm
  dir="$(project_dir "$name")"
  modules="$(install_modules_of "$name")"

  # 前端项目用 @npm/@pnpm/@yarn/@bun 标记，执行对应包管理器的 install。
  case "$modules" in
    @npm|@pnpm|@yarn|@bun)
      pm="${modules#@}"
      if ! command -v "$pm" >/dev/null 2>&1; then
        echo "[$name] 未找到 $pm 命令"
        return 1
      fi
      echo "[$name] $pm install ..."
      ( cd "$dir" && "$pm" install )
      return $?
      ;;
  esac

  if [ ! -f "$dir/pom.xml" ]; then
    echo "[$name] 不是 Maven 项目，跳过"
    return 0
  fi
  if [ -z "$modules" ]; then
    echo "[$name] 未找到 api 模块，跳过"
    return 0
  fi
  echo "[$name] mvn install 模块：$modules"
  # -am 连父 pom 一起装，否则单独 install 子模块会找不到 parent。
  ( cd "$dir" && run_mvn -B -Dmaven.test.skip=true -pl "$modules" -am install )
}

# 状态输出，包含热更新监听状态。
status_one() {
  local name="$1" pid watch_pid
  if pid_alive "$name"; then
    pid="$(cat "$(pid_file "$name")")"
    echo "[$name] 运行中 PID=$pid 端口=$(project_port "$name") 日志=$(log_file "$name")"
  elif port_listening "$name"; then
    echo "[$name] 端口 $(project_port "$name") 监听中（pid 文件未记录）"
  else
    echo "[$name] 未运行"
  fi

  watch_pid="$(watch_pid_value "$name")"
  [ -n "$watch_pid" ] && echo "  热更新监听中 PID=$watch_pid"
}

# 热更新 pid 文件：单项目用 /tmp/<项目>.watch.pid，全部用 /tmp/dev-watch-all.pid。
watch_pid_path() {
  if [ "$1" = "all" ]; then echo "/tmp/dev-watch-all.pid"; else echo "/tmp/$1.watch.pid"; fi
}

# 读取仍存活的监听进程 PID，顺带清理失效文件。
watch_pid_value() {
  local file pid
  file="$(watch_pid_path "$1")"
  [ -f "$file" ] || return 0
  pid="$(cat "$file" 2>/dev/null)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$pid"
  else
    rm -f "$file"
  fi
}

# 热更新：监听 java/xml/yml/pom 变更，自动 mvn package 并重启对应服务。
# 这是“自动重编译 + 重启”，不是 JVM 级热替换；构建失败会保留旧进程。
watch_projects() {
  local target="$1"; shift
  local names="$*" pidfile stamp changed
  local -a dirs=()
  local name dir build_cmd build_path build_ok

  pidfile="$(watch_pid_path "$target")"
  if [ -n "$(watch_pid_value "$target")" ]; then
    echo "[$target] 热更新已在运行 (PID $(watch_pid_value "$target"))"
    return 1
  fi

  for name in $names; do
    dirs+=("$(project_dir "$name")")
  done

  stamp="/tmp/dev-watch-$target.stamp"
  touch "$stamp"
  echo $$ > "$pidfile"
  # TERM/INT 通过 exit 触发 EXIT，确保 pid 文件被清理；不能只 trap 不退出，否则 kill 无效。
  trap 'rm -f "$pidfile"' EXIT
  trap 'exit 0' INT TERM

  echo "[$target] 热更新监听中，源码变化后自动构建并重启；Ctrl+C 退出"
  echo "监听项目：$names"

  while true; do
    changed="$(find "${dirs[@]}" -type f \( -name '*.java' -o -name '*.xml' -o -name '*.yml' -o -name '*.yaml' -o -name '*.properties' -o -name 'pom.xml' \) ! -path '*/target/*' ! -path '*/.git/*' -newer "$stamp" -print 2>/dev/null)"
    if [ -n "$changed" ]; then
      touch "$stamp"
      sleep 2

      for name in $names; do
        dir="$(project_dir "$name")"
        if printf '%s\n' "$changed" | grep -q "^$dir/"; then
          echo "[$name] 检测到变更，构建中..."
          build_cmd="$(projects_conf_field "$name" build)"
          build_ok=0
          if [ -n "$build_cmd" ]; then
            build_path="$(resolve_cmd_path "$dir" "$build_cmd")"
            if [ -f "$build_path" ]; then
              ( cd "$dir" && bash "$build_path" ) && build_ok=1
            fi
          else
            ( cd "$dir" && run_mvn -B -q -Dmaven.test.skip=true package ) && build_ok=1
          fi

          if [ "$build_ok" = "1" ]; then
            echo "[$name] 构建成功，重启..."
            stop_one "$name"
            start_one "$name" || true
          else
            echo "[$name] 构建失败，保留当前进程"
          fi
        fi
      done
    fi
    sleep 2
  done
}

# 停止热更新监听进程。
unwatch_one() {
  local target="$1" file pid
  file="$(watch_pid_path "$target")"
  if [ -f "$file" ]; then
    pid="$(cat "$file" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "[$target] 停止热更新 (PID $pid)"
      kill "$pid" 2>/dev/null
      sleep 1
      kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$file"
  else
    echo "[$target] 未在监听"
  fi
}

# 按依赖顺序输出启动顺序：核心项目（gateway 除外）-> 其他项目 -> gateway。
# 如果目标列表里没有 gateway，则不会额外添加。
order_for_start() {
  local targets="$*" n core
  core="$(core_names)"

  for n in $core; do
    [ "$n" = "txlc-gateway" ] && continue
    case " $targets " in *" $n "*) echo "$n" ;; esac
  done

  for n in $targets; do
    [ "$n" = "txlc-gateway" ] && continue
    case " $core " in *" $n "*) ;; *) echo "$n" ;; esac
  done

  case " $targets " in *" txlc-gateway "*) echo "txlc-gateway" ;; esac
}

# 反转启动顺序得到停止顺序。
reverse_lines() {
  awk '{ a[NR] = $0 } END { for (i = NR; i >= 1; i--) print a[i] }'
}

# 判断文件存在且修改时间在指定秒数内，避免残留标记永久卡住 UI。
recent_file() {
  local f="$1" max="$2" now mtime
  [ -f "$f" ] || return 1
  now="$(date +%s)"
  mtime="$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)"
  [ -n "$mtime" ] || return 0
  [ $((now - mtime)) -lt "$max" ]
}

# 返回文件修改时间（epoch 秒）；不存在或无法读取时返回 0。
file_mtime() {
  local f="$1" mtime
  [ -f "$f" ] || { echo 0; return; }
  mtime="$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)"
  echo "${mtime:-0}"
}

# 输出 JSON 状态，供 Web 控制台调用。
# 把逗号分隔的名称列表转成 JSON 数组字符串，空值输出 []。
# json_status 用它输出 depends/requires，避免前端再拆字符串。
json_list() {
  awk -v s="$1" 'BEGIN{
    n = split(s, a, ",");
    printf "[";
    c = 0;
    for (i = 1; i <= n; i++) {
      item = a[i];
      gsub(/^[ \t]+|[ \t]+$/, "", item);
      if (item == "") continue;
      if (c++) printf ",";
      printf "\"%s\"", item;
    }
    printf "]";
  }'
}

json_status() {
  local first=1 name pid running watch core port doc install_modules project_kind
  local action action_running action_exit action_exit_time start_script stop_script build_script
  local group depends_json requires_json
  # 默认入口/文档地址用的主机名：默认 localhost，与控制台地址栏保持一致，
  # 纯 127.0.0.1 会被部分后端按 Host 判成非法来源。可用 TXLC_LOCAL_HOST 覆盖。
  local local_host="${TXLC_LOCAL_HOST:-localhost}"
  load_listening_ports
  printf '['
  for name in $(all_names); do
    [ "$first" -eq 1 ] || printf ','
    first=0
    port="$(project_port "$name")"
    project_kind="$(project_type "$name")"
    doc="$(projects_conf_field "$name" doc)"
    if [ -z "$doc" ]; then
      # 前端项目默认入口是首页；后端默认是 knife4j 文档页。
      if [ "$project_kind" = "frontend" ]; then
        doc="http://$local_host:$port/"
      else
        doc="http://$local_host:$port/doc.html"
      fi
    fi
    # 只读配置列，避免每次状态轮询都去扫描 pom（扫描留给 install 动作）。
    install_modules="$(projects_conf_field "$name" install)"
    # 自定义脚本路径也返回给“项目设置”弹窗，方便直接修改。
    start_script="$(projects_conf_field "$name" start)"
    stop_script="$(projects_conf_field "$name" stop)"
    build_script="$(projects_conf_field "$name" build)"
    # 分组与依赖顺序：老配置没有第 11–13 列时为空，控制台按类型/核心标记推导。
    group="$(projects_conf_field "$name" group)"
    depends_json="$(json_list "$(projects_conf_field "$name" depends)")"
    requires_json="$(json_list "$(projects_conf_field "$name" requires)")"

    if pid_alive "$name"; then
      running=true
      pid="$(cat "$(pid_file "$name")")"
    elif port_in_cache "$port"; then
      running=true
      pid=""
    else
      running=false
      pid=""
    fi

    if [ -n "$(watch_pid_value "$name")" ]; then watch=true; else watch=false; fi
    if is_core "$name"; then core=true; else core=false; fi

    # Web 控制台的异步启动/停止标记：包装进程运行期间写入，结束后删除并写退出码。
    action=""
    action_running=false
    action_exit=""
    action_exit_time=0
    if recent_file "/tmp/$name.action.running" 600; then
      action="$(cat "/tmp/$name.action.running" 2>/dev/null)"
      action_running=true
    fi
    if recent_file "/tmp/$name.action.exit" 600; then
      action_exit="$(cat "/tmp/$name.action.exit" 2>/dev/null)"
      action_exit_time="$(file_mtime "/tmp/$name.action.exit")"
    fi

    printf '{"name":"%s","port":%s,"running":%s,"pid":"%s","watch":%s,"core":%s,"log":"%s","dir":"%s","doc":"%s","action":"%s","action_running":%s,"action_exit":"%s","action_exit_time":%s,"install":"%s","type":"%s","start":"%s","stop":"%s","build":"%s","group":"%s","depends":%s,"requires":%s}' \
      "$name" "$port" "$running" "$pid" "$watch" "$core" \
      "$(log_file "$name")" "$(project_dir "$name")" "$doc" \
      "$action" "$action_running" "$action_exit" "$action_exit_time" "$install_modules" "$project_kind" \
      "$start_script" "$stop_script" "$build_script" "$group" "$depends_json" "$requires_json"
  done
  printf ']\n'
}

# 交互式控制面板，供 macOS .app 双击后调用。
panel() {
  local op project
  while true; do
    # 非交互环境（如脚本测试）没有 TERM，避免 clear 报错。
    [ -t 1 ] && clear
    echo "=============================================="
    echo " TXLC Java 服务控制面板"
    echo "=============================================="
    "$0" status all
    echo
    echo "项目列表："
    "$0" list
    echo
    echo "1) 启动项目        2) 停止项目"
    echo "3) 重启项目        4) 更新项目"
    echo "5) 热更新项目      6) 停止热更新"
    echo "7) 启动核心项目    8) 停止全部"
    echo "0) 退出"
    echo
    read -r -p "选择操作: " op || exit 0

    case "$op" in
      0) exit 0 ;;
      7) "$0" start core ;;
      8) "$0" stop all ;;
      1|2|3|4|5|6)
        read -r -p "输入项目名（全名或简称，回车=all）: " project || exit 0
        [ -z "$project" ] && project=all
        case "$op" in
          1) "$0" start "$project" ;;
          2) "$0" stop "$project" ;;
          3) "$0" restart "$project" ;;
          4) "$0" update "$project" ;;
          5) "$0" watch "$project" ;;
          6) "$0" unwatch "$project" ;;
        esac
        ;;
      *) echo "无效选择"; sleep 1; continue ;;
    esac

    echo
    read -r -p "回车返回菜单..." _ || exit 0
  done
}

main() {
  local action="${1:-status}" target="${2:-all}" names name rc

  case "$action" in
    json)
      json_status
      ;;
    list)
      for name in $(all_names); do
        echo "$name  port=$(project_port "$name")  dir=$(project_dir "$name")"
      done
      ;;
    status)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      for name in $names; do status_one "$name"; done
      ;;
    start)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      for name in $(order_for_start $names); do start_one "$name" || exit 1; done
      ;;
    stop)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      for name in $(order_for_start $names | reverse_lines); do stop_one "$name"; done
      ;;
    restart)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      for name in $(order_for_start $names | reverse_lines); do stop_one "$name"; done
      for name in $(order_for_start $names); do start_one "$name" || exit 1; done
      ;;
    update)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      for name in $(order_for_start $names); do update_one "$name" || true; done
      ;;
    gitpull)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      rc=0
      for name in $names; do
        echo
        git_pull_one "$name" || rc=1
      done
      exit "$rc"
      ;;
    gitfetch)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      rc=0
      for name in $names; do
        echo
        git_fetch_one "$name" || rc=1
      done
      exit "$rc"
      ;;
    gitstash)
      name="$(resolve_name "$target")" || { echo "未知项目：$target"; exit 1; }
      git_stash_one "$name"
      ;;
    gitunstash)
      name="$(resolve_name "$target")" || { echo "未知项目：$target"; exit 1; }
      git_unstash_one "$name"
      ;;
    install)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      rc=0
      for name in $names; do
        echo
        install_one "$name" || rc=1
      done
      exit "$rc"
      ;;
    watch)
      names="$(resolve_targets "$target")" || { echo "未知项目：$target"; exit 1; }
      # 别名统一落成项目全名，保证 watch/unwatch 使用同一个 pid 文件。
      if [ "$target" = "all" ] || [ "$target" = "core" ]; then
        names="$(resolve_targets "$target")"
        watch_projects "$target" $names
      else
        name="$(resolve_name "$target")"
        watch_projects "$name" $name
      fi
      ;;
    unwatch)
      if [ "$target" = "all" ]; then
        for name in $(all_names); do unwatch_one "$name"; done
        unwatch_one all
      else
        name="$(resolve_name "$target")" || { echo "未知项目：$target"; exit 1; }
        unwatch_one "$name"
      fi
      ;;
    panel)
      panel
      ;;
    *)
      echo "用法：$0 <list|status|start|stop|restart|update|install|gitpull|gitfetch|gitstash|gitunstash|watch|unwatch|panel> [项目|all|core]"
      exit 1
      ;;
  esac
}

main "$@"
