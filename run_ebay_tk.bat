@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist keys.json (
    if not exist agnes_key.txt (
        echo [错误] 缺少 keys.json，请复制 keys.example.json 为 keys.json 并填入密钥
        pause
        exit /b 1
    )
)

echo ============================================
echo  eBay - TikTok Shop 越南站 表格清洗
echo  拖单个 xlsx: 处理该文件
echo  拖文件夹:   批量处理文件夹里所有 xlsx
echo ============================================
echo.

if "%~1"=="" (
    echo [错误] 请把 xlsx 文件或文件夹拖到本 bat 上
    pause
    exit /b 1
)

where uv >nul 2>nul
if %errorlevel%==0 (set PY=uv run python) else (set PY=python)

if exist "%~1\" (
    echo 批量模式: 处理文件夹 %~1 里的所有 xlsx
    %PY% -u scripts\batch_process.py "%~1"
) else (
    %PY% -u scripts\process_ebay_tk.py "%~1"
)

echo.
pause
