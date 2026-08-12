@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ^
  "%PROJECT_ROOT%\90_系统工具\服务器后台\更新系统.ps1"
if errorlevel 1 (
  echo.
  echo 更新失败，旧版本已恢复或保持不变。请查看上方原因。
) else (
  echo.
  echo 更新成功，后台服务已恢复。
)
pause
