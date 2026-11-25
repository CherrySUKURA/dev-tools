@echo off
chcp 65001 >nul
rem Windows 双击入口：等价于 macOS 的 服务控制台.app。
rem
rem 做三件事：
rem   1. 找到 Python 3；
rem   2. 调 launch.py 启动后台服务并等待就绪；
rem   3. 用 Edge/Chrome 的 --app 模式打开无地址栏窗口。
rem
rem 建议给这个文件建桌面快捷方式，图标可用 app\AppIcon.icns 转换后的 .ico。

setlocal
set "HERE=%~dp0"

set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  set "PY_CMD=py -3"
) else (
  where python >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
  echo 找不到 Python 3：请安装 Python 并勾选 "Add python.exe to PATH" 1>&2
  pause
  exit /b 1
)

%PY_CMD% "%HERE%launch.py"
if errorlevel 1 pause
exit /b %errorlevel%
