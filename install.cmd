@echo off
rem 首次初始化脚本（Windows）。
rem
rem 与 install.sh 做同样的事，只是命令换成 cmd 写法：
rem   1. 从 *.example 复制出本机配置（已存在的不覆盖）
rem   2. 建好 services\ logs\ tmp\
rem   3. 检查 Python / Java / Maven / Git / Node
rem
rem 可选 --scan：扫描 Maven 工作区生成 projects.conf 候选。
rem 注意：本脚本尚未在 Windows 真机验证，首次使用请留意输出。
rem
rem 用法：
rem   install.cmd
rem   install.cmd --scan C:\work
rem
rem 退出码：0 = 完成；1 = 关键依赖缺失；2 = 参数错误。

setlocal enabledelayedexpansion
cd /d "%~dp0" || exit /b 1

set "SCAN=0"
set "ROOTS="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--scan" (
    set "SCAN=1"
    shift
    goto parse
)
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
if /i "%~1"=="--" (
    shift
    goto parse
)
if "%SCAN%"=="0" (
    echo 未知参数：%~1
    echo 用法：install.cmd [--scan 目录...]
    exit /b 2
)
rem 目录参数统一加引号，避免路径里有空格。
set "ROOTS=!ROOTS! "%~1""
shift
goto parse

:parsed
if "%SCAN%"=="1" if "%ROOTS%"=="" (
    echo --scan 需要至少一个目录，例如：install.cmd --scan C:\work
    exit /b 2
)

echo TXLC 本地服务控制台 · 初始化
echo 目录：%CD%

echo.
echo == 配置模板 ==
if "%SCAN%"=="0" (
    if not exist projects.conf (
        copy /y projects.conf.example projects.conf >nul
        echo   OK   创建 projects.conf（来自模板，请改成你的路径）
    ) else (
        echo   OK   projects.conf 已存在，保持不变
    )
) else (
    if exist projects.conf (
        echo   OK   projects.conf 已存在，扫描结果不会覆盖它
    ) else (
        echo   OK   projects.conf 尚未创建，将直接写入扫描结果
    )
)

rem 其余配置文件：存在就跳过，避免覆盖用户已经填好的内网地址。
for %%F in (deps ports entries health legacy-proxy) do (
    if not exist "%%F.conf" (
        if exist "%%F.conf.example" (
            copy /y "%%F.conf.example" "%%F.conf" >nul
            echo   OK   创建 %%F.conf
        ) else (
            echo   WARN 缺少 %%F.conf.example，跳过
        )
    ) else (
        echo   OK   %%F.conf 已存在，保持不变
    )
)

if not exist maven-settings.xml if exist maven-settings.xml.example (
    copy /y maven-settings.xml.example maven-settings.xml >nul
    echo   OK   创建 maven-settings.xml（仅内网 Maven 仓库需要填写）
)

if not exist services mkdir services
if not exist logs mkdir logs
if not exist tmp mkdir tmp
echo   OK   运行目录 services\ logs\ tmp\ 已就绪

rem ---------------------------------------------------------------
rem 扫描工作区（可选）
rem ---------------------------------------------------------------
if "%SCAN%"=="1" (
    echo.
    echo == 扫描 Maven 项目 ==
    where python >nul 2>nul
    if errorlevel 1 (
        echo   FAIL 找不到 python，无法扫描
    ) else (
        set "TMP_OUT=%TEMP%\txlc-discover.txt"
        python discover.py !ROOTS! > "!TMP_OUT!"
        if errorlevel 1 (
            echo   FAIL discover.py 执行失败，可稍后手动运行：python discover.py ^<目录^>
        ) else (
            rem 候选行不以 # 开头；没有候选时只保留注释。
            findstr /R /C:"^[^#]" "!TMP_OUT!" >nul
            if errorlevel 1 (
                echo   WARN 没有识别到 Spring Boot 项目，保留手写配置的方式
                if not exist projects.conf (
                    copy /y projects.conf.example projects.conf >nul
                    echo   OK   回退使用 projects.conf.example 模板
                )
            ) else (
                if exist projects.conf (
                    copy /y "!TMP_OUT!" projects.conf.candidate >nul
                    echo   OK   已有配置，扫描结果写入 projects.conf.candidate（确认后再替换）
                ) else (
                    copy /y "!TMP_OUT!" projects.conf >nul
                    echo   OK   已写入 projects.conf
                )
            )
        )
        del "!TMP_OUT!" >nul 2>nul
    )
)

rem ---------------------------------------------------------------
rem 环境检查
rem ---------------------------------------------------------------
echo.
echo == 环境检查 ==
where python >nul 2>nul
if errorlevel 1 (
    echo   FAIL 未找到 Python 3.8+；控制台无法启动
    set "MISSING=1"
) else (
    for /f "delims=" %%V in ('python -c "import platform;print(platform.python_version())" 2^>nul') do echo   OK   Python %%V
)

where java >nul 2>nul
if errorlevel 1 (
    echo   WARN 未找到 java（启动后端服务需要 JDK 17+）
) else (
    echo   OK   Java 可用
)

rem Maven 可能不在 PATH，也可能解压在 dev-tools 下。
where mvn >nul 2>nul
if errorlevel 1 (
    if exist "apache-maven-*\bin\mvn.cmd" (
        echo   OK   Maven 可用（dev-tools 目录内）
    ) else (
        echo   WARN 未找到 mvn（构建/更新后端项目时需要）
    )
) else (
    echo   OK   Maven 可用
)

where git >nul 2>nul
if errorlevel 1 (
    echo   WARN 未找到 git（update / gitpull 等功能会不可用）
) else (
    echo   OK   Git 可用
)

where node >nul 2>nul
if errorlevel 1 (
    echo   INFO 未找到 node（只有前端项目需要）
) else (
    for /f "delims=" %%V in ('node --version 2^>nul') do echo   OK   Node %%V
)

rem ---------------------------------------------------------------
rem 下一步
rem ---------------------------------------------------------------
echo.
echo == 下一步 ==
echo   1. 检查 projects.conf 里的路径是否为本机路径
echo   2. python devctl.py doctor        ^(环境与配置自检^)
echo   3. devctl.cmd start all           ^(启动全部项目^)
echo   4. 打开 http://127.0.0.1:18000

echo.
if defined MISSING (
    echo 初始化未完成：请先安装 Python 3.8+。
    exit /b 1
)
echo 初始化完成。
exit /b 0

:usage
echo 用法：install.cmd [--scan 目录...]
echo.
echo   --scan 目录   扫描 Maven 工作区，生成 projects.conf 候选行
echo   -h, --help    显示本帮助
exit /b 0
