"""PyInstaller 打包入口。双击 CrossPilot.exe → 自动启动服务 + 开浏览器。"""
import os, sys, threading, time, socket, webbrowser, json

# PyInstaller 打包后 __file__ 路径特殊，用 sys.executable 定位资源
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APPDATA = os.path.join(os.environ.get('APPDATA', BASE_DIR), 'CrossPilot')
os.makedirs(APPDATA, exist_ok=True)
os.makedirs(os.path.join(APPDATA, 'data'), exist_ok=True)
os.makedirs(os.path.join(APPDATA, 'data', 'uploads'), exist_ok=True)

# 首次启动：创建空 keys.json 模板（不复制示例文件，防止示例 key 误用）
KEYS_PATH = os.path.join(APPDATA, 'keys.json')
if not os.path.exists(KEYS_PATH):
    with open(KEYS_PATH, 'w', encoding='utf-8') as f:
        json.dump({
            "text_provider": "deepseek",
            "vision_provider": "agnes",
            "image_gen_provider": "agnes",
            "deepseek_key": "",
            "agnes_key": "",
        }, f, ensure_ascii=False, indent=2)

# 设置环境变量让 web 层用 APPDATA 路径
os.environ['CROSSPILOT_DATA_DIR'] = os.path.join(APPDATA, 'data')
os.environ['CROSSPILOT_KEYS_PATH'] = KEYS_PATH

# PyInstaller 把 --add-data 的文件解压到 sys._MEIPASS，加入搜索路径
if getattr(sys, 'frozen', False):
    sys.path.insert(0, sys._MEIPASS)
    sys.path.insert(0, os.path.join(sys._MEIPASS, 'scripts'))
sys.path.insert(0, BASE_DIR)

if len(sys.argv) >= 4 and sys.argv[1] == '--run-job':
    from web.runner import run_pipeline
    raise SystemExit(run_pipeline(sys.argv[2], sys.argv[3]))

from web.app import app
from web.updater import check_for_update, apply_update, write_version_file
from web import jobs, store


def find_free_port(start=8765):
    for p in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', p)) != 0:
                return p
    return start


def main():
    port = find_free_port()
    print(f"CrossPilot v{__import__('web').__version__}")
    print(f"Starting on http://localhost:{port}")

    # 启动时写 version.txt
    write_version_file()

    # 后台线程：检查更新
    def _update():
        time.sleep(3)
        try:
            info = check_for_update()
            if info:
                print(f"New version available: {info['version']}; waiting for active jobs...", flush=True)
                jobs.begin_drain()
                jobs.wait_until_idle()
                if apply_update(info['path']):
                    jobs.stop_monitor(cancel_running=False)
                    store.close()
                    print("Update helper started. CrossPilot will restart.")
                    os._exit(0)
                jobs.cancel_drain()
        except Exception:  # 更新检查失败不影响主流程
            pass

    threading.Thread(target=_update, daemon=True).start()

    def _open():
        time.sleep(1.5)
        try:
            webbrowser.open(f'http://localhost:{port}')
        except Exception:  # 浏览器打开失败，用户手动打开
            print(f"Browser open failed. Open http://localhost:{port} manually.")

    threading.Thread(target=_open, daemon=True).start()

    import uvicorn
    for attempt in range(3):
        try:
            uvicorn.run(app, host='127.0.0.1', port=port, log_level='info')
            break
        except OSError:
            if attempt < 2:
                port = find_free_port(port + 1)
                print(f"Port busy, retrying on {port}...")
            else:
                raise


if __name__ == '__main__':
    main()
