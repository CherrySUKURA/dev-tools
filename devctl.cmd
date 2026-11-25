@echo off
chcp 65001 >nul
rem Windows 入口：把参数原样转发给 devctl.py。
rem
rem 为什么还要 .cmd：
rem   用户和文档里习惯写 devctl 命令；Windows 不能直接执行 .py，
rem   这个包装负责找 Python（优先 py 启动器），再调用同目录的 devctl.py。
rem
rem 用法（与 macOS 的 devctl.sh 一致）：
rem   devctl.cmd list
rem   devctl.cmd json
rem   devctl.cmd doctor [--env]
rem   devctl.cmd start all
rem   devctl.cmd stop core

setlocal
set "TOOLS_DIR=%~dp0"

rem 优先用 Windows 自带的 py 启动器（能自动选到 Python 3）。
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
  exit /b 1
)

%PY_CMD% "%TOOLS_DIR%devctl.py" %*
exit /b %errorlevel%
