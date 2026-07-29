@echo off
setlocal
if "%~1"=="" (
  echo Drag an Amazon JSON collection file onto this BAT.
  pause
  exit /b 2
)
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
uv run python -m amazon_processor run "%~1" --open
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Processing failed. See the error above.
pause
exit /b 1
