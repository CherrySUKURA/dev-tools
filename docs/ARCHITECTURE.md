# 架构与行为契约

> 这份文档写给两类读者：维护这个 Python 版控制台的人，以及准备用 Go + Vue 3 重写 `txlc-console` 的人/AI。
> 目标是把「必须保持不变的行为」和「可以推倒重来的实现」分开，避免重写时凭印象补需求。

---

## 1. 全貌

```
入口一：服务控制台.app（app/shell.swift：WKWebView 原生窗口 + 菜单）
入口二：浏览器（直接访问 http://localhost:18000）
   │  同一个页面 dashboard.html（单页，内联 CSS/JS）
   │  HTTP/JSON，1 秒轮询 /api/status
   ▼
dashboard.py（Python 标准库，无第三方依赖）
   ├─ ThreadingHTTPServer：30 个路由
   ├─ 任务队列 jobs：构建/更新/安装/Git 动作，全局串行
   ├─ 状态缓存 status_cache：stale-while-revalidate，1 秒
   ├─ 健康巡检线程 health_loop：2 秒调度，按项目间隔探活
   ├─ 依赖巡检线程 deps_loop：15 秒探活 deps.conf，启动闸门依据
   ├─ 编排任务 ordered_task：按 depends 拓扑排序，逐个项目等就绪
   ├─ Git 面板：分支/提交历史/冲突解析/stash/diff
   └─ 平台层 console_platform.py
          │  subprocess
          ▼
devctl.sh（bash 核心）
   ├─ start_one / stop_one / update_one / build_one
   ├─ watch：源码监听 + 自动重编译重启（常驻进程）
   └─ json_status：把所有项目状态输出为 JSON
          │
          ▼
真实进程（java -jar / services/*.sh / npm dev），日志重定向到临时目录
```

---

## 2. 分层与职责

| 层 | 文件 | 只做这些事 |
|---|---|---|
| 外壳层（仅 macOS） | `app/shell.swift` + `app/launcher.sh` | 先确保本地服务在跑，再用 WKWebView 开原生窗口；菜单只做刷新、浏览器入口、打开配置/日志，不承载业务逻辑 |
| 表现层 | `dashboard.html` | 渲染卡片、分组、弹窗、轮询状态；搜索/过滤/高亮在本地完成，不再请求后端 |
| HTTP / 编排层 | `dashboard.py` | 路由、参数校验、任务排队、状态聚合、健康巡检、依赖探活与闸门、拓扑排序与编排执行、Git 面板 |
| 平台层 | `console_platform.py` | `listening_ports`、`pid_on_port`、`process_tree`、`kill_tree`、`port_open`、`open_url`、`notify`、`temp_dir`、`find_executable` |
| 执行层 | `devctl.sh` | 进程的真实启停、Maven 构建、jar 查找、日志重定向、端口占用判定 |
| 配置 | `*.conf` | 项目、端口、入口、健康、外部依赖，全部为竖线分隔纯文本 |

**判断新逻辑该放哪：** 需要跨平台或需要单测 → `dashboard.py` / `console_platform.py`；必须直接操作进程树或 bash 语义 → `devctl.sh`，并且尽量只做薄壳。

**两个入口的边界：** App 与网页最终都落在同一个 `dashboard.html` 和同一套 `/api/*`。任何配置都必须在页面里完成（App 不另做一套表单）；App 只额外提供文件级快捷方式（打开 `projects.conf`、配置目录、控制台日志）和「在浏览器中打开」。这样重写成 Go 版时，可以先用浏览器验证行为，再决定是否补外壳。

**判断一次操作走哪条路：** 只涉及一个项目 → `start_fast`（立即返回，进度靠轮询）；涉及多个项目 → `start_ordered`（拓扑排序后逐个项目 `start_fast` + 等就绪）；构建/更新/安装/Git → 任务队列（全局串行）。

---

## 3. 状态与文件布局

| 位置 | 内容 | 生命周期 |
|---|---|---|
| `logs/console.log`（+ `.1`~`.3`） | 控制台进程日志：启动/退出、访问日志、异常堆栈；2MB × 3 轮转 | 持久，重启后仍可查 |
| `logs/`（可用 `TXLC_LOG_DIR` 覆盖） | 目录本身进 `.gitignore`，不进仓库 | — |
| `/tmp/<项目>.pid` | 启动后的主进程 PID | 重启机器即失效 |
| `/tmp/<项目>.log` | 服务 stdout/stderr | 覆盖写 |
| `/tmp/<项目>.action.running` | 正在执行的启停动作名 | 动作结束后删除 |
| `/tmp/<项目>.action.exit` | 上次启停的退出码 + mtime | 保留，用于前端判定结束 |
| `/tmp/<项目>.action.log` | 启停操作输出（与操作日志弹窗对应） | 追加 |
| `/tmp/txlc-dashboard/jobs/*.log|*.json` | 任务输出与元数据，保留最近 20 个 | 控制台重启后仍可查历史 |
| `<系统临时目录>/<项目>.health.log` | 自动重启记录 | 追加 |
| `health.conf` | 探活配置 | 持久 |
| `deps.conf` | 外部依赖登记 | 持久 |

> 这一层是**当前实现最弱的地方**：状态全在临时目录，重启即丢，且 Windows 无 `/tmp`。重写时应改成项目内的 `state/` 目录并落库。

---

## 4. 一次「启动」的完整链路

以点击卡片上的「启动」为例：

1. 前端 `POST /api/action`，body `{action: "start-bg", target: "svc-order"}` 或 `{action: "start-bg", names: [...]}`。
2. `valid_target` 校验目标（只允许 `all` / `core` / `group:xx` / 真实项目名，防命令注入）；`expand_target` 展开成项目名列表。
3. **依赖闸门**：单个项目且未 `force` 时先跑 `dependency_gate`，`requires` 里有不通的依赖就返回 `409 {can_force, down_deps}`，不进入启动。
4. **单项目**：`start_fast` 写 `/tmp/svc-order.action.running`，清掉旧 `.action.exit`，然后 `subprocess.Popen` 一个 bash wrapper。
   **多项目**：`start_ordered` 先 `topo_order`（依赖在前，停止时反序），再逐个项目重复「`start_fast` → `wait_project_state` 等端口就绪」，进度写入内存里的 `ordered_task`，由 `/api/status.ordered` 暴露。
5. wrapper 调 `devctl.sh start svc-order`：
   - `pid_alive` / `port_listening` 先判重，已在运行则直接返回；
   - 有自定义启动脚本（WAR / 前端）→ 用 `services/*.sh`，注入 `PORT` 与 `SERVER_PORT`；
   - 否则找 jar，没有就先 `build_one`（Maven）；
   - 注入端口覆盖与「本机联调地址」（把部分服务间调用指回 `127.0.0.1`）；
   - `java -jar ... &`，写 pid 文件，轮询端口最多 90 秒。
6. wrapper 结束时把退出码写进 `.action.exit`，删除 `.action.running`。
7. 前端 1 秒轮询 `/api/status`，用 `action_running` / `action_exit_time` 清掉 pending 状态。
8. 健康线程按 `health.conf` 探活；连续失败达到阈值且允许自动重启时，调 `start_fast("restart-bg", name)`。
9. 依赖巡检线程每 15 秒刷新 `deps.conf` 里各依赖的可用性，供下一次启动闸门与顶栏徽标使用；`/api/deps?refresh=1` 可手动触发。

**关键语义：** 接口立即返回，真实进度靠轮询。这条不能改成同步阻塞——Spring Boot 冷启动可达一分钟，同步会把 HTTP 线程占满。

---

## 5. 并发模型

| 机制 | 说明 |
|---|---|
| 每请求一线程 | `ThreadingHTTPServer` |
| 状态缓存 | 首个请求同步读 `devctl json`，之后立即返回缓存并后台刷新；同一时刻只允许一个刷新线程 |
| 任务队列 | `active_job_id` 全局唯一，构建/更新/安装/Git 动作串行；第二个任务返回 409 |
| 快速动作 | 启停/重启走 `FAST_ACTIONS`，每项目一个 wrapper，**不占任务队列**，因此可以与更新并行 |
| 健康巡检 | 独立 daemon 线程，2 秒调度一次；项目清单 5 秒刷新一次；每项目按自身间隔探活 |
| 依赖巡检 | 独立 daemon 线程，每 15 秒探活一次 `deps.conf`；慢依赖不拖累服务探活 |
| 编排任务 | 内存里唯一一个 `ordered_task`；多项目启停按拓扑序串行，进度由 `/api/status` 暴露，同一时刻只允许一个编排 |
| 热更新 | `devctl.sh watch` 独立常驻进程，不占队列 |
| 锁 | `jobs_lock`、`status_cache_lock`、`git_cache_lock`、`health_lock`、`health_conf_lock`、`project_conf_lock`、`dep_lock`、`ordered_lock` |

**已知竞态与防护：**

- 健康巡检遇到 `_action_in_flight`（`.action.running` 存在且 10 分钟内）就跳过，避免和人工启停互相打断。
- 启动/重启后 90 秒宽限期内不判故障（Spring Boot 冷启动慢）。
- `start_fast` 对同一项目 10 分钟内的 running 标记会跳过重复提交。
- 端口释放以服务端实时 `listening_ports()` 为准，前端传来的 PID 对不上就拒绝。
- 编排任务等待就绪时**不用状态缓存**，每轮直读 `devctl json`：缓存滞后会把「刚启动」误判为已就绪，也会让停止动作提前宣布完成。
- 编排中某个项目被依赖闸门挡住时跳过并记录原因，不中断其余项目；`force: true` 则跳过闸门整体执行。

---

## 6. 行为契约（重写时必须保持）

### 6.1 接口

路径与语义见 README 的接口表。重写时最容易踩的几条：

- `POST /api/action` 接受 `all` / `core` / `group:xx` / 项目名，或 `names` 数组；**未知目标必须拒绝**。
- `GET /api/status` 是前端唯一的状态来源，1 秒轮询，必须足够便宜（缓存 + SWR）；`deps` 与 `ordered` 只是附加字段，缺失时前端仍要能正常工作。
- `GET /api/log` 的 `kind=service|action`，并通过 `source` 选择同一项目下的多个日志源。
- `GET /api/deps` 只做探活，不做增删；`refresh=1` 同步重探，其余情况返回最近一次结果。
- `POST /api/ports/kill` 只允许释放**当前真实监听**的端口，禁止杀控制台自身与 PID 0/1。
- `POST /api/health` 写入 `health.conf` 时，非法输入必须拒绝而不是写坏文件。
- `GET /api/diagnostics*` 只接受**本次清单登记过的 id**（路径 sha1 前 12 位），不能拿 id 读任意文件；清单本身也只列真实存在的日志。`POST /api/diagnostics/export` 每个文件最多 512KB，且**不得包含 `*.conf`**。
- 控制台自身日志必须落盘（`logs/console.log`），不能只依赖启动器的 stdout 重定向——那个文件在临时目录，重启机器就没了。

### 6.2 配置

- `projects.conf` 前 10 列的位置与语义不能变；新字段只能追加。第 11 列分组、第 12 列项目依赖、第 13 列外部依赖为空时按旧逻辑推导。
- 空值表示「用默认逻辑」：jar 留空自动找、文档留空按类型给默认值、脚本留空用内置逻辑、分组留空按类型与核心标记推导。
- `ports.conf` 的覆盖通过 `SERVER_PORT` 注入；自定义脚本同时拿到 `PORT` 与 `SERVER_PORT`。分隔符空白与 `|` 都必须接受。
- `deps.conf` 的 `tcp` 只建立连接，`http` 只看有没有 HTTP 响应；**不能**因为 HTTP 4xx 判为不可用。
- `entries.conf` 与文档地址展示时统一归一化成 `localhost`，避免后端按 Host 判成非法来源。

### 6.3 状态语义

- 运行中 = pid 存活 **或** 端口在监听（两者之一即可，容错外部启动的服务）。
- 用户手动停止 ≠ 崩溃：崩溃后保留 `health=down` 红色状态，否则用户分不清原因。
- 自动重启必须有四道保险：启动宽限期、失败阈值、重启冷却、预算上限（持续正常后归还）。
- 前端入口、健康状态、Git 状态都是「附加信息」，任一获取失败不能影响启停主流程。

### 6.4 编排与依赖语义

- 多项目启动顺序 = `depends` 的拓扑序；依赖不在本次范围内就忽略，不自动把依赖带进来。
- 停止顺序 = 启动顺序的反序。
- 依赖环不能死循环：遇到回边直接忽略，保证每个项目最多执行一次。
- 单项目启动且外部依赖不通 → `409 {can_force: true, down_deps}`；`force: true` 才跳过闸门。
- 整组/批量启动时，被闸门挡住的项目跳过并记录原因，其余项目照常执行。
- 编排进度只存在于内存，控制台重启即丢失；重建时不能阻塞状态接口。

---

## 7. 可以推倒重来的部分

| 现状 | 建议 |
|---|---|
| bash 执行层 + `/tmp` 状态 | Go 原生实现进程管理，状态落 `state/` 目录 |
| 单文件 4,000 行前端 | Vue 3 组件化，过滤/高亮抽成独立模块 |
| mtime 拼出的 `version` | 语义化版本 + 构建信息 |
| 竖线分隔配置 | TOML；提供一次性转换脚本，不要求用户手改 |
| pid 文件判活 | 校验 pid 对应的命令行（防 PID 复用与残留文件） |
| 仅 TCP 探活 | 增加 HTTP/actuator、进程内指标；依赖可加账号级检查（MySQL ping） |
| 内存里的编排进度 | 落库并支持取消、重试、历史 |
| 项目间依赖写在竖线配置 | TOML 里显式声明，并校验环 |
| 临时目录里的任务历史 | 持久化，支持按时间/项目筛选 |

**不要照搬的实现细节：** wrapper 里的内联 bash 字符串、通过 `.exit` 文件传递退出码、用 mtime 触发前端刷新、把 CSS/JS 内联进 HTML。

---

## 8. 重构任务拆分建议

对应 `txlc-console` 的里程碑：

| 阶段 | 内容 | 依赖的前置契约 |
|---|---|---|
| M0 地基 | 配置解析（TOML + 迁移）、状态目录、日志设施、CLI 骨架 | 配置列语义 |
| M1 日常可用 | 状态聚合、启停、构建、日志、健康检查、端口占用者 | 启动即返回 + 轮询、状态语义 |
| M2 Git | 分支/改动/提交历史/冲突解决/stash | 接口字段 |
| M3 界面 | 卡片、分组、批量、日志过滤、端口面板 | 接口与字段 |
| M4 打包 | macOS `.app`、Windows `.exe`、启动器与环境探测 | 平台层能力清单 |
| M5 扩展 | 多服务、依赖顺序、远程主机 | 分组与依赖语义 |

---

## 9. 验收方式

**自动：**

```bash
python3 -m unittest discover -s tests -v
```

**手工回归清单（重写后逐条对照）：**

1. 17 个项目全部出现在对应分组，卡片显示端口与运行状态。
2. 启动一个 Spring Boot 服务：无 jar 时自动构建，成功后有 pid、端口监听、健康变绿。
3. 停止后卡片变「已停止」；崩溃/探活失败的项目保留红色状态。
4. 「启动核心项目」按依赖顺序执行，网关在核心服务就绪后才启动。
5. 停止全部按依赖反序，先停门户再停核心。
6. 日志弹窗：关键字过滤、级别过滤、高亮、导出当前视图。
7. 端口面板：端口→进程→项目，释放非登记端口需二次确认。
8. 项目设置：改端口、改目录、健康检查配置保存后立即生效。
9. Git 弹窗：分支、改动、拉取、stash、冲突解决（含二进制冲突拒绝）。
10. 页面入口与文档按钮用 `localhost` 打开，翻面动画正常。
11. 外部依赖：`deps.conf` 里故意填一个不通的端口，启动引用它的项目应提示「外部依赖不可用」；确认后强制启动能成功。
12. 整组启动：组内有项目依赖不通时被跳过并在横幅说明原因，同组其他项目照常启动。
13. 批量操作：勾选多个项目后底部批量条出现，启动/停止按依赖顺序执行，进度横幅最终显示 `x/y`。
14. 搜索：输入名称/端口/分组能过滤，`⌘K` 聚焦、`Esc` 清空；清空后恢复分组视图。
