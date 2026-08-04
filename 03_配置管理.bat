@echo off
setlocal
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\AmazonProcessor\venv"
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo uv was not found. Install it from https://docs.astral.sh/uv/
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
echo Configuration manager failed. See the error above.
pause
exit /b 1
