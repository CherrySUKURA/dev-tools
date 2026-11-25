# 架构与行为契约

> 这份文档写给两类读者：维护这个 Python 版控制台的人，以及准备用 Go + Vue 3 重写 `txlc-console` 的人/AI。
> 目标是把「必须保持不变的行为」和「可以推倒重来的实现」分开，避免重写时凭印象补需求。

---

## 1. 全貌

```
浏览器（dashboard.html 单页，内联 CSS/JS）
   │  HTTP/JSON，1 秒轮询 /api/status
   ▼
dashboard.py（Python 标准库，无第三方依赖）
   ├─ ThreadingHTTPServer：25 个路由
   ├─ 任务队列 jobs：构建/更新/安装/Git 动作，全局串行
   ├─ 状态缓存 status_cache：stale-while-revalidate，1 秒
   ├─ 健康巡检线程 health_loop：2 秒调度，按项目间隔探活
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
| 表现层 | `dashboard.html` | 渲染卡片、弹窗、轮询状态；所有过滤/高亮在本地完成，不再请求后端 |
| HTTP / 编排层 | `dashboard.py` | 路由、参数校验、任务排队、状态聚合、健康巡检、Git 面板 |
| 平台层 | `console_platform.py` | `listening_ports`、`pid_on_port`、`process_tree`、`kill_tree`、`port_open`、`open_url`、`notify`、`temp_dir`、`find_executable` |
| 执行层 | `devctl.sh` | 进程的真实启停、Maven 构建、jar 查找、日志重定向、端口占用判定 |
| 配置 | `*.conf` | 项目、端口、入口、健康、外部依赖，全部为竖线分隔纯文本 |

**判断新逻辑该放哪：** 需要跨平台或需要单测 → `dashboard.py` / `console_platform.py`；必须直接操作进程树或 bash 语义 → `devctl.sh`，并且尽量只做薄壳。

---

## 3. 状态与文件布局

| 位置 | 内容 | 生命周期 |
|---|---|---|
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

1. 前端 `POST /api/action`，body `{action: "start-bg", target: "svc-order"}`。
2. `valid_target` 校验目标（只允许 `all` / `core` / 真实项目名，防命令注入）。
3. `start_fast` 写 `/tmp/svc-order.action.running`，清掉旧 `.action.exit`，然后 `subprocess.Popen` 一个 bash wrapper。
4. wrapper 调 `devctl.sh start svc-order`：
   - `pid_alive` / `port_listening` 先判重，已在运行则直接返回；
   - 有自定义启动脚本（WAR / 前端）→ 用 `services/*.sh`，注入 `PORT` 与 `SERVER_PORT`；
   - 否则找 jar，没有就先 `build_one`（Maven）；
   - 注入端口覆盖与「本机联调地址」（把部分服务间调用指回 `127.0.0.1`）；
   - `java -jar ... &`，写 pid 文件，轮询端口最多 90 秒。
5. wrapper 结束时把退出码写进 `.action.exit`，删除 `.action.running`。
6. 前端 1 秒轮询 `/api/status`，用 `action_running` / `action_exit_time` 清掉 pending 状态。
7. 健康线程按 `health.conf` 探活；连续失败达到阈值且允许自动重启时，调 `start_fast("restart-bg", name)`。

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
| 热更新 | `devctl.sh watch` 独立常驻进程，不占队列 |
| 锁 | `jobs_lock`、`status_cache_lock`、`git_cache_lock`、`health_lock`、`health_conf_lock`、`project_conf_lock` |

**已知竞态与防护：**

- 健康巡检遇到 `_action_in_flight`（`.action.running` 存在且 10 分钟内）就跳过，避免和人工启停互相打断。
- 启动/重启后 90 秒宽限期内不判故障（Spring Boot 冷启动慢）。
- `start_fast` 对同一项目 10 分钟内的 running 标记会跳过重复提交。
- 端口释放以服务端实时 `listening_ports()` 为准，前端传来的 PID 对不上就拒绝。

---

## 6. 行为契约（重写时必须保持）

### 6.1 接口

路径与语义见 README 的接口表。重写时最容易踩的几条：

- `POST /api/action` 接受 `all` / `core` / 项目名；**未知目标必须拒绝**。
- `GET /api/status` 是前端唯一的状态来源，1 秒轮询，必须足够便宜（缓存 + SWR）。
- `GET /api/log` 的 `kind=service|action`，并通过 `source` 选择同一项目下的多个日志源。
- `POST /api/ports/kill` 只允许释放**当前真实监听**的端口，禁止杀控制台自身与 PID 0/1。
- `POST /api/health` 写入 `health.conf` 时，非法输入必须拒绝而不是写坏文件。

### 6.2 配置

- `projects.conf` 前 10 列的位置与语义不能变；新字段只能追加。
- 空值表示「用默认逻辑」：jar 留空自动找、文档留空按类型给默认值、脚本留空用内置逻辑。
- `ports.conf` 的覆盖通过 `SERVER_PORT` 注入；自定义脚本同时拿到 `PORT` 与 `SERVER_PORT`。
- `entries.conf` 与文档地址展示时统一归一化成 `localhost`，避免后端按 Host 判成非法来源。

### 6.3 状态语义

- 运行中 = pid 存活 **或** 端口在监听（两者之一即可，容错外部启动的服务）。
- 用户手动停止 ≠ 崩溃：崩溃后保留 `health=down` 红色状态，否则用户分不清原因。
- 自动重启必须有四道保险：启动宽限期、失败阈值、重启冷却、预算上限（持续正常后归还）。
- 前端入口、健康状态、Git 状态都是「附加信息」，任一获取失败不能影响启停主流程。

---

## 7. 可以推倒重来的部分

| 现状 | 建议 |
|---|---|
| bash 执行层 + `/tmp` 状态 | Go 原生实现进程管理，状态落 `state/` 目录 |
| 单文件 4,000 行前端 | Vue 3 组件化，过滤/高亮抽成独立模块 |
| mtime 拼出的 `version` | 语义化版本 + 构建信息 |
| 竖线分隔配置 | TOML；提供一次性转换脚本，不要求用户手改 |
| pid 文件判活 | 校验 pid 对应的命令行（防 PID 复用与残留文件） |
| 仅 TCP 探活 | 增加 HTTP/actuator、进程内指标 |
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
