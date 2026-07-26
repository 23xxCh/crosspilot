@echo off
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>&1
if %errorlevel%==0 (
    start "" http://localhost:8765
    uv run uvicorn web.app:app --host 127.0.0.1 --port 8765
) else (
    echo uv 未安装，尝试用 python...
    if not exist ".venv\Scripts\python.exe" (
        echo 请先安装 uv 或手动: python -m pip install fastapi "uvicorn[standard]" python-multipart
    )
    start "" http://localhost:8765
    python -m uvicorn web.app:app --host 127.0.0.1 --port 8765
)
