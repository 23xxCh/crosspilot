@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
if "%~1"=="" (
  echo 请把终审包导出的“审核决定.json”拖到这个文件上。
  pause
  exit /b 2
)
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
uv run python -m amazon_processor apply "%~1" --open
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo 应用审核结果失败，请保留上面的错误提示。
pause
exit /b 1
