# TXLC 本地服务控制台（dev-tools）

在浏览器里管理本机多个 Java 服务 / 前端项目 / 传统门户的**启停、构建、热更新、日志、Git、端口与健康巡检**。

> 当前状态：macOS 上日常在用，管理 17 个注册项目。S1（健康检查 + 自动重启、日志增强、端口面板）与 S2（外部依赖检查、分组与依赖顺序、批量操作与搜索）已完成。详见 [ROADMAP.md](./ROADMAP.md)。
>
> 后续计划用 Go + Vue 3 重写为 `txlc-console`，本仓库是那次重写的**可运行规格书**——行为契约见 [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)。

---

## 快速开始

```bash
cd ~/java/dev-tools
python3 dashboard.py          # 或双击「服务控制台.app」
# 浏览器打开 http://localhost:18000
```

只监听 `127.0.0.1`，不对外暴露。端口可用 `TXLC_DASHBOARD_PORT` 覆盖。

运行前提（控制台本身不安装这些，被管理的项目需要）：

| 依赖 | 用途 | 说明 |
|---|---|---|
| Python 3 | 控制台后端 | 只用标准库，无第三方包 |
| bash | `devctl.sh` 核心 | macOS 自带 bash 3.2 即可 |
| JDK 17 | Java 服务 | 脚本按 `JAVA_HOME` 或常见安装位置查找 |
| Maven | 构建 | 找不到时自动尝试 `~/dev-tools/apache-maven-*` |
| Node + 包管理器 | 前端项目 | 按项目 install 列决定 npm/pnpm/yarn/bun |

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
- 端口面板：端口 → 进程 → 项目，一键释放（只允许当前真实监听的端口）。
- Git 面板：分支、改动、提交历史、拉取、stash、冲突解决、diff。

---

## 文件说明

| 文件 / 目录 | 作用 |
|---|---|
| `dashboard.py` | HTTP 服务、任务队列、状态缓存、Git 面板、健康巡检、依赖探活、编排执行（约 3,300 行） |
| `dashboard.html` | 单页前端，内联 CSS 与 JS（约 4,000 行） |
| `devctl.sh` | 进程与构建的实际执行者：启动/停止/重启/更新/热更新/状态 JSON |
| `console_platform.py` | 平台适配层：端口列表、进程树、终止、通知、打开浏览器、临时目录 |
| `tests/` | 标准库 `unittest` 用例（健康检查、探活、端口释放、配置写入、依赖闸门、分组、拓扑排序） |
| `projects.conf` | 项目注册表（13 列，见下） |
| `ports.conf` | 端口覆盖表：项目实际监听端口与 `projects.conf` 不同时用 |
| `entries.conf` | 卡片背面的页面入口按钮 |
| `health.conf` | 健康检查配置（首次保存时生成，未出现的项目用默认值） |
| `deps.conf` | 外部依赖登记表（MySQL / Redis 等远程依赖，用于启动前探活） |
| `*.conf.example` | 各配置的模板。**真实配置不入库**（含本机绝对路径与内网地址），首次使用先复制再改 |
| `services/` | 为 WAR / 前端项目生成的启动、停止、构建脚本样例 |
| `maven-settings.xml` | 允许从内网 HTTP Maven 仓库拉包 |
| `legacy-proxy.conf` | 旧门户入口代理使用的 Host 头（真实域名不入库，默认 `localhost`） |
| `ROADMAP.md` | 功能路线图 P0–P3、阶段 S1–S4 与进度 |
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
| GET | `/api/jobs`、`/api/job` | 任务列表与单个任务输出 |
| GET | `/api/git`、`/api/git/conflicts` | 分支/改动/提交历史、冲突列表 |
| GET | `/api/project/inspect`、`/api/project/pick-dir` | 目录检测、目录选择 |
| POST | `/api/action` | 启停/重启/更新/热更新/安装/Git 动作 |
| POST | `/api/ports/kill` | 释放端口（仅允许当前真实监听的端口） |
| POST | `/api/health` | 保存单个项目的探活配置 |
| POST | `/api/project/add`、`/api/project/update`、`/api/project/port` | 增改项目与端口 |
| POST | `/api/git/commit`、`/resolve`、`/stash-apply`、`/stash-drop`、`/open-diff` | Git 面板动作 |
| POST | `/api/job/stop`、`/api/shutdown` | 停止任务、关闭控制台 |

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

## 运行时文件放在哪

| 路径 | 内容 |
|---|---|
| `/tmp/txlc-dashboard/jobs/` | 任务日志 `<job>.log` 与元数据 `<job>.json`（保留最近 20 个） |
| `/tmp/<项目>.action.running`、`.action.exit` | 异步启停的进度标记与退出码 |
| `/tmp/<项目>.action.log` | 启停操作的输出 |
| `/tmp/<项目>.log` | 服务自身输出（由 `devctl.sh` 重定向） |
| `<系统临时目录>/<项目>.health.log` | 自动重启记录 |

> 这些路径是当前实现的弱点（重启机器即丢失、Windows 不适用），重构时应改为项目自己的 `state/` 目录。

---

## 已知限制

- **仅 macOS**：`devctl.sh` 依赖 `lsof`、`pkill`、`osascript`；生成的服务脚本也是 bash。Windows 支持见 ROADMAP S3。
- **前端是单文件**：CSS/JS 内联在 `dashboard.html`，无法单独测试、改动冲突概率高。
- **无持久化**：任务历史与健康状态都在临时目录，控制台重启即丢。
- **测试覆盖有限**：32 个用例覆盖健康检查、端口逻辑、依赖探活与闸门、分组展开、拓扑排序；Git 解析、路径推导、前端逻辑尚无测试。
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
