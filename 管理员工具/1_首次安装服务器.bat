@echo off
setlocal
chcp 65001 >nul
call "%~dp0..\00_常用入口\04_一键安装服务器.bat"
exit /b %errorlevel%
