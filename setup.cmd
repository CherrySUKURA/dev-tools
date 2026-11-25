@echo off
chcp 65001 >nul
call "%~dp0install.cmd"
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
