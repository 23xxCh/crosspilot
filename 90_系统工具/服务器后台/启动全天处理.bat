@echo off
setlocal
chcp 65001 >nul
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
if not exist "%PROJECT_ROOT%\.runtime\server\logs" mkdir "%PROJECT_ROOT%\.runtime\server\logs"
set "STARTUP_LOG=%PROJECT_ROOT%\.runtime\server\logs\worker_startup.log"
echo [%date% %time%] Worker launcher started>>"%STARTUP_LOG%"
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\AmazonProcessor\venv"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "UV_EXE=uv"
set "BUNDLED_UV=%PROJECT_ROOT%\90_系统工具\运行环境\uv.exe"

if exist "%BUNDLED_UV%" (
  set "UV_EXE=%BUNDLED_UV%"
) else (
  where uv >nul 2>nul
  if errorlevel 1 (
  set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
  if not exist "%UV_EXE%" (
    echo uv was not found. Install it before registering this worker.
    exit /b 3
  )
  )
)

if not exist "%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe" (
  "%UV_EXE%" sync --frozen --quiet >>"%STARTUP_LOG%" 2>&1
  if errorlevel 1 exit /b 4
)

if /I "%~1"=="status" (
  "%UV_EXE%" run python -m amazon_processor worker-status --max-age-seconds 120
  if errorlevel 1 exit /b 2
  exit /b 0
)

rem Server mode: no browser, no pause, one input at a time.
"%UV_EXE%" run python -m amazon_processor worker --input-dir "%PROJECT_ROOT%\Amazon日常操作\1_把采集表放这里" --poll-seconds 15 --stable-seconds 5 --max-retries 0 --retry-base-seconds 30 --blocked-retry-hours 6 --stall-minutes 45 --timeout-hours 24 >>"%STARTUP_LOG%" 2>&1
exit /b %errorlevel%
