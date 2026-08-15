@echo off
setlocal
chcp 65001 >nul
call "%~dp0..\00_常用入口\03_配置与模型.bat"
exit /b %errorlevel%
