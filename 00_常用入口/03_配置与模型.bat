@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\AmazonProcessor\venv"
cd /d "%PROJECT_ROOT%"
where uv >nul 2>nul
if errorlevel 1 (
  echo 没有找到 uv，请先安装运行环境。
  pause
  exit /b 3
)
uv sync --quiet
if errorlevel 1 goto :failed
uv run python -m amazon_processor config
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo 配置管理中心启动失败，请保留上面的错误提示。
pause
exit /b 1
