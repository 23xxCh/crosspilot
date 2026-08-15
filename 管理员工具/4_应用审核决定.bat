@echo off
setlocal
chcp 65001 >nul
call "%~dp0..\00_常用入口\02_应用审核结果.bat" %*
exit /b %errorlevel%
