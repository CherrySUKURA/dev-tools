# 新机器安装指南

面向「换一台电脑，从 clone 到服务跑起来」的完整步骤。日常使用和接口说明见 [README.md](../README.md)，行为契约见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

控制台本身不安装业务依赖；当前 `install.sh` / `install.cmd` 创建空配置和运行目录并检查环境，尚不能自动安装缺失的软件。


## 同事快速开始

安装脚本**只检查环境并提示安装地址，不会自动下载安装软件**。已有配置会保留，不会被覆盖。第一次运行生成空项目列表；先打开控制台，再添加本机项目。

### Windows

1. 把仓库克隆或下载并解压到你想存放的目录。没有 Git 时可在 GitHub 仓库页面选择 **Code → Download ZIP**。
2. 双击项目根目录的 `setup.cmd`。窗口会显示环境检查结果；如果提示缺少 Python 3.8+，按提示打开官网安装，选择安装目录并勾选 **Add python.exe to PATH**，然后重新打开终端或重新双击检查。Java、Maven、Git、Node 只在对应功能需要时安装。
3. 检查完成后，双击根目录的 `start.cmd`，或者双击 `app\windows\launch.cmd`。也可以在浏览器打开 http://127.0.0.1:18000。
4. 页面空白列表代表还没有项目；在控制台中添加本机项目。需要启动或构建 Java 服务时，请先按提示准备 JDK 和 Maven。

### macOS

1. 克隆或下载并解压仓库，打开“终端”，进入项目目录。例如输入 `cd `（末尾有空格），再把项目文件夹拖进终端并回车。
2. 第一次运行 `chmod +x setup.command start.command`。之后可在 Finder 双击 `setup.command` 检查环境；也可以直接运行 `bash install.sh`。
3. 若提示缺少 Python 3.8+，按提示前往官网下载并安装，然后重新检查；其他工具只在对应项目需要时安装。
4. 双击 `start.command`，或运行 `bash start.command`，会在浏览器打开控制台。首次双击时如果 macOS 提示无法验证来源，请根据系统提示操作，或使用终端命令启动。
5. 在控制台里添加本机项目。公司内网地址和旧电脑配置不会随仓库自动下载；需要时另行安全迁移并修改路径。

安装后如遇问题，先看脚本中的 FAIL/WARN；下文有配置和排障的详细说明。

---

## 0. 前置依赖

| 软件 | 要求 | 用途 | 是否必需 |
|---|---|---|---|
| Python | 3.8+ | 控制台后端与 `devctl.py` | 必需 |
| JDK | 17+ | 启动后端 Java 服务 | 有后端项目时必需 |
| Maven | 3.9+ | 构建/更新后端 | 有后端项目时必需 |
| Git | 较新版本 | clone、拉取、更新 | 必需 |
| Node + 包管理器 | 18+ | 前端项目 | 有前端项目时必需 |
| Xcode 命令行工具 | macOS | 生成 `服务控制台.app` | 可选 |

### macOS

```bash
brew install --cask temurin@17
brew install maven git
brew install node          # 只有前端项目需要
xcode-select --install     # 只有要生成 服务控制台.app 时需要
```

没有 Homebrew 时：

- JDK：装 Temurin 17 pkg，确认 `java -version` 可用
- Maven：下载 `apache-maven-3.9.x`，解压到 `dev-tools/apache-maven-3.9.x`；`devctl.py` 会自动把 `dev-tools/apache-maven-*/bin` 加进子进程 PATH
- Python：运行 `python3 --version` 确认是 3.8 或以上；没有可用版本时从 Python 官网安装。

### Linux（Debian / Ubuntu）

```bash
sudo apt install python3 openjdk-17-jdk maven git
sudo apt install nodejs npm     # 只有前端项目需要
```

### Windows

- Python 3.8+：python.org 安装包，勾选 **Add python.exe to PATH**
- JDK 17：Temurin，设置 `JAVA_HOME`，并把 `%JAVA_HOME%\bin` 加入 `PATH`
- Maven：解压后把 `bin` 加入 `PATH`；或把 `apache-maven-3.9.x` 放在 `dev-tools\` 下
- Git for Windows
- Node.js：只有前端项目需要

安装后确认：

```bash
python3 --version   # Windows: python --version
java -version
mvn -v
git --version
```

---

## 1. macOS / Linux

### 1.1 clone

```bash
mkdir -p ~/java
cd ~/java
git clone https://github.com/CherrySUKURA/dev-tools.git dev-tools
cd dev-tools
```

控制台可以放在任意目录，`projects.conf` 里写项目绝对路径即可。放在项目旁边只是为了目录整齐。

### 1.2 初始化

```bash
./install.sh
```

没有执行权限时：

```bash
bash install.sh
```

它会：

- 创建空的 `projects.conf`、`deps.conf`、`ports.conf`、`entries.conf`、`health.conf`、`legacy-proxy.conf`；可参考同名的 `*.conf.example`
- 复制 `maven-settings.xml`
- 创建 `services/`、`logs/`、`tmp/`
- 检查 Python / Java / Maven / Git / Node

**已有文件不覆盖**，重复执行是安全的。

### 1.3 配置项目 `projects.conf`

#### 方式 A：扫描 Maven 工作区

```bash
./install.sh --scan ~/java
```

- 识别带 `*-server` 模块或 Spring Boot 启动类的工程
- 输出候选行，端口建议打印在终端
- 已有真实项目时结果写入 `projects.conf.candidate`；空配置可由扫描结果填充
- **项目名、路径、核心标记仍需人工确认**

#### 方式 B：手写

最小只需前 3 列：

```text
svc-auth|/Users/alice/java/svc/svc-auth|1
svc-order|/Users/alice/java/svc/svc-order|0
txlc-gateway|/Users/alice/java/txlc-gateway|1
```

完整 13 列：

```text
项目名|目录|核心(1/0)|jar|start|stop|build|文档|install模块|类型|分组|依赖项目|必需外部依赖
```

需要外部依赖时：

```text
svc-order|/Users/alice/java/svc/svc-order|0|||||||backend|business||mysql-test,redis-test
```

后 10 列可省略；类型默认 `backend`，分组与端口会自动推导。

> 前端项目、传统 WAR + Tomcat 项目建议在控制台页面里点「添加项目」注册：它会同时写 `projects.conf` 并生成 `services/` 下的启动/构建脚本。手写 conf 不会生成脚本。

### 1.4 按本机情况修改其余配置

`deps.conf`：MySQL / Redis / Nacos 等外部依赖，改成实际内网地址。

```text
mysql-test|tcp|192.0.2.10|3306|测试库
redis-test|tcp|192.0.2.10|6379|共享缓存
```

`ports.conf`：只有端口与默认值不一致时才需要。

```text
svc-order 18083
svc-auth 18089
```

`maven-settings.xml`：使用内网 Maven 仓库时，改 `url` 和 `mirrorOf`。不用内网仓库可以保持模板。

`legacy-proxy.conf`：只有访问旧门户入口时才需要，填旧系统域名。

### 1.5 自检

```bash
./devctl.py doctor
```

- `FAIL`：必须修，修完重跑
- `WARN`：可以继续，建议后续处理
- 退出码 0 = 没有 FAIL

只查环境：

```bash
./devctl.py doctor --env
```

`doctor` 是只读检查，不启动服务、不改文件。

### 1.6 启动

先打开控制台，不要求先启动业务项目：

```bash
bash start.command
```

它会启动控制台并在浏览器打开 `http://127.0.0.1:18000`。首次添加项目后，按实际需要启动对应服务。也可以直接在终端运行 `python3 dashboard.py`；结束该终端进程不会停止已经启动的业务服务。

### 1.7 可选：生成原生窗口

```bash
./app/build.sh
open 服务控制台.app
```

之后日常双击 `服务控制台.app`：它会先确保后台服务在跑，再打开原生窗口。

如果系统阻止打开自己构建的 App，可在“系统设置 → 隐私与安全性”中查看提示；也可以继续使用上面的浏览器启动方式。

---

## 2. Windows

### 2.1 clone 并初始化

```bat
mkdir C:\work
cd /d C:\work
git clone https://github.com/CherrySUKURA/dev-tools.git dev-tools
cd /d C:\work\dev-tools
install.cmd
```

扫描工作区：

```bat
install.cmd --scan C:\work
```

### 2.2 配置

`projects.conf` 使用 Windows 绝对路径：

```text
svc-auth|C:\work\svc\svc-auth|1
svc-order|C:\work\svc\svc-order|0
txlc-gateway|C:\work\txlc-gateway|1
```

> 从 macOS 拷来的配置必须把所有 `/Users/...` 改成 `C:\work\...`。

启动/停止/构建脚本必须是 `.cmd`。不要直接使用 macOS 的 `services/*.sh`；在控制台里重新添加项目，或手写 `.cmd`。

### 2.3 自检与启动

```bat
python devctl.py doctor
devctl.cmd start core
devctl.cmd status all
```

起控制台：

```bat
python dashboard.py
```

浏览器打开 `http://127.0.0.1:18000`。

或双击：

```text
app\windows\launch.cmd
```

它会后台启动控制台，再用 Edge/Chrome 的应用窗口打开。

> Windows 分支尚未在真机验证。首次使用请保留 `doctor` 输出；PyInstaller 打包后可用 `TXLCConsole.exe --devctl doctor` 自检。

---

## 3. 从旧电脑迁移配置

这些文件都在 `.gitignore` 里，不随仓库走，需要单独拷贝到新电脑的 `dev-tools/`：

```text
projects.conf
ports.conf
deps.conf
entries.conf
health.conf
legacy-proxy.conf
maven-settings.xml
services/
```

拷贝后替换路径。

macOS / Linux：

```bash
# 只换了用户名
sed -i '' 's#/Users/oldname#/Users/newname#g' projects.conf

# 或整个工作区根目录都变了
sed -i '' 's#/Users/oldname/java#/Users/newname/work#g' projects.conf services/*.sh

./devctl.py doctor
```

Linux 上把 `sed -i ''` 改成 `sed -i`。

Windows 用编辑器全局替换成 `C:\work\...`，并把 `services/*.sh` 换成 `.cmd`。

`doctor` 报「目录不存在」「启动脚本不存在」通常就是旧路径没换干净。

> 真实 `*.conf` 含本机路径和内网地址，不要提交到 Git；通过 AirDrop、scp 或其他安全通道传输。

---

## 4. 新机器验收清单

```bash
./devctl.py doctor                  # 退出码 0，无 FAIL
./devctl.py list                    # 项目数量、端口、目录符合预期
./devctl.sh json | python3 -m json.tool >/dev/null   # JSON 可解析
./devctl.sh start core              # 核心服务端口就绪
```

再检查：

- 浏览器能打开 `http://127.0.0.1:18000`
- 日志弹窗能读到 `/tmp/<项目>.log`（Windows 为 `%TEMP%\txlc\<项目>.log`）
- 前端项目能在页面上启动并打开页面入口

---

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| `Permission denied` | `bash install.sh` |
| `doctor` 报未找到 java | 装 JDK 17，确认 `java -version` 在 PATH 里可用 |
| `doctor` 报未找到 mvn | 装 Maven，或解压到 `dev-tools/apache-maven-*` |
| `doctor` 报目录不存在 | `projects.conf` 里还是旧机器路径 |
| `doctor` 报端口已被占用 | 服务可能已被 IDE 启动，控制台无法接管；或改 `ports.conf` |
| 启动脚本不存在 | `services/` 没拷过来，或前端/WAR 项目没在控制台里重新注册 |
| 前端启动失败 | 缺 pnpm/npm，或 `install` 列没写 `@pnpm` 等标记 |
| 18000 被占用 | 关掉旧控制台；或 `TXLC_DASHBOARD_PORT=18001 python3 dashboard.py` |
| macOS 打不开 App | 右键 →「打开」 |
| Windows 中文乱码 | `install.cmd` / `devctl.cmd` 已 `chcp 65001`；仍异常时把终端切到 UTF-8 |
