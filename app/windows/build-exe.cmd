@echo off
chcp 65001 >nul
rem 用 PyInstaller 把控制台打包成单目录 exe。
rem
rem 状态：脚本已写好，但还没有在真实 Windows 机器上验证（见 README「已知限制」）。
rem 产物：
rem   dist\TXLCConsole\TXLCConsole.exe   双击即可启动（后台服务 + Edge/Chrome 窗口）
rem
rem 为什么是 --onedir 而不是 --onefile：
rem   配置、日志、services 脚本都要放在 exe 同级目录，用户能直接看到和备份；
rem   --onefile 每次启动解压到临时目录，这些文件会跟着临时目录一起消失。

setlocal
cd /d "%~dp0..\.."

set "PY_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  set "PY_CMD=py -3"
) else (
  where python >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
  echo 找不到 Python 3，无法打包 1>&2
  exit /b 1
)

%PY_CMD% -m pip install --upgrade pyinstaller || exit /b 1

rem devctl.py 是运行时用 import 进来的（--devctl 分发），必须显式声明 hidden import。
%PY_CMD% -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name TXLCConsole ^
  --hidden-import devctl ^
  dashboard.py || exit /b 1

rem 运行时要读的静态文件与配置：放到 exe 同级目录。
copy /Y dashboard.html dist\TXLCConsole\ >nul
copy /Y maven-settings.xml dist\TXLCConsole\ >nul 2>nul
for %%f in (projects.conf ports.conf deps.conf entries.conf health.conf legacy-proxy.conf) do (
  if exist %%f copy /Y %%f dist\TXLCConsole\ >nul
)
if not exist dist\TXLCConsole\logs mkdir dist\TXLCConsole\logs
if not exist dist\TXLCConsole\services mkdir dist\TXLCConsole\services

echo.
echo 已生成：dist\TXLCConsole\TXLCConsole.exe
echo 首次运行可能被 SmartScreen 拦截，请选择「更多信息 - 仍要运行」。
