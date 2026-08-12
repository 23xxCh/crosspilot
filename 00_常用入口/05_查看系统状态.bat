@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\AmazonProcessor\venv"
set "UV_EXE=uv"

where uv >nul 2>nul
if errorlevel 1 (
  set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
  if not exist "%UV_EXE%" (
    echo 系统还没有安装运行环境，请先双击 04_一键安装服务器.bat
    pause
    exit /b 1
  )
)

if not exist "%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe" (
  echo 系统还没有完成安装，请先双击 04_一键安装服务器.bat
  pause
  exit /b 1
)

"%UV_EXE%" run python -m amazon_processor system-status
echo.
pause
exit /b 0
