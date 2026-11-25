# dev-tools 控制台 · 功能路线图

> 目标：把现在这套「脚本 + 单文件页面」的可用工具，补成一个**能当日常主力、能装到 Windows、能一路演进到 txlc-console（Go 版）**的完整项目。

## 1. 现状盘点

| 维度 | 现状 |
|---|---|
| 代码规模 | `devctl.py` 约 1230 行（含注释）、`dashboard.py` 约 4100 行、`dashboard.html` 约 5600 行 |
| 已有能力 | 启停/重启/更新、热更新、日志、Git 弹窗（分支/拉取/stash/冲突解决/diff）、项目增删改、端口覆盖、页面入口、前端项目、异步任务、健康巡检、端口面板、外部依赖闸门、分组与依赖顺序、批量操作与搜索 |
| 技术债 | 前端仍是单文件、状态仍在临时目录、配置仍是自定义竖线格式、`dashboard.py` 仍偏大 |
| 跨平台 | 进程/端口/通知/目录选择已收进 `console_platform.py`；核心迁到 `devctl.py`；Windows 有 `devctl.cmd` 与 `app/windows/launch.cmd`（未在真机验证）；新机器有 `install.sh` / `install.cmd` 与 `devctl.py doctor` |

结论：功能不算"薄"，薄在**运维闭环**（起得来但崩了没人管、没有中间件托管、没有分组和顺序）和**平台绑定**。

## 2. "完整"的定义（四条验收线）

1. **日常闭环**：服务挂了能自动拉起、依赖的 MySQL/Redis/Nacos 一起管、日志能查问题。
2. **可批量**：十几二十个服务能按分组、按依赖顺序一把启停，而不是一个个点。
3. **可交付**：macOS 双击装、Windows 双击装，配置能整包导出/导入，有版本号和升级路径。
4. **可演进**：核心逻辑集中在可测试的模块里，Go 重写时按功能清单一一对照移植，不靠回忆。

## 3. 架构决策（先定，避免返工）

**决策 1：新功能一律写在 Python 侧，bash 只做薄壳。**

`devctl.sh` 里的进程/端口/构建逻辑逐步搬到 `devctl.py`，`devctl.sh` 只保留参数转发。这样：
- Windows 直接可用（Python 跨平台，bash 不行）
- 逻辑可单元测试
- 以后 Go 重写时，Python 版就是逐条对照的"规格书"

**决策 2：平台差异收敛到一个适配层。**

新增 `console_platform.py`（不能叫 platform.py，会覆盖标准库），只暴露 4 个能力：`listening_ports()`、`pid_on_port()`、`kill_tree()`、`open_url()`，再加 `temp_dir()`、`spawn()`。macOS 用 `lsof`/`pkill`/`open`，Windows 用 `Get-NetTCPConnection`（或 `netstat`）/`taskkill /T /F`/`start`，Linux 用 `ss`/`kill -TERM`/`xdg-open`。

**决策 3：配置格式向前兼容。**

继续读现有 `projects.conf` / `ports.conf` / `entries.conf`（不破坏你现在跑着的东西），新字段只追加，写到第 11 列之后；后续 txlc-console 再切 TOML，迁移时提供一次性转换脚本。

**决策 4：前端拆成静态资源。**

`dashboard.html` 拆成 `web/index.html + app.js + styles.css`，先不用构建工具（保持零依赖双击可用）；需要组件化时再上 Vite，避免现在引入 Node 依赖链。

## 4. 功能清单

### P0 · 让控制台能兜住日常（缺一件都不算"完整"）

| # | 功能 | 为什么必须 | 验收标准 |
|---|---|---|---|
| 1 | **健康检查 + 崩溃自动重启** | 现在服务挂了只能靠肉眼发现 | 配置 HTTP 路径或端口探活；连续 N 次失败按退避重启，超过上限停止并标红；页面显示健康状态与重启次数 |
| 2 | **基础设施托管** | MySQL/Redis/Nacos 不在控制台里，每天要先手动开它们 | 独立"基础设施"页签；支持本机进程和 docker 容器两种模式；一键启停、端口/版本展示；业务服务启动前自动检查依赖是否就绪 |
| 3 | **服务分组 + 依赖顺序** | 14 个服务靠三个"核心"标记不够用 | 自定义分组（核心/业务/前端/基础设施）；支持 `depends_on`；启动按拓扑序并等健康，停止反序 |
| 4 | **日志增强** | 现在只能看滚动 tail，排错靠翻 | 级别过滤（ERROR/WARN/INFO）、关键字高亮、只看错误、按时间段筛选、导出当前视图、清空日志 |
| 5 | **批量操作与搜索** | 一个个点太慢 | 卡片多选后批量启停/更新；顶部搜索（名称/端口/分组）；`⌘K` 聚焦搜索；操作结果汇总展示 |
| 6 | **端口面板** | 端口冲突时只提示"被占用"，不告诉你是谁 | 列出所有被业务占用的端口→进程→项目；一键释放；与 `ports.conf` 的建议端口联动 |

### P1 · 协作与交付

| # | 功能 | 说明 |
|---|---|---|
| 7 | Profile / 环境变量切换 | 每个项目存多套启动参数与环境变量（local/dev/test），一键切换；写进 `profiles.conf` |
| 8 | 配置导出 / 导入 | 把 projects/ports/entries/services + 设置打成 zip，换机器或给同事直接导入 |
| 9 | 系统通知 | macOS 通知中心 + Windows Toast；构建失败、服务崩溃、自动重启达上限时提醒；可选企微/飞书 webhook |
| 10 | 任务队列可见化 | 并发上限、排队状态、取消、失败重试、历史留存（现在只有后台 job 日志） |
| 11 | Git 增强 | 面板内 checkout/merge/rebase、按文件丢弃改动、提交前跑格式检查 |

### P2 · 跨平台与打包

| # | 功能 | 说明 |
|---|---|---|
| 12 | devctl 核心迁移到 Python | `devctl.py` 与 bash 版行为对齐；`devctl.sh` 变薄壳；加单元测试 |
| 13 | Windows 支持 | `devctl.cmd` + PyInstaller 打包 `.exe`；单实例锁；开机自启可选；`services/` 脚本模板分平台生成 |
| 14 | 三平台打包与升级 | macOS `.app`/`.dmg`、Windows `.exe`/安装包、Linux AppImage；版本号写入 `/api/status`；启动时检查新版本 |
| 15 | 无人值守巡检 | 定时健康检查、夜间构建、每日汇总（成功率/耗时/崩溃次数） |

### P3 · 按需再做

| # | 功能 | 说明 |
|---|---|---|
| 16 | 指标面板 | CPU/内存/启动耗时曲线，定位内存泄漏和慢启动 |
| 17 | Docker / 远程主机 | 容器列表与日志；SSH 到测试机启停同一套配置 |
| 18 | 前端项目增强 | Vite 实际端口探测、依赖安装进度、构建产物预览 |

## 5. 实施顺序（建议）

| 阶段 | 内容 | 预期 |
|---|---|---|
| **S1** | P0 的 1、4、6（健康检查+自动重启、日志增强、端口面板） | 不动架构，只改 `dashboard.py` + 页面，能立刻感受到"完整" |
| **S2** | P0 的 2、3、5（基础设施、分组依赖、批量搜索） | 从"能启停"变成"能编排" |
| **S3** | P2 的 12、13（Python 核心 + Windows） | 拿到 Windows 可用的版本，同时为 Go 版铺路 |
| **P4** | P1 全部 | 交付、通知、队列 |
| **S4** | P2 的 14、15 + P3 | 打包升级、可选增强 |

每阶段结束都要满足：`devctl.py json` 合法、`/api/status` 正常、既有按钮回归一遍、新增功能有对应验收命令。

## 6. 与 txlc-console（Go 版）的映射

| dev-tools 阶段 | 对应 txlc-console 里程碑 |
|---|---|
| S1（健康检查、日志、端口） | M1 · 009 构建、010 文件监听、011 端口占用者、014 日志状态 |
| S2（基础设施、分组、批量） | M1 扩展 + M5 · 032 多服务、034 依赖顺序 |
| S3（Python 核心、Windows） | M4 · 027–031 打包与启动器探测 |
| P4（Profile、通知、队列） | M2/M3 之外的补充，可作为 M5 扩张 |
| 统一后的配置格式 | 迁移时转 TOML，一次性脚本 |

好处：dev-tools 的每条功能都对应一张 txlc-console 任务卡，你重写时不用重新想需求，只照着验收标准写实现。

## 7. 现在就开始的第一批（我建议）

**S1 三件套**，理由：改动范围只在现有两个文件内，不碰架构，做完立刻能感知到"不再是个薄工具"。

1. 健康检查 + 崩溃自动重启（含页面红/黄/绿状态和重启计数）
2. 日志增强（级别过滤、只看错误、关键字高亮、导出）
3. 端口面板（端口→进程→项目，一键释放）

同时并行做一件不显眼但要紧的事：把 `lsof`/`pkill`/`open` 这些调用收进 `console_platform.py`，为 S3 的 Windows 支持提前铺路。

## 8. 进度

### S1 已完成（2026-11-25）

| 交付 | 位置 | 验收方式 |
|---|---|---|
| 平台适配层 | `console_platform.py` | `python3 -c "import console_platform"`；Windows/Linux 分支已写好但未在真机验证 |
| 健康检查 + 自动重启 | `dashboard.py`：`health_loop` / `health_settings` / `trigger_auto_restart` | `/api/status` 的 `health` 字段；失败达阈值自动重启并写 `<临时目录>/<项目>.health.log` |
| 健康检查配置 | `health.conf`（新文件，按项目覆盖默认值） | 项目设置弹窗底部「健康检查与自动重启」→ 保存 → `health.conf` 出现对应行 |
| 日志增强 | 日志弹窗：关键字过滤、关键字高亮、级别过滤（全部/仅 ERROR/ERROR+WARN）、导出当前视图 | 打开任意服务日志，输入关键字看行数与高亮 |
| 端口面板 | 顶栏「端口」按钮，`/api/ports`、`/api/ports/kill` | 列出监听端口/进程/PID/项目；释放前二次确认；拒绝不存在的端口和控制台自身 |
| 单元测试 | `tests/test_health_and_ports.py`（12 用例） | `python3 -m unittest discover -s tests` |
| 技能包同步 | `~/.agents/skills/txlc-local-dev-console/assets/` | `install.sh` 已加 `console_platform.py`；`requirements.md` 已补新能力章节 |

设计取舍（写下来避免以后返工）：

- 自动重启默认开启，但有四道保险：启动/重启后 90 秒宽限期（Spring Boot 冷启动久）、3 次失败才判异常、20 秒重启冷却、预算上限默认 5 次且连续正常 60 秒后归还。
- 探活失败后项目被停掉时保留 `down` 状态，而不是变回普通「已停止」，否则用户分不清"崩了还是我停的"。
- 端口释放只接受"当前真实在监听"的端口，PID 以服务端返回的为准，前端传来的 PID 对不上就拒绝，避免任意杀进程。

### 下一步（S2）

基础设施托管（MySQL/Redis/Nacos）、分组与依赖顺序、批量操作与搜索。这三件决定它能不能从"能启停"变成"能编排"。

### S2 已完成（2026-11-25）

| 交付 | 位置 | 验收方式 |
|---|---|---|
| 外部依赖登记与探活 | `deps.conf` + `dashboard.py`：`load_deps_conf` / `probe_dep` / `refresh_deps` / `deps_loop` | `/api/deps` 返回状态与延迟；MySQL/Redis 通、不可达依赖标红 |
| 启动前依赖闸门 | `dashboard.py`：`dependency_gate` | 单项目启动依赖不通 → `409` + `can_force`；前端确认后可强制启动 |
| 分组展示与整组启停 | `projects.conf` 第 11 列；页面按组渲染，组头「启动组 / 停止组」 | 17 个项目分到 core / business / portal / frontend 四组 |
| 依赖顺序编排 | `dashboard.py`：`topo_order` / `start_ordered` / `wait_project_state`；`/api/status.ordered` | 网关在 svc-auth、svc-baseinfo 就绪后才启动；停止反序；阻塞项跳过并记录原因 |
| 批量操作 | 卡片勾选 + 底部批量条；`/api/action` 接受 `names` + `force` | 勾选多个项目批量启动/停止/重启/更新，进度显示在横幅 |
| 顶部搜索 | `dashboard.html`：`⌘K` 聚焦、`Esc` 清空 | 按名称/端口/分组/目录过滤，搜索时跨页签 |
| 单元测试 | `tests/test_deps_and_groups.py`（20 用例） | 依赖解析、探活、闸门、分组展开、拓扑排序（含环） |
| 配置模板 | `*.conf.example` 全套；真实配置不入库 | 新机器 `cp *.example` 后即可启动 |

设计取舍（S2 写下来避免返工）：

- **不做「本机托管 MySQL/Redis」**：实际依赖在远端，控制台没有 Docker 也不该管它们的生命周期；把力气花在「启动前先告诉你哪个依赖不通」上，收益更高。
- **闸门不硬拦整组**：整组启动时被依赖挡住的项目跳过并记录原因，其余继续；否则一个坏依赖会让整组停摆。
- **顺序执行放在 Python 侧**：`devctl start` 只管单个项目，编排需要「等端口就绪再下一个」，还要复用健康状态和依赖检查，放在 `dashboard.py` 里更好测试。
- **等待用实时状态**：`wait_project_state` 每轮直读 `devctl json`，不复用 1 秒 SWR 缓存，避免「刚启动」被误判为已就绪。
- **`ports.conf` 同时兼容空白与 `|`**：老文件是空白分隔，文档曾写成竖线，两种都接受，避免因分隔符失效。

### S2.5 已完成（2026-11-25）

| 交付 | 位置 | 验收方式 |
|---|---|---|
| 原生窗口外壳 | `app/shell.swift`（WKWebView + 原生菜单，无第三方依赖） | 双击 `服务控制台.app` 得到独立窗口，Dock 图标、`⌘Q` 退出、窗口位置记忆 |
| 启动器 | `app/launcher.sh` | 服务没跑时先拉起（日志仍写临时目录），就绪后才开窗；找不到窗口二进制时退化为浏览器打开 |
| 构建脚本 | `app/build.sh` | `swiftc` 单文件编译 + 组装 `.app` + ad-hoc 签名；`install.sh` 在有 swiftc 时调用 |
| 双入口 | App 窗口 / 浏览器 `http://localhost:18000` | 同一份 `dashboard.html`、同一套 `/api/*`、同一批 conf 文件；配置在两边等价 |
| App 菜单 | `shell.swift` | 重新加载 / 强制刷新 / 在浏览器中打开 / 打开 `projects.conf` / 打开配置目录 / 打开控制台日志 |

设计取舍：

- **App 不做第二套配置界面**：App、网页都渲染同一个页面，避免「App 里改了但网页看不到」的分叉；App 只补文件级快捷入口（conf、目录、日志）。
- **页面内链接交给系统浏览器**：`target=_blank`（页面入口、接口文档）在 App 里跳到默认浏览器，避免出现没有地址栏、无法返回的子窗口。
- **窗口跟随当前 Space**：`collectionBehavior = .moveToActiveSpace`，多显示器 / 多桌面下双击后窗口出现在当前桌面，而不是被记到上次的桌面。
- **服务与应用解耦**：退出 App 不停止控制台、也不停止业务服务；这与网页入口「关掉标签页不等于关服务」的语义一致。
- **Windows 外壳留到 S3**：Windows 上用 Edge/Chrome 的 `--app=` 模式即可得到无地址栏窗口，等 `devctl.py` 落地后再提供（当前 bash 执行层还不支持 Windows）。

### S2.6 已完成（2026-11-25）

补上「出问题怎么看日志」这条运维闭环。

| 交付 | 位置 | 验收方式 |
|---|---|---|
| 控制台自身日志 | `dashboard.py`：`configure_logging()`、`logger`；`logs/console.log`（2MB × 3 轮转，`TXLC_LOG_DIR` 可覆盖） | 启动/退出、访问日志、GET/POST 异常堆栈都落盘；重启机器后仍在 |
| 诊断清单接口 | `GET /api/diagnostics` | 汇总控制台 / 任务 / 项目输出 / 健康巡检 / 应用日志，只列真实存在的文件 |
| 尾随与单文件下载 | `GET /api/diagnostics/tail`、`/file` | id 是路径 sha1 前 12 位，未登记的 id 返回 404 |
| 诊断包导出 | `POST /api/diagnostics/export` | zip 含 `meta.txt` + 每个日志末尾 512KB；**不含 `*.conf`** |
| 诊断面板 | `dashboard.html` | 顶栏文件图标；左侧分组清单、右侧尾部内容、单文件下载、一键导出 |
| 两处查看器统一 | `dashboard.html` + `dashboard.py` | 诊断面板与项目日志同为 1200 行、同一套过滤/高亮/2 秒自动刷新；项目类条目带「在项目日志中打开」；轮询接口不写访问日志 |
| App 菜单 | `app/shell.swift` | 「打开控制台日志」优先打开 `logs/console.log`，另加「打开日志目录」 |

设计取舍：

- **控制台日志要落盘**：原来只有启动器的 `/tmp/txlc-dashboard.log`，重启机器即失；现在 `logs/console.log` 是主位置，临时文件只在进程被直接杀掉时才有内容。
- **id 白名单而不是传路径**：接口入参是清单里发出去的 id，后端映射回路径，避免 `/api/diagnostics/file?path=/etc/passwd` 这类读任意文件。
- **诊断包不含配置**：`*.conf` 里有内网地址与本机绝对路径，发出去前必须由人确认；包里用 `meta.txt` 说明这一点。
- **只在点击时汇总**：`/api/diagnostics` 不参与 1 秒轮询，避免每秒扫一遍日志目录。
- **不重复实现查看器**：诊断面板复用项目日志的渲染函数（行数、过滤、高亮、报错统计同一份），项目类条目再提供跳转；否则同一文件两处输出会不一致（先前的 1200 / 800 与「服务输出 / 启动日志」命名分裂）。
- **每文件 512KB 上限**：几十 MB 的服务日志仍能进包，但不会把包撑到几百 MB。

### S3 已完成（2026-11-25）

P2 的 12、13：核心逻辑从 bash 迁到 Python，并补齐 Windows 执行路径。

| 交付 | 位置 | 验收方式 |
|---|---|---|
| Python 核心 | `devctl.py`：配置解析、目标解析、端口/jar 发现、start/stop/update/install、Git、watch、status/list/json/panel | 与旧 bash 版逐字段对比：`list` 完全相同；`json` 17 个项目、20 个字段、顺序与值全部相同 |
| 兼容入口 | `devctl.sh`（37 行，只找 Python 并转发）、`devctl.cmd`（Windows） | `./devctl.sh list` / `devctl.cmd list` 输出与旧版一致 |
| 异步动作标记 | `devctl.py _action <项目> <start|stop|restart>` | 控制台 FAB 动作改调它；写 `<状态目录>/<项目>.action.running` / `.action.exit`，不再依赖 bash wrapper |
| 跨平台状态目录 | `console_platform.state_dir()`：macOS / Linux `/tmp`，Windows `%TEMP%\txlc` | 控制台与 devctl 共用；升级后旧 `/tmp` pid/日志仍能读 |
| 平台进程能力 | `pid_alive` / `kill_pid` / `spawn_detached` / `script_suffix` / `pick_directory` / `open_app_window` | macOS 已自检通过；Windows 分支用 `tasklist` / `taskkill /T` / PowerShell |
| 前端脚本分平台 | `dashboard.write_frontend_scripts()` | macOS/Linux 生成 `*.sh`；Windows 生成 `*.cmd`（`%PORT%`、`cd /d`） |
| Windows 启动器 | `app/windows/launch.py` + `launch.cmd` | 后台起 `dashboard.py`，等 `/api/status` 就绪后用 Edge/Chrome `--app=` 开窗 |
| Windows 打包 | `app/windows/build-exe.cmd`（PyInstaller `--onedir`） | 脚本已写，**待 Windows 真机验证**；`dashboard.py --devctl` 支持同 exe 分发 |
| 单元测试 | `tests/test_devctl.py`（24 用例） | 配置解析、目标/顺序、端口两种分隔符、jar 发现、JSON 字段与类型、action 标记、源码扫描、假项目 start/stop 全流程 |

设计取舍：

- **bash 不保留第二份实现**：`devctl.sh` 只剩 37 行转发，避免以后修 bug 要改两处；旧脚本已完整对照验收后删除。
- **JSON 的 `port` 必须是数字**：旧 bash 版输出数字，前端按数字比较端口；迁移时字符串端口导致「运行中」全部判错，已修正并用 17 个项目 diff 锁住。
- **状态目录不跟着 `tempfile.gettempdir()` 走**：macOS 的 `gettempdir()` 是 `/var/folders/...`，直接切过去会让正在跑的服务变成「未运行」；POSIX 明确保持 `/tmp`。
- **`_action` 而不是在每个前端动作里拼 shell**：标记文件、退出码、异常清理都在 Python 一个地方，Windows 上也不需要 `bash -c`。
- **打包仍保留 `devctl.py` 作为规格书**：PyInstaller 用 `--hidden-import devctl` + `dashboard.py --devctl` 复用同一入口，源码模式与 exe 模式行为一致。
- **Windows 先做到「能跑」而不是「装好」**：本轮交付 Python 执行路径与应用窗口；安装包、开机自启、单实例锁留给 P2 #14 / 后续。

已知限制：

- Windows 分支没有在真机跑过；`devctl.cmd`、`.cmd` 前端脚本、`%TEMP%` 状态目录、PyInstaller 打包都需要一台 Windows 机器做一次冒烟。
- `services/` 里已有的旧 bash 脚本不会自动迁移；在 Windows 上新增项目时才会生成 `.cmd`。

### S3.5 已完成（2026-11-25）

补上「换一台机器后怎么开始」：不是安装包，而是约一百行 bootstrap + 一个只读自检。

| 交付 | 位置 | 验收方式 |
|---|---|---|
| 初始化脚本 | `install.sh` / `install.cmd` | 复制 `*.conf.example`（已存在不覆盖）、建 `services/logs/tmp`、检查 Python/Java/Maven/Git/Node |
| 工作区扫描 | `install.sh --scan <目录>` / `discover.py` | 识别 Spring Boot server 模块生成候选；已有配置时只写 `projects.conf.candidate` |
| 环境与配置自检 | `devctl.py doctor [--env]` / `doctor.py` | 目录缺失、端口重复/占用、脚本与 jar、`requires` 与 `deps.conf`；FAIL 退出码非 0 |
| 空配置提示 | `devctl.py list` / `status` | 不再只报「未知项目」，而是提示编辑配置或 `--scan`；`json` 仍只输出 `[]` |
| 单元测试 | `tests/test_doctor.py`（32 用例） | 缺失/空配置、目录、重名、端口、脚本、前端、依赖、环境分级；全套 99 个用例通过 |

设计取舍：

- **先做配置与环境，不做导出/导入**：迁移摩擦的主要来源是路径和工具链，不是配置格式。
- **doctor 独立成模块**：不 import `dashboard.py`，避免自检顺手拉起 HTTP 服务；只复用 `devctl.py` 的解析与探测。
- **初始化不碰已有配置**：所有复制都带「文件已存在则跳过」，扫描结果也不能直接覆盖 `projects.conf`。
- **WARN 不阻塞**：Java 缺失且有后端项目才 FAIL；Maven/Git 缺失、端口被 IDE 占用只警告，保留「先试启动」的空间。
- **Windows 脚本同步给到但未真机验证**：`install.cmd` 与 `install.sh` 行为一一对应。

### 下一步（P4 / S4）

P1 的交付闭环（配置导出导入、通知、任务队列可见化、Git 增强），以及 P2 的 14、15（三平台打包与升级、无人值守巡检）。
