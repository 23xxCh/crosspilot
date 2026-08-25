@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\install_skill.ps1"
if errorlevel 1 (
  echo.
  echo Skill 注册失败，请把上方错误发给管理员。
  pause
  exit /b 1
)
echo.
echo 以后把 Amazon 采集表路径交给 Agent，并要求“处理这个 Amazon 采集表”即可。
pause
