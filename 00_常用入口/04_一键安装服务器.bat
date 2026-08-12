@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"

echo ====================================
echo Amazon 自动处理系统 - 一键安装
echo ====================================
echo.
echo 接下来 Windows 会询问是否允许管理员权限，请选择“是”。
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass ^
  -File "%PROJECT_ROOT%\90_系统工具\服务器后台\安装后台服务.ps1"

if errorlevel 1 (
  echo.
  echo 安装没有完成，请把这个窗口中的提示发给管理员。
  pause
  exit /b 1
)

echo.
echo 安装完成。系统以后会随 Windows 自动启动。
echo 等待约 1 分钟后，双击“05_查看系统状态.bat”即可查看。
pause
exit /b 0
