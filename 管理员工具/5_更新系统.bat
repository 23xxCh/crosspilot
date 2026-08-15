@echo off
setlocal
chcp 65001 >nul
call "%~dp0..\00_常用入口\07_更新系统.bat"
exit /b %errorlevel%
