@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\AmazonProcessor\venv"
set "UV_EXE=uv"
set "BUNDLED_UV=%PROJECT_ROOT%\90_系统工具\运行环境\uv.exe"

if exist "%BUNDLED_UV%" (
  set "UV_EXE=%BUNDLED_UV%"
) else (
  where uv >nul 2>nul
  if errorlevel 1 (
    set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
    if not exist "%UV_EXE%" (
      echo 找不到运行器，请重新复制完整的服务器部署包。
      pause
      exit /b 1
    )
  )
)

if not exist "%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe" (
  echo 首次查看状态，正在自动准备运行环境，请稍候...
  "%UV_EXE%" sync --frozen --quiet
  if errorlevel 1 (
    echo 运行环境准备失败，请把本窗口内容发给管理员。
    pause
    exit /b 1
  )
)

"%UV_EXE%" run python -m amazon_processor system-status
echo.
pause
exit /b 0
