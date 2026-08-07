@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\AmazonProcessor\venv"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv was not found. Install it before registering this worker.
  exit /b 3
)

if not exist "%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe" (
  uv sync --frozen --quiet
  if errorlevel 1 exit /b 4
)

rem Server mode: no browser, no pause, one input at a time.
uv run python -m amazon_processor worker ^
  --input-dir "%~dp0\01_输入采集表" ^
  --poll-seconds 15 ^
  --stable-seconds 5 ^
  --max-retries 3 ^
  --retry-base-seconds 30 ^
  --timeout-hours 24
exit /b %errorlevel%
