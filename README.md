# TXLC 本地服务控制台（dev-tools）

管理本机多个 Java 服务 / 前端项目 / 传统门户的**启停、构建、热更新、日志、Git、端口与健康巡检**。

**两个入口，同一份配置**：双击 `服务控制台.app` 得到原生窗口（日常管理用）；浏览器打开 `http://localhost:18000` 得到网页版（多屏、远程调试用）。两边读写同一份 `projects.conf` / `ports.conf` / `deps.conf`，配置在哪个入口做都一样。

> 当前状态：macOS 上日常在用，管理 17 个注册项目。S1（健康检查 + 自动重启、日志增强、端口面板）、S2（外部依赖检查、分组与依赖顺序、批量操作与搜索）、S2.5 原生窗口外壳、S2.6 诊断日志、S3（Python 核心 + Windows 执行路径）、S3.5（初始化与环境自检）已完成。详见 [ROADMAP.md](./ROADMAP.md)。
>
> 后续计划用 Go + Vue 3 重写为 `txlc-console`，本仓库是那次重写的**可运行规格书**——行为契约见 [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)，换机器安装见 [docs/INSTALL.md](./docs/INSTALL.md)。

---

## 快速开始

### 新机器：从 clone 到启动

```bash
git clone https://github.com/CherrySUKURA/dev-tools.git dev-tools && cd dev-tools

./install.sh                  # 复制 *.conf.example、建 services/logs/tmp、检查环境
vim projects.conf             # 把示例路径改成本机路径（至少改「名字|目录」两列）
./devctl.py doctor            # 环境 + 配置自检；有 FAIL 先按提示修复
./devctl.sh start core        # 启动核心项目（首次缺 jar 会自动构建，耗时较长）
python3 dashboard.py          # 起控制台，浏览器打开 http://127.0.0.1:18000
# 可选：./app/build.sh 生成 服务控制台.app（需要 Xcode 命令行工具）
```

完整步骤（依赖安装、Windows、从旧电脑迁移配置、验收清单、常见问题）见 **[docs/INSTALL.md](./docs/INSTALL.md)**。

不知道 `projects.conf` 怎么写时，可以先生成候选：

```bash
./install.sh --scan ~/java    # 扫描 Maven 工作区，项目名/路径/核心标记仍需人工确认
```

已有 `projects.conf` 时，扫描结果写到 `projects.conf.candidate`，不会覆盖现有配置。

`doctor` 是**只读检查**：不启动服务、不改文件；`--env` 只查 Python/Java/Maven/Git/Node。检查内容包括配置目录是否存在、端口是否重复或已被占用、启动脚本与 jar 是否就位、`requires` 是否在 `deps.conf` 登记。退出码 0 表示没有 FAIL，可直接接 CI 或 Git 钩子。

Windows 上把 `./install.sh` 换成 `install.cmd`、`./devctl.sh` 换成 `devctl.cmd`；`./devctl.py doctor` 相同。用 PyInstaller 打包后没有独立 Python 时，自检走 `TXLCConsole.exe --devctl doctor`。

### 已经配置好之后：日常启动

```bash
cd ~/java/dev-tools
python3 dashboard.py          # 启动服务（只用标准库）
# 入口一：双击 服务控制台.app —— 原生窗口，不需要浏览器
# 入口二：浏览器打开 http://localhost:18000 —— 网页版
```

Windows 上等价入口是双击 `app\windows\launch.cmd`：后台起服务，再用 Edge/Chrome 的应用窗口打开；命令行可用 `devctl.cmd status all` / `devctl.cmd start core`。

`服务控制台.app` 会先确保服务在跑，再打开原生窗口；直接跑 `dashboard.py` 时只提供网页入口。

首次从源码构建应用（需要 Xcode Command Line Tools，提供 `swiftc`）：

```bash
./app/build.sh                       # 生成 ./服务控制台.app
./app/build.sh /Applications/服务控制台.app   # 或装到别处
```

没有 `swiftc` 时应用会退化成「启动服务 + 默认浏览器打开」，功能不受影响。

只监听 `127.0.0.1`，不对外暴露。端口可用 `TXLC_DASHBOARD_PORT` 覆盖。

运行前提（控制台本身不安装这些，被管理的项目需要）：

| 依赖 | 用途 | 说明 |
|---|---|---|
| Python 3.8+ | 控制台后端 + `devctl.py` | 只用标准库，无第三方包；`install.sh` 与 `doctor` 会检查版本 |
| JDK 17 | Java 服务 | 脚本按 `JAVA_HOME` 或常见安装位置查找 |
| Maven | 构建 | 找不到时自动尝试 `~/dev-tools/apache-maven-*` |
| Node + 包管理器 | 前端项目 | 按项目 install 列决定 npm/pnpm/yarn/bun |

### 应用入口与网页入口

| 入口 | 打开方式 | 适合什么 |
|---|---|---|
| App | 双击 `服务控制台.app` | 日常启停、日志、排障。独立窗口、Dock 图标、原生菜单，没有浏览器地址栏和标签页 |
| 网页 | 浏览器打开 `http://localhost:18000` | 多屏、临时调试、把页面贴到另一台设备上操作 |

两者共用同一个后端、同一份配置文件，配置（添加项目、端口、依赖、Git 设置）在哪个入口做都一样。

App 菜单除了刷新，还提供：**在浏览器中打开（网页配置入口）**、**打开配置文件**（`projects.conf`）、**打开配置目录**、**打开控制台日志**。页面里的「页面入口 / 接口文档 / 日志链接」用 `target=_blank`，一律交给系统浏览器打开，不在 App 里再开没有地址栏的子窗口。

---

## 主要功能

**启停与编排**

- 单项目启动 / 停止 / 重启 / 更新 / 热更新。
- 「启动核心项目」按 `projects.conf` 第 12 列的项目依赖做拓扑排序，**前一个端口就绪才启动下一个**；「停止全部」按反序执行。
- 项目按分组展示（`core` / `business` / `portal` / `frontend`，可自定义），每组可一键「启动组 / 停止组」。
- 卡片左侧勾选后底部出现批量条：批量启动 / 停止 / 重启 / 更新；多项目启停同样按依赖顺序串行。
- 顶部搜索框支持名称 / 端口 / 分组 / 目录，`⌘K`（Windows 为 `Ctrl+K`）聚焦，`Esc` 清空。

**外部依赖闸门**

- `deps.conf` 登记 MySQL / Redis / Nacos 等远程依赖，控制台每 15 秒探活一次，顶栏「依赖」按钮显示 `可用/总数`，有不通的会变红。
- 启动项目前检查 `requires` 列：依赖不通直接返回 409 并说明原因，前端二次确认后可强制启动。
- 批量 / 整组启动时，被依赖拦住的项目**跳过并记录原因**，其余项目继续执行，不会被一个坏依赖卡住整组。
- 依赖面板列出状态、地址、协议、延迟，以及「被哪些项目依赖」。

**日常排障**

- 健康巡检 + 崩溃自动重启（启动宽限期、失败阈值、重启冷却、预算上限）。
- 日志弹窗：关键字过滤与高亮、级别过滤、只看错误、导出当前视图。
- 诊断日志：把控制台自身、任务、项目日志、健康巡检、应用日志汇总到一处，看尾部、单文件下载、导出诊断包；与项目日志共用行数、过滤与高亮。
- 端口面板：端口 → 进程 → 项目，一键释放（只允许当前真实监听的端口）。
- Git 面板：分支、改动、提交历史、拉取、stash、冲突解决、diff。

---

## 文件说明

| 文件 / 目录 | 作用 |
|---|---|
| `dashboard.py` | HTTP 服务、任务队列、状态缓存、Git 面板、健康巡检、依赖探活、编排执行（约 3,300 行） |
| `dashboard.html` | 单页前端，内联 CSS 与 JS（约 4,000 行） |
| `devctl.py` | 进程与构建的实际执行者：启动/停止/重启/更新/热更新/状态 JSON |
| `devctl.sh` / `devctl.cmd` | 兼容入口，只负责找 Python 并转发参数（macOS/Linux / Windows） |
| `install.sh` / `install.cmd` | 首次初始化：复制配置模板、建运行目录、检查环境；`--scan` 生成项目候选 |
| `doctor.py` | 环境与配置自检（只读）：打印 OK/WARN/FAIL，有 FAIL 时退出码非 0 |
| `discover.py` | 扫描 Maven 工作区，输出 `projects.conf` 候选行（启发式，需人工确认） |
| `console_platform.py` | 平台适配层：端口列表、进程树、终止、通知、目录选择、应用窗口、状态目录 |
| `app/` | macOS 原生窗口外壳：`shell.swift`（WKWebView + 菜单）、`launcher.sh`、`build.sh`；`app/windows/` 提供 Windows 启动器与 PyInstaller 打包脚本 |
| `logs/` | 控制台自身日志 `console.log`（运行时生成，已 gitignore） |
| `tests/test_diagnostics.py` | 诊断清单、id 白名单、512KB 截断、诊断包不含配置 |
| `tests/` | 标准库 `unittest` 用例（健康检查、探活、端口释放、配置写入、依赖闸门、分组、拓扑排序） |
| `projects.conf` | 项目注册表（13 列，见下） |
| `projects.conf.candidate` | `install.sh --scan` 在已有配置时的候选结果，确认后再替换（已 gitignore） |
| `ports.conf` | 端口覆盖表：项目实际监听端口与 `projects.conf` 不同时用 |
| `entries.conf` | 卡片背面的页面入口按钮 |
| `health.conf` | 健康检查配置（首次保存时生成，未出现的项目用默认值） |
| `deps.conf` | 外部依赖登记表（MySQL / Redis 等远程依赖，用于启动前探活） |
| `*.conf.example` | 各配置的模板。**真实配置不入库**（含本机绝对路径与内网地址），首次使用先复制再改 |
| `services/` | 为 WAR / 前端项目生成的启动、停止、构建脚本样例 |
| `maven-settings.xml` | 允许从内网 HTTP Maven 仓库拉包 |
| `legacy-proxy.conf` | 旧门户入口代理使用的 Host 头（真实域名不入库，默认 `localhost`） |
| `ROADMAP.md` | 功能路线图 P0–P3、阶段 S1–S4 与进度 |
| `docs/INSTALL.md` | 新机器安装指南：依赖、macOS/Linux/Windows 步骤、旧机迁移、验收与常见问题 |
| `docs/ARCHITECTURE.md` | 分层、数据流、行为契约、重构边界 |

---

## 配置格式

所有配置都是**竖线分隔的纯文本**，`#` 开头为注释，忽略空行。字段只追加在末尾，保证老文件仍可读。

### `projects.conf`（项目注册表）

```
项目名|目录|核心(1/0)|jar相对路径|start脚本|stop脚本|build脚本|文档地址|install模块|类型|分组|依赖项目|必需外部依赖
```

| # | 字段 | 说明 |
|---|---|---|
| 1 | 项目名 | 唯一标识，也是接口里的 `target` |
| 2 | 目录 | 项目绝对路径 |
| 3 | 核心 | `1` 时「启动核心项目」会带上它 |
| 4 | jar | 留空则自动找 `*-server/target/*.jar` 或 `target/*.jar` |
| 5–7 | start / stop / build | 留空则用默认逻辑；WAR / 前端项目填 `services/*.sh` |
| 8 | 文档地址 | 留空时后端默认 `:port/doc.html`，前端默认首页 |
| 9 | install 模块 | Maven 模块或 `@npm/@pnpm/@yarn/@bun` |
| 10 | 类型 | `backend`（默认）/ `frontend` |
| 11 | 分组 | 如 `core` / `business` / `portal` / `frontend`，界面按组展示、按组启停 |
| 12 | 依赖项目 | 逗号分隔的项目名；启动时先起依赖并等其就绪，停止时反序 |
| 13 | 必需外部依赖 | 逗号分隔的 `deps.conf` 名称；不通时拒绝启动（可强制） |

### `ports.conf`

```
# 项目名 端口（也兼容「项目名|端口」写法）
svc-order 18083
```

### `entries.conf`

```
# 项目名|按钮名称|入口地址|说明（可省略）
txlcsystem|门户首页|/efarm/entry/index|子平台对外门户首页
```

### `health.conf`

```
# 项目名|方式(tcp/http/off)|目标(端口或URL)|间隔秒|超时秒|连续失败次数|自动重启(1/0)|最大重启次数
svc-order|tcp|18083|10|2|3|1|5
```

未登记的项目默认：TCP 探活自身端口、间隔 10 秒、连续 3 次失败判异常、自动重启最多 5 次。

### `deps.conf`

```
# 名称|类型(tcp/http)|地址|端口|说明
mysql-test|tcp|192.0.2.10|3306|测试库（MySQL）
```

### `legacy-proxy.conf`

```
# 旧系统入口（/efarm/**）代理时使用的 Host 头，一行即可
portal.example.internal
```

不配置时用 `localhost`，只影响旧门户的域名入口校验；也可用 `TXLC_LEGACY_PROXY_HOST` 环境变量覆盖。

---

## HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 单页控制台 |
| GET | `/api/status` | 项目状态、健康、外部依赖、编排进度、当前任务（前端 1 秒轮询） |
| GET | `/api/deps` | 外部依赖探活结果；`?refresh=1` 立即重新探活 |
| GET | `/api/log` | 服务日志 / 操作日志，支持 `lines`、`kind`、`source` |
| GET | `/api/ports` | 本机监听端口 → 进程 → 项目 |
| GET | `/api/health` | 全部项目的探活配置与状态 |
| GET | `/api/diagnostics` | 诊断日志清单（控制台 / 任务 / 项目输出 / 健康巡检 / 应用日志） |
| GET | `/api/diagnostics/tail` | 按诊断 id 读取某个日志的末尾内容，支持 `lines` |
| GET | `/api/diagnostics/file` | 按诊断 id 下载单个日志（末尾 2MB） |
| GET | `/api/jobs`、`/api/job` | 任务列表与单个任务输出 |
| GET | `/api/git`、`/api/git/conflicts` | 分支/改动/提交历史、冲突列表 |
| GET | `/api/project/inspect`、`/api/project/pick-dir` | 目录检测、目录选择 |
| POST | `/api/action` | 启停/重启/更新/热更新/安装/Git 动作 |
| POST | `/api/ports/kill` | 释放端口（仅允许当前真实监听的端口） |
| POST | `/api/health` | 保存单个项目的探活配置 |
| POST | `/api/project/add`、`/api/project/update`、`/api/project/port` | 增改项目与端口 |
| POST | `/api/git/commit`、`/resolve`、`/stash-apply`、`/stash-drop`、`/open-diff` | Git 面板动作 |
| POST | `/api/job/stop`、`/api/shutdown` | 停止任务、关闭控制台 |
| POST | `/api/diagnostics/export` | 导出诊断包 zip（日志末尾各 512KB，**不含配置文件**） |

`POST /api/action` 的请求体：

```json
{ "action": "start-bg", "target": "group:core" }
{ "action": "start-bg", "names": ["svc-auth", "svc-order"], "force": true }
```

- `action` 取 `start-bg` / `stop-bg` / `restart-bg` / `update` / `watch` / `unwatch` 等；
- `target` 可为 `all` / `core` / `group:<分组>` / 项目名，`names` 为勾选的项目名数组（二选一）；
- 多个项目时按依赖顺序串行执行，`/api/status` 的 `ordered` 字段返回进度；
- 单项目启动且外部依赖不通时返回 `409`，带 `can_force: true` 与 `down_deps`，加 `force: true` 重试即可跳过闸门。

---

## 日志与排障

出问题先看**诊断日志**：顶栏右上角的文件图标（App 里同样有），左边是日志清单，右边是尾部内容，右上角可导出诊断包。

日志分四类，位置各不相同：

| 类别 | 位置 | 什么时候看 |
|---|---|---|
| 控制台自身 | `logs/console.log`（2MB × 3 轮转） | 页面打不开、接口 500、控制台自己崩了。启动/退出、每个 HTTP 请求、异常堆栈都在这 |
| 任务 | 系统临时目录 `txlc-dashboard/jobs/<id>.log` | 构建/更新/Git 操作为什么失败；`<id>.json` 是动作与目标 |
| 项目输出 | 系统临时目录 `<项目>.log`、`<项目>.action.log` | 服务起不来、启动即退出；`action` 是控制台执行启停命令的输出 |
| 应用日志 | Spring Boot 的 `~/logs/<项目>/app.log`、`error.log` | 服务起来了但接口报错、业务异常 |
| 健康巡检 | 系统临时目录 `<项目>.health.log` | 服务被自动重启过几次、什么时候挂的 |

诊断面板会把上面这些汇总成一份清单（只列真实存在的文件，按控制台 → 任务 → 项目排序），点开即看末尾 1200 行，支持单文件下载。

**和项目卡片的「日志」是什么关系**：

- 诊断面板是**跨项目索引 + 控制台/任务/健康日志的查看器**，回答「该看哪个文件」；
- 卡片上的「日志」是**单项目深查**（切来源、过滤、导出）的入口；
- 两边读同一个文件、同样 1200 行、同一套过滤与高亮，所以内容一致；诊断面板里能对应到项目来源的条目（启动/操作/应用/错误日志）带「在项目日志中打开」，一键跳过去继续用完整功能。
- 只读轮询（`/api/status`、日志尾巴刷新等）成功时不写访问日志，`console.log` 只留启动、动作、报错和导出这类真正有用的事件。

**导出诊断包**：`POST /api/diagnostics/export`，zip 内含 `meta.txt`（时间、目录、平台）与每个日志的末尾 512KB。**故意不打包 `*.conf`**——里面可能有内网地址和本机绝对路径，需要给别人时请自行确认后再单独发。

命令行查同一份日志：

```bash
tail -f logs/console.log                      # 控制台自身
tail -f /tmp/txlc-dashboard.log               # 启动器 stdout（Windows 为 %TEMP%\txlc-dashboard.log）
tail -f /tmp/svc-price.log                    # 某个服务的输出
```

> `logs/` 已加入 `.gitignore`；日志不进仓库，也不会进诊断包里的配置项。

---

## 运行时文件放在哪

| 路径 | 内容 |
|---|---|
| `logs/console.log`（+ `.1`~`.3` 轮转） | 控制台自身日志，诊断面板的主要来源 |
| `/tmp/txlc-dashboard/jobs/` | 任务日志 `<job>.log` 与元数据 `<job>.json`（保留最近 20 个） |
| `/tmp/<项目>.action.running`、`.action.exit` | 异步启停的进度标记与退出码 |
| `/tmp/<项目>.action.log` | 启停操作的输出 |
| `/tmp/<项目>.log` | 服务自身输出（由 `devctl.py` 重定向） |
| `<系统临时目录>/<项目>.health.log` | 自动重启记录 |

macOS / Linux 的状态目录是 `/tmp`（保持历史 pid/日志路径不变）；Windows 没有 `/tmp`，统一放在 `%TEMP%\txlc`。判断逻辑在 `console_platform.state_dir()`，控制台与 `devctl.py` 共用。

> 这些路径仍是临时目录（重启机器即丢失），后续可改为项目自己的 `state/` 目录并落库。

---

## 已知限制

- **Windows 未在真机验证**：执行路径、`devctl.cmd`、`%TEMP%\txlc` 状态目录、`.cmd` 前端脚本和 Edge/Chrome `--app` 窗口都已实现，但当前开发机是 macOS；`app/windows/build-exe.cmd` 的 PyInstaller 打包同样待 Windows 实测。
- **前端是单文件**：CSS/JS 内联在 `dashboard.html`，无法单独测试、改动冲突概率高。
- **无持久化**：任务历史与健康状态都在临时目录，控制台重启即丢。
- **测试覆盖有限**：99 个用例覆盖健康检查、端口逻辑、依赖探活与闸门、分组展开、拓扑排序、诊断日志白名单与打包、devctl 配置/端口/jar/JSON、假项目启停、分平台前端脚本，以及 `doctor` 的配置/环境/依赖检查；Git 解析、前端交互逻辑尚无测试。
- **版本号是文件 mtime**：`/api/status` 的 `version` 用于前端自动刷新，不是语义化版本。
- **依赖探活只到端口**：`tcp` 只判断端口可连，不代表库/账号/权限可用；`http` 只发一次 GET。
- **编排进度只在内存**：控制台重启后 `ordered` 丢失；项目自身状态仍靠临时目录与端口推断。

---

## 给重构者（人类或 AI）

重写目标与技术选型见 `txlc-console` 仓库的 `docs/PLAN.md`。本仓库的价值是**用可运行的方式定义行为**，动手前请先读 [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)，其中列明了：

- 哪些是**必须保持的行为契约**（接口路径、JSON 字段、配置列位置、启动等待语义）；
- 哪些是**可以推倒重来的实现细节**（临时目录、bash wrapper、单文件前端、mtime 版本号）；
- 一次「启动」从点击到端口就绪的完整链路，以及并发与状态缓存模型；
- 可用的验收方式：`python3 -m unittest discover -s tests` + 手工回归清单。

---

## 测试

```bash
python3 -m unittest discover -s tests -v
```

首次换机器时先跑 `./install.sh && ./devctl.py doctor`；测试不依赖真实配置与真实服务。
